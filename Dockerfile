FROM python:3.14-slim

# ---------------------------------------------------------------------
# Arc 8 Work-Unit 3 -- build-time identity (BUILD_GIT_SHA)
# ---------------------------------------------------------------------
# D-version-endpoint-hardcoded-not-build-sha-2026-05-22 resolution. The
# /api/v1/version endpoint (app/api/v1/health.py) reads BUILD_GIT_SHA
# from the environment at process start via app/core/build_info.py and
# returns it in the public version payload so operators can verify
# which build is live without holding a JWT.
#
# Wiring: the deploy script (scripts/deploy_arc8.ps1 and successors)
# passes --build-arg BUILD_GIT_SHA=$(git rev-parse --short HEAD) at
# docker-build time; this ARG is baked into the image as an ENV so
# uvicorn/celery processes inherit it.
#
# Default "unknown" is the boot-safe pattern: a docker build without
# the --build-arg (a dev / local build) still produces a runnable
# image; /api/v1/version simply reports git_sha="unknown" in that
# case rather than failing or omitting the field.
#
# BUILD_IMAGE_DIGEST is NOT a build-arg because the digest is the
# hash of the image content -- it cannot be known inside the build.
# The runtime endpoint populates the digest field from the ECS
# Container Metadata endpoint V4 (ECS_CONTAINER_METADATA_URI_V4) at
# request time, falling back to null when running outside ECS.
ARG BUILD_GIT_SHA="unknown"
ENV BUILD_GIT_SHA=$BUILD_GIT_SHA

# Set working directory
WORKDIR /app

# Install system dependencies
# - gcc, libpq-dev: build deps for psycopg/psycopg2 native code
# - procps: provides ps/pgrep/top for ops debugging inside the container.
#   Not required by the worker HEALTHCHECK as of rev 11 (the probe is
#   now an mtime check on /tmp/celery_alive via scripts/healthcheck_worker.py;
#   producer-side touch lives in app/worker/celery_app.py). procps is
#   retained because a container without ps is operationally hostile
#   when debugging a stuck worker at 2am. ~3MB image-size cost.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    procps \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml .
COPY app/ app/
COPY alembic/ alembic/
COPY alembic.ini .
COPY scripts/ scripts/

# Install Python dependencies
RUN pip install --no-cache-dir .

# ---------------------------------------------------------------------
# Arc 8 Work-Unit 2 -- non-root runtime user (D-worker-runs-as-root...)
# ---------------------------------------------------------------------
# D-worker-runs-as-root-in-container-2026-05-22 resolution. Celery
# emits SecurityWarning: "You're running the worker with superuser
# privileges..." when its boot-time euid is 0. The same image runs
# both the backend (uvicorn) and worker (celery) processes, so the
# non-root posture applies to both services.
#
# Choices:
#   * uid=10001 is a high, fixed numeric uid that does not collide
#     with any Debian system uid (those live below 1000) and is
#     stable across image rebuilds (a non-fixed uid would shift if
#     the order of package installs changed).
#   * --shell /usr/sbin/nologin denies interactive login from within
#     the container; ECS Exec still works because it uses the
#     execute-command agent, not a login shell.
#   * chown /app: the application code lives at /app and needs read
#     access from the luciel user. /tmp is world-writable so the
#     worker liveness-touch file at /tmp/celery_alive does not need
#     a chown.
#
# Closure evidence: `aws ecs execute-command ... -- id` returns
# uid=10001(luciel) and the next worker boot log no longer emits
# the SecurityWarning.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin luciel \
    && chown -R luciel:luciel /app
USER luciel

# Expose port
EXPOSE 8000

# Start command
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
