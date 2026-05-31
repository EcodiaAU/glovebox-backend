# app/api/stripe.py
#
# Stripe Checkout + Webhook + RevenueCat webhook endpoints.
# Migrated from frontend Next.js API routes to the FastAPI backend
# so they work with Capacitor static builds.

from __future__ import annotations

import logging
from typing import Optional

import stripe as stripe_lib
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core.auth import AuthUser, get_optional_user
from app.core.error_models import (
    CheckoutSessionResponse,
    ErrorResponse,
    ReceivedResponse,
    UnlockedResponse,
)
from app.core.settings import settings
from app.core.supabase_admin import get_supabase_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stripe", tags=["stripe"])


# ── Models ───────────────────────────────────────────────────────


class ConfirmRequest(BaseModel):
    session_id: str = Field(description="Stripe Checkout Session id, format `cs_...`")


# ── Helpers ──────────────────────────────────────────────────────


def _get_stripe() -> stripe_lib.StripeClient:
    if not settings.stripe_secret_key:
        raise RuntimeError("STRIPE_SECRET_KEY not configured")
    return stripe_lib.StripeClient(settings.stripe_secret_key)


def _error(message: str, status_code: int) -> JSONResponse:
    """Render the legacy `{"error": "..."}` shape with the matching HTTP code.

    Kept as a small helper so every route emits the same payload and OpenAPI
    references the same `ErrorResponse` model in its `responses=` block.
    """
    return JSONResponse(
        ErrorResponse(error=message).model_dump(), status_code=status_code
    )


async def _upsert_entitlement(
    user_id: str,
    source: str,
    *,
    stripe_customer_id: Optional[str] = None,
    stripe_payment_intent: Optional[str] = None,
    rc_app_user_id: Optional[str] = None,
) -> None:
    from datetime import datetime, timezone

    supa = get_supabase_admin()
    row = {
        "user_id": user_id,
        "source": source,
        "unlocked_at": datetime.now(timezone.utc).isoformat(),
    }
    if stripe_customer_id:
        row["stripe_customer_id"] = stripe_customer_id
    if stripe_payment_intent:
        row["stripe_payment_intent"] = stripe_payment_intent
    if rc_app_user_id:
        row["rc_app_user_id"] = rc_app_user_id

    logger.info(
        "[stripe] Upserting entitlement for user %s source=%s row=%s",
        user_id,
        source,
        row,
    )
    try:
        result = (
            supa.table("user_entitlements")
            .upsert(row, on_conflict="user_id,source")
            .execute()
        )
        if hasattr(result, "error") and result.error:
            logger.error(
                "[stripe] Supabase upsert error for user %s: %s", user_id, result.error
            )
        else:
            logger.info(
                "[stripe] Entitlement upserted successfully for user %s, data=%s",
                user_id,
                getattr(result, "data", None),
            )
    except Exception as exc:
        logger.error(
            "[stripe] Supabase upsert EXCEPTION for user %s: %s",
            user_id,
            exc,
            exc_info=True,
        )
        raise


# ── POST /stripe/checkout ────────────────────────────────────────


@router.post(
    "/checkout",
    response_model=CheckoutSessionResponse,
    responses={
        401: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def create_checkout_session(
    request: Request,
    user: Optional[AuthUser] = Depends(get_optional_user),
) -> CheckoutSessionResponse | JSONResponse:
    if not user:
        return _error("Unauthorized", 401)

    price_id = settings.stripe_price_id
    if not price_id:
        return _error("Payment not configured.", 500)

    origin = request.headers.get("origin", "https://roam.ecodia.au")

    client = _get_stripe()
    session = client.checkout.sessions.create(
        params={  # type: ignore[arg-type]
            "mode": "payment",
            "line_items": [{"price": price_id, "quantity": 1}],
            "metadata": {"supabase_user_id": user.id},
            "customer_email": user.email,
            "success_url": f"{origin}/purchase/success?session_id={{CHECKOUT_SESSION_ID}}",
            "cancel_url": f"{origin}/new",
            "allow_promotion_codes": True,
        }
    )

    return CheckoutSessionResponse(url=session.url)


# ── POST /stripe/confirm ─────────────────────────────────────────
# Called by the success page as a fallback when the webhook is slow.
# Verifies the checkout session directly with Stripe and grants the entitlement.


@router.post(
    "/confirm",
    response_model=UnlockedResponse,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
    },
)
async def confirm_checkout_session(
    body: ConfirmRequest,
    user: Optional[AuthUser] = Depends(get_optional_user),
) -> UnlockedResponse | JSONResponse:
    logger.info("[stripe/confirm] Called. user=%s", user.id if user else None)
    if not user:
        return _error("Unauthorized", 401)

    session_id = body.session_id
    if not session_id or not session_id.startswith("cs_"):
        logger.warning("[stripe/confirm] Invalid session_id: %r", session_id)
        return _error("Invalid session_id", 400)

    client = _get_stripe()
    try:
        session = client.checkout.sessions.retrieve(session_id)
    except Exception as exc:
        logger.error(
            "[stripe/confirm] Failed to retrieve session %s: %s", session_id, exc
        )
        return _error("Could not verify session", 502)

    logger.info(
        "[stripe/confirm] Session %s: payment_status=%s, metadata=%s, customer=%s",
        session_id,
        session.payment_status,
        session.metadata,
        session.customer,
    )

    # Verify the session belongs to this user
    session_user_id = (session.metadata or {}).get("supabase_user_id", "")
    if session_user_id != user.id:
        logger.warning(
            "[stripe/confirm] User %s tried to confirm session owned by %s",
            user.id,
            session_user_id,
        )
        return _error("Session does not belong to this user", 403)

    if session.payment_status != "paid":
        logger.info(
            "[stripe/confirm] Session %s not paid yet: %s",
            session_id,
            session.payment_status,
        )
        return UnlockedResponse(unlocked=False, payment_status=session.payment_status)

    customer = session.customer
    payment_intent = session.payment_intent
    try:
        await _upsert_entitlement(
            user.id,
            "stripe",
            stripe_customer_id=customer if isinstance(customer, str) else None,
            stripe_payment_intent=payment_intent
            if isinstance(payment_intent, str)
            else None,
        )
    except Exception as exc:
        logger.error(
            "[stripe/confirm] Entitlement upsert failed for user %s: %s",
            user.id,
            exc,
            exc_info=True,
        )
        return _error("Failed to grant entitlement", 500)

    logger.info(
        "[stripe/confirm] Entitlement granted for user %s via session %s",
        user.id,
        session_id,
    )
    return UnlockedResponse(unlocked=True)


# ── POST /stripe/webhook ─────────────────────────────────────────


@router.post(
    "/webhook",
    response_model=ReceivedResponse,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
    },
)
async def stripe_webhook(request: Request) -> JSONResponse:
    """Handles both Stripe and RevenueCat webhooks.

    Stripe sends a stripe-signature header; RevenueCat does not.

    Webhook bodies are vendor-defined and not modelled at the route signature
    layer; signature verification needs the raw request body, so Request stays.
    """

    is_stripe = "stripe-signature" in request.headers
    if is_stripe:
        return await _handle_stripe_webhook(request)
    return await _handle_revenuecat_webhook(request)


