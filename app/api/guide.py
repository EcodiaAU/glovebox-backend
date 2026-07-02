# app/api/guide.py
from __future__ import annotations

from fastapi import APIRouter

from app.core.contracts import (
    GuideAskRequest,
    GuideAskResponse,
    GuideContext,
    GuideMsg,
    GuideTurnRequest,
    GuideTurnResponse,
    TripProgress,
)
from app.core.errors import bad_request
from app.services.guide import GuideService

router = APIRouter(prefix="/guide")

# Shared instance so the underlying httpx client is reused across requests
_guide_svc: GuideService | None = None


def _get_guide_svc() -> GuideService:
    global _guide_svc
    if _guide_svc is None:
        _guide_svc = GuideService()
    return _guide_svc


@router.post("/turn", response_model=GuideTurnResponse)
async def guide_turn(req: GuideTurnRequest) -> GuideTurnResponse:
    svc = _get_guide_svc()
    try:
        return await svc.turn(req)
    except RuntimeError as e:
        bad_request("guide_error", str(e))
        raise  # unreachable - bad_request raises HTTPException


@router.post("/ask", response_model=GuideAskResponse)
async def guide_ask(req: GuideAskRequest) -> GuideAskResponse:
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

    try:
        turn_resp = await svc.turn(turn_req)
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
