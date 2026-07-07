from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel


from app.core.contracts import (
    PlacesRequest,
    PlacesPack,
    PlaceCategory,
    CorridorPlacesRequest,
    PlacesSuggestRequest,
    PlacesSuggestResponse,
    StopSuggestionsRequest,
    StopSuggestionsResponse,
    density_budget_multiplier,
)
from app.core.errors import bad_request, not_found
from app.services.places import Places
from app.services.corridor import Corridor
from app.services.mapbox_geocoding import MapboxGeocoding

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/places")

_mapbox: MapboxGeocoding | None = None


def get_cache_conn():
    raise RuntimeError("cache conn must be provided by app dependency override")


def _get_mapbox(conn=None) -> MapboxGeocoding:
    global _mapbox
    if _mapbox is None:
        _mapbox = MapboxGeocoding(conn=conn)
    elif conn is not None and _mapbox.conn is None:
        _mapbox.conn = conn
    return _mapbox


def get_places_service() -> Places:
    raise RuntimeError("Places must be provided by app dependency override")


def get_corridor_service() -> Corridor:
    raise RuntimeError("Corridor must be provided by app dependency override")


# ──────────────────────────────────────────────────────────────
# Default category sets - tiered by use case
# ──────────────────────────────────────────────────────────────

_CORRIDOR_DEFAULT_CATS: list[PlaceCategory] = [
    # ── Essentials & safety (non-negotiable for remote driving) ──
    "fuel",
    "ev_charging",
    "rest_area",
    "toilet",
    "water",
    "dump_point",
    "mechanic",
    "hospital",
    "pharmacy",
    # ── Supplies ──
    "grocery",
    "town",
    "atm",
    "laundromat",
    # ── Food & drink ──
    "bakery",
    "cafe",
    "restaurant",
    "fast_food",
    "pub",
    "bar",
    # ── Accommodation ──
    "camp",
    "hotel",
    "motel",
    "hostel",
    # ── Nature & outdoors ──
    "viewpoint",
    "waterfall",
    "swimming_hole",
    "beach",
    "national_park",
    "hiking",
    "picnic",
    "hot_spring",
    "cave",
    "fishing",
    "surf",
    # ── Family & recreation ──
    "playground",
    "pool",
    "zoo",
    "theme_park",
    "dog_park",
    "golf",
    "cinema",
    # ── Culture & sightseeing ──
    "visitor_info",
    "museum",
    "gallery",
    "heritage",
    "winery",
    "brewery",
    "attraction",
    "market",
    "park",
    "library",
    "showground",
]

_SUGGEST_DEFAULT_CATS: list[PlaceCategory] = [
    "fuel",
    "ev_charging",
    "rest_area",
    "water",
    "toilet",
    "bakery",
    "cafe",
    "restaurant",
    "fast_food",
    "pub",
    "camp",
    "motel",
    "hotel",
    "viewpoint",
    "waterfall",
    "swimming_hole",
    "beach",
    "national_park",
    "hiking",
    "picnic",
    "hot_spring",
    "cave",
    "fishing",
    "surf",
    "playground",
    "pool",
    "zoo",
    "visitor_info",
    "winery",
    "brewery",
    "attraction",
    "museum",
    "heritage",
    "market",
    "showground",
    "town",
]


# ──────────────────────────────────────────────────────────────
# /places/search
# ──────────────────────────────────────────────────────────────


