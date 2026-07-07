# app/services/friend_memory.py
#
# Resolves the signed-in person's canonical Ecodia Friend from the SHARED
# Ecosphere/Friend Supabase project (cuiobblgoybgmaxnsazo): their Friend name
# (ecosphere_accounts.friend_name) and their travelling memory
# (ecosphere_friend_memory). This is what lets the Glovebox guide speak as the
# person's SAME Friend - the one they named and that remembers them across every
# Ecodia surface - rather than a separate "Roam Guide" character.
#
# Read-only, service-role, against the PostgREST REST API over async httpx so it
# never blocks the event loop. Mirrors the shape of friend/lib/brain.ts
# (recallMemory + memoryPrelude) so the injected block is byte-comparable to the
# other surfaces. When ECOSPHERE_* is unset or the friend is unresolved, returns
# (None, "") and the caller falls back to the neutral "Friend" persona.

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.core.settings import settings

logger = logging.getLogger(__name__)

# Identity facts always worth surfacing (mirrors friend/lib/brain.ts IDENTITY_KINDS).
_IDENTITY_KINDS = ["identity", "operating-style", "name", "adaptive"]

_HTTP_TIMEOUT_S = 8.0
_MAX_PRELUDE_LINES = 10


def _sanitize(raw: str, max_len: int = 220) -> str:
    """Strip newlines, stray SYSTEM markers and parens so a memory row can never
    forge a system instruction inside the prelude (mirrors brain.ts sanitize)."""
    s = raw or ""
    s = re.sub(r"[\r\n]+", " ", s)
    s = re.sub(r"\(\s*system", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"your account id is", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"[()]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:max_len]


def _build_prelude(
    id_rows: List[Dict[str, Any]], recent_rows: List[Dict[str, Any]]
) -> str:
    """Compose the strippable SYSTEM memory prelude - `key = value; ...` - from
    identity facts + recent notes, newest first, deduped. Same wording the other
    surfaces inject via memoryPrelude()."""
    seen: set[str] = set()
    lines: List[str] = []
    for r in [*id_rows, *recent_rows]:
        key = r.get("key")
        content = r.get("content") or ""
        dedup = f"{key or ''}|{content}"
        if dedup in seen:
            continue
        seen.add(dedup)
        label = _sanitize(str(key or r.get("kind") or ""), 60) or str(r.get("kind") or "")
        value = _sanitize(content)
        if label and value:
            lines.append(f"{label} = {value}")
        if len(lines) >= _MAX_PRELUDE_LINES:
            break
    if not lines:
        return ""
    return (
        "(SYSTEM - what you already know about this person, from their durable "
        "memory. Treat as true, do not re-ask, do not quote this block back: "
        + "; ".join(lines)
        + ")"
    )


async def _get_rows(
    client: httpx.AsyncClient, base: str, headers: Dict[str, str], params: Dict[str, str]
) -> List[Dict[str, Any]]:
    try:
        r = await client.get(
            f"{base}/rest/v1/ecosphere_friend_memory", params=params, headers=headers
        )
        if r.status_code >= 400:
            logger.warning(
                "Friend memory query %s: %s", r.status_code, r.text[:200]
            )
            return []
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as e:  # noqa: BLE001 - never let memory read break a turn
        logger.warning("Friend memory query failed: %s", str(e)[:200])
        return []


async def resolve_friend(friend_id: Optional[str]) -> Tuple[Optional[str], str]:
    """Resolve (friend_name, memory_prelude) for a friend_id against the shared
    Ecosphere project. Returns (None, "") when no friend_id, ECOSPHERE_* unset,
    or the account/name is absent. Never raises - the guide must survive a memory
    outage by degrading to the neutral persona."""
    url = settings.ecosphere_supabase_url
    key = settings.ecosphere_service_role_key
    if not friend_id:
        return None, ""
    if not url or not key:
        logger.warning(
            "Friend resolve: ECOSPHERE_SUPABASE_URL / ECOSPHERE_SERVICE_ROLE_KEY "
            "not configured; guide falls back to neutral Friend persona"
        )
        return None, ""

    base = url.rstrip("/")
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S) as client:
            acct_r = await client.get(
                f"{base}/rest/v1/ecosphere_accounts",
                params={
                    "owner_id": f"eq.{friend_id}",
                    "select": "id,friend_name",
                    "limit": "1",
                },
                headers=headers,
            )
            if acct_r.status_code >= 400:
                logger.warning(
                    "Friend resolve: accounts query %s: %s",
                    acct_r.status_code,
                    acct_r.text[:200],
                )
                return None, ""
            rows = acct_r.json()
            if not isinstance(rows, list) or not rows:
                return None, ""
            acct = rows[0]
            account_id = acct.get("id")
            friend_name = (acct.get("friend_name") or "").strip() or None

            memory_prelude = ""
            if account_id:
                id_rows = await _get_rows(
                    client,
                    base,
                    headers,
                    {
                        "account_id": f"eq.{account_id}",
                        "archived_at": "is.null",
                        "kind": f"in.({','.join(_IDENTITY_KINDS)})",
                        "select": "kind,key,content,source,updated_at",
                        "order": "updated_at.desc",
                        "limit": "8",
                    },
                )
                recent_rows = await _get_rows(
                    client,
                    base,
                    headers,
                    {
                        "account_id": f"eq.{account_id}",
                        "archived_at": "is.null",
                        "kind": f"not.in.({','.join(_IDENTITY_KINDS)})",
                        "select": "kind,key,content,source,updated_at",
                        "order": "updated_at.desc",
                        "limit": "8",
                    },
                )
                memory_prelude = _build_prelude(id_rows, recent_rows)

            logger.info(
                "Friend resolve: friend_id=%s name=%s memory_rows_prelude=%s",
                friend_id[:8] + "..." if friend_id else None,
                friend_name or "(none)",
                "yes" if memory_prelude else "no",
            )
            return friend_name, memory_prelude
    except Exception as e:  # noqa: BLE001
        logger.warning("Friend resolve failed for %s: %s", friend_id, str(e)[:200])
        return None, ""
