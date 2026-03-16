from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.contracts import EmergencyServicesOverlay
from app.core.errors import bad_request
from app.services.emergency_services import EmergencyServices

router = APIRouter(prefix="/nav")


# ──────────────────────────────────────────────────────────────
# Dependency factory (overridden in main.py)
# ──────────────────────────────────────────────────────────────

def get_cache_conn() -> sqlite3.Connection:
    raise RuntimeError("cache conn must be provided by app dependency override")


def get_emergency_service(cache_conn: sqlite3.Connection = Depends(get_cache_conn)) -> EmergencyServices:
    return EmergencyServices(conn=cache_conn)


# ──────────────────────────────────────────────────────────────
# Request model
# ──────────────────────────────────────────────────────────────

class EmergencyRequest(BaseModel):
    geometry: str  # polyline6 of the route
    buffer_km: float = Field(default=25.0, ge=1.0, le=100.0)


# ──────────────────────────────────────────────────────────────
# Endpoint
# ──────────────────────────────────────────────────────────────

@router.post("/emergency/along-route", response_model=EmergencyServicesOverlay)
async def emergency_along_route(
    req: EmergencyRequest,
    svc: EmergencyServices = Depends(get_emergency_service),
) -> EmergencyServicesOverlay:
    if not req.geometry:
        bad_request("bad_emergency_request", "geometry (polyline6) is required")

    return await svc.along_route(
        polyline6=req.geometry,
        buffer_km=req.buffer_km,
    )
