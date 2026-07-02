-- 012_entitlements.sql
--
-- Self-contained, server-authoritative entitlement substrate for the native
-- iOS app, replacing the dead Cloud Run /entitlement + /entitlement/redeem
-- path. No Apple App Store Server API key is involved: the client sends the
-- StoreKit 2 signed transaction (JWS) to the `verify-entitlement` Edge
-- Function, which verifies the JWS against Apple's public cert chain (the x5c
-- in the JWS header, rooted at Apple's root CA), then upserts a row here with
-- the service role. The client reads its own row via PostgREST under RLS.
--
-- This is ADDITIVE and idempotent on top of the live `public.entitlements`
-- table (created by 009_v2_entitlements.sql in the glovebox-web repo, extended
-- by 010_v2_entitlements_source_and_updated_at.sql). It does NOT recreate the
-- table. The live shape is:
--   id, user_id, tier (month|season|lifetime), expires_at, source_platform
--   (ios|android|web), product_id, transaction_id, granted_at, raw_receipt
--   jsonb, source, updated_at, created_at + unique (source_platform,
--   transaction_id) + RLS "Users can read own v2 entitlements".
--
-- What this migration adds:
--   1. original_transaction_id  - the StoreKit 2 stable key. For renewals /
--      restores / re-redeems Apple keeps `originalTransactionId` constant while
--      `transactionId` changes per event, so this is the correct idempotency
--      key for the JWS path. Unique so a re-redeem updates the same row.
--   2. environment - "Production" | "Sandbox" from the JWS, so a sandbox test
--      transaction is observable and never confused with a real one.
--   3. A `select` RLS policy guaranteed present (idempotent) so the native
--      client's PostgREST read returns only the caller's rows. Writes stay
--      service-role only (the Edge Function), no client insert/update policy.
--
-- Apply to Glovebox Supabase project ref vzauarlfmkjfkcphojbd.

-- 1. StoreKit 2 stable key + environment ------------------------------------

alter table public.entitlements
  add column if not exists original_transaction_id text;

alter table public.entitlements
  add column if not exists environment text;

-- Idempotency for the JWS path: one row per Apple original transaction. A
-- re-redeem (restore, renewal event, re-launch grandfather scan) of the same
-- purchase updates this row rather than inserting a duplicate. Partial so the
-- legacy rows written by the old Cloud Run path (which only set
-- source_platform + transaction_id, leaving this null) are not forced unique
-- on null.
create unique index if not exists entitlements_original_txn_idx
  on public.entitlements (original_transaction_id)
  where original_transaction_id is not null;

-- Hot read path is already covered by entitlements_user_expiry_idx
-- (user_id, expires_at desc nulls first) from 009. No new read index needed.

-- 2. RLS: user reads own rows, writes are service-role only -----------------
-- Idempotent restatement so this migration is self-sufficient even if applied
-- to a project where 009's policy was dropped. The Edge Function uses the
-- service role, which bypasses RLS, so it needs no insert/update policy.

alter table public.entitlements enable row level security;

drop policy if exists "Users can read own v2 entitlements" on public.entitlements;
create policy "Users can read own v2 entitlements"
  on public.entitlements for select
  using (auth.uid() = user_id);
