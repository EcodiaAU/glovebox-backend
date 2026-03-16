from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import orjson

from app.services.places_store import PlacesStore


# ──────────────────────────────────────────────────────────────
# Connections
# ──────────────────────────────────────────────────────────────

def connect_sqlite(path: str) -> sqlite3.Connection:
    """
    Open a RW SQLite connection with sane pragmas.

    IMPORTANT:
    - SQLite will NOT create parent directories.
    - WAL mode requires the directory to be writable (creates -wal/-shm).
    """
    if path != ":memory:":
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA cache_size=-32768;")   # 32 MiB page cache
    conn.execute("PRAGMA mmap_size=268435456;") # 256 MiB mmap
    return conn


def connect_sqlite_ro(path: str) -> sqlite3.Connection:
    """
    Open a read-only SQLite connection.

    Note:
    - This requires the DB file to already exist.
    - We intentionally do NOT mkdir here.
    """
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.execute("PRAGMA query_only=ON;")
    return conn


# ──────────────────────────────────────────────────────────────
# Batch commit helper
# ──────────────────────────────────────────────────────────────

@contextmanager
def deferred_commit(conn: sqlite3.Connection):
    """
    Context manager that wraps a block of put_* calls in a single
    transaction, eliminating per-write fsync.

    Usage:
        with deferred_commit(conn):
            put_traffic_pack(conn, ..., _commit=False)
            put_hazards_pack(conn, ..., _commit=False)
        # single commit here
    """
    conn.execute("BEGIN IMMEDIATE;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ──────────────────────────────────────────────────────────────
# Bulk byte-size query (used by build_manifest)
# ──────────────────────────────────────────────────────────────

# Maps (table_name, pk_column) for every pack type.
_PACK_TABLE_MAP: Dict[str, Tuple[str, str]] = {
    "nav":           ("nav_packs",          "route_key"),
    "corridor":      ("corridor_packs",     "corridor_key"),
    "places":        ("places_packs",       "places_key"),
    "traffic":       ("traffic_packs",      "traffic_key"),
    "hazards":       ("hazard_packs",       "hazards_key"),
    "weather":       ("weather_packs",      "weather_key"),
    "fuel":          ("fuel_packs",         "fuel_key"),
    "flood":         ("flood_packs",        "flood_key"),
    "coverage":      ("coverage_packs",     "coverage_key"),
    "wildlife":      ("wildlife_packs",     "wildlife_key"),
    "rest":          ("rest_area_packs",    "rest_key"),
    "score":         ("score_packs",        "score_key"),
    "emergency":     ("emergency_packs",    "emergency_key"),
    "heritage":      ("heritage_packs",     "heritage_key"),
    "aqi":           ("aqi_packs",          "aqi_key"),
    "bushfire":      ("bushfire_packs",     "bushfire_key"),
    "cameras":       ("cameras_packs",      "cameras_key"),
    "toilets":       ("toilets_packs",      "toilets_key"),
    "school_zones":  ("school_zone_packs",  "school_zones_key"),
    "roadkill":      ("roadkill_packs",     "roadkill_key"),
}


def bulk_pack_bytes(conn: sqlite3.Connection, keys: Dict[str, Optional[str]]) -> Dict[str, int]:
    """
    Fetch byte sizes for multiple pack types in one call.

    *keys* maps pack-type names (matching _PACK_TABLE_MAP) to their cache
    keys.  Returns a dict with the same pack-type names mapped to byte
    counts (0 if key is None or row missing).
    """
    result: Dict[str, int] = {}
    for pack_type, key in keys.items():
        if not key:
            result[pack_type] = 0
            continue
        info = _PACK_TABLE_MAP.get(pack_type)
        if not info:
            result[pack_type] = 0
            continue
        table, pk_col = info
        cur = conn.execute(
            f"SELECT length(pack_json) FROM {table} WHERE {pk_col}=?;",
            (key,),
        )
        row = cur.fetchone()
        result[pack_type] = int(row[0]) if row and row[0] is not None else 0
    return result


def bulk_pack_raw(conn: sqlite3.Connection, keys: Dict[str, Optional[str]]) -> Dict[str, Optional[bytes]]:
    """
    Fetch raw JSON blobs for multiple pack types in one call.

    Returns dict mapping pack-type name to raw bytes (None if missing).
    """
    result: Dict[str, Optional[bytes]] = {}
    for pack_type, key in keys.items():
        if not key:
            result[pack_type] = None
            continue
        info = _PACK_TABLE_MAP.get(pack_type)
        if not info:
            result[pack_type] = None
            continue
        table, pk_col = info
        cur = conn.execute(
            f"SELECT pack_json FROM {table} WHERE {pk_col}=?;",
            (key,),
        )
        row = cur.fetchone()
        result[pack_type] = bytes(row[0]) if row else None
    return result


# ──────────────────────────────────────────────────────────────
# Schema
# ──────────────────────────────────────────────────────────────

def ensure_schema(conn: sqlite3.Connection) -> None:
    # Core cache tables
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS corridor_packs (
            corridor_key TEXT PRIMARY KEY,
            route_key TEXT NOT NULL,
            profile TEXT NOT NULL,
            buffer_m INTEGER NOT NULL,
            max_edges INTEGER NOT NULL,
            algo_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            pack_json BLOB NOT NULL
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS places_packs (
            places_key TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            algo_version TEXT NOT NULL,
            pack_json BLOB NOT NULL
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS nav_packs (
            route_key TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            algo_version TEXT NOT NULL,
            pack_json BLOB NOT NULL
        );
        """
    )

    # Overlays (traffic + hazards)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS traffic_packs (
            traffic_key TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            algo_version TEXT NOT NULL,
            pack_json BLOB NOT NULL
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hazard_packs (
            hazards_key TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            algo_version TEXT NOT NULL,
            pack_json BLOB NOT NULL
        );
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS weather_packs (
            weather_key TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            algo_version TEXT NOT NULL,
            pack_json BLOB NOT NULL
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS manifests (
            plan_id TEXT PRIMARY KEY,
            route_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            manifest_json BLOB NOT NULL
        );
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fuel_packs (
            fuel_key TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            algo_version TEXT NOT NULL,
            pack_json BLOB NOT NULL
        );
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS flood_packs (
            flood_key TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            algo_version TEXT NOT NULL,
            pack_json BLOB NOT NULL
        );
        """
    )

    # BOM gauge station list cache (refreshed every flood_station_refresh_hours; ~8000 stations)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS flood_stations (
            id INTEGER PRIMARY KEY,
            fetched_at TEXT NOT NULL,
            stations_json BLOB NOT NULL
        );
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rest_area_packs (
            rest_key TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            algo_version TEXT NOT NULL,
            pack_json BLOB NOT NULL
        );
        """
    )

    # Cell towers from OpenCelliD bulk CSV (MCC 505 = Australia).
    # ~50-100k rows; spatial queries use a (lat_bucket, lon_bucket) index
    # on 0.1° tiles (~11km) so we only scan rows in nearby tiles.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cell_towers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            radio TEXT NOT NULL,
            mcc INTEGER NOT NULL,
            mnc INTEGER NOT NULL,
            carrier_name TEXT NOT NULL,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            range_m INTEGER NOT NULL,
            samples INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            lat_bucket INTEGER NOT NULL,
            lon_bucket INTEGER NOT NULL
        );
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_cell_towers_bucket
        ON cell_towers (lat_bucket, lon_bucket);
        """
    )
    # Track when the cell tower data was last imported.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cell_towers_meta (
            id INTEGER PRIMARY KEY,
            imported_at TEXT NOT NULL,
            row_count INTEGER NOT NULL
        );
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS coverage_packs (
            coverage_key TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            algo_version TEXT NOT NULL,
            pack_json BLOB NOT NULL
        );
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wildlife_packs (
            wildlife_key TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            algo_version TEXT NOT NULL,
            pack_json BLOB NOT NULL
        );
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS score_packs (
            score_key TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            algo_version TEXT NOT NULL,
            pack_json BLOB NOT NULL
        );
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS emergency_packs (
            emergency_key TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            algo_version TEXT NOT NULL,
            pack_json BLOB NOT NULL
        );
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS heritage_packs (
            heritage_key TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            algo_version TEXT NOT NULL,
            pack_json BLOB NOT NULL
        );
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS aqi_packs (
            aqi_key TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            algo_version TEXT NOT NULL,
            pack_json BLOB NOT NULL
        );
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bushfire_packs (
            bushfire_key TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            algo_version TEXT NOT NULL,
            pack_json BLOB NOT NULL
        );
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cameras_packs (
            cameras_key TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            algo_version TEXT NOT NULL,
            pack_json BLOB NOT NULL
        );
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS toilets_packs (
            toilets_key TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            algo_version TEXT NOT NULL,
            pack_json BLOB NOT NULL
        );
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS school_zone_packs (
            school_zones_key TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            algo_version TEXT NOT NULL,
            pack_json BLOB NOT NULL
        );
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS roadkill_packs (
            roadkill_key TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            algo_version TEXT NOT NULL,
            pack_json BLOB NOT NULL
        );
        """
    )

    # Mapbox geocoding response cache (TTL-based, keyed by SHA256 of query params)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS geocode_cache (
            cache_key TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            pack_json BLOB NOT NULL
        );
        """
    )

    # ── Presence (dead-reckoning proximity) ──────────────────────
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS presence (
            user_id TEXT PRIMARY KEY,
            lat REAL NOT NULL,
            lng REAL NOT NULL,
            speed_kmh REAL NOT NULL DEFAULT 0,
            heading_deg REAL NOT NULL DEFAULT 0,
            pinged_at TEXT NOT NULL,
            lat_bucket INTEGER NOT NULL,
            lng_bucket INTEGER NOT NULL
        );
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_presence_bucket
        ON presence (lat_bucket, lng_bucket);
        """
    )

    # ── User Observations (crowd-sourced road intel) ──────────
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_observations (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            type TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'info',
            lat REAL NOT NULL,
            lng REAL NOT NULL,
            heading_deg REAL,
            message TEXT,
            value TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            lat_bucket INTEGER NOT NULL,
            lng_bucket INTEGER NOT NULL
        );
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_obs_bucket
        ON user_observations (lat_bucket, lng_bucket);
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_obs_created
        ON user_observations (created_at);
        """
    )

    # Canonical POI store (local-first)
    PlacesStore(conn).ensure_schema()

    conn.commit()


