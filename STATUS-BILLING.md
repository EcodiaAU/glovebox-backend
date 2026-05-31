# GB-BACKEND-02 - v2 Billing Status

Worker chat owning the v2 billing-model implementation on glovebox-backend
end-to-end. Conductor reads this file to verify progress; updated after every
feature batch.

## Phase
**Worker arc COMPLETE 2026-05-31.** All 5 code phases shipped + pushed.
Phase F is conductor-only operational work (GUI gates + cred grants).
Provisioning scripts and Dockerfile hook authored; conductor runs them
with the right creds in env.

**Worker exits cleanly. Conductor handoff list below.**

## Discoveries flagged to conductor (need conductor decision or action)

1. **Deploy target is Google Cloud Run, NOT Fly.io.** Brief said "deploys via
   `flyctl deploy` per backend/fly.toml". Reality: `backend/fly.toml` is for the
   unrelated `roam-edges-db` Postgres app. The API deploys to
   `https://roam-backend-176723812810.australia-southeast1.run.app` (Cloud Run,
   Sydney, project `roam-backend-176723812810` in GCP). The entrypoint.sh mounts
   `/mnt/roam-cache` via GCSFuse - Cloud Run idiom, not Fly. This worker chat
   cannot `gcloud run deploy` without `gcloud` auth in this session; the
   conductor should run the deploy after each batch ships to main.

2. **Supabase schema lives in `D:/.code/glovebox/frontend/supabase/migrations/`,
   NOT `backend/`.** Brief said "Migration via Supabase migration file" without
   path; only one migrations dir exists in the repo tree. New v2 migrations
   land at `frontend/supabase/migrations/009_v2_entitlements.sql` etc.
   Production project ref: `vzauarlfmkjfkcphojbd` (ROAM in CLAUDE.md project
   table). Org PAT at `D:/PRIVATE/ecodia-creds/supabase.env` reaches it.

3. **`GLOVEBOX_BACKEND_BOT_TOKEN` provisioning.** Brief said the token may be
   at `kv_store.creds.github_glovebox_backend_bot`. Probed EcodiaOS app
   Supabase (`nxmtfzofemtrlezlyhcj`): row absent. Either the cred has never
   been minted (likely, since the bump-clients workflow only shipped at commit
   2c58639) or it lives elsewhere. The workflow degrades gracefully without it
   (uploads artifact + warns), so this is non-blocking for code work. Conductor
   should mint the fine-grained PAT (scopes: repo, contents:write, metadata:read,
   on `EcodiaTate/glovebox-ios` + `glovebox-android` + `glovebox-web`) and
   stash both at `kv_store.creds.github_glovebox_backend_bot` AND as the GH
   Actions secret `GLOVEBOX_BACKEND_BOT_TOKEN` on `EcodiaTate/glovebox-backend`.

4. **RevenueCat is alive in v1.** The existing v1 (Cap) frontend buys
   `roam_unlimited` via RevenueCat, RC fires its webhook, server writes
   `user_entitlements`. Brief direction for v2 is **direct server-to-server
   receipt validation against Apple App Store Server API + Google Play
   Developer API**, bypassing RC entirely. This worker treats RC as
   v1-frozen substrate; new code goes to direct paths.

5. **Apple App Store Server API needs ASC API credentials.** Server-to-server
   verification needs an ASC API Key (Issuer ID + Key ID + .p8 private key)
   with App Manager role or higher. These should exist already if the SY094
   headless-ship recipe works; if not, conductor should mint them in ASC and
   stash at `kv_store.creds.apple_asc_api_key` (object: `{issuer_id, key_id,
   p8_b64}`). Code is written to read from settings; missing creds degrade
   the iOS receipt path to "verification skipped, entitlement granted on
   client claim only" with a loud log line.

6. **Google Play Developer API needs a service-account JSON.** The Chambers
   Android ship recipe uses `D:/PRIVATE/ecodia-creds/play/play-uploader-key.json`.
   For glovebox-android Play purchase verification, the same service account
   needs `androidpublisher` scope and Play Console access to
   `au.ecodia.roam`. Conductor should add `au.ecodia.roam` to the
   service-account's app access if not already present.

## Phase log

