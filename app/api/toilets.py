from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.services.toilets import Toilets, ToiletsOverlay
from app.core.errors import bad_request

router = APIRouter(prefix="/nav")


def get_cache_conn() -> sqlite3.Connection:
    raise RuntimeError("cache conn must be provided by app dependency override")


def get_toilets_service(cache_conn: sqlite3.Connection = Depends(get_cache_conn)) -> Toilets:
    return Toilets(conn=cache_conn)


class ToiletsRequest(BaseModel):
    geometry: str
    buffer_km: float = Field(default=15.0, ge=1.0, le=50.0)


@router.post("/toilets/along-route", response_model=ToiletsOverlay)
async def toilets_along_route(
    req: ToiletsRequest,
    svc: Toilets = Depends(get_toilets_service),
) -> ToiletsOverlay:
    if not req.geometry:
        bad_request("bad_toilets_request", "geometry (polyline6) is required")

    return await svc.along_route(
        polyline6=req.geometry,
        buffer_km=req.buffer_km,
    )
