from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.services.school_zones import SchoolZones, SchoolZonesOverlay
from app.core.errors import bad_request

router = APIRouter(prefix="/nav")


def get_cache_conn() -> sqlite3.Connection:
    raise RuntimeError("cache conn must be provided by app dependency override")


def get_school_zones_service(cache_conn: sqlite3.Connection = Depends(get_cache_conn)) -> SchoolZones:
    return SchoolZones(conn=cache_conn)


class SchoolZonesRequest(BaseModel):
    geometry: str
    buffer_km: float = Field(default=5.0, ge=0.5, le=20.0)


@router.post("/school-zones/along-route", response_model=SchoolZonesOverlay)
async def school_zones_along_route(
    req: SchoolZonesRequest,
    svc: SchoolZones = Depends(get_school_zones_service),
) -> SchoolZonesOverlay:
    if not req.geometry:
        bad_request("bad_school_zones_request", "geometry (polyline6) is required")

    return await svc.along_route(
        polyline6=req.geometry,
        buffer_km=req.buffer_km,
    )
