"""FastAPI application factory.

Builds the app, wires routers, and attaches the observability middleware that
generates a request ID, records the Prometheus latency histogram, and emits
structured start/end log lines for every request.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from uuid import uuid4

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import Response
from structlog.contextvars import bind_contextvars, clear_contextvars

from app.api import health, routes
from app.api.dependencies import get_client_identifier, hash_client_ip
from app.config import get_settings
from app.core.logging_config import configure_logging
from app.metrics import http_request_duration_seconds


def create_app() -> FastAPI:
    """Create and configure the FastAPI application.

    Router order is significant: the health router is included *before* the API
    router because the redirect route (``GET /{short_code}``) is a single-segment
    catch-all that would otherwise shadow ``/health``, ``/ready``, and ``/metrics``.
    """
    settings = get_settings()
    configure_logging(settings)

    app = FastAPI(
        title="Observable URL Shortener",
        version="0.1.0",
        summary="A production-style, observable, rate-limited URL shortener.",
    )

    @app.middleware("http")
    async def observability_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Per-request observability: request ID, structured logs, Prometheus timing.

        Two concerns live here rather than in separate middlewares because they
        share a single ``perf_counter`` start time — splitting them would require
        passing the timestamp between layers, which is more ceremony than the
        problem warrants.
        """
        # Generate a request ID (prefer a forwarded one from an upstream proxy so
        # distributed traces stay correlated across the load balancer boundary).
        request_id = request.headers.get("x-request-id") or str(uuid4())

        # Hash the client IP the same way the rate-limiter does so structured
        # log lines and rate-limit events can be correlated without storing PII.
        client_id = get_client_identifier(request)
        client_id_hash = hash_client_ip(client_id, settings.ip_hash_salt)

        # Clear any leftover context from a previous request on this async task
        # (important for server environments that reuse tasks across requests).
        clear_contextvars()
        bind_contextvars(
            request_id=request_id,
            # Bind the raw path now so mid-request logs (cache, rate-limit) have
            # route context; the middleware overwrites this with the route template
            # after routing completes, so request.end always carries the template.
            route=request.url.path,
            client_id_hash=client_id_hash,
        )

        log = structlog.get_logger("app.middleware")
        log.info("request.start", method=request.method, path=request.url.path)

        start = time.perf_counter()
        response: Response | None = None
        _exc_occurred = False
        try:
            response = await call_next(request)
            return response
        except Exception:
            _exc_occurred = True
            raise
        finally:
            duration_s = time.perf_counter() - start
            duration_ms = round(duration_s * 1000, 2)
            status_code = str(response.status_code) if response is not None else "500"

            # Resolve the route template now that routing has completed. Using
            # the template (e.g. ``/{short_code}``) rather than the actual path
            # keeps Prometheus label cardinality bounded — every unique short
            # code would otherwise become its own label value.
            matched_route = request.scope.get("route")
            route_label = (
                matched_route.path
                if matched_route is not None and hasattr(matched_route, "path")
                else request.url.path
            )

            # Update structlog context with the resolved template so request.end
            # carries the canonical route name even for parameterized paths.
            bind_contextvars(route=route_label)

            http_request_duration_seconds.labels(
                route=route_label,
                method=request.method,
                status=status_code,
            ).observe(duration_s)

            # Add request ID to the response headers so clients and proxies can
            # correlate their own logs with ours.
            if response is not None:
                response.headers["x-request-id"] = request_id

            # exc_info=True is safe here: in the exception path sys.exc_info()
            # is non-empty (the except clause above just set _exc_occurred and
            # re-raised, so the exception is still active when finally runs);
            # on the normal return path _exc_occurred is False and
            # ExceptionRenderer treats a falsy exc_info as a no-op.
            log.info(
                "request.end",
                method=request.method,
                route=route_label,
                status=status_code,
                duration_ms=duration_ms,
                exc_info=_exc_occurred,
            )

    app.include_router(health.router)
    app.include_router(routes.router)

    return app


# Module-level ASGI app for `uvicorn app.main:app`.
app = create_app()
