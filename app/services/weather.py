from __future__ import annotations

"""
Weather overlay service for Roam.

Provides forecast weather conditions at points along a route, timed to the
user's estimated arrival at each point.

NOTE: Open-Meteo integration has been removed. This service currently returns
an empty overlay. The contract models (WeatherOverlay, WeatherPoint) are
preserved so the frontend and bundle flow continue to work.
"""

import logging

from app.core.contracts import WeatherOverlay
from app.core.settings import settings
from app.core.time import utc_now_iso

logger = logging.getLogger(__name__)


class Weather:
    def __init__(self, *, conn):
        self.conn = conn

    async def forecast_along_route(
        self,
        *,
        polyline6: str,
        departure_iso: str,
        avg_speed_kmh: float = 90.0,
        sample_interval_km: float | None = None,
    ) -> WeatherOverlay:
        """Return an empty weather overlay (data source removed)."""
        return WeatherOverlay(
            weather_key="disabled",
            polyline6=polyline6,
            departure_iso=departure_iso,
            algo_version=settings.weather_algo_version,
            created_at=utc_now_iso(),
            points=[],
            warnings=["Weather data source disabled."],
        )
