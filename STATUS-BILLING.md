# GB-BACKEND-02 - v2 Billing Status

Worker chat owning the v2 billing-model implementation on glovebox-backend
end-to-end. Conductor reads this file to verify progress; updated after every
feature batch.

## Phase
**A - shipped 2026-05-31.** Moving to B (Stripe webhook for v2 SKUs).

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

### Phase B - Stripe webhook for new SKUs (pending)
### Phase C - Apple App Store Server API path (pending)
### Phase D - Google Play Developer API path (pending)
### Phase E - Unified POST /entitlement/redeem + grandfather (pending)
### Phase F - Product ID configuration in ASC/Play/Stripe (pending)

## Last Fly deploy version
N/A - target is Cloud Run, not Fly. Last Cloud Run revision: unknown to this
worker (conductor probes `gcloud run services describe roam-backend --region
australia-southeast1` to verify).

## Next action
Phase B: extend `app/api/stripe.py` to recognise the three new v2 product
IDs (`glovebox_pass_month`, `glovebox_pass_season`, `glovebox_lifetime`)
and write to the new `entitlements` table via
`app/services/entitlements.upsert_entitlement`. Keep the v1 RevenueCat +
legacy Stripe path writing to `user_entitlements` (no v1 disruption).
Targeted commit: "feat(v2-billing): Stripe webhook handles v2 tiered SKUs".
