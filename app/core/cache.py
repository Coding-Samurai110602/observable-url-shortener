"""Cache-aside layer for ``short_code -> long_url`` lookups.

The redirect hot path reads this cache first and only falls through to Postgres
on a miss, then back-fills the cache. Keeping the caching concern in one small
class (rather than scattering ``redis.get``/``set`` calls across routes) means
the key scheme, TTL policy, and (later) metric instrumentation live in exactly
one place.

TTL policy: entries expire after ``Settings.cache_ttl_seconds`` and the TTL is
*refreshed on read* (sliding expiration) so that hot links stay cached while
cold ones age out. We use ``GETEX`` so the read-and-refresh is a single atomic
Redis command rather than a ``GET`` followed by a separate ``EXPIRE``.
"""

from __future__ import annotations

import time

import structlog
from redis.asyncio import Redis

from app.config import Settings
from app.metrics import (
    cache_hit_total,
    cache_miss_total,
    redis_operation_duration_seconds,
)

_log = structlog.get_logger(__name__)


class UrlCache:
    """Cache-aside accessor for short-code -> long-URL mappings.

    Implemented as a class (rather than module-level functions) so the Redis
    client and settings are injected once and reused, keeping it testable and
    free of module-global state.
    """

    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings

    @staticmethod
    def _key(short_code: str) -> str:
        # Namespaced so cache entries never collide with rate-limit keys.
        return f"cache:url:{short_code}"

    async def get_cached_url(self, short_code: str) -> str | None:
        """Return the cached long URL for ``short_code``, or ``None`` on miss.

        On a hit, the entry's TTL is refreshed (sliding expiration) via
        ``GETEX`` so frequently-accessed links remain cached.
        """
        _t0 = time.perf_counter()
        value = await self._redis.getex(
            self._key(short_code),
            ex=self._settings.cache_ttl_seconds,
        )
        redis_operation_duration_seconds.labels(operation="get").observe(
            time.perf_counter() - _t0
        )
        # Route label is hardcoded to the redirect template because get_cached_url
        # is only called from GET /{short_code}; passing route as a parameter
        # would require a verified-file signature change that isn't warranted here.
        if value is None:
            cache_miss_total.labels(route="/{short_code}").inc()
            _log.debug("cache.miss", short_code=short_code)
            return None
        cache_hit_total.labels(route="/{short_code}").inc()
        _log.debug("cache.hit", short_code=short_code)
        return value.decode() if isinstance(value, bytes) else value

    async def set_cached_url(
        self,
        short_code: str,
        long_url: str,
        ttl_seconds: int | None = None,
    ) -> None:
        """Populate the cache for ``short_code`` with a TTL.

        Args:
            short_code: The short code key.
            long_url: The destination URL to cache.
            ttl_seconds: Override TTL; defaults to ``Settings.cache_ttl_seconds``.
        """
        ttl = ttl_seconds if ttl_seconds is not None else self._settings.cache_ttl_seconds
        _t0 = time.perf_counter()
        await self._redis.set(self._key(short_code), long_url, ex=ttl)
        redis_operation_duration_seconds.labels(operation="set").observe(
            time.perf_counter() - _t0
        )
        # No separate cache_fill counter — the spec defines only hit/miss counters;
        # the set operation is captured in redis_operation_duration_seconds.

    async def invalidate_cached_url(self, short_code: str) -> None:
        """Explicitly evict ``short_code`` from the cache (delete/expiry path)."""
        _t0 = time.perf_counter()
        await self._redis.delete(self._key(short_code))
        redis_operation_duration_seconds.labels(operation="delete").observe(
            time.perf_counter() - _t0
        )
