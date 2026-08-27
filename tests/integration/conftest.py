"""Shared fixtures for integration tests.

These tests exercise the real app against real Postgres + Redis, so they assume
the ``infra/docker-compose.yml`` services are running and migrations have been
applied (``alembic upgrade head``) — the same setup as local development.

Each test runs with a clean slate: the tables are truncated and Redis is flushed
before every test so rate-limit buckets and cached entries never leak across
tests. ``flushdb`` here targets the local/CI test Redis only.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.api.dependencies import get_redis_client
from app.db.database import engine
from app.main import create_app


@pytest_asyncio.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    """An HTTP client bound to the ASGI app, with redirect-following disabled.

    ``follow_redirects=False`` so the 302 from the redirect route is observable
    rather than transparently followed to the (external) destination.
    """
    transport = ASGITransport(app=create_app())
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        follow_redirects=False,
    ) as http_client:
        yield http_client


@pytest_asyncio.fixture(autouse=True)
async def reset_state() -> AsyncGenerator[None, None]:
    """Truncate tables and flush Redis before each test for isolation."""
    async with engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE urls, click_events RESTART IDENTITY CASCADE")
        )
    await get_redis_client().flushdb()
    yield
