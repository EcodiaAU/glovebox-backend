# app/core/geo.py
"""
Shared geospatial helpers used by overlay services.

Centralises polyline6 decoding, haversine distance, route sampling,
bounding-box calculation, and min-distance-to-route so each service
doesn't carry its own copy.
"""
from __future__ import annotations

import math
from typing import List, Tuple

# Re-export from the canonical polyline6 module so callers only need one import.
from app.core.polyline6 import decode_polyline6, encode_polyline6  # noqa: F401

# ──────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────

EARTH_RADIUS_KM = 6_371.0


# ──────────────────────────────────────────────────────────────
# Distance
# ──────────────────────────────────────────────────────────────

def haversine_km(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """Great-circle distance in km between two (lat, lng) points."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    x = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2.0 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(x)))


def haversine_m(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """Great-circle distance in metres between two (lat, lng) points."""
    return haversine_km(a, b) * 1000.0


def min_dist_to_route(
    lat: float,
    lng: float,
    samples: List[Tuple[float, float]],
) -> float:
    """Minimum haversine distance (km) from (lat, lng) to any sample point."""
    best = float("inf")
    pt = (lat, lng)
    for s in samples:
        d = haversine_km(pt, s)
        if d < best:
            best = d
        if d < 0.05:
            break
    return best


# ──────────────────────────────────────────────────────────────
# Route sampling
# ──────────────────────────────────────────────────────────────

def sample_route(
    coords: List[Tuple[float, float]],
    interval_km: float = 5.0,
) -> List[Tuple[float, float]]:
    """Down-sample a coordinate list to roughly one point every *interval_km*."""
    if not coords:
        return []
    samples = [coords[0]]
    accum = 0.0
    for i in range(1, len(coords)):
        accum += haversine_km(coords[i - 1], coords[i])
        if accum >= interval_km:
            samples.append(coords[i])
            accum = 0.0
    if len(coords) > 1 and samples[-1] != coords[-1]:
        samples.append(coords[-1])
    return samples


def sample_route_with_km(
    coords: List[Tuple[float, float]],
    interval_km: float = 5.0,
) -> List[Tuple[float, float, float]]:
    """
    Down-sample coords returning (lat, lng, km_along) tuples.

    Used by services that need cumulative distance along the route
    (e.g. coverage, air_quality, rest_areas).
    """
    if not coords:
        return []
    samples: List[Tuple[float, float, float]] = [(coords[0][0], coords[0][1], 0.0)]
    total_km = 0.0
    accum = 0.0
    for i in range(1, len(coords)):
        d = haversine_km(coords[i - 1], coords[i])
        total_km += d
        accum += d
        if accum >= interval_km:
            samples.append((coords[i][0], coords[i][1], round(total_km, 2)))
            accum = 0.0
    if len(coords) > 1 and (samples[-1][0], samples[-1][1]) != coords[-1]:
        samples.append((coords[-1][0], coords[-1][1], round(total_km, 2)))
    return samples


# ──────────────────────────────────────────────────────────────
# Bounding box
# ──────────────────────────────────────────────────────────────

def bbox_from_coords(
    coords: List[Tuple[float, float]],
    buffer_km: float,
) -> Tuple[float, float, float, float]:
    """
    Compute (min_lat, min_lng, max_lat, max_lng) bounding box from coords
    with a buffer in kilometres.
    """
    lats = [c[0] for c in coords]
    lngs = [c[1] for c in coords]
    buf_lat = buffer_km / 111.32
    center_lat = (min(lats) + max(lats)) / 2.0
    cos_v = max(0.2, math.cos(math.radians(center_lat)))
    buf_lng = buffer_km / (111.32 * cos_v)
    return (
        min(lats) - buf_lat,
        min(lngs) - buf_lng,
        max(lats) + buf_lat,
        max(lngs) + buf_lng,
    )
