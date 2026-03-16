from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.contracts import BushfireOverlay
from app.core.errors import bad_request
from app.services.bushfire import Bushfire

router = APIRouter(prefix="/nav")


# ──────────────────────────────────────────────────────────────
# Dependency factory (overridden in main.py)
# ──────────────────────────────────────────────────────────────

def get_cache_conn() -> sqlite3.Connection:
    raise RuntimeError("cache conn must be provided by app dependency override")


def get_bushfire_service(cache_conn: sqlite3.Connection = Depends(get_cache_conn)) -> Bushfire:
    return Bushfire(conn=cache_conn)


# ──────────────────────────────────────────────────────────────
# Request model
# ──────────────────────────────────────────────────────────────

class BushfireRequest(BaseModel):
    geometry: str  # polyline6 of the route
    buffer_km: float = Field(default=50.0, ge=5.0, le=200.0)


# ──────────────────────────────────────────────────────────────
# Endpoint
# ──────────────────────────────────────────────────────────────

@router.post("/bushfire/along-route", response_model=BushfireOverlay)
async def bushfire_along_route(
    req: BushfireRequest,
    svc: Bushfire = Depends(get_bushfire_service),
) -> BushfireOverlay:
    if not req.geometry:
        bad_request("bad_bushfire_request", "geometry (polyline6) is required")

    return await svc.along_route(
        polyline6=req.geometry,
        buffer_km=req.buffer_km,
    )
