"""Integration tests for the redirect flow (``GET /{short_code}``)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient

from app.db.database import SessionFactory
from app.db.models import Url
from app.db.repository import get_recent_clicks


async def _wait_for_clicks(short_code: str, minimum: int, timeout: float = 3.0) -> int:
    """Poll for background-recorded click events.

    Clicks are written by a background task after the response is sent, so we
    poll briefly rather than assume immediate visibility.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        async with SessionFactory() as session:
            clicks = await get_recent_clicks(session, short_code, limit=50)
        if len(clicks) >= minimum or asyncio.get_event_loop().time() >= deadline:
            return len(clicks)
        await asyncio.sleep(0.05)


async def test_redirect_success_returns_302_with_location(client: AsyncClient) -> None:
    created = await client.post("/api/urls", json={"long_url": "https://example.com/dest"})
    short_code = created.json()["short_code"]

    response = await client.get(f"/{short_code}")

    assert response.status_code == 302
    assert response.headers["location"].startswith("https://example.com/dest")


async def test_redirect_records_a_click_event(client: AsyncClient) -> None:
    created = await client.post("/api/urls", json={"long_url": "https://example.com/dest"})
    short_code = created.json()["short_code"]

    await client.get(f"/{short_code}", headers={"referer": "https://news.example"})

    recorded = await _wait_for_clicks(short_code, minimum=1)
    assert recorded >= 1


async def test_redirect_unknown_code_returns_404(client: AsyncClient) -> None:
    response = await client.get("/doesnotexist")
    assert response.status_code == 404


async def test_redirect_expired_code_returns_404(client: AsyncClient) -> None:
    # The API only allows future expiry, so insert an already-expired row
    # directly to exercise the expiry branch.
    async with SessionFactory() as session:
        session.add(
            Url(
                short_code="expird",
                long_url="https://example.com/old",
                expires_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
        await session.commit()

    response = await client.get("/expird")
    assert response.status_code == 404
