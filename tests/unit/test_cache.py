"""Unit tests for UrlCache fault handling under Redis connection failure.

These tests verify the fail-safe policy documented in POSTMORTEM-001:
- get_cached_url returns None (cache miss) instead of raising on Redis failure.
- set_cached_url and invalidate_cached_url swallow the error instead of raising.

In all three cases the Redis client is replaced with a MagicMock whose async
methods raise ConnectionError, simulating a hard outage. No fakeredis is needed
here because we are testing the exception-handling paths, not Redis semantics.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from redis.exceptions import ConnectionError as RedisConnectionError

from app.config import Settings
from app.core.cache import UrlCache


def _make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "database_url": "postgresql+asyncpg://u:p@localhost:5432/test",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _cache_with_failing_redis() -> UrlCache:
    """Return a UrlCache whose underlying Redis client raises on every call."""
    redis = MagicMock()
    redis.getex = AsyncMock(side_effect=RedisConnectionError("Connection refused"))
    redis.set = AsyncMock(side_effect=RedisConnectionError("Connection refused"))
    redis.delete = AsyncMock(side_effect=RedisConnectionError("Connection refused"))
    return UrlCache(redis, _make_settings())


async def test_get_cached_url_returns_none_on_redis_failure() -> None:
    """A Redis outage on GETEX is treated as a cache miss — caller falls to Postgres."""
    cache = _cache_with_failing_redis()
    result = await cache.get_cached_url("abc123")
    assert result is None


async def test_set_cached_url_does_not_raise_on_redis_failure() -> None:
    """A Redis outage on SET is swallowed — the redirect that already completed is safe."""
    cache = _cache_with_failing_redis()
    await cache.set_cached_url("abc123", "https://example.com")


async def test_invalidate_cached_url_does_not_raise_on_redis_failure() -> None:
    """A Redis outage on DELETE is swallowed — stale entry expires via TTL instead."""
    cache = _cache_with_failing_redis()
    await cache.invalidate_cached_url("abc123")
