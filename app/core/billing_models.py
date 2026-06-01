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
#
# Client-contract note (2026-06-01): the shipped glovebox-ios client
# (Sources/Glovebox/Services/Billing) reads BOTH endpoints as
# `{"entitlement": {tier, expires_at, source}}` (a single-key wrapper, decoded
# by its `EntitlementWrapper`) and POSTs redeem as
# `{product_id, receipt_data: <string>, source: "purchase|restore|grandfather"}`
# - no `platform` field, and `receipt_data` is the base64 App Store receipt
# blob, not a StoreKit-2 JWS. The schemas below therefore: (1) carry a
# human-facing `source` string on the entitlement object that the iOS client
# reads, (2) wrap GET in `EntitlementEnvelope`, and (3) accept the iOS redeem
# request shape alongside the original `{platform, receipt}` shape used by the
# Android/web generated clients. The iOS JSONDecoder ignores unknown keys, so
# the extra `source_platform`/`product_id`/`granted`/`grandfathered` fields are
# harmless to it while staying informative for the other clients.

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator


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


class RedeemSource(str, Enum):
    """Why the client is redeeming, as sent by the iOS client's `source` field.

    Informational on the server side except for `grandfather`, which lets a
    client signal "this is a legacy v1 buyer" so the server checks the v1
    `user_entitlements` / `roam_unlimited` purchase and grants Lifetime.
    """

    PURCHASE = "purchase"
    RESTORE = "restore"
    GRANDFATHER = "grandfather"


# ── GET /entitlement response ──────────────────────────────────────────────


class EntitlementResponse(BaseModel):
    """Current effective entitlement for the authenticated user.

    `tier=free` with `expires_at=null` and `source=free` is the no-purchase
    shape. `source` is the field the iOS client reads (`Entitlement.source`);
    `source_platform`/`product_id` are richer, optional, and ignored by iOS.
    """

    tier: Tier = Field(
        description="The user's current effective tier. `free` means no "
        "active pass and no grandfathered lifetime."
    )
    expires_at: Optional[str] = Field(
        default=None,
        description="ISO8601 UTC. Null for `lifetime` and `free`.",
    )
    source: Optional[str] = Field(
        default=None,
        description="Human-facing provenance the client surfaces: one of "
        "`free` / `purchase` / `restore` / `grandfather` / `legacy`. Derived "
        "from `source_platform` + how the row was written.",
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


class EntitlementEnvelope(BaseModel):
    """Single-key wrapper the native clients decode.

    The iOS client's `EntitlementWrapper` reads `{"entitlement": {...}}`; the
    Android/web generated clients read the same wrapper. GET and the redeem
    success body both nest the entitlement under this key.
    """

    entitlement: EntitlementResponse


# ── POST /entitlement/redeem request and response ──────────────────────────


class RedeemRequest(BaseModel):
    """Client-supplied receipt for server verification.

    Two accepted shapes:

    * iOS (shipped client): `{product_id, receipt_data: <base64 str>,
      source: "purchase|restore|grandfather"}`. No `platform` - it is inferred
      as `ios`. `receipt_data` is the base64 App Store receipt blob.
    * Android / web generated clients: `{platform, product_id, receipt}` where
      `receipt` is `{purchase_token, product_id}` (Android) or
      `{signed_transaction_info: <JWS>}` (a future StoreKit-2 iOS client).

    `model_validator` normalises the iOS shape into the canonical
    `(platform, receipt, redeem_source)` the route consumes, so the endpoint
    has one code path regardless of which client called it.
    """

    platform: Optional[SourcePlatform] = Field(
        default=None,
        description="Where the purchase was made. Inferred as `ios` when "
        "omitted (the shipped iOS client sends no platform). `legacy` is not "
        "accepted - it is a server-side synthesis only.",
    )
    product_id: str = Field(
        description="Platform product identifier (e.g. `glovebox_pass_month`)."
    )
    receipt: dict[str, Any] = Field(
        default_factory=dict,
        description="Platform-specific verification payload. "
        "iOS (StoreKit 2): `{signed_transaction_info: <JWS>}`. "
        "Android: `{purchase_token: str, product_id: str}`. "
        "Web: `{stripe_session_id: str}`.",
    )
    receipt_data: Optional[str] = Field(
        default=None,
        description="Base64 App Store receipt blob sent by the shipped iOS "
        "client. Folded into `receipt.receipt_data` during validation.",
    )
    source: Optional[RedeemSource] = Field(
        default=None,
        description="Why the client is redeeming. `grandfather` triggers the "
        "v1 legacy-purchase check that grants Lifetime.",
    )

    @model_validator(mode="after")
    def _normalise_ios_shape(self) -> "RedeemRequest":
        """Fold the shipped-iOS request shape into the canonical fields.

        - No `platform` -> default to iOS (the shipped client omits it).
        - `receipt_data` present -> merge it into `receipt` so the iOS verifier
          sees one envelope regardless of which field carried the blob.
        """

        if self.platform is None:
            self.platform = SourcePlatform.IOS
        if self.receipt_data and "receipt_data" not in self.receipt:
            # Don't mutate a shared dict in place beyond what we own.
            self.receipt = {**self.receipt, "receipt_data": self.receipt_data}
        return self

    @property
    def redeem_source(self) -> Optional[RedeemSource]:
        """Alias kept for readability at the call site."""

        return self.source


class RedeemResponse(BaseModel):
    """Result of a redeem call.

    Nests the resulting entitlement under `entitlement` so the iOS client's
    `EntitlementWrapper` decodes a redeem response with the same code path it
    uses for GET. `granted`/`grandfathered` are additive booleans the iOS
    client ignores and the Android/web clients can read.
    """

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
