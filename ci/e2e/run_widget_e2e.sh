#!/usr/bin/env bash
#
# Widget-surface end-to-end harness orchestrator.
#
# Step 30d, Deliverable C.
#
# What this script does
# =====================
#
# Given a running uvicorn app on $BASE_URL and a platform-admin key in
# $ADMIN_KEY, this script provisions exactly enough tenant/domain/embed-
# key state to make two public widget-chat calls -- one benign, one
# blocked by the keyword moderation provider -- and asserts the SSE
# frame contracts that ARCHITECTURE.md section 3.3 (steps 1-7) and the
# Step 30d Deliverable B refusal envelope require.
#
# What this script deliberately is NOT
# ====================================
#
#   * Not a full pillar verification suite. The pillar suite runs in
#     luciel-verify ECS against production-or-staging. The point of
#     this harness is to catch widget-surface regressions that are
#     too fast-moving for the pillar suite to be the gate of record.
#
#   * Not a load test. We make two HTTP calls. Anything more belongs
#     in a different harness.
#
#   * Not a substitute for the JS widget bundle build/size gate (which
#     already lives in .github/workflows/ci.yml). We hit the HTTP
#     surface directly; the bundle is the widget's other half and
#     has its own gate.
#
# Provisioning chain
# ==================
#
# A widget chat call requires (per ARCHITECTURE section 3.2 + 3.2.2):
#
#   1. A tenant_configs row whose system_prompt_additions is set
#      (otherwise the tenant-wide path lacks scope; we also set the
#       domain-level scope below).
#   2. A domain_configs row whose system_prompt_additions is set --
#      this is exactly what the Step 30d Deliverable A preflight
#      checks at embed-key issuance time. Setting it gives us a
#      green light from the preflight and a real scope prompt for
#      the chat call to render against.
#   3. An embed key minted via POST /api/v1/admin/embed-keys with
#      allowed_origins set to $TEST_ORIGIN. The endpoint sets
#      permissions=["chat"] server-side, which require_embed_key
#      then enforces.
#
# The two assertion calls then hit POST /api/v1/chat/widget with that
# embed key in Authorization: Bearer ..., the matching Origin: header,
# and a JSON payload with a "message" field.
#
# Required environment
# ====================
#
#   ADMIN_KEY    -- raw platform-admin key (from bootstrap script)
#   BASE_URL     -- defaults to http://127.0.0.1:8000
#   TEST_ORIGIN  -- defaults to https://e2e.luciel.test
#                   Must match exactly one entry in allowed_origins.
#
# These environment variables MUST line up with the moderation config
# the running app was booted with (see widget-e2e.yml):
#
#   moderation_provider=keyword
#   moderation_keyword_block_terms='["E2E_REFUSE_SENTINEL"]'
#
# If they don't, the refusal-path assertion will fail. That is by
# design -- a mismatch between the workflow's app boot and this
# harness's sentinel is exactly the misconfig the harness should
# catch.
#
# Exit code
# =========
#
#   0 if every step succeeded.
#   non-zero on any failure (set -euo pipefail makes the offending
#   step's exit code propagate).

set -euo pipefail

# ---------------------------------------------------------------------------
# Inputs and defaults
# ---------------------------------------------------------------------------

if [ -z "${ADMIN_KEY:-}" ]; then
    echo "FATAL: ADMIN_KEY env var is required" >&2
    exit 1
fi

BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
TEST_ORIGIN="${TEST_ORIGIN:-https://e2e.luciel.test}"

# Unique tenant_id per run so re-runs in the same DB don't collide.
# In CI the DB is ephemeral so this is belt-and-suspenders; locally
# it lets you re-run without resetting Postgres.
TS="$(date +%s)"
TENANT_ID="e2e-tenant-${TS}"
DOMAIN_ID="e2e-domain-${TS}"

# This sentinel MUST match moderation_keyword_block_terms in the
# workflow env. See the file-header comment block above.
REFUSAL_SENTINEL="E2E_REFUSE_SENTINEL"

