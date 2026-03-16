# app/api/peer_sync.py
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.contracts import PeerSyncDelta, PeerSyncRequest
from app.services.peer_sync import PeerSync

router = APIRouter(prefix="/peer")


def get_cache_conn():
    raise RuntimeError("cache conn must be provided by app dependency override")


def get_peer_sync_service(cache_conn=Depends(get_cache_conn)) -> PeerSync:
    return PeerSync(conn=cache_conn)


@router.post("/sync", response_model=PeerSyncDelta)
def peer_sync(
    req: PeerSyncRequest,
    svc: PeerSync = Depends(get_peer_sync_service),
) -> PeerSyncDelta:
    """
    Build a delta of overlay data newer than the caller's timestamps.
    Used when roamers exchange data via BLE or when the app regains signal.
    """
    return svc.build_delta(
        lat=req.lat,
        lng=req.lng,
        radius_km=req.radius_km,
        overlay_timestamps=req.overlay_timestamps,
    )
