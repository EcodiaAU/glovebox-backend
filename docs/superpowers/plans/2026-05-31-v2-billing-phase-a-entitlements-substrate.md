# Phase A - v2 Billing Entitlements Substrate

**Worker:** GB-BACKEND-02
**Date:** 2026-05-31
**Spec:** `D:/.code/glovebox/v2-billing-model-spec.md`
**Architectural context:** `D:/.code/EcodiaOS/backend/docs/superpowers/specs/2026-05-31-glovebox-v2-native-rebuild-design.md`

## Goal

Land the core entitlements substrate (table + Pydantic types + service +
read endpoint) so the rest of the billing arc has something to write to.
This phase ships first because every subsequent phase (Stripe / iOS / Android
/ redeem) depends on the data shape locked here.

## Scope

1. Supabase migration `009_v2_entitlements.sql` creating `public.entitlements`
   with the shape defined in the brief: `user_id` + `tier` + `expires_at` +
   `source_platform` + `transaction_id` + `granted_at` + `raw_receipt`.
2. Pydantic V2 schemas in `app/core/billing_models.py` covering the tier enum,
   source-platform enum, current-entitlement response, redeem request, redeem
   response.
3. Settings additions in `app/core/settings.py` for ASC API key creds (issuer,
   key id, p8 base64), Play service-account JSON path, and the three new
   product IDs (with sensible defaults so dev runs don't crash).
4. Core service `app/services/entitlements.py` with `get_current_tier(user_id)`
   that resolves: lifetime row -> most recent active pass row -> grandfather
   from legacy `user_entitlements` row -> free.
5. New route module `app/api/entitlement.py` exposing `GET /entitlement`,
   registered into `api_router` in `app/api/__init__.py`.
6. Test `tests/test_entitlement_get.py` asserting the four resolution branches
   above, plus shape-level assertions matching `EntitlementResponse`.

## Out of scope (later phases)

- `POST /entitlement/redeem` (phase E)
- Apple App Store Server API receipt verification (phase C)
- Google Play Developer API verification (phase D)
- Stripe webhook extension for new SKUs (phase B)
- Product ID provisioning in ASC / Play / Stripe (phase F)

## Data model decisions

### Why a new table rather than augmenting `user_entitlements`

- v1 (Cap) still writes to `user_entitlements` via RevenueCat and Stripe
  webhooks. v1 will keep shipping during the v2 build. Splitting tables
  avoids mid-flight migration risk on a live binary-unlock substrate.
- The tier model only makes sense in v2. Putting nullable `tier`/`expires_at`
  on the legacy table would make every legacy row "undefined tier" and force
  a one-time backfill that we can't safely do without knowing each historic
  purchase's intent.
- Grandfather is cleaner as a *read-side join* than a *write-side migration*:
  if a v2 client redeems and the user has a legacy row, the service treats
  them as Lifetime and writes a fresh row to `entitlements` mirroring that
  fact. The legacy row stays as the audit trail.

### Why `(source_platform, transaction_id)` is the idempotency key

- Apple, Google, and Stripe all expose stable per-transaction IDs. Redeeming
  the same receipt twice from a flaky client should be a no-op.
- Composite key, not just `transaction_id` alone, because the three platforms'
  ID spaces don't promise global uniqueness.

### Why no `'free'` tier row

- Absence of any active row = free. Cheaper than storing a row per signup
  and prevents the "I downgraded to free" trap when a pass expires.

## TDD outline

### test_entitlement_get.py

```
class TestGetEntitlement:
    def test_no_rows_returns_free(self, client, authed_user):
        # No entitlements, no legacy row -> tier='free', expires_at=None
        ...

    def test_active_month_pass_returns_month(self, client, authed_user, fake_supabase):
        # entitlements row tier='month' expires_at=now()+15d -> tier='month'
        ...

    def test_expired_pass_returns_free(self, client, authed_user, fake_supabase):
        # entitlements row tier='month' expires_at=now()-1d -> tier='free'
        ...

    def test_lifetime_row_returns_lifetime(self, client, authed_user, fake_supabase):
        # entitlements row tier='lifetime' expires_at=None -> tier='lifetime'
        ...

    def test_legacy_user_entitlements_row_grandfathers_to_lifetime(self, ...):
        # No entitlements row, but user_entitlements row exists -> tier='lifetime'
        # source_platform='legacy' in response
        ...

    def test_unauthed_returns_401(self, client):
        ...
```

`fake_supabase` is a small fixture that monkeypatches `get_supabase_admin()`
to return an in-memory dict-table double. The existing test suite (1 file,
`test_openapi.py`) does not yet test routes; this phase introduces the first
authed-route test pattern. The double avoids hitting production Supabase from
CI; integration testing against a real branch DB is a later concern.

### test_openapi.py

Existing test must stay green: `GET /entitlement` needs an explicit
`response_model` so the strict-Pydantic check passes, and the route must show
up in the regenerated `openapi.json` with the locked 3.1.0 baseline updated.

## Step-by-step

1. Author `frontend/supabase/migrations/009_v2_entitlements.sql`. Idempotent
   (`create table if not exists`, `create unique index if not exists`). RLS
   on, with a "read own row" policy; writes are service-role only.
2. Apply via Supabase Management API against `vzauarlfmkjfkcphojbd` using the
   org PAT at `D:/PRIVATE/ecodia-creds/supabase.env`. Verify the table is
   visible.
3. Author `app/core/billing_models.py` with `Tier`, `SourcePlatform`,
   `EntitlementResponse`, `RedeemRequest`, `RedeemResponse` (the redeem
   shapes get used by phase E but cost nothing to define now and keep the
   models file from being split later).
4. Extend `app/core/settings.py` with new fields. All optional with safe
   defaults so missing creds degrade gracefully.
5. Author `app/services/entitlements.py` with `get_current_tier(user_id)`.
   Pure function over the Supabase client; no FastAPI imports in the service
   layer.
6. Author `app/api/entitlement.py` with the route handler.
7. Wire into `app/api/__init__.py`.
8. Author `tests/conftest.py` (if absent) with the FastAPI `TestClient`
   fixture + `fake_supabase` fixture. Author `tests/test_entitlement_get.py`.
9. Regenerate locked OpenAPI via the same incantation CI uses:
   `python -c "import json; from app.main import app; print(json.dumps(app.openapi(), indent=2, sort_keys=True))" > docs/openapi-3.1.0-locked.json`.
10. `pytest tests/ -q` must be green.
11. Commit. Conductor deploys to Cloud Run.

## Acceptance

- `GET /entitlement` with a valid Supabase JWT returns
  `{ "tier": "free|month|season|lifetime", "expires_at": null | iso8601,
  "source_platform": null | "ios|android|web|legacy" }`.
- All six tests in `test_entitlement_get.py` pass.
- `test_openapi.py` passes against the regenerated locked baseline.
- Migration file is on disk; conductor applies it to prod Supabase.
