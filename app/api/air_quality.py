from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.contracts import AirQualityOverlay
from app.core.errors import bad_request
from app.services.air_quality import AirQuality

router = APIRouter(prefix="/nav")


# ──────────────────────────────────────────────────────────────
# Dependency factory (overridden in main.py)
# ──────────────────────────────────────────────────────────────

def get_cache_conn() -> sqlite3.Connection:
    raise RuntimeError("cache conn must be provided by app dependency override")


def get_aqi_service(cache_conn: sqlite3.Connection = Depends(get_cache_conn)) -> AirQuality:
    return AirQuality(conn=cache_conn)


# ──────────────────────────────────────────────────────────────
# Request model
# ──────────────────────────────────────────────────────────────

class AirQualityRequest(BaseModel):
    geometry: str  # polyline6 of the route
    sample_interval_km: float = Field(default=50.0, ge=10.0, le=200.0)


# ──────────────────────────────────────────────────────────────
# Endpoint
# ──────────────────────────────────────────────────────────────

@router.post("/air-quality/along-route", response_model=AirQualityOverlay)
async def air_quality_along_route(
    req: AirQualityRequest,
    svc: AirQuality = Depends(get_aqi_service),
) -> AirQualityOverlay:
    if not req.geometry:
        bad_request("bad_aqi_request", "geometry (polyline6) is required")

    return await svc.along_route(
        polyline6=req.geometry,
        sample_interval_km=req.sample_interval_km,
    )
