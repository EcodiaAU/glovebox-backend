-- Migration: v2 entitlements - human-facing `source` + `updated_at`
--
-- Additive follow-up to the v2 entitlements table created by
-- `009_v2_entitlements.sql` (which lives in the SEPARATE glovebox-web repo at
-- `frontend/supabase/migrations/`). This file is kept inside the backend repo
-- because the backend is the only repo this work was scoped to touch; apply it
-- to the same Glovebox Supabase project as 009 (project ref
-- `vzauarlfmkjfkcphojbd`). The convention note for the human is in the worker
-- report - if you prefer all migrations in one place, copy this into
-- `frontend/supabase/migrations/` so `supabase db push` picks it up.
--
-- Why this migration exists: the shipped glovebox-ios client
-- (Sources/Glovebox/Services/Billing) reads a human-facing `source` string off
-- the entitlement object (`Entitlement.source`: free / purchase / restore /
-- grandfather / legacy). 009 only stored `source_platform` (ios/android/web),
-- which answers "which storefront" but not "how was this granted". This
-- migration adds the `source` column the redeem path writes
-- (app/api/entitlement.py -> app/services/entitlements.py::upsert_entitlement)
-- and an `updated_at` column bumped on every upsert so a re-redeem of the same
-- (source_platform, transaction_id) is observable.
--
-- Safe to run on the live table: both columns are nullable / defaulted, so the
-- existing rows and the existing unique index (source_platform, transaction_id)
-- are untouched. No data backfill is required - the read path
-- (get_current_entitlement) derives a sensible `source` for rows written before
-- this column existed ("purchase" fallback; "legacy" for the v1 grandfather
-- path), so old rows keep returning a non-null source to the client.
--
-- Reads: app/services/entitlements.py::get_current_entitlement (selects
--        `source`), ::_effective_source.
-- Writes: app/services/entitlements.py::upsert_entitlement (sets `source` +
--        `updated_at`).

-- ── entitlements.source ──────────────────────────────────────────────────────
-- Provenance lives in the purchase row so the client can surface it
-- ("Lifetime - grandfathered") and support can distinguish a grandfather grant
-- from a fresh purchase without joining the raw receipt.

alter table public.entitlements
  add column if not exists source text;

-- Constrain to the known provenance values. Added NOT VALID so the statement
-- never scans/locks existing rows; existing rows have source = null (allowed)
-- and the read path fills the gap. New writes are constrained.
do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'entitlements_source_check'
  ) then
    alter table public.entitlements
      add constraint entitlements_source_check
      check (source in ('purchase', 'restore', 'grandfather', 'legacy'))
      not valid;
  end if;
end $$;

-- ── entitlements.updated_at ──────────────────────────────────────────────────
-- Bumped app-side on every upsert (mirrors the `updated_at = now()` pattern the
-- 001 increment_trip_count function already uses), so a duplicate redeem that
-- matches the (source_platform, transaction_id) unique index updates this even
-- though it inserts no new row.

alter table public.entitlements
  add column if not exists updated_at timestamptz not null default now();
