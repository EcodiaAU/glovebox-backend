# app/services/entitlements.py
#
# Core read + write helpers for the v2 tiered entitlements substrate.
#
# Reads from public.entitlements (v2 tiered) plus public.user_entitlements
# (v1 binary, kept for the grandfather path).
#
# Writes only happen through the redeem endpoint (phase E) and the v2 Stripe
# webhook path (phase B). This module exposes the write helper so both call
# sites share the same idempotent upsert path.
#
# The supabase-py client this module talks to uses the service-role key
# (bypasses RLS). All routes that reach this module MUST authenticate the
# caller first; the service does not re-check auth.

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from app.core.billing_models import (
    EntitlementResponse,
    SourcePlatform,
    Tier,
)
from app.core.settings import settings
from app.core.supabase_admin import get_supabase_admin

logger = logging.getLogger(__name__)


# ── Read: resolve current effective tier ──────────────────────────────────


def get_current_entitlement(user_id: str) -> EntitlementResponse:
    """Resolve the user's current effective tier.

    Resolution order (first match wins):

    1. Lifetime row in `public.entitlements` (tier='lifetime', no expiry).
    2. Active pass row in `public.entitlements` with expires_at > now().
    3. Legacy `public.user_entitlements` row (any source) -> tier=lifetime,
       source_platform=legacy. The v1 binary unlock was always a one-off
       perpetual purchase, so Lifetime is the right shape to grandfather it
       into without writing a synthetic v2 row at read time.
    4. tier=free.

    The function is synchronous because supabase-py's default client is sync.
    Routes that wrap it in `async def` are still fine; supabase-py uses
    blocking httpx underneath but the typical entitlement read is sub-50ms
    against the project's Sydney region.
    """

    supa = get_supabase_admin()

    # Step 1 + 2: read public.entitlements ordered so lifetime sorts first
    # (expires_at is null for lifetime, the migration indexes it as `desc
    # nulls first`), then active passes by furthest expiry.
    result = (
        supa.table("entitlements")
        .select("tier, expires_at, source_platform, product_id")
        .eq("user_id", user_id)
        .order("expires_at", desc=True, nullsfirst=True)
        .limit(5)
        .execute()
    )
    rows: list[dict[str, Any]] = getattr(result, "data", None) or []

    now = datetime.now(timezone.utc)

    for row in rows:
        tier_raw = row.get("tier")
        if tier_raw == Tier.LIFETIME.value:
            return EntitlementResponse(
                tier=Tier.LIFETIME,
                expires_at=None,
                source_platform=SourcePlatform(row.get("source_platform"))
                if row.get("source_platform")
                else None,
                product_id=row.get("product_id"),
            )

        expires_at_raw = row.get("expires_at")
        if expires_at_raw and _parse_iso(expires_at_raw) > now:
            return EntitlementResponse(
                tier=Tier(tier_raw),
                expires_at=expires_at_raw,
                source_platform=SourcePlatform(row.get("source_platform"))
                if row.get("source_platform")
                else None,
                product_id=row.get("product_id"),
            )

    # Step 3: legacy grandfather. Any row in v1 user_entitlements -> Lifetime.
    legacy = (
        supa.table("user_entitlements")
        .select("id")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    legacy_rows = getattr(legacy, "data", None) or []
    if legacy_rows:
        return EntitlementResponse(
            tier=Tier.LIFETIME,
            expires_at=None,
            source_platform=SourcePlatform.LEGACY,
            product_id=None,
        )

    # Step 4: free.
    return EntitlementResponse(
        tier=Tier.FREE,
        expires_at=None,
        source_platform=None,
        product_id=None,
    )


# ── Write: upsert a verified entitlement row ──────────────────────────────


def upsert_entitlement(
    *,
    user_id: str,
    tier: Tier,
    source_platform: SourcePlatform,
    product_id: str,
    transaction_id: str,
    expires_at: Optional[datetime],
    raw_receipt: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """Idempotent insert into `public.entitlements`.

    Unique on (source_platform, transaction_id). Redeeming the same receipt
    twice from a flaky client is a no-op; the upsert returns the existing row.
    Lifetime rows pass `expires_at=None`.

    Returns the row that landed (whether newly inserted or pre-existing).
    """

    if source_platform == SourcePlatform.LEGACY:
        raise ValueError(
            "SourcePlatform.LEGACY is read-side only; the redeem path must "
            "name the real platform that delivered the receipt."
        )

    supa = get_supabase_admin()
    row = {
        "user_id": user_id,
        "tier": tier.value,
        "source_platform": source_platform.value,
        "product_id": product_id,
        "transaction_id": transaction_id,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "raw_receipt": raw_receipt,
    }

    logger.info(
        "[entitlements] upsert user=%s tier=%s platform=%s product=%s txn=%s expires=%s",
        user_id,
        tier.value,
        source_platform.value,
        product_id,
        transaction_id,
        expires_at.isoformat() if expires_at else None,
    )

    result = (
        supa.table("entitlements")
        .upsert(row, on_conflict="source_platform,transaction_id")
        .execute()
    )
    data = getattr(result, "data", None) or []
    if not data:
        logger.warning(
            "[entitlements] upsert returned no row for user=%s txn=%s; "
            "treating as idempotent no-op",
            user_id,
            transaction_id,
        )
        return row
    return data[0]


# ── Tier and expiry helpers ───────────────────────────────────────────────


def tier_from_product_id(product_id: str) -> Tier:
    """Map a platform product identifier to a tier.

    The legacy `roam_unlimited` SKU maps to Lifetime (the grandfather case);
    the new v2 product IDs map to their named tiers. Unknown product IDs
    raise ValueError so the caller can decide whether to 400 or log-and-skip.
    """

    if product_id == settings.legacy_lifetime_sku:
        return Tier.LIFETIME
    if product_id == settings.product_id_month:
        return Tier.MONTH
    if product_id == settings.product_id_season:
        return Tier.SEASON
    if product_id == settings.product_id_lifetime:
        return Tier.LIFETIME
    raise ValueError(f"Unknown product_id: {product_id!r}")


def expiry_for_tier(tier: Tier, purchase_time: datetime) -> Optional[datetime]:
    """Compute the absolute expiry for a tier from the original purchase time.

    Lifetime has no expiry. Pass durations come from settings so they can be
    tuned per-environment without code change.
    """

    if tier == Tier.LIFETIME:
        return None
    if tier == Tier.MONTH:
        from datetime import timedelta

        return purchase_time + timedelta(days=settings.pass_month_days)
    if tier == Tier.SEASON:
        from datetime import timedelta

        return purchase_time + timedelta(days=settings.pass_season_days)
    if tier == Tier.FREE:
        return None
    raise ValueError(f"Unhandled tier: {tier!r}")


# ── Internal ──────────────────────────────────────────────────────────────


def _parse_iso(iso_str: str) -> datetime:
    """Parse an ISO8601 timestamp returned by Supabase.

    Supabase emits values like `2026-09-30T12:34:56.789+00:00`. Python's
    fromisoformat handles that since 3.11; the backend pins 3.11 in CI.
    """

    return datetime.fromisoformat(iso_str)
