# app/api/presence.py
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.auth import AuthUser, get_current_user
from app.core.contracts import (
    NearbyQuery,
    NearbyResponse,
    PresencePingRequest,
    PresencePingResponse,
)
from app.services.presence import Presence

router = APIRouter(prefix="/presence")


def get_cache_conn():
    raise RuntimeError("cache conn must be provided by app dependency override")


def get_presence_service(cache_conn=Depends(get_cache_conn)) -> Presence:
    return Presence(conn=cache_conn)


@router.post("/ping", response_model=PresencePingResponse)
def ping(
    req: PresencePingRequest,
    user: AuthUser = Depends(get_current_user),
    svc: Presence = Depends(get_presence_service),
) -> PresencePingResponse:
    """Upsert the user's latest known position."""
    svc.ping(
        user_id=user.id,
        lat=req.lat,
        lng=req.lng,
        speed_kmh=req.speed_kmh,
        heading_deg=req.heading_deg,
    )
    return PresencePingResponse(ok=True)


@router.post("/nearby", response_model=NearbyResponse)
def nearby(
    req: NearbyQuery,
    user: AuthUser = Depends(get_current_user),
    svc: Presence = Depends(get_presence_service),
) -> NearbyResponse:
    """Find other roamers predicted to be nearby (dead-reckoning)."""
    roamers = svc.nearby(
        user_id=user.id,
        lat=req.lat,
        lng=req.lng,
        radius_km=req.radius_km,
    )
    return NearbyResponse(roamers=roamers)