@router.post("/search", response_model=PlacesPack)
def places_search(
    req: PlacesRequest,
    places: Places = Depends(get_places_service),
    cache_conn=Depends(get_cache_conn),
) -> PlacesPack:
    # Text query → try Mapbox geocoding first (forward search)
    if req.query and req.query.strip():
        proximity: tuple[float, float] | None = None
        if req.center:
            proximity = (req.center.lat, req.center.lng)

        bbox_tuple: tuple[float, float, float, float] | None = None
        if req.bbox:
            bbox_tuple = (
                req.bbox.minLng,
                req.bbox.minLat,
                req.bbox.maxLng,
                req.bbox.maxLat,
            )

        limit = min(req.limit or 10, 10)

        # Custom/partner POIs (Locals merchants etc.) matched by name, so a typed
        # business name finds the Ecosphere-listed business, not just Mapbox places.
        custom = places.custom_by_name(req.query.strip(), req.bbox, limit)

        try:
            mapbox = _get_mapbox(conn=cache_conn)
            pack = mapbox.search(
                query=req.query.strip(),
                proximity=proximity,
                limit=limit,
                bbox=bbox_tuple,
            )
            if custom:
                pack.items = places._merge_front(custom, pack.items)[:limit]
            return pack
        except RuntimeError as exc:
            logger.error("mapbox_search_failed: %s - falling back to overpass", exc)

    if not req.bbox and not (req.center and req.radius_m) and not req.query:
        bad_request("bad_places_request", "Provide bbox or center+radius_m or query")

    return places.search(req)


# ──────────────────────────────────────────────────────────────
# /places/along-route
#
# Mobile-friendly alias for /places/corridor that takes the simpler
# {polyline6, kinds, max_per_kind, buffer_km} shape that Android and iOS
# send. Calls Places.search_corridor_polyline directly so the result is
# balanced across the WHOLE route rather than clustered around a single
# bbox centre. Tate flagged "places clustered at the origin not balanced
# across the route" - that was the client falling back to /places/search
# with a bbox centred on the destination because /places/corridor required
# a corridor_key the client never built.
# ──────────────────────────────────────────────────────────────


class PlacesAlongRouteRequest(BaseModel):
    polyline6: str
    kinds: Optional[List[str]] = None  # mobile-side name
    categories: Optional[List[PlaceCategory]] = None  # backend name
    buffer_km: Optional[float] = 35.0
    max_per_kind: Optional[int] = 100
    limit: Optional[int] = None
    stop_density: int = 3


@router.post("/along-route", response_model=PlacesPack)
def places_along_route(
    req: PlacesAlongRouteRequest,
    places: Places = Depends(get_places_service),
) -> PlacesPack:
    """Balanced corridor search along the supplied polyline."""
    if not req.polyline6 or len(req.polyline6) < 10:
        bad_request("bad_places_request", "polyline6 is required")

    # categories takes precedence; kinds is the mobile alias
    cats: list[PlaceCategory] = (
        req.categories or [k for k in (req.kinds or []) if k] or _CORRIDOR_DEFAULT_CATS
    )

    buffer_km = float(req.buffer_km or 35.0)
    density_mult = density_budget_multiplier(req.stop_density)
    if req.limit:
        limit = int(max(1, int(req.limit) * density_mult))
    elif req.max_per_kind:
        # Translate per-kind cap into an overall budget that scales with
        # how many categories the caller asked for.
        limit = int(max(1, int(req.max_per_kind) * max(1, len(cats)) * density_mult))
    else:
        from app.services.places import _corridor_places_budget, _route_extent_km

        extent_km = _route_extent_km(req.polyline6)
        limit = int(max(1, _corridor_places_budget(extent_km) * density_mult))

    logger.info(
        "places_along_route: cats=%d buffer_km=%s limit=%d density=%d",
        len(cats),
        buffer_km,
        limit,
        req.stop_density,
    )

    return places.search_corridor_polyline(
        polyline6=req.polyline6,
        buffer_km=buffer_km,
        categories=cats,
        limit=limit,
        sample_interval_km=8.0,
    )


# ──────────────────────────────────────────────────────────────
# /places/corridor
# ──────────────────────────────────────────────────────────────


