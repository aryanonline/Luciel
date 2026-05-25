# Branch protection — Luciel `main`

**Doctrine D7.3 (Arc 9 C7 Commit B):** `main` is gated by a required CI
check. No PR merges to `main` with a red CI run except via the
exception procedure documented below.

This file is the **source of truth** for the GitHub branch-protection
rule on `main`. GitHub stores rules in its own database (not in the
repo), so this file is paired with `scripts/apply_branch_protection.sh`
which calls the GitHub REST API to apply the rule. Anyone with admin
rights to the repo can re-apply the rule by running that script.

## Rule (as applied)

Branch: `main`

| Setting | Value |
|---|---|
| Required status checks | `AST + unit tests (backend-free)`, `Widget bundle build + size gate (Step 30b)` |
| Strict checks (require up-to-date branch before merge) | true |
| Required reviews | 0 (single-owner repo; the AI agent is the reviewer) |
| Dismiss stale reviews on push | true |
| Required signed commits | false |
| Required linear history | false |
| Required conversation resolution | true |
| Lock branch | false |
| Restrict pushes that create matching branches | false |
| Allow force pushes | false |
| Allow deletions | false |
| Enforce for admins | **false** (see Admin merge procedure) |

The `Enforce for admins = false` setting is deliberate. The agent is
the sole reviewer, and the agent must be able to land a fix when CI
itself is broken (the situation that triggered this doctrine). Every
admin merge is logged in the PR ledger with a doctrinal justification.

## Admin merge procedure

An admin merge (`gh pr merge --admin`) is permitted only when **all**
of the following are true:

1. The CI failure is **not caused by the PR being merged**. The PR
   must run the failing job locally and demonstrate the failure is
   pre-existing on `main`.
2. The PR description records:
   - The exact failing job name + step + first error line
   - The most recent green CI run on `main` (or "never green since X"
     with the SHA of the regression-introducing commit)
   - The follow-up PR number that will re-green CI
3. The agent obtains explicit `confirm_action` approval from the user
   before invoking `gh pr merge --admin`. The default ask must
   summarise (1) and (2) so the user can refuse.
4. The follow-up PR from (2.c) lands within **3 calendar days** of
   the admin merge. If it slips, no further admin merges are
   permitted until CI is green.

Arc 9 C7 burned this procedure on six consecutive PRs (#72-#77) because
the CI workflow referenced six test files / AST targets that had been
removed or renamed in earlier sub-arcs. PR #78 re-greens CI by removing
the stale references and adds the Arc 9 C7.1 log-format contract test
to the gate.

## Applying the rule

Pre-requisites: `gh` CLI authenticated as a repo admin.

```bash
bash scripts/apply_branch_protection.sh
```

The script is idempotent. Running it on an unchanged repo is a no-op.

## Verification

```bash
gh api repos/aryanonline/Luciel/branches/main/protection \
  --jq '{required_status_checks, enforce_admins, required_pull_request_reviews}'
```
