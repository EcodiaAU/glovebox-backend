from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.contracts import HeritageOverlay
from app.core.errors import bad_request
from app.services.heritage import Heritage

router = APIRouter(prefix="/nav")


# ──────────────────────────────────────────────────────────────
# Dependency factory (overridden in main.py)
# ──────────────────────────────────────────────────────────────

def get_cache_conn() -> sqlite3.Connection:
    raise RuntimeError("cache conn must be provided by app dependency override")


def get_heritage_service(cache_conn: sqlite3.Connection = Depends(get_cache_conn)) -> Heritage:
    return Heritage(conn=cache_conn)


# ──────────────────────────────────────────────────────────────
# Request model
# ──────────────────────────────────────────────────────────────

class HeritageRequest(BaseModel):
    geometry: str  # polyline6 of the route
    buffer_km: float = Field(default=25.0, ge=1.0, le=100.0)


# ──────────────────────────────────────────────────────────────
# Endpoint
# ──────────────────────────────────────────────────────────────

@router.post("/heritage/along-route", response_model=HeritageOverlay)
async def heritage_along_route(
    req: HeritageRequest,
    svc: Heritage = Depends(get_heritage_service),
) -> HeritageOverlay:
    if not req.geometry:
        bad_request("bad_heritage_request", "geometry (polyline6) is required")

    return await svc.along_route(
        polyline6=req.geometry,
        buffer_km=req.buffer_km,
    )