# ──────────────────────────────────────────────────────────────
# Bytes helpers (used by Bundle)
# ──────────────────────────────────────────────────────────────

def _len_or_zero(row) -> int:
    return int(row[0]) if row and row[0] is not None else 0


def get_nav_pack_bytes(conn: sqlite3.Connection, route_key: str) -> int:
    cur = conn.execute("SELECT length(pack_json) FROM nav_packs WHERE route_key=?;", (route_key,))
    return _len_or_zero(cur.fetchone())


def get_corridor_pack_bytes(conn: sqlite3.Connection, corridor_key: str | None) -> int:
    if not corridor_key:
        return 0
    cur = conn.execute("SELECT length(pack_json) FROM corridor_packs WHERE corridor_key=?;", (corridor_key,))
    return _len_or_zero(cur.fetchone())


def get_places_pack_bytes(conn: sqlite3.Connection, places_key: str | None) -> int:
    if not places_key:
        return 0
    cur = conn.execute("SELECT length(pack_json) FROM places_packs WHERE places_key=?;", (places_key,))
    return _len_or_zero(cur.fetchone())


def get_traffic_pack_bytes(conn: sqlite3.Connection, traffic_key: str | None) -> int:
    if not traffic_key:
        return 0
    cur = conn.execute("SELECT length(pack_json) FROM traffic_packs WHERE traffic_key=?;", (traffic_key,))
    return _len_or_zero(cur.fetchone())


def get_hazards_pack_bytes(conn: sqlite3.Connection, hazards_key: str | None) -> int:
    if not hazards_key:
        return 0
    cur = conn.execute("SELECT length(pack_json) FROM hazard_packs WHERE hazards_key=?;", (hazards_key,))
    return _len_or_zero(cur.fetchone())


def get_weather_pack_bytes(conn: sqlite3.Connection, weather_key: str | None) -> int:
    if not weather_key:
        return 0
    cur = conn.execute("SELECT length(pack_json) FROM weather_packs WHERE weather_key=?;", (weather_key,))
    return _len_or_zero(cur.fetchone())


# ──────────────────────────────────────────────────────────────
# Corridor packs
# ──────────────────────────────────────────────────────────────

