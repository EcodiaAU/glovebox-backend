# app/services/presence.py
#
# Dead-reckoning presence awareness between roamers.
# Each roamer pings their position whenever they get signal.
# When another roamer queries, we project all pings forward
# using speed + heading to estimate current positions.

from __future__ import annotations

import math
import sqlite3
import threading
from datetime import datetime, timezone
from typing import List

from app.core.contracts import NearbyRoamer
from app.core.storage import upsert_presence, get_nearby_presence
from app.core.time import utc_now_iso


# Earth radius in km (WGS84 mean)
_R_KM = 6371.0


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two points in km."""
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlng / 2) ** 2
    return 2 * _R_KM * math.asin(math.sqrt(a))


def _project_position(
    lat: float, lng: float,
    speed_kmh: float, heading_deg: float,
    elapsed_s: float,
) -> tuple[float, float]:
    """
    Dead-reckoning: project a position forward in time.
    Uses constant-velocity straight-line on a sphere.
    """
    if speed_kmh <= 0 or elapsed_s <= 0:
        return lat, lng

    distance_km = speed_kmh * (elapsed_s / 3600.0)
    bearing_rad = math.radians(heading_deg)
    lat_rad = math.radians(lat)
    lng_rad = math.radians(lng)
    d_over_r = distance_km / _R_KM

    new_lat = math.asin(
        math.sin(lat_rad) * math.cos(d_over_r)
        + math.cos(lat_rad) * math.sin(d_over_r) * math.cos(bearing_rad)
    )
    new_lng = lng_rad + math.atan2(
        math.sin(bearing_rad) * math.sin(d_over_r) * math.cos(lat_rad),
        math.cos(d_over_r) - math.sin(lat_rad) * math.sin(new_lat),
    )

    return math.degrees(new_lat), math.degrees(new_lng)


def _confidence(elapsed_s: float) -> str:
    """Prediction confidence degrades with time since last ping."""
    if elapsed_s < 600:       # <10 min
        return "high"
    elif elapsed_s < 3600:    # <1 hour
        return "medium"
    return "low"


class Presence:
    _lock = threading.Lock()

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def ping(
        self,
        *,
        user_id: str,
        lat: float,
        lng: float,
        speed_kmh: float,
        heading_deg: float,
    ) -> None:
        """Upsert the user's latest known position."""
        with self._lock:
            upsert_presence(
                self.conn,
                user_id=user_id,
                lat=lat, lng=lng,
                speed_kmh=speed_kmh,
                heading_deg=heading_deg,
                pinged_at=utc_now_iso(),
            )

    def nearby(
        self,
        *,
        user_id: str,
        lat: float,
        lng: float,
        radius_km: float = 50.0,
    ) -> List[NearbyRoamer]:
        """
        Find other roamers predicted to be within radius_km.
        Projects each ping forward using dead-reckoning.
        """
        now = datetime.now(timezone.utc)
        with self._lock:
            rows = get_nearby_presence(
                self.conn, lat=lat, lng=lng,
                exclude_user_id=user_id,
                max_age_hours=4.0,
            )

        results: list[NearbyRoamer] = []
        for r in rows:
            pinged_at = datetime.fromisoformat(r["pinged_at"].replace("Z", "+00:00"))
            elapsed_s = (now - pinged_at).total_seconds()
            if elapsed_s < 0:
                elapsed_s = 0

            pred_lat, pred_lng = _project_position(
                r["lat"], r["lng"],
                r["speed_kmh"], r["heading_deg"],
                elapsed_s,
            )

            dist = _haversine_km(lat, lng, pred_lat, pred_lng)
            if dist > radius_km:
                continue

            results.append(NearbyRoamer(
                user_id=r["user_id"],
                predicted_lat=round(pred_lat, 6),
                predicted_lng=round(pred_lng, 6),
                speed_kmh=r["speed_kmh"],
                heading_deg=r["heading_deg"],
                last_pinged_at=r["pinged_at"],
                predicted_at=utc_now_iso(),
                distance_km=round(dist, 2),
                confidence=_confidence(elapsed_s),
            ))

        results.sort(key=lambda r: r.distance_km)
        return results