# Shared curl flags. --fail-with-body returns non-zero on non-2xx so
# the script halts with the response body visible. --silent +
# --show-error keeps logs clean unless there's an error.
CURL_BASE=(
    curl
    --silent
    --show-error
    --fail-with-body
    --max-time 30
    -H "Authorization: Bearer ${ADMIN_KEY}"
    -H "Content-Type: application/json"
)

echo "==> widget-e2e starting"
echo "    BASE_URL    = ${BASE_URL}"
echo "    TEST_ORIGIN = ${TEST_ORIGIN}"
echo "    TENANT_ID   = ${TENANT_ID}"
echo "    DOMAIN_ID   = ${DOMAIN_ID}"

# ---------------------------------------------------------------------------
# Step 1: create the tenant
# ---------------------------------------------------------------------------

echo "==> [1/5] POST /api/v1/admin/tenants"
"${CURL_BASE[@]}" \
    -X POST \
    "${BASE_URL}/api/v1/admin/tenants" \
    -d "$(cat <<JSON
{
  "tenant_id": "${TENANT_ID}",
  "display_name": "E2E tenant ${TS}",
  "system_prompt_additions": "You are an E2E test assistant. Reply tersely.",
  "created_by": "widget-e2e@step-30d-c"
}
JSON
)" \
    >/dev/null

# ---------------------------------------------------------------------------
# Step 2: create the domain WITH system_prompt_additions
# (Deliverable A preflight green-light)
# ---------------------------------------------------------------------------

echo "==> [2/5] POST /api/v1/admin/domains"
"${CURL_BASE[@]}" \
    -X POST \
    "${BASE_URL}/api/v1/admin/domains" \
    -d "$(cat <<JSON
{
  "tenant_id": "${TENANT_ID}",
  "domain_id": "${DOMAIN_ID}",
  "display_name": "E2E domain ${TS}",
  "system_prompt_additions": "Domain-level scope for E2E tests.",
  "created_by": "widget-e2e@step-30d-c"
}
JSON
)" \
    >/dev/null

# ---------------------------------------------------------------------------
# Step 3: mint the embed key. response includes the raw key once.
# ---------------------------------------------------------------------------

echo "==> [3/5] POST /api/v1/admin/embed-keys"
EMBED_RESPONSE="$(
    "${CURL_BASE[@]}" \
        -X POST \
        "${BASE_URL}/api/v1/admin/embed-keys" \
        -d "$(cat <<JSON
{
  "tenant_id": "${TENANT_ID}",
  "domain_id": "${DOMAIN_ID}",
  "display_name": "E2E embed key ${TS}",
  "allowed_origins": ["${TEST_ORIGIN}"],
  "rate_limit_per_minute": 1000,
  "widget_config": {},
  "created_by": "widget-e2e@step-30d-c"
}
JSON
)"
)"

# Extract raw_key with a minimal Python one-liner (jq may or may not
# be on the runner; Python is guaranteed because the workflow uses
# setup-python). We deliberately do NOT echo EMBED_RESPONSE to stdout
# so the raw key cannot leak into the CI log.
EMBED_KEY="$(
    python -c '
import json, sys
data = json.loads(sys.stdin.read())
raw = data.get("raw_key")
if not raw:
    sys.stderr.write("FATAL: response missing raw_key field\n")
    sys.exit(1)
print(raw)
' <<<"${EMBED_RESPONSE}"
)"

# ---------------------------------------------------------------------------
# Step 4: happy-path widget chat
# ---------------------------------------------------------------------------

echo "==> [4/5] widget chat: happy path"
python ci/e2e/assert_widget_stream.py \
    --mode happy \
    --base-url "${BASE_URL}" \
    --embed-key "${EMBED_KEY}" \
    --origin "${TEST_ORIGIN}"

# ---------------------------------------------------------------------------
# Step 5: refusal-path widget chat (keyword moderation block)
# ---------------------------------------------------------------------------

echo "==> [5/5] widget chat: refusal path"
python ci/e2e/assert_widget_stream.py \
    --mode refusal \
    --base-url "${BASE_URL}" \
    --embed-key "${EMBED_KEY}" \
    --origin "${TEST_ORIGIN}" \
    --sentinel "${REFUSAL_SENTINEL}"

echo "==> widget-e2e: all assertions passed"
