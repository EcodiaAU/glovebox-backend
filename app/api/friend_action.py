# app/api/friend_action.py
#
# Friend cross-app WRITE endpoint (v2, one named action per app). Implements the
# "friend-action contract" in friend/docs/friend-context-contract.md so the
# person's one Ecodia Friend can act INTO Glovebox for them. Sibling of
# friend_context.py: same transport (HMAC over raw body), same trust, same
# reverse-resolution (public.resolve_friend_user, migration 012).
#
# Allowlist (hard-coded, never a dynamic table/SQL path):
#   save_place  - creates a row in public.saved_places the person can see and
#                 remove in the app. Reversible, non-destructive, v1-rule clean.
#
# Verify order is exactly the contract's: secret absent -> 503; bad HMAC -> 401;
# stale ts -> 401; unknown sub -> 200 {connected:false, done:false}; unknown
# action -> 400; idempotency replay -> original result, no second insert.
#
# Idempotency: the created row's place_id is minted as "friend:<idempotency_key>",
# so the key is stored ON the row and the existing unique index
# saved_places_user_place_idx (user_id, place_id) makes a duplicate insert
# impossible at the DB layer even under a replay race.
#
# saved_places.lat/lng are NOT NULL (migration 002) and every client (web,
# native iOS Swift, native Android Compose) decodes them as non-optional, so a
# save_place without coordinates is a validation failure (done:false with a
# plain sentence), never a schema change.

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.supabase_admin import get_supabase_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/friend", tags=["friend"])

_APP_ID = "glovebox"
_TS_SKEW_MS = 120_000

_ACTION_ALLOWLIST = frozenset({"save_place"})

_NAME_MAX = 120
_NOTE_MAX = 500
_IDEM_KEY_MAX = 128

# Friend-saved rows carry a valid PlaceCategory so every client renders them.
_FRIEND_PLACE_CATEGORY = "place"


def _err(status: int, error: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": error}, status_code=status)


def _act(connected: bool, done: bool, result: str = "") -> JSONResponse:
    payload: Dict[str, Any] = {
        "ok": True,
        "app": _APP_ID,
        "connected": connected,
        "done": done,
    }
    if result:
        payload["result"] = result
    return JSONResponse(payload, status_code=200)


@router.post("/action")
async def friend_action(request: Request) -> JSONResponse:
    # 1. Shared secret must be configured (else the whole verification is a no-op).
    secret = (os.environ.get("FRIEND_SUITE_SECRET") or "").strip()
    if not secret:
        logger.warning("[friend/action] FRIEND_SUITE_SECRET not configured")
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

    # 4. Reverse-resolve the Friend sub -> local Glovebox user id (service role).
    friend_sub = payload.get("friend_sub")
    if not isinstance(friend_sub, str) or not friend_sub.strip():
        return _act(connected=False, done=False)

    try:
        supa = get_supabase_admin()
        res = supa.rpc(
            "resolve_friend_user", {"p_friend_sub": friend_sub.strip()}
        ).execute()
    except Exception as exc:  # noqa: BLE001 - a resolution outage must not 500 the Friend
        logger.warning("[friend/action] resolve_friend_user failed: %s", str(exc)[:200])
        return _act(connected=False, done=False)

    user_id = res.data if isinstance(res.data, str) and res.data else None
    if not user_id:
        return _act(connected=False, done=False)

    # 5. Action must be on the hard-coded allowlist.
    action = payload.get("action")
    if not isinstance(action, str) or action not in _ACTION_ALLOWLIST:
        return _err(400, "unknown action")

    # 6. Idempotency key is mandatory transport plumbing (the gateway always
    #    sends one); a missing/garbage key is a malformed request, not a
    #    user-level validation failure.
    idem_key = payload.get("idempotency_key")
    if (
        not isinstance(idem_key, str)
        or not idem_key.strip()
        or len(idem_key.strip()) > _IDEM_KEY_MAX
    ):
        return _err(400, "bad idempotency_key")
    idem_key = idem_key.strip()

    params = payload.get("params")
    if not isinstance(params, dict):
        params = {}

    # Only save_place exists today; a future second action becomes a dispatch.
    return _save_place(supa, user_id, idem_key, params)


def _num(value: Any) -> Optional[float]:
    """A finite JSON number (bool excluded), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def _save_place(
    supa: Any, user_id: str, idem_key: str, params: Dict[str, Any]
) -> JSONResponse:
    """Create one saved_places row for the resolved user. Reversible: the
    person sees and can remove it in the app. Every read/write is scoped to
    user_id; params can never point at another person's rows."""

    name = params.get("name")
    name = name.strip() if isinstance(name, str) else ""
    if not name or len(name) > _NAME_MAX:
        return _act(
            connected=True,
            done=False,
            result="Saving a place needs a name between 1 and 120 characters.",
        )

    lat = _num(params.get("lat"))
    lng = _num(params.get("lng"))
    if lat is None or lng is None or abs(lat) > 90 or abs(lng) > 180:
        return _act(
            connected=True,
            done=False,
            result=(
                "Saving a place in Glovebox needs a map location: include both "
                "lat and lng, or they can save it from search in the app."
            ),
        )

    note = params.get("note")
    note = note.strip()[:_NOTE_MAX] if isinstance(note, str) and note.strip() else None

    place_id = f"friend:{idem_key}"

    # Idempotency replay: the key lives on the row as place_id; if the row is
    # already there, return the ORIGINAL result and change nothing.
    try:
        existing = (
            supa.table("saved_places")
            .select("id,name")
            .eq("user_id", user_id)
            .eq("place_id", place_id)
            .limit(1)
            .execute()
        )
        rows = existing.data if isinstance(existing.data, list) else []
        if rows:
            original = (rows[0].get("name") or name).strip() or name
            return _act(
                connected=True,
                done=True,
                result=f"Saved {original} to their places.",
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[friend/action] idempotency read failed: %s", str(exc)[:200])
        return _act(
            connected=True,
            done=False,
            result="Glovebox could not save the place right now.",
        )

    row = {
        "user_id": user_id,
        "place_id": place_id,
        "name": name,
        "lat": lat,
        "lng": lng,
        "category": _FRIEND_PLACE_CATEGORY,
    }
    if note:
        row["note"] = note

    try:
        supa.table("saved_places").insert(row).execute()
    except Exception as exc:  # noqa: BLE001
        # A replay race lands here via the unique (user_id, place_id) index:
        # re-read and, if the row exists, hand back the original result.
        try:
            again = (
                supa.table("saved_places")
                .select("id,name")
                .eq("user_id", user_id)
                .eq("place_id", place_id)
                .limit(1)
                .execute()
            )
            arows = again.data if isinstance(again.data, list) else []
            if arows:
                original = (arows[0].get("name") or name).strip() or name
                return _act(
                    connected=True,
                    done=True,
                    result=f"Saved {original} to their places.",
                )
        except Exception:  # noqa: BLE001
            pass
        logger.warning("[friend/action] saved_places insert failed: %s", str(exc)[:200])
        return _act(
            connected=True,
            done=False,
            result="Glovebox could not save the place right now.",
        )

    return _act(connected=True, done=True, result=f"Saved {name} to their places.")
