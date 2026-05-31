# app/api/entitlement.py
#
# v2 tiered-entitlement endpoints.
#
#   GET  /entitlement         - current effective tier for the authed user
#   POST /entitlement/redeem  - client-supplied receipt -> server verifies -> grant
#
# Phase A ships GET only. POST /redeem lands in phase E once the iOS / Play /
# Stripe verification services are in place.
#
# Both endpoints return `EntitlementResponse` shaped payloads so a successful
# redeem can be consumed by the same client code path that consumes GET.

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from app.core.auth import AuthUser, get_current_user
from app.core.billing_models import EntitlementResponse
from app.core.error_models import ErrorResponse
from app.services.entitlements import get_current_entitlement

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/entitlement", tags=["entitlement"])


@router.get(
    "",
    response_model=EntitlementResponse,
    responses={
        401: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def get_entitlement(
    user: AuthUser = Depends(get_current_user),
) -> EntitlementResponse:
    """Resolve and return the authenticated user's current effective tier.

    Source of truth for all three native clients (`glovebox-ios`,
    `glovebox-android`, `glovebox-web`). Resolution order is in
    `app/services/entitlements.py`. The endpoint is cheap (one indexed read,
    optional second read for the legacy grandfather check) and called on
    every cold start of the v2 clients plus before any paywalled action.
    """

    return get_current_entitlement(user.id)
