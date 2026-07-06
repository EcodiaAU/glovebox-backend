# app/api/guide.py
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import AuthUser, get_optional_user
from app.core.contracts import GuideTurnRequest, GuideTurnResponse
from app.core.errors import bad_request
from app.services.friend_memory import resolve_friend
from app.services.guide import GuideService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/guide")

# Shared instance so the underlying httpx client is reused across requests
_guide_svc: GuideService | None = None


def _get_guide_svc() -> GuideService:
    global _guide_svc
    if _guide_svc is None:
        _guide_svc = GuideService()
    return _guide_svc


class GuideFriendResponse(BaseModel):
    """The signed-in person's resolved Ecodia Friend name, or None when they are
    not Friend-connected. The floating chat header shows this (fallback 'Friend')."""

    name: Optional[str] = None
    connected: bool = False


async def _resolve_friend_for(user: Optional[AuthUser]) -> tuple[Optional[str], str]:
    """Resolve (friend_name, memory_prelude) for an optional signed-in user.
    Never raises - a memory outage must not break a guide turn."""
    if not user or not getattr(user, "friend_id", None):
        return None, ""
    try:
        return await resolve_friend(user.friend_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("guide: friend resolve failed: %s", str(e)[:200])
        return None, ""


@router.get("/friend", response_model=GuideFriendResponse)
async def guide_friend(
    user: Optional[AuthUser] = Depends(get_optional_user),
) -> GuideFriendResponse:
    """Resolve the signed-in person's Friend name for the floating-chat header.
    Returns {name: null, connected: false} for anonymous or non-Friend users."""
    if not user or not getattr(user, "friend_id", None):
        return GuideFriendResponse(name=None, connected=False)
    friend_name, _ = await _resolve_friend_for(user)
    return GuideFriendResponse(name=friend_name, connected=True)


@router.post("/turn", response_model=GuideTurnResponse)
async def guide_turn(
    req: GuideTurnRequest,
    user: Optional[AuthUser] = Depends(get_optional_user),
) -> GuideTurnResponse:
    svc = _get_guide_svc()
    # Resolve the signed-in person's canonical Friend name + travelling memory so
    # the guide speaks as their SAME Friend, not a separate "Roam Guide". Anonymous
    # / non-connected users resolve to (None, "") and get the neutral persona.
    friend_name, memory_prelude = await _resolve_friend_for(user)
    try:
        return await svc.turn(
            req, friend_name=friend_name, memory_prelude=memory_prelude
        )
    except RuntimeError as e:
        bad_request("guide_error", str(e))
        raise  # unreachable - bad_request raises HTTPException
