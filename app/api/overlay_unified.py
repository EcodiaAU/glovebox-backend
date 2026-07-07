# app/api/overlay_unified.py
"""
Unified overlay endpoint that mobile clients (iOS, Android) call instead of
the 18 typed per-kind routes. Each typed route on the backend returns a
rich domain shape ({stations, incidents, hotspots, gauges, cameras, ...})
which the mobile clients cannot decode because they expect a flat
{items, source, fetched_at} OverlayPack. This route bridges the gap by
dispatching to the right typed service and flattening the response via
app.services.overlay_normalize.

Adding a new overlay kind is two files: the typed service + service call
here in the dispatcher + a mapper in overlay_normalize.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.contracts import BBox4
from app.core.errors import bad_request
from app.services.air_quality import AirQuality
from app.services.bushfire import Bushfire
from app.services.coverage import Coverage
from app.services.elevation import Elevation
from app.services.emergency_services import EmergencyServices
from app.services.flood import Flood
from app.services.fuel import Fuel
from app.services.hazards import Hazards
from app.services.heritage import Heritage
from app.services.overlay_normalize import normalize
from app.services.rest_areas import RestAreas
from app.services.roadkill import Roadkill
from app.services.route_score import RouteScore
from app.services.school_zones import SchoolZones
from app.services.speed_cameras import SpeedCameras
from app.services.toilets import Toilets
from app.services.traffic import Traffic
from app.services.weather import Weather
from app.services.wildlife import Wildlife

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/nav")


# ──────────────────────────────────────────────────────────────
# Dependency factory (overridden in main.py with real cache conn)
# ──────────────────────────────────────────────────────────────


def get_cache_conn() -> sqlite3.Connection:
    raise RuntimeError("cache conn must be provided by app dependency override")


# ──────────────────────────────────────────────────────────────
# Unified request shape
# ──────────────────────────────────────────────────────────────


class OverlayUnifiedRequest(BaseModel):
    """Single request shape for every overlay kind. Optional fields are only
    read by services that need them; e.g. bbox is required for hazards/flood
    while polyline6 is required for along-route services."""

    polyline6: Optional[str] = None
    bbox: Optional[BBox4] = None
    buffer_km: Optional[float] = Field(default=None, ge=0.5, le=200.0)
    departure_iso: Optional[str] = None
    avg_speed_kmh: float = Field(default=80.0, gt=0)


# ──────────────────────────────────────────────────────────────
# Dispatcher
# ──────────────────────────────────────────────────────────────


async def _fetch_overlay(
    kind: str,
    req: OverlayUnifiedRequest,
    cache_conn: sqlite3.Connection,
) -> Any:
    """Dispatch to the right service for the given kind. Returns the typed
    overlay object so overlay_normalize can flatten it."""

    polyline = req.polyline6
    bbox = req.bbox
    buffer_km = req.buffer_km

    if kind == "fuel-along-route":
        if not polyline:
            bad_request("bad_overlay_request", "polyline6 required for fuel")
        return await Fuel(conn=cache_conn).along_route(
            polyline6=polyline, buffer_km=buffer_km or 20.0
        )

    if kind == "rest-areas":
        if not polyline:
            bad_request("bad_overlay_request", "polyline6 required for rest-areas")
        return await RestAreas(conn=cache_conn).along_route(polyline6=polyline)

    if kind == "weather":
        if not polyline:
            bad_request("bad_overlay_request", "polyline6 required for weather")
        departure = req.departure_iso or datetime.now(timezone.utc).isoformat()
        return await Weather(conn=cache_conn).forecast_along_route(
            polyline6=polyline,
            departure_iso=departure,
            avg_speed_kmh=req.avg_speed_kmh,
        )

    if kind == "hazards":
        if not bbox:
            bad_request("bad_overlay_request", "bbox required for hazards")
        return await Hazards(conn=cache_conn).poll(bbox=bbox)

    if kind == "traffic":
        if not bbox:
            bad_request("bad_overlay_request", "bbox required for traffic")
        return await Traffic(conn=cache_conn).poll(bbox=bbox)

    if kind == "flood":
        if not bbox:
            bad_request("bad_overlay_request", "bbox required for flood")
        return await Flood(conn=cache_conn).poll(bbox=bbox)

    if kind == "coverage":
        if not polyline:
            bad_request("bad_overlay_request", "polyline6 required for coverage")
        return await Coverage(conn=cache_conn).along_route(polyline6=polyline)

    if kind == "wildlife":
        if not polyline:
            bad_request("bad_overlay_request", "polyline6 required for wildlife")
        return await Wildlife(conn=cache_conn).along_route(
            polyline6=polyline,
            buffer_km=buffer_km or 5.0,
        )

    if kind == "emergency":
        if not polyline:
            bad_request("bad_overlay_request", "polyline6 required for emergency")
        return await EmergencyServices(conn=cache_conn).along_route(polyline6=polyline)

    if kind == "heritage":
        if not polyline:
            bad_request("bad_overlay_request", "polyline6 required for heritage")
        return await Heritage(conn=cache_conn).along_route(polyline6=polyline)

    if kind == "air-quality":
        if not polyline:
            bad_request("bad_overlay_request", "polyline6 required for air-quality")
        return await AirQuality(conn=cache_conn).along_route(polyline6=polyline)

    if kind == "bushfire":
        if not polyline:
            bad_request("bad_overlay_request", "polyline6 required for bushfire")
        return await Bushfire(conn=cache_conn).along_route(
            polyline6=polyline,
            buffer_km=buffer_km or 50.0,
        )

    if kind == "speed-cameras":
        if not polyline:
            bad_request("bad_overlay_request", "polyline6 required for speed-cameras")
        return await SpeedCameras(conn=cache_conn).along_route(polyline6=polyline)

    if kind == "toilets":
        if not polyline:
            bad_request("bad_overlay_request", "polyline6 required for toilets")
        return await Toilets(conn=cache_conn).along_route(polyline6=polyline)

    if kind == "school-zones":
        if not polyline:
            bad_request("bad_overlay_request", "polyline6 required for school-zones")
        return await SchoolZones(conn=cache_conn).along_route(polyline6=polyline)

    if kind == "roadkill":
        if not polyline:
            bad_request("bad_overlay_request", "polyline6 required for roadkill")
        return await Roadkill(conn=cache_conn).along_route(polyline6=polyline)

    if kind == "elevation":
        if not polyline:
            bad_request("bad_overlay_request", "polyline6 required for elevation")
        from app.core.contracts import ElevationRequest

        elev = Elevation()
        profile = elev.profile(ElevationRequest(geometry=polyline))
        return profile

    if kind == "route-score":
        if not polyline or not bbox:
            bad_request(
                "bad_overlay_request", "polyline6 + bbox required for route-score"
            )
        departure = req.departure_iso or datetime.now(timezone.utc).isoformat()
        scorer = RouteScore()
        traffic = Traffic(conn=cache_conn)
        hazards = Hazards(conn=cache_conn)
        weather = Weather(conn=cache_conn)
        flood = Flood(conn=cache_conn)
        fuel = Fuel(conn=cache_conn)
        rest = RestAreas(conn=cache_conn)
        coverage = Coverage(conn=cache_conn)
        wildlife = Wildlife(conn=cache_conn)
        results = await asyncio.gather(
            traffic.poll(bbox=bbox),
            hazards.poll(bbox=bbox),
            weather.forecast_along_route(
                polyline6=polyline,
                departure_iso=departure,
                avg_speed_kmh=req.avg_speed_kmh,
            ),
            flood.poll(bbox=bbox),
            fuel.along_route(polyline6=polyline),
            rest.along_route(polyline6=polyline),
            coverage.along_route(polyline6=polyline),
            wildlife.along_route(polyline6=polyline),
            return_exceptions=True,
        )

        def _safe(val: Any, name: str) -> Any:
            if isinstance(val, Exception):
                logger.warning("overlay route-score: %s overlay failed: %s", name, val)
                return None
            return val

        (
            traffic_ov,
            hazards_ov,
            weather_ov,
            flood_ov,
            fuel_ov,
            rest_ov,
            cov_ov,
            wild_ov,
        ) = results
        return scorer.compute(
            traffic=_safe(traffic_ov, "traffic"),
            hazards=_safe(hazards_ov, "hazards"),
            weather=_safe(weather_ov, "weather"),
            flood=_safe(flood_ov, "flood"),
            fuel=_safe(fuel_ov, "fuel"),
            rest=_safe(rest_ov, "rest"),
            coverage=_safe(cov_ov, "coverage"),
            wildlife=_safe(wild_ov, "wildlife"),
        )

    bad_request("unknown_overlay_kind", f"unknown overlay kind: {kind}")
    return None  # unreachable; bad_request always raises


# ──────────────────────────────────────────────────────────────
# Endpoint
# ──────────────────────────────────────────────────────────────


@router.post("/overlay/{kind}")
async def overlay_unified(
    kind: str,
    req: OverlayUnifiedRequest,
    cache_conn: sqlite3.Connection = Depends(get_cache_conn),
) -> dict:
    """
    Unified overlay endpoint for mobile clients. Dispatches to the right
    typed service and normalizes the response to a flat OverlayPack shape:

        {items: [{id, name, lat, lng, distanceKm?, severity?, description?}, ...],
         source: str, fetchedAt: str}

    Mobile callers iterate the items list and render pins. Empty items
    list means no entries returned by the underlying service (or the
    service errored out, in which case the failure was logged but not
    surfaced).
    """
    try:
        overlay = await _fetch_overlay(kind, req, cache_conn)
    except Exception as e:
        logger.warning("overlay %s fetch failed: %s", kind, e)
        # Return empty pack rather than raising so the bundle build keeps
        # going past failed kinds instead of aborting the whole download.
        return {
            "items": [],
            "source": kind,
            "fetchedAt": datetime.now(timezone.utc).isoformat(),
        }

    if overlay is None:
        return {
            "items": [],
            "source": kind,
            "fetchedAt": datetime.now(timezone.utc).isoformat(),
        }

    return normalize(kind, overlay, source=kind)
