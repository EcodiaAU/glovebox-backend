"""POST /entitlement/redeem - shipped glovebox-ios client contract.

The shipped iOS client (Sources/Glovebox/Services/Billing/EntitlementService +
StoreKitClient) posts a DIFFERENT shape from the `{platform, receipt}` shape in
test_entitlement_redeem.py:

    {"product_id": "...", "receipt_data": "<base64>", "source": "purchase"}

- No `platform` field (inferred as ios).
- `receipt_data` is the base64 App Store receipt blob, verified via
  `verify_app_store_receipt` (Apple /verifyReceipt), NOT a StoreKit-2 JWS.
- `source` is purchase / restore / grandfather.
- Both GET and redeem responses are wrapped under `entitlement`.

These tests stub `verify_app_store_receipt` so they exercise the route's iOS
shape handling, source threading, grandfather logic, and idempotency without
real receipts or Apple creds. They are the regression guard for the
client-contract reconciliation done 2026-06-01.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest


def _ios_payload(*, product_id: str, txn_id: str, grandfather: bool = False) -> Any:
    from app.services.apple_receipt import AppleTransactionPayload

    return AppleTransactionPayload(
        transaction_id=txn_id,
        original_transaction_id=txn_id,
        product_id=product_id,
        bundle_id="au.ecodia.roam",
        purchase_date=datetime.now(timezone.utc),
        is_grandfather_eligible=grandfather,
        raw_payload={"product_id": product_id, "transaction_id": txn_id},
    )


@pytest.fixture
def fake_receipt_verifier(monkeypatch):
    """Patch the base64-receipt verifier reference imported into the route."""
    import app.api.entitlement as ent_route

    holder: dict[str, Any] = {"payload": None, "error": None}

    def _verify(receipt_b64: str, *, expected_product_id=None):
        if holder["error"]:
            raise holder["error"]
        return holder["payload"]

    monkeypatch.setattr(ent_route, "verify_app_store_receipt", _verify)
    return holder


# ── iOS purchase shape (no platform, receipt_data, source) ─────────────────


class TestIOSClientPurchaseShape:
    def test_month_pass_purchase_grants_and_wraps(
        self, client, fake_receipt_verifier, fake_supabase, authed_user_id
    ):
        fake_receipt_verifier["payload"] = _ios_payload(
            product_id="glovebox_pass_month", txn_id="ios-blob-month-1"
        )
        resp = client.post(
            "/entitlement/redeem",
            json={
                "product_id": "glovebox_pass_month",
                "receipt_data": "BASE64RECEIPTBLOB==",
                "source": "purchase",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # iOS reads body["entitlement"]; granted/grandfathered are additive.
        assert body["granted"] is True
        assert body["grandfathered"] is False
        assert body["entitlement"]["tier"] == "month"
        assert body["entitlement"]["expires_at"] is not None
        assert body["entitlement"]["source"] == "purchase"

        rows = fake_supabase.tables["entitlements"]
        assert len(rows) == 1
        assert rows[0]["transaction_id"] == "ios-blob-month-1"
        assert rows[0]["source_platform"] == "ios"  # inferred
        assert rows[0]["source"] == "purchase"

    def test_lifetime_purchase_null_expiry(
        self, client, fake_receipt_verifier, fake_supabase, authed_user_id
    ):
        fake_receipt_verifier["payload"] = _ios_payload(
            product_id="glovebox_lifetime", txn_id="ios-blob-life-1"
        )
        body = client.post(
            "/entitlement/redeem",
            json={
                "product_id": "glovebox_lifetime",
                "receipt_data": "BLOB==",
                "source": "purchase",
            },
        ).json()
        assert body["entitlement"]["tier"] == "lifetime"
        assert body["entitlement"]["expires_at"] is None

    def test_restore_source_threads_through(
        self, client, fake_receipt_verifier, fake_supabase, authed_user_id
    ):
        fake_receipt_verifier["payload"] = _ios_payload(
            product_id="glovebox_pass_season", txn_id="ios-blob-restore-1"
        )
        body = client.post(
            "/entitlement/redeem",
            json={
                "product_id": "glovebox_pass_season",
                "receipt_data": "BLOB==",
                "source": "restore",
            },
        ).json()
        assert body["entitlement"]["source"] == "restore"
        assert fake_supabase.tables["entitlements"][0]["source"] == "restore"

    def test_missing_receipt_and_jws_returns_403(self, client, fake_receipt_verifier):
        resp = client.post(
            "/entitlement/redeem",
            json={"product_id": "glovebox_lifetime", "source": "purchase"},
        )
        assert resp.status_code == 403
        assert "receipt_data" in resp.json()["error"]

    def test_idempotent_on_duplicate_blob(
        self, client, fake_receipt_verifier, fake_supabase, authed_user_id
    ):
        fake_receipt_verifier["payload"] = _ios_payload(
            product_id="glovebox_pass_month", txn_id="ios-blob-dup-1"
        )
        first = client.post(
            "/entitlement/redeem",
            json={
                "product_id": "glovebox_pass_month",
                "receipt_data": "BLOB==",
                "source": "purchase",
            },
        )
        second = client.post(
            "/entitlement/redeem",
            json={
                "product_id": "glovebox_pass_month",
                "receipt_data": "BLOB==",
                "source": "purchase",
            },
        )
        assert first.status_code == 200
        assert second.status_code == 200
        assert len(fake_supabase.tables["entitlements"]) == 1


# ── Grandfather via source="grandfather" ──────────────────────────────────


class TestIOSGrandfather:
    def test_grandfather_via_receipt_roam_unlimited(
        self, client, fake_receipt_verifier, fake_supabase, authed_user_id
    ):
        """Receipt names roam_unlimited -> Lifetime, gated on the receipt."""
        fake_receipt_verifier["payload"] = _ios_payload(
            product_id="roam_unlimited", txn_id="ios-blob-legacy-1", grandfather=True
        )
        body = client.post(
            "/entitlement/redeem",
            json={
                # The iOS client sends glovebox_lifetime as the product_id for
                # the grandfather redeem; the receipt blob carries roam_unlimited.
                "product_id": "glovebox_lifetime",
                "receipt_data": "LEGACYBLOB==",
                "source": "grandfather",
            },
        ).json()
        assert body["granted"] is True
        assert body["grandfathered"] is True
        assert body["entitlement"]["tier"] == "lifetime"
        assert body["entitlement"]["source"] == "grandfather"
        rows = fake_supabase.tables["entitlements"]
        assert len(rows) == 1
        assert rows[0]["product_id"] == "roam_unlimited"
        assert rows[0]["source_platform"] == "ios"

    def test_grandfather_via_legacy_table_when_receipt_unverifiable(
        self, client, fake_receipt_verifier, fake_supabase, authed_user_id
    ):
        """No verifiable receipt, but the user has a v1 user_entitlements row."""
        from app.services.apple_receipt import ReceiptError

        fake_receipt_verifier["error"] = ReceiptError("no shared secret / dev blob")
        fake_supabase.tables["user_entitlements"] = [
            {
                "id": "22222222-2222-2222-2222-222222222222",
                "user_id": authed_user_id,
                "source": "revenuecat",
            }
        ]
        body = client.post(
            "/entitlement/redeem",
            json={
                "product_id": "glovebox_lifetime",
                "receipt_data": "GARBAGE",
                "source": "grandfather",
            },
        ).json()
        assert body["grandfathered"] is True
        assert body["entitlement"]["tier"] == "lifetime"
        # Deterministic synthetic txn id keeps re-redeems idempotent.
        rows = fake_supabase.tables["entitlements"]
        assert len(rows) == 1
        assert rows[0]["transaction_id"] == f"legacy-grandfather:{authed_user_id}"

    def test_grandfather_without_any_prior_purchase_returns_403(
        self, client, fake_receipt_verifier, fake_supabase, authed_user_id
    ):
        """source=grandfather but no receipt proof and no legacy row -> 403.

        The grant cannot be forged by a bare client claim.
        """
        from app.services.apple_receipt import ReceiptError

        fake_receipt_verifier["error"] = ReceiptError("unverifiable")
        # user_entitlements empty (default)
        resp = client.post(
            "/entitlement/redeem",
            json={
                "product_id": "glovebox_lifetime",
                "receipt_data": "GARBAGE",
                "source": "grandfather",
            },
        )
        assert resp.status_code == 403
        assert "roam_unlimited" in resp.json()["error"]
        assert fake_supabase.tables["entitlements"] == []

    def test_grandfather_no_receipt_at_all_uses_legacy_table(
        self, client, fake_receipt_verifier, fake_supabase, authed_user_id
    ):
        """Empty receipt_data + legacy row present -> grandfather succeeds."""
        fake_supabase.tables["user_entitlements"] = [
            {
                "id": "33333333-3333-3333-3333-333333333333",
                "user_id": authed_user_id,
                "source": "stripe",
            }
        ]
        body = client.post(
            "/entitlement/redeem",
            json={
                "product_id": "glovebox_lifetime",
                "receipt_data": "",
                "source": "grandfather",
            },
        ).json()
        assert body["grandfathered"] is True
        assert body["entitlement"]["tier"] == "lifetime"

    def test_grandfather_is_idempotent(
        self, client, fake_receipt_verifier, fake_supabase, authed_user_id
    ):
        fake_supabase.tables["user_entitlements"] = [
            {
                "id": "44444444-4444-4444-4444-444444444444",
                "user_id": authed_user_id,
                "source": "revenuecat",
            }
        ]
        body = {
            "product_id": "glovebox_lifetime",
            "receipt_data": "",
            "source": "grandfather",
        }
        client.post("/entitlement/redeem", json=body)
        client.post("/entitlement/redeem", json=body)
        assert len(fake_supabase.tables["entitlements"]) == 1


# ── Original {platform, receipt} shape still works (back-compat) ───────────
#
# The full JWS + explicit-platform path is covered in test_entitlement_redeem.py;
# this just confirms the dual-shape RedeemRequest didn't break the explicit
# `platform` field's rejection branches.


class TestPlatformShapeStillAccepted:
    def test_web_platform_still_rejected(self, client):
        resp = client.post(
            "/entitlement/redeem",
            json={
                "platform": "web",
                "product_id": "glovebox_lifetime",
                "receipt": {"stripe_session_id": "cs_x"},
            },
        )
        assert resp.status_code == 400
        assert "Stripe" in resp.json()["error"]

    def test_legacy_platform_still_rejected(self, client):
        resp = client.post(
            "/entitlement/redeem",
            json={
                "platform": "legacy",
                "product_id": "roam_unlimited",
                "receipt": {},
            },
        )
        assert resp.status_code == 400
