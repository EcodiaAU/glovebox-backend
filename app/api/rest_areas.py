from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.contracts import RestAreaOverlay
from app.core.errors import bad_request
from app.services.rest_areas import RestAreas

router = APIRouter(prefix="/nav")


# ──────────────────────────────────────────────────────────────
# Dependency factory
# ──────────────────────────────────────────────────────────────

def get_rest_areas_service() -> RestAreas:
    raise RuntimeError("RestAreas must be provided by app dependency override")


# ──────────────────────────────────────────────────────────────
# Request model
# ──────────────────────────────────────────────────────────────

class RestAreasRequest(BaseModel):
    geometry: str                           # polyline6 of the route
    sample_interval_km: float = Field(default=8.0, ge=1.0, le=100.0)
    buffer_km: float = Field(default=5.0, ge=0.5, le=50.0)


# ──────────────────────────────────────────────────────────────
# Endpoint
# ──────────────────────────────────────────────────────────────

@router.post("/rest-areas/along-route", response_model=RestAreaOverlay)
async def rest_areas_along_route(
    req: RestAreasRequest,
    svc: RestAreas = Depends(get_rest_areas_service),
) -> RestAreaOverlay:
    if not req.geometry:
        bad_request("bad_rest_areas_request", "geometry (polyline6) is required")

    return await svc.along_route(
        polyline6=req.geometry,
        sample_interval_km=req.sample_interval_km,
        buffer_km=req.buffer_km,
    )
