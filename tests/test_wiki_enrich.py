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


def test_p18_fallback_when_summary_has_no_thumb(monkeypatch):
    # Summary returns an extract + qid but NO image; entity P18 supplies the image.
    monkeypatch.setattr(W, "_wikipedia_summary",
                        lambda c, lang, title: {"title": title, "extract": "blurb",
                                                "image_url": None, "source_url": "u", "qid": "Q42"})
    monkeypatch.setattr(W, "_wikidata_entity",
                        lambda c, qid: {"image_url": "https://commons/p18.jpg"})
    got = W._fetch_one("wp:en:SomeTown")
    assert got["extract"] == "blurb"
    assert got["image_url"] == "https://commons/p18.jpg"  # filled from P18


def test_summary_thumb_wins_over_p18(monkeypatch):
    monkeypatch.setattr(W, "_wikipedia_summary",
                        lambda c, lang, title: {"title": title, "extract": "b",
                                                "image_url": "https://summary/thumb.jpg",
                                                "source_url": "u", "qid": "Q42"})
    # _wikidata_entity must NOT even be consulted when the summary has a thumb;
    # guard that by making it raise if called.
    def _boom(c, qid):
        raise AssertionError("P18 should not be fetched when summary has a thumb")
    monkeypatch.setattr(W, "_wikidata_entity", _boom)
    got = W._fetch_one("wp:en:Town")
    assert got["image_url"] == "https://summary/thumb.jpg"


def test_qid_only_entity_image_no_wikipedia(monkeypatch):
    # A Q-id with a P18 image but no usable Wikipedia page still yields an image.
    monkeypatch.setattr(W, "_wikidata_entity",
                        lambda c, qid: {"image_url": "https://commons/only.jpg"})
    monkeypatch.setattr(W, "_wikipedia_summary", lambda c, lang, title: None)
    got = W._fetch_one("Q123")
    assert got == {"image_url": "https://commons/only.jpg"}


def test_commons_thumb_encodes_filename():
    url = W._commons_thumb("Coober Pedy - pano.jpg")
    assert "Special:FilePath/Coober_Pedy_-_pano.jpg" in url
    assert "width=" in url


def test_empty_result_cached_no_crash(monkeypatch):
    monkeypatch.setattr(W, "_fetch_one", lambda k: {})
    conn = _conn()
    items = [_item({"name": "X", "wikidata": "Q999"})]
    assert W.enrich_items(items, conn) == 0
    # empty result is cached so a re-run does not re-fetch
    row = conn.execute("SELECT key FROM wiki_cache WHERE key='Q999'").fetchone()
    assert row is not None
