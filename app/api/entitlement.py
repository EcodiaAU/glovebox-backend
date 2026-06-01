# app/api/entitlement.py
#
# v2 tiered-entitlement endpoints.
#
#   GET  /entitlement         - current effective tier for the authed user
#   POST /entitlement/redeem  - client-supplied receipt -> server verifies -> grant
#
# Both endpoints nest the entitlement under an `entitlement` key so the shipped
# glovebox-ios client decodes a redeem response with the same `EntitlementWrapper`
# code path it uses for GET. GET returns `EntitlementEnvelope`; redeem returns
# `RedeemResponse` (which also carries the additive `granted` / `grandfathered`
# booleans the Android/web generated clients read and iOS ignores).
#
# Request-shape compatibility: redeem accepts BOTH the shipped-iOS shape
# (`{product_id, receipt_data, source}`, no `platform`) and the original
# `{platform, receipt}` shape. See app/core/billing_models.py::RedeemRequest.

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.core.auth import AuthUser, get_current_user
from app.core.billing_models import (
    EntitlementEnvelope,
    RedeemRequest,
    RedeemResponse,
    RedeemSource,
    SourcePlatform,
    Tier,
)
from app.core.error_models import ErrorResponse
from app.core.settings import settings
from app.services.apple_receipt import (
    AppleTransactionPayload,
    ReceiptError,
    verify_app_store_receipt,
    verify_signed_transaction,
)
from app.services.entitlements import (
    expiry_for_tier,
    get_current_entitlement,
    tier_from_product_id,
    upsert_entitlement,
    user_has_legacy_entitlement,
)
from app.services.play_purchase import (
    PlayPurchaseError,
    PlayPurchasePayload,
    verify_purchase_token,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/entitlement", tags=["entitlement"])


def _error(message: str, status_code: int) -> JSONResponse:
    """Match the existing `{"error": "..."}` shape used across the backend."""

    return JSONResponse(
        ErrorResponse(error=message).model_dump(), status_code=status_code
    )


@router.get(
    "",
    response_model=EntitlementEnvelope,
    responses={
        401: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def get_entitlement(
    user: AuthUser = Depends(get_current_user),
) -> EntitlementEnvelope:
    """Resolve and return the authenticated user's current effective tier.

    Source of truth for all three native clients (`glovebox-ios`,
    `glovebox-android`, `glovebox-web`). Wrapped under `entitlement` so the
    shipped iOS client's `EntitlementWrapper` decodes it directly. Resolution
    order is in `app/services/entitlements.py`. The endpoint is cheap (one
    indexed read, optional second read for the legacy grandfather check) and
    called on every cold start of the v2 clients plus before any paywalled
    action.
    """

    return EntitlementEnvelope(entitlement=get_current_entitlement(user.id))


@router.post(
    "/redeem",
    response_model=RedeemResponse,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
    },
)
async def redeem_entitlement(
    body: RedeemRequest,
    user: AuthUser = Depends(get_current_user),
) -> RedeemResponse | JSONResponse:
    """Verify a platform-specific receipt and grant the corresponding tier.

    iOS and Android receipts are verified server-to-server against Apple's
    JWS public keys / Google's Play Developer API. The web path is
    deliberately rejected: web purchases flow through the Stripe Checkout +
    webhook path, never through this endpoint.

    Grandfather behaviour: there are two ways a redeem grants Lifetime as a
    grandfather. (1) The verified receipt names the legacy `roam_unlimited`
    SKU. (2) The shipped iOS client sends `source="grandfather"` (it detected
    the v1 non-consumable in `Transaction.currentEntitlements`); the server
    confirms a prior v1 purchase via the `user_entitlements` table or the
    receipt before granting, so the grant is gated on a real purchase signal
    rather than a bare client claim. Either way the response's
    `grandfathered=True` flag tells the client to update its UI.

    Idempotency: the `(source_platform, transaction_id)` unique index on
    `public.entitlements` absorbs duplicate redemptions from flaky clients
    or auto-retries. The endpoint always returns the user's current
    effective entitlement after the call, so a duplicate redeem returns
    the same `granted=True` shape as the first one.
    """

    if body.platform == SourcePlatform.LEGACY:
        return _error("legacy platform is read-side only", 400)
    if body.platform == SourcePlatform.WEB:
        return _error(
            "web purchases use the Stripe webhook; do not POST /entitlement/redeem",
            400,
        )

    # Grandfather short-circuit (shipped iOS client path). The client signals a
    # legacy v1 buyer with source="grandfather"; we confirm the prior purchase
    # before granting Lifetime so the grant can't be forged by a bare claim.
    if body.redeem_source == RedeemSource.GRANDFATHER:
        return _redeem_grandfather(body, user)

    try:
        if body.platform == SourcePlatform.IOS:
            verified = _verify_ios(body)
        elif body.platform == SourcePlatform.ANDROID:
            verified = _verify_android(body)
        else:
            return _error(f"unsupported platform {body.platform.value!r}", 400)
    except (ReceiptError, PlayPurchaseError) as exc:
        logger.info(
            "[entitlement/redeem] verification rejected for user=%s platform=%s: %s",
            user.id,
            body.platform.value if body.platform else "ios",
            exc,
        )
        return _error(str(exc), 403)
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(
            "[entitlement/redeem] verifier raised unexpected error for user=%s: %s",
            user.id,
            exc,
            exc_info=True,
        )
        return _error("receipt verification failed", 502)

    # Resolve tier. Grandfather wins: if the verified receipt names the legacy
    # roam_unlimited SKU we always grant Lifetime, even if the client asked
    # for a Month pass with the wrong product id.
    grandfathered = verified["is_grandfather_eligible"]
    if grandfathered:
        tier = Tier.LIFETIME
    else:
        try:
            tier = tier_from_product_id(verified["product_id"])
        except ValueError as exc:
            return _error(str(exc), 400)
        if tier == Tier.FREE:
            return _error("verified product maps to free tier", 400)

    # Human-facing source: grandfather wins; else the client's stated source
    # (purchase/restore), defaulting to purchase.
    human_source = (
        "grandfather"
        if grandfathered
        else (body.redeem_source.value if body.redeem_source else "purchase")
    )

    try:
        upsert_entitlement(
            user_id=user.id,
            tier=tier,
            source_platform=body.platform or SourcePlatform.IOS,
            product_id=verified["product_id"],
            transaction_id=verified["transaction_id"],
            expires_at=expiry_for_tier(tier, verified["purchase_date"]),
            raw_receipt=verified["raw_payload"],
            source=human_source,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(
            "[entitlement/redeem] upsert failed for user=%s txn=%s: %s",
            user.id,
            verified["transaction_id"],
            exc,
            exc_info=True,
        )
        return _error("could not record entitlement", 502)

    current = get_current_entitlement(user.id)
    return RedeemResponse(
        granted=True,
        entitlement=current,
        grandfathered=grandfathered,
    )


# ── Per-platform verification adapters ────────────────────────────────────


def _verify_ios(body: RedeemRequest) -> dict[str, Any]:
    """Verify an iOS receipt, accepting both the JWS and base64-blob shapes.

    Two shapes, in priority order:

    * StoreKit-2 JWS: `receipt: {"signed_transaction_info": "<JWS>"}`. Verified
      against Apple's root cert chain by `verify_signed_transaction`.
    * Legacy base64 receipt blob (the shipped iOS client): `receipt_data` (or
      `receipt: {"receipt_data": ...}`). Verified against Apple's
      `/verifyReceipt` by `verify_app_store_receipt`.

    Raises ReceiptError when neither is present or verification fails.
    """

    signed = body.receipt.get("signed_transaction_info") or body.receipt.get(
        "signedTransactionInfo"
    )
    if signed:
        payload: AppleTransactionPayload = verify_signed_transaction(signed)
    else:
        receipt_blob = body.receipt_data or body.receipt.get("receipt_data")
        if not receipt_blob:
            raise ReceiptError(
                "ios receipt must contain signed_transaction_info (JWS string) "
                "or receipt_data (base64 App Store receipt)"
            )
        payload = verify_app_store_receipt(
            receipt_blob, expected_product_id=body.product_id
        )
    return {
        "transaction_id": payload.transaction_id,
        "product_id": payload.product_id,
        "purchase_date": payload.purchase_date,
        "is_grandfather_eligible": payload.is_grandfather_eligible,
        "raw_payload": payload.raw_payload,
    }


def _verify_android(body: RedeemRequest) -> dict[str, Any]:
    """Pull the Play purchase token + product id from the envelope and verify.

    The client sends `receipt: {"purchase_token": "...", "product_id": "..."}`
    (`product_id` in the envelope may differ from `body.product_id` for the
    grandfather case; the envelope value is authoritative for the Play
    lookup so the server can verify the legacy SKU when the client doesn't
    know to ask for it).
    """

    token = body.receipt.get("purchase_token") or body.receipt.get("purchaseToken")
    inner_product_id = (
        body.receipt.get("product_id")
        or body.receipt.get("productId")
        or body.product_id
    )
    if not token:
        raise PlayPurchaseError("android receipt must contain purchase_token")
    payload: PlayPurchasePayload = verify_purchase_token(
        purchase_token=token, product_id=inner_product_id
    )
    if payload.purchase_state != 0:
        raise PlayPurchaseError(
            f"play purchase_state is {payload.purchase_state}, expected 0 (purchased)"
        )
    return {
        "transaction_id": payload.transaction_id,
        "product_id": payload.product_id,
        "purchase_date": payload.purchase_date,
        "is_grandfather_eligible": payload.is_grandfather_eligible,
        "raw_payload": payload.raw_payload,
    }


# ── Grandfather path (shipped iOS client, source="grandfather") ────────────


def _redeem_grandfather(
    body: RedeemRequest, user: AuthUser
) -> RedeemResponse | JSONResponse:
    """Grant Lifetime to a confirmed legacy v1 buyer.

    The grant is gated on a real prior-purchase signal so it can't be forged:

    1. A `roam_unlimited` transaction in the verified App Store receipt
       (`receipt_data`), OR
    2. an existing `user_entitlements` row for this user.

    When neither holds, returns 403. When granted, writes an idempotent
    Lifetime row (`source_platform=ios`, `source=grandfather`) keyed on the
    real Apple transaction id when available, else a deterministic synthetic
    id so re-redeems are no-ops.
    """

    transaction_id: str | None = None
    raw_receipt: dict[str, Any] | None = None
    receipt_confirmed_legacy = False

    receipt_blob = body.receipt_data or body.receipt.get("receipt_data")
    if receipt_blob:
        try:
            payload = verify_app_store_receipt(
                receipt_blob, expected_product_id=settings.legacy_lifetime_sku
            )
            raw_receipt = payload.raw_payload
            if payload.is_grandfather_eligible:
                receipt_confirmed_legacy = True
                transaction_id = payload.transaction_id
        except ReceiptError as exc:
            # A bad/dev-shaped blob is not fatal here - we can still grandfather
            # off the legacy table below. Log and continue.
            logger.info(
                "[entitlement/redeem] grandfather receipt not verifiable for "
                "user=%s (%s); falling back to legacy-table check",
                user.id,
                exc,
            )

    if not receipt_confirmed_legacy and not user_has_legacy_entitlement(user.id):
        return _error("no prior roam_unlimited purchase found to grandfather", 403)

    if not transaction_id:
        # Deterministic synthetic id so repeat grandfather redeems are
        # idempotent on (source_platform, transaction_id).
        transaction_id = f"legacy-grandfather:{user.id}"

    try:
        upsert_entitlement(
            user_id=user.id,
            tier=Tier.LIFETIME,
            source_platform=SourcePlatform.IOS,
            product_id=settings.legacy_lifetime_sku,
            transaction_id=transaction_id,
            expires_at=None,
            raw_receipt=raw_receipt,
            source="grandfather",
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(
            "[entitlement/redeem] grandfather upsert failed for user=%s: %s",
            user.id,
            exc,
            exc_info=True,
        )
        return _error("could not record entitlement", 502)

    current = get_current_entitlement(user.id)
    return RedeemResponse(granted=True, entitlement=current, grandfathered=True)
