"""GET /entitlement - resolves the four tier branches plus the grandfather path.

Covers the resolution rules in `app/services/entitlements.py`:
  - No rows anywhere -> tier='free'
  - Active pass in `entitlements` -> tier=month/season + expires_at
  - Expired pass row + no other rows -> tier='free'
  - Lifetime row in `entitlements` -> tier='lifetime'
  - Legacy `user_entitlements` row only -> tier='lifetime', source='legacy'
  - Lifetime in `entitlements` beats an unexpired month pass

Wire shape: the response is wrapped under `entitlement` (the shipped iOS
client's `EntitlementWrapper` decodes `{"entitlement": {...}}`), and the
entitlement object carries a human-facing `source` string the client reads.

Plus a 401 path on the unauthed client.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _ent(resp_json: dict) -> dict:
    """Unwrap the `{"entitlement": {...}}` envelope the clients decode."""
    assert "entitlement" in resp_json, resp_json
    return resp_json["entitlement"]


# ── Free-tier branches ────────────────────────────────────────────────────


def test_no_rows_returns_free(client, fake_supabase, authed_user_id):
    resp = client.get("/entitlement")
    assert resp.status_code == 200, resp.text
    assert _ent(resp.json()) == {
        "tier": "free",
        "expires_at": None,
        "source": "free",
        "source_platform": None,
        "product_id": None,
    }


def test_expired_pass_returns_free(client, fake_supabase, authed_user_id):
    fake_supabase.tables["entitlements"] = [
        {
            "user_id": authed_user_id,
            "tier": "month",
            "expires_at": _iso(datetime.now(timezone.utc) - timedelta(days=1)),
            "source_platform": "ios",
            "product_id": "glovebox_pass_month",
        }
    ]
    resp = client.get("/entitlement")
    assert resp.status_code == 200
    assert _ent(resp.json())["tier"] == "free"


# ── Active pass branches ──────────────────────────────────────────────────


def test_active_month_pass(client, fake_supabase, authed_user_id):
    expires = datetime.now(timezone.utc) + timedelta(days=15)
    fake_supabase.tables["entitlements"] = [
        {
            "user_id": authed_user_id,
            "tier": "month",
            "expires_at": _iso(expires),
            "source": "purchase",
            "source_platform": "ios",
            "product_id": "glovebox_pass_month",
        }
    ]
    resp = client.get("/entitlement")
    assert resp.status_code == 200
    body = _ent(resp.json())
    assert body["tier"] == "month"
    assert body["source"] == "purchase"
    assert body["source_platform"] == "ios"
    assert body["product_id"] == "glovebox_pass_month"
    assert body["expires_at"].startswith(str(expires.year))


def test_active_season_pass(client, fake_supabase, authed_user_id):
    expires = datetime.now(timezone.utc) + timedelta(days=60)
    fake_supabase.tables["entitlements"] = [
        {
            "user_id": authed_user_id,
            "tier": "season",
            "expires_at": _iso(expires),
            "source_platform": "android",
            "product_id": "glovebox_pass_season",
        }
    ]
    body = _ent(client.get("/entitlement").json())
    assert body["tier"] == "season"
    assert body["source_platform"] == "android"


# ── Lifetime branches ─────────────────────────────────────────────────────


def test_lifetime_row(client, fake_supabase, authed_user_id):
    fake_supabase.tables["entitlements"] = [
        {
            "user_id": authed_user_id,
            "tier": "lifetime",
            "expires_at": None,
            "source": "purchase",
            "source_platform": "web",
            "product_id": "glovebox_lifetime",
        }
    ]
    body = _ent(client.get("/entitlement").json())
    assert body == {
        "tier": "lifetime",
        "expires_at": None,
        "source": "purchase",
        "source_platform": "web",
        "product_id": "glovebox_lifetime",
    }


def test_lifetime_beats_active_month_pass(client, fake_supabase, authed_user_id):
    """A lifetime row must dominate even when an active pass also exists."""
    expires = datetime.now(timezone.utc) + timedelta(days=15)
    fake_supabase.tables["entitlements"] = [
        {
            "user_id": authed_user_id,
            "tier": "month",
            "expires_at": _iso(expires),
            "source_platform": "ios",
            "product_id": "glovebox_pass_month",
        },
        {
            "user_id": authed_user_id,
            "tier": "lifetime",
            "expires_at": None,
            "source_platform": "ios",
            "product_id": "glovebox_lifetime",
        },
    ]
    body = _ent(client.get("/entitlement").json())
    assert body["tier"] == "lifetime"


# ── Grandfather branch ───────────────────────────────────────────────────


def test_legacy_user_entitlements_grandfathers_to_lifetime(
    client, fake_supabase, authed_user_id
):
    fake_supabase.tables["user_entitlements"] = [
        {
            "id": "11111111-1111-1111-1111-111111111111",
            "user_id": authed_user_id,
            "source": "revenuecat",
        }
    ]
    body = _ent(client.get("/entitlement").json())
    assert body == {
        "tier": "lifetime",
        "expires_at": None,
        "source": "legacy",
        "source_platform": "legacy",
        "product_id": None,
    }


def test_v2_row_beats_legacy_user_entitlements(client, fake_supabase, authed_user_id):
    """If both v2 and legacy rows exist for the user, v2 wins (its product_id
    + source_platform is more informative than the synthesised legacy shape)."""
    expires = datetime.now(timezone.utc) + timedelta(days=10)
    fake_supabase.tables["entitlements"] = [
        {
            "user_id": authed_user_id,
            "tier": "season",
            "expires_at": _iso(expires),
            "source_platform": "android",
            "product_id": "glovebox_pass_season",
        }
    ]
    fake_supabase.tables["user_entitlements"] = [
        {
            "id": "11111111-1111-1111-1111-111111111111",
            "user_id": authed_user_id,
            "source": "stripe",
        }
    ]
    body = _ent(client.get("/entitlement").json())
    assert body["tier"] == "season"
    assert body["source_platform"] == "android"


# ── Auth ─────────────────────────────────────────────────────────────────


def test_unauthed_returns_401(unauthed_client):
    resp = unauthed_client.get("/entitlement")
    assert resp.status_code == 401