def put_corridor_pack(
    conn: sqlite3.Connection,
    *,
    corridor_key: str,
    route_key: str,
    profile: str,
    buffer_m: int,
    max_edges: int,
    algo_version: str,
    created_at: str,
    pack: dict,
    _commit: bool = True,
) -> int:
    blob = orjson.dumps(pack)
    conn.execute(
        """
        INSERT OR REPLACE INTO corridor_packs
          (corridor_key, route_key, profile, buffer_m, max_edges, algo_version, created_at, pack_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (corridor_key, route_key, profile, int(buffer_m), int(max_edges), algo_version, created_at, blob),
    )
    if _commit:
        conn.commit()
    return len(blob)


def get_corridor_pack(conn: sqlite3.Connection, corridor_key: str) -> Optional[dict]:
    cur = conn.execute("SELECT pack_json FROM corridor_packs WHERE corridor_key=?;", (corridor_key,))
    row = cur.fetchone()
    if not row:
        return None
    return orjson.loads(row[0])


# ──────────────────────────────────────────────────────────────
# Places packs
# ──────────────────────────────────────────────────────────────

def put_places_pack(
    conn: sqlite3.Connection,
    *,
    places_key: str,
    created_at: str,
    algo_version: str,
    pack: dict,
    _commit: bool = True,
) -> int:
    blob = orjson.dumps(pack)
    conn.execute(
        """
        INSERT OR REPLACE INTO places_packs (places_key, created_at, algo_version, pack_json)
        VALUES (?, ?, ?, ?);
        """,
        (places_key, created_at, algo_version, blob),
    )
    if _commit:
        conn.commit()
    return len(blob)


def get_places_pack(conn: sqlite3.Connection, places_key: str) -> Optional[dict]:
    cur = conn.execute("SELECT pack_json FROM places_packs WHERE places_key=?;", (places_key,))
    row = cur.fetchone()
    if not row:
        return None
    return orjson.loads(row[0])


# ──────────────────────────────────────────────────────────────
# Nav packs
# ──────────────────────────────────────────────────────────────

def put_nav_pack(
    conn: sqlite3.Connection,
    *,
    route_key: str,
    created_at: str,
    algo_version: str,
    pack: dict,
    _commit: bool = True,
) -> int:
    blob = orjson.dumps(pack)
    conn.execute(
        """
        INSERT OR REPLACE INTO nav_packs (route_key, created_at, algo_version, pack_json)
        VALUES (?, ?, ?, ?);
        """,
        (route_key, created_at, algo_version, blob),
    )
    if _commit:
        conn.commit()
    return len(blob)


def get_nav_pack(conn: sqlite3.Connection, route_key: str) -> Optional[dict]:
    cur = conn.execute("SELECT pack_json FROM nav_packs WHERE route_key=?;", (route_key,))
    row = cur.fetchone()
    if not row:
        return None
    return orjson.loads(row[0])


# ──────────────────────────────────────────────────────────────
# Traffic packs
# ──────────────────────────────────────────────────────────────

def put_traffic_pack(
    conn: sqlite3.Connection,
    *,
    traffic_key: str,
    created_at: str,
    algo_version: str,
    pack: dict,
    _commit: bool = True,
) -> int:
    blob = orjson.dumps(pack)
    conn.execute(
        """
        INSERT OR REPLACE INTO traffic_packs (traffic_key, created_at, algo_version, pack_json)
        VALUES (?, ?, ?, ?);
        """,
        (traffic_key, created_at, algo_version, blob),
    )
    if _commit:
        conn.commit()
    return len(blob)


def get_traffic_pack(conn: sqlite3.Connection, traffic_key: str) -> Optional[dict]:
    cur = conn.execute("SELECT pack_json FROM traffic_packs WHERE traffic_key=?;", (traffic_key,))
    row = cur.fetchone()
    if not row:
        return None
    return orjson.loads(row[0])


# ──────────────────────────────────────────────────────────────
# Hazards packs
# ──────────────────────────────────────────────────────────────

def put_hazards_pack(
    conn: sqlite3.Connection,
    *,
    hazards_key: str,
    created_at: str,
    algo_version: str,
    pack: dict,
    _commit: bool = True,
) -> int:
    blob = orjson.dumps(pack)
    conn.execute(
        """
        INSERT OR REPLACE INTO hazard_packs (hazards_key, created_at, algo_version, pack_json)
        VALUES (?, ?, ?, ?);
        """,
        (hazards_key, created_at, algo_version, blob),
    )
    if _commit:
        conn.commit()
    return len(blob)


def get_hazards_pack(conn: sqlite3.Connection, hazards_key: str) -> Optional[dict]:
    cur = conn.execute("SELECT pack_json FROM hazard_packs WHERE hazards_key=?;", (hazards_key,))
    row = cur.fetchone()
    if not row:
        return None
    return orjson.loads(row[0])


# ──────────────────────────────────────────────────────────────
# Weather packs
# ──────────────────────────────────────────────────────────────

def put_weather_pack(
    conn: sqlite3.Connection,
    *,
    weather_key: str,
    created_at: str,
    algo_version: str,
    pack: dict,
    _commit: bool = True,
) -> int:
    blob = orjson.dumps(pack)
    conn.execute(
        """
        INSERT OR REPLACE INTO weather_packs (weather_key, created_at, algo_version, pack_json)
        VALUES (?, ?, ?, ?);
        """,
        (weather_key, created_at, algo_version, blob),
    )
    if _commit:
        conn.commit()
    return len(blob)


def get_weather_pack(conn: sqlite3.Connection, weather_key: str) -> Optional[dict]:
    cur = conn.execute("SELECT pack_json FROM weather_packs WHERE weather_key=?;", (weather_key,))
    row = cur.fetchone()
    if not row:
        return None
    return orjson.loads(row[0])


# ──────────────────────────────────────────────────────────────
# Raw-bytes accessors (used by build_zip to skip JSON round-trip)
# ──────────────────────────────────────────────────────────────

def get_nav_pack_raw(conn: sqlite3.Connection, route_key: str) -> Optional[bytes]:
    cur = conn.execute("SELECT pack_json FROM nav_packs WHERE route_key=?;", (route_key,))
    row = cur.fetchone()
    return bytes(row[0]) if row else None


def get_corridor_pack_raw(conn: sqlite3.Connection, corridor_key: str) -> Optional[bytes]:
    cur = conn.execute("SELECT pack_json FROM corridor_packs WHERE corridor_key=?;", (corridor_key,))
    row = cur.fetchone()
    return bytes(row[0]) if row else None


def get_places_pack_raw(conn: sqlite3.Connection, places_key: str) -> Optional[bytes]:
    cur = conn.execute("SELECT pack_json FROM places_packs WHERE places_key=?;", (places_key,))
    row = cur.fetchone()
    return bytes(row[0]) if row else None


def get_traffic_pack_raw(conn: sqlite3.Connection, traffic_key: str) -> Optional[bytes]:
    cur = conn.execute("SELECT pack_json FROM traffic_packs WHERE traffic_key=?;", (traffic_key,))
    row = cur.fetchone()
    return bytes(row[0]) if row else None


def get_hazards_pack_raw(conn: sqlite3.Connection, hazards_key: str) -> Optional[bytes]:
    cur = conn.execute("SELECT pack_json FROM hazard_packs WHERE hazards_key=?;", (hazards_key,))
    row = cur.fetchone()
    return bytes(row[0]) if row else None


def get_weather_pack_raw(conn: sqlite3.Connection, weather_key: str) -> Optional[bytes]:
    cur = conn.execute("SELECT pack_json FROM weather_packs WHERE weather_key=?;", (weather_key,))
    row = cur.fetchone()
    return bytes(row[0]) if row else None


def get_manifest_raw(conn: sqlite3.Connection, plan_id: str) -> Optional[bytes]:
    cur = conn.execute("SELECT manifest_json FROM manifests WHERE plan_id=?;", (plan_id,))
    row = cur.fetchone()
    return bytes(row[0]) if row else None


# ──────────────────────────────────────────────────────────────
# Manifests
# ──────────────────────────────────────────────────────────────

def put_manifest(
    conn: sqlite3.Connection,
    *,
    plan_id: str,
    route_key: str,
    created_at: str,
    manifest: dict,
    _commit: bool = True,
) -> None:
    blob = orjson.dumps(manifest)
    conn.execute(
        """
        INSERT OR REPLACE INTO manifests (plan_id, route_key, created_at, manifest_json)
        VALUES (?, ?, ?, ?);
        """,
        (plan_id, route_key, created_at, blob),
    )
    if _commit:
        conn.commit()


def get_manifest(conn: sqlite3.Connection, plan_id: str) -> Optional[dict]:
    cur = conn.execute("SELECT manifest_json FROM manifests WHERE plan_id=?;", (plan_id,))
    row = cur.fetchone()
    if not row:
        return None
    return orjson.loads(row[0])


def get_manifest_meta(conn: sqlite3.Connection, plan_id: str) -> Optional[Tuple[str, str]]:
    cur = conn.execute("SELECT route_key, created_at FROM manifests WHERE plan_id=?;", (plan_id,))
    row = cur.fetchone()
    if not row:
        return None
    return str(row[0]), str(row[1])


# ──────────────────────────────────────────────────────────────
# Fuel packs
# ──────────────────────────────────────────────────────────────

def put_fuel_pack(
    conn: sqlite3.Connection,
    *,
    fuel_key: str,
    created_at: str,
    algo_version: str,
    pack: dict,
    _commit: bool = True,
) -> int:
    blob = orjson.dumps(pack)
    conn.execute(
        """
        INSERT OR REPLACE INTO fuel_packs (fuel_key, created_at, algo_version, pack_json)
        VALUES (?, ?, ?, ?);
        """,
        (fuel_key, created_at, algo_version, blob),
    )
    if _commit:
        conn.commit()
    return len(blob)


def get_fuel_pack(conn: sqlite3.Connection, fuel_key: str) -> Optional[dict]:
    cur = conn.execute("SELECT pack_json FROM fuel_packs WHERE fuel_key=?;", (fuel_key,))
    row = cur.fetchone()
    if not row:
        return None
    return orjson.loads(row[0])


def get_fuel_pack_raw(conn: sqlite3.Connection, fuel_key: str) -> Optional[bytes]:
    cur = conn.execute("SELECT pack_json FROM fuel_packs WHERE fuel_key=?;", (fuel_key,))
    row = cur.fetchone()
    return bytes(row[0]) if row else None


def get_fuel_pack_bytes(conn: sqlite3.Connection, fuel_key: Optional[str]) -> int:
    if not fuel_key:
        return 0
    cur = conn.execute("SELECT length(pack_json) FROM fuel_packs WHERE fuel_key=?;", (fuel_key,))
    return _len_or_zero(cur.fetchone())


# ──────────────────────────────────────────────────────────────
# Flood packs
# ──────────────────────────────────────────────────────────────

def put_flood_pack(
    conn: sqlite3.Connection,
    *,
    flood_key: str,
    created_at: str,
    algo_version: str,
    pack: dict,
    _commit: bool = True,
) -> int:
    blob = orjson.dumps(pack)
    conn.execute(
        """
        INSERT OR REPLACE INTO flood_packs (flood_key, created_at, algo_version, pack_json)
        VALUES (?, ?, ?, ?);
        """,
        (flood_key, created_at, algo_version, blob),
    )
    if _commit:
        conn.commit()
    return len(blob)


def get_flood_pack(conn: sqlite3.Connection, flood_key: str) -> Optional[dict]:
    cur = conn.execute("SELECT pack_json FROM flood_packs WHERE flood_key=?;", (flood_key,))
    row = cur.fetchone()
    if not row:
        return None
    return orjson.loads(row[0])


def get_flood_pack_raw(conn: sqlite3.Connection, flood_key: str) -> Optional[bytes]:
    cur = conn.execute("SELECT pack_json FROM flood_packs WHERE flood_key=?;", (flood_key,))
    row = cur.fetchone()
    return bytes(row[0]) if row else None


def get_flood_pack_bytes(conn: sqlite3.Connection, flood_key: Optional[str]) -> int:
    if not flood_key:
        return 0
    cur = conn.execute("SELECT length(pack_json) FROM flood_packs WHERE flood_key=?;", (flood_key,))
    return _len_or_zero(cur.fetchone())


# ──────────────────────────────────────────────────────────────
# Flood station list (BOM bulk station cache)
# ──────────────────────────────────────────────────────────────

def put_flood_stations(conn: sqlite3.Connection, *, fetched_at: str, stations: list) -> None:
    """Store the full BOM station list. Always row id=1 (single cached copy)."""
    blob = orjson.dumps(stations)
    conn.execute(
        """
        INSERT OR REPLACE INTO flood_stations (id, fetched_at, stations_json)
        VALUES (1, ?, ?);
        """,
        (fetched_at, blob),
    )
    conn.commit()


def get_flood_stations(conn: sqlite3.Connection) -> Tuple[Optional[str], Optional[list]]:
    """Return (fetched_at, stations) or (None, None) if not cached."""
    cur = conn.execute("SELECT fetched_at, stations_json FROM flood_stations WHERE id=1;")
    row = cur.fetchone()
    if not row:
        return None, None
    return str(row[0]), orjson.loads(row[1])


# ──────────────────────────────────────────────────────────────
# Rest area packs
# ──────────────────────────────────────────────────────────────

def put_rest_area_pack(
    conn: sqlite3.Connection,
    *,
    rest_key: str,
    created_at: str,
    algo_version: str,
    pack: dict,
    _commit: bool = True,
) -> int:
    blob = orjson.dumps(pack)
    conn.execute(
        """
        INSERT OR REPLACE INTO rest_area_packs (rest_key, created_at, algo_version, pack_json)
        VALUES (?, ?, ?, ?);
        """,
        (rest_key, created_at, algo_version, blob),
    )
    if _commit:
        conn.commit()
    return len(blob)


def get_rest_area_pack(conn: sqlite3.Connection, rest_key: str) -> Optional[dict]:
    cur = conn.execute("SELECT pack_json FROM rest_area_packs WHERE rest_key=?;", (rest_key,))
    row = cur.fetchone()
    if not row:
        return None
    return orjson.loads(row[0])


def get_rest_area_pack_raw(conn: sqlite3.Connection, rest_key: str) -> Optional[bytes]:
    cur = conn.execute("SELECT pack_json FROM rest_area_packs WHERE rest_key=?;", (rest_key,))
    row = cur.fetchone()
    return bytes(row[0]) if row else None


def get_rest_area_pack_bytes(conn: sqlite3.Connection, rest_key: Optional[str]) -> int:
    if not rest_key:
        return 0
    cur = conn.execute("SELECT length(pack_json) FROM rest_area_packs WHERE rest_key=?;", (rest_key,))
    return _len_or_zero(cur.fetchone())


# ──────────────────────────────────────────────────────────────
# Cell towers (OpenCelliD bulk import)
# ──────────────────────────────────────────────────────────────

def get_cell_towers_meta(conn: sqlite3.Connection) -> Optional[Tuple[str, int]]:
    """Return (imported_at, row_count) or None if the table has never been loaded."""
    cur = conn.execute("SELECT imported_at, row_count FROM cell_towers_meta WHERE id=1;")
    row = cur.fetchone()
    if not row:
        return None
    return str(row[0]), int(row[1])


def set_cell_towers_meta(conn: sqlite3.Connection, *, imported_at: str, row_count: int, _commit: bool = True) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO cell_towers_meta (id, imported_at, row_count)
        VALUES (1, ?, ?);
        """,
        (imported_at, row_count),
    )
    if _commit:
        conn.commit()


