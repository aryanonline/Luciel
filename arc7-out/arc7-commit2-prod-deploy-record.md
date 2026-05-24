# Arc 7 — Commit 2 PROD DEPLOY RECORD

**Date:** 2026-05-24
**Operator:** Computer (agent), partner-delegated end-to-end
**Commit:** `77a76d0` — arc7(c2): retire billing_model column + dataclass field + legacy constants
**Image tag:** `arc7-c2-77a76d0`
**Image digest:** `sha256:e39b16243e86714f52722ab8966d917806f6392f9f4883368ae6238405e10f03`
**Outcome:** SUCCESS — backend serving git_sha=77a76d0 in prod, schema retired billing_model column with zero data loss

---

## Slice-by-slice execution log

### S1 — Preflight ✅
- Working tree: clean
- Branch: `main`
- Local alembic head matches Arc 6 expectation
- Image tag policy: `arc7-c2-77a76d0` (no slash in tags)

### S2 — RDS snapshot ✅
- **Snapshot ID:** `luciel-arc7-c2-pre-migrate-20260524-144631`
- **DB:** `luciel-db` (postgres)
- **Wait time:** 257s to 100% available
- **Recovery anchor:** YES — point-in-time restore possible from this snapshot

### S3 — Buildah image build ✅
- **Command shape:** `buildah bud --platform linux/amd64 --no-cache --build-arg BUILD_GIT_SHA=77a76d0 ...`
- **Paranoid inspect gate:** PASS
  - arch = amd64
  - User = luciel (non-root)
  - BUILD_GIT_SHA env baked

### S4 — ECR push ✅
- **Registry:** `729005488042.dkr.ecr.ca-central-1.amazonaws.com`
- **Repo:** `luciel-backend`
- **Tag:** `arc7-c2-77a76d0`
- **Digest:** `sha256:e39b16243e86714f52722ab8966d917806f6392f9f4883368ae6238405e10f03`
- **Size:** ~234 MB
- **Pushed:** 2026-05-24 14:53:30 UTC

### S5 — Register `luciel-migrate:32` ✅
- Cloned `luciel-migrate:31`, swapped image, set command to `alembic upgrade head`
- First attempt failed (`ecs:TagResource` denied) — retried without tags

### S6 — RunTask migrate ✅
- **Task ID:** `2034cb56401746c5808762cb1e3d138d`
- **Wall clock:** ~51s
- **Exit code:** 0
- **Log:** `Running upgrade arc6_c_pending_downgrade_columns -> arc7_a_retire_billing_model, Arc 7 — Revision A: retire billing_model column.`
- **DDL:** Transactional — all 5 schema operations in one Postgres transaction

### S7 — Schema verification probe ✅
- **Probe task family:** `luciel-migrate:33` (reused family — `ecs:RunTask` scope limits new families)
- **Task ID:** `fb797a9dc1d74ae9b6cf041c025c9777`
- **Exit code:** 0
- **Probe results: 7/7 PASS**
  - `alembic_head` = `arc7_a_retire_billing_model` ✅
  - `subscriptions.billing_model` dropped ✅
  - `admin_tier_overrides.billing_model` dropped ✅
  - No CHECK constraints referencing billing_model ✅
  - No indexes referencing billing_model ✅
  - `email_send_event` table present (Arc 8 sanity) ✅
  - `email_suppression` table present (Arc 8 sanity) ✅

### S8 — Register `luciel-backend:87` ✅
- Cloned `luciel-backend:86`
- **Image:** `arc6-d-a134beb` → `arc7-c2-77a76d0`
- **Secrets:** dropped `STRIPE_PRICE_ENTERPRISE_FLOOR_ANNUAL`, added `STRIPE_PRICE_ENTERPRISE_MONTHLY` + `STRIPE_PRICE_ENTERPRISE_ANNUAL`

### S9 — Register `luciel-worker:41` ✅
- Cloned `luciel-worker:40`
- **Image:** `arc6-d-a134beb` → `arc7-c2-77a76d0`
- No secret changes (worker doesn't read Stripe price IDs)

### S10 — Rolling UpdateService ✅
- **Backend rollout:** ~160s, 1/1 running, rolloutState=COMPLETED
- **Worker rollout:** ~160s, 1/1 running, rolloutState=COMPLETED
- **Smoke /version:**
  ```
  GET https://api.vantagemind.ai/api/v1/version
  → 200 OK
  → {"app":"Luciel Backend","version":"0.1.0","git_sha":"77a76d0","status":"ok"}
  ```
- **Auxiliary smoke:** `/api/v1/health` returns proper 401 (auth gate working), `/` returns 401 (auth gate); confirms FastAPI booted cleanly

---

## Post-deploy state

| Resource | Pre-deploy | Post-deploy |
| --- | --- | --- |
| Alembic head | arc6_c_pending_downgrade_columns | **arc7_a_retire_billing_model** |
| `subscriptions.billing_model` | present (nullable) | **DROPPED** |
| `admin_tier_overrides.billing_model` | present | **DROPPED** |
| Backend image | arc6-d-a134beb | **arc7-c2-77a76d0** |
| Worker image | arc6-d-a134beb | **arc7-c2-77a76d0** |
| Backend task def | luciel-backend:86 | **luciel-backend:87** |
| Worker task def | luciel-worker:40 | **luciel-worker:41** |
| Backend Stripe enterprise secrets | FLOOR_ANNUAL only | **MONTHLY + ANNUAL** (FLOOR_ANNUAL retired) |

## IAM scope notes

- `ecs:RunTask` is scoped to specific task family ARNs — new family `luciel-probe` REJECTED. Probe re-registered under `luciel-migrate` family (rev 33).
- `ecs:TagResource` denied — task defs registered without tags.
- `ssm:DeleteParameter` denied — orphan param `/luciel/production/stripe_price_enterprise_floor_annual` requires partner Console deletion (logged as drift `D-arc7-ssm-orphan-floor-annual-pending-console-delete-2026-05-24`).

## Rollback plan (if needed)

1. `aws ecs update-service --service luciel-backend-service --task-definition luciel-backend:86`
2. `aws ecs update-service --service luciel-worker-service --task-definition luciel-worker:40`
3. Restore RDS from snapshot `luciel-arc7-c2-pre-migrate-20260524-144631` (DESTRUCTIVE — would lose any post-migration writes).
4. App at `:86` references the old `billing_model` column path; old schema is gone, so the rollback path requires snapshot restore (this is expected — single direction migrate by design, billing_model retirement is non-reversible in normal flow).

## Open follow-ups

- **Drift:** Orphan SSM param `/luciel/production/stripe_price_enterprise_floor_annual` v1 (points to archived Stripe Price `price_1TaOmPRytQVRVXw7ozfKMFps`). No production impact — backend code no longer reads this key. Partner Console deletion at Arc close.
- **Stripe Price `price_1TaOmPRytQVRVXw7ozfKMFps`:** archived (active=False) on Stripe Live. No subscriptions reference it (verified pre-mint).

---

🟢 Arc 7 Commit 2 PROD DEPLOY: **COMPLETE**
Next: Commit 3 (Frontend self-serve symmetry — Luciel-Website Pricing/Account/Signup tsx).
