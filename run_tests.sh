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
export ENABLE_STUB_EMBEDDING_PROVIDER=true
export CHANNELS_LIVE_PROVISIONING_ENABLED=false
export MAIL_INBOUND_DOMAIN=luciel-mail.com
# Enable the live-Postgres RLS integration test (tests/isolation/test_c9_5_
# live_rls_integration.py). Without this var that crown-jewel test -- the
# only one that proves RLS Layer 2 actually enforces cross-Admin isolation
# against a real non-superuser role -- silently SKIPS. The local stack IS
# a live Postgres, so it must run. (Unit 3 tenant-isolation re-verify.)
#
# The behavioral tenant-isolation gate is the subset `tests/isolation` ONLY
# (Unit 13f MOVE 2, founder ruling: isolation is defined by behavioral
# purpose -- live cross-tenant non-access as a non-superuser -- not by
# filename; migration-shape contract tests were separated to
# tests/migrations_contract/). Invoke `./run_tests.sh tests/isolation` for
# the gate, `./run_tests.sh tests/` for the full suite.
export LUCIEL_LIVE_POSTGRES_URL="postgresql://postgres:postgres@localhost:5432/luciel"
exec python -m pytest -o addopts="" -p no:cacheprovider "$@"
