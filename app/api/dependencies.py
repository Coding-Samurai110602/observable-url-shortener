"""FastAPI dependencies: shared clients, client identification, rate limiting.

Everything a route needs from infrastructure is wired here via ``Depends`` so
handlers declare *what* they need, not *how* it's built. Shared clients (Redis,
rate limiter, cache) are constructed once and reused across requests — a new
Redis connection pool per request would be wasteful and defeat pooling.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import AsyncGenerator
from functools import lru_cache
from typing import Annotated

import structlog
from fastapi import Depends, HTTPException, Request, status
from fastapi import params as fastapi_params
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.core.cache import UrlCache
from app.core.rate_limiter import RateLimiter, RouteClass
from app.db.database import get_db_session
from app.metrics import rate_limit_rejections_total

_log = structlog.get_logger(__name__)


@lru_cache
def get_redis_client() -> Redis:
    """Return the process-wide async Redis client (single connection pool).

    ``decode_responses=True`` so cached URLs and Lua replies come back as ``str``
    rather than ``bytes``, keeping call sites clean. Cached so every dependent
    shares one pool.
    """
    settings = get_settings()
    return Redis.from_url(str(settings.redis_url), decode_responses=True)


@lru_cache
def get_rate_limiter() -> RateLimiter:
    """Return the shared rate limiter (registers its Lua script once)."""
    return RateLimiter(redis=get_redis_client(), settings=get_settings())


@lru_cache
def get_url_cache() -> UrlCache:
    """Return the shared cache-aside accessor."""
    return UrlCache(redis=get_redis_client(), settings=get_settings())


async def get_db(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> AsyncGenerator[AsyncSession, None]:
    """Re-export the DB session dependency under the API layer's namespace.

    Thin passthrough so routes depend on ``app.api.dependencies`` uniformly
    rather than reaching into ``app.db.database`` directly.
    """
    yield session


def get_client_identifier(request: Request) -> str:
    """Derive a stable client identifier for rate limiting.

    Prefers the first hop in ``X-Forwarded-For`` because in deployment the app
    sits behind a proxy/load balancer and ``request.client.host`` would be the
    proxy's address (collapsing every client into one bucket). Falls back to the
    socket peer address, which is correct for direct/local connections.

    Note: ``X-Forwarded-For`` is client-spoofable if the edge proxy doesn't
    overwrite it; hardening that is a deployment concern (trust only the known
    proxy), not something this function can decide.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # Format: "client, proxy1, proxy2" — the left-most entry is the origin.
        return forwarded.split(",")[0].strip()
    if request.client is not None:
        return request.client.host
    return "unknown"


def hash_client_ip(ip: str, salt: str) -> str:
    """Return a salted SHA-256 hex digest of a client IP.

    We store this (never the raw IP) on ``click_events`` so per-client analytics
    are possible without retaining PII. The salt prevents trivial reversal of
    the small IPv4 space via precomputed tables.
    """
    return hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()


def rate_limit(route_class: RouteClass) -> fastapi_params.Depends:
    """Build a ``Depends`` that enforces the bucket for ``route_class``.

    Returns a dependency callable (not the result of calling one) so routes can
    write ``dependencies=[rate_limit("redirect")]``. On rejection it raises
    ``429`` with a ``Retry-After`` header derived from the bucket's own estimate
    of when the next token frees up.
    """

    async def _dependency(
        client_id: Annotated[str, Depends(get_client_identifier)],
        limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
    ) -> None:
        result = await limiter.check(client_id, route_class)
        if not result.allowed:
            rate_limit_rejections_total.labels(route_class=route_class).inc()
            _log.warning(
                "rate_limit.rejected",
                route_class=route_class,
                retry_after_seconds=result.retry_after_seconds,
                tokens_remaining=result.tokens_remaining,
            )
            # Retry-After is defined in whole seconds; round up (and floor at 1)
            # so we never tell the client to retry before a token is available.
            retry_after = result.retry_after_seconds or 0.0
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded. Slow down and retry later.",
                headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
            )

    return Depends(_dependency)
