#!/usr/bin/env bash
#
# Widget-surface end-to-end harness orchestrator.
#
# Originally: Step 30d, Deliverable C.
# Rebuilt at: Arc 9.2 PR #99 — Option 2 harness rebuild against the
#             Admin -> Instance V2 hierarchy (Arc 5 Path A landed).
#
# What this script does
# =====================
#
# Given a running uvicorn app on $BASE_URL and a platform-admin key in
# $ADMIN_KEY, this script provisions exactly enough Admin / Instance /
# embed-key state to make two public widget-chat calls -- one benign,
# one blocked by the keyword moderation provider -- and asserts the
# SSE frame contracts that ARCHITECTURE.md section 3.3 (steps 1-7)
# and the Step 30d Deliverable B refusal envelope require.
#
# What this script deliberately is NOT
# ====================================
#
#   * Not a full pillar verification suite. The pillar suite runs in
#     luciel-verify ECS against production-or-staging. The point of
#     this harness is to catch widget-surface regressions that are
#     too fast-moving for the pillar suite to be the gate of record.
#
#   * Not a load test. We make a handful of HTTP calls; anything more
#     belongs in a different harness.
#
#   * Not a substitute for the JS widget bundle build/size gate (which
#     already lives in .github/workflows/ci.yml). We hit the HTTP
#     surface directly; the bundle is the widget's other half and
#     has its own gate.
#
# Provisioning chain (V2 — Admin -> Instance)
# ===========================================
#
# A widget chat call requires (per ARCHITECTURE section 3.2 + 3.2.2):
#
#   1. An admins row (created via POST /api/v1/admin/tenants which
#      remains the public surface for Admin creation — the route name
#      is preserved for API back-compat; the underlying entity is the
#      Admin in V2 vocab).
#   2. An instances row tied to that Admin (POST /api/v1/admin/instances).
#      system_prompt_additions is set here so the four-layer prompt has
#      a populated instance layer at chat time.
#   3. An embed key minted via POST /api/v1/admin/embed-keys, pinned to
#      (tenant_id=Admin.id, luciel_instance_id=Instance.id) and with
#      allowed_origins=[$TEST_ORIGIN]. The endpoint sets
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

# Unique Admin / Instance per run so re-runs in the same DB don't collide.
# In CI the DB is ephemeral so this is belt-and-suspenders; locally
# it lets you re-run without resetting Postgres.
TS="$(date +%s)"
TENANT_ID="e2e-tenant-${TS}"
INSTANCE_SLUG="e2e-instance-${TS}"

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
echo "    BASE_URL       = ${BASE_URL}"
echo "    TEST_ORIGIN    = ${TEST_ORIGIN}"
echo "    TENANT_ID      = ${TENANT_ID}"
echo "    INSTANCE_SLUG  = ${INSTANCE_SLUG}"

# ---------------------------------------------------------------------------
# Step 1: create the Admin (POST /admin/tenants — V2-aliased route)
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
  "created_by": "widget-e2e@arc9_2-pr99"
}
JSON
)" \
    >/dev/null

# ---------------------------------------------------------------------------
# Step 2: create the Instance under that Admin (V2: replaces Step 30d
# Deliverable A domain-preflight; the per-Instance system_prompt_additions
# carries the same role that domain_configs.system_prompt_additions used
# to play in the legacy four-layer prompt).
# ---------------------------------------------------------------------------

echo "==> [2/5] POST /api/v1/admin/instances"
INSTANCE_RESPONSE="$(
    "${CURL_BASE[@]}" \
        -X POST \
        "${BASE_URL}/api/v1/admin/instances" \
        -d "$(cat <<JSON
{
  "admin_id": "${TENANT_ID}",
  "instance_slug": "${INSTANCE_SLUG}",
  "display_name": "E2E instance ${TS}",
  "description": "Provisioned by widget-e2e arc9_2-pr99 harness.",
  "active": true,
  "created_by": "widget-e2e@arc9_2-pr99",
  "system_prompt_additions": "Instance-level scope for E2E tests."
}
JSON
)"
)"

INSTANCE_ID="$(
    python -c '
import json, sys
data = json.loads(sys.stdin.read())
pk = data.get("id")
if pk is None:
    sys.stderr.write("FATAL: instance response missing id field\n")
    sys.exit(1)
print(pk)
' <<<"${INSTANCE_RESPONSE}"
)"

echo "    instance pk = ${INSTANCE_ID}"

# ---------------------------------------------------------------------------
# Step 3: mint the embed key. response includes the raw key once.
# Key is pinned to (tenant_id, luciel_instance_id). domain_id is omitted
# (the Domain layer no longer exists in V2; the issuance preflight that
# used to validate domain_configs.system_prompt_additions is now satisfied
# by the Instance row created in Step 2).
# ---------------------------------------------------------------------------

echo "==> [3/5] POST /api/v1/admin/embed-keys"
EMBED_RESPONSE="$(
    "${CURL_BASE[@]}" \
        -X POST \
        "${BASE_URL}/api/v1/admin/embed-keys" \
        -d "$(cat <<JSON
{
  "tenant_id": "${TENANT_ID}",
  "luciel_instance_id": ${INSTANCE_ID},
  "display_name": "E2E embed key ${TS}",
  "allowed_origins": ["${TEST_ORIGIN}"],
  "rate_limit_per_minute": 1000,
  "widget_config": {},
  "created_by": "widget-e2e@arc9_2-pr99"
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
