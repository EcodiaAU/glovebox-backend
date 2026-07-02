# app/services/overlay_normalize.py
"""
Flatten typed overlay responses to the unified OverlayPack shape that mobile
clients (iOS, Android) consume. Each overlay has its own rich domain shape
(stations, incidents, hotspots, gauges, cameras, ...) but the mobile pin
renderer only needs a flat list of {id, name, lat, lng, severity?,
description?} entries plus a source + fetched_at marker.

This module owns the kind -> mapper registry. Any new overlay just adds
a mapper here and surfaces through /nav/overlay/{kind} automatically.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _item(
    *,
    id: Optional[str] = None,
    name: Optional[str] = None,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    distance_km: Optional[float] = None,
    severity: Optional[str] = None,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a single OverlayItem dict, dropping nulls so the wire is compact."""
    out: Dict[str, Any] = {}
    if id is not None:
        out["id"] = id
    if name is not None:
        out["name"] = name
    if lat is not None:
        out["lat"] = lat
    if lng is not None:
        out["lng"] = lng
    if distance_km is not None:
        out["distanceKm"] = distance_km
    if severity is not None:
        out["severity"] = severity
    if description is not None:
        out["description"] = description
    return out


# ══════════════════════════════════════════════════════════════
# Per-kind mappers
# Each takes a Pydantic model instance and returns a list of items.
# Mappers stay defensive: missing fields default to None.
# ══════════════════════════════════════════════════════════════


