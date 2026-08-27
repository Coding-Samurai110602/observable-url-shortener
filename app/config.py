"""Application configuration.

All runtime configuration flows through the single :class:`Settings` instance
exposed by :func:`get_settings`. Nothing in the codebase should read
``os.environ`` directly — doing so scatters config, defeats validation, and
makes the app impossible to test with overrides. Import ``get_settings`` (or
depend on it via FastAPI's ``Depends``) instead.
"""

from functools import lru_cache

from pydantic import Field, PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly-typed settings loaded from environment / ``.env``.

    Field names are lower-case; ``BaseSettings`` matches them case-insensitively
    against environment variables (e.g. ``DATABASE_URL`` -> ``database_url``).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        # Fail loudly on unknown env vars rather than silently ignoring typos.
        extra="forbid",
    )

    # --- Application ---
    app_env: str = Field(
        default="local",
        description="Deployment environment name (local|ci|staging|prod).",
    )
    log_level: str = Field(default="INFO", description="Root log level.")
    base_url: str = Field(
        default="http://localhost:8000",
        description="Public base URL used to construct returned short URLs (no trailing slash).",
    )

    # --- PostgreSQL (async DSN; asyncpg driver enforced by the type + validator) ---
    database_url: PostgresDsn = Field(
        description="Async SQLAlchemy DSN, e.g. postgresql+asyncpg://user:pass@host:5432/db",
    )

    # --- Redis (cache + rate-limit state) ---
    redis_url: RedisDsn = Field(
        default="redis://localhost:6379/0",  # type: ignore[assignment]
        description="Redis connection URL used for both caching and rate limiting.",
    )

    # --- Caching ---
    cache_ttl_seconds: int = Field(
        default=3600,
        gt=0,
        description="TTL for cached short_code -> long_url entries, in seconds.",
    )

    # --- Rate limiting: redirect route class (GET /{short_code}) ---
    rate_limit_redirect_capacity: int = Field(
        default=100,
        gt=0,
        description="Token bucket capacity (burst size) for the redirect route class.",
    )
    rate_limit_redirect_refill_per_minute: int = Field(
        default=100,
        gt=0,
        description="Sustained token refill rate per minute for the redirect route class.",
    )

    # --- Rate limiting: create route class (POST /api/urls) ---
    rate_limit_create_capacity: int = Field(
        default=10,
        gt=0,
        description="Token bucket capacity (burst size) for the create route class.",
    )
    rate_limit_create_refill_per_minute: int = Field(
        default=10,
        gt=0,
        description="Sustained token refill rate per minute for the create route class.",
    )

    # --- Short code generation ---
    short_code_length: int = Field(
        default=7,
        ge=4,
        le=10,
        description="Length of auto-generated Base62 short codes (DB column caps at 10).",
    )
    ip_hash_salt: str = Field(
        default="change-me-to-a-random-secret",
        description="Salt mixed into client-IP hashing so raw IPs are never stored.",
    )


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide cached settings instance.

    Cached so repeated ``Depends(get_settings)`` calls don't re-parse the
    environment. Tests can override by calling ``get_settings.cache_clear()``
    after mutating the environment.
    """
    return Settings()  # type: ignore[call-arg]  # values sourced from env/.env