def query_cell_towers_in_buckets(
    conn: sqlite3.Connection,
    lat_buckets: list,
    lon_buckets: list,
) -> list:
    """
    Return all rows whose (lat_bucket, lon_bucket) falls within the given sets.
    Each row is a dict with keys: radio, mnc, carrier_name, lat, lon, range_m, samples.
    """
    if not lat_buckets or not lon_buckets:
        return []

    lat_placeholders = ",".join("?" * len(lat_buckets))
    lon_placeholders = ",".join("?" * len(lon_buckets))
    sql = f"""
        SELECT radio, mnc, carrier_name, lat, lon, range_m, samples
        FROM cell_towers
        WHERE lat_bucket IN ({lat_placeholders})
          AND lon_bucket IN ({lon_placeholders});
    """
    cur = conn.execute(sql, lat_buckets + lon_buckets)
    cols = ("radio", "mnc", "carrier_name", "lat", "lon", "range_m", "samples")
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ──────────────────────────────────────────────────────────────
# Coverage packs
# ──────────────────────────────────────────────────────────────

def put_coverage_pack(
    conn: sqlite3.Connection,
    *,
    coverage_key: str,
    created_at: str,
    algo_version: str,
    pack: dict,
    _commit: bool = True,
) -> int:
    blob = orjson.dumps(pack)
    conn.execute(
        """
        INSERT OR REPLACE INTO coverage_packs (coverage_key, created_at, algo_version, pack_json)
        VALUES (?, ?, ?, ?);
        """,
        (coverage_key, created_at, algo_version, blob),
    )
    if _commit:
        conn.commit()
    return len(blob)


