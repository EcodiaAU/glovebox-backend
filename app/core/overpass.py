# app/core/overpass.py
"""
Global Overpass API client with concurrency control, instance rotation,
and rate-limit awareness.

Problem: Multiple services (places, rest_areas, speed_cameras) all hit
Overpass simultaneously during trip enrichment, causing 429s, timeouts,
and 403s from exhausting all instances.

Solution: A single global gate that:
  - Limits concurrent Overpass requests (semaphore)
  - Rotates through instances round-robin per request (not per retry)
  - Enforces minimum spacing between requests to the same instance
  - Provides both sync and async interfaces

Usage:
    from app.core.overpass import overpass_fetch, overpass_fetch_sync

    # Async (rest_areas, speed_cameras)
    data = await overpass_fetch(ql)

    # Sync (places.py thread pool)
    data = overpass_fetch_sync(ql)
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Dict, Optional

import httpx

from app.core.settings import settings

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────

# Max concurrent Overpass requests across ALL services.
# Overpass.de allows ~2 concurrent per IP; with 3 instances, 4 is safe.
_MAX_CONCURRENT = 4

# Minimum seconds between requests to the SAME instance.
_MIN_INSTANCE_SPACING_S = 1.5

# Retryable HTTP status codes.
_RETRYABLE = frozenset({429, 502, 503, 504})

# ── State ─────────────────────────────────────────────────────

_async_semaphore: Optional[asyncio.Semaphore] = None
_sync_lock = threading.Semaphore(_MAX_CONCURRENT)

# Per-instance last-request timestamp (thread-safe via _timing_lock).
_timing_lock = threading.Lock()
_instance_last_ts: Dict[str, float] = {}

# Round-robin counter (thread-safe via atomicity of int increment).
_robin_counter = 0
_robin_lock = threading.Lock()


def _get_urls() -> list[str]:
    urls = [settings.overpass_url]
    fallbacks = getattr(settings, "overpass_fallback_urls", None) or []
    urls.extend(fallbacks)
    return urls


def _next_url() -> str:
    """Pick the next instance via round-robin."""
    global _robin_counter
    urls = _get_urls()
    with _robin_lock:
        idx = _robin_counter % len(urls)
        _robin_counter += 1
    return urls[idx]


def _wait_for_instance(url: str) -> None:
    """Block until MIN_INSTANCE_SPACING_S has elapsed since last request to this host."""
    host = url.split("/")[2]
    with _timing_lock:
        last = _instance_last_ts.get(host, 0.0)
    wait = _MIN_INSTANCE_SPACING_S - (time.monotonic() - last)
    if wait > 0:
        time.sleep(wait)
    with _timing_lock:
        _instance_last_ts[host] = time.monotonic()


async def _async_wait_for_instance(url: str) -> None:
    """Async version — yields to event loop while waiting."""
    host = url.split("/")[2]
    with _timing_lock:
        last = _instance_last_ts.get(host, 0.0)
    wait = _MIN_INSTANCE_SPACING_S - (time.monotonic() - last)
    if wait > 0:
        await asyncio.sleep(wait)
    with _timing_lock:
        _instance_last_ts[host] = time.monotonic()


def _get_async_semaphore() -> asyncio.Semaphore:
    """Lazy-init the async semaphore (must be in a running event loop)."""
    global _async_semaphore
    if _async_semaphore is None:
        _async_semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
    return _async_semaphore


# ── Public API: async ─────────────────────────────────────────

async def overpass_fetch(
    ql: str,
    *,
    timeout_s: float | None = None,
    label: str = "overpass",
) -> Dict[str, Any]:
    """
    Execute an Overpass QL query with global concurrency control.

    Raises on total failure after exhausting all instances.
    """
    timeout = timeout_s or float(getattr(settings, "overpass_timeout_s", 90))
    urls = _get_urls()
    attempts = len(urls)
    sem = _get_async_semaphore()
    last_exc: Optional[Exception] = None

    for i in range(attempts):
        url = _next_url()

        async with sem:
            await _async_wait_for_instance(url)
            host = url.split("/")[2]
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(timeout, connect=10.0),
                    follow_redirects=True,
                ) as client:
                    resp = await client.post(url, data={"data": ql})

                if resp.status_code in _RETRYABLE:
                    logger.warning(
                        "[overpass] %s %s returned %d, rotating to next instance",
                        label, host, resp.status_code,
                    )
                    last_exc = httpx.HTTPStatusError(
                        f"{resp.status_code}", request=resp.request, response=resp,
                    )
                    continue

                if resp.status_code == 403:
                    logger.warning("[overpass] %s %s returned 403, skipping instance", label, host)
                    last_exc = httpx.HTTPStatusError(
                        "403", request=resp.request, response=resp,
                    )
                    continue

                resp.raise_for_status()
                return resp.json()

            except httpx.HTTPStatusError:
                raise
            except Exception as e:
                logger.warning(
                    "[overpass] %s %s error: %s, rotating to next instance",
                    label, host, type(e).__name__,
                )
                last_exc = e
                # Brief pause before trying the next instance to avoid
                # hammering all instances in quick succession after a timeout.
                await asyncio.sleep(1.0)

    raise last_exc or RuntimeError(f"[overpass] {label}: all instances failed")


# ── Public API: sync (for places.py thread pool) ─────────────

def overpass_fetch_sync(
    ql: str,
    *,
    timeout_s: float | None = None,
    label: str = "overpass",
) -> Dict[str, Any]:
    """
    Synchronous version for use in thread pools.

    Uses a threading.Semaphore for the same global concurrency limit.
    """
    timeout = timeout_s or float(getattr(settings, "overpass_timeout_s", 90))
    urls = _get_urls()
    attempts = len(urls)
    last_exc: Optional[Exception] = None

    for i in range(attempts):
        url = _next_url()

        _sync_lock.acquire()
        try:
            _wait_for_instance(url)
            host = url.split("/")[2]
            try:
                with httpx.Client(
                    timeout=httpx.Timeout(timeout, connect=10.0),
                    follow_redirects=True,
                ) as client:
                    resp = client.post(url, data={"data": ql})

                if resp.status_code in _RETRYABLE:
                    logger.warning(
                        "[overpass] %s %s returned %d, rotating to next instance",
                        label, host, resp.status_code,
                    )
                    last_exc = httpx.HTTPStatusError(
                        f"{resp.status_code}", request=resp.request, response=resp,
                    )
                    continue

                if resp.status_code == 403:
                    logger.warning("[overpass] %s %s returned 403, skipping instance", label, host)
                    last_exc = httpx.HTTPStatusError(
                        "403", request=resp.request, response=resp,
                    )
                    continue

                resp.raise_for_status()
                return resp.json()

            except httpx.HTTPStatusError:
                raise
            except Exception as e:
                logger.warning(
                    "[overpass] %s %s error: %s, rotating to next instance",
                    label, host, type(e).__name__,
                )
                last_exc = e
                # Brief pause before trying the next instance
                time.sleep(1.0)
        finally:
            _sync_lock.release()

    raise last_exc or RuntimeError(f"[overpass] {label}: all instances failed")
