"""
Wikipedia / Wikidata POI enrichment.

Turns a bare OSM POI that carries only a ``wikidata`` Q-id or a ``wikipedia``
"lang:Title" tag into a rich card: a one-paragraph description (Wikipedia
"extract") plus a Commons thumbnail. This is the single biggest legal,
zero-cost, cacheable enrichment for a remote-AU roadtrip app - towns, parks,
landmarks and lookouts almost always have a wikidata/wikipedia tag but sparse
inline OSM detail, so a dot like "Pimba" becomes "Pimba: a small settlement on
the Stuart Highway, gateway to Woomera and Lake Hart..." with a photo.

Source + licence: content comes from the Wikipedia REST summary endpoint and
the Wikidata EntityData endpoint. Both are CC BY-SA - cacheable and
redistributable WITH attribution, which we attach as ``wiki_attribution`` +
``wiki_source`` on each enriched item so the client can credit it. No Google
Maps, no API key.

Caching: results (including negative results) are persisted in the SQLite cache
DB so a repeat route is instant and we never hammer Wikimedia. Descriptions are
stable, so the TTL is long. Network fetches are concurrent, capped, short-
timeout and fully best-effort - any failure simply leaves the item unenriched.

This runs in the PLACES path (the full bundle build), never in the navigate-now
nav tier, so it does not slow "download -> navigate".
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

# Wikimedia asks every client to send a descriptive User-Agent with contact.
_UA = "GloveboxRoam/1.0 (https://glovebox.ecodia.au; code@ecodia.au) httpx"
_THUMB_W = 480
_CACHE_TTL_DAYS = 90
_FETCH_TIMEOUT = 4.0
_MAX_FETCH = 40          # cold fetches per build; cached hits are unbounded
_MAX_WORKERS = 8


def ensure_schema(conn) -> None:
    """Create the wiki_cache table if absent. Safe to call repeatedly."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_cache (
            key         TEXT PRIMARY KEY,   -- "Q12345" or "wp:en:Pimba"
            title       TEXT,
            extract     TEXT,
            image_url   TEXT,
            source_url  TEXT,
            fetched_at  TEXT NOT NULL
        )
        """
    )
    conn.commit()


def _fetch_key(item: Any) -> Optional[str]:
    """The cache key for an item, or None if it carries no wiki reference."""
    extra = getattr(item, "extra", None) or {}
    qid = extra.get("wikidata")
    if isinstance(qid, str) and qid.startswith("Q"):
        return qid
    wp = extra.get("wikipedia")
    if isinstance(wp, str) and ":" in wp:
        lang, _, title = wp.partition(":")
        if lang and title:
            return f"wp:{lang.strip()}:{title.strip()}"
    return None


def _needs_enrich(item: Any) -> bool:
    """True if the item is missing a description or a thumbnail we could fill."""
    extra = getattr(item, "extra", None) or {}
    has_desc = bool(extra.get("description"))
    has_thumb = bool(extra.get("thumbnail_url"))
    return not (has_desc and has_thumb)


def _wikipedia_summary(client: httpx.Client, lang: str, title: str) -> Optional[dict]:
    """Wikipedia REST summary: extract + thumbnail in one call."""
    enc = quote(title.replace(" ", "_"), safe="")
    url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{enc}"
    r = client.get(url, timeout=_FETCH_TIMEOUT, follow_redirects=True)
    if r.status_code != 200:
        return None
    d = r.json()
    if d.get("type") == "disambiguation":
        return None
    extract = (d.get("extract") or "").strip()
    thumb = (d.get("thumbnail") or {}).get("source")
    page = (d.get("content_urls") or {}).get("desktop", {}).get("page")
    if not extract and not thumb:
        return None
    return {
        "title": d.get("title"),
        "extract": extract[:600] if extract else None,
        "image_url": thumb,
        "source_url": page,
    }


def _resolve_qid_to_enwiki(client: httpx.Client, qid: str) -> Optional[tuple[str, str]]:
    """Q-id -> (lang, title) of its best Wikipedia sitelink (prefer en, then any)."""
    url = f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"
    r = client.get(url, timeout=_FETCH_TIMEOUT, follow_redirects=True)
    if r.status_code != 200:
        return None
    ent = (r.json().get("entities") or {}).get(qid) or {}
    sitelinks = ent.get("sitelinks") or {}
    for key in ("enwiki", "simplewiki"):
        if key in sitelinks and sitelinks[key].get("title"):
            return ("en", sitelinks[key]["title"])
    # any *wiki sitelink (skip commons/specieswiki/meta)
    for sk, sv in sitelinks.items():
        if sk.endswith("wiki") and sv.get("title") and sk not in ("commonswiki", "specieswiki", "metawiki"):
            return (sk[:-4], sv["title"])
    return None


def _fetch_one(key: str) -> dict:
    """Fetch enrichment for a single cache key. Returns a (possibly empty) dict."""
    try:
        with httpx.Client(headers={"User-Agent": _UA}) as client:
            if key.startswith("wp:"):
                _, lang, title = key.split(":", 2)
                got = _wikipedia_summary(client, lang, title)
            else:  # Q-id
                resolved = _resolve_qid_to_enwiki(client, key)
                got = _wikipedia_summary(client, *resolved) if resolved else None
            return got or {}
    except Exception as exc:  # best-effort; never raise into the bundle path
        logger.debug("wiki_enrich fetch failed for %s: %s", key, exc)
        return {}


def _load_cached(conn, keys: list[str]) -> dict[str, dict]:
    if not keys:
        return {}
    placeholders = ",".join("?" for _ in keys)
    cutoff = f"-{_CACHE_TTL_DAYS} days"
    rows = conn.execute(
        f"""
        SELECT key, title, extract, image_url, source_url
        FROM wiki_cache
        WHERE key IN ({placeholders})
          AND fetched_at > datetime('now', ?)
        """,
        (*keys, cutoff),
    ).fetchall()
    out: dict[str, dict] = {}
    for row in rows:
        out[row[0]] = {
            "title": row[1], "extract": row[2],
            "image_url": row[3], "source_url": row[4],
        }
    return out


def _store_cached(conn, key: str, data: dict) -> None:
    conn.execute(
        """
        INSERT INTO wiki_cache (key, title, extract, image_url, source_url, fetched_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(key) DO UPDATE SET
            title=excluded.title, extract=excluded.extract,
            image_url=excluded.image_url, source_url=excluded.source_url,
            fetched_at=excluded.fetched_at
        """,
        (key, data.get("title"), data.get("extract"),
         data.get("image_url"), data.get("source_url")),
    )


def enrich_items(items: list[Any], conn) -> int:
    """
    Fill description + thumbnail_url on POIs that carry a wikidata/wikipedia tag
    but lack inline detail. Cached + best-effort. Returns the count enriched.
    """
    if not items or conn is None:
        return 0
    try:
        ensure_schema(conn)
    except Exception as exc:
        logger.warning("wiki_enrich schema ensure failed (skipping): %s", exc)
        return 0

    # Map cache-key -> the items that want it (dedup the network work).
    key_to_items: dict[str, list[Any]] = {}
    for it in items:
        if not _needs_enrich(it):
            continue
        k = _fetch_key(it)
        if k:
            key_to_items.setdefault(k, []).append(it)
    if not key_to_items:
        return 0

    cached = _load_cached(conn, list(key_to_items.keys()))
    misses = [k for k in key_to_items if k not in cached][:_MAX_FETCH]

    # Concurrent cold fetches.
    fetched: dict[str, dict] = {}
    if misses:
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
            futs = {pool.submit(_fetch_one, k): k for k in misses}
            for fut in as_completed(futs):
                k = futs[fut]
                try:
                    fetched[k] = fut.result() or {}
                except Exception:
                    fetched[k] = {}
        # Persist every result (empty included, so a dead key is not re-hit).
        for k, data in fetched.items():
            try:
                _store_cached(conn, k, data)
            except Exception as exc:
                logger.debug("wiki_enrich cache write failed for %s: %s", k, exc)
        try:
            conn.commit()
        except Exception:
            pass

    resolved = {**cached, **fetched}
    enriched = 0
    for k, data in resolved.items():
        if not data or not (data.get("extract") or data.get("image_url")):
            continue
        for it in key_to_items.get(k, []):
            extra = getattr(it, "extra", None)
            if extra is None:
                continue
            changed = False
            if data.get("extract") and not extra.get("description"):
                extra["description"] = data["extract"]
                changed = True
            if data.get("image_url") and not extra.get("thumbnail_url"):
                extra["thumbnail_url"] = data["image_url"]
                changed = True
            if changed:
                extra["wiki_attribution"] = "Wikipedia, CC BY-SA 4.0"
                if data.get("source_url"):
                    extra["wiki_source"] = data["source_url"]
                enriched += 1
    if enriched:
        logger.info("wiki_enrich: enriched %d POIs (%d cold fetches)", enriched, len(misses))
    return enriched
