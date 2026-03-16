from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.contracts import SpeedCamerasOverlay
from app.core.errors import bad_request
from app.services.speed_cameras import SpeedCameras

router = APIRouter(prefix="/nav")


# ──────────────────────────────────────────────────────────────
# Dependency factory (overridden in main.py)
# ──────────────────────────────────────────────────────────────

def get_cache_conn() -> sqlite3.Connection:
    raise RuntimeError("cache conn must be provided by app dependency override")


def get_cameras_service(cache_conn: sqlite3.Connection = Depends(get_cache_conn)) -> SpeedCameras:
    return SpeedCameras(conn=cache_conn)


# ──────────────────────────────────────────────────────────────
# Request model
# ──────────────────────────────────────────────────────────────

class SpeedCamerasRequest(BaseModel):
    geometry: str  # polyline6 of the route
    buffer_km: float = Field(default=10.0, ge=1.0, le=50.0)


# ──────────────────────────────────────────────────────────────
# Endpoint
# ──────────────────────────────────────────────────────────────

@router.post("/speed-cameras/along-route", response_model=SpeedCamerasOverlay)
async def speed_cameras_along_route(
    req: SpeedCamerasRequest,
    svc: SpeedCameras = Depends(get_cameras_service),
) -> SpeedCamerasOverlay:
    if not req.geometry:
        bad_request("bad_cameras_request", "geometry (polyline6) is required")

    return await svc.along_route(
        polyline6=req.geometry,
        buffer_km=req.buffer_km,
    )
