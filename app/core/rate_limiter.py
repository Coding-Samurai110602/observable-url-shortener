"""Atomic token-bucket rate limiter backed by a single Redis Lua script.

Why a Lua script and not GET-then-SET
-------------------------------------
A token bucket has read-modify-write semantics: read current tokens, refill
based on elapsed time, decide, then write the new count. If those steps are
separate round-trips (``GET`` ... compute ... ``SET``), two concurrent requests
can both read the same "1 token left", both decide "allowed", and both write
back — so the bucket serves *two* requests when it should have served one. That
is a lost-update race, and under real concurrency it silently lets clients
exceed their limit.

Executing the whole read-modify-write inside one ``EVAL`` makes it atomic:
Redis runs the script to completion single-threaded, with no interleaving from
other clients. This is the property the concurrency test in
``tests/unit/test_rate_limiter.py`` exercises, and the reason this file is the
correctness centerpiece of the project.

State model
-----------
Per ``(client, route_class)`` we keep a Redis hash with two fields:

* ``tokens`` — fractional token count remaining.
* ``ts``     — timestamp (ms) of the last refill, so we can replenish
  continuously rather than in fixed windows.

The key is given a TTL equal to the time needed to refill from empty to full,
so an idle client's key simply expires; an absent key is treated as a full
bucket, which is the correct default. This keeps Redis memory bounded without a
sweeper.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import Settings
from app.metrics import redis_fallback_total, redis_operation_duration_seconds

_log = structlog.get_logger(__name__)

# Route classes get different bucket sizes/refill rates (redirects are cheap and
# high-volume; creates are expensive and low-volume).
RouteClass = Literal["redirect", "create"]

# The atomic token-bucket script.
#
# KEYS[1] = bucket key
# ARGV[1] = capacity (max tokens / burst size)
# ARGV[2] = refill_per_minute (sustained rate)
# ARGV[3] = now in milliseconds
# ARGV[4] = tokens requested (always 1 here, but parameterized for clarity)
#
# Returns: { allowed (1|0), retry_after_ms (int), tokens_remaining (string) }
# tokens_remaining is returned as a string because Redis truncates Lua numbers
# to integers on the way out, and we want to preserve the fractional count for
# observability/debugging.
_LUA_TOKEN_BUCKET = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_per_minute = tonumber(ARGV[2])
local now_ms = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])

-- Tokens added per millisecond of elapsed time.
local rate_per_ms = refill_per_minute / 60000.0

local state = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(state[1])
local last_ts = tonumber(state[2])

-- Cold bucket (absent key or expired): start full.
if tokens == nil or last_ts == nil then
    tokens = capacity
    last_ts = now_ms
end

-- Continuous refill, clamped to capacity. Guard against clock skew going
-- backwards (elapsed < 0) so we never subtract tokens.
local elapsed = now_ms - last_ts
if elapsed < 0 then
    elapsed = 0
end
tokens = math.min(capacity, tokens + elapsed * rate_per_ms)

local allowed = 0
local retry_after_ms = 0
if tokens >= requested then
    allowed = 1
    tokens = tokens - requested
else
    -- How long until enough tokens accumulate for this request.
    local deficit = requested - tokens
    if rate_per_ms > 0 then
        retry_after_ms = math.ceil(deficit / rate_per_ms)
    else
        retry_after_ms = -1  -- unreachable given config validation (refill > 0)
    end
end

redis.call('HSET', key, 'tokens', tokens, 'ts', now_ms)

-- Expire the key once it would be fully refilled from empty; an absent key is
-- treated as a full bucket, so this loses no state.
local ttl_ms = capacity / rate_per_ms
redis.call('PEXPIRE', key, math.ceil(ttl_ms) + 1000)

return { allowed, retry_after_ms, tostring(tokens) }
"""


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    """Outcome of a single rate-limit check.

    Attributes:
        allowed: Whether the request may proceed.
        retry_after_seconds: When rejected, how long the caller should wait
            before the next token is available (suitable for a ``Retry-After``
            header). ``None`` when ``allowed`` is ``True``.
        tokens_remaining: Fractional tokens left in the bucket after this check;
            useful for logging/metrics.
    """

    allowed: bool
    retry_after_seconds: float | None
    tokens_remaining: float


