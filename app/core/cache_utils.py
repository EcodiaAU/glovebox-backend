# app/core/cache_utils.py
"""
Shared cache-key generation and freshness checking used by overlay services.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Optional


def stable_key(namespace: str, obj: dict) -> str:
    """
    Deterministic SHA-256 cache key from *namespace* + JSON-serialised *obj*.

    Example:
        stable_key("toilets", {"polyline6": "...", "buffer_km": 15.0, "algo_version": "1.0"})
    """
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    h = hashlib.sha256((namespace + "::" + raw).encode()).digest()
    return base64.urlsafe_b64encode(h).decode().rstrip("=")


def is_fresh(created_at: Optional[str], *, max_age_s: int) -> bool:
    """Return True if *created_at* ISO-8601 timestamp is within *max_age_s* seconds of now."""
    if not created_at:
        return False
    try:
        t = created_at.strip()
        if t.endswith("Z"):
            t = t[:-1] + "+00:00"
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (time.time() - dt.timestamp()) <= float(max_age_s)
    except Exception:
        return False
