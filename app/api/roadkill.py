from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.services.roadkill import Roadkill, RoadkillOverlay
from app.core.errors import bad_request

router = APIRouter(prefix="/nav")


def get_cache_conn() -> sqlite3.Connection:
    raise RuntimeError("cache conn must be provided by app dependency override")


def get_roadkill_service(cache_conn: sqlite3.Connection = Depends(get_cache_conn)) -> Roadkill:
    return Roadkill(conn=cache_conn)


class RoadkillRequest(BaseModel):
    geometry: str
    buffer_km: float = Field(default=10.0, ge=1.0, le=30.0)


@router.post("/roadkill/along-route", response_model=RoadkillOverlay)
async def roadkill_along_route(
    req: RoadkillRequest,
    svc: Roadkill = Depends(get_roadkill_service),
) -> RoadkillOverlay:
    if not req.geometry:
        bad_request("bad_roadkill_request", "geometry (polyline6) is required")

    return await svc.along_route(
        polyline6=req.geometry,
        buffer_km=req.buffer_km,
    )
