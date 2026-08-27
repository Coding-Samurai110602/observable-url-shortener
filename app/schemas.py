"""Pydantic request/response models for the API.

These are the API's contract surface: every request body is validated here
before a handler runs, and every response body is shaped here so the wire format
is explicit and documented in the OpenAPI schema. Route handlers never touch raw
dicts.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, HttpUrl, field_validator

from app.core.shortener import BASE62_ALPHABET

# Reuse the generator's alphabet as the single source of truth for what a valid
# custom code may contain, so custom and generated codes are interchangeable.
_BASE62_CHARS = set(BASE62_ALPHABET)

# Mirror the DB column limit (varchar(10)) and the generator's minimum.
CUSTOM_CODE_MIN_LENGTH = 4
CUSTOM_CODE_MAX_LENGTH = 10


class CreateUrlRequest(BaseModel):
    """Body for ``POST /api/urls``."""

    long_url: HttpUrl = Field(description="The destination URL to shorten.")
    custom_code: str | None = Field(
        default=None,
        description="Optional caller-chosen short code (Base62, 4-10 chars).",
    )
    expires_in_days: int | None = Field(
        default=None,
        gt=0,
        le=3650,
        description="Optional lifetime in days; omit for a link that never expires.",
    )

    @field_validator("custom_code")
    @classmethod
    def _validate_custom_code(cls, value: str | None) -> str | None:
        """Reject custom codes that aren't valid Base62 of the allowed length.

        We validate rather than trust caller input so a custom code can't smuggle
        in path-breaking characters or exceed the ``short_code`` column width.
        """
        if value is None:
            return None
        if not (CUSTOM_CODE_MIN_LENGTH <= len(value) <= CUSTOM_CODE_MAX_LENGTH):
            raise ValueError(
                f"custom_code must be {CUSTOM_CODE_MIN_LENGTH}-{CUSTOM_CODE_MAX_LENGTH} "
                "characters long"
            )
        invalid = set(value) - _BASE62_CHARS
        if invalid:
            raise ValueError(
                f"custom_code contains non-Base62 characters: {''.join(sorted(invalid))}"
            )
        return value


class UrlResponse(BaseModel):
    """Response body for URL creation."""

    short_code: str
    short_url: str = Field(description="Fully-qualified short URL (BASE_URL + code).")
    long_url: str
    expires_at: datetime | None

    @classmethod
    def build(
        cls,
        *,
        short_code: str,
        long_url: str,
        expires_at: datetime | None,
        base_url: str,
    ) -> UrlResponse:
        """Construct a response, composing ``short_url`` from the base URL.

        Centralized here so the base-URL-joining rule (strip trailing slash) is
        applied consistently and route handlers don't build URLs by hand.
        """
        return cls(
            short_code=short_code,
            short_url=f"{base_url.rstrip('/')}/{short_code}",
            long_url=long_url,
            expires_at=expires_at,
        )


class ClickInfo(BaseModel):
    """A single recent click, for the stats response."""

    clicked_at: datetime
    referrer: str | None
    user_agent: str | None


class ReferrerCount(BaseModel):
    """Aggregated click count for one referrer."""

    referrer: str | None
    count: int


class StatsResponse(BaseModel):
    """Response body for ``GET /api/urls/{short_code}/stats``."""

    short_code: str
    click_count: int
    recent_clicks: list[ClickInfo]
    top_referrers: list[ReferrerCount]
