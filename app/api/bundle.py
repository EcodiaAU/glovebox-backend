from __future__ import annotations

import asyncio
import hashlib
import io
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.core.contracts import (
    BBox4,
    OfflineBundleManifest,
    RouteIntelligenceScore,
    TripPreferences,
    resolve_categories,
    density_budget_multiplier,
)
from app.core.errors import bad_request, not_found
from app.core.polyline6 import decode_polyline6
from app.core.settings import settings
from app.core.storage import get_manifest, put_score_pack
from app.core.time import utc_now_iso
from app.services import corridor_tiles
from app.services.bundle import Bundle
from app.services.corridor import Corridor
from app.services.coverage import Coverage
from app.services.flood import Flood
from app.services.fuel import Fuel
from app.services.hazards import Hazards
from app.services.places import Places
from app.services.rest_areas import RestAreas
from app.services.route_score import RouteScore
from app.services.traffic import Traffic
from app.services.weather import Weather
from app.services.wildlife import Wildlife
from app.services.emergency_services import EmergencyServices
from app.services.heritage import Heritage
from app.services.air_quality import AirQuality
from app.services.bushfire import Bushfire
from app.services.speed_cameras import SpeedCameras
from app.services.toilets import Toilets
from app.services.school_zones import SchoolZones
from app.services.roadkill import Roadkill

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/bundle")


def get_bundle_service() -> Bundle:
    raise RuntimeError("Bundle must be provided by app dependency override")


def get_corridor_service() -> Corridor:
    raise RuntimeError("Corridor must be provided by app dependency override")


def get_places_service() -> Places:
    raise RuntimeError("Places must be provided by app dependency override")


def get_cache_conn():
    raise RuntimeError("cache conn must be provided by app dependency override")


def get_traffic_service(cache_conn=Depends(get_cache_conn)) -> Traffic:
    return Traffic(conn=cache_conn)


def get_hazards_service(cache_conn=Depends(get_cache_conn)) -> Hazards:
    return Hazards(conn=cache_conn)


def get_weather_service(cache_conn=Depends(get_cache_conn)) -> Weather:
    return Weather(conn=cache_conn)


def get_coverage_service(cache_conn=Depends(get_cache_conn)) -> Coverage:
    return Coverage(conn=cache_conn)


def get_fuel_service(cache_conn=Depends(get_cache_conn)) -> Fuel:
    return Fuel(conn=cache_conn)


def get_flood_service(cache_conn=Depends(get_cache_conn)) -> Flood:
    return Flood(conn=cache_conn)


def get_wildlife_service(cache_conn=Depends(get_cache_conn)) -> Wildlife:
    return Wildlife(conn=cache_conn)


def get_rest_areas_service(cache_conn=Depends(get_cache_conn)) -> RestAreas:
    return RestAreas(conn=cache_conn)


def get_route_score_service() -> RouteScore:
    return RouteScore()


def get_emergency_service(cache_conn=Depends(get_cache_conn)) -> EmergencyServices:
    return EmergencyServices(conn=cache_conn)


def get_heritage_service(cache_conn=Depends(get_cache_conn)) -> Heritage:
    return Heritage(conn=cache_conn)


def get_air_quality_service(cache_conn=Depends(get_cache_conn)) -> AirQuality:
    return AirQuality(conn=cache_conn)


def get_bushfire_service(cache_conn=Depends(get_cache_conn)) -> Bushfire:
    return Bushfire(conn=cache_conn)


def get_speed_cameras_service(cache_conn=Depends(get_cache_conn)) -> SpeedCameras:
    return SpeedCameras(conn=cache_conn)


def get_toilets_service(cache_conn=Depends(get_cache_conn)) -> Toilets:
    return Toilets(conn=cache_conn)


def get_school_zones_service(cache_conn=Depends(get_cache_conn)) -> SchoolZones:
    return SchoolZones(conn=cache_conn)


def get_roadkill_service(cache_conn=Depends(get_cache_conn)) -> Roadkill:
    return Roadkill(conn=cache_conn)


