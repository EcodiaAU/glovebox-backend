# app/services/wildlife.py
"""
Wildlife hazard overlay service for Roam.

Data source: iNaturalist Node API v1
  Base URL : https://api.inaturalist.org/v1
  License  : CC0 and CC-BY observations only (commercial use)
  Rate     : 60 req/min (enforced in INaturalistClient)

Algorithm
─────────
1. Decode the route polyline6.
2. Sample every WILDLIFE_SAMPLE_INTERVAL_KM km along the route.
3. For each sample, query iNaturalist /observations within WILDLIFE_RADIUS_KM.
4. Aggregate observations per sample point into a WildlifeZone:
     high    ≥ WILDLIFE_HIGH_RISK_COUNT observations
     medium  ≥ WILDLIFE_MEDIUM_RISK_COUNT
     low     ≥ 1
     none    0 observations
5. Attach one representative photo + species from the highest-count obs.
6. Cache result in wildlife_packs for WILDLIFE_CACHE_SECONDS (7 days default).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional, Tuple

from app.core.contracts import WildlifeOverlay, WildlifeZone
from app.core.polyline6 import decode_polyline6
from app.core.settings import settings
from app.core.storage import get_wildlife_pack, put_wildlife_pack
from app.core.time import utc_now_iso
from app.core.geo import haversine_km
from app.core.cache_utils import is_fresh, stable_key
from app.services.inaturalist import INaturalistClient, INatObservation

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# Geo helpers (shared with coverage.py pattern)
# ──────────────────────────────────────────────────────────────

def _cumulative_distances(coords: List[Tuple[float, float]]) -> List[float]:
    dists = [0.0]
    for i in range(1, len(coords)):
        d = haversine_km((coords[i - 1][0], coords[i - 1][1]), (coords[i][0], coords[i][1]))
        dists.append(dists[-1] + d)
    return dists


def _sample_points(
    coords: List[Tuple[float, float]],
    cum_dists: List[float],
    interval_km: float,
) -> List[Tuple[float, float, float]]:
    """Sample every interval_km; always include start and end. Returns [(lat, lng, km), ...]."""
    total_km = cum_dists[-1]
    if total_km == 0 or not coords:
        return []

    samples: List[Tuple[float, float, float]] = [(coords[0][0], coords[0][1], 0.0)]
    target_km = interval_km
    i = 0
    while target_km < total_km:
        while i < len(cum_dists) - 1 and cum_dists[i + 1] < target_km:
            i += 1
        if i >= len(coords) - 1:
            break
        seg_len = cum_dists[i + 1] - cum_dists[i]
        frac = (target_km - cum_dists[i]) / seg_len if seg_len > 0 else 0.0
        lat = coords[i][0] + frac * (coords[i + 1][0] - coords[i][0])
        lng = coords[i][1] + frac * (coords[i + 1][1] - coords[i][1])
        samples.append((lat, lng, target_km))
        target_km += interval_km

    last_lat, last_lng = coords[-1]
    if not samples or haversine_km((samples[-1][0], samples[-1][1]), (last_lat, last_lng)) > 0.5:
        samples.append((last_lat, last_lng, total_km))

    return samples


# ──────────────────────────────────────────────────────────────
# Cache helpers
# ──────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────
# Risk classification
# ──────────────────────────────────────────────────────────────

def _classify_risk(count: int) -> str:
    if count >= settings.wildlife_high_risk_count:
        return "high"
    if count >= settings.wildlife_medium_risk_count:
        return "medium"
    if count >= 1:
        return "low"
    return "none"


def _build_zone(
    lat: float,
    lng: float,
    km_along: float,
    interval_km: float,
    observations: List[INatObservation],
) -> WildlifeZone:
    count = len(observations)
    risk = _classify_risk(count)

    # Collect species names (deduplicated, most frequent first by order of appearance)
    seen: Dict[str, int] = {}
    for obs in observations:
        name = obs.species_guess or "Unknown"
        seen[name] = seen.get(name, 0) + 1
    dominant = sorted(seen, key=lambda k: -seen[k])[:5]

    # Representative observation: most recent (first in list, ordered desc by created_at)
    rep: Optional[INatObservation] = observations[0] if observations else None

    half = interval_km / 2.0
    message = (
        f"{count} wildlife observation{'s' if count != 1 else ''} "
        f"within {settings.wildlife_radius_km:.0f} km. "
        f"Species: {', '.join(dominant[:3]) or 'unknown'}."
        if count > 0
        else None
    )

    return WildlifeZone(
        lat=lat,
        lng=lng,
        km_from=max(0.0, km_along - half),
        km_to=km_along + half,
        risk_level=risk,
        dominant_species=dominant,
        occurrence_count=count,
        is_twilight_risk=False,  # iNaturalist has no time-of-day metadata
        message=message,
        species_guess=rep.species_guess if rep else None,
        photos=rep.photos[:3] if rep else [],  # up to 3 photos per zone
        attribution=rep.attribution if rep else None,
        observation_id=rep.id if rep else None,
    )


# ──────────────────────────────────────────────────────────────
# Service
# ──────────────────────────────────────────────────────────────

class Wildlife:
    """
    Wildlife collision risk overlay service.

    Powered by iNaturalist Node API v1 (CC0/CC-BY observations only).
    Results are cached in SQLite for wildlife_cache_seconds (default 7 days).
    """

    def __init__(self, *, conn) -> None:
        self.conn = conn

    async def along_route(
        self,
        *,
        polyline6: str,
        buffer_km: float = 10.0,
        departure_iso: Optional[str] = None,
        cache_seconds: Optional[int] = None,
        timeout_s: Optional[float] = None,
    ) -> Optional[WildlifeOverlay]:
        """
        Build a wildlife observation overlay along a route.

        Returns None when WILDLIFE_ENABLED=false.
        Returns a WildlifeOverlay (possibly with empty zones) otherwise.
        """
        if not settings.wildlife_enabled:
            return None

        algo_version = settings.wildlife_algo_version
        max_age = cache_seconds if cache_seconds is not None else settings.wildlife_cache_seconds
        radius_km = max(buffer_km, settings.wildlife_radius_km)
        interval_km = settings.wildlife_sample_interval_km
        t_s = timeout_s if timeout_s is not None else settings.wildlife_timeout_s

        # Cache key
        wildlife_key = stable_key("wildlife", {
            "polyline6": polyline6,
            "radius_km": round(radius_km, 1),
            "interval_km": round(interval_km, 1),
            "algo_version": algo_version,
        })

        # Check cache
        cached = get_wildlife_pack(self.conn, wildlife_key)
        if cached:
            created_at = cached.get("created_at", "")
            if is_fresh(created_at, max_age_s=max_age):
                return WildlifeOverlay(**cached)

        # Decode geometry
        coords = decode_polyline6(polyline6)
        if not coords:
            return WildlifeOverlay(
                wildlife_key=wildlife_key,
                polyline6=polyline6,
                algo_version=algo_version,
                created_at=utc_now_iso(),
                warnings=["Empty route geometry."],
            )

        cum_dists = _cumulative_distances(coords)
        samples = _sample_points(coords, cum_dists, interval_km)

        zones: List[WildlifeZone] = []
        warnings: List[str] = []

        try:
            async with INaturalistClient(
                timeout_s=t_s,
                rate_per_min=settings.wildlife_rate_per_min,
            ) as client:
                tasks = [
                    client.get_observations(
                        lat=lat,
                        lng=lng,
                        radius=radius_km,
                        per_page=settings.wildlife_per_page,
                        photo_size=settings.wildlife_photo_size,
                    )
                    for lat, lng, _ in samples
                ]
                results: List[List[INatObservation]] = await asyncio.gather(*tasks)

        except Exception as exc:
            logger.warning("[wildlife] iNaturalist fetch failed: %s", exc)
            warnings.append("Wildlife data temporarily unavailable.")
            results = [[] for _ in samples]

        for (lat, lng, km_along), observations in zip(samples, results):
            zone = _build_zone(lat, lng, km_along, interval_km, observations)
            if zone.risk_level != "none":
                zones.append(zone)

        created_at = utc_now_iso()
        overlay = WildlifeOverlay(
            wildlife_key=wildlife_key,
            polyline6=polyline6,
            algo_version=algo_version,
            created_at=created_at,
            zones=zones,
            warnings=warnings,
        )

        # Persist to cache
        try:
            put_wildlife_pack(
                self.conn,
                wildlife_key=wildlife_key,
                created_at=created_at,
                algo_version=algo_version,
                pack=overlay.model_dump(),
            )
        except Exception as exc:
            logger.warning("[wildlife] Cache write failed: %s", exc)

        return overlay
