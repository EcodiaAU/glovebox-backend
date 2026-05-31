# app/api/account.py
#
# Account management endpoints.
# DELETE /account - permanently deletes the authenticated user and all
# associated data (cascade rules on auth.users handle table cleanup).

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core.auth import AuthUser, get_current_user
from app.core.error_models import ErrorResponse
from app.core.supabase_admin import get_supabase_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/account", tags=["account"])


class AccountDeleteResponse(BaseModel):
    deleted: bool


@router.delete(
    "",
    response_model=AccountDeleteResponse,
    responses={500: {"model": ErrorResponse}},
)
async def delete_account(
    user: AuthUser = Depends(get_current_user),
) -> AccountDeleteResponse | JSONResponse:
    """
    Permanently delete the authenticated user's account and all associated data.

    Supabase cascade rules on auth.users ensure all rows in user_entitlements,
    user_trip_counts, saved_places, roam_plans, roam_plan_members,
    emergency_contacts, and stop_memories are removed automatically.
    """
    supa = get_supabase_admin()

    try:
        # Supabase Admin API: delete the auth user (cascades to all app tables)
        supa.auth.admin.delete_user(user.id)
    except Exception as exc:
        logger.error("[account/delete] Failed to delete user %s: %s", user.id, exc)
        return JSONResponse(
            ErrorResponse(
                error="Failed to delete account. Please try again or contact support."
            ).model_dump(),
            status_code=500,
        )

    logger.info(
        "[account/delete] Deleted user %s (%s)", user.id, user.email or "no-email"
    )
    return AccountDeleteResponse(deleted=True)