def get_coverage_pack(conn: sqlite3.Connection, coverage_key: str) -> Optional[dict]:
    cur = conn.execute("SELECT pack_json FROM coverage_packs WHERE coverage_key=?;", (coverage_key,))
    row = cur.fetchone()
    if not row:
        return None
    return orjson.loads(row[0])


def get_coverage_pack_raw(conn: sqlite3.Connection, coverage_key: str) -> Optional[bytes]:
    cur = conn.execute("SELECT pack_json FROM coverage_packs WHERE coverage_key=?;", (coverage_key,))
    row = cur.fetchone()
    return bytes(row[0]) if row else None


def get_coverage_pack_bytes(conn: sqlite3.Connection, coverage_key: Optional[str]) -> int:
    if not coverage_key:
        return 0
    cur = conn.execute("SELECT length(pack_json) FROM coverage_packs WHERE coverage_key=?;", (coverage_key,))
    return _len_or_zero(cur.fetchone())


# ──────────────────────────────────────────────────────────────
# Wildlife packs
# ──────────────────────────────────────────────────────────────

def put_wildlife_pack(
    conn: sqlite3.Connection,
    *,
    wildlife_key: str,
    created_at: str,
    algo_version: str,
    pack: dict,
    _commit: bool = True,
) -> int:
    blob = orjson.dumps(pack)
    conn.execute(
        """
        INSERT OR REPLACE INTO wildlife_packs (wildlife_key, created_at, algo_version, pack_json)
        VALUES (?, ?, ?, ?);
        """,
        (wildlife_key, created_at, algo_version, blob),
    )
    if _commit:
        conn.commit()
    return len(blob)


def get_wildlife_pack(conn: sqlite3.Connection, wildlife_key: str) -> Optional[dict]:
    cur = conn.execute("SELECT pack_json FROM wildlife_packs WHERE wildlife_key=?;", (wildlife_key,))
    row = cur.fetchone()
    if not row:
        return None
    return orjson.loads(row[0])


def get_wildlife_pack_raw(conn: sqlite3.Connection, wildlife_key: str) -> Optional[bytes]:
    cur = conn.execute("SELECT pack_json FROM wildlife_packs WHERE wildlife_key=?;", (wildlife_key,))
    row = cur.fetchone()
    return bytes(row[0]) if row else None


def get_wildlife_pack_bytes(conn: sqlite3.Connection, wildlife_key: Optional[str]) -> int:
    if not wildlife_key:
        return 0
    cur = conn.execute("SELECT length(pack_json) FROM wildlife_packs WHERE wildlife_key=?;", (wildlife_key,))
    return _len_or_zero(cur.fetchone())


# ──────────────────────────────────────────────────────────────
# Route score packs
# ──────────────────────────────────────────────────────────────

