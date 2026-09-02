# ─────────────────────────────────────────────────────────────────────────────
# Stage 1 — builder
#
# Install runtime Python dependencies into an isolated virtualenv.
# The builder stage may pull in build tools (pip, wheel, setuptools, and any
# C-extension compilers). None of these land in the final image — only the
# populated /opt/venv directory is copied across.
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

RUN pip install --upgrade pip

# Copy the package manifest before any application source. Because this COPY
# is the only thing the pip install below depends on, Docker's layer cache
# keeps the (large, slow) dependency-install layer intact across rebuilds
# where only application code changes — it only re-runs when
# [project.dependencies] in pyproject.toml actually changes.
COPY pyproject.toml README.md ./

# Create the virtualenv and install runtime dependencies from pyproject.toml.
#
# tomllib (stdlib ≥ 3.11) is used to extract the [project.dependencies] list
# directly so we install only the production packages — not the [dev] extras
# (pytest, ruff, mypy, locust, fakeredis). The application package itself is
# NOT installed here; it is copied as plain source in the runtime stage below,
# which means code-only changes never bust this dependency layer.
RUN python -m venv /opt/venv && \
    /opt/venv/bin/python -c "\
import tomllib, subprocess; \
deps = tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']; \
subprocess.run(['/opt/venv/bin/pip', 'install', '--no-cache-dir'] + deps, check=True)"


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2 — runtime
#
# Fresh python:3.12-slim base. Only the pre-built virtualenv and application
# source are present — the builder's toolchain and intermediate files are
# discarded entirely, keeping the final image small and the attack surface
# minimal.
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Non-root user: running as root inside a container means a successful
# container-escape exploit grants the attacker host-root privileges. A
# dedicated system account with no home directory and no login shell is the
# standard defence-in-depth mitigation for containerised services.
RUN useradd --system --no-create-home --shell /bin/false appuser

WORKDIR /app

# Pre-built virtualenv — all production Python packages, no dev tooling.
COPY --from=builder /opt/venv /opt/venv

# Application source, copied from the build context as a distinct layer so
# that code-only changes don't invalidate the (much larger) venv layer above.
# Python resolves `import app` here because /app is the working directory and
# therefore the first entry on sys.path in -c invocation mode.
COPY app/ ./app/

# Alembic config: needed so `alembic upgrade head` can be run as a pre-deploy
# step or init container before the service starts.
# script_location = app/db/migrations in alembic.ini resolves to
# /app/app/db/migrations at runtime, which is provided by the COPY above.
COPY alembic.ini ./

# Add the venv's bin directory to PATH so `python`, `uvicorn`, and `alembic`
# all resolve to the venv's versions without needing explicit activation.
#
# PYTHONUNBUFFERED: flush stdout/stderr immediately so container log drivers
#   capture every log line even if the process crashes between flushes.
# PYTHONDONTWRITEBYTECODE: skip .pyc generation — the container filesystem is
#   ephemeral so cached bytecode just wastes space.
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

# Liveness probe against /health, which is intentionally dependency-free (no
# Redis or Postgres check) so a transient downstream blip cannot cause the
# orchestrator to restart a healthy container via this probe.
#
# urllib is used instead of curl or wget because the slim base image ships
# neither, and installing either would widen the image's attack surface for
# no runtime benefit.
#
# --start-period 10s: allow first-request overhead (module imports, SQLAlchemy
#   engine creation, Prometheus registry init) before failures count.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

# Drop to the non-root user before starting the server process.
USER appuser

# log_config=None tells uvicorn to skip its own logging setup entirely,
# leaving the JSON structlog configuration — applied in configure_logging()
# during create_app() in app/main.py — as the sole log handler.
#
# Without this, uvicorn's default plain-text formatter would emit startup and
# shutdown messages in a different format from the rest of the JSON log stream,
# breaking any log aggregator (CloudWatch Logs Insights, Loki, Datadog) that
# expects a single consistent encoding per stream.
#
# The Python API is used here rather than the uvicorn CLI because the CLI
# offers no equivalent to log_config=None: the --log-config flag requires a
# path to a logging config file and cannot express "skip all logging setup".
#
# This implements the "Option C / log_config=None" approach documented as
# planned in the README's observability section.
CMD ["python", "-c", \
     "import uvicorn; uvicorn.run('app.main:app', host='0.0.0.0', port=8000, log_config=None)"]
