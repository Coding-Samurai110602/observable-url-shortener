"""SQLAlchemy ORM models for the URL shortener.

Schema notes (the "why" a reviewer cares about):

* ``short_code`` is the read hot-path key, so it is uniquely indexed on ``urls``
  and non-uniquely indexed on ``click_events`` (many events per code).
* We never store raw client IPs. ``click_events.client_ip_hash`` holds a salted
  hash so we retain per-client analytics without holding PII.
* Timestamps are timezone-aware (``timestamptz``) and defaulted server-side via
  ``now()`` so the database, not the app clock, is the source of truth.
* ``urls.click_count`` is a denormalized counter for cheap stats reads; the
  authoritative per-click history lives in ``click_events``.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class Url(Base):
    """A shortened URL mapping and its aggregate click counter."""

    __tablename__ = "urls"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    short_code: Mapped[str] = mapped_column(String(10), nullable=False, unique=True, index=True)
    long_url: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # NULL means "never expires".
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Optional owner/creator identifier; indexed for per-client listing/analytics.
    client_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    click_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0", default=0
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return f"<Url short_code={self.short_code!r} long_url={self.long_url!r}>"


class ClickEvent(Base):
    """One redirect hit against a short code.

    Written on the redirect hot path (asynchronously) to support the stats
    endpoint's "recent clicks" and "top referrers" without scanning app logs.
    """

    __tablename__ = "click_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    short_code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    clicked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    referrer: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Salted hash of the client IP — never the raw address (see module docstring).
    client_ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Composite index supports the common "recent clicks for this code" query,
    # ordered newest-first, without a separate sort step.
    __table_args__ = (
        Index("ix_click_events_short_code_clicked_at", "short_code", "clicked_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return f"<ClickEvent short_code={self.short_code!r} clicked_at={self.clicked_at!r}>"