### Phase A - entitlements substrate (shipped 2026-05-31)
- [x] Discovery + STATUS-BILLING.md authored
- [x] Plans directory created at `backend/docs/superpowers/plans/`
- [x] Plan 01 written (entitlements substrate)
- [x] Migration `009_v2_entitlements.sql` authored and applied to prod
      Supabase (`vzauarlfmkjfkcphojbd`, table verified via Management API)
- [x] Pydantic models for entitlement tier + redeem request
      (`app/core/billing_models.py`)
- [x] Settings additions for ASC API key + Play service account paths
      (`app/core/settings.py` v2 billing block)
- [x] `app/services/entitlements.py` core service (4-branch resolution)
- [x] `GET /entitlement` route registered (`app/api/entitlement.py`)
- [x] CI green: 12/12 tests pass; OpenAPI 3.1.0; 50 routes (+1 from v1)
- [x] Locked baseline regenerated at `docs/openapi-3.1.0-locked.json`
- [ ] Conductor-driven `gcloud run deploy roam-backend --region
      australia-southeast1` (flagged)

### Phase B - Stripe webhook for new SKUs (shipped 2026-05-31)
- [x] Plan written
- [x] `POST /stripe/checkout/v2` - tier-picker checkout
- [x] Webhook routes v1 sessions to `user_entitlements`, v2 sessions to
      `entitlements` (idempotent on Stripe payment_intent)
- [x] 12 new tests; CI green (24/24)
- [x] OpenAPI regenerated (51 routes)
- [ ] Conductor-driven `gcloud run deploy` (flagged)
### Phase C - Apple App Store Server API path (shipped 2026-05-31)
- [x] Plan written
- [x] Added `app-store-server-library>=1.6.0` to requirements
- [x] `app/services/apple_receipt.py` - `verify_signed_transaction()`
      with library-path and dev-mode-decode-only fallback
- [x] `roam_unlimited` grandfather detection on `is_grandfather_eligible`
- [x] 14 new tests; CI green (36/36)
- [ ] Conductor: download Apple root certs to container at
      `app/data/apple-roots/` + set `APPLE_ROOT_CERT_BUNDLE_PATH` env var
      + set `APPLE_APP_APPLE_ID` from ASC listing
### Phase D - Google Play Developer API path (shipped 2026-05-31)
- [x] Added `google-api-python-client` + `google-auth` to requirements
- [x] `app/services/play_purchase.py` -
      `verify_purchase_token(purchase_token, product_id)`
- [x] Service account loading: path wins over inline base64
- [x] `roam_unlimited` grandfather flag
- [x] 12 new tests; CI green (48/48)
- [ ] Conductor: add `au.ecodia.roam` to the Play uploader service-account
      app access list + mount the SA JSON to Cloud Run (path or
      GOOGLE_PLAY_SERVICE_ACCOUNT_JSON_B64 env)
### Phase E - Unified POST /entitlement/redeem + grandfather (shipped 2026-05-31)
- [x] `POST /entitlement/redeem` accepts {platform, product_id, receipt}
      and routes to apple_receipt / play_purchase per platform
- [x] Web + legacy platforms rejected with 400
- [x] Grandfather: verified `roam_unlimited` SKU on iOS or Android always
      lands as Lifetime regardless of client-requested product_id
- [x] Idempotent on (source_platform, transaction_id) - duplicate
      redemptions return the same `granted=True` shape
- [x] Android `purchase_state != 0` (pending / canceled) returns 403
- [x] 13 new tests; CI green (61/61); locked OpenAPI 52 routes
### Phase F - Product ID configuration (scripts authored, conductor runs)
- [x] `scripts/provision_stripe_v2_products.py` - idempotent Stripe
      Product + Price creation. Conductor runs with
      `STRIPE_SECRET_KEY=sk_live_...` and pastes the printed
      `STRIPE_PRICE_MONTH/_SEASON/_LIFETIME` env vars into Cloud Run.
- [x] `scripts/provision_play_v2_products.py` - Play `inappproducts.insert`
      for all 3 SKUs. Attempted from this worker; got 403 because the
      Play uploader service account at
      `D:/PRIVATE/ecodia-creds/play/play-uploader-key.json` does not have
      access to `au.ecodia.roam`. **Conductor: grant the SA "Admin
      (all permissions)" or "Manage store presence + Manage in-app
      products" role on au.ecodia.roam in Play Console -> Users and
      permissions, then re-run.**
