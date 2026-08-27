# Observable, Rate-Limited URL Shortener

A production-style URL shortener built to demonstrate on-call/observability
engineering, not just CRUD API design: aggressive caching, an atomic Redis
token-bucket rate limiter, Prometheus metrics wired into real code paths,
structured JSON logging, and (later) real alerts + an incident postmortem.

> **Status:** scaffolding phase. Repo structure, config, data model, and the
> Base62 shortener are in place. The API, rate limiter, cache, metrics, and
> observability stack land in subsequent phases.

## Tech stack

- Python 3.12 · FastAPI · uvicorn
- Redis 7 (`redis-py` async) — caching + rate-limit state
- PostgreSQL 16 · SQLAlchemy 2 (async) · Alembic
- `prometheus-client` · `structlog`
- `pytest` / `pytest-asyncio` / `httpx` · `locust`
- Docker · docker-compose · GitHub Actions
- Prometheus · Alertmanager · Grafana (later phase)

## Local setup

```bash
# 1. Configure environment
cp .env.example .env

# 2. Create and activate a virtualenv, then install (runtime + dev)
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 3. Start local dependencies (Postgres + Redis)
docker compose -f infra/docker-compose.yml up -d

# 4. Apply database migrations
alembic upgrade head
```

## Running locally

```bash
uvicorn app.main:app --reload --no-access-log
```

`--no-access-log` suppresses uvicorn's built-in plain-text access log lines; the
app's own observability middleware already emits a structured JSON `request.end`
event for every request containing richer data — `request_id`, route template,
`client_id_hash`, and `duration_ms` — than uvicorn's access log provides.

> **Planned (Docker/infra session):** the production container entry point will
> pass `log_config=None` to `uvicorn.run()` (Option C) so that uvicorn's
> startup and error messages also flow through the same JSON processor chain
> instead of printing in uvicorn's default plain-text format.

## Repository layout

```
app/        FastAPI app: api/, core/, db/, config, metrics, schemas
tests/      unit/, integration/, load/
infra/      docker-compose + (later) prometheus/alertmanager/grafana
docs/       ARCHITECTURE.md, incidents/
```

## Running tests

```bash
pytest
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for design details.
