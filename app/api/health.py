from __future__ import annotations

import logging

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

router = APIRouter()
logger = logging.getLogger(__name__)


class HealthResponse(BaseModel):
    ok: bool


class ReadinessChecks(BaseModel):
    cache_db: str = Field(
        description="ok | not_initialised | error: <message>",
    )


class ReadinessResponse(BaseModel):
    ok: bool
    checks: ReadinessChecks


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """
    Liveness probe - always 200 while the process is up.
    Fly.io and load balancers use this to decide whether to route traffic.
    """
    return HealthResponse(ok=True)


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse}},
)
def ready(response: Response) -> ReadinessResponse:
    """
    Readiness probe - checks that the cache DB is queryable.
    Returns 503 if the DB is not ready so Fly.io stops routing traffic.
    """
    from app.main import _cache_conn_ref  # populated by lifespan startup

    ok = True
    cache_db_status = "ok"

    conn = _cache_conn_ref()
    if conn is None:
        cache_db_status = "not_initialised"
        ok = False
    else:
        try:
            conn.execute("SELECT 1")
        except Exception as exc:
            logger.warning("readiness check: cache_db error: %s", exc)
            cache_db_status = f"error: {exc}"
            ok = False

    if not ok:
        response.status_code = 503

    return ReadinessResponse(ok=ok, checks=ReadinessChecks(cache_db=cache_db_status))