- [ ] **ASC: GUI-only.** Conductor opens
      https://appstoreconnect.apple.com -> Glovebox -> Monetization -> In-App
      Purchases and creates three IAPs under bundle `au.ecodia.roam`:
      - `glovebox_pass_month` (Non-Renewing Subscription, AUD 9.99)
      - `glovebox_pass_season` (Non-Renewing Subscription, AUD 19.99)
      - `glovebox_lifetime` (Non-Consumable, AUD 34.99)
      All need review screenshots, localised descriptions, and
      Apple-side review submission. Three reviews in parallel.
- [x] `scripts/fetch-apple-root-certs.sh` - curls
      AppleRootCA-G3.cer + AppleWWDRCAG6.cer into the target dir.
- [x] Dockerfile updated: installs curl + ca-certificates,
      runs the fetch script during build into
      `/app/app/data/apple-roots`. Cloud Run env should set
      `APPLE_ROOT_CERT_BUNDLE_PATH=/app/app/data/apple-roots`.

## Last Fly deploy version
N/A - target is Cloud Run, not Fly. Last Cloud Run revision: unknown to this
worker (conductor probes `gcloud run services describe roam-backend --region
australia-southeast1` to verify).

## Conductor handoff - the ten things to do after this worker exits

1. **Cloud Run deploy** of the new commits (A-E + F-Dockerfile):
   ```
   cd D:/.code/glovebox/backend
   gcloud run deploy roam-backend \
     --source . \
     --region australia-southeast1 \
     --project <gcp project id>
   ```
2. **Grant Play SA access** on `au.ecodia.roam` (see Phase F note above),
   then run:
   ```
   GOOGLE_PLAY_SERVICE_ACCOUNT_JSON_PATH=D:/PRIVATE/ecodia-creds/play/play-uploader-key.json \
     python scripts/provision_play_v2_products.py
   ```
3. **Stripe products**: run
   `STRIPE_SECRET_KEY=sk_live_... python scripts/provision_stripe_v2_products.py`
   on a machine with the glovebox Stripe key. Copy the three printed
   `STRIPE_PRICE_*` env vars to Cloud Run.
4. **ASC IAP records**: manual via App Store Connect (see Phase F note).
5. **Set Cloud Run env vars** post-deploy:
   - `STRIPE_PRICE_MONTH`, `STRIPE_PRICE_SEASON`, `STRIPE_PRICE_LIFETIME`
   - `APPLE_ROOT_CERT_BUNDLE_PATH=/app/app/data/apple-roots`
   - `APPLE_APP_APPLE_ID=<numeric ASC app id>`
   - `APPLE_ASC_API_KEY_ID`, `APPLE_ASC_API_ISSUER_ID`,
     `APPLE_ASC_API_KEY_P8_B64`
   - `GOOGLE_PLAY_SERVICE_ACCOUNT_JSON_B64=<base64 of SA JSON>` or mount
     a Secret volume + set `GOOGLE_PLAY_SERVICE_ACCOUNT_JSON_PATH`
6. **Mint `GLOVEBOX_BACKEND_BOT_TOKEN`** as a fine-grained PAT scoped to
   `EcodiaTate/glovebox-ios` + `glovebox-android` + `glovebox-web` with
   `contents: write` + `metadata: read`. Stash at
   `kv_store.creds.github_glovebox_backend_bot` AND as the GH Actions
   secret on `EcodiaTate/glovebox-backend`. Without this the
   `bump-clients.yml` workflow uploads `openapi.json` as an artifact but
   skips the per-repo dispatches.
7. **Verify the deploy** via
   `curl https://roam-backend-176723812810.australia-southeast1.run.app/entitlement`
   with a real Supabase JWT - should return `{"tier":"free",...}` for a
   fresh user.
8. **Smoke-test the redeem path** with a sandbox iOS receipt once ASC
   IAP records are approved + a sandbox Apple ID has purchased one.
9. **Smoke-test the Stripe v2 path** via
   `POST /stripe/checkout/v2 {"tier":"month"}` -> follow the URL ->
   complete with a test card -> verify the new `entitlements` row lands.
10. **Update the three native client repos** when the auto-PR opens
    (after step 6, the next backend push triggers `bump-clients.yml`
    which dispatches `openapi-bump` events to each native repo, and
    each repo's CI regenerates its typed client folder).