def put_score_pack(
    conn: sqlite3.Connection,
    *,
    score_key: str,
    created_at: str,
    algo_version: str,
    pack: dict,
    _commit: bool = True,
) -> int:
    blob = orjson.dumps(pack)
    conn.execute(
        """
        INSERT OR REPLACE INTO score_packs (score_key, created_at, algo_version, pack_json)
        VALUES (?, ?, ?, ?);
        """,
        (score_key, created_at, algo_version, blob),
    )
    if _commit:
        conn.commit()
    return len(blob)


def get_score_pack(conn: sqlite3.Connection, score_key: str) -> Optional[dict]:
    cur = conn.execute("SELECT pack_json FROM score_packs WHERE score_key=?;", (score_key,))
    row = cur.fetchone()
    if not row:
        return None
    return orjson.loads(row[0])


def get_score_pack_raw(conn: sqlite3.Connection, score_key: str) -> Optional[bytes]:
    cur = conn.execute("SELECT pack_json FROM score_packs WHERE score_key=?;", (score_key,))
    row = cur.fetchone()
    return bytes(row[0]) if row else None


def get_score_pack_bytes(conn: sqlite3.Connection, score_key: Optional[str]) -> int:
    if not score_key:
        return 0
    cur = conn.execute("SELECT length(pack_json) FROM score_packs WHERE score_key=?;", (score_key,))
    return _len_or_zero(cur.fetchone())


# ──────────────────────────────────────────────────────────────
# Geocode cache (Mapbox geocoding responses, TTL-based)
# ──────────────────────────────────────────────────────────────

def put_geocode_cache(
    conn: sqlite3.Connection,
    *,
    cache_key: str,
    created_at: str,
    pack: dict,
    _commit: bool = True,
) -> None:
    blob = orjson.dumps(pack)
    conn.execute(
        """
        INSERT OR REPLACE INTO geocode_cache (cache_key, created_at, pack_json)
        VALUES (?, ?, ?);
        """,
        (cache_key, created_at, blob),
    )
    if _commit:
        conn.commit()


def get_geocode_cache(conn: sqlite3.Connection, cache_key: str) -> Optional[dict]:
    cur = conn.execute("SELECT created_at, pack_json FROM geocode_cache WHERE cache_key=?;", (cache_key,))
    row = cur.fetchone()
    if not row:
        return None
    return {"created_at": str(row[0]), "pack": orjson.loads(row[1])}


# ──────────────────────────────────────────────────────────────
# Emergency services packs
# ──────────────────────────────────────────────────────────────

def put_emergency_pack(
    conn: sqlite3.Connection,
    *,
    emergency_key: str,
    created_at: str,
    algo_version: str,
    pack: dict,
    _commit: bool = True,
) -> int:
    blob = orjson.dumps(pack)
    conn.execute(
        """
        INSERT OR REPLACE INTO emergency_packs (emergency_key, created_at, algo_version, pack_json)
        VALUES (?, ?, ?, ?);
        """,
        (emergency_key, created_at, algo_version, blob),
    )
    if _commit:
        conn.commit()
    return len(blob)


def get_emergency_pack(conn: sqlite3.Connection, emergency_key: str) -> Optional[dict]:
    cur = conn.execute("SELECT pack_json FROM emergency_packs WHERE emergency_key=?;", (emergency_key,))
    row = cur.fetchone()
    if not row:
        return None
    return orjson.loads(row[0])


def get_emergency_pack_raw(conn: sqlite3.Connection, emergency_key: str) -> Optional[bytes]:
    cur = conn.execute("SELECT pack_json FROM emergency_packs WHERE emergency_key=?;", (emergency_key,))
    row = cur.fetchone()
    return bytes(row[0]) if row else None


def get_emergency_pack_bytes(conn: sqlite3.Connection, emergency_key: Optional[str]) -> int:
    if not emergency_key:
        return 0
    cur = conn.execute("SELECT length(pack_json) FROM emergency_packs WHERE emergency_key=?;", (emergency_key,))
    return _len_or_zero(cur.fetchone())


# ──────────────────────────────────────────────────────────────
# Heritage packs
# ──────────────────────────────────────────────────────────────

def put_heritage_pack(
    conn: sqlite3.Connection,
    *,
    heritage_key: str,
    created_at: str,
    algo_version: str,
    pack: dict,
    _commit: bool = True,
) -> int:
    blob = orjson.dumps(pack)
    conn.execute(
        """
        INSERT OR REPLACE INTO heritage_packs (heritage_key, created_at, algo_version, pack_json)
        VALUES (?, ?, ?, ?);
        """,
        (heritage_key, created_at, algo_version, blob),
    )
    if _commit:
        conn.commit()
    return len(blob)


def get_heritage_pack(conn: sqlite3.Connection, heritage_key: str) -> Optional[dict]:
    cur = conn.execute("SELECT pack_json FROM heritage_packs WHERE heritage_key=?;", (heritage_key,))
    row = cur.fetchone()
    if not row:
        return None
    return orjson.loads(row[0])


def get_heritage_pack_raw(conn: sqlite3.Connection, heritage_key: str) -> Optional[bytes]:
    cur = conn.execute("SELECT pack_json FROM heritage_packs WHERE heritage_key=?;", (heritage_key,))
    row = cur.fetchone()
    return bytes(row[0]) if row else None


def get_heritage_pack_bytes(conn: sqlite3.Connection, heritage_key: Optional[str]) -> int:
    if not heritage_key:
        return 0
    cur = conn.execute("SELECT length(pack_json) FROM heritage_packs WHERE heritage_key=?;", (heritage_key,))
    return _len_or_zero(cur.fetchone())


# ──────────────────────────────────────────────────────────────
# Air quality packs
# ──────────────────────────────────────────────────────────────

def put_aqi_pack(
    conn: sqlite3.Connection,
    *,
    aqi_key: str,
    created_at: str,
    algo_version: str,
    pack: dict,
    _commit: bool = True,
) -> int:
    blob = orjson.dumps(pack)
    conn.execute(
        """
        INSERT OR REPLACE INTO aqi_packs (aqi_key, created_at, algo_version, pack_json)
        VALUES (?, ?, ?, ?);
        """,
        (aqi_key, created_at, algo_version, blob),
    )
    if _commit:
        conn.commit()
    return len(blob)


def get_aqi_pack(conn: sqlite3.Connection, aqi_key: str) -> Optional[dict]:
    cur = conn.execute("SELECT pack_json FROM aqi_packs WHERE aqi_key=?;", (aqi_key,))
    row = cur.fetchone()
    if not row:
        return None
    return orjson.loads(row[0])


