"""Unit tests for the Wikipedia/Wikidata POI enrichment (no network)."""
import sqlite3
import types

from app.services import wiki_enrich as W


def _item(extra):
    o = types.SimpleNamespace()
    o.extra = extra
    return o


def _conn():
    c = sqlite3.connect(":memory:")
    return c


def test_fetch_key_resolution():
    assert W._fetch_key(_item({"wikidata": "Q5"})) == "Q5"
    assert W._fetch_key(_item({"wikipedia": "en:Pimba"})) == "wp:en:Pimba"
    assert W._fetch_key(_item({"name": "x"})) is None
    # non-Q wikidata is ignored
    assert W._fetch_key(_item({"wikidata": "garbage"})) is None


def test_needs_enrich():
    assert W._needs_enrich(_item({})) is True
    assert W._needs_enrich(_item({"description": "d"})) is True   # still wants thumb
    assert W._needs_enrich(_item({"description": "d", "thumbnail_url": "u"})) is False


def test_enrich_fills_and_caches(monkeypatch):
    calls = {"n": 0}

    def fake_fetch(key):
        calls["n"] += 1
        return {
            "title": "Pimba",
            "extract": "Pimba is a small settlement on the Stuart Highway.",
            "image_url": "https://upload.wikimedia.org/x.jpg",
            "source_url": "https://en.wikipedia.org/wiki/Pimba",
        }

    monkeypatch.setattr(W, "_fetch_one", fake_fetch)
    conn = _conn()
    items = [_item({"name": "Pimba", "wikipedia": "en:Pimba"})]
    n = W.enrich_items(items, conn)
    assert n == 1
    e = items[0].extra
    assert "small settlement" in e["description"]
    assert e["thumbnail_url"] == "https://upload.wikimedia.org/x.jpg"
    assert e["wiki_attribution"] == "Wikipedia, CC BY-SA 4.0"
    assert e["wiki_source"] == "https://en.wikipedia.org/wiki/Pimba"
    assert calls["n"] == 1

    # Second call on a fresh item with the same key must hit the cache (no fetch).
    items2 = [_item({"name": "Pimba", "wikipedia": "en:Pimba"})]
    n2 = W.enrich_items(items2, conn)
    assert n2 == 1
    assert calls["n"] == 1  # still 1 - served from cache
    assert "small settlement" in items2[0].extra["description"]


def test_existing_detail_not_clobbered(monkeypatch):
    monkeypatch.setattr(W, "_fetch_one", lambda k: {"extract": "WIKI", "image_url": "WIKIIMG"})
    conn = _conn()
    # has description already, missing thumb -> only thumb filled
    items = [_item({"name": "X", "wikidata": "Q1", "description": "OSM blurb"})]
    W.enrich_items(items, conn)
    assert items[0].extra["description"] == "OSM blurb"   # preserved
    assert items[0].extra["thumbnail_url"] == "WIKIIMG"   # filled


def test_no_wiki_ref_skipped(monkeypatch):
    monkeypatch.setattr(W, "_fetch_one", lambda k: {"extract": "X"})
    conn = _conn()
    items = [_item({"name": "Rest Area"})]
    assert W.enrich_items(items, conn) == 0
    assert "description" not in items[0].extra


def test_empty_result_cached_no_crash(monkeypatch):
    monkeypatch.setattr(W, "_fetch_one", lambda k: {})
    conn = _conn()
    items = [_item({"name": "X", "wikidata": "Q999"})]
    assert W.enrich_items(items, conn) == 0
    # empty result is cached so a re-run does not re-fetch
    row = conn.execute("SELECT key FROM wiki_cache WHERE key='Q999'").fetchone()
    assert row is not None
