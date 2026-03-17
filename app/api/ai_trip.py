# app/api/ai_trip.py
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.core.errors import bad_request
from app.services.ai_trip import AiTripService

router = APIRouter(prefix="/ai")

_svc: AiTripService | None = None


def _get_svc() -> AiTripService:
    global _svc
    if _svc is None:
        _svc = AiTripService()
    return _svc


class AiTripRequest(BaseModel):
    vibe: str = Field(..., min_length=3, max_length=1000)


class AiTripStop(BaseModel):
    name: str
    lat: float
    lng: float
    reason: str = ""


class AiTripResponse(BaseModel):
    title: str
    stops: list[AiTripStop]


@router.post("/trip", response_model=AiTripResponse)
async def generate_trip(req: AiTripRequest) -> AiTripResponse:
    svc = _get_svc()
    try:
        result = await svc.generate(req.vibe)
        return AiTripResponse(
            title=result["title"],
            stops=[AiTripStop(**s) for s in result["stops"]],
        )
    except RuntimeError as e:
        bad_request("ai_trip_error", str(e))
        raise  # unreachable
