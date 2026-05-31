# app/api/trips.py
#
# Trip counter endpoints. Migrated from frontend Next.js API routes.
# Uses SUPABASE_SERVICE_ROLE_KEY to write user_trip_counts (the user's
# anon key cannot write this table directly).

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core.auth import AuthUser, get_current_user
from app.core.error_models import ErrorResponse
from app.core.supabase_admin import get_supabase_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/trips", tags=["trips"])


class MergeRequest(BaseModel):
    local_count: int = Field(default=0, ge=0)


class TripCountResponse(BaseModel):
    trips_used: int


# ── POST /trips/increment ────────────────────────────────────────


@router.post(
    "/increment",
    response_model=TripCountResponse,
    responses={500: {"model": ErrorResponse}},
)
async def increment_trip_count(
    user: AuthUser = Depends(get_current_user),
) -> TripCountResponse | JSONResponse:
    supa = get_supabase_admin()

    result = supa.rpc("increment_trip_count", {"p_user_id": user.id}).execute()

    if hasattr(result, "error") and result.error:
        logger.error("[trips/increment] %s", result.error)
        return JSONResponse(
            ErrorResponse(error="Failed to increment.").model_dump(),
            status_code=500,
        )

    return TripCountResponse(trips_used=int(result.data))


# ── POST /trips/merge ────────────────────────────────────────────


@router.post("/merge", response_model=TripCountResponse)
async def merge_trip_count(
    body: MergeRequest,
    user: AuthUser = Depends(get_current_user),
) -> TripCountResponse:
    supa = get_supabase_admin()

    # Clamp to sane range
    local_count = max(0, min(body.local_count, 100))

    # Get current server count
    existing = (
        supa.table("user_trip_counts")
        .select("trips_used")
        .eq("user_id", user.id)
        .maybe_single()
        .execute()
    )

    _data: dict = existing.data if existing and isinstance(existing.data, dict) else {}  # type: ignore[assignment]
    server_count: int = int(_data.get("trips_used", 0))
    merged = max(server_count, local_count)

    if merged > server_count:
        supa.table("user_trip_counts").upsert(
            {"user_id": user.id, "trips_used": merged},
            on_conflict="user_id",
        ).execute()

    return TripCountResponse(trips_used=merged)
