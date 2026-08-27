"""Data-access helpers for the ``urls`` and ``click_events`` tables.

Route handlers stay thin by delegating all persistence and querying here (a
lightweight repository layer). This keeps SQL in one place, makes the query
shapes reviewable, and lets the handlers read as orchestration only.

Each function takes an ``AsyncSession`` rather than opening its own, so callers
control the transaction boundary — except where a function is explicitly meant
to run outside the request lifecycle (see :func:`record_click_in_new_session`).
"""

from __future__ import annotations

import time
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import SessionFactory
from app.db.models import ClickEvent, Url
from app.metrics import db_query_duration_seconds


async def code_exists(session: AsyncSession, short_code: str) -> bool:
    """Return whether a URL row already uses ``short_code``.

    Used both by custom-code collision checks and as the existence predicate
    passed to :func:`app.core.shortener.generate_unique_short_code`.
    """
    _t0 = time.perf_counter()
    result = await session.execute(
        select(Url.id).where(Url.short_code == short_code).limit(1)
    )
    db_query_duration_seconds.labels(query_type="code_exists").observe(
        time.perf_counter() - _t0
    )
    return result.scalar_one_or_none() is not None


async def create_url(
    session: AsyncSession,
    *,
    short_code: str,
    long_url: str,
    expires_at: datetime | None,
    client_id: str | None,
) -> Url:
    """Insert a new URL row and return the persisted model.

    Flushes (not commits) so the caller's transaction — managed by the session
    dependency — remains the commit boundary, while ``url`` is populated with
    server-side defaults if needed.
    """
    url = Url(
        short_code=short_code,
        long_url=long_url,
        expires_at=expires_at,
        client_id=client_id,
    )
    session.add(url)
    _t0 = time.perf_counter()
    await session.flush()
    db_query_duration_seconds.labels(query_type="create_url").observe(
        time.perf_counter() - _t0
    )
    return url


async def get_url(session: AsyncSession, short_code: str) -> Url | None:
    """Fetch a URL row by code, or ``None`` if it does not exist."""
    _t0 = time.perf_counter()
    result = await session.execute(select(Url).where(Url.short_code == short_code))
    db_query_duration_seconds.labels(query_type="get_url").observe(
        time.perf_counter() - _t0
    )
    return result.scalar_one_or_none()


def is_expired(url: Url, now: datetime) -> bool:
    """Return whether ``url`` has an expiry in the past relative to ``now``."""
    return url.expires_at is not None and url.expires_at <= now


async def record_click_in_new_session(
    *,
    short_code: str,
    referrer: str | None,
    user_agent: str | None,
    client_ip_hash: str | None,
) -> None:
    """Record a click and bump the aggregate counter, in a fresh session.

    Intended to run as a background task *after* the redirect response has been
    sent, so the request-scoped session (already closed by then) cannot be
    reused. Opening a dedicated session here keeps the analytics write off the
    redirect's critical path while still committing durably.
    """
    async with SessionFactory() as session:
        session.add(
            ClickEvent(
                short_code=short_code,
                referrer=referrer,
                user_agent=user_agent,
                client_ip_hash=client_ip_hash,
            )
        )
        # Atomic increment at the DB level avoids a read-modify-write race on the
        # counter when many redirects for the same code land concurrently.
        _t0 = time.perf_counter()
        await session.execute(
            update(Url)
            .where(Url.short_code == short_code)
            .values(click_count=Url.click_count + 1)
        )
        await session.commit()
        db_query_duration_seconds.labels(query_type="record_click").observe(
            time.perf_counter() - _t0
        )


async def get_recent_clicks(
    session: AsyncSession, short_code: str, limit: int
) -> list[ClickEvent]:
    """Return the most recent clicks for ``short_code``, newest first."""
    _t0 = time.perf_counter()
    result = await session.execute(
        select(ClickEvent)
        .where(ClickEvent.short_code == short_code)
        .order_by(ClickEvent.clicked_at.desc())
        .limit(limit)
    )
    db_query_duration_seconds.labels(query_type="get_recent_clicks").observe(
        time.perf_counter() - _t0
    )
    return list(result.scalars().all())


async def get_top_referrers(
    session: AsyncSession, short_code: str, limit: int
) -> list[tuple[str | None, int]]:
    """Return ``(referrer, count)`` pairs for a code, most frequent first."""
    count_col = func.count().label("count")
    _t0 = time.perf_counter()
    result = await session.execute(
        select(ClickEvent.referrer, count_col)
        .where(ClickEvent.short_code == short_code)
        .group_by(ClickEvent.referrer)
        .order_by(count_col.desc())
        .limit(limit)
    )
    db_query_duration_seconds.labels(query_type="get_top_referrers").observe(
        time.perf_counter() - _t0
    )
    return [(row.referrer, row.count) for row in result.all()]