class RateLimiter:
    """Async token-bucket rate limiter using one atomic Lua script per check.

    A single instance is meant to be created once and shared (the Lua script is
    registered against the connection once via ``register_script``). Capacity
    and refill rates are read from :class:`Settings`, keyed by route class, so
    limits are configurable and never hardcoded here.
    """

    def __init__(
        self,
        redis: Redis,
        settings: Settings,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Initialize the limiter.

        Args:
            redis: Async Redis client (shared connection pool).
            settings: Application settings providing per-route-class limits.
            clock: Wall-clock source returning seconds since the epoch. Injected
                so tests can advance time deterministically without sleeping.
        """
        self._redis = redis
        self._settings = settings
        self._clock = clock
        # Registering the script lets redis-py use EVALSHA (with EVAL fallback),
        # avoiding re-sending the script body on every call. The returned object
        # is callable and awaitable when bound to an async client.
        self._script = redis.register_script(_LUA_TOKEN_BUCKET)

    def _limits_for(self, route_class: RouteClass) -> tuple[int, int]:
        """Return ``(capacity, refill_per_minute)`` for a route class."""
        if route_class == "redirect":
            return (
                self._settings.rate_limit_redirect_capacity,
                self._settings.rate_limit_redirect_refill_per_minute,
            )
        if route_class == "create":
            return (
                self._settings.rate_limit_create_capacity,
                self._settings.rate_limit_create_refill_per_minute,
            )
        raise ValueError(f"Unknown route class: {route_class!r}")

    @staticmethod
    def _key(client_id: str, route_class: RouteClass) -> str:
        return f"ratelimit:{client_id}:{route_class}"

    async def check(self, client_id: str, route_class: RouteClass) -> RateLimitResult:
        """Atomically consume one token for ``client_id`` on ``route_class``.

        Args:
            client_id: Stable per-client identifier (typically the client IP).
            route_class: Which bucket to draw from (``"redirect"`` or
                ``"create"``).

        Returns:
            A :class:`RateLimitResult` describing whether the request is allowed
            and, if not, how long to wait.

        Raises:
            ValueError: If ``route_class`` is not a recognized class.
        """
        capacity, refill_per_minute = self._limits_for(route_class)
        key = self._key(client_id, route_class)
        now_ms = int(self._clock() * 1000)

        # One await == one atomic server-side execution. This is the whole point.
        _t0 = time.perf_counter()
        try:
            raw = await self._script(
                keys=[key],
                args=[capacity, refill_per_minute, now_ms, 1],
            )
            redis_operation_duration_seconds.labels(operation="token_bucket").observe(
                time.perf_counter() - _t0
            )
        except RedisError as exc:
            # Mirror of hash_client_ip() in app/api/dependencies.py — inlined to
            # avoid a circular import (dependencies.py imports from this module).
            client_id_hash = hashlib.sha256(
                f"{self._settings.ip_hash_salt}:{client_id}".encode()
            ).hexdigest()
            _log.warning(
                "rate_limiter.redis_unavailable",
                route_class=route_class,
                client_id_hash=client_id_hash,
                error=str(exc),
            )
            redis_fallback_total.labels(
                component="rate_limiter", operation="token_bucket"
            ).inc()
            # Fail open: allow the request through rather than returning 500.
            # tokens_remaining=nan is a deliberate sentinel — not a valid token
            # count, distinguishable from any real result, and causes callers that
            # do numeric comparisons to behave safely (nan < x is always False).
            return RateLimitResult(
                allowed=True,
                retry_after_seconds=None,
                tokens_remaining=float("nan"),
            )

        allowed = bool(int(raw[0]))
        retry_after_ms = int(raw[1])
        tokens_remaining = float(_as_str(raw[2]))

        return RateLimitResult(
            allowed=allowed,
            retry_after_seconds=None if allowed else max(retry_after_ms, 0) / 1000.0,
            tokens_remaining=tokens_remaining,
        )


def _as_str(value: str | bytes) -> str:
    """Coerce a Redis reply element to ``str`` regardless of ``decode_responses``."""
    return value.decode() if isinstance(value, bytes) else value
