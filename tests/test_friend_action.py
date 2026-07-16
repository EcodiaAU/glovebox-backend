"""POST /friend/action - the friend-action write contract (save_place).

Covers the verify ladder in `app/api/friend_action.py` in contract order:
  - FRIEND_SUITE_SECRET absent            -> 503 unconfigured
  - missing / wrong x-friend-signature    -> 401 bad signature
  - stale ts (>120s skew)                 -> 401 stale
  - unknown friend_sub                    -> 200 {connected:false, done:false}
  - unknown action                        -> 400 unknown action
  - missing idempotency_key               -> 400 bad idempotency_key
  - save_place without lat/lng            -> 200 done:false (validation sentence)
  - save_place without a name             -> 200 done:false
  - happy path                            -> 200 done:true + row in saved_places
  - idempotency replay                    -> original result, NO second row

The Supabase admin client is replaced with an in-memory double that supports
the rpc + select/eq/limit + insert verbs the route uses; the resolve RPC maps
one known sub to one user id.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

TEST_SECRET = "test-friend-suite-secret"
KNOWN_SUB = "friend-sub-0001"
LOCAL_USER = "00000000-0000-0000-0000-00000000abcd"


# ── In-memory Supabase double (rpc + select/insert on saved_places) ────────


class _Result:
    def __init__(self, data: Any) -> None:
        self.data = data
        self.error = None


class _Query:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self._filters: list[tuple[str, Any]] = []
        self._limit: int | None = None
        self._insert_pending: list[dict[str, Any]] | None = None

    def select(self, *_a: Any, **_kw: Any) -> "_Query":
        return self

    def eq(self, col: str, val: Any) -> "_Query":
        self._filters.append((col, val))
        return self

    def limit(self, n: int) -> "_Query":
        self._limit = n
        return self

    def insert(self, row: dict[str, Any] | list[dict[str, Any]]) -> "_Query":
        self._insert_pending = row if isinstance(row, list) else [row]
        return self

    def execute(self) -> _Result:
        if self._insert_pending is not None:
            for new_row in self._insert_pending:
                # Enforce the live unique (user_id, place_id) index.
                for existing in self._rows:
                    if (
                        existing.get("user_id") == new_row.get("user_id")
                        and existing.get("place_id") == new_row.get("place_id")
                    ):
                        raise RuntimeError(
                            "duplicate key value violates unique constraint "
                            '"saved_places_user_place_idx"'
                        )
                self._rows.append(dict(new_row))
            return _Result(list(self._insert_pending))
        out = [
            r for r in self._rows if all(r.get(c) == v for c, v in self._filters)
        ]
        if self._limit is not None:
            out = out[: self._limit]
        return _Result(out)


class _Rpc:
    def __init__(self, mapping: dict[str, str], fn: str, args: dict[str, Any]) -> None:
        self._mapping = mapping
        self._fn = fn
        self._args = args

    def execute(self) -> _Result:
        assert self._fn == "resolve_friend_user", self._fn
        return _Result(self._mapping.get(self._args.get("p_friend_sub", "")))


class FakeFriendSupabase:
    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {"saved_places": []}
        self.sub_map: dict[str, str] = {KNOWN_SUB: LOCAL_USER}

    def table(self, name: str) -> _Query:
        return _Query(self.tables.setdefault(name, []))

    def rpc(self, fn: str, args: dict[str, Any]) -> _Rpc:
        return _Rpc(self.sub_map, fn, args)


# ── Fixtures / helpers ────────────────────────────────────────────────────


@pytest.fixture
def friend_supa(monkeypatch: pytest.MonkeyPatch) -> FakeFriendSupabase:
    import app.api.friend_action as mod

    fake = FakeFriendSupabase()
    monkeypatch.setattr(mod, "get_supabase_admin", lambda: fake)
    monkeypatch.setenv("FRIEND_SUITE_SECRET", TEST_SECRET)
    return fake


@pytest.fixture
def plain_client() -> TestClient:
    from app.main import app

    return TestClient(app)


def _sign(body: str, secret: str = TEST_SECRET) -> str:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def _post(client: TestClient, body: dict[str, Any], *, secret: str | None = TEST_SECRET):
    raw = json.dumps(body)
    headers = {"content-type": "application/json"}
    if secret is not None:
        headers["x-friend-signature"] = _sign(raw, secret)
    return client.post("/friend/action", content=raw, headers=headers)


def _body(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "friend_sub": KNOWN_SUB,
        "ts": int(time.time() * 1000),
        "action": "save_place",
        "params": {"name": "Sails Restaurant", "lat": -26.68, "lng": 153.12},
        "idempotency_key": "11111111-2222-3333-4444-555555555555",
    }
    base.update(over)
    return base


# ── Verify ladder ─────────────────────────────────────────────────────────


def test_no_secret_returns_503(plain_client, friend_supa, monkeypatch):
    monkeypatch.delenv("FRIEND_SUITE_SECRET", raising=False)
    resp = _post(plain_client, _body())
    assert resp.status_code == 503
    assert resp.json() == {"ok": False, "error": "unconfigured"}


def test_unsigned_returns_401(plain_client, friend_supa):
    resp = _post(plain_client, _body(), secret=None)
    assert resp.status_code == 401
    assert resp.json()["error"] == "bad signature"


def test_wrong_signature_returns_401(plain_client, friend_supa):
    resp = _post(plain_client, _body(), secret="not-the-secret")
    assert resp.status_code == 401
    assert resp.json()["error"] == "bad signature"


def test_stale_ts_returns_401(plain_client, friend_supa):
    resp = _post(plain_client, _body(ts=int(time.time() * 1000) - 600_000))
    assert resp.status_code == 401
    assert resp.json()["error"] == "stale"


def test_unknown_sub_returns_connected_false(plain_client, friend_supa):
    resp = _post(plain_client, _body(friend_sub="nobody-here"))
    assert resp.status_code == 200
    j = resp.json()
    assert j["ok"] is True and j["connected"] is False and j["done"] is False
    assert friend_supa.tables["saved_places"] == []


def test_unknown_action_returns_400(plain_client, friend_supa):
    resp = _post(plain_client, _body(action="drop_table"))
    assert resp.status_code == 400
    assert resp.json()["error"] == "unknown action"


def test_missing_idempotency_key_returns_400(plain_client, friend_supa):
    body = _body()
    del body["idempotency_key"]
    resp = _post(plain_client, body)
    assert resp.status_code == 400
    assert resp.json()["error"] == "bad idempotency_key"


# ── save_place validation ─────────────────────────────────────────────────


def test_save_place_without_coords_is_done_false(plain_client, friend_supa):
    resp = _post(plain_client, _body(params={"name": "Sails Restaurant"}))
    assert resp.status_code == 200
    j = resp.json()
    assert j["connected"] is True and j["done"] is False
    assert "lat" in j["result"]
    assert friend_supa.tables["saved_places"] == []


def test_save_place_without_name_is_done_false(plain_client, friend_supa):
    resp = _post(plain_client, _body(params={"lat": -26.68, "lng": 153.12}))
    assert resp.status_code == 200
    j = resp.json()
    assert j["connected"] is True and j["done"] is False
    assert friend_supa.tables["saved_places"] == []


def test_save_place_out_of_range_coords_is_done_false(plain_client, friend_supa):
    resp = _post(
        plain_client, _body(params={"name": "X", "lat": -226.68, "lng": 153.12})
    )
    assert resp.status_code == 200
    assert resp.json()["done"] is False
    assert friend_supa.tables["saved_places"] == []


# ── Happy path + idempotency ──────────────────────────────────────────────


def test_save_place_creates_row(plain_client, friend_supa):
    resp = _post(plain_client, _body())
    assert resp.status_code == 200, resp.text
    j = resp.json()
    assert j == {
        "ok": True,
        "app": "glovebox",
        "connected": True,
        "done": True,
        "result": "Saved Sails Restaurant to their places.",
    }
    rows = friend_supa.tables["saved_places"]
    assert len(rows) == 1
    row = rows[0]
    assert row["user_id"] == LOCAL_USER
    assert row["place_id"] == "friend:11111111-2222-3333-4444-555555555555"
    assert row["name"] == "Sails Restaurant"
    assert row["lat"] == -26.68 and row["lng"] == 153.12
    assert row["category"] == "place"


def test_save_place_note_is_stored(plain_client, friend_supa):
    resp = _post(
        plain_client,
        _body(
            params={
                "name": "Sails Restaurant",
                "lat": -26.68,
                "lng": 153.12,
                "note": "great sunset spot",
            }
        ),
    )
    assert resp.json()["done"] is True
    assert friend_supa.tables["saved_places"][0]["note"] == "great sunset spot"


def test_idempotency_replay_inserts_nothing(plain_client, friend_supa):
    first = _post(plain_client, _body())
    assert first.json()["done"] is True
    assert len(friend_supa.tables["saved_places"]) == 1

    # Same idempotency_key, even with a mutated name: original result, no insert.
    replay = _post(
        plain_client,
        _body(params={"name": "Different Name", "lat": 0.0, "lng": 0.0}),
    )
    assert replay.status_code == 200
    j = replay.json()
    assert j["done"] is True
    assert j["result"] == "Saved Sails Restaurant to their places."
    assert len(friend_supa.tables["saved_places"]) == 1


def test_new_idempotency_key_inserts_second_row(plain_client, friend_supa):
    _post(plain_client, _body())
    second = _post(
        plain_client,
        _body(
            idempotency_key="99999999-8888-7777-6666-555555555555",
            params={"name": "Mooloolaba Beach", "lat": -26.68, "lng": 153.12},
        ),
    )
    assert second.json()["done"] is True
    assert len(friend_supa.tables["saved_places"]) == 2
