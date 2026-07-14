-- 012_drop_emergency_contacts.sql
--
-- Emergency contacts now come from the phone's own address book.
--
-- The SOS screen used to carry a hand-typed contact form backed by this table,
-- an offline op queue and a last-write-wins cloud merge, all of it built to
-- duplicate an address book that both platforms already hold and already sync
-- (iCloud / Google). The native apps now pick contacts through the OS contact
-- picker and keep a device-local snapshot of who was picked, so the cloud lane
-- has nothing left to carry.
--
-- Contents at drop time: 3 rows across 3 users, all ours (a delete-test fixture
-- account plus Tate's own contact under two identities). No third-party data.
--
-- ORDERING MATTERS. `public.delete_glovebox_account` explicitly deletes from
-- this table, so it is corrected FIRST and the table dropped SECOND. Dropping
-- first would leave a live account-deletion RPC pointing at a missing relation,
-- and account deletion is an Apple Guideline 5.1.1(v) compliance path.
--
-- Note that `delete_glovebox_account` had NO migration file before this one: it
-- was created directly against the live database. Its full definition is
-- captured below so the repo is the source of truth from here on.

begin;

-- Same function as live, minus the emergency_contacts delete.
--
-- It enumerates user-keyed tables explicitly rather than leaning on cascade
-- because roam_plan_invites.created_by is NO ACTION and would otherwise block
-- the auth.users delete. Missing a table here is a compliance failure, so the
-- enumeration stays exhaustive.
create or replace function public.delete_glovebox_account(p_user_id uuid default null::uuid)
returns jsonb
language plpgsql
security definer
set search_path to ''
as $function$
DECLARE
  v_uid uuid;
BEGIN
  -- Authenticated end-users (web/native, JWT-bound) can ONLY delete themselves.
  -- A trusted service_role backend (auth.uid() null) may pass an explicit target.
  IF auth.uid() IS NOT NULL THEN
    v_uid := auth.uid();
  ELSIF p_user_id IS NOT NULL THEN
    v_uid := p_user_id;
  ELSE
    RAISE EXCEPTION 'not_authenticated' USING errcode = '28000';
  END IF;

  -- App-owned data keyed to this user, deleted explicitly in FK-safe order so a
  -- real erasure never relies on implicit cascade (roam_plan_invites.created_by
  -- is NO ACTION and would otherwise block the auth.users delete). Enumerate every
  -- Glovebox user-keyed table; missing one is a compliance FAIL.
  DELETE FROM public.public_trip_clones WHERE cloner_id  = v_uid; -- this user's clones of others' trips
  DELETE FROM public.roam_plan_members  WHERE user_id    = v_uid; -- membership in others' plans
  DELETE FROM public.roam_plan_invites  WHERE created_by = v_uid; -- invites created in others' plans
  DELETE FROM public.public_trips       WHERE owner_id   = v_uid; -- cascades public_trip_clones by trip_id
  DELETE FROM public.roam_plans         WHERE owner_id   = v_uid; -- cascades members + invites by plan_id
  DELETE FROM public.saved_places       WHERE user_id    = v_uid;
  DELETE FROM public.stop_memories      WHERE owner_id   = v_uid;
  DELETE FROM public.user_trip_counts   WHERE user_id    = v_uid;
  DELETE FROM public.user_entitlements  WHERE user_id    = v_uid;
  DELETE FROM public.entitlements       WHERE user_id    = v_uid;
  -- emergency_contacts is deliberately absent: the table no longer exists.
  -- Emergency contacts live in the phone's address book and never leave the device.

  -- Finally the auth identity itself. Cascades auth.identities (incl. the
  -- custom:friend link for THIS app), auth.sessions, mfa, one_time_tokens, etc.
  -- Does NOT touch the Friend IdP project (eionabtkzyjnipwfdsfy) - that account
  -- is deleted separately via the Friend app's own in-app deletion.
  DELETE FROM auth.users WHERE id = v_uid;

  RETURN jsonb_build_object('deleted', true, 'user_id', v_uid);
END;
$function$;

-- Now safe: nothing references the table (no views, no inbound FKs, and the one
-- function that named it has just been corrected above).
drop table if exists public.emergency_contacts;

commit;
