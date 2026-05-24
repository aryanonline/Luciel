# Arc 7 — Commit 6 Prod Deploy Record (bundled C5 + C6)

**Date:** 2026-05-24 (Sunday)
**Wave:** WU-2 (signup fraud surface) + WU-2 retirement sweep (leads-cap removal)
**Image:** `arc7-c6-173e4cb`
**Digest:** `sha256:0090a7107e0421b9ea13280fd01996c7b279020cc21c4644d5f596fcce5bd94e`
**Size:** 222.9 MB
**Recovery snapshot:** `luciel-arc7-c6-pre-migrate-20260524-162103`

## Doctrine

Arc 7 keeps a flat-recurring tier shape with no metering and no overflow billing.
`api_rate_limit_rpm` (C4 tier-aware middleware) is the abuse boundary on the request surface.
C6 introduces **a second, narrower boundary on the signup surface**: per-IP signup throttle on `signup_free` only — Free is the only attacker-discoverable on-ramp without payment friction.

## Bundle Contents

### C5 — Retire `leads_per_month_cap` (Option 1)
- Backend `8068740` (aryanonline/Luciel): deleted `leads_per_month_cap` from `TierEntitlement` dataclass + all 3 tier rows; rewrote doctrine comments; `app/core/config.py` + `app/services/billing_service.py` comments name `api_rate_limit_rpm` as abuse boundary.
- Frontend `4ce82bd` (aryanonline/Luciel-Website): "X leads/month" copy purged from `Pricing.tsx`, `Signup.tsx`, `SignupFree.tsx`.
- Deferred: `admin_tier_overrides.leads_per_month_override` column kept in schema for Arc 8 schema sweep (no longer read).
- 98 tests green (38 policy + 17 tier-aware + 60 services).

### C6 — `admins.last_signup_ip` + 1-per-IP soft gate
- Migration `arc7_b_admins_last_signup_ip` — adds `admins.last_signup_ip` Postgres `INET` nullable + partial index `ix_admins_last_signup_ip WHERE last_signup_ip IS NOT NULL AND active = true`. `down_revision = arc7_a_retire_billing_model`.
- Model `app/models/admin.py` — `last_signup_ip: Mapped[str | None]` with `INET()` dialect type; index OWNED by migration.
- Route `app/api/v1/billing.py:signup_free`:
  - Soft gate BEFORE captcha verify — `recent_same_ip >= 1` in last 24h → HTTP 429
  - Stamp `admin.last_signup_ip = remote_ip` AFTER `onboard_tenant` succeeds, BEFORE pre-mint
  - Fail-open on `remote_ip is None`
  - Free-only (paid Stripe flows leave NULL on purpose)
- Tests `tests/api/test_arc7_c6_signup_ip_gate.py` — 13/13 green (model shape, migration anchor, route source pins, gate decision table, paid-checkout absence-test).

## Deploy Sequence (S1–S10)

| Step | Action | Result |
|---|---|---|
| S1 | Pre-flight prod state snapshot | backend:88 / worker:42 on `arc7-c4-75c946f` 1/1 stable; alembic head `arc7_a_retire_billing_model` |
| S2 | RDS snapshot | `luciel-arc7-c6-pre-migrate-20260524-162103` available |
| S3 | Build `arc7-c6-173e4cb` via buildah linux/amd64 | OK, paranoid inspect arch=amd64, User=luciel, BUILD_GIT_SHA=173e4cb |
| S4 | ECR push | digest `sha256:0090a7107e0421b9ea13280fd01996c7b279020cc21c4644d5f596fcce5bd94e` |
| S5 | Register `luciel-migrate:34` with `alembic upgrade head` on new image | OK |
| S6 | RunTask migrate | exit 0; alembic advanced `arc7_a_retire_billing_model` → `arc7_b_admins_last_signup_ip` |
| S7 | Schema probe via `luciel-migrate:35` (inline python `-c`) | PASS — column `udt_name=inet` `is_nullable=YES`, partial index predicate `last_signup_ip IS NOT NULL AND active = true`, alembic head correct |
| S8 | Register `luciel-backend:89` cloning `:88` swapping image to `arc7-c6-173e4cb` | OK |
| S9 | Register `luciel-worker:43` cloning `:42` swapping image | OK |
| S10 | `UpdateService` on `luciel-backend-service` + `luciel-worker-service`, rolling | both COMPLETED ~3 min; smoke `/api/v1/version` → `git_sha=173e4cb` |

## Final Prod State

| Resource | Live |
|---|---|
| Alembic head | `arc7_b_admins_last_signup_ip` |
| Backend | `luciel-backend:89` on `arc7-c6-173e4cb` 1/1 stable |
| Worker | `luciel-worker:43` on `arc7-c6-173e4cb` 1/1 stable |
| `admins.last_signup_ip` | INET, nullable, present |
| `ix_admins_last_signup_ip` | partial index live (predicate: NOT NULL AND active=true) |
| Frontend | C5 `4ce82bd` deployed via Amplify auto-build |

## Abuse Surface Summary (post-C6)

| Surface | Boundary | Tunable |
|---|---|---|
| API requests | `api_rate_limit_rpm` per-(tier,admin,instance) token bucket | tier table (free=30/pro=300/ent=3000) |
| Signup-Free | 1-per-IP 24h soft gate, INET-typed | hardcoded `recent_same_ip >= 1`, 24h window |

Paid Stripe flows intentionally leave `last_signup_ip` NULL — payment is itself the friction.

## Carry-Forward Drifts

- `D-arc7-ssm-orphan-floor-annual-pending-console-delete-2026-05-24` — orphan SSM param `/luciel/production/stripe_price_enterprise_floor_annual` v1, partner Console deletion scheduled at Arc 7 close.

## Snags Cleared

None this commit. Pattern from C2/C4 build/push/register/UpdateService held clean. S7 probe via inline `python -c` (no rebuild) reused proven approach.
