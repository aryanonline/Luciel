#!/usr/bin/env bash
# =====================================================================
# Step 30b Phase 2B -- widget bundle CDN deploy
# =====================================================================
#
# Uploads the built widget bundle to the CDN bucket created by the
# luciel-widget-cdn CloudFormation stack, then invalidates the stable
# alias on CloudFront so customers see the new bundle within a few
# minutes (the alias has max-age=300).
#
# What this script does
# =====================
#
#   1. Verifies the build artifacts exist under widget/dist.
#   2. Computes a 12-char content hash of luciel-chat-widget.js.
#   3. Uploads to S3 under TWO key names:
#        - widget.js                         (stable alias customers paste)
#        - luciel-chat-widget.<hash>.js      (immutable, version-pinnable)
#      And the corresponding .map files.
#   4. Sets Cache-Control headers so the CDN behavior matches design:
#        - Stable alias  -> max-age=300, public  (5 minutes)
#        - Hashed bundle -> max-age=31536000, public, immutable (1 year)
#      The CloudFront cache policy honors origin Cache-Control headers,
#      so these values are what visitors actually see.
#   5. Sets Content-Type: application/javascript on .js, application/json
#      on .map.
#   6. Issues a CloudFront invalidation on /widget.js so the previous
#      version stops being served from edges. The hashed bundle is
#      content-addressed, so it does NOT need invalidation -- old hashes
#      stay valid for any customer that pinned a specific version
#      (Pattern E: forward-only).
#   7. Prints the customer-pasteable URL on success.
#
# How to invoke
# =============
#
#   Local (operator, one-time bucket seed):
#       cd C:\\Users\\aryan\\Projects\\Business\\Luciel
#       cd widget && npm run build && cd ..
#       bash scripts/deploy_widget_cdn.sh
#
#   CI (GitHub Actions, after the OIDC role is assumed):
#       same script, AWS_REGION already in env, no AWS profile needed.
#
# Requirements
# ============
#
#   - AWS CLI v2 in PATH.
#   - jq (used for parsing CloudFront invalidation response).
#   - sha256sum (Linux/Mac/Git-Bash) OR shasum -a 256 (Mac fallback
#     handled below).
#   - Either AWS_PROFILE pointing at the prod account, or AWS_*
#     credentials in env (CI path), or an assumed OIDC role.
#
# Stack outputs are baked in as defaults below. They came from
#     aws cloudformation describe-stacks --stack-name luciel-widget-cdn
# on 2026-05-09 deploy.
# =====================================================================

set -euo pipefail

# --- Defaults (from the luciel-widget-cdn CFN stack outputs) ---------
AWS_REGION="${AWS_REGION:-ca-central-1}"
BUCKET="${WIDGET_CDN_BUCKET:-luciel-widget-cdn-prod-ca-central-1}"
DISTRIBUTION_ID="${WIDGET_CDN_DISTRIBUTION_ID:-EU5R6YVX26RPY}"
DISTRIBUTION_DOMAIN="${WIDGET_CDN_DISTRIBUTION_DOMAIN:-d1t84i96t71fsi.cloudfront.net}"

# --- Build paths -----------------------------------------------------
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="${REPO_ROOT}/widget/dist"
BUNDLE_PATH="${DIST_DIR}/luciel-chat-widget.js"
SOURCEMAP_PATH="${DIST_DIR}/luciel-chat-widget.js.map"

# --- Cache-Control values -------------------------------------------
STABLE_CACHE_CONTROL="public, max-age=300, must-revalidate"
HASHED_CACHE_CONTROL="public, max-age=31536000, immutable"

echo "==> Step 30b Phase 2B: widget CDN deploy"
echo "    region:       ${AWS_REGION}"
echo "    bucket:       ${BUCKET}"
echo "    distribution: ${DISTRIBUTION_ID}"
echo "    domain:       ${DISTRIBUTION_DOMAIN}"
echo

# --- [1/6] Pre-flight: artifacts must exist --------------------------
echo "==> [1/6] Pre-flight: verify build artifacts exist"
if [[ ! -f "${BUNDLE_PATH}" ]]; then
  echo "ERROR: bundle not found at ${BUNDLE_PATH}" >&2
  echo "       Run 'cd widget && npm run build' first." >&2
  exit 1
