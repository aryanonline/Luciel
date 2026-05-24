# Arc 7 — Commit 4 PROD DEPLOY RECORD

**Date:** 2026-05-24
**Operator:** Computer (agent), partner-delegated end-to-end
**Commit:** `75c946f` — arc7(c4): wire api_rate_limit_rpm from entitlements into rate_limit middleware (WU-2)
**Image tag:** `arc7-c4-75c946f`
**Image digest:** `sha256:57023527a6357f17d08363861a248473358ea7e5a808c75c04661ab1255e45c5`
**Image size:** 223.0 MB
**Outcome:** SUCCESS — backend serving git_sha=75c946f in prod, tier-aware rate-limit middleware live (free=30, pro=300, enterprise=3000 rpm)

---

## Slice-by-slice execution log

### S1 — Preflight ✅
- Working tree: clean post-commit `75c946f`
- Branch: `main`
- Alembic head unchanged from Commit 2 (`arc7_a_retire_billing_model`) — no migration this commit

### S2 — RDS snapshot ⏭️
- Skipped — no schema change in C4 (middleware only). Recovery anchor from C2 (`luciel-arc7-c2-pre-migrate-20260524-144631`) remains valid.

### S3 — Buildah image build ✅
- **Pattern:** `buildah bud --platform linux/amd64 --no-cache --build-arg BUILD_GIT_SHA=75c946f --tag luciel-backend:arc7-c4-75c946f`
- Disk pressure remediation prior: cleared `~/.local/share/containers/storage/vfs` + grep + pytest caches → 3.5G free → build succeeded
- Paranoid inspect: arch=amd64, user=luciel (non-root), `BUILD_GIT_SHA=75c946f` baked

### S4 — ECR push ✅
- **Registry:** `729005488042.dkr.ecr.ca-central-1.amazonaws.com`
- **Repo:** `luciel-backend`
- **Tag:** `arc7-c4-75c946f`
- **Digest:** `sha256:57023527a6357f17d08363861a248473358ea7e5a808c75c04661ab1255e45c5`
- **Pushed:** 2026-05-24 15:58:23 UTC

### S5 — Register `luciel-backend:88` ✅
- Cloned `luciel-backend:87`, swapped image only
- **Image:** `arc7-c2-77a76d0` → `arc7-c4-75c946f`
- **Secrets:** unchanged (no Stripe price change this commit)
- No tags / no propagateTags (TagResource scope)

### S6 — Register `luciel-worker:42` ✅
- Cloned `luciel-worker:41`, swapped image only
- **Image:** `arc7-c2-77a76d0` → `arc7-c4-75c946f`

### S7 — Rolling UpdateService ✅
- **Service name correction:** Commit 2 deploy record refers to `luciel-backend-service` / `luciel-worker-service`; initial UpdateService call here used unscoped names (`luciel-backend`/`luciel-worker`) and hit `AccessDeniedException`. Retried with the resource-scoped ARN names — passed cleanly.
- **Backend rollout:** t+163s — `state=COMPLETED`, 1/1 running, single deployment
- **Worker rollout:**  t+184s — `state=COMPLETED`, 1/1 running, single deployment
- **Smoke /version:**
  ```
  GET https://api.vantagemind.ai/api/v1/version
  → 200 OK
  → {"app":"Luciel Backend","version":"0.1.0","git_sha":"75c946f","status":"ok"}
  ```

---

## Post-deploy state

| Resource | Pre-deploy (C2 state) | Post-deploy (C4 state) |
| --- | --- | --- |
| Alembic head | arc7_a_retire_billing_model | arc7_a_retire_billing_model (unchanged) |
| Backend image | arc7-c2-77a76d0 | **arc7-c4-75c946f** |
| Worker image | arc7-c2-77a76d0 | **arc7-c4-75c946f** |
| Backend task def | luciel-backend:87 | **luciel-backend:88** |
| Worker task def | luciel-worker:41 | **luciel-worker:42** |
| Rate-limit middleware | constants-based (CHAT_RATE_LIMIT etc.) | **tier-aware (free=30/pro=300/enterprise=3000 rpm)** |
| Bucket key shape | `api_key_id` or `ip` | **`tier:{t}:admin:{aid}:inst:{iid|none}` / `ip:{ip}`** |
| Drift `D-pro-tier-rate-limit-abuse-surface-2026-05-23` | open | **closed** (per-admin × per-instance buckets) |

## What the deploy changes for traffic

- Authenticated requests are now bucketed by `tier:{tier}:admin:{admin_id}:inst:{instance_id|none}` — closes the cross-instance abuse surface from a single Pro admin (one of Arc 6's open drifts).
- Tier RPMs:
  - **Free:** 30 rpm
  - **Pro:** 300 rpm
  - **Enterprise:** 3,000 rpm
- Tier lookup: 60s TTL cache, 4,096-entry bound, fail-safe to Free on DB error (no fail-open).
- Anonymous traffic: keyed by client IP, capped at Free=30 rpm.

## Rollback plan (if needed)

1. `ecs update-service --service luciel-backend-service --task-definition luciel-backend:87`
2. `ecs update-service --service luciel-worker-service --task-definition luciel-worker:41`
3. No schema rollback needed (C4 is middleware/code only).

---

🟢 Arc 7 Commit 4 PROD DEPLOY: **COMPLETE**
Next: Commit 5 (`leads_per_month_cap` enforcement + `enterprise_overflow_archive` at 50k).
