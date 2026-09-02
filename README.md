# Observable, Rate-Limited URL Shortener

A production-style URL shortener built to demonstrate on-call/observability
engineering, not just CRUD API design: aggressive caching, an atomic Redis
token-bucket rate limiter, Prometheus metrics wired into real code paths,
structured JSON logging, and (later) real alerts + an incident postmortem.

> **Status:** core API, rate limiter, cache, Prometheus metrics, and structured
> JSON logging are complete and tested. The observability stack (Prometheus,
> Alertmanager, Grafana) is wired. Failure-injection exercise and postmortem
> are the next manual step.

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

> **Planned (app containerization session):** the production container entry point will
> pass `log_config=None` to `uvicorn.run()` (Option C) so that uvicorn's
> startup and error messages also flow through the same JSON processor chain
> instead of printing in uvicorn's default plain-text format.

## Observability stack

The full local observability stack — Prometheus, Alertmanager, and Grafana — runs
alongside Postgres and Redis in the same Compose file:

```bash
docker compose -f infra/docker-compose.yml up -d
```

| Service       | URL                          | Default credentials |
|---------------|------------------------------|---------------------|
| Prometheus    | <http://localhost:9090>      | none                |
| Alertmanager  | <http://localhost:9093>      | none                |
| Grafana       | <http://localhost:3000>      | admin / admin       |

> **Security note:** the Grafana `admin/admin` credentials are local-dev-only
> defaults and are flagged as such in `infra/docker-compose.yml`. Rotate them
> before exposing Grafana to any shared network.

**What's provisioned automatically on first start:**

- Grafana datasource pointing at the Prometheus container (no manual click-through).
- The _Observable URL Shortener_ dashboard loaded from
  `infra/grafana/dashboards/url-shortener.json` — six panels covering request
  latency (p50/p95/p99 by route), cache hit ratio, rate-limit rejections, redirect
  outcomes (success / not\_found / expired, stacked), and DB/Redis operation latency.

**Alert rules** (defined in `infra/prometheus/alert_rules.yml`, justified thresholds):

| Alert | Condition | Severity |
|---|---|---|
| HighRedirectLatency | redirect p95 > 500 ms for 2 m | warning |
| RedisOperationSlow | Redis p95 > 50 ms for 2 m | warning |
| CacheHitRatioDrop | hit ratio < 50% for 5 m | warning |
| RateLimitRejectionSpike | rejections > 1/s for 2 m | warning |
| HighErrorRate | any 5xx rate > 0 for 1 m | critical |

Firing alerts route to a local webhook receiver at `host.docker.internal:9091`
(configurable in `infra/alertmanager/alertmanager.yml`). To observe alerts without
a listener, watch the Alertmanager UI at <http://localhost:9093>.

**Hot-reloading Prometheus config** (after editing alert rules or scrape targets):

```bash
curl -X POST http://localhost:9090/-/reload
```

## Repository layout

```
app/        FastAPI app: api/, core/, db/, config, metrics, schemas
tests/      unit/, integration/, load/
infra/      docker-compose.yml, prometheus/, alertmanager/, grafana/
docs/       ARCHITECTURE.md, incidents/
```

## Running tests

```bash
pytest
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for design details.
