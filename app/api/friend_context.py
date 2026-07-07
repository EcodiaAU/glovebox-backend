# app/api/friend_context.py
#
# Friend cross-app READ endpoint (v1, read-only). Implements the exact contract
# in friend/docs/friend-context-contract.md so the person's one Ecodia Friend can
# read their Glovebox data on any surface.
#
# The central gateway (friend/lib/suite-read.ts) POSTs
#   body = JSON.stringify({ friend_sub, ts: Date.now() })   # ts is epoch MILLIS
#   header x-friend-signature = HMAC_SHA256(FRIEND_SUITE_SECRET, <raw body bytes>) hex
# We verify the signature over the RAW bytes (constant-time), check ts freshness
# (< 120s), reverse-resolve the custom:friend OIDC sub to the local Glovebox user
# via the service-role RPC public.resolve_friend_user (migration 012), read that
# user's OWN trips + saved places via SERVICE ROLE, and return a compact
# third-person summary.
#
# Verify order is exactly the contract's: secret absent -> 503; bad HMAC -> 401;
# stale ts -> 401; unknown sub -> 200 {connected:false, summary:""}.

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.supabase_admin import get_supabase_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/friend", tags=["friend"])

_APP_ID = "glovebox"
_TS_SKEW_MS = 120_000
_SUMMARY_MAX = 800

# How many named items to fold into the compact summary (the summary is grounding
# for the Friend, not a full listing - keep it tight).
_TRIPS_IN_SUMMARY = 4
_PLACES_IN_SUMMARY = 5


def _err(status: int, error: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": error}, status_code=status)


def _ctx(connected: bool, summary: str, items: List[Dict[str, Any]]) -> JSONResponse:
    payload: Dict[str, Any] = {
        "ok": True,
        "app": _APP_ID,
        "connected": connected,
        "summary": summary[:_SUMMARY_MAX],
    }
    if items:
        payload["items"] = items
    return JSONResponse(payload, status_code=200)


@router.post("/context")
async def friend_context(request: Request) -> JSONResponse:
    # 1. Shared secret must be configured (else the whole verification is a no-op).
    secret = (os.environ.get("FRIEND_SUITE_SECRET") or "").strip()
    if not secret:
        logger.warning("[friend/context] FRIEND_SUITE_SECRET not configured")
        return _err(503, "unconfigured")

    raw = await request.body()  # the exact bytes the gateway signed

    # 2. Constant-time HMAC over the RAW body.
    provided = request.headers.get("x-friend-signature", "")
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not provided or not hmac.compare_digest(provided, expected):
        return _err(401, "bad signature")

    # Only parse once the signature proves the body is ours.
    try:
        payload: Dict[str, Any] = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return _err(400, "bad body")

    # 3. Replay guard: ts within 120s of now.
    ts = payload.get("ts")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return _err(401, "stale")
    now_ms = int(time.time() * 1000)
    if abs(now_ms - int(ts)) > _TS_SKEW_MS:
        return _err(401, "stale")

    friend_sub = payload.get("friend_sub")
    if not isinstance(friend_sub, str) or not friend_sub.strip():
        return _ctx(connected=False, summary="", items=[])

    # 4. Reverse-resolve the Friend sub -> local Glovebox user id (service role).
    #    resolve_friend_user is SECURITY DEFINER, granted to service_role only.
    try:
        supa = get_supabase_admin()
        res = supa.rpc(
            "resolve_friend_user", {"p_friend_sub": friend_sub.strip()}
        ).execute()
    except Exception as exc:  # noqa: BLE001 - a resolution outage must not 500 the Friend
        logger.warning("[friend/context] resolve_friend_user failed: %s", str(exc)[:200])
        return _ctx(connected=False, summary="", items=[])

    user_id = res.data if isinstance(res.data, str) and res.data else None
    if not user_id:
        return _ctx(connected=False, summary="", items=[])

    summary, items = _build_summary(supa, user_id)
    return _ctx(connected=True, summary=summary, items=items)


def _stop_count(stops: Any) -> int:
    return len(stops) if isinstance(stops, list) else 0


def _build_summary(supa: Any, user_id: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Compose the compact third-person summary from the person's OWN Glovebox
    trips + saved places. Every read is scoped to user_id. Never raises - a
    partial read degrades to a shorter summary rather than a 500."""
    trip_titles: List[str] = []
    trips_total = 0
    place_names: List[str] = []
    places_total = 0
    items: List[Dict[str, Any]] = []

    # Trips (their saved/published routes). owner_id scopes to this user only.
    try:
        tr = (
            supa.table("public_trips")
            .select("title,stops,published_at,created_at")
            .eq("owner_id", user_id)
            .order("created_at", desc=True)
            .limit(50)
            .execute()
        )
        rows = tr.data if isinstance(tr.data, list) else []
        trips_total = len(rows)
        for r in rows[:_TRIPS_IN_SUMMARY]:
            title = (r.get("title") or "Untitled trip").strip() or "Untitled trip"
            n = _stop_count(r.get("stops"))
            if n:
                trip_titles.append(f"{title} ({n} stop{'s' if n != 1 else ''})")
            else:
                trip_titles.append(title)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[friend/context] trips read failed: %s", str(exc)[:200])

    # Saved places. user_id scopes to this user only.
    try:
        sp = (
            supa.table("saved_places")
            .select("name,category,saved_at")
            .eq("user_id", user_id)
            .order("saved_at", desc=True)
            .limit(200)
            .execute()
        )
        prows = sp.data if isinstance(sp.data, list) else []
        places_total = len(prows)
        for r in prows[:_PLACES_IN_SUMMARY]:
            nm = (r.get("name") or "").strip()
            if nm:
                place_names.append(nm)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[friend/context] saved_places read failed: %s", str(exc)[:200])

    parts: List[str] = []
    if trips_total:
        noun = "Glovebox trip" if trips_total == 1 else "Glovebox trips"
        if trip_titles:
            parts.append(f"They have {trips_total} {noun}: {', '.join(trip_titles)}")
        else:
            parts.append(f"They have {trips_total} {noun}")
        items.append({"kind": "trips", "count": trips_total})
    if places_total:
        noun = "saved place" if places_total == 1 else "saved places"
        if place_names:
            parts.append(f"{places_total} {noun} incl {', '.join(place_names)}")
        else:
            parts.append(f"{places_total} {noun}")
        items.append({"kind": "saved_places", "count": places_total})

    summary = "; ".join(parts)
    if summary:
        summary = summary.rstrip(".") + "."
    return summary[:_SUMMARY_MAX], items