fi
if [[ ! -f "${SOURCEMAP_PATH}" ]]; then
  echo "ERROR: source map not found at ${SOURCEMAP_PATH}" >&2
  exit 1
fi
BUNDLE_SIZE_BYTES="$(wc -c < "${BUNDLE_PATH}")"
echo "    bundle:    $(printf '%s\n' "${BUNDLE_PATH}") (${BUNDLE_SIZE_BYTES} bytes)"
echo "    sourcemap: $(printf '%s\n' "${SOURCEMAP_PATH}")"

# --- [2/6] Compute content hash --------------------------------------
echo "==> [2/6] Compute content hash"
if command -v sha256sum >/dev/null 2>&1; then
  HASH_FULL="$(sha256sum "${BUNDLE_PATH}" | awk '{print $1}')"
elif command -v shasum >/dev/null 2>&1; then
  HASH_FULL="$(shasum -a 256 "${BUNDLE_PATH}" | awk '{print $1}')"
else
  echo "ERROR: neither sha256sum nor shasum found in PATH" >&2
  exit 1
fi
HASH="${HASH_FULL:0:12}"
HASHED_BUNDLE_KEY="luciel-chat-widget.${HASH}.js"
HASHED_SOURCEMAP_KEY="luciel-chat-widget.${HASH}.js.map"
echo "    hash (full):     ${HASH_FULL}"
echo "    hash (12-char):  ${HASH}"
echo "    hashed key:      ${HASHED_BUNDLE_KEY}"

# --- [3/6] Upload to S3 ----------------------------------------------
echo "==> [3/6] Upload to S3"

upload() {
  local local_path="$1"
  local s3_key="$2"
  local cache_control="$3"
  local content_type="$4"
  echo "    -> s3://${BUCKET}/${s3_key}"
  aws s3 cp "${local_path}" "s3://${BUCKET}/${s3_key}" \
    --region "${AWS_REGION}" \
    --cache-control "${cache_control}" \
    --content-type "${content_type}" \
    --no-progress \
    --only-show-errors
}

# Stable alias (default behavior, short cache).
upload "${BUNDLE_PATH}"     "widget.js"            "${STABLE_CACHE_CONTROL}" "application/javascript; charset=utf-8"
upload "${SOURCEMAP_PATH}"  "widget.js.map"        "${STABLE_CACHE_CONTROL}" "application/json; charset=utf-8"

# Hashed bundle (path-pattern behavior, long cache, immutable).
upload "${BUNDLE_PATH}"     "${HASHED_BUNDLE_KEY}"     "${HASHED_CACHE_CONTROL}" "application/javascript; charset=utf-8"
upload "${SOURCEMAP_PATH}"  "${HASHED_SOURCEMAP_KEY}"  "${HASHED_CACHE_CONTROL}" "application/json; charset=utf-8"

# --- [4/6] Invalidate CloudFront on the stable alias -----------------
echo "==> [4/6] Invalidate CloudFront for /widget.js (and its map)"
INVALIDATION_ID="$(
  aws cloudfront create-invalidation \
    --distribution-id "${DISTRIBUTION_ID}" \
    --paths "/widget.js" "/widget.js.map" \
    --query "Invalidation.Id" \
    --output text
)"
echo "    invalidation id: ${INVALIDATION_ID}"
echo "    (the hashed bundle is content-addressed and not invalidated;"
echo "     old hashes stay reachable for version-pinning customers.)"

# --- [5/6] Wait for invalidation to complete -------------------------
echo "==> [5/6] Wait for invalidation to complete (typically 30-60 sec)"
aws cloudfront wait invalidation-completed \
  --distribution-id "${DISTRIBUTION_ID}" \
  --id "${INVALIDATION_ID}"
echo "    invalidation ${INVALIDATION_ID} complete"

# --- [6/6] Print customer-pasteable URL ------------------------------
echo "==> [6/6] Done"
echo
echo "Widget URLs:"
echo "  Stable alias (paste this in customer sites):"
echo "    https://${DISTRIBUTION_DOMAIN}/widget.js"
echo "  Pinned version (this build, immutable):"
echo "    https://${DISTRIBUTION_DOMAIN}/${HASHED_BUNDLE_KEY}"
echo
echo "Smoke test from any network:"
echo "  curl -I https://${DISTRIBUTION_DOMAIN}/widget.js"
echo
