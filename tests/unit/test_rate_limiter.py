"""Unit tests for the atomic token-bucket rate limiter.

Redis backend choice
--------------------
These tests use ``fakeredis.aioredis`` rather than a live Redis. That keeps them
hermetic (no service dependency, deterministic) *without weakening the property
under test*: fakeredis executes a Lua script to completion in a single
synchronous step, exactly as a real Redis serves ``EVAL`` single-threaded. So
the concurrency test below still fails against a non-atomic implementation
(e.g. an ``await hget`` ... compute ... ``await hset`` version), because those
awaits would let ``asyncio.gather`` interleave the read-modify-write and
over-admit. Time is injected via the limiter's ``clock`` parameter so refill is
controlled precisely instead of via ``sleep``.
"""

from __future__ import annotations

import asyncio

import fakeredis.aioredis
import pytest

from app.config import Settings
from app.core.rate_limiter import RateLimiter


def _make_settings(**overrides: object) -> Settings:
    """Build a Settings instance for tests without reading a real ``.env``.

    ``_env_file=None`` isolates the test from any local ``.env``; ``database_url``
    is required by the model but unused by the rate limiter.
    """
    base: dict[str, object] = {
        "_env_file": None,
        "database_url": "postgresql+asyncpg://u:p@localhost:5432/test",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _fake_redis() -> fakeredis.aioredis.FakeRedis:
    """Fresh in-process Redis with string decoding, matching production client."""
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


async def test_concurrent_requests_admit_exactly_capacity() -> None:
    """N concurrent checks against a K-token bucket admit exactly K.

    This is the test that would catch a non-atomic implementation: with the
    clock frozen (no refill), the bucket can serve exactly its capacity, and a
    correct atomic limiter admits precisely that many even when all requests
    race via ``asyncio.gather``.
    """
    capacity = 10
    concurrent_requests = 50
    settings = _make_settings(
        rate_limit_redirect_capacity=capacity,
        rate_limit_redirect_refill_per_minute=60,  # 1/sec, but clock is frozen
    )
    # Frozen clock => elapsed is always 0 => refill contributes nothing.
    limiter = RateLimiter(_fake_redis(), settings, clock=lambda: 1_000.0)

    results = await asyncio.gather(
        *(limiter.check("client-a", "redirect") for _ in range(concurrent_requests))
    )

    admitted = sum(1 for result in results if result.allowed)
    assert admitted == capacity


async def test_boundary_kth_allowed_and_next_rejected() -> None:
    """The Kth request is allowed and the (K+1)th is rejected with a wait hint."""
    capacity = 3
    settings = _make_settings(
        rate_limit_redirect_capacity=capacity,
        rate_limit_redirect_refill_per_minute=1,  # negligible; clock frozen anyway
    )
    limiter = RateLimiter(_fake_redis(), settings, clock=lambda: 1_000.0)

    for _ in range(capacity):
        result = await limiter.check("client-b", "redirect")
        assert result.allowed

    rejected = await limiter.check("client-b", "redirect")
    assert rejected.allowed is False
    assert rejected.retry_after_seconds is not None
    assert rejected.retry_after_seconds > 0


async def test_tokens_refill_over_time() -> None:
    """After exhausting the bucket, advancing the clock frees up tokens again."""
    capacity = 5
    settings = _make_settings(
        rate_limit_create_capacity=capacity,
        rate_limit_create_refill_per_minute=60,  # 1 token per second
    )
    now = {"t": 1_000.0}
    limiter = RateLimiter(_fake_redis(), settings, clock=lambda: now["t"])

    # Drain the full bucket.
    for _ in range(capacity):
        assert (await limiter.check("client-c", "create")).allowed
    assert (await limiter.check("client-c", "create")).allowed is False

    # Advance 3 seconds => 3 tokens should have refilled.
    now["t"] += 3.0
    refilled = [(await limiter.check("client-c", "create")).allowed for _ in range(3)]
    assert refilled == [True, True, True]

    # The 4th after refill exceeds what accrued, so it's rejected again.
    assert (await limiter.check("client-c", "create")).allowed is False


async def test_buckets_are_isolated_per_client_and_route_class() -> None:
    """Draining one client's bucket must not affect another client or route class."""
    settings = _make_settings(
        rate_limit_redirect_capacity=2,
        rate_limit_redirect_refill_per_minute=1,
        rate_limit_create_capacity=2,
        rate_limit_create_refill_per_minute=1,
    )
    limiter = RateLimiter(_fake_redis(), settings, clock=lambda: 1_000.0)

    # Drain client-a's redirect bucket.
    assert (await limiter.check("client-a", "redirect")).allowed
    assert (await limiter.check("client-a", "redirect")).allowed
    assert (await limiter.check("client-a", "redirect")).allowed is False

    # A different client is unaffected.
    assert (await limiter.check("client-b", "redirect")).allowed
    # The same client's *other* route class is a separate bucket.
    assert (await limiter.check("client-a", "create")).allowed


async def test_unknown_route_class_raises() -> None:
    """A programming error (bad route class) surfaces as ValueError, not silently."""
    limiter = RateLimiter(_fake_redis(), _make_settings(), clock=lambda: 1_000.0)
    with pytest.raises(ValueError):
        await limiter.check("client-a", "does-not-exist")  # type: ignore[arg-type]
