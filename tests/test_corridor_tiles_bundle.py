"""Per-trip corridor street tiles in the offline bundle (Supabase-backed).

The bundle includes `corridor-tiles.pmtiles` only when the feature is enabled
AND a pack is present in Supabase Storage. With the flag off (the production
default) the bundle is byte-identical to before the feature existed, so the
working web/app cannot regress. iOS reads the member to render real streets
offline and otherwise falls back to the nationwide z14 pack.
"""

from __future__ import annotations

import io
import sqlite3
import zipfile

from app.core.contracts import BBox4
from app.core.storage import ensure_schema, put_nav_pack, put_corridor_pack
from app.services import corridor_tiles
from app.services.bundle import Bundle


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    return conn


def _seed_core(conn: sqlite3.Connection, route_key: str, corridor_key: str) -> None:
    put_nav_pack(conn, route_key=route_key, created_at="t", algo_version="v1", pack={"nav": 1})
    put_corridor_pack(
        conn, corridor_key=corridor_key, route_key=route_key, profile="drive",
        buffer_m=1000, max_edges=10, algo_version="v1", created_at="t", pack={"graph": 1},
    )


def _build_zip(conn: sqlite3.Connection, *, ctk: str | None, ready: bool):
    b = Bundle(conn=conn)
    b.build_manifest(
        plan_id="plan-x", route_key="route-x", styles=["bright"], navpack_ready=True,
        corridor_key="corr-x", corridor_ready=True,
        corridor_tiles_key=ctk, corridor_tiles_ready=ready,
        corridor_tiles_bbox="136.5,-32.7,138.0,-31.0",
        places_key=None, places_ready=False, traffic_key=None, traffic_ready=False,
        hazards_key=None, hazards_ready=False,
    )
    return b.build_zip(plan_id="plan-x")


def test_miss_records_generation_request(monkeypatch) -> None:
    # On a storage miss with a key present, build_zip records a generation
    # request carrying the corridor bbox so the worker can build the pack.
    monkeypatch.setattr(corridor_tiles, "fetch_from_storage", lambda key: None)
    noted = {}
    monkeypatch.setattr(corridor_tiles, "note_missing_pack",
                        lambda key, bbox: noted.update(key=key, bbox=bbox))
    conn = _conn()
    _seed_core(conn, "route-x", "corr-x")
    _build_zip(conn, ctk="ct_missing", ready=True)
    assert noted == {"key": "ct_missing", "bbox": "136.5,-32.7,138.0,-31.0"}


def test_no_request_when_no_key(monkeypatch) -> None:
    monkeypatch.setattr(corridor_tiles, "fetch_from_storage", lambda key: None)
    called = []
    monkeypatch.setattr(corridor_tiles, "note_missing_pack", lambda key, bbox: called.append(1))
    conn = _conn()
    _seed_core(conn, "route-x", "corr-x")
    _build_zip(conn, ctk=None, ready=False)
    assert called == []


def test_note_missing_pack_noop_when_flag_off(monkeypatch) -> None:
    monkeypatch.setattr(corridor_tiles.settings, "corridor_tiles_enabled", False)
    # Must not raise and must not touch Supabase when the flag is off.
    corridor_tiles.note_missing_pack("ct_x", "1,2,3,4")


def test_key_is_stable_and_bbox_quantised() -> None:
    # Two routes whose corridor bboxes fall in the same ~0.05 deg bin share one
    # generated pack; the key is deterministic and namespaced.
    a = corridor_tiles.corridor_tiles_key(BBox4(minLng=136.51, minLat=-32.69, maxLng=138.02, maxLat=-31.01))
    b = corridor_tiles.corridor_tiles_key(BBox4(minLng=136.52, minLat=-32.70, maxLng=138.01, maxLat=-31.00))
    assert a == b
    assert a.startswith("ct_")
    far = corridor_tiles.corridor_tiles_key(BBox4(minLng=150.0, minLat=-28.0, maxLng=151.0, maxLat=-27.0))
    assert far != a


def test_fetch_is_none_when_flag_off(monkeypatch) -> None:
    # Flag off short-circuits before any Supabase call, so it is safe with no
    # client configured and can never raise into the bundle.
    monkeypatch.setattr(corridor_tiles.settings, "corridor_tiles_enabled", False)
    assert corridor_tiles.fetch_from_storage("ct_anything") is None


def test_bundle_omits_corridor_tiles_when_flag_off(monkeypatch) -> None:
    # The production-default path: identical to pre-feature behaviour.
    monkeypatch.setattr(corridor_tiles.settings, "corridor_tiles_enabled", False)
    conn = _conn()
    _seed_core(conn, "route-x", "corr-x")
    ctk = corridor_tiles.corridor_tiles_key(BBox4(minLng=136.5, minLat=-32.7, maxLng=138.0, maxLat=-31.0))
    res = _build_zip(conn, ctk=ctk, ready=False)
    names = zipfile.ZipFile(io.BytesIO(res.zip_bytes)).namelist()
    assert "corridor-tiles.pmtiles" not in names
    assert "navpack.json" in names and "corridor.json" in names


def test_bundle_includes_corridor_tiles_when_present(monkeypatch) -> None:
    pmtiles = b"PMTiles\x03" + bytes(2_000_000)
    monkeypatch.setattr(corridor_tiles, "fetch_from_storage", lambda key: pmtiles if key else None)
    conn = _conn()
    _seed_core(conn, "route-x", "corr-x")
    res = _build_zip(conn, ctk="ct_present", ready=True)
    z = zipfile.ZipFile(io.BytesIO(res.zip_bytes))
    assert "corridor-tiles.pmtiles" in z.namelist()
    assert z.read("corridor-tiles.pmtiles") == pmtiles


def test_bundle_omits_corridor_tiles_on_storage_miss(monkeypatch) -> None:
    monkeypatch.setattr(corridor_tiles, "fetch_from_storage", lambda key: None)
    conn = _conn()
    _seed_core(conn, "route-x", "corr-x")
    res = _build_zip(conn, ctk="ct_missing", ready=True)
    names = zipfile.ZipFile(io.BytesIO(res.zip_bytes)).namelist()
    assert "corridor-tiles.pmtiles" not in names
