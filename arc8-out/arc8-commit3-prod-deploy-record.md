# Arc 8 Commit 3 — Prod Deploy Record

**Date:** 2026-05-24
**Commit:** `5f83d9d` — `Arc 8 C3: Per-embed-key + per-instance rate-limit composition + bucket_scope reporting`
**Image tag:** `arc8-c3-5f83d9d`
**Image digest:** `sha256:efa227a1435d958670396ab3740c79231afe3f86021966398f3904271fdf3c56`
**Image size:** 222.98 MB
**Schema change:** None (C3 is schema-free; Alembic head unchanged at `arc7_b_admins_last_signup_ip`).
**Drift closed (code-side):** `D-pro-tier-rate-limit-abuse-surface-2026-05-23` (WU-3 abuse-surface).

---

## Deploy gate sequence (schema-free shape)

| Step | Action | Outcome | Duration |
| --- | --- | --- | --- |
| S1 | Pre-flight: git status clean on `main` at `5f83d9d`; secret-scan diff clean (no `sk_live`, `AKIA…`, `aws_secret`) | OK | <1s |
| S2 | RDS snapshot | SKIPPED (no schema change) | — |
| S3 | `buildah bud` against repo `Dockerfile` | Success on retry after `buildah prune --all --force` (first attempt ENOSPC at 93% disk) | 48.5s |
| S4 | `buildah push` to ECR `729005488042.dkr.ecr.ca-central-1.amazonaws.com/luciel-backend:arc8-c3-5f83d9d` | Success | 11.2s |
| S5–S7 | Alembic migrate | SKIPPED (no schema change) | — |
| S8 | Register backend task-def — clone `luciel-backend:91`, swap container `luciel-backend` image to `arc8-c3-5f83d9d` | `luciel-backend:93` registered (rev 92 was a misfired first attempt with the wrong container-name filter; left in registry, never deployed) | <2s |
| S9 | Register worker task-def — clone `luciel-worker:45`, swap container `luciel-worker` image to `arc8-c3-5f83d9d` | `luciel-worker:46` registered | <2s |
| S10 | `UpdateService` on both ECS services; poll until `rolloutState=COMPLETED` and `runningCount=desiredCount=1` for both | Backend COMPLETED at 139s; worker COMPLETED at 185s | 3 min |
| Post | Smoke triplet `/health` + `/ready` + `/api/v1/version` against `https://api.vantagemind.ai` | 200/200/200; `/ready` body `{status:ready, checks:{db:ok, redis:ok}}` | <1s each |

**Total deploy wall-time (S3 → smoke):** ~5–6 minutes.

---

## Live prod state (post-deploy)

| Resource | State |
| --- | --- |
| Backend service | `luciel-backend:93` on `arc8-c3-5f83d9d` — 1/1 RUNNING, rollout COMPLETED |
| Worker service | `luciel-worker:46` on `arc8-c3-5f83d9d` — 1/1 RUNNING, rollout COMPLETED |
| Alembic head | `arc7_b_admins_last_signup_ip` (unchanged — C3 schema-free) |
| Frontend | C8 `ffb7e18` (Luciel-Website, unchanged this commit) |
| `/health` | 200 `{status:ok, service:"Luciel Backend"}` |
| `/ready` | 200 `{status:ready, checks:{db:ok, redis:ok}}` |
| `/api/v1/version` | 200 `{app:"Luciel Backend", version:"0.1.0", git_sha:"unknown", status:"ok"}` (build-arg gap known) |

---

## What C3 actually changes in prod

The wire-level behaviour change is on the widget endpoint and the 429
response body:

* **Widget endpoint `POST /api/v1/chat/widget`** — pre-C3 used a shared
  static `30/minute` cap keyed by raw API key string. Post-C3 each
  minted embed key gets its own Redis bucket keyed by `api_keys.id`,
  with the per-key rpm derived dynamically from the tier matrix via
  `per_key_api_rate_limit_rpm(tier=...)`. By Option-A construction
  the derivation lands at 30rpm per key for every tier (Free 30/1,
  Pro 300/10, Enterprise 3000/100), but it now varies correctly with
  future tier-matrix revisions and isolates per-key burns.
* **Admin/chat surface** — composition rule already shipped at Arc 7
  C4 (per-(admin,instance) bucket). C3 adds the entitlement-derivation
  helper `per_instance_api_rate_limit_rpm(tier=...)` as a module-level
  function on `app/policy/entitlements.py`, so the per-tier per-instance
  cap is now derivable from a single call (Pro: 30rpm per Instance;
  Enterprise unlimited = identity 3000rpm).
* **429 response body** — now carries a stable `bucket_scope` field
  (`embed_key` / `tier_admin_instance` / `ip` / `unknown`). Pre-C3 the
  body was `{error, detail, message}`; post-C3 it adds the scope so
  clients and ops can distinguish which bucket emptied without parsing
  the SlowAPI exception text.