def get_aqi_pack_raw(conn: sqlite3.Connection, aqi_key: str) -> Optional[bytes]:
    cur = conn.execute("SELECT pack_json FROM aqi_packs WHERE aqi_key=?;", (aqi_key,))
    row = cur.fetchone()
    return bytes(row[0]) if row else None


def get_aqi_pack_bytes(conn: sqlite3.Connection, aqi_key: Optional[str]) -> int:
    if not aqi_key:
        return 0
    cur = conn.execute("SELECT length(pack_json) FROM aqi_packs WHERE aqi_key=?;", (aqi_key,))
    return _len_or_zero(cur.fetchone())


# ──────────────────────────────────────────────────────────────
# Bushfire packs
# ──────────────────────────────────────────────────────────────

def put_bushfire_pack(
    conn: sqlite3.Connection,
    *,
    bushfire_key: str,
    created_at: str,
    algo_version: str,
    pack: dict,
    _commit: bool = True,
) -> int:
    blob = orjson.dumps(pack)
    conn.execute(
        """
        INSERT OR REPLACE INTO bushfire_packs (bushfire_key, created_at, algo_version, pack_json)
        VALUES (?, ?, ?, ?);
        """,
        (bushfire_key, created_at, algo_version, blob),
    )
    if _commit:
        conn.commit()
    return len(blob)


def get_bushfire_pack(conn: sqlite3.Connection, bushfire_key: str) -> Optional[dict]:
    cur = conn.execute("SELECT pack_json FROM bushfire_packs WHERE bushfire_key=?;", (bushfire_key,))
    row = cur.fetchone()
    if not row:
        return None
    return orjson.loads(row[0])


def get_bushfire_pack_raw(conn: sqlite3.Connection, bushfire_key: str) -> Optional[bytes]:
    cur = conn.execute("SELECT pack_json FROM bushfire_packs WHERE bushfire_key=?;", (bushfire_key,))
    row = cur.fetchone()
    return bytes(row[0]) if row else None


def get_bushfire_pack_bytes(conn: sqlite3.Connection, bushfire_key: Optional[str]) -> int:
    if not bushfire_key:
        return 0
    cur = conn.execute("SELECT length(pack_json) FROM bushfire_packs WHERE bushfire_key=?;", (bushfire_key,))
    return _len_or_zero(cur.fetchone())


# ──────────────────────────────────────────────────────────────
# Speed cameras packs
# ──────────────────────────────────────────────────────────────

def put_cameras_pack(
    conn: sqlite3.Connection,
    *,
    cameras_key: str,
    created_at: str,
    algo_version: str,
    pack: dict,
    _commit: bool = True,
) -> int:
    blob = orjson.dumps(pack)
    conn.execute(
        """
        INSERT OR REPLACE INTO cameras_packs (cameras_key, created_at, algo_version, pack_json)
        VALUES (?, ?, ?, ?);
        """,
        (cameras_key, created_at, algo_version, blob),
    )
    if _commit:
        conn.commit()
    return len(blob)


def get_cameras_pack(conn: sqlite3.Connection, cameras_key: str) -> Optional[dict]:
    cur = conn.execute("SELECT pack_json FROM cameras_packs WHERE cameras_key=?;", (cameras_key,))
    row = cur.fetchone()
    if not row:
        return None
    return orjson.loads(row[0])


def get_cameras_pack_raw(conn: sqlite3.Connection, cameras_key: str) -> Optional[bytes]:
    cur = conn.execute("SELECT pack_json FROM cameras_packs WHERE cameras_key=?;", (cameras_key,))
    row = cur.fetchone()
    return bytes(row[0]) if row else None


def get_cameras_pack_bytes(conn: sqlite3.Connection, cameras_key: Optional[str]) -> int:
    if not cameras_key:
        return 0
    cur = conn.execute("SELECT length(pack_json) FROM cameras_packs WHERE cameras_key=?;", (cameras_key,))
    return _len_or_zero(cur.fetchone())


# ──────────────────────────────────────────────────────────────
# Toilets packs
# ──────────────────────────────────────────────────────────────

def put_toilets_pack(
    conn: sqlite3.Connection,
    *,
    toilets_key: str,
    created_at: str,
    algo_version: str,
    pack: dict,
    _commit: bool = True,
) -> int:
    blob = orjson.dumps(pack)
    conn.execute(
        """
        INSERT OR REPLACE INTO toilets_packs (toilets_key, created_at, algo_version, pack_json)
        VALUES (?, ?, ?, ?);
        """,
        (toilets_key, created_at, algo_version, blob),
    )
    if _commit:
        conn.commit()
    return len(blob)


def get_toilets_pack(conn: sqlite3.Connection, toilets_key: str) -> Optional[dict]:
    cur = conn.execute("SELECT pack_json FROM toilets_packs WHERE toilets_key=?;", (toilets_key,))
    row = cur.fetchone()
    if not row:
        return None
    return orjson.loads(row[0])


def get_toilets_pack_raw(conn: sqlite3.Connection, toilets_key: str) -> Optional[bytes]:
    cur = conn.execute("SELECT pack_json FROM toilets_packs WHERE toilets_key=?;", (toilets_key,))
    row = cur.fetchone()
    return bytes(row[0]) if row else None


def get_toilets_pack_bytes(conn: sqlite3.Connection, toilets_key: Optional[str]) -> int:
    if not toilets_key:
        return 0
    cur = conn.execute("SELECT length(pack_json) FROM toilets_packs WHERE toilets_key=?;", (toilets_key,))
    return _len_or_zero(cur.fetchone())


# ──────────────────────────────────────────────────────────────
# School zones packs
# ──────────────────────────────────────────────────────────────

def put_school_zones_pack(
    conn: sqlite3.Connection,
    *,
    school_zones_key: str,
    created_at: str,
    algo_version: str,
    pack: dict,
    _commit: bool = True,
) -> int:
    blob = orjson.dumps(pack)
    conn.execute(
        """
        INSERT OR REPLACE INTO school_zone_packs (school_zones_key, created_at, algo_version, pack_json)
        VALUES (?, ?, ?, ?);
        """,
        (school_zones_key, created_at, algo_version, blob),
    )
    if _commit:
        conn.commit()
    return len(blob)


def get_school_zones_pack(conn: sqlite3.Connection, school_zones_key: str) -> Optional[dict]:
    cur = conn.execute("SELECT pack_json FROM school_zone_packs WHERE school_zones_key=?;", (school_zones_key,))
    row = cur.fetchone()
    if not row:
        return None
    return orjson.loads(row[0])