@router.post("/corridor", response_model=PlacesPack)
def places_corridor(
    req: CorridorPlacesRequest,
    corridor: Corridor = Depends(get_corridor_service),
    places: Places = Depends(get_places_service),
) -> PlacesPack:
    cats = req.categories or _CORRIDOR_DEFAULT_CATS

    # ── Direct attribute access (geometry is on the Pydantic model) ──
    geometry = req.geometry
    buffer_km = req.buffer_km or 35.0

    # ── Dynamic limit based on route extent + density ───────
    extent_km = 0.0
    _density_mult = density_budget_multiplier(req.stop_density)
    if req.limit:
        limit = int(max(1, int(req.limit) * _density_mult))
    elif geometry and len(geometry) > 10:
        from app.services.places import _corridor_places_budget, _route_extent_km

        extent_km = _route_extent_km(geometry)
        limit = int(max(1, _corridor_places_budget(extent_km) * _density_mult))
    else:
        limit = int(max(1, 2000 * _density_mult))  # fallback for bbox-only requests

    logger.info(
        "places_corridor: corridor_key=%s geometry=%s buffer_km=%s extent_km=%.0f limit=%d density=%d mult=%.2f",
        req.corridor_key[:16] if req.corridor_key else "?",
        f"polyline6[{len(geometry)}]" if geometry else "NONE",
        buffer_km,
        extent_km,
        limit,
        req.stop_density,
        _density_mult,
    )

    # ── Preferred path: route geometry provided ──────────────
    if geometry and len(geometry) > 10:
        logger.info("places_corridor: using POLYLINE path (search_corridor_polyline)")
        return places.search_corridor_polyline(
            polyline6=geometry,
            buffer_km=float(buffer_km),
            categories=cats,
            limit=limit,
            sample_interval_km=8.0,
        )

    # ── Fallback: corridor pack bbox ─────────────────────────
    logger.warning(
        "places_corridor: NO geometry - falling back to corridor bbox. "
        "This produces destination-biased results!"
    )
    cpack = corridor.get(req.corridor_key)
    if not cpack:
        not_found("corridor_missing", f"no corridor pack found for {req.corridor_key}")

    preq = PlacesRequest(
        bbox=cpack.bbox,  # type: ignore[union-attr]
        categories=cats,
        limit=limit,
    )
    return places.search(preq)


# ──────────────────────────────────────────────────────────────
# /places/suggest
# ──────────────────────────────────────────────────────────────


@router.post("/suggest", response_model=PlacesSuggestResponse)
def places_suggest(
    req: PlacesSuggestRequest,
    places: Places = Depends(get_places_service),
) -> PlacesSuggestResponse:
    cats = req.categories or _SUGGEST_DEFAULT_CATS
    _density_mult = density_budget_multiplier(req.stop_density)
    _scaled_limit = int(max(1, (req.limit_per_sample or 150) * _density_mult))
    try:
        clusters = places.suggest_along_route(
            polyline6=req.geometry,
            interval_km=int(req.interval_km or 50),
            radius_m=int(req.radius_m or 15000),
            categories=cats,
            limit_per_sample=_scaled_limit,
        )
    except ValueError as exc:
        bad_request("invalid_geometry", str(exc))
    return PlacesSuggestResponse(clusters=clusters)  # type: ignore[arg-type]


# ──────────────────────────────────────────────────────────────
# /places/stop-suggestions
# ──────────────────────────────────────────────────────────────


@router.post("/stop-suggestions", response_model=StopSuggestionsResponse)
def places_stop_suggestions(
    req: StopSuggestionsRequest,
    places: Places = Depends(get_places_service),
) -> StopSuggestionsResponse:
    """Return up to req.limit nearby POI suggestions for the trip stop list.

    Queries Overpass within the trip bounding box, scores candidates by
    proximity to the route midpoint and category diversity, returns top N.
    """
    midpoint = (req.midpoint.lat, req.midpoint.lng)
    suggestions = places.suggest_stops(
        bbox=req.bbox,
        midpoint=midpoint,
        existing_categories=req.existing_categories,
        limit=max(1, min(8, req.limit)),
    )
    return StopSuggestionsResponse(suggestions=suggestions)
