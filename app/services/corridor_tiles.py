"""Per-trip z16 corridor street tiles, served from Supabase Storage.

The nationwide basemap pack is z14 (no street zoom). A route-clipped z16 pack
(built out-of-band by tiles/build-corridor.sh, a few MB) is what lets the iOS
turn-by-turn screen render real streets offline. Tile generation needs
tilemaker + Docker + an OSM extract, none of which exist in the Cloud Run
backend, so the backend never generates: it READS a pre-generated pack from
Supabase Storage (durable + shared across ephemeral instances) and ships it
inside the trip bundle zip as `corridor-tiles.pmtiles`.

Everything here is best-effort and gated on `settings.corridor_tiles_enabled`
(default off). A miss, an error, or the flag being off all resolve to "no
pack", so the bundle omits the member and iOS falls back to the nationwide z14
pack exactly as it does today. Nothing in this module can raise into the bundle
hot path.
"""

from __future__ import annotations

import hashlib
import logging

from app.core.contracts import BBox4
from app.core.settings import settings

logger = logging.getLogger(__name__)

# Smallest accepted pack. A real corridor pack is megabytes; anything tiny is a
# truncated upload or a placeholder object and is treated as absent.
_MIN_PACK_BYTES = 100_000

# Quantise the corridor bbox onto a ~0.05 degree (~5 km) grid before hashing, so
# routes through the same region resolve to the SAME pack (one generated pack
# serves many similar trips) while distinct regions stay separate. The producer
# (tiles/gen-corridor.sh) calls this exact function so keys never drift.
_GRID_DEG = 0.05


def corridor_tiles_key(bbox: BBox4, maxzoom: int = 16) -> str:
    def q(v: float) -> float:
        return round(v / _GRID_DEG) * _GRID_DEG

    raw = f"{q(bbox.minLng):.2f},{q(bbox.minLat):.2f},{q(bbox.maxLng):.2f},{q(bbox.maxLat):.2f}|z{maxzoom}"
    return "ct_" + hashlib.sha1(raw.encode()).hexdigest()[:20]


def storage_path(key: str) -> str:
    return f"{key}.pmtiles"


def fetch_from_storage(key: str | None) -> bytes | None:
    """Download a corridor pmtiles by key from Supabase Storage, or None.

    Returns None when the flag is off, the key is empty, the object is missing
    or too small, or any error occurs. Never raises - the bundle must keep
    working regardless of corridor-tile availability.
    """
    if not key or not settings.corridor_tiles_enabled:
        return None
    try:
        from app.core.supabase_admin import get_supabase_admin

        client = get_supabase_admin()
        data = client.storage.from_(settings.corridor_tiles_bucket).download(storage_path(key))
        if data and len(data) >= _MIN_PACK_BYTES:
            return data
        logger.info("corridor-tiles %s absent or too small (%s bytes) - omitting",
                    key, len(data) if data else 0)
        return None
    except Exception as exc:  # noqa: BLE001 - best-effort; never break the bundle
        logger.info("corridor-tiles fetch miss for %s: %s", key, exc)
        return None


if __name__ == "__main__":
    # CLI so the out-of-band producer (tiles/gen-corridor.sh) derives the
    # storage key from the SAME function the bundle endpoint uses, with no
    # second implementation to drift.
    #   python -m app.services.corridor_tiles <minLng> <minLat> <maxLng> <maxLat> [maxzoom]
    import sys

    a = sys.argv[1:]
    if len(a) < 4:
        sys.stderr.write(
            "usage: python -m app.services.corridor_tiles "
            "<minLng> <minLat> <maxLng> <maxLat> [maxzoom]\n"
        )
        raise SystemExit(2)
    mz = int(a[4]) if len(a) > 4 else 16
    box = BBox4(minLng=float(a[0]), minLat=float(a[1]), maxLng=float(a[2]), maxLat=float(a[3]))
    print(corridor_tiles_key(box, mz))
