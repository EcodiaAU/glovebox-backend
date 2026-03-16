from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.contracts import CoverageOverlay
from app.core.errors import bad_request
from app.services.coverage import Coverage

router = APIRouter(prefix="/nav")


# ──────────────────────────────────────────────────────────────
# Dependency factory (overridden in main.py)
# ──────────────────────────────────────────────────────────────

def get_cache_conn() -> sqlite3.Connection:
    raise RuntimeError("cache conn must be provided by app dependency override")


def get_coverage_service(cache_conn: sqlite3.Connection = Depends(get_cache_conn)) -> Coverage:
    return Coverage(conn=cache_conn)


# ──────────────────────────────────────────────────────────────
# Request model
# ──────────────────────────────────────────────────────────────

class CoverageRequest(BaseModel):
    geometry: str                                           # polyline6 of the route
    sample_interval_km: float = Field(default=5.0, ge=1.0, le=50.0)
    carriers: list[str] | None = None                       # e.g. ["Telstra", "Optus"]


# ──────────────────────────────────────────────────────────────
# Endpoint
# ──────────────────────────────────────────────────────────────

@router.post("/coverage/along-route", response_model=CoverageOverlay)
async def coverage_along_route(
    req: CoverageRequest,
    svc: Coverage = Depends(get_coverage_service),
) -> CoverageOverlay:
    if not req.geometry:
        bad_request("bad_coverage_request", "geometry (polyline6) is required")

    return await svc.along_route(
        polyline6=req.geometry,
        sample_interval_km=req.sample_interval_km,
        carriers=req.carriers,
    )
