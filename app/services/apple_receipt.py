# app/services/apple_receipt.py
#
# Apple StoreKit 2 signed transaction (JWS) verification.
#
# The iOS client hands the server a `signedTransactionInfo` JWS string after
# a purchase or restore. The server verifies the JWS against Apple's root
# certificate chain, validates basic claims (bundle id matches), and
# extracts the transaction fields we need to mint an `entitlements` row
# (productId, transactionId, originalTransactionId, purchaseDate).
#
# Uses Apple's official `app-store-server-library` (in requirements.txt) for
# cert-chain + signature verification. The library encapsulates the WWDR root
# cert chain validation, JWS decode, and revocation checking.
#
# Operational note: production needs Apple's root certs on disk at
# settings.apple_root_cert_bundle_path. When that path is empty the service
# runs in decode-only mode and logs a loud WARNING per request, so a
# misconfigured prod environment is visible in logs rather than silently
# trusting any JWS the client supplies.

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.settings import settings

logger = logging.getLogger(__name__)


# ── Public types ──────────────────────────────────────────────────────────


class ReceiptError(Exception):
    """Raised when a JWS cannot be verified or fails basic claim checks.

    Callers map this to a 4xx response. The message is safe to surface to
    the client; the verifier never returns sensitive payload data on error.
    """


@dataclass
class AppleTransactionPayload:
    """Trustworthy subset of an Apple `JWSTransactionDecodedPayload`."""

    transaction_id: str
    original_transaction_id: str
    product_id: str
    bundle_id: str
    purchase_date: datetime
    is_grandfather_eligible: bool
    raw_payload: dict[str, Any]


# ── Verifier ──────────────────────────────────────────────────────────────


def verify_signed_transaction(signed_jws: str) -> AppleTransactionPayload:
    """Verify and decode an Apple StoreKit 2 `signedTransactionInfo` JWS.

    Returns an `AppleTransactionPayload` on success. Raises `ReceiptError`
    on any failure (signature, bundle mismatch, decode failure).
    """

    if not signed_jws or not isinstance(signed_jws, str):
        raise ReceiptError("signed_jws is required and must be a string")

    payload: dict[str, Any]

    root_certs = _load_root_certs(settings.apple_root_cert_bundle_path)
    if root_certs:
        payload = _verify_via_apple_library(signed_jws, root_certs)
    else:
        logger.warning(
            "[apple_receipt] APPLE_ROOT_CERT_BUNDLE_PATH empty; decoding JWS "
            "WITHOUT signature verification. Set the env var in production."
        )
        payload = _decode_jws_unverified(signed_jws)

    bundle_id = payload.get("bundleId") or payload.get("bundle_id")
    if bundle_id != settings.apple_app_bundle_id:
        raise ReceiptError(
            f"bundleId mismatch: receipt={bundle_id!r}, "
            f"expected={settings.apple_app_bundle_id!r}"
        )

    transaction_id = payload.get("transactionId") or payload.get("transaction_id")
    original_transaction_id = (
        payload.get("originalTransactionId")
        or payload.get("original_transaction_id")
        or transaction_id
    )
    product_id = payload.get("productId") or payload.get("product_id")
    purchase_date_ms = payload.get("purchaseDate") or payload.get("purchase_date") or 0

    if not transaction_id or not product_id:
        raise ReceiptError("receipt is missing transactionId or productId")

    return AppleTransactionPayload(
        transaction_id=str(transaction_id),
        original_transaction_id=str(original_transaction_id),
        product_id=str(product_id),
        bundle_id=str(bundle_id),
        purchase_date=_apple_ms_to_datetime(int(purchase_date_ms)),
        is_grandfather_eligible=(product_id == settings.legacy_lifetime_sku),
        raw_payload=payload,
    )


# ── Internal: Apple library path ──────────────────────────────────────────


def _verify_via_apple_library(
    signed_jws: str, root_certs: list[bytes]
) -> dict[str, Any]:
    """Use app-store-server-library's `SignedDataVerifier`.

    Lazy import: the library is heavy and not needed in dev when roots are
    unset. Tests monkeypatch this function rather than the library itself.
    """

    try:
        from appstoreserverlibrary.signed_data_verifier import (  # type: ignore[import-not-found]
            SignedDataVerifier,
        )
        from appstoreserverlibrary.models.Environment import (  # type: ignore[import-not-found]
            Environment,
        )
    except ImportError as exc:
        raise ReceiptError(
            "app-store-server-library not available; install via requirements.txt"
        ) from exc

    env = Environment.SANDBOX if settings.apple_use_sandbox else Environment.PRODUCTION
    app_apple_id = settings.apple_app_apple_id or None

    verifier = SignedDataVerifier(
        root_certificates=root_certs,
        enable_online_checks=False,
        environment=env,
        bundle_id=settings.apple_app_bundle_id,
        app_apple_id=app_apple_id,
    )

    try:
        decoded = verifier.verify_and_decode_signed_transaction(signed_jws)
    except Exception as exc:  # library raises VerificationException variants
        raise ReceiptError(f"Apple JWS verification failed: {exc}") from exc

    # The library returns a dataclass-like object with named attributes; the
    # path of least surprise is to coerce it to a dict via vars() so callers
    # see the same shape regardless of library version.
    try:
        return {k: v for k, v in vars(decoded).items() if not k.startswith("_")}
    except TypeError:
        # Some library versions return a plain dict-like; fall back.
        return dict(decoded)