No customer is on Pro or Enterprise yet, so the live blast-radius of
C3 is purely defensive — the per-key bucket bounds a hypothetical
leaked-embed-key abuse class before any Pro customer onboards.

---

## Validation (closure)

* `tests/security/test_rate_limit_per_key_composition.py` — 30/30
  passing (new C3 contract: per-tier derivation, 1rpm floor, override
  hook, per-key key-func + limit-provider, `_classify_bucket_scope`,
  `bucket_scope` on 429, end-to-end per-key isolation, end-to-end
  per-key derived cap firing at 30rpm not 300rpm).
* `tests/security/test_rate_limit_tier_aware.py` — 14/14 passing
  (no regression on the per-(admin,instance) tier composition).
* `tests/security/test_rate_limit_failmode.py` — 18/18 passing
  (no regression on the fail-mode + fallback middleware that wraps
  the new handler).
* `tests/policy/` — 8/8 passing.
* `tests/api/test_widget_e2e_harness_shape.py` — 32/32 passing
  (widget endpoint shape preserved across the decorator swap).
* Full `from app.main import app` import → 95 routes register clean.
* Live `/ready` returns 200 with `{db:ok, redis:ok}` confirming both
  the new revisions cleanly exercise the C1 readiness path.

Pre-existing failures **not** introduced by C3 and confirmed unrelated
by re-running against `main` pre-edit (same set as documented in the
C2 record):

* 5 in `tests/middleware/test_actor_user_id_binding.py` — references
  `app.middleware.auth.AgentRepository` which does not exist on this
  module; carry-over from a prior refactor.
* 40 in `tests/api/test_step24_5c_*.py`, `test_step30a_2_*.py`,
  `test_step31_*.py` — shape-test debt.
* 2 in `tests/api/test_arc6_signup_free.py` — need real Postgres.

---

## Drift status

`D-pro-tier-rate-limit-abuse-surface-2026-05-23` — Closure-evidence
stanza appended to `docs/DRIFTS.md` in this commit. Strikethrough on
the heading is deferred to the Arc 8 C7 envelope-close sweep, per the
established Arc 8 pattern (C1 + C2 closure stanzas also deferred their
strikethrough to C7). The resolution-path items addressed:

| Item | Status |
| --- | --- |
| 1. Per-key bucket | **LANDED** — `get_embed_key_aware_key` + `get_embed_key_rate_limit_for_key` + widget decorator swap. |
| 2. Per-instance bucket | **LANDED** — routing landed at Arc 7 C4; entitlement-derivation helper landed at Arc 8 C3. |
| 3. Per-seat bucket | **DEFERRED** — dashboard-only surface, no programmatic abuse path. Will land when a paying customer crosses 5 seats or reports a symptom. |
| 4. Composition rule | **LANDED** — implicit in the key-shape (widget surface fires per-key, admin/chat fires per-(admin,instance)); `bucket_scope` on 429 makes the dimension visible to clients/ops. |
| 5. Audit hook | **DEFERRED** — `bucket_scope` on 429 gives ops the bucket dimension without the audit-row throttling complexity; the `RATE_LIMIT_EXCEEDED` audit-row can land with the next audit-chain expansion. |
| 6. Doctrine alignment | **LANDED** — `app/policy/entitlements.py` exposes the two helpers as module-level functions (not frozen-dataclass fields, which would break every existing `TierEntitlement(...)` constructor). |

---

## Arc 8 progress after C3

| # | Work unit | Drift closed | Status |
| --- | --- | --- | --- |
| C1 | `/ready` endpoint | `D-health-endpoint-shallow-no-db-readiness-check-2026-05-22` | ✅ deployed `arc8-c1-a0d304b` |
| C2 | Stripe checkout email-deliverability + premint kwarg fix | `D-stripe-checkout-no-email-validation-2026-05-18` + `D-tier-provisioning-tenant-id-kwarg-mismatch-2026-05-24` | ✅ deployed `arc8-c2-08fd4ff` |
| C3 | Per-key + per-instance rate-limit composition + `bucket_scope` | `D-pro-tier-rate-limit-abuse-surface-2026-05-23` | ✅ deployed `arc8-c3-5f83d9d` (this record) |
| C4 | Fargate in-cluster smoke probe task-def | `D-no-internal-smoke-path-for-direct-alb-2026-05-22` | ⏳ next |
| C5 | Partner-Console packet | n/a | ⏳ |
| C6 | E2E test plan runbook | n/a | ⏳ |
| C7 | Envelope close — tag `arc-8-pre-e2e-hardening-complete` | n/a | ⏳ |

**Three drifts closed in Arc 8 so far (code-side); four to go (C4 closes one more, C5–C7 are packaging/documentation).**