def _map_fuel(o: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for s in getattr(o, "stations", []) or []:
        items.append(
            _item(
                id=getattr(s, "id", None),
                name=getattr(s, "name", None),
                lat=getattr(s, "lat", None),
                lng=getattr(s, "lng", None),
                distance_km=getattr(s, "distance_from_route_km", None),
            )
        )
    for c in getattr(o, "ev_chargers", []) or []:
        items.append(
            _item(
                id=getattr(c, "id", None),
                name=getattr(c, "name", None) or "EV charger",
                lat=getattr(c, "lat", None),
                lng=getattr(c, "lng", None),
                distance_km=getattr(c, "distance_from_route_km", None),
            )
        )
    return items


def _map_rest_areas(o: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for r in getattr(o, "rest_areas", []) or []:
        items.append(
            _item(
                id=getattr(r, "id", None),
                name=getattr(r, "name", None),
                lat=getattr(r, "lat", None),
                lng=getattr(r, "lng", None),
                distance_km=getattr(r, "distance_from_route_km", None),
                description=getattr(r, "facilities", None),
            )
        )
    return items


def _map_weather(o: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for s in getattr(o, "samples", []) or []:
        items.append(
            _item(
                id=None,
                name=getattr(s, "condition", None)
                or getattr(s, "weather_code_label", None),
                lat=getattr(s, "lat", None),
                lng=getattr(s, "lng", None),
                distance_km=getattr(s, "km_from_start", None),
                severity=getattr(s, "severity", None),
            )
        )
    return items


def _map_hazards(o: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for h in getattr(o, "hazards", []) or []:
        items.append(
            _item(
                id=getattr(h, "id", None),
                name=getattr(h, "title", None) or getattr(h, "name", None),
                lat=getattr(h, "lat", None),
                lng=getattr(h, "lng", None),
                severity=getattr(h, "severity", None),
                description=getattr(h, "description", None),
            )
        )
    return items


def _map_traffic(o: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for t in getattr(o, "incidents", []) or []:
        items.append(
            _item(
                id=getattr(t, "id", None),
                name=getattr(t, "title", None) or getattr(t, "name", None),
                lat=getattr(t, "lat", None),
                lng=getattr(t, "lng", None),
                severity=getattr(t, "severity", None),
                description=getattr(t, "description", None),
            )
        )
    return items


def _map_flood(o: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for g in getattr(o, "gauges", []) or []:
        items.append(
            _item(
                id=getattr(g, "station_no", None),
                name=getattr(g, "station_name", None),
                lat=getattr(g, "lat", None),
                lng=getattr(g, "lng", None),
                distance_km=getattr(g, "distance_from_route_km", None),
                severity=getattr(g, "severity", None),
                description=f"Trend: {getattr(g, 'trend', 'unknown')}",
            )
        )
    for c in getattr(o, "cameras", []) or []:
        items.append(
            _item(
                id=getattr(c, "id", None),
                name=getattr(c, "name", None) or "Flood camera",
                lat=getattr(c, "lat", None),
                lng=getattr(c, "lng", None),
            )
        )
    return items


def _map_coverage(o: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for g in getattr(o, "gaps", []) or []:
        items.append(
            _item(
                id=getattr(g, "id", None),
                name=getattr(g, "carrier", None) or "Coverage gap",
                lat=getattr(g, "lat", None),
                lng=getattr(g, "lng", None),
                distance_km=getattr(g, "km_from_start", None),
                severity=getattr(g, "severity", None),
            )
        )
    return items


def _map_wildlife(o: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for sp in getattr(o, "sightings", []) or []:
        items.append(
            _item(
                id=getattr(sp, "id", None),
                name=getattr(sp, "species", None) or getattr(sp, "name", None),
                lat=getattr(sp, "lat", None),
                lng=getattr(sp, "lng", None),
                distance_km=getattr(sp, "distance_from_route_km", None),
                description=getattr(sp, "common_name", None),
            )
        )
    return items


def _map_emergency(o: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for s in getattr(o, "services", []) or []:
        items.append(
            _item(
                id=getattr(s, "id", None),
                name=getattr(s, "name", None),
                lat=getattr(s, "lat", None),
                lng=getattr(s, "lng", None),
                distance_km=getattr(s, "distance_from_route_km", None),
                description=getattr(s, "service_type", None),
            )
        )
    return items


def _map_heritage(o: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for s in getattr(o, "sites", []) or []:
        items.append(
            _item(
                id=getattr(s, "id", None),
                name=getattr(s, "name", None),
                lat=getattr(s, "lat", None),
                lng=getattr(s, "lng", None),
                distance_km=getattr(s, "distance_from_route_km", None),
                description=getattr(s, "description", None),
            )
        )
    return items


def _map_air_quality(o: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for s in getattr(o, "stations", []) or []:
        aqi = getattr(s, "aqi", None)
        items.append(
            _item(
                id=getattr(s, "id", None),
                name=getattr(s, "name", None),
                lat=getattr(s, "lat", None),
                lng=getattr(s, "lng", None),
                severity=getattr(s, "category", None),
                description=f"AQI: {aqi}" if aqi is not None else None,
            )
        )
    return items


def _map_bushfire(o: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for i in getattr(o, "incidents", []) or []:
        items.append(
            _item(
                id=getattr(i, "id", None),
                name=getattr(i, "title", None) or getattr(i, "name", None),
                lat=getattr(i, "lat", None),
                lng=getattr(i, "lng", None),
                severity=getattr(i, "alert_level", None) or getattr(i, "status", None),
                description=getattr(i, "location", None),
            )
        )
    for h in getattr(o, "hotspots", []) or []:
        items.append(
            _item(
                id=None,
                name="Hotspot",
                lat=getattr(h, "lat", None),
                lng=getattr(h, "lng", None),
                severity=getattr(h, "confidence", None),
            )
        )
    return items


def _map_speed_cameras(o: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for c in getattr(o, "cameras", []) or []:
        items.append(
            _item(
                id=getattr(c, "id", None),
                name=getattr(c, "location_desc", None)
                or getattr(c, "camera_type", None),
                lat=getattr(c, "lat", None),
                lng=getattr(c, "lng", None),
                distance_km=getattr(c, "distance_from_route_km", None),
            )
        )
    for b in getattr(o, "black_spots", []) or []:
        items.append(
            _item(
                id=getattr(b, "id", None),
                name=getattr(b, "road", None) or "Black spot",
                lat=getattr(b, "lat", None),
                lng=getattr(b, "lng", None),
                severity="high",
                description=getattr(b, "location_desc", None),
            )
        )
    return items


def _map_toilets(o: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for t in getattr(o, "toilets", []) or []:
        items.append(
            _item(
                id=getattr(t, "id", None),
                name=getattr(t, "name", None) or "Toilet",
                lat=getattr(t, "lat", None),
                lng=getattr(t, "lng", None),
                description=getattr(t, "address", None),
            )
        )
    return items


def _map_school_zones(o: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for z in getattr(o, "zones", []) or []:
        items.append(
            _item(
                id=getattr(z, "id", None),
                name=getattr(z, "school_name", None) or "School zone",
                lat=getattr(z, "lat", None),
                lng=getattr(z, "lng", None),
                distance_km=getattr(z, "distance_from_route_km", None),
                description=getattr(z, "speed_limit", None),
            )
        )
    return items


def _map_roadkill(o: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for r in getattr(o, "incidents", []) or []:
        items.append(
            _item(
                id=getattr(r, "id", None),
                name=getattr(r, "species", None) or "Roadkill",
                lat=getattr(r, "lat", None),
                lng=getattr(r, "lng", None),
                distance_km=getattr(r, "distance_from_route_km", None),
                severity=getattr(r, "severity", None),
            )
        )
    return items


def _map_elevation(o: Any) -> List[Dict[str, Any]]:
    """Elevation profile flattened so the strip renderer can draw a sparkline."""
    items: List[Dict[str, Any]] = []
    profile = getattr(o, "profile", o)
    samples = getattr(profile, "samples", []) or []
    for s in samples:
        items.append(
            _item(
                id=None,
                name=None,
                lat=getattr(s, "lat", None),
                lng=getattr(s, "lng", None),
                distance_km=getattr(s, "km_from_start", None),
                description=str(getattr(s, "elevation_m", "")),
            )
        )
    return items


def _map_route_score(o: Any) -> List[Dict[str, Any]]:
    """Route score is summary-shaped, not pin-shaped. One item carrying the overall score."""
    overall = getattr(o, "overall", None)
    label = getattr(o, "overall_label", None) or ""
    summary = getattr(o, "summary", None)
    if overall is None:
        return []
    return [
        _item(
            id="route-score",
            name=f"Route score: {overall}/10 ({label})",
            severity=label.lower() if label else None,
            description=summary,
        )
    ]


_MAPPERS: Dict[str, Callable[[Any], List[Dict[str, Any]]]] = {
    "fuel-along-route": _map_fuel,
    "rest-areas": _map_rest_areas,
    "weather": _map_weather,
    "hazards": _map_hazards,
    "traffic": _map_traffic,
    "flood": _map_flood,
    "coverage": _map_coverage,
    "wildlife": _map_wildlife,
    "emergency": _map_emergency,
    "heritage": _map_heritage,
    "air-quality": _map_air_quality,
    "bushfire": _map_bushfire,
    "speed-cameras": _map_speed_cameras,
    "toilets": _map_toilets,
    "school-zones": _map_school_zones,
    "roadkill": _map_roadkill,
    "elevation": _map_elevation,
    "route-score": _map_route_score,
}


def normalize(kind: str, overlay: Any, source: Optional[str] = None) -> Dict[str, Any]:
    """
    Flatten any typed overlay to the OverlayPack shape mobile expects:

        { items: [...], source: str?, fetchedAt: str? }

    Unknown kinds return an empty pack so the client renders zero pins
    rather than failing.
    """
    mapper = _MAPPERS.get(kind)
    items = mapper(overlay) if mapper else []
    return {
        "items": items,
        "source": source or kind,
        "fetchedAt": _utc_now_iso(),
    }
