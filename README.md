# Observable, Rate-Limited URL Shortener

A production-style URL shortener built to demonstrate on-call/observability
engineering, not just CRUD API design: aggressive caching, an atomic Redis
token-bucket rate limiter, Prometheus metrics wired into real code paths,
structured JSON logging, and (later) real alerts + an incident postmortem.

> **Status:** core API, rate limiter, cache, Prometheus metrics, structured JSON
> logging, observability stack (Prometheus / Alertmanager / Grafana), fault
> tolerance (Redis fail-open), incident postmortem, and CI/CD pipeline are all
> complete. Docker image publishes to GHCR on every merge to main.

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

## CI/CD

The pipeline lives in `.github/workflows/ci-cd.yml` and runs four jobs:

| Job | Trigger | What it does |
|---|---|---|
| **lint** | every push & PR | `ruff check .` + `mypy .` |
| **test** | every push & PR (needs lint) | spins up Postgres 16 + Redis 7 service containers, runs `alembic upgrade head`, then `pytest tests/unit/ tests/integration/ -v` |
| **build** | push to `main` only (needs test) | builds the multi-stage Docker image and pushes two tags (`sha-<full-sha>` and `latest`) to GitHub Container Registry |
| **deploy** | manual (`workflow_dispatch`) only | placeholder — prints the planned ECS Fargate steps; no AWS infrastructure is configured yet |

The deploy job is intentionally manual-trigger only: no automatic deploys happen until the AWS/ECS Fargate target is defined in a future Terraform session.

**Build status and published images** (visible once the repository is public or you have org access):

- Actions runs: `https://github.com/<owner>/observable-url-shortener/actions`
- Container packages: `https://github.com/<owner>/observable-url-shortener/pkgs/container/observable-url-shortener`

**Docker image** — multi-stage build on `python:3.12-slim`:

- Builder stage installs only the production dependencies from `[project.dependencies]` in `pyproject.toml` into an isolated virtualenv — no `pytest`, `ruff`, `mypy`, or `locust` in the final image.
- Runtime stage copies the pre-built virtualenv and application source, runs as a non-root system user (`appuser`), and starts uvicorn via the Python API with `log_config=None` so uvicorn's startup messages flow through the same JSON structlog pipeline as the rest of the application (rather than printing in uvicorn's default plain-text format).
- `HEALTHCHECK` uses the `/health` liveness endpoint (dependency-free by design).

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for design details.
