"""Liveness and readiness probes.

The distinction matters for orchestration (Kubernetes/ECS): ``/health`` answers
"is the process up?" and must never depend on external systems, or a transient
Redis blip would get the container killed. ``/ready`` answers "can this instance
serve traffic?" and *does* check dependencies, so a not-ready instance is pulled
from the load balancer without being restarted.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_redis_client

router = APIRouter(tags=["health"])


@router.get("/metrics", summary="Prometheus metrics", include_in_schema=False)
async def metrics() -> Response:
    """Expose all registered Prometheus metrics in the standard text exposition format.

    Placed here alongside the other operational endpoints (health/ready) rather
    than in a separate module because all three are infrastructure probes, not
    application API. The health router is registered before the catch-all
    ``GET /{short_code}`` route, so this path resolves correctly.
    """
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.get("/health", summary="Liveness probe")
async def health() -> dict[str, str]:
    """Return 200 if the process is alive. No dependency checks by design."""
    return {"status": "ok"}


@router.get("/ready", summary="Readiness probe")
async def ready(
    response: Response,
    session: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis_client)],
) -> dict[str, object]:
    """Check Redis and Postgres connectivity.

    Returns 200 only if both dependencies respond. On failure, returns 503 and
    names which dependency (or dependencies) failed, so an operator sees the
    cause without digging through logs. We catch broadly here *on purpose*: any
    connectivity error means "not ready", and we still want to report the state
    of the other dependency rather than let one failure mask the check.
    """
    checks: dict[str, str] = {}
    healthy = True

    try:
        await redis.ping()
        checks["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001 - readiness must report, not raise
        checks["redis"] = f"error: {exc.__class__.__name__}"
        healthy = False

    try:
        await session.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:  # noqa: BLE001 - readiness must report, not raise
        checks["postgres"] = f"error: {exc.__class__.__name__}"
        healthy = False

    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {"status": "ready" if healthy else "not_ready", "checks": checks}
