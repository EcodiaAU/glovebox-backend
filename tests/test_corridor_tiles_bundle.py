"""Corridor street-zoom tiles flow through the offline bundle.

The z16 corridor pmtiles (built by tiles/build-corridor.sh) is stored like any
other pack and must ship in the bundle zip as `corridor-tiles.pmtiles` when the
manifest marks it ready, and be omitted otherwise. iOS reads that member to
render real streets offline.
"""

from __future__ import annotations

import io
import sqlite3
import zipfile

from app.core.storage import (
    ensure_schema,
    put_nav_pack,
    put_corridor_pack,
    put_corridor_tiles_pack,
)
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


def test_corridor_tiles_shipped_when_ready() -> None:
    conn = _conn()
    rk, ck, ctk = "route-a", "corr-a", "ctiles-a"
    _seed_core(conn, rk, ck)
    pmtiles = b"PMTiles\x03" + bytes(2_000_000)  # valid-ish header + bulk binary
    put_corridor_tiles_pack(
        conn, corridor_tiles_key=ctk, route_key=rk, bbox="136.5,-32.7,138.0,-31.0",
        maxzoom=16, created_at="t", pmtiles_bytes=pmtiles,
    )
    b = Bundle(conn=conn)
    b.build_manifest(
        plan_id="plan-a", route_key=rk, styles=["bright"], navpack_ready=True,
        corridor_key=ck, corridor_ready=True,
        corridor_tiles_key=ctk, corridor_tiles_ready=True,
        places_key=None, places_ready=False, traffic_key=None, traffic_ready=False,
        hazards_key=None, hazards_ready=False,
    )
    res = b.build_zip(plan_id="plan-a")
    z = zipfile.ZipFile(io.BytesIO(res.zip_bytes))
    assert "corridor-tiles.pmtiles" in z.namelist()
    assert z.read("corridor-tiles.pmtiles") == pmtiles


def test_corridor_tiles_absent_when_not_ready() -> None:
    conn = _conn()
    rk, ck = "route-b", "corr-b"
    _seed_core(conn, rk, ck)
    b = Bundle(conn=conn)
    b.build_manifest(
        plan_id="plan-b", route_key=rk, styles=["bright"], navpack_ready=True,
        corridor_key=ck, corridor_ready=True,
        places_key=None, places_ready=False, traffic_key=None, traffic_ready=False,
        hazards_key=None, hazards_ready=False,
    )
    res = b.build_zip(plan_id="plan-b")
    z = zipfile.ZipFile(io.BytesIO(res.zip_bytes))
    assert "corridor-tiles.pmtiles" not in z.namelist()
