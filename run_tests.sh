#!/usr/bin/env bash
# Local production-like test harness for the audit-and-alignment phase.
# Postgres 17 + pgvector 0.8.0 on :5432, Redis 8 on :6379.
set -e
cd "$(dirname "$0")"
. .venv/bin/activate
export DATABASE_URL="postgresql+psycopg://postgres:postgres@localhost:5432/luciel"
export REDIS_URL="redis://localhost:6379/0"
export MODERATION_PROVIDER=null
export ENABLE_STUB_LLM_PROVIDER=true
export CHANNELS_LIVE_PROVISIONING_ENABLED=false
export MAIL_INBOUND_DOMAIN=luciel-mail.com
exec python -m pytest -o addopts="" -p no:cacheprovider "$@"
