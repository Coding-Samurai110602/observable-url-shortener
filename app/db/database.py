"""Async database engine, session factory, and the declarative base.

This module owns the SQLAlchemy 2.x wiring shared by the ORM models, the
request-scoped session dependency, and Alembic's migration environment. Keeping
the engine/session here (rather than in ``models.py``) avoids an import cycle:
models import only :class:`Base`, while callers that need a live connection
import the engine/session factory.
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models.

    A single ``Base`` means one ``MetaData`` registry, which is what Alembic's
    autogenerate compares against the live database schema.
    """


def _create_engine() -> AsyncEngine:
    """Build the async engine from settings.

    ``pool_pre_ping`` guards against stale connections after a Postgres restart
    or idle-timeout, which is exactly the kind of failure we want to survive
    gracefully rather than surface as a 500.
    """
    settings = get_settings()
    return create_async_engine(
        str(settings.database_url),
        pool_pre_ping=True,
        future=True,
    )


# Module-level singletons: one engine (and therefore one connection pool) per
# process, and a session factory bound to it.
engine: AsyncEngine = _create_engine()

SessionFactory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a request-scoped async session.

    The session is committed on success and rolled back if the request handler
    raises, then always closed. Wiring commit/rollback here keeps route handlers
    free of transaction boilerplate.
    """
    async with SessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
