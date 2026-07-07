# app/api/account.py
#
# Account management endpoints.
# DELETE /account - permanently, irreversibly erases the authenticated user and
# every Glovebox row keyed to them. Delegates to the public.delete_glovebox_account
# SECURITY DEFINER RPC (the single canonical erase definition, shared with the web
# app and the native apps) so the deletion is atomic and enumerates every app table
# explicitly rather than relying on implicit auth.users cascades (roam_plan_invites
# is NO ACTION and would otherwise block the delete). Required for Apple guideline
# 5.1.1(v), Google Play data-deletion policy, and GDPR Art 17.

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

    Calls the public.delete_glovebox_account RPC as service_role, passing the
    authenticated user's id. The RPC deletes, in one atomic transaction, every
    Glovebox user-keyed row (public_trips, public_trip_clones, roam_plans,
    roam_plan_members, roam_plan_invites, saved_places, stop_memories,
    emergency_contacts, user_trip_counts, user_entitlements, entitlements) and
    the auth.users row - which cascades auth.identities, including the
    custom:friend link for THIS app (the separate Friend IdP account is untouched).
    """
    supa = get_supabase_admin()

    try:
        # Single canonical erase: atomic, enumerates every table, removes auth user.
        supa.rpc("delete_glovebox_account", {"p_user_id": user.id}).execute()
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