async def _handle_stripe_webhook(request: Request) -> JSONResponse:
    raw_body = await request.body()
    sig = request.headers.get("stripe-signature", "")
    webhook_secret = settings.stripe_webhook_secret

    if not sig or not webhook_secret:
        return _error("Missing signature", 400)

    try:
        event = stripe_lib.Webhook.construct_event(
            raw_body.decode(), sig, webhook_secret
        )
    except stripe_lib.SignatureVerificationError as exc:
        logger.error("[stripe/webhook] Signature verification failed: %s", exc)
        return _error("Invalid signature", 400)

    if event.type == "checkout.session.completed":
        session = event.data.object
        user_id = (session.get("metadata") or {}).get("supabase_user_id")
        if not user_id:
            logger.error(
                "[stripe/webhook] No supabase_user_id in session metadata: %s",
                session.get("id"),
            )
            return _error("No user ID in metadata", 400)

        customer = session.get("customer")
        payment_intent = session.get("payment_intent")
        await _upsert_entitlement(
            user_id,
            "stripe",
            stripe_customer_id=customer if isinstance(customer, str) else None,
            stripe_payment_intent=payment_intent
            if isinstance(payment_intent, str)
            else None,
        )
        logger.info("[stripe/webhook] Unlocked user %s via Stripe", user_id)

    return JSONResponse(ReceivedResponse(received=True).model_dump())


async def _handle_revenuecat_webhook(request: Request) -> JSONResponse:
    secret = settings.revenuecat_webhook_secret
    if secret:
        auth = (
            (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
        )
        if auth != secret:
            return _error("Unauthorized", 401)

    body = await request.json()
    event_type: str = (body.get("event") or {}).get("type", "")

    if event_type not in ("INITIAL_PURCHASE", "NON_RENEWING_PURCHASE"):
        return JSONResponse(ReceivedResponse(received=True).model_dump())

    rc_user_id: str = (body.get("event") or {}).get("app_user_id", "")
    if not rc_user_id:
        return _error("No app_user_id", 400)

    import re

    uuid_pattern = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
    )
    if not uuid_pattern.match(rc_user_id):
        logger.warning(
            "[rc/webhook] app_user_id is not a UUID - skipping: %s", rc_user_id
        )
        return JSONResponse(ReceivedResponse(received=True).model_dump())

    await _upsert_entitlement(rc_user_id, "revenuecat", rc_app_user_id=rc_user_id)
    logger.info("[rc/webhook] Unlocked user %s via RevenueCat", rc_user_id)

    return JSONResponse(ReceivedResponse(received=True).model_dump())


# ── POST /stripe/grant-manual (dev only) ──────────────────────────
# Grants entitlement to the authenticated user without Stripe.
# Only available when STRIPE_SECRET_KEY starts with "sk_test_".


@router.post(
    "/grant-manual",
    response_model=UnlockedResponse,
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
    },
)
async def grant_manual_entitlement(
    user: Optional[AuthUser] = Depends(get_optional_user),
) -> UnlockedResponse | JSONResponse:
    if not user:
        return _error("Unauthorized", 401)

    # Safety: only allow in test mode
    if not settings.stripe_secret_key.startswith("sk_test_"):
        return _error("Manual grant only available in test mode", 403)

    await _upsert_entitlement(user.id, "manual")
    logger.info("[stripe/grant-manual] Manual entitlement granted for user %s", user.id)
    return UnlockedResponse(unlocked=True, source="manual")
