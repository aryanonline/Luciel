#!/usr/bin/env bash
# Apply the Luciel main-branch protection rule via the GitHub REST API.
#
# Source of truth for the rule is .github/branch-protection.md. This
# script just translates that doctrine into a PUT against the
# branches/main/protection endpoint. It is idempotent.
#
# Pre-requisites:
#   - gh CLI authenticated (`gh auth status`)
#   - the authenticated identity has admin rights on aryanonline/Luciel
#
# Usage:
#   bash scripts/apply_branch_protection.sh
#
# Doctrine D7.3 (Arc 9 C7 Commit B).

set -euo pipefail

OWNER="aryanonline"
REPO="Luciel"
BRANCH="main"

# The two required-check names below MUST match the `name:` field of
# the two jobs in .github/workflows/ci.yml. If either job is renamed,
# update this script in the same PR. The pre-receive gate on main does
# not auto-discover check names.
REQUIRED_CHECK_AST="AST + unit tests (backend-free)"
REQUIRED_CHECK_WIDGET="Widget bundle build + size gate (Step 30b)"

echo "Applying branch protection on ${OWNER}/${REPO}@${BRANCH}..."

gh api \
  --method PUT \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  "repos/${OWNER}/${REPO}/branches/${BRANCH}/protection" \
  --input - <<JSON
{
  "required_status_checks": {
    "strict": true,
    "contexts": [
      "${REQUIRED_CHECK_AST}",
      "${REQUIRED_CHECK_WIDGET}"
    ]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": false,
    "required_approving_review_count": 0,
    "require_last_push_approval": false
  },
  "restrictions": null,
  "required_linear_history": false,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "block_creations": false,
  "required_conversation_resolution": true,
  "lock_branch": false,
  "allow_fork_syncing": false
}
JSON

echo ""
echo "Applied. Current state:"
gh api "repos/${OWNER}/${REPO}/branches/${BRANCH}/protection" \
  --jq '{required_status_checks: .required_status_checks, enforce_admins: .enforce_admins.enabled, required_reviews: .required_pull_request_reviews.required_approving_review_count}'