def get_school_zones_pack_raw(conn: sqlite3.Connection, school_zones_key: str) -> Optional[bytes]:
    cur = conn.execute("SELECT pack_json FROM school_zone_packs WHERE school_zones_key=?;", (school_zones_key,))
    row = cur.fetchone()
    return bytes(row[0]) if row else None


def get_school_zones_pack_bytes(conn: sqlite3.Connection, school_zones_key: Optional[str]) -> int:
    if not school_zones_key:
        return 0
    cur = conn.execute("SELECT length(pack_json) FROM school_zone_packs WHERE school_zones_key=?;", (school_zones_key,))
    return _len_or_zero(cur.fetchone())


# ──────────────────────────────────────────────────────────────
# Roadkill packs
# ──────────────────────────────────────────────────────────────

def put_roadkill_pack(
    conn: sqlite3.Connection,
    *,
    roadkill_key: str,
    created_at: str,
    algo_version: str,
    pack: dict,
    _commit: bool = True,
) -> int:
    blob = orjson.dumps(pack)
    conn.execute(
        """
        INSERT OR REPLACE INTO roadkill_packs (roadkill_key, created_at, algo_version, pack_json)
        VALUES (?, ?, ?, ?);
        """,
        (roadkill_key, created_at, algo_version, blob),
    )
    if _commit:
        conn.commit()
    return len(blob)


def get_roadkill_pack(conn: sqlite3.Connection, roadkill_key: str) -> Optional[dict]:
    cur = conn.execute("SELECT pack_json FROM roadkill_packs WHERE roadkill_key=?;", (roadkill_key,))
    row = cur.fetchone()
    if not row:
        return None
    return orjson.loads(row[0])


def get_roadkill_pack_raw(conn: sqlite3.Connection, roadkill_key: str) -> Optional[bytes]:
    cur = conn.execute("SELECT pack_json FROM roadkill_packs WHERE roadkill_key=?;", (roadkill_key,))
    row = cur.fetchone()
    return bytes(row[0]) if row else None


def get_roadkill_pack_bytes(conn: sqlite3.Connection, roadkill_key: Optional[str]) -> int:
    if not roadkill_key:
        return 0
    cur = conn.execute("SELECT length(pack_json) FROM roadkill_packs WHERE roadkill_key=?;", (roadkill_key,))
    return _len_or_zero(cur.fetchone())


# ──────────────────────────────────────────────────────────────
# Presence (dead-reckoning proximity)
# ──────────────────────────────────────────────────────────────

def _bucket(v: float, step: float = 0.5) -> int:
    """Convert lat/lng to a spatial bucket (0.5° ≈ 55km at equator)."""
    import math
    return math.floor(v / step)


def upsert_presence(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    lat: float,
    lng: float,
    speed_kmh: float,
    heading_deg: float,
    pinged_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO presence (user_id, lat, lng, speed_kmh, heading_deg, pinged_at, lat_bucket, lng_bucket)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            lat=excluded.lat, lng=excluded.lng,
            speed_kmh=excluded.speed_kmh, heading_deg=excluded.heading_deg,
            pinged_at=excluded.pinged_at,
            lat_bucket=excluded.lat_bucket, lng_bucket=excluded.lng_bucket;
        """,
        (user_id, lat, lng, speed_kmh, heading_deg, pinged_at,
         _bucket(lat), _bucket(lng)),
    )
    conn.commit()


def get_nearby_presence(
    conn: sqlite3.Connection,
    *,
    lat: float,
    lng: float,
    exclude_user_id: str,
    max_age_hours: float = 4.0,
) -> List[dict]:
    """Fetch all non-expired presence rows within ±1 bucket (≈110km) of (lat, lng)."""
    from datetime import datetime, timedelta, timezone
    lb = _bucket(lat)
    lnb = _bucket(lng)
    threshold = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
    rows = conn.execute(
        """
        SELECT user_id, lat, lng, speed_kmh, heading_deg, pinged_at
        FROM presence
        WHERE lat_bucket BETWEEN ? AND ?
          AND lng_bucket BETWEEN ? AND ?
          AND user_id != ?
          AND pinged_at > ?;
        """,
        (lb - 1, lb + 1, lnb - 1, lnb + 1, exclude_user_id, threshold),
    ).fetchall()
    return [
        {"user_id": r[0], "lat": r[1], "lng": r[2],
         "speed_kmh": r[3], "heading_deg": r[4], "pinged_at": r[5]}
        for r in rows
    ]


# ──────────────────────────────────────────────────────────────
# User Observations (crowd-sourced road intel)
# ──────────────────────────────────────────────────────────────

def put_observation(
    conn: sqlite3.Connection,
    *,
    id: str,
    user_id: str,
    type: str,
    severity: str,
    lat: float,
    lng: float,
    heading_deg: Optional[float],
    message: Optional[str],
    value: Optional[str],
    created_at: str,
    expires_at: Optional[str],
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO user_observations
        (id, user_id, type, severity, lat, lng, heading_deg, message, value,
         created_at, expires_at, lat_bucket, lng_bucket)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (id, user_id, type, severity, lat, lng, heading_deg, message, value,
         created_at, expires_at, _bucket(lat), _bucket(lng)),
    )
    conn.commit()


def get_nearby_observations(
    conn: sqlite3.Connection,
    *,
    lat: float,
    lng: float,
    radius_buckets: int = 2,
    since_iso: Optional[str] = None,
    types: Optional[List[str]] = None,
) -> List[dict]:
    """Fetch non-expired observations within bucket range."""
    lb = _bucket(lat)
    lnb = _bucket(lng)

    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    sql = """
        SELECT id, user_id, type, severity, lat, lng, heading_deg,
               message, value, created_at, expires_at
        FROM user_observations
        WHERE lat_bucket BETWEEN ? AND ?
          AND lng_bucket BETWEEN ? AND ?
          AND (expires_at IS NULL OR expires_at > ?)
    """
    params: list = [lb - radius_buckets, lb + radius_buckets,
                    lnb - radius_buckets, lnb + radius_buckets, now_iso]

    if since_iso:
        sql += " AND created_at > ?"
        params.append(since_iso)

    if types:
        placeholders = ",".join("?" * len(types))
        sql += f" AND type IN ({placeholders})"
        params.extend(types)

    sql += " ORDER BY created_at DESC LIMIT 500;"
    rows = conn.execute(sql, params).fetchall()
    return [
        {"id": r[0], "user_id": r[1], "type": r[2], "severity": r[3],
         "lat": r[4], "lng": r[5], "heading_deg": r[6],
         "message": r[7], "value": r[8], "created_at": r[9], "expires_at": r[10]}
        for r in rows
    ]


