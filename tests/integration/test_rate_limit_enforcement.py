"""Integration test: the redirect route enforces its rate limit end-to-end.

Unlike the unit tests (which drive ``RateLimiter`` directly with an injected
clock), this drives the limit through the real HTTP stack: dependency wiring,
the 429 mapping, and the ``Retry-After`` header. The expected cutoff is derived
from configuration, not hardcoded, so it stays correct if limits change.
"""

from __future__ import annotations

from httpx import AsyncClient

from app.config import get_settings


async def test_redirect_rate_limit_returns_429_with_retry_after(client: AsyncClient) -> None:
    settings = get_settings()
    capacity = settings.rate_limit_redirect_capacity

    created = await client.post("/api/urls", json={"long_url": "https://example.com/rl"})
    short_code = created.json()["short_code"]

    # Fire beyond capacity. Redis was flushed by the autouse fixture, so the
    # bucket starts full at exactly `capacity` tokens.
    total_requests = capacity + 15
    statuses: list[int] = []
    first_429_index: int | None = None
    first_429_retry_after: str | None = None

    for index in range(total_requests):
        response = await client.get(f"/{short_code}")
        statuses.append(response.status_code)
        if response.status_code == 429 and first_429_index is None:
            first_429_index = index
            # Header names are case-insensitive in httpx's Headers mapping.
            first_429_retry_after = response.headers.get("retry-after")

    # A 429 must have appeared...
    assert 429 in statuses
    assert first_429_index is not None
    # ...no earlier than the configured capacity (refill during the short burst
    # is negligible, so the cutoff should be right around `capacity`).
    assert first_429_index >= capacity
    # ...and it must carry a positive Retry-After hint.
    assert first_429_retry_after is not None
    assert int(first_429_retry_after) >= 1

    successful = sum(1 for status_code in statuses if status_code == 302)
    assert capacity <= successful <= capacity + 5
