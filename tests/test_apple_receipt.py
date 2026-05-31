"""Phase C - Apple App Store Server API receipt verification.

The tests mock the inner verifier function so they don't need real Apple
root certs or a real signed JWS to run. Two layers exercised:

  1. `_verify_via_apple_library` - the production code path. Tests
     monkeypatch this to inject canned decoded payloads.
  2. `_decode_jws_unverified` - the dev-mode fallback. Tested with a real
     base64-encoded JWS payload (hand-constructed, no signature).
"""

from __future__ import annotations

import base64
import json

import pytest

from app.services.apple_receipt import (
    AppleTransactionPayload,
    ReceiptError,
    _apple_ms_to_datetime,
    _decode_jws_unverified,
    verify_signed_transaction,
)


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_unsigned_jws(payload: dict) -> str:
    """Hand-construct a JWS-shaped string with a known payload, no signature.

    Used to exercise the decode-only dev path. The header and signature
    sections are dummies; verification is intentionally bypassed.
    """

    header = base64.urlsafe_b64encode(b'{"alg":"ES256"}').rstrip(b"=").decode()
    body_b = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).rstrip(b"=")
    body = body_b.decode()
    return f"{header}.{body}.signature-placeholder"


# ── Decode-only path (dev) ───────────────────────────────────────────────


class TestDecodeJwsUnverified:
    def test_decodes_known_payload(self):
        payload = {
            "transactionId": "1000000000000001",
            "originalTransactionId": "1000000000000001",
            "productId": "glovebox_pass_month",
            "bundleId": "au.ecodia.roam",
            "purchaseDate": 1717000000000,
        }
        jws = _make_unsigned_jws(payload)
        result = _decode_jws_unverified(jws)
        assert result == payload

    def test_malformed_jws_raises(self):
        with pytest.raises(ReceiptError, match="malformed JWS"):
            _decode_jws_unverified("not.a.jws.with.too.many.parts")

    def test_garbled_payload_raises(self):
        with pytest.raises(ReceiptError, match="payload decode failed"):
            _decode_jws_unverified("aGVsbG8.!!!notbase64!!!.sig")


# ── verify_signed_transaction (decode-only path used in tests by default) ─


class TestVerifySignedTransactionDecodeOnly:
    """Default test env has no root cert path set, so the public function
    falls through to `_decode_jws_unverified`. Asserts the full pipeline:
    decode + bundle check + grandfather detection."""

    def test_happy_path(self, monkeypatch, caplog):
        payload = {
            "transactionId": "1000000000000001",
            "originalTransactionId": "1000000000000001",
            "productId": "glovebox_pass_month",
            "bundleId": "au.ecodia.roam",
            "purchaseDate": 1717000000000,
        }
        jws = _make_unsigned_jws(payload)
        from app.core import settings as settings_mod

        monkeypatch.setattr(settings_mod.settings, "apple_root_cert_bundle_path", "")

        with caplog.at_level("WARNING"):
            result = verify_signed_transaction(jws)

        assert isinstance(result, AppleTransactionPayload)
        assert result.transaction_id == "1000000000000001"
        assert result.product_id == "glovebox_pass_month"
        assert result.bundle_id == "au.ecodia.roam"
        assert result.is_grandfather_eligible is False
        # Dev-mode warning surfaces
        assert any(
            "WITHOUT signature verification" in rec.getMessage()
            for rec in caplog.records
        )

    def test_bundle_id_mismatch_raises(self, monkeypatch):
        payload = {
            "transactionId": "tx",
            "productId": "glovebox_pass_month",
            "bundleId": "com.someone.else",
            "purchaseDate": 1717000000000,
        }
        jws = _make_unsigned_jws(payload)
        from app.core import settings as settings_mod

        monkeypatch.setattr(settings_mod.settings, "apple_root_cert_bundle_path", "")

        with pytest.raises(ReceiptError, match="bundleId mismatch"):
            verify_signed_transaction(jws)

    def test_legacy_sku_flags_grandfather(self, monkeypatch):
        payload = {
            "transactionId": "tx_v1",
            "productId": "roam_unlimited",
            "bundleId": "au.ecodia.roam",
            "purchaseDate": 1700000000000,
        }
        jws = _make_unsigned_jws(payload)
        from app.core import settings as settings_mod

        monkeypatch.setattr(settings_mod.settings, "apple_root_cert_bundle_path", "")

        result = verify_signed_transaction(jws)
        assert result.is_grandfather_eligible is True
        assert result.product_id == "roam_unlimited"

    def test_missing_transaction_id_raises(self, monkeypatch):
        payload = {
            "productId": "glovebox_pass_month",
            "bundleId": "au.ecodia.roam",
        }
        jws = _make_unsigned_jws(payload)
        from app.core import settings as settings_mod

        monkeypatch.setattr(settings_mod.settings, "apple_root_cert_bundle_path", "")

        with pytest.raises(ReceiptError, match="missing transactionId"):
            verify_signed_transaction(jws)

    def test_empty_jws_raises(self):
        with pytest.raises(ReceiptError, match="signed_jws is required"):
            verify_signed_transaction("")


# ── verify_signed_transaction (verified path, mocked) ────────────────────


class TestVerifySignedTransactionViaLibrary:
    """The library path: monkeypatch `_verify_via_apple_library` so tests
    don't depend on Apple's library being importable or real root certs."""

    def test_library_path_used_when_roots_present(self, monkeypatch, tmp_path):
        # Seed a non-empty cert directory.
        cert_dir = tmp_path / "apple-roots"
        cert_dir.mkdir()
        (cert_dir / "AppleRootCA-G3.cer").write_bytes(b"fake-cert-bytes")

        from app.core import settings as settings_mod
        from app.services import apple_receipt as ar

        monkeypatch.setattr(
            settings_mod.settings, "apple_root_cert_bundle_path", str(cert_dir)
        )

        canned = {
            "transactionId": "9000000000000001",
            "originalTransactionId": "9000000000000001",
            "productId": "glovebox_lifetime",
            "bundleId": "au.ecodia.roam",
            "purchaseDate": 1717000000000,
        }

        def _fake_verify(_jws: str, _certs):
            return canned

        monkeypatch.setattr(ar, "_verify_via_apple_library", _fake_verify)

        result = verify_signed_transaction("any.string.here")
        assert result.product_id == "glovebox_lifetime"
        assert result.transaction_id == "9000000000000001"

    def test_library_verification_failure_propagates(self, monkeypatch, tmp_path):
        cert_dir = tmp_path / "apple-roots"
        cert_dir.mkdir()
        (cert_dir / "AppleRootCA-G3.cer").write_bytes(b"fake-cert-bytes")

        from app.core import settings as settings_mod
        from app.services import apple_receipt as ar

        monkeypatch.setattr(
            settings_mod.settings, "apple_root_cert_bundle_path", str(cert_dir)
        )

        def _fake_verify(_jws, _certs):
            raise ReceiptError("Apple JWS verification failed: bad signature")

        monkeypatch.setattr(ar, "_verify_via_apple_library", _fake_verify)

        with pytest.raises(ReceiptError, match="verification failed"):
            verify_signed_transaction("any.string.here")


# ── _apple_ms_to_datetime ─────────────────────────────────────────────────


class TestAppleMsToDatetime:
    def test_zero_ms_returns_epoch(self):
        dt = _apple_ms_to_datetime(0)
        assert dt.year == 1970

    def test_known_ms(self):
        # 1717000000000 ms = 2024-05-29 something UTC
        dt = _apple_ms_to_datetime(1717000000000)
        assert dt.year == 2024
        assert dt.month == 5
        assert dt.tzinfo is not None