# ── Internal: decode-only path (dev) ──────────────────────────────────────


def _decode_jws_unverified(signed_jws: str) -> dict[str, Any]:
    """Decode the JWS payload WITHOUT signature verification.

    Dev / CI use only. Production deploys MUST set
    `APPLE_ROOT_CERT_BUNDLE_PATH` so the real verification path runs.
    """

    parts = signed_jws.split(".")
    if len(parts) != 3:
        raise ReceiptError("malformed JWS: expected 3 segments")
    try:
        payload_b64 = parts[1]
        # JWS uses URL-safe base64 without padding. Add padding back.
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload_bytes = base64.urlsafe_b64decode(padded)
        return json.loads(payload_bytes)
    except Exception as exc:
        raise ReceiptError(f"JWS payload decode failed: {exc}") from exc


def _load_root_certs(path: str) -> list[bytes]:
    """Read Apple root cert files from a directory.

    Empty path returns []. Non-existent path returns [] with a warning so
    a typo in the env var does not silently fall through to decode-only.
    """

    if not path:
        return []
    p = Path(path)
    if not p.exists():
        logger.warning("[apple_receipt] root cert path %s does not exist", path)
        return []
    files = sorted(p.glob("*.cer")) + sorted(p.glob("*.pem"))
    return [f.read_bytes() for f in files]


def _apple_ms_to_datetime(ms: int) -> datetime:
    """Apple emits purchaseDate as ms since epoch. Convert to aware datetime."""

    if not ms:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


# ── Legacy base64 receipt-blob path (shipped iOS client) ───────────────────
#
# The shipped glovebox-ios client (StoreKitClient.receiptDataString) sends the
# base64 contents of `Bundle.main.appStoreReceiptURL` in the `receipt_data`
# field, NOT a StoreKit-2 JWS. That blob is verified against Apple's
# `/verifyReceipt` endpoint with the App-Specific Shared Secret. Apple still
# serves `/verifyReceipt` (it is "deprecated" in docs but fully operational and
# is the only server-side validator for the receipt-blob shape this client
# emits). The response's `in_app` array carries every purchase; we pick the
# transaction matching the requested product or the legacy `roam_unlimited`
# SKU. Mirrors the JWS path's dev-mode posture: no shared secret -> decode-only
# with a loud WARNING rather than silently trusting the client.


def verify_app_store_receipt(
    receipt_b64: str, *, expected_product_id: Optional[str] = None
) -> AppleTransactionPayload:
    """Verify a base64 App Store receipt blob and return the chosen transaction.

    `expected_product_id` is the product the client claims it bought; when the
    blob contains it we return that transaction. Otherwise we fall back to the
    most recent in-app purchase, and we always flag `is_grandfather_eligible`
    when the blob contains the legacy `roam_unlimited` SKU.

    Raises `ReceiptError` on any failure (empty blob, Apple status != 0,
    no in-app purchases).
    """

    if not receipt_b64 or not isinstance(receipt_b64, str):
        raise ReceiptError("receipt_data is required and must be a base64 string")

    secret = settings.apple_shared_secret
    if not secret:
        logger.warning(
            "[apple_receipt] APPLE_SHARED_SECRET empty; decoding receipt blob "
            "WITHOUT Apple verification. Set the env var in production."
        )
        response = _decode_receipt_unverified(receipt_b64)
    else:
        response = _verify_receipt_with_apple(receipt_b64, secret)

    in_app = _extract_in_app(response)
    if not in_app:
        raise ReceiptError("receipt contains no in-app purchases")

    legacy_present = any(
        (it.get("product_id") or it.get("productId")) == settings.legacy_lifetime_sku
        for it in in_app
    )

    chosen = _choose_transaction(in_app, expected_product_id)
    product_id = chosen.get("product_id") or chosen.get("productId")
    transaction_id = (
        chosen.get("transaction_id")
        or chosen.get("transactionId")
        or chosen.get("original_transaction_id")
        or chosen.get("originalTransactionId")
    )
    original_transaction_id = (
        chosen.get("original_transaction_id")
        or chosen.get("originalTransactionId")
        or transaction_id
    )
    # /verifyReceipt emits purchase_date_ms as a string of epoch ms.
    purchase_ms_raw = (
        chosen.get("purchase_date_ms")
        or chosen.get("purchaseDate")
        or chosen.get("original_purchase_date_ms")
        or 0
    )
    try:
        purchase_ms = int(purchase_ms_raw)
    except (TypeError, ValueError):
        purchase_ms = 0

    if not transaction_id or not product_id:
        raise ReceiptError("receipt transaction is missing transactionId or productId")

    return AppleTransactionPayload(
        transaction_id=str(transaction_id),
        original_transaction_id=str(original_transaction_id),
        product_id=str(product_id),
        bundle_id=str(
            (response.get("receipt") or {}).get("bundle_id")
            or settings.apple_app_bundle_id
        ),
        purchase_date=_apple_ms_to_datetime(purchase_ms),
        is_grandfather_eligible=(
            product_id == settings.legacy_lifetime_sku or legacy_present
        ),
        raw_payload=chosen,
    )


