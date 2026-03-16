# app/services/heritage.py
"""
Heritage and protected areas overlay service for Roam.

Data sources (DCCEEW GIS, CC-BY 3.0 AU, no auth required):
  - World Heritage Areas
  - National Heritage List
  - Commonwealth Heritage List
  - CAPAD Protected Areas (National Parks, Nature Reserves, etc.)

All endpoints are ArcGIS REST MapServer/0/query with esriGeometryEnvelope.

Algorithm:
  1. Decode polyline6 to extract the route bounding box + buffer.
  2. Query all 4 DCCEEW endpoints concurrently.
  3. Parse results (each endpoint has different field names).
  4. Deduplicate by name (sites can appear in multiple lists).
  5. For CAPAD, filter to interesting types (National Park, Nature Reserve,
     State Forest, Marine Park).
  6. Cache in SQLite for 7 days (heritage sites rarely change).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
from typing import Any, Dict, List, Tuple

import httpx

from app.core.contracts import HeritageSite, HeritageOverlay
from app.core.settings import settings
from app.core.storage import get_heritage_pack, put_heritage_pack
from app.core.time import utc_now_iso
from app.core.geo import bbox_from_coords, decode_polyline6
from app.core.cache_utils import is_fresh, stable_key

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────

_CACHE_TTL_S = 604_800  # 7 days

_WORLD_HERITAGE_URL = f"{settings.dcceew_gis_base_url}/World_Heritage_Areas/MapServer/0/query"
_NATIONAL_HERITAGE_URL = f"{settings.dcceew_gis_base_url}/National_Heritage_List/MapServer/0/query"
_COMMONWEALTH_HERITAGE_URL = f"{settings.dcceew_gis_base_url}/Commonwealth_Heritage_List/MapServer/0/query"
_CAPAD_URL = f"{settings.dcceew_gis_base_url}/CAPAD/MapServer/0/query"

# CAPAD types worth surfacing (skip e.g. "Indigenous Protected Area", misc reserves)
_INTERESTING_CAPAD_TYPES = {
    "national park",
    "nature reserve",
    "state forest",
    "marine park",
}

_HTTP_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


# ──────────────────────────────────────────────────────────────
# Geometry helpers
# ──────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────
# Cache helpers
# ──────────────────────────────────────────────────────────────


def _site_id(name: str, site_type: str) -> str:
    """Deterministic short id from name + type."""
    raw = f"{site_type}::{name}"
    h = hashlib.sha256(raw.encode()).digest()
    return base64.urlsafe_b64encode(h).decode("ascii").rstrip("=")[:20]


# ──────────────────────────────────────────────────────────────
# ArcGIS fetchers
# ──────────────────────────────────────────────────────────────


async def _query_arcgis(
    client: httpx.AsyncClient,
    url: str,
    bbox: Tuple[float, float, float, float],
    warnings: list[str],
    label: str,
) -> List[Dict[str, Any]]:
    """
    Query an ArcGIS MapServer/0/query endpoint with an envelope geometry.
    bbox = (minLat, minLng, maxLat, maxLng).
    Returns the list of feature attribute dicts.
    """
    min_lat, min_lng, max_lat, max_lng = bbox
    geometry = f"{min_lng},{min_lat},{max_lng},{max_lat}"
    params = {
        "where": "1=1",
        "geometry": geometry,
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "outFields": "*",
        "outSR": "4326",
        "returnGeometry": "false",
        "f": "json",
    }
    try:
        resp = await client.get(url, params=params, timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        features = data.get("features") or []
        return [f.get("attributes") or {} for f in features]
    except Exception as e:
        warnings.append(f"heritage:{label}: {e}")
        return []


async def _fetch_world_heritage(
    client: httpx.AsyncClient,
    bbox: Tuple[float, float, float, float],
    warnings: list[str],
) -> List[HeritageSite]:
    rows = await _query_arcgis(client, _WORLD_HERITAGE_URL, bbox, warnings, "world_heritage")
    sites: List[HeritageSite] = []
    for attrs in rows:
        name = str(attrs.get("NAME") or attrs.get("name") or "").strip()
        if not name:
            continue
        sites.append(HeritageSite(
            id=_site_id(name, "world_heritage"),
            name=name,
            site_type="world_heritage",
            classification=str(attrs.get("STATUS") or "").strip() or None,
        ))
    return sites


async def _fetch_national_heritage(
    client: httpx.AsyncClient,
    bbox: Tuple[float, float, float, float],
    warnings: list[str],
) -> List[HeritageSite]:
    rows = await _query_arcgis(client, _NATIONAL_HERITAGE_URL, bbox, warnings, "national_heritage")
    sites: List[HeritageSite] = []
    for attrs in rows:
        name = str(attrs.get("NAME") or attrs.get("name") or "").strip()
        if not name:
            continue
        classification = str(attrs.get("CLASSIFICATION") or "").strip().lower() or None
        sites.append(HeritageSite(
            id=_site_id(name, "national_heritage"),
            name=name,
            site_type="national_heritage",
            classification=classification,
        ))
    return sites


async def _fetch_commonwealth_heritage(
    client: httpx.AsyncClient,
    bbox: Tuple[float, float, float, float],
    warnings: list[str],
) -> List[HeritageSite]:
    rows = await _query_arcgis(client, _COMMONWEALTH_HERITAGE_URL, bbox, warnings, "commonwealth_heritage")
    sites: List[HeritageSite] = []
    for attrs in rows:
        name = str(attrs.get("NAME") or attrs.get("name") or "").strip()
        if not name:
            continue
        classification = str(attrs.get("CLASS") or "").strip().lower() or None
        sites.append(HeritageSite(
            id=_site_id(name, "commonwealth_heritage"),
            name=name,
            site_type="commonwealth_heritage",
            classification=classification,
        ))
    return sites


async def _fetch_capad(
    client: httpx.AsyncClient,
    bbox: Tuple[float, float, float, float],
    warnings: list[str],
) -> List[HeritageSite]:
    rows = await _query_arcgis(client, _CAPAD_URL, bbox, warnings, "capad")
    sites: List[HeritageSite] = []
    for attrs in rows:
        name = str(attrs.get("NAME") or attrs.get("name") or "").strip()
        if not name:
            continue
        pa_type = str(attrs.get("TYPE") or attrs.get("type") or "").strip()
        # Filter to interesting types
        if pa_type.lower() not in _INTERESTING_CAPAD_TYPES:
            continue
        iucn = str(attrs.get("IUCN") or attrs.get("iucn") or "").strip() or None
        state = str(attrs.get("STATE") or attrs.get("state") or "").strip() or None
        authority = str(attrs.get("AUTHORITY") or attrs.get("authority") or "").strip() or None
        sites.append(HeritageSite(
            id=_site_id(name, "protected_area"),
            name=name,
            site_type="protected_area",
            classification=iucn,
            state=state,
            authority=authority,
        ))
    return sites


# ──────────────────────────────────────────────────────────────
# Deduplication
# ──────────────────────────────────────────────────────────────

# Priority: world > national > commonwealth > protected_area
_TYPE_PRIORITY: Dict[str, int] = {
    "world_heritage": 4,
    "national_heritage": 3,
    "commonwealth_heritage": 2,
    "protected_area": 1,
}


def _dedup_sites(sites: List[HeritageSite]) -> List[HeritageSite]:
    """Deduplicate by normalised name, keeping the highest-priority type."""
    best: Dict[str, HeritageSite] = {}
    for site in sites:
        key = site.name.lower().strip()
        existing = best.get(key)
        if existing is None or _TYPE_PRIORITY.get(site.site_type, 0) > _TYPE_PRIORITY.get(existing.site_type, 0):
            best[key] = site
    return list(best.values())


# ──────────────────────────────────────────────────────────────
# Service
# ──────────────────────────────────────────────────────────────


class Heritage:
    """
    Heritage and protected areas overlay service.

    Powered by DCCEEW GIS services (CC-BY 3.0 AU, no auth required).
    Results are cached in SQLite for 7 days (heritage sites rarely change).
    """

    def __init__(self, *, conn) -> None:
        self.conn = conn

    async def along_route(
        self,
        *,
        polyline6: str,
        buffer_km: float = 25.0,
        cache_seconds: int | None = None,
    ) -> HeritageOverlay:
        """
        Find heritage sites and protected areas near a route.

        Args:
            polyline6:      Polyline6-encoded route geometry.
            buffer_km:      Search buffer around the route bbox (default 25 km).
            cache_seconds:  Override default cache TTL (default 7 days).

        Returns:
            HeritageOverlay with deduplicated sites and any warnings.
        """
        algo_version = settings.heritage_algo_version
        max_age = cache_seconds if cache_seconds is not None else _CACHE_TTL_S

        heritage_key = stable_key("heritage", {
            "polyline6": polyline6,
            "buffer_km": round(buffer_km, 1),
            "algo_version": algo_version,
        })

        # ── Cache hit ─────────────────────────────────────────────
        cached = get_heritage_pack(self.conn, heritage_key)
        if cached:
            created_at = cached.get("created_at", "")
            if is_fresh(created_at, max_age_s=max_age):
                return HeritageOverlay(**cached)

        # ── Decode route ──────────────────────────────────────────
        coords = decode_polyline6(polyline6)
        if not coords:
            overlay = HeritageOverlay(
                heritage_key=heritage_key,
                polyline6=polyline6,
                algo_version=algo_version,
                created_at=utc_now_iso(),
                warnings=["Empty route geometry."],
            )
            put_heritage_pack(
                self.conn,
                heritage_key=heritage_key,
                created_at=overlay.created_at,
                algo_version=algo_version,
                pack=overlay.model_dump(),
            )
            return overlay

        bbox = bbox_from_coords(coords, buffer_km)
        warnings: List[str] = []

        # ── Query all 4 endpoints concurrently ────────────────────
        transport = httpx.AsyncHTTPTransport(retries=1)
        async with httpx.AsyncClient(
            follow_redirects=True,
            transport=transport,
        ) as client:
            results = await asyncio.gather(
                _fetch_world_heritage(client, bbox, warnings),
                _fetch_national_heritage(client, bbox, warnings),
                _fetch_commonwealth_heritage(client, bbox, warnings),
                _fetch_capad(client, bbox, warnings),
                return_exceptions=True,
            )

        all_sites: List[HeritageSite] = []
        for i, res in enumerate(results):
            if isinstance(res, Exception):
                label = ["world_heritage", "national_heritage", "commonwealth_heritage", "capad"][i]
                warnings.append(f"heritage:{label} fetch error: {res}")
            elif isinstance(res, list):
                all_sites.extend(res)

        # ── Deduplicate ───────────────────────────────────────────
        sites = _dedup_sites(all_sites)

        # Sort by type priority (world first) then name
        sites.sort(key=lambda s: (-_TYPE_PRIORITY.get(s.site_type, 0), s.name))

        # ── Attribution ───────────────────────────────────────────
        if sites:
            warnings.append(
                "Heritage data: Australian Government Department of Climate Change, "
                "Energy, the Environment and Water (CC-BY 3.0 AU)."
            )

        created_at = utc_now_iso()
        overlay = HeritageOverlay(
            heritage_key=heritage_key,
            polyline6=polyline6,
            algo_version=algo_version,
            created_at=created_at,
            sites=sites,
            warnings=warnings,
        )

        # ── Persist to cache ──────────────────────────────────────
        try:
            put_heritage_pack(
                self.conn,
                heritage_key=heritage_key,
                created_at=created_at,
                algo_version=algo_version,
                pack=overlay.model_dump(),
            )
        except Exception as exc:
            logger.warning("[heritage] Cache write failed: %s", exc)

        return overlay