class BundleBuildRequest(BaseModel):
    plan_id: str
    route_key: str
    geometry: str  # polyline6
    profile: str = "drive"
    buffer_m: int | None = None
    max_edges: int | None = None
    styles: list[str] = []
    departure_iso: str | None = None
    avg_speed_kmh: float = 90.0
    # Trip preferences - controls stop density & category filtering
    trip_prefs: TripPreferences | None = None
    # Phase-1 (navigate-now) build: skip the 16 live overlay Overpass/API queries
    # + the route score so the manifest returns as soon as the nav-critical packs
    # (places-for-corridor + corridor graph + corridor-tiles key) are ready. The
    # overlays are richness, not navigation, and are fetched by a later full build.
    nav_only: bool = False


@router.post("/build", response_model=OfflineBundleManifest)
async def build_bundle(
    req: BundleBuildRequest,
    bundle: Bundle = Depends(get_bundle_service),
    corridor: Corridor = Depends(get_corridor_service),
    places: Places = Depends(get_places_service),
    traffic: Traffic = Depends(get_traffic_service),
    hazards: Hazards = Depends(get_hazards_service),
    weather: Weather = Depends(get_weather_service),
    coverage: Coverage = Depends(get_coverage_service),
    fuel: Fuel = Depends(get_fuel_service),
    flood: Flood = Depends(get_flood_service),
    wildlife: Wildlife = Depends(get_wildlife_service),
    rest_areas: RestAreas = Depends(get_rest_areas_service),
    route_score: RouteScore = Depends(get_route_score_service),
    emergency_svc: EmergencyServices = Depends(get_emergency_service),
    heritage_svc: Heritage = Depends(get_heritage_service),
    air_quality_svc: AirQuality = Depends(get_air_quality_service),
    bushfire_svc: Bushfire = Depends(get_bushfire_service),
    speed_cameras_svc: SpeedCameras = Depends(get_speed_cameras_service),
    toilets_svc: Toilets = Depends(get_toilets_service),
    school_zones_svc: SchoolZones = Depends(get_school_zones_service),
    roadkill_svc: Roadkill = Depends(get_roadkill_service),
) -> OfflineBundleManifest:
    if not req.plan_id:
        bad_request("bad_bundle_request", "plan_id required")
    if not req.route_key:
        bad_request("bad_bundle_request", "route_key required")
    if not req.geometry:
        bad_request("bad_bundle_request", "geometry required")

    profile = req.profile or "drive"
    buffer_m = int(req.buffer_m or 5000)
    max_edges = int(req.max_edges or 2000000)

    # 1) Fetch places FIRST - we need stop coordinates for the corridor.
    #    search_bundle is sync (httpx.Client) so run in thread executor.
    #    Pass trip preferences to control density + category filtering.
    loop = asyncio.get_event_loop()
    ppack = None

    # Resolve categories and density from user preferences
    _trip_prefs = req.trip_prefs
    _enabled_cats = resolve_categories(_trip_prefs) if _trip_prefs else None
    _density_mult = (
        density_budget_multiplier(_trip_prefs.stop_density) if _trip_prefs else 1.0
    )

    try:
        ppack = await loop.run_in_executor(
            None,
            lambda: places.search_bundle(
                polyline6=req.geometry,
                categories=_enabled_cats,  # type: ignore[arg-type]
                density_multiplier=_density_mult,
            ),
        )
    except Exception as exc:
        logger.warning("bundle places fetch failed (non-fatal): %s", exc)

    # Extract stop coordinates for corridor building.
    #
    # CAP: a long route's places set can be ~1000+ POIs (e.g. Brisbane->Toowoomba
    # returns 1070). Tree-routing the corridor to every one of them produced a
    # 124k-edge graph in ~14s WARM, and on a cold container (roam-backend + OSRM
    # both scale to zero) the cumulative OSRM fan-out blew past Cloud Run's request
    # timeout -> the bundle "500" the client saw. The corridor exists for offline
    # reroute; the route spine + buffer already covers the whole route, so we only
    # need a BOUNDED set of off-spine stops for reach. Spatially stride-sample the
    # POIs down to a cap so the graph (and cold build time) stays small. The full
    # places set still ships in places.json for display - only the corridor stop
    # input is capped.
    CORRIDOR_MAX_STOP_COORDS = 60
    all_stop_coords: list[tuple[float, float]] = []
    if ppack and hasattr(ppack, "items") and ppack.items:
        for item in ppack.items:
            all_stop_coords.append((item.lat, item.lng))
    if len(all_stop_coords) > CORRIDOR_MAX_STOP_COORDS:
        # Stride-sample to preserve spread along the route (places come ordered
        # along the corridor), rather than taking the first N which would cluster
        # at the start.
        stride = len(all_stop_coords) / CORRIDOR_MAX_STOP_COORDS
        stop_coords = [
            all_stop_coords[int(i * stride)] for i in range(CORRIDOR_MAX_STOP_COORDS)
        ]
    else:
        stop_coords = all_stop_coords
    logger.info(
        "corridor stop_coords: %d (capped from %d) from places (ppack=%s)",
        len(stop_coords),
        len(all_stop_coords),
        type(ppack).__name__ if ppack else "None",
    )

    # 2) Build corridor using stop locations + route spine. A COLD build (OSRM +
    # roam-backend both scaling from zero) can exceed Cloud Run's request timeout
    # and previously RAISED -> the bundle "500" the client saw on the first
    # attempt (the second, warm, succeeded). Degrade gracefully: if the corridor
    # build fails, ship the bundle WITHOUT the offline-reroute corridor (route
    # spine + places + overlays still ship) rather than 500. It builds warm on the
    # next attempt; corridor_ready in the manifest tells the client.
    logger.info(">>> BUNDLE corridor.ensure with %d stop_coords", len(stop_coords))
    cmeta = None
    cpack = None
    try:
        ensure_result = corridor.ensure(
            route_key=req.route_key,
            route_polyline6=req.geometry,
            profile=profile,
            buffer_m=buffer_m,
            max_edges=max_edges,
            stop_coords=stop_coords,
        )
        cmeta = ensure_result.meta
        cpack = ensure_result.pack or corridor.get(cmeta.corridor_key)
    except Exception as exc:
        logger.warning(
            "bundle corridor.ensure failed (non-fatal; bundle ships without corridor): %s",
            exc,
        )

    # Bbox for the bbox-keyed overlays + corridor tiles. Prefer the corridor pack's
    # bbox; if the corridor build failed, derive it from the route geometry so the
    # overlays and the corridor-tiles key still resolve.
    if cpack is not None:
        corridor_bbox = cpack.bbox
    else:
        _coords = decode_polyline6(req.geometry) if req.geometry else []
        if _coords:
            _lats = [c[0] for c in _coords]
            _lngs = [c[1] for c in _coords]
            corridor_bbox = BBox4(
                minLng=min(_lngs), minLat=min(_lats),
                maxLng=max(_lngs), maxLat=max(_lats),
            )
        else:
            corridor_bbox = BBox4(minLng=0.0, minLat=0.0, maxLng=0.0, maxLat=0.0)

    # 3) All remaining overlays - run concurrently. Skipped entirely for a
    #    nav-only (phase-1) build: navigation needs only the corridor graph +
    #    nav pack + corridor tiles, so the 16 live Overpass/API overlay queries
    #    (the slow part, esp. cold) do not block the navigate-now manifest. A
    #    later full build (nav_only=false) fetches + caches them.
    tpack = hpack = wpack = cov_pack = fuel_pack = flood_pack = None
    wildlife_pack = rest_pack = emergency_pack = heritage_pack = aqi_pack = None
    bushfire_pack = cameras_pack = toilets_pack = school_zones_pack = roadkill_pack = None
    score_pack = None
    score_key = None

    async def _safe(coro_or_awaitable, name: str):
        """Await a coroutine; return None and log on any exception."""
        try:
            return await coro_or_awaitable
        except Exception as exc:
            logger.warning("bundle overlay '%s' failed (non-fatal): %s", name, exc)
            return None

    async def _maybe_weather():
        if not req.departure_iso:
            return None
        return await weather.forecast_along_route(
            polyline6=req.geometry,
            departure_iso=req.departure_iso,
            avg_speed_kmh=req.avg_speed_kmh,
        )

    async def _maybe_coverage():
        from app.core.settings import settings as _s

        if not _s.coverage_enabled:
            return None
        return await coverage.along_route(polyline6=req.geometry)

    async def _maybe_flood():
        from app.core.settings import settings as _s

        if not _s.flood_enabled:
            return None
        return await flood.poll(bbox=corridor_bbox)

    async def _maybe_wildlife():
        from app.core.settings import settings as _s

        if not _s.wildlife_enabled:
            return None
        return await wildlife.along_route(
            polyline6=req.geometry,
            departure_iso=req.departure_iso,
        )

    if not req.nav_only:
        (
            tpack,
            hpack,
            wpack,
            cov_pack,
            fuel_pack,
            flood_pack,
            wildlife_pack,
            rest_pack,
            emergency_pack,
            heritage_pack,
            aqi_pack,
            bushfire_pack,
            cameras_pack,
            toilets_pack,
            school_zones_pack,
            roadkill_pack,
        ) = await asyncio.gather(
            _safe(traffic.poll(bbox=corridor_bbox), "traffic"),
            _safe(hazards.poll(bbox=corridor_bbox), "hazards"),
            _safe(_maybe_weather(), "weather"),
            _safe(_maybe_coverage(), "coverage"),
            _safe(fuel.along_route(polyline6=req.geometry), "fuel"),
            _safe(_maybe_flood(), "flood"),
            _safe(_maybe_wildlife(), "wildlife"),
            _safe(rest_areas.along_route(polyline6=req.geometry), "rest_areas"),
            _safe(emergency_svc.along_route(polyline6=req.geometry), "emergency"),
            _safe(heritage_svc.along_route(polyline6=req.geometry), "heritage"),
            _safe(air_quality_svc.along_route(polyline6=req.geometry), "air_quality"),
            _safe(bushfire_svc.along_route(polyline6=req.geometry), "bushfire"),
            _safe(speed_cameras_svc.along_route(polyline6=req.geometry), "speed_cameras"),
            _safe(toilets_svc.along_route(polyline6=req.geometry), "toilets"),
            _safe(school_zones_svc.along_route(polyline6=req.geometry), "school_zones"),
            _safe(roadkill_svc.along_route(polyline6=req.geometry), "roadkill"),
        )

        # Route intelligence score - synchronous, uses the overlay results above.
        try:
            score_result = route_score.compute(
                weather=wpack,
                fuel=fuel_pack,
                flood=flood_pack,
                rest=rest_pack,
                coverage=cov_pack,
                wildlife=wildlife_pack,
                traffic=tpack,
                hazards=hpack,
            )
            # Stable key from route_key so the score caches + retrieves.
            score_key = "score_" + hashlib.sha1(req.route_key.encode()).hexdigest()[:16]
            from app.core.settings import settings as _s

            put_score_pack(
                bundle.conn,
                score_key=score_key,
                created_at=utc_now_iso(),
                algo_version=getattr(_s, "score_algo_version", "1"),
                pack=score_result.model_dump(),
            )
            score_pack = score_result
        except Exception as exc:
            logger.warning("route_score compute failed (non-fatal): %s", exc)

    # 4) Manifest
    return bundle.build_manifest(
        plan_id=req.plan_id,
        route_key=req.route_key,
        styles=req.styles,
        navpack_ready=True,
        corridor_key=(cmeta.corridor_key if cmeta else req.route_key),
        corridor_ready=(cpack is not None),
        places_key=(ppack.places_key if ppack else None),
        places_ready=(ppack is not None),
        traffic_key=(tpack.traffic_key if tpack else None),
        traffic_ready=(tpack is not None),
        hazards_key=(hpack.hazards_key if hpack else None),
        hazards_ready=(hpack is not None),
        weather_key=(wpack.weather_key if wpack else None),
        weather_ready=(wpack is not None),
        coverage_key=(cov_pack.coverage_key if cov_pack else None),
        coverage_ready=(cov_pack is not None),
        fuel_key=(fuel_pack.fuel_key if fuel_pack else None),
        fuel_ready=(fuel_pack is not None),
        flood_key=(flood_pack.flood_key if flood_pack else None),
        flood_ready=(flood_pack is not None),
        wildlife_key=(wildlife_pack.wildlife_key if wildlife_pack else None),
        wildlife_ready=(wildlife_pack is not None),
        rest_key=(rest_pack.rest_key if rest_pack else None),
        rest_ready=(rest_pack is not None),
        score_key=score_key,
        score_ready=(score_pack is not None),
        emergency_key=(emergency_pack.emergency_key if emergency_pack else None),
        emergency_ready=(emergency_pack is not None),
        heritage_key=(heritage_pack.heritage_key if heritage_pack else None),
        heritage_ready=(heritage_pack is not None),
        aqi_key=(aqi_pack.aqi_key if aqi_pack else None),
        aqi_ready=(aqi_pack is not None),
        bushfire_key=(bushfire_pack.bushfire_key if bushfire_pack else None),
        bushfire_ready=(bushfire_pack is not None),
        cameras_key=(cameras_pack.cameras_key if cameras_pack else None),
        cameras_ready=(cameras_pack is not None),
        toilets_key=(toilets_pack.toilets_key if toilets_pack else None),
        toilets_ready=(toilets_pack is not None),
        school_zones_key=(
            school_zones_pack.school_zones_key if school_zones_pack else None
        ),
        school_zones_ready=(school_zones_pack is not None),
        roadkill_key=(roadkill_pack.roadkill_key if roadkill_pack else None),
        roadkill_ready=(roadkill_pack is not None),
        # Per-trip street-zoom corridor tiles. The key is derived from the
        # corridor bbox so the bundle endpoint and the out-of-band producer
        # agree on it; readiness tracks the feature flag (the actual pack is
        # fetched best-effort at build_zip time, falling back gracefully).
        corridor_tiles_key=corridor_tiles.corridor_tiles_key(corridor_bbox),
        corridor_tiles_ready=settings.corridor_tiles_enabled,
        corridor_tiles_bbox=(
            f"{corridor_bbox.minLng},{corridor_bbox.minLat},"
            f"{corridor_bbox.maxLng},{corridor_bbox.maxLat}"
        ),
    )


