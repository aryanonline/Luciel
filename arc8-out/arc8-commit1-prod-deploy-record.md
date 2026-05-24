# Arc 8 — Commit 1 Prod Deploy Record (WU-1 /ready readiness endpoint)

**Date:** 2026-05-24 (Sunday)
**Wave:** WU-1 (Reliability)
**Image:** `arc8-c1-a0d304b`
**Digest:** `sha256:dc1c51d4a32250440550c2eac5fe5c85bdca85171bc798bd0703e9d1f746230a`
**Size:** 223.0 MB

## Doctrine

Arc 8 opens with a reliability separation: /health stays a pure liveness
probe (ALB target-group binding — a transient Redis/RDS blip must not
remove a healthy task from rotation), and /ready is the richer signal
consumed by deploy-gate smoke probes (Arc 8 C4), uptime monitors, and
human operators investigating customer-reported slowdowns.

Closes the long-standing `D-health-endpoint-shallow-no-db-readiness-check-2026-05-22`
drift and lays the foundation for Arc 8 C4's in-cluster Fargate
deploy-gate smoke probe (Option B from `arc7-internal-alb-smoke-path.md`).

## Bundle Contents

### C1 — /ready endpoint
- Backend `a0d304b` (aryanonline/Luciel): `/ready` route added to `app/main.py`
  with DB (SELECT 1 via shared engine) + Redis (PING via one-shot client,
  1.0s socket timeouts so a slow probe never starves the limiter pool)
  probes. Returns 200 `{"status":"ready","checks":{...}}` on success;
  503 `{"status":"not_ready","checks":{...}}` on failure. Failure body
  reports only exception class names — never the underlying message
  (which can carry connection strings).
- `/health` preserved verbatim with expanded docstring clarifying the
  liveness-vs-readiness split.
- `app/middleware/auth.py`: `/ready` added to `SKIP_AUTH_PATHS` so the
  deploy-gate Fargate probe (Arc 8 C4) and uptime monitors can hit it
  without holding a JWT.
- Tests: `tests/api/test_ready.py` 4/4 green (happy path, DB-failure
  503 + class-name reporting + message non-leak, Redis-failure 503 +
  class-name reporting, SKIP_AUTH exemption).
- No schema change. No SSM mutation. No IAM expansion. Purely additive
  route + middleware allowlist entry.

## Deploy Sequence (S1–S10 — schema-free shape)

| Step | Action | Result |
|---|---|---|
| S1 | Pre-flight prod state snapshot | backend:89 / worker:43 on `arc7-c6-173e4cb` 1/1 stable; alembic head `arc7_b_admins_last_signup_ip` |
| S2 | RDS snapshot | SKIPPED — C1 has no schema change |
| S3 | Build `arc8-c1-a0d304b` via buildah linux/amd64 | OK, paranoid inspect arch=amd64, User=luciel (uid 10001), BUILD_GIT_SHA=a0d304b |
| S4 | ECR push | digest `sha256:dc1c51d4a32250440550c2eac5fe5c85bdca85171bc798bd0703e9d1f746230a`, size 233.9 MB |
| S5 | Register `luciel-migrate:*` with alembic upgrade head | SKIPPED — no migration |
| S6 | RunTask migrate | SKIPPED — no migration |
| S7 | Schema probe | SKIPPED — no migration |
| S8 | Register `luciel-backend:90` cloning `:89` swapping image to `arc8-c1-a0d304b` | OK |
| S9 | Register `luciel-worker:44` cloning `:43` swapping image | OK |
| S10 | `UpdateService` on `luciel-backend-service` + `luciel-worker-service`, rolling | both COMPLETED ~2.5 min; smoke triplet (`/api/v1/version`, `/health`, `/ready`) all green |

## Smoke Evidence (post-deploy)

```
$ curl -sf https://api.vantagemind.ai/api/v1/version
{"app":"Luciel Backend","version":"0.1.0","git_sha":"a0d304b","status":"ok"}

$ curl -sf https://api.vantagemind.ai/health
{"status":"ok","service":"Luciel Backend"}

$ curl -s -w "HTTP %{http_code}\n" https://api.vantagemind.ai/ready
HTTP 200
{"status":"ready","checks":{"db":"ok","redis":"ok"}}
```

`/ready` 200 + `{db:ok, redis:ok}` proves end-to-end reachability: ALB →
backend container → RDS Postgres → ElastiCache Redis. This is the first
deploy where the deploy-gate has a true end-to-end signal beyond ECS
task-state + target-group health-check + customer-domain probe.

## Final Prod State

| Resource | Live |
|---|---|
| Alembic head | `arc7_b_admins_last_signup_ip` (unchanged — C1 schema-free) |
| Backend | `luciel-backend:90` on `arc8-c1-a0d304b` 1/1 stable |
| Worker | `luciel-worker:44` on `arc8-c1-a0d304b` 1/1 stable |
| `/health` | liveness probe — 200 OK |
| `/ready` | readiness probe — 200 OK {db:ok, redis:ok} |
| Frontend | C8 `ffb7e18` (Luciel-Website, unchanged) |

## Drift Status Update

- `D-health-endpoint-shallow-no-db-readiness-check-2026-05-22` →
  CLOSURE EVIDENCE LANDED. Strikethrough deferred to Arc 8 C7 envelope
  close (the C7 commit applies the strikethrough + §5 closure stanza
  along with all other Arc-8-closed drifts).

## Carry-Forward to C2

- C2 wires `email_validator.validate_email(..., check_deliverability=True)`
  into `tier_provisioning_service` pre-mint, closing
  `D-stripe-checkout-no-email-validation-2026-05-18`. No deploy-shape
  surprises — same additive-only deploy posture as C1.

## Snags Cleared

None this commit. Buildah + ECR push + ECS rolling-update pattern from
Arc 7 C4/C6 held clean. The schema-free shape (no S2/S5/S6/S7) shaves
~3 minutes off the deploy cycle versus the Arc 7 C2/C6 schema-touch
shape.
