FROM python:3.14-slim

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

# Expose port
EXPOSE 8000

# Start command
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]