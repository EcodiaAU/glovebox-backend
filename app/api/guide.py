# app/api/guide.py
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.auth import AuthUser, get_optional_user
from app.core.contracts import (
    GuideAskAction,
    GuideAskRequest,
    GuideAskResponse,
    GuideContext,
    GuideMsg,
    GuideTurnRequest,
    GuideTurnResponse,
    TripProgress,
)
from app.core.errors import bad_request
from app.services.friend_memory import resolve_friend
from app.services.guide import GuideService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/guide")


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
        return await resolve_friend(user.friend_id, getattr(user, "email", None))
    except Exception as e:  # noqa: BLE001
        logger.warning("guide: friend resolve failed: %s", str(e)[:200])
        return None, ""

# Shared instance so the underlying httpx client is reused across requests
_guide_svc: GuideService | None = None


def _get_guide_svc() -> GuideService:
    global _guide_svc
    if _guide_svc is None:
        _guide_svc = GuideService()
    return _guide_svc


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
    friend_name, memory_prelude = await _resolve_friend_for(user)
    try:
        return await svc.turn(
            req, friend_name=friend_name, memory_prelude=memory_prelude
        )
    except RuntimeError as e:
        bad_request("guide_error", str(e))
        raise  # unreachable - bad_request raises HTTPException


@router.post("/ask", response_model=GuideAskResponse)
async def guide_ask(
    req: GuideAskRequest,
    user: Optional[AuthUser] = Depends(get_optional_user),
) -> GuideAskResponse:
    """
    Mobile-friendly alias for /guide/turn.

    Native iOS and Android clients send a tiny payload (just the question
    plus a flat context with lat/lng/timezone/local_time) instead of the
    full GuideTurnRequest tree. This endpoint accepts that shape and
    translates internally so mobile does not have to build the full
    GuideContext + thread + tool_results structure.

    Returns a flat {text, sources} so the client never has to know about
    actions, tool_calls, or web_searched flags.
    """
    svc = _get_guide_svc()

    ctx_in = req.context
    progress: TripProgress | None = None
    if ctx_in and ctx_in.lat is not None and ctx_in.lng is not None:
        progress = TripProgress(
            user_lat=ctx_in.lat,
            user_lng=ctx_in.lng,
            local_time_iso=ctx_in.local_time_iso,
            timezone=ctx_in.timezone or "Australia/Brisbane",
        )

    turn_req = GuideTurnRequest(
        context=GuideContext(progress=progress),
        thread=[GuideMsg(role="user", content=req.question)],
    )

    friend_name, memory_prelude = await _resolve_friend_for(user)
    try:
        turn_resp = await svc.turn(
            turn_req, friend_name=friend_name, memory_prelude=memory_prelude
        )
    except RuntimeError as e:
        bad_request("guide_error", str(e))
        raise  # unreachable - bad_request raises HTTPException

    sources: list[str] = []
    for s in turn_resp.sources:
        label = s.title.strip() if s.title else ""
        url = s.url.strip() if s.url else ""
        if label and url:
            sources.append(f"{label} ({url})")
        elif label:
            sources.append(label)
        elif url:
            sources.append(url)

    # Map structured GuideAction tool-call entries to the flat GuideAskAction
    # shape mobile clients render as action buttons / cards.
    actions: list[GuideAskAction] = []
    for a in turn_resp.actions:
        actions.append(
            GuideAskAction(
                type=str(a.type),
                label=a.label,
                place_id=a.place_id,
                place_name=a.place_name,
                url=a.url,
                tel=a.tel,
                lat=a.lat,
                lng=a.lng,
                category=a.category,
                description=a.description,
            )
        )

    return GuideAskResponse(
        text=turn_resp.assistant,
        sources=sources,
        actions=actions,
    )
