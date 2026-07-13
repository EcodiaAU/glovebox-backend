"""Road names in the corridor pack.

The edges DB has always carried a `name` column and both readers already SELECT it,
but `Corridor.ensure()` dropped it when it constructed each `CorridorEdge`. Offline
turn-by-turn therefore said a nameless "Turn left" in exactly the no-signal case the
offline router exists for. These tests pin the fix.

Names are INTERNED: the pack carries a `names` table and each edge carries `n`, an
index into it. A corridor for one highway can hold thousands of edges that all say
"Warrego Highway" (measured: 8,596 of them on a real Brisbane->Charleville corridor),
so a literal string per edge bloats a pack that users pull over patchy rural
connections.
"""

from __future__ import annotations

import sqlite3
from typing import List, Optional

from app.core.edges_db import EdgeRow, EdgesDB
from app.core.polyline6 import encode_polyline6
from app.core.storage import ensure_schema
from app.services.corridor import Corridor


class FakeEdgesDB(EdgesDB):
    """Returns a fixed edge set, standing in for the Fly Postgres road graph."""

    def __init__(self, rows: List[EdgeRow]) -> None:
        self._rows = rows

    def query_bbox(self, *a, **k) -> List[EdgeRow]:
        return self._rows

    def query_by_node_ids(self, node_ids: List[int]) -> List[EdgeRow]:
        return self._rows

    def count(self) -> int:
        return len(self._rows)

    def close(self) -> None:
        return None


def _edge(eid: int, a: int, b: int, name: Optional[str]) -> EdgeRow:
    return EdgeRow(
        id=eid, from_id=a, to_id=b,
        from_lat=-27.40 + a * 0.01, from_lng=153.00 + a * 0.01,
        to_lat=-27.40 + b * 0.01, to_lng=153.00 + b * 0.01,
        dist_m=1000.0, cost_s=60.0,
        toll=0, ferry=0, unsealed=0,
        highway="primary", name=name, osm_way_id=eid * 10,
    )


def _corridor(rows: List[EdgeRow]) -> Corridor:
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    c = Corridor(cache_conn=conn, edges_db=FakeEdgesDB(rows), algo_version="v-test")
    # Stub the OSRM hop: it only supplies the spine node ids, and FakeEdgesDB
    # ignores them and returns the fixture edge set regardless.
    c._osrm_route_nodes = lambda *a, **k: [1, 2, 3, 4]  # type: ignore[method-assign]
    c._osrm_nearest_node = lambda *a, **k: 1  # type: ignore[method-assign]
    return c


def _ensure(c: Corridor):
    poly = encode_polyline6([(-27.40, 153.00), (-27.38, 153.02), (-27.36, 153.04)])
    return c.ensure(
        route_key="rk-names", route_polyline6=poly, profile="driving",
        buffer_m=1000, max_edges=100_000,
    ).pack


def test_road_names_reach_the_pack_and_are_interned():
    rows = [
        _edge(1, 1, 2, "Waterworks Road"),
        _edge(2, 2, 3, "Warrego Highway"),
        _edge(3, 3, 4, "Warrego Highway"),  # repeat: must reuse the SAME index
    ]
    pack = _ensure(_corridor(rows))

    # Each distinct name is stored exactly once.
    assert pack.names == ["Waterworks Road", "Warrego Highway"]

    by_ab = {(e.a, e.b): e for e in pack.edges}
    assert pack.names[by_ab[(1, 2)].n] == "Waterworks Road"
    assert pack.names[by_ab[(2, 3)].n] == "Warrego Highway"
    # The repeated highway points at the same intern slot rather than a second copy.
    assert by_ab[(2, 3)].n == by_ab[(3, 4)].n


def test_unnamed_edges_stay_null_and_do_not_enter_the_table():
    rows = [
        _edge(1, 1, 2, "Milton Road"),
        _edge(2, 2, 3, None),   # unnamed track
        _edge(3, 3, 4, ""),     # empty string is not a name
        _edge(4, 4, 5, "   "),  # whitespace is not a name
    ]
    pack = _ensure(_corridor(rows))

    assert pack.names == ["Milton Road"]
    by_ab = {(e.a, e.b): e for e in pack.edges}
    assert by_ab[(1, 2)].n == 0
    # An unnamed edge carries no index, so the client degrades to the old nameless
    # phrasing for that edge rather than speaking a blank or the word "null".
    assert by_ab[(2, 3)].n is None
    assert by_ab[(3, 4)].n is None
    assert by_ab[(4, 5)].n is None


def test_pack_with_no_named_edges_emits_an_empty_table():
    """A corridor of entirely unnamed tracks must not grow a names key full of junk;
    it should look exactly like a pre-names pack to any client reading it."""
    pack = _ensure(_corridor([_edge(1, 1, 2, None), _edge(2, 2, 3, None)]))
    assert pack.names == []
    assert all(e.n is None for e in pack.edges)