class ScoreRefreshRequest(BaseModel):
    route_key: str
    bbox: BBox4


@router.post("/score/refresh", response_model=RouteIntelligenceScore)
async def refresh_score(
    req: ScoreRefreshRequest,
    bundle: Bundle = Depends(get_bundle_service),
    traffic: Traffic = Depends(get_traffic_service),
    hazards: Hazards = Depends(get_hazards_service),
    route_score: RouteScore = Depends(get_route_score_service),
) -> RouteIntelligenceScore:
    """Re-fetch traffic & hazards, recompute and cache the route intelligence score."""
    if not req.route_key:
        bad_request("bad_score_refresh_request", "route_key required")

    async def _safe(coro, name: str):
        try:
            return await coro
        except Exception as exc:
            logger.warning(
                "score_refresh overlay '%s' failed (non-fatal): %s", name, exc
            )
            return None

    tpack, hpack = await asyncio.gather(
        _safe(traffic.poll(bbox=req.bbox), "traffic"),
        _safe(hazards.poll(bbox=req.bbox), "hazards"),
    )

    score_result = route_score.compute(traffic=tpack, hazards=hpack)
    score_key = "score_" + hashlib.sha1(req.route_key.encode()).hexdigest()[:16]
    from app.core.settings import settings as _s

    put_score_pack(
        bundle.conn,
        score_key=score_key,
        created_at=utc_now_iso(),
        algo_version=getattr(_s, "score_algo_version", "1"),
        pack=score_result.model_dump(),
    )
    return score_result


