# app/api/friend_entitlement.py
#
# Friend cross-app WRITE endpoint: the write sibling of friend_context.py. The
# central Friend hub (friend/lib/suite-write.ts) calls this when a person's Friend
# "A little" subscription changes state on a native store (Apple IAP / Play Billing
# via RevenueCat), so the Glovebox app-local perk (unlimited trips + always-on
# road-trip guide) follows the subscription.
#
# Same trust model as /friend/context: HMAC over the RAW body with the shared
# FRIEND_SUITE_SECRET, a 120s replay window, and the service-role RPC
# public.resolve_friend_user (migration 012) to reverse-resolve the custom:friend
# sub to the local Glovebox user. The Glovebox DB write happens HERE with Glovebox's
# own service-role key - the hub never holds it.
#
# Body (JSON), signed by friend/lib/suite-write.ts::pushGloveboxPerk:
#   { friend_sub, active, source_platform, product_id,
#     transaction_id, original_transaction_id, expires_at, ts }
#
# active=true  -> grant: upsert a v2 `entitlements` row (tier=month) so
#                 get_current_entitlement returns a non-free tier => unlimited trips.
# active=false -> revert: expire every Friend-granted row for this user (set
#                 expires_at into the past) so get_current_entitlement falls back to
#                 free => the 2-trip cap returns. tier='free' is not a writable value
#                 (CHECK tier IN month/season/lifetime), so expiry is how we revert.
#
# Verify order matches the contract: secret absent -> 503; bad HMAC -> 401;
# stale ts -> 401; unknown sub -> 200 {ok:true, connected:false}.

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.billing_models import SourcePlatform, Tier
from app.core.supabase_admin import get_supabase_admin
from app.services.entitlements import upsert_entitlement

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/friend", tags=["friend"])

_TS_SKEW_MS = 120_000
_VALID_PLATFORMS = {"ios", "android", "web"}


def _err(status: int, error: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": error}, status_code=status)


def _ok(note: str, connected: bool = True, extra: Optional[Dict[str, Any]] = None) -> JSONResponse:
    payload: Dict[str, Any] = {"ok": True, "connected": connected, "note": note}
    if extra:
        payload.update(extra)
    return JSONResponse(payload, status_code=200)


def _parse_iso(v: Any) -> Optional[datetime]:
    if not isinstance(v, str) or not v.strip():
        return None
    s = v.strip().replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


@router.post("/entitlement")
async def friend_entitlement(request: Request) -> JSONResponse:
    # 1. Shared secret must be configured (else the verification is a no-op).
    secret = (os.environ.get("FRIEND_SUITE_SECRET") or "").strip()
    if not secret:
        logger.warning("[friend/entitlement] FRIEND_SUITE_SECRET not configured")
        return _err(503, "unconfigured")

    raw = await request.body()  # the exact bytes the hub signed

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
        return _err(400, "no friend_sub")

    active = bool(payload.get("active"))
    source_platform_raw = str(payload.get("source_platform") or "").strip().lower()
    if source_platform_raw not in _VALID_PLATFORMS:
        return _err(400, f"bad source_platform: {source_platform_raw or 'missing'}")
    product_id = str(payload.get("product_id") or "").strip()
    if not product_id:
        return _err(400, "no product_id")
    transaction_id = payload.get("transaction_id")
    original_transaction_id = payload.get("original_transaction_id")
    expires_at = _parse_iso(payload.get("expires_at"))

    # 4. Reverse-resolve the Friend sub -> local Glovebox user id (service role).
    try:
        supa = get_supabase_admin()
        res = supa.rpc("resolve_friend_user", {"p_friend_sub": friend_sub.strip()}).execute()
    except Exception as exc:  # noqa: BLE001 - a resolution outage must not 500 the hub
        logger.warning("[friend/entitlement] resolve_friend_user failed: %s", str(exc)[:200])
        return _err(502, "resolve failed")

    user_id = res.data if isinstance(res.data, str) and res.data else None
    if not user_id:
        # The person has not connected Glovebox to their Friend. The central
        # subscription row still landed; there is simply no local perk to grant.
        return _ok("no glovebox user for this friend", connected=False)

    if active:
        return _grant(user_id, source_platform_raw, product_id, transaction_id, expires_at)
    return _revert(supa, user_id, product_id)


def _grant(
    user_id: str,
    source_platform_raw: str,
    product_id: str,
    transaction_id: Any,
    expires_at: Optional[datetime],
) -> JSONResponse:
    """Grant the Glovebox unlock: a MONTH-tier entitlement row that
    get_current_entitlement returns as an active pass (=> unlimited trips)."""
    txn = str(transaction_id).strip() if isinstance(transaction_id, (str, int)) and str(transaction_id).strip() else None
    if not txn:
        # The unique key (source_platform, transaction_id) needs a txn id.
        txn = f"friend:{user_id}:{product_id}"
    try:
        row = upsert_entitlement(
            user_id=user_id,
            tier=Tier.MONTH,
            source_platform=SourcePlatform(source_platform_raw),
            product_id=product_id,
            transaction_id=txn,
            expires_at=expires_at,
            raw_receipt={"granted_by": "friend-rc-webhook", "product_id": product_id},
            source="purchase",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[friend/entitlement] grant failed user=%s: %s", user_id, str(exc)[:200])
        return _err(500, "grant failed")
    logger.info("[friend/entitlement] granted user=%s product=%s expires=%s", user_id, product_id, expires_at)
    return _ok("granted", extra={"user_id": user_id, "tier": row.get("tier", "month")})


def _revert(supa: Any, user_id: str, product_id: str) -> JSONResponse:
    """Revert the Glovebox unlock: expire every Friend-granted row for this user
    (tier='free' is not a writable value, so we push expires_at into the past). A
    later get_current_entitlement then falls through to free => the 2-trip cap."""
    past = datetime.now(timezone.utc).isoformat()
    try:
        res = (
            supa.table("entitlements")
            .update({"expires_at": past, "updated_at": past})
            .eq("user_id", user_id)
            .eq("product_id", product_id)
            .execute()
        )
        affected = len(res.data) if isinstance(getattr(res, "data", None), list) else 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("[friend/entitlement] revert failed user=%s: %s", user_id, str(exc)[:200])
        return _err(500, "revert failed")
    logger.info("[friend/entitlement] reverted user=%s product=%s rows=%d", user_id, product_id, affected)
    return _ok("reverted", extra={"user_id": user_id, "rows": affected})
