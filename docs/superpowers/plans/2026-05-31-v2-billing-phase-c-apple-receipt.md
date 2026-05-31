# Phase C - Apple App Store Server API Receipt Verification

**Worker:** GB-BACKEND-02
**Date:** 2026-05-31
**Depends on:** phase A (entitlements substrate)

## Goal

Server-to-server verification of Apple StoreKit 2 signed transactions
(`JWSTransaction`). The iOS client sends the JWS string after a purchase or
restore; the server verifies the signature against Apple's root, validates
bundle id and basic claims, extracts `transactionId` + `productId` +
`originalPurchaseDate`. Tier and expiry derive from product id.

## Why JWS over the legacy receipt endpoint

The legacy `/verifyReceipt` endpoint accepts a base64 receipt blob and is
explicitly marked deprecated by Apple. The StoreKit 2 path sends JWS strings
that contain everything the server needs without an Apple round-trip; the
round-trip is reserved for transaction history queries (out of scope here).

## Scope

1. Add `app-store-server-library>=1.6.0` to `requirements.txt`. Apple's
   official library handles JWS decode plus Apple root cert chain verify.
2. Author `app/services/apple_receipt.py`:
   - `verify_signed_transaction(signed_jws: str) -> AppleTransactionPayload`
   - Loads Apple root certs from a configurable path. If unset, runs in
     decode-only mode with a loud warning so dev environments without the
     bundled certs do not silently accept unverified receipts.
   - Returns a structured payload with `transaction_id`, `product_id`,
     `bundle_id`, `purchase_date`, `original_transaction_id`,
     `is_grandfather_eligible` (true when `product_id` matches the legacy
     `roam_unlimited` SKU).
3. Settings additions: path to the Apple root cert bundle file.
4. `tests/test_apple_receipt.py`: mocks Apple's `SignedDataVerifier` so the
   tests do not need real root certs in CI. Covers: success path, bundle
   mismatch, expired signature, grandfather detection, dev-mode warning.

## Out of scope

- `POST /entitlement/redeem` endpoint integration (phase E).
- Apple App Store Server Notifications V2 webhook (separate arc).
- Server-side transaction history fetch (when client only has a userId
  and we need to look up prior purchases): later, only if needed.

## TDD outline

```
class TestVerifySignedTransaction:
    def test_verified_jws_returns_payload(monkeypatch):
        # Mocked verifier returns a JWSTransactionDecodedPayload
        # service returns AppleTransactionPayload with the right fields
    def test_bundle_mismatch_raises(monkeypatch):
        # Verifier raises VerificationException
        # service raises ReceiptError mapping to a 4xx in the route layer
    def test_legacy_roam_unlimited_marks_grandfather(monkeypatch):
        # product_id='roam_unlimited' -> is_grandfather_eligible=True
    def test_dev_mode_without_roots_logs_warning_and_decodes(monkeypatch, caplog):
        # No root cert path -> WARNING logged, payload still returned via
        # decode-only path
```

## Step-by-step

1. Add the dep to `requirements.txt`.
2. Settings: `apple_root_cert_bundle_path` + sandbox toggle.
3. Service module.
4. Tests with mocked verifier.
5. `pytest tests/ -q` green; locked OpenAPI regenerated (no new routes yet,
   service only).
6. Commit + push. Conductor deploys; conductor downloads Apple root certs
   to the Cloud Run container as part of the next deploy.

## Apple root certs - conductor task

Apple publishes its WWDR + Root CA certs at
https://www.apple.com/certificateauthority/. The container needs them on
disk. Plan: a tiny `scripts/fetch-apple-root-certs.sh` that pulls
`AppleRootCA-G3.cer` + `AppleWWDRCAG6.cer` into `app/data/apple-roots/`
during Docker build, with the entrypoint pointing the env var at the
directory. This worker authors the script; the conductor adds the wget
step to `Dockerfile` and verifies on next deploy.
