"""HTTP route handlers: create, redirect, and stats.

Handlers here are intentionally thin — they validate input (via Pydantic
schemas and dependencies) and orchestrate calls into ``app.core`` (cache,
shortener) and ``app.db.repository`` (persistence). No SQL, no Redis commands,
and no URL-building logic live inline.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    get_client_identifier,
    get_db,
    get_url_cache,
    hash_client_ip,
    rate_limit,
)
from app.config import Settings, get_settings
from app.core.cache import UrlCache
from app.core.shortener import ShortCodeGenerationError, generate_unique_short_code
from app.db import repository
from app.metrics import redirect_total
from app.schemas import (
    ClickInfo,
    CreateUrlRequest,
    ReferrerCount,
    StatsResponse,
    UrlResponse,
)

_log = structlog.get_logger(__name__)

router = APIRouter()

# How many rows the stats endpoint returns; kept modest so the endpoint stays
# cheap and its response bounded.
_RECENT_CLICKS_LIMIT = 20
_TOP_REFERRERS_LIMIT = 10


@router.post(
    "/api/urls",
    response_model=UrlResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[rate_limit("create")],
    summary="Create a short URL",
)
async def create_url(
    payload: CreateUrlRequest,
    session: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    client_id: Annotated[str, Depends(get_client_identifier)],
) -> UrlResponse:
    """Create a new short URL, using a caller-supplied code or generating one."""
    long_url = str(payload.long_url)

    if payload.custom_code is not None:
        if await repository.code_exists(session, payload.custom_code):
            _log.warning("create.custom_code_conflict", short_code=payload.custom_code)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"custom_code '{payload.custom_code}' is already in use.",
            )
        short_code = payload.custom_code
    else:
        # The generator owns ret/collision logic; we only supply the DB-backed
        # existence predicate so uniqueness is checked against real rows.
        async def _exists(code: str) -> bool:
            return await repository.code_exists(session, code)

        try:
            short_code = await generate_unique_short_code(
                _exists, length=settings.short_code_length
            )
        except ShortCodeGenerationError as exc:
            # Keyspace saturation is a server-side capacity problem, not client error.
            _log.error("create.keyspace_saturated", short_code_length=settings.short_code_length)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Could not allocate a unique short code; please retry.",
            ) from exc

    expires_at: datetime | None = None
    if payload.expires_in_days is not None:
        expires_at = datetime.now(timezone.utc) + timedelta(days=payload.expires_in_days)

    try:
        url = await repository.create_url(
            session,
            short_code=short_code,
            long_url=long_url,
            expires_at=expires_at,
            client_id=client_id,
        )
    except IntegrityError as exc:
        # A concurrent request may have claimed the same code between our check
        # and this insert; the DB unique constraint is the final arbiter. The
        # session dependency rolls back on this raised exception.
        _log.warning("create.race_conflict", short_code=short_code)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"short_code '{short_code}' was just taken; please retry.",
        ) from exc

    return UrlResponse.build(
        short_code=url.short_code,
        long_url=url.long_url,
        expires_at=url.expires_at,
        base_url=settings.base_url,
    )


@router.get(
    "/api/urls/{short_code}/stats",
    response_model=StatsResponse,
    summary="Get click statistics for a short URL",
)
async def get_stats(
    short_code: str,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> StatsResponse:
    """Return the aggregate count, recent clicks, and top referrers for a code."""
    url = await repository.get_url(session, short_code)
    if url is None:
        _log.warning("stats.not_found", short_code=short_code)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Short code '{short_code}' not found.",
        )

    recent = await repository.get_recent_clicks(session, short_code, _RECENT_CLICKS_LIMIT)
    top_referrers = await repository.get_top_referrers(session, short_code, _TOP_REFERRERS_LIMIT)

    return StatsResponse(
        short_code=short_code,
        click_count=url.click_count,
        recent_clicks=[
            ClickInfo(
                clicked_at=click.clicked_at,
                referrer=click.referrer,
                user_agent=click.user_agent,
            )
            for click in recent
        ],
        top_referrers=[
            ReferrerCount(referrer=referrer, count=count) for referrer, count in top_referrers
        ],
    )


@router.get(
    "/{short_code}",
    dependencies=[rate_limit("redirect")],
    summary="Redirect a short URL to its destination",
)
async def redirect(
    short_code: str,
    request: Request,
    background_tasks: BackgroundTasks,
    session: Annotated[AsyncSession, Depends(get_db)],
    cache: Annotated[UrlCache, Depends(get_url_cache)],
    settings: Annotated[Settings, Depends(get_settings)],
    client_id: Annotated[str, Depends(get_client_identifier)],
) -> RedirectResponse:
    """Resolve a short code to its destination and issue a 302 redirect.

    Cache-aside: the Redis cache is consulted first; on a miss we hit Postgres
    and back-fill. Only non-expiring URLs are cached — time-bounded links always
    resolve through the DB so an expired link can never be served from a stale
    cache entry. The click is recorded in the background so analytics never sit
    on the redirect's critical path.
    """
    long_url = await cache.get_cached_url(short_code)

    if long_url is None:
        url = await repository.get_url(session, short_code)
        now = datetime.now(timezone.utc)
        # Split not_found and expired into separate branches so redirect_total
        # can carry the precise outcome label for distinct alerting thresholds.
        if url is None:
            redirect_total.labels(status="not_found").inc()
            _log.warning("redirect.not_found", short_code=short_code)
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Short code '{short_code}' not found or expired.",
            )
        if repository.is_expired(url, now):
            redirect_total.labels(status="expired").inc()
            _log.warning("redirect.expired", short_code=short_code, expires_at=str(url.expires_at))
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Short code '{short_code}' not found or expired.",
            )
        long_url = url.long_url
        # Cache only immutable (non-expiring) mappings; see docstring.
        if url.expires_at is None:
            await cache.set_cached_url(short_code, long_url)

    redirect_total.labels(status="success").inc()
    background_tasks.add_task(
        repository.record_click_in_new_session,
        short_code=short_code,
        referrer=request.headers.get("referer"),
        user_agent=request.headers.get("user-agent"),
        client_ip_hash=hash_client_ip(client_id, settings.ip_hash_salt),
    )

    return RedirectResponse(url=long_url, status_code=status.HTTP_302_FOUND)
