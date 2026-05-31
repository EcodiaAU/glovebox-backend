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