def _verify_receipt_with_apple(receipt_b64: str, secret: str) -> dict[str, Any]:
    """POST the receipt to Apple's /verifyReceipt; auto-retry against sandbox.

    Apple returns status 21007 when a sandbox receipt is sent to the prod
    endpoint; the documented handling is to retry against the sandbox URL.
    Lazy `httpx` import keeps the module importable without the network stack
    in environments that only exercise the decode-only path.
    """

    payload = {
        "receipt-data": receipt_b64,
        "password": secret,
        "exclude-old-transactions": True,
    }

    prod_url = settings.apple_verify_receipt_url
    sandbox_url = settings.apple_verify_receipt_sandbox_url
    first_url = sandbox_url if settings.apple_use_sandbox else prod_url

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(first_url, json=payload)
            data = resp.json()
            status = data.get("status")
            # 21007: this sandbox receipt was sent to production -> retry sandbox.
            if status == 21007 and first_url != sandbox_url:
                resp = client.post(sandbox_url, json=payload)
                data = resp.json()
                status = data.get("status")
            # 21008: this production receipt was sent to sandbox -> retry prod.
            elif status == 21008 and first_url != prod_url:
                resp = client.post(prod_url, json=payload)
                data = resp.json()
                status = data.get("status")
    except Exception as exc:  # network / json errors
        raise ReceiptError(f"Apple /verifyReceipt call failed: {exc}") from exc

    if data.get("status") != 0:
        raise ReceiptError(
            f"Apple /verifyReceipt rejected the receipt (status={data.get('status')})"
        )
    return data


def _decode_receipt_unverified(receipt_b64: str) -> dict[str, Any]:
    """Dev/CI fallback: treat the blob as a base64-encoded JSON /verifyReceipt
    response so tests + local dev can exercise the path without a shared secret.

    This is NOT real verification - production MUST set APPLE_SHARED_SECRET.
    A genuine App Store receipt is an ASN.1 PKCS#7 container, not JSON, so this
    path only succeeds for the test-shaped payloads that fixtures encode; a real
    device blob decoded here raises ReceiptError (caught upstream), which is the
    safe failure mode for a misconfigured environment.
    """

    try:
        padded = receipt_b64 + "=" * (-len(receipt_b64) % 4)
        raw = base64.b64decode(padded)
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ReceiptError("decoded receipt is not a JSON object")
        return parsed
    except ReceiptError:
        raise
    except Exception as exc:
        raise ReceiptError(
            f"receipt blob decode failed (no APPLE_SHARED_SECRET set, and the "
            f"blob is not dev-shaped JSON): {exc}"
        ) from exc


def _extract_in_app(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull the in-app purchase array from a /verifyReceipt response.

    Apple puts it at `latest_receipt_info` (preferred, newest first) and/or
    `receipt.in_app`. We coalesce both, preferring latest_receipt_info.
    """

    latest = response.get("latest_receipt_info")
    if isinstance(latest, list) and latest:
        return [it for it in latest if isinstance(it, dict)]
    receipt = response.get("receipt") or {}
    in_app = receipt.get("in_app")
    if isinstance(in_app, list):
        return [it for it in in_app if isinstance(it, dict)]
    return []


def _choose_transaction(
    in_app: list[dict[str, Any]], expected_product_id: Optional[str]
) -> dict[str, Any]:
    """Pick the transaction to grant from the in-app array.

    Prefer an exact product-id match (the product the client claims it bought).
    Otherwise prefer the legacy `roam_unlimited` SKU (grandfather), then fall
    back to the last element (newest, since latest_receipt_info is newest-first
    Apple-side but we don't re-sort here).
    """

    def _pid(it: dict[str, Any]) -> Optional[str]:
        return it.get("product_id") or it.get("productId")

    if expected_product_id:
        for it in in_app:
            if _pid(it) == expected_product_id:
                return it
    for it in in_app:
        if _pid(it) == settings.legacy_lifetime_sku:
            return it
    return in_app[0]