@router.get("/{plan_id}", response_model=OfflineBundleManifest)
def get_bundle(
    plan_id: str, cache_conn=Depends(get_cache_conn)
) -> OfflineBundleManifest:
    row = get_manifest(cache_conn, plan_id)
    if not row:
        not_found("bundle_missing", f"no manifest for plan_id {plan_id}")
    return OfflineBundleManifest.model_validate(row)


@router.get(
    "/{plan_id}/download",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"application/zip": {}},
            "description": "Offline trip bundle zip (manifest + tiles + corridor + places + fuel)",
        },
        404: {"description": "Bundle not found for plan_id"},
    },
)
def download_bundle(
    plan_id: str,
    tier: str = "full",
    bundle: Bundle = Depends(get_bundle_service),
) -> StreamingResponse:
    # tier="nav" returns the small navigate-now zip (manifest + navpack +
    # corridor + corridor-tiles + places); tier="full" (default) returns the
    # complete bundle with all richness overlays. The client downloads "nav"
    # first to become offline-navigable immediately, then "full" in the
    # background. iOS unpack is idempotent + subset-tolerant, so the full zip
    # merges the overlays onto the already-unpacked nav packs.
    nav_only = tier == "nav"
    z = bundle.build_zip(plan_id=plan_id, nav_only=nav_only)
    return StreamingResponse(
        io.BytesIO(z.zip_bytes),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="roam_bundle_{plan_id}_{tier}.zip"'
        },
    )
