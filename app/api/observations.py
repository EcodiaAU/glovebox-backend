# app/api/observations.py
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.auth import AuthUser, get_current_user
from app.core.contracts import (
    NearbyObservationsQuery,
    NearbyObservationsResponse,
    ObservationSubmitRequest,
    ObservationSubmitResponse,
)
from app.services.observations import Observations

router = APIRouter(prefix="/observations")


def get_cache_conn():
    raise RuntimeError("cache conn must be provided by app dependency override")


def get_observations_service(cache_conn=Depends(get_cache_conn)) -> Observations:
    return Observations(conn=cache_conn)


@router.post("/submit", response_model=ObservationSubmitResponse)
def submit_observation(
    req: ObservationSubmitRequest,
    user: AuthUser = Depends(get_current_user),
    svc: Observations = Depends(get_observations_service),
) -> ObservationSubmitResponse:
    """Submit a crowd-sourced road observation."""
    obs = svc.submit(
        user_id=user.id,
        type=req.type,
        severity=req.severity,
        lat=req.lat,
        lng=req.lng,
        heading_deg=req.heading_deg,
        message=req.message,
        value=req.value,
    )
    return ObservationSubmitResponse(id=obs.id, ok=True)


@router.post("/nearby", response_model=NearbyObservationsResponse)
def nearby_observations(
    req: NearbyObservationsQuery,
    svc: Observations = Depends(get_observations_service),
) -> NearbyObservationsResponse:
    """Query aggregated observations near a position."""
    aggregated = svc.nearby(
        lat=req.lat,
        lng=req.lng,
        radius_km=req.radius_km,
        types=req.types,  # type: ignore[arg-type]
        since_iso=req.since_iso,
    )
    return NearbyObservationsResponse(observations=aggregated)
