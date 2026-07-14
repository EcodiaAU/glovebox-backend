# app/api/account.py
#
# Account management endpoints.
# DELETE /account - permanently deletes the authenticated user and all
# associated data by invoking the exhaustive `delete_glovebox_account` RPC
# (the single canonical Glovebox delete path, shared with web + iOS).

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

    Calls the SECURITY DEFINER `delete_glovebox_account` RPC (defined in
    glovebox/backend/migrations/012), which is the ONLY exhaustive Glovebox
    delete path and the same RPC web + iOS call. It enumerates every
    user-keyed table in explicit FK-safe order (public_trip_clones,
    roam_plan_members, roam_plan_invites, public_trips, roam_plans,
    saved_places, stop_memories, user_trip_counts, user_entitlements,
    entitlements) THEN deletes auth.users.

    This replaces the previous raw `auth.admin.delete_user`, which relied on
    implicit ON DELETE CASCADE and FK-failed on roam_plan_invites.created_by
    (NO ACTION) the moment a user had created a plan invite. Because this is a
    trusted service_role call (no JWT sub, so auth.uid() is null), the RPC
    takes the explicit p_user_id target, mirroring the increment_trip_count
    call in trips.py.
    """
    supa = get_supabase_admin()

    try:
        # Exhaustive, FK-safe erasure via the canonical RPC (service_role target).
        result = supa.rpc("delete_glovebox_account", {"p_user_id": user.id}).execute()
    except Exception as exc:
        logger.error("[account/delete] Failed to delete user %s: %s", user.id, exc)
        return JSONResponse(
            ErrorResponse(
                error="Failed to delete account. Please try again or contact support."
            ).model_dump(),
            status_code=500,
        )

    # The RPC returns the id it actually erased. It targets p_user_id only while
    # auth.uid() is null, which holds for a service_role JWT (no sub claim). If a
    # future change hands this route a user-scoped client, auth.uid() would resolve
    # and the RPC would erase THAT identity instead of the caller's target, silently.
    # Assert the erased id is the one we asked for rather than trust the precondition.
    # PostgREST returns the scalar jsonb directly; tolerate a single-row list wrap.
    payload = result.data
    if isinstance(payload, list):
        payload = payload[0] if payload else None
    deleted_id = payload.get("user_id") if isinstance(payload, dict) else None
    if str(deleted_id) != str(user.id):
        logger.error(
            "[account/delete] RPC erased %s but the request targeted %s; refusing to "
            "report success",
            deleted_id,
            user.id,
        )
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
