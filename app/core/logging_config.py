"""structlog configuration: JSON lines to stdout with per-request context.

JSON line output is directly ingestible by any log aggregator (Datadog, Loki,
CloudWatch Logs Insights) without a custom parser. Per-request context
(request_id, route, client_id_hash) is injected via structlog contextvars so
every log call within a request automatically carries that context — no need to
thread it through every function signature.

Both structlog-native callers (``structlog.get_logger()``) and stdlib-based
library loggers (uvicorn, SQLAlchemy, asyncpg) emit the same JSON schema.
This is achieved by routing all stdlib log records through
``structlog.stdlib.ProcessorFormatter``, which applies the same shared processor
chain before the final ``JSONRenderer``.
"""

from __future__ import annotations

import logging
import sys

import structlog

from app.config import Settings


def configure_logging(settings: Settings) -> None:
    """Configure structlog and the stdlib root logger to emit JSON lines to stdout.

    Must be called once at application startup, before any requests are served.
    Subsequent calls are harmless — structlog replaces its processor chain on
    each call rather than appending.

    All log records — whether emitted by ``structlog.get_logger()`` or by
    ``logging.getLogger()`` in a dependency library — pass through the same
    shared processor chain before ``JSONRenderer`` so the JSON schema is
    uniform across every emitter in the process.

    Args:
        settings: Application settings; only ``log_level`` is consumed.
    """
    numeric_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    # Processors shared by both structlog-native callers and stdlib-based library
    # loggers (uvicorn, SQLAlchemy, asyncpg). Defined once here so the JSON
    # schema is identical regardless of which logging API the caller used.
    shared_processors = [
        # Merge per-request context variables bound by the observability
        # middleware (request_id, route, client_id_hash) into every event
        # dict automatically, with no threading through function args.
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.ExceptionRenderer(),
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            # Hand the prepared event dict to ProcessorFormatter for final JSON
            # rendering. This is the bridge between structlog's chain and the
            # stdlib handler set up below.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        # LoggerFactory routes structlog calls through the stdlib system so they
        # land on the same handler (and formatter) as non-structlog library logs.
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        context_class=dict,
        # Cache the logger on first use so repeated structlog.get_logger() calls
        # in hot paths (redirect handler, cache layer) have zero configuration
        # overhead after the first request.
        cache_logger_on_first_use=True,
    )

    # ProcessorFormatter is the single JSON renderer for ALL log records.
    # foreign_pre_chain runs for records that originated from stdlib logging
    # (uvicorn, SQLAlchemy, asyncpg) and never passed through structlog's chain.
    # processors runs last for every record — structlog-native and foreign —
    # after their respective pre-chains have completed.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            # Remove structlog's internal bookkeeping keys (_record,
            # _from_structlog) that wrap_for_formatter injected; they must not
            # appear in the rendered JSON output.
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    # Replace any handlers installed by earlier basicConfig calls (e.g. from
    # test harness setup or library imports) so our formatter is authoritative.
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(numeric_level)
