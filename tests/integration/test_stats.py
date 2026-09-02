"""Integration tests for GET /api/urls/{short_code}/stats.

Focuses on the top_referrers field, which was silently broken: the SQLAlchemy
label "count" collides with the inherited tuple.count method on Row, causing
row.count to return a bound method object instead of the aggregate integer.
"""

from __future__ import annotations

import asyncio

from httpx import AsyncClient

from app.db.database import SessionFactory
from app.db.repository import get_recent_clicks


async def _wait_for_clicks(short_code: str, minimum: int, timeout: float = 3.0) -> None:
    """Poll until at least ``minimum`` click events have been persisted.

    Clicks are written by a background task after the response is sent, so a
    brief poll is needed before asserting on the recorded data.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        async with SessionFactory() as session:
            clicks = await get_recent_clicks(session, short_code, limit=minimum + 10)
        if len(clicks) >= minimum or asyncio.get_event_loop().time() >= deadline:
            return
        await asyncio.sleep(0.05)


async def test_top_referrers_counts_are_integers_matching_real_clicks(
    client: AsyncClient,
) -> None:
    """top_referrers counts must be integers equal to the real per-referrer click counts.

    Regression test for the row.count label-collision bug: SQLAlchemy's Row
    inherits .count from tuple, so func.count().label("count") causes
    row.count to return the bound tuple method instead of the aggregate integer.
    The fix renames the label to "click_count" and accesses row.click_count.

    This test would have caught the original bug because isinstance(method, int)
    is False and the per-referrer totals would not match the expected values.
    """
    created = await client.post("/api/urls", json={"long_url": "https://example.com/target"})
    assert created.status_code == 201
    short_code = created.json()["short_code"]

    # Drive three distinct referrers with known click counts: 3, 2, 1.
    for _ in range(3):
        await client.get(f"/{short_code}", headers={"referer": "https://a.example"})
    for _ in range(2):
        await client.get(f"/{short_code}", headers={"referer": "https://b.example"})
    await client.get(f"/{short_code}", headers={"referer": "https://c.example"})

    # Wait for all six background click-recording tasks to commit.
    await _wait_for_clicks(short_code, minimum=6)

    response = await client.get(f"/api/urls/{short_code}/stats")
    assert response.status_code == 200

    body = response.json()
    referrers = body["top_referrers"]

    # Every count field must be an integer — not a method object, not a string.
    for entry in referrers:
        assert isinstance(entry["count"], int), (
            f"top_referrers count for {entry['referrer']!r} is "
            f"{type(entry['count']).__name__!r} ({entry['count']!r}), expected int. "
            "This indicates the row.count label-collision bug has regressed."
        )

    # Counts must match the real per-referrer totals.
    counts_by_referrer = {entry["referrer"]: entry["count"] for entry in referrers}
    assert counts_by_referrer.get("https://a.example") == 3
    assert counts_by_referrer.get("https://b.example") == 2
    assert counts_by_referrer.get("https://c.example") == 1

    # Results must be ordered highest-count first (as the query specifies).
    returned_counts = [entry["count"] for entry in referrers]
    assert returned_counts == sorted(returned_counts, reverse=True)


async def test_stats_returns_404_for_unknown_code(client: AsyncClient) -> None:
    """Stats endpoint returns 404 for a code that was never created."""
    response = await client.get("/api/urls/doesnotexist/stats")
    assert response.status_code == 404


async def test_stats_click_count_matches_redirect_count(client: AsyncClient) -> None:
    """The top-level click_count field matches the number of redirects made."""
    created = await client.post("/api/urls", json={"long_url": "https://example.com/ct"})
    assert created.status_code == 201
    short_code = created.json()["short_code"]

    for _ in range(4):
        await client.get(f"/{short_code}")

    await _wait_for_clicks(short_code, minimum=4)

    response = await client.get(f"/api/urls/{short_code}/stats")
    assert response.status_code == 200
    assert response.json()["click_count"] == 4
