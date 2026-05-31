# app/core/billing_models.py
#
# Pydantic V2 schemas for the v2 tiered-entitlement model.
# Shared by:
#   - app/api/entitlement.py            (GET /entitlement, POST /entitlement/redeem)
#   - app/api/stripe.py                 (new v2 webhook path that writes tiered rows)
#   - app/services/entitlements.py      (core service that reads and writes rows)
#   - app/services/apple_receipt.py     (App Store Server API verification)
#   - app/services/play_purchase.py     (Play Developer API verification)
#
# The wire shape lands as `EntitlementResponse` in the generated Swift / Kotlin
# / TypeScript clients - keep field names stable.

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ── Tier and platform enums ────────────────────────────────────────────────


class Tier(str, Enum):
    """User entitlement tier.

    `free` is the absence-of-row default; no row in the `entitlements` table
    ever stores `'free'`. The enum carries it so the GET endpoint can return a
    typed value rather than null.
    """

    FREE = "free"
    MONTH = "month"
    SEASON = "season"
    LIFETIME = "lifetime"


class SourcePlatform(str, Enum):
    """Where the purchase was made (and which receipt verifier to trust).

    `legacy` is synthesised by the read path when the user has only a v1
    `user_entitlements` row (binary unlock, pre-v2). It never appears in the
    `entitlements` table itself.
    """

    IOS = "ios"
    ANDROID = "android"
    WEB = "web"
    LEGACY = "legacy"


# ── GET /entitlement response ──────────────────────────────────────────────


class EntitlementResponse(BaseModel):
    """Current effective entitlement for the authenticated user.

    `tier=free` with `expires_at=null` and `source_platform=null` is the
    no-purchase shape.
    """

    tier: Tier = Field(
        description="The user's current effective tier. `free` means no "
        "active pass and no grandfathered lifetime."
    )
    expires_at: Optional[str] = Field(
        default=None,
        description="ISO8601 UTC. Null for `lifetime` and `free`.",
    )
    source_platform: Optional[SourcePlatform] = Field(
        default=None,
        description="Where the entitlement was purchased. `legacy` means it "
        "came from the v1 `user_entitlements` grandfather path.",
    )
    product_id: Optional[str] = Field(
        default=None,
        description="The platform product identifier that granted this tier. "
        "Null for `free` and for `legacy` grandfather rows.",
    )


# ── POST /entitlement/redeem request and response ──────────────────────────


class RedeemRequest(BaseModel):
    """Client-supplied receipt for server verification.

    The `receipt` shape is platform-specific (Apple `signedTransactionInfo`
    JWS, Google purchase token + product id, or Stripe Checkout Session id).
    The receipt is opaque to the request schema; verification happens in the
    platform-specific service layer.
    """

    platform: SourcePlatform = Field(
        description="Where the purchase was made. `legacy` is not accepted by "
        "the redeem endpoint - it is a server-side synthesis only."
    )
    product_id: str = Field(
        description="Platform product identifier (e.g. `glovebox_pass_month`)."
    )
    receipt: dict[str, Any] = Field(
        description="Platform-specific verification payload. "
        "iOS: `{signed_transaction_info: <JWS>}`. "
        "Android: `{purchase_token: str, product_id: str}`. "
        "Web: `{stripe_session_id: str}`.",
    )


class RedeemResponse(BaseModel):
    """Result of a redeem call - same shape as GET /entitlement after success."""

    granted: bool = Field(
        description="True when the receipt verified and an entitlement row was "
        "written or matched an existing row by (platform, transaction_id)."
    )
    entitlement: EntitlementResponse = Field(
        description="The user's entitlement after the redeem attempt. On "
        "`granted=false` this is the prior tier unchanged."
    )
    grandfathered: bool = Field(
        default=False,
        description="True when the receipt named the legacy `roam_unlimited` "
        "SKU and the server granted Lifetime as a result.",
    )
