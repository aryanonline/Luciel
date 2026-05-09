# Luciel — Drifts (Open + Resolved)

**Scope:** Every named drift token, every closure, every supersession, every disclosure.
**Out of scope:** Business value (`CANONICAL_RECAP.md`), code/AWS topology (`ARCHITECTURE.md`).

**Doctrine:** Every fix lands as one named drift token + one named commit + a verify gate. Tokens are immutable once recorded. Supersession is captured via a new entry that references the prior token, never by editing. Source-of-truth precedence: code > commit > recap > prior recaps > chat. Hashes here are pointers; if a hash and `git log` disagree, `git log` wins.

**Maintenance protocol:** Surgical edits only. New tokens append. Status changes edit the row in a new commit (the commit message must reference the token). Resolved tokens are kept (with strikethrough on the token cell) so future operators can see the full history.

**Last updated:** 2026-05-08 (Step 29.y close-out)

---

## §1 Schema

| Column | Meaning |
|---|---|
| Token | Drift token, format `D-<slug>-YYYY-MM-DD` |
| Commit | Hash on the branch where the fix landed (or `_deferred_` / `_pending_`) |
| Status | `closed` (fix landed + verified), `open` (acknowledged, not yet fixed), `deferred` (carry-forward to a future step), `superseded-by-<token>` |
| Pillar / Cluster | Verification pillar or cluster the token belongs to, if any |
| Summary | One-line description; full rationale lives in the commit body |

---

## §2 Open / Deferred (forward-looking)

These are the gaps the platform knowingly carries forward. Each has a target step.

| Token | Target step | Pillar / Cluster | Summary |
|---|---|---|---|
| `D-step29y-impl-no-close-tag-2026-05-07` | Step 29.y close (this commit cycle) | meta | No `step-29y-complete` tag exists yet; closes when tag is created on main after merge |
| `D-canonical-recap-v3.4-omits-step-29x-29y-2026-05-07` | Step 29.y close (this commit cycle) | meta | Old canonical recap omitted Step 29.x and 29.y; closes when this 3-doc regime ships and old recap moves to `docs/archive/` |
| `D-no-alembic-head-vs-rds-startup-check-2026-05-08` | Step 30 | meta / deploy process | Add backend startup health-check comparing image's `alembic heads` vs `SELECT version_num FROM alembic_version`; refuse traffic or alarm on mismatch. Structural fix for RC-3 of the prod-RDS-two-migrations-behind drift. |
| `D-prod-ops-role-cannot-list-or-remove-ssm-tags-2026-05-08` | Step 30 | meta / IAM | Cosmetic: `luciel-ecs-prod-ops-role` lacks `ssm:ListTagsForResource` and `ssm:RemoveTagsFromResource`. Leftover `luciel:iam-probe=ok` tag from 2026-05-08 12:27 EDT IAM-propagation probe cannot be removed. Harmless. |
| `D-backend-service-no-autoscaling-2026-05-08` | Step 30 | meta / capacity planning | Backend service has no scalable target. Deliberate per Step 28 ALB-fronted steady-state design. Worker autoscaling IS live (CFN `luciel-prod-worker-autoscaling`, CPU 60%, capacity 1–4) since 2026-05-05. Add backend autoscaling once real customer traffic patterns exist to size against. |
| `D-actor-permissions-storage-format-migration-step-30b-2026-05-07` | Step 30b | meta | Actor permissions stored as untyped JSONB; migrate to typed format. Full column rewrite + chain recompute requires prod migration + Pattern N alignment; outside code-only window. |
| Cluster 4b items (E-6 prod IaC, E-12 cross-region replication, E-13 DR runbook) | Step 30b | Cluster 4b | Prod IaC, cross-region replication, DR runbook; outside code-only window. |
| `D-pre-launch-validation-gate-not-on-roadmap-2026-05-08` | Step 31 | meta / product | Five-tier validation gate (isolation / customer journey / memory quality / ops readiness / compliance) not yet a roadmap step. Closes when Step 31 spec lands. See `CANONICAL_RECAP.md` §1.1. |

**Per-pillar product-intent notes (not yet drift tokens — captured here for Step 31 spec):**

- **Per-agent leak attack-test:** schema enforces agent_id, but no live attack-test exists analogous to P13 A5. Step 31 Tier 1.
- **Per-domain scoping:** `domain` is not a column on `memory_items` today. Either add column or scope-via-agent-domain-mapping. Step 31 Tier 1.
- **Per-luciel-instance coverage:** schema FK exists, weak verification coverage. Step 31 Tier 1.

---

## §3 Closed — Step 29.y gap-fix series (2026-05-07)

Branch: `step-29y-gapfix` (forked from `step-29y-impl`).

| Token | Commit | Status | Pillar / Cluster | Summary |
|---|---|---|---|---|
| ~~`D-actor-permissions-comma-fragility-2026-05-07`~~ | `1099f4f` | closed | P23 | JSON-serialize new `actor_permissions` writes; dual-format read; historical rows untouched to preserve hash chain. |
| ~~`D-audit-note-length-unbounded-2026-05-07`~~ | `30878b0` | closed | P23 | Cap audit `note` at 256 chars at `AdminAuditRepository.record()` boundary with truncation marker. |
| ~~`D-scope-policy-action-class-gap-2026-05-07`~~ | `5d99db8` | closed | P14 | Add `ScopePolicy.enforce_action(...)` primitive; no callers wired in this commit. |
| ~~`D-worker-audit-write-failure-not-alerted-2026-05-07`~~ | `491d427` | closed | P23 | `WORKER_AUDIT_WRITE_FAILED` structured log marker + thread-safe process-local failure counter. |
| ~~`D-findings-docs-not-in-repo-2026-05-07`~~ | `d2572db` | closed | meta | Reconstruct `docs/findings/` index for phases 1b/1d/1e/1f/1g from code + commit history. (Note: this folder is now in `docs/archive/findings/` after Step 29.y close-out.) |
| ~~`D-cluster-4b-deferral-undocumented-2026-05-07`~~ | `4c8522a` | closed | Cluster 4b | Cluster 4b deferrals (E-6 / E-12 / E-13) recorded; carry-forward listed in §2 above. |
| ~~`D-cluster-7-unaccounted-2026-05-07`~~ | (cluster commit) | closed | Cluster 7 | Cluster 7 has no commits, tests, or code citations on `step-29y-impl`; investigated, no evidence on branch; logged here as canonical disposition. |
| ~~`D-historical-rate-limit-typo-disclosure-2026-05-07`~~ | C19 | closed | P11 | Disclosure of the `REDISURL` (no underscore) typo. Pre-29.y the SlowAPI middleware read `os.getenv("REDISURL")`, which always resolved to None, falling through to `memory://`. Per-route limits were per-process across N backend containers — a tenant on `100/min` could effectively achieve `100*N/min`. Corrected in `7e783a5`. Full disclosure in §6 below (DISC-2026-001). |
| ~~`D-audit-verification-harness-retry-duplicates-2026-05-07`~~ | C11 (`3798b32`) + C12 | closed | Cluster 4 (E-2) | 223 verification-harness duplicate `worker_*` audit rows blocked migration `d8e2c4b1a0f3`. First attempt deleted 166 rows but broke 60 hash-chain links. Reverted via CSV restore; migration redesigned forward-only. Full disclosure §6 (DISC-2026-003). |
| ~~`D-cluster4-e2-rework-as-forward-only-2026-05-07`~~ | C12 | closed | Cluster 4 (E-2) | Migration `d8e2c4b1a0f3` redesigned to add `AND created_at >= TIMESTAMPTZ '2026-05-08 04:00:00+00'` to its partial UNIQUE index; preserves Pattern E without exception. |
| ~~`D-celery-app-not-imported-on-uvicorn-boot-2026-05-07`~~ | C13 (`0922cdf`) | closed | P25 | `app/main.py` did not import `app.worker.celery_app`, so `@shared_task` on `extract_memory_from_turn` bound to Celery's default `current_app` whose default broker is `amqp://guest@localhost//`. Fix: import in `app/main.py` (canonical) and `app/memory/service.py` (defense in depth). |
| ~~`D-pillar14-consent-grant-uses-wrong-tenant-key-2026-05-07`~~ | C14 (`0b6f913`) | closed | P14 | `pillar_14_departure_semantics.py` issued T2 consent grant with `k1_raw` while specifying `tenant_id=t2_id`; correctly rejected as cross-tenant. Test bug. Fix: swap `k1_raw → k2_raw`. |
| ~~`D-pillar13-spoof-audit-poll-too-short-2026-05-07`~~ | C15 (`cf69393`) + C16 (`128ce83`) | superseded-by-`D-pillar13-action-constant-case-divergence-2026-05-08` | P13 | Initially diagnosed as poll-too-short under `--pool=solo` Windows dev contention; widened to 180s. Real cause was action-constant case divergence (lowercase canonical vs uppercase test literal); see C18. |
| ~~`D-pillar13-action-constant-case-divergence-2026-05-08`~~ | C18 | closed | P13 | Test re-declared audit-action constant locally as `"WORKER_IDENTITY_SPOOF_REJECT"` (uppercase) while model defines canonical `"worker_identity_spoof_reject"` (lowercase). PostgreSQL `text` is case-sensitive; test polled 180s with no row found while row was always present. Fix: import `ACTION_WORKER_IDENTITY_SPOOF_REJECT` from `app.models.admin_audit_log` so divergence becomes compile-time error. Revert poll budget to 90s. |
| ~~`D-redis-url-centralize-via-settings-2026-05-08`~~ | C19 | closed | meta / architecture | `REDIS_URL` historically read in 4 separate locations; 3 sites used `os.environ.get("REDIS_URL", "redis://localhost:6379/0")` directly, bypassing `Settings`. Same drift class as the original `REDISURL` typo. Fix: route all readers through `app.core.config.settings.redis_url`. CELERY_BROKER_URL stays a direct env read because it is broker-selection state. |
| ~~`D-celery-app-set-default-or-import-order-2026-05-08`~~ | C17 (`2f73121`) | closed | P25 | After branch consolidation, P25 regressed to FAIL (`OperationalError [WinError 10061]` to `pyamqp/5672`). C13 import-on-boot is necessary but not sufficient; under uvicorn another import path can touch `celery.current_app` first, leaving the default at the stock instance. Fix: `celery_app.set_default()` after `Celery(...)` constructor. Idempotent and import-order-independent. |

---

## §4 Closed — Step 29.y close-out series (2026-05-08)

Branch: `step-29y-gapfix` (continuing).

| Token | Commit | Status | Pillar / Cluster | Summary |
|---|---|---|---|---|
| ~~`D-prod-ops-role-missing-ssm-tags-2026-05-08`~~ | C23 (`bc8b269`) | closed | meta / IAM | `luciel-ecs-prod-ops-role` policy `WriteProdPlatformAdminKey` granted `ssm:PutParameter` but not `ssm:AddTagsToResource`. boto3's `put_parameter(Tags=[...])` issues both API calls, so any rotation requesting tags would 403 on the tag write while value-write succeeded. Fix: add `ssm:AddTagsToResource` to the same Sid, scoped to single-resource ARN. |
| ~~`D-ssm-write-overwrite-false-blocks-rotation-2026-05-08`~~ | C24 (`bc8b269`) | closed | meta / key rotation | `_write_key_to_ssm` issued `put_parameter(Overwrite=False, Tags=[...])`, the bootstrap-only contract. Production rotation needs `Overwrite=True`. AWS API also forbids `Tags` alongside `Overwrite=True`, so the rotation path is split: put_parameter then add_tags_to_resource. Bootstrap path keeps original single-call contract. Tag-write failures logged-but-non-fatal. |
| ~~`D-audit-chain-listener-only-in-app-main-2026-05-08`~~ | C25 | closed | P23 | `install_audit_chain_event()` was registered only by `app/main.py` and `app/worker/celery_app.py` at process boot. Code paths importing `SessionLocal` directly without importing `app.main` (operator heredocs, one-off scripts) constructed sessions whose flushes did NOT trigger the chain handler — silently writing audit rows with NULL `row_hash` / `prev_row_hash`. Fix: install listener in `app/db/session.py` at module-import time. Every ORM code path imports `SessionLocal`, so "forgot to install the listener" becomes impossible. |
| ~~`D-audit-row-3445-hash-backfilled-2026-05-08`~~ | C25 | closed | P23 | During Phase E platform-admin-key consolidation, ad-hoc heredoc minted api_keys row 597 + audit row 3445 without listener registered (heredoc imported `SessionLocal` directly without `app.main`). Row 3445 backfilled by hand by computing `canonical_row_hash(_row_to_dict(row), prev_hash=row_3444.row_hash)` exactly as listener would have done; meta-audit row 3446 documents the backfill. Audit chain end-to-end walk passes. |
| ~~`D-prod-rds-two-migrations-behind-deployed-code-2026-05-08`~~ | C28 | closed | meta / deploy / P23 (latent) | Two alembic migrations sat in-repo for ~14h45m without being applied to prod RDS while application code depending on the schema was deployed. `c5d8a1e7b3f9` (audit_logs hash NOT NULL) and `d8e2c4b1a0f3` (worker-reject idempotency unique index). Applied at 2026-05-08 14:19 EDT. Latent risks did not materialize because: (a) hash columns were nullable during the gap so the row 3445 NULL-hash incident was hand-recoverable; (b) the worker-reject idempotency window was small. Structural fix tracked as open token in §2. |
| ~~`D-task-def-registry-bloat-2026-05-08`~~ | C29 | closed | meta / operational hygiene | Pre-cleanup: 84 ACTIVE task defs across 9 families. Only latest revision of each family was referenced. 82 deregister-task-definition calls applied 2026-05-08 14:39–14:48 EDT (47 in Phase 1: backend rev 1–33 + worker rev 2–13 + prod-ops rev 1–2; 35 in Phase 2: grant-check rev 1–3 + migrate rev 1–13 + mint rev 1–3 + smoke rev 1 + verify rev 1–12 + 14–16). All returned INACTIVE cleanly. |
| ~~`D-orphaned-apprunner-ecr-role-2026-05-08`~~ | C29 | closed | meta / IAM | `luciel-apprunner-ecr-role` (created 2026-04-12) was a remnant of an abandoned App Runner exploration from before the ECS-only consolidation in Step 27. No active App Runner service; no IaC referenced the role. Detach + delete applied 2026-05-08 14:48 EDT. Final luciel-* role inventory = 8. |
| ~~`D-session-summary-asserted-no-autoscaling-cfn-shows-live-2026-05-08`~~ | C29 | closed | meta / process | Track 3 session-summary carried stale assumption that no production autoscaling existed. Tier 2 enumeration revealed three live luciel-* CFN stacks: `luciel-prod-alarms`, `luciel-prod-worker-autoscaling`, `luciel-prod-verify-role`. Worker autoscaling has been live since Step 28 Phase 2 Commit 6 (2026-05-05). Process fix: ground autoscaling claims against `aws cloudformation list-stacks` before any Tier 2 cleanup planning. |
| ~~`D-cluster-task-def-cleanup-runbook-stale-2026-05-08`~~ | C29 | closed | meta / docs | `docs/runbooks/step-29y-prod-cleanup-2026-05-08.md` included a deregistration directive for `luciel-grant-check:4` — but rev 4 is the latest, currently-runnable revision. Following verbatim would have wrongly deregistered the active task def. T2.2 Phase 2 corrected this and kept rev 4 active. Process fix: compute "keep latest, deregister all others" programmatically rather than read static revision lists from runbooks. |
| ~~`D-c19-tests-not-updated-after-redis-centralize-2026-05-08`~~ | C30 | closed | meta / test maintenance | C19 centralized `REDIS_URL` reads through `settings.redis_url`. Two tests in `tests/security/test_rate_limit_failmode.py` continued asserting the pre-C19 read pattern. AST-asserted literal `os.getenv("REDIS_URL")` no longer exists; reload-chain didn't include `app.core.config` so cached Settings retained boot-time value. Fix: tests updated to assert through `Settings.redis_url`. No behavioral regression in production code. |
| ~~`D-pillar14-fail-on-stale-verify-td-image-2026-05-08`~~ | C31 | closed | P14 / meta / deploy | F.1.b verify-suite returned 24/25 GREEN with P14 FAIL on `luciel-verify:17`. Bug NOT reproduced from laptop diagnostic against the same `api.vantagemind.ai` endpoint. Diagnosis: failing client image was in verify TD, not backend. `luciel-verify:17` ran image `sha256:14ae8b53...` (rev28-era, pushed 2026-05-07 14:54 EDT) — pre-C14, so issued consent.grant with k1_raw while specifying tenant_id=t2_id. Production route is not regressed. Fix: re-pin verify-td.json to rev30 digest, register as `luciel-verify:18`. Subsequent re-pin to rev32 in C32d. |
| ~~`D-verify-td-registered-with-mystery-digest-2026-05-08`~~ | C31 | closed | meta / deploy / process | Local `verify-td.json` carried `sha256:933a141a...` (Step 28 C10-era) but `aws ecs describe-task-definition --task-definition luciel-verify:17` returned `sha256:14ae8b53...`. Most likely cause: out-of-band `aws ecs register-task-definition` with inline JSON override never committed back. Fix: verify-td.json updated to rev30 digest, registered as `:18`. Carry-forward to Step 30: CI guardrail diffing in-repo verify-td.json against `luciel-verify:LATEST` and failing build on mismatch. |
| ~~`D-verify-worker-reachable-not-sqs-aware-2026-05-08`~~ | C32 series | closed | P14 | Verify worker-reachable check assumed Redis broker; rewrote to be SQS-aware on prod-shaped path. Closed alongside C32-final (25/25 FULL). |
| ~~`D-pillar11-f1-cold-start-vs-steady-state-2026-05-08`~~ | C32c (`db164dc`) + C32d (`f8de897`) + C32-close (`4443f96`) | closed | P11 | F1 measured cold-start latency (275ms FAIL) instead of steady-state. Patch in `app/verification/tests/pillar_11_async_memory.py` lines 257–356 enqueues warm-up so steady-state latency budget is what gets measured. Result: 3.98s FULL on luciel-verify:20 (rev32). |
| ~~`D-untracked-diag-artifacts-no-gitignore-2026-05-08`~~ | C33a (`cea23af`) | closed | meta / hygiene | 37 stale operational JSONs (probe outputs, CFN describe dumps, run-task skeletons) tracked in repo root + diag artifacts had no `.gitignore` rules. Fix: delete the 37 stale JSONs; extend `.gitignore` with diag artifact patterns. |

---

## §5 Step 29.y close-out — three-doc regime (this commit cycle)

| Token | Commit | Status | Pillar / Cluster | Summary |
|---|---|---|---|---|
| `D-three-doc-regime-not-yet-adopted-2026-05-08` | C34a/b/c (this cycle) | closing | meta | Adopt `CANONICAL_RECAP.md` (business), `ARCHITECTURE.md` (technical), `DRIFTS.md` (this file) as the only three living docs. Legacy docs move to `docs/archive/`. Closes when C35 ships. |

---

## §6 Disclosures (security-relevant historical defects)

CVE-style internal log of defects that were silently present in production (or production-equivalent) and have since been remediated. Append-only.

### DISC-2026-001 — Rate-limiter env-var typo silently disabled cluster-shared rate limiting

- **Date logged:** 2026-05-07
- **Drift token:** `D-historical-rate-limit-typo-disclosure-2026-05-07` (closed §3)
- **Remediation commit:** `7e783a5` (Step 29.y Cluster 5 / B-1)
- **Verification gate:** Pillar P11; behavioral + AST tests in `a98525a`; structural follow-up `D-redis-url-centralize-via-settings-2026-05-08` / C19
- **Severity (internal):** High — control was advertised as enforced, was silently bypassed in prod
- **Customer impact:** None observed; no abuse incident recovered from logs

**Root cause:** Rate-limiter module read its storage URL via `os.getenv('REDISURL')` — missing underscore. Production exports `REDIS_URL`. The `getenv` resolved to `None` on every process start. SlowAPI silently fell through to its `memory://` backend (per-process). Cluster-shared rate limiting was never in effect for the affected build window. Upper-bound theoretical exposure: a `60/minute` route would have permitted `N × 60/minute` cluster-wide where `N` is the active task count.

**Why it escaped:** (1) SlowAPI's fallback is silent — no raise, no log, when it falls through to `memory://`. (2) No startup assertion that the configured backend matched the intended backend. (3) Local dev ran a single process, so per-process behavior was indistinguishable from intended cluster-shared behavior. (4) No integration test asserted that two simultaneous workers shared a counter.

**Remediation:** (1) Env-var name corrected to `REDIS_URL` with fail-loud assertion at module load. (2) Pool hardening: `retry_on_timeout=True`, 1.5s connect / 1.5s read socket timeouts, 30s `health_check_interval`. (3) Differentiated fail-mode: `in_memory_fallback_enabled=True` so reads transparently degrade to per-process when primary backend dies (read fail-open); fallback middleware classifies escaping exceptions and returns `503 + Retry-After` only for write methods, preserving write-quota integrity.

**Verification:** Behavioral tests assert two test-mode limiter instances sharing a backend block at the configured threshold; AST tests assert `REDIS_URL` (with underscore) is the only string the module reads, blocking regression.

**Status:** Remediated. Structural follow-up C19 routes all `REDIS_URL` reads through `Settings`, making future regressions of this class compile-time-detectable.

### DISC-2026-003 — Verification-harness retry generated 4× duplicate audit rows; deletion attempted, reverted, migration redesigned forward-only

- **Date logged:** 2026-05-07
- **Drift token:** `D-audit-verification-harness-retry-duplicates-2026-05-07` (closed §3)
- **Remediation commits:** C11 (`3798b32`), C12 (forward-only redesign of `d8e2c4b1a0f3`)
- **Severity (internal):** Low — Pattern E preserved
- **Customer impact:** None

**Root cause:** Verification harness retry produced 223 duplicate `worker_*` audit rows in `admin_audit_logs`. Migration `d8e2c4b1a0f3` (worker-reject idempotency partial UNIQUE index) could not apply against the duplicated history.

**Why the duplicates exist:** Verification scenarios re-enqueue the same scope-policy attack to assert idempotency, and the worker-side audit emit fired once per delivery before the application-level idempotency check landed. The duplicate rows were a true history of duplicate worker actions, not corruption.

**Procedure used (full timeline including the reverted attempt):**

1. First attempt: `DELETE FROM admin_audit_logs WHERE ...` removed 166 rows.
2. Pillar 23 walk failed: 60 hash-chain links broken (each deleted row had been the `prev_hash` source for a subsequent row).
3. Reverted via CSV restore from pre-deletion snapshot.
4. Migration redesigned forward-only: partial UNIQUE index gains `AND created_at >= TIMESTAMPTZ '2026-05-08 04:00:00+00'` so only post-cutoff rows are governed by the idempotency rule. Historical duplicates retained.

**Pattern E reasoning (lesson):** Pattern E (deactivate, never delete) applies to audit rows even when those rows look like junk. The hash chain depends on every row being present; deletion is irreversible loss of forensic provenance. Forward-only migrations with explicit timestamp watermarks are the correct primitive when historical data has a property the new constraint can't satisfy.

**Status:** Resolved. Pattern E preserved. Historical duplicates retained.

---

## §7 Maintenance Contract

1. New drift tokens must be appended in the same commit that introduces them.
2. Status changes (open → closed, etc.) are recorded by editing the row in a new commit; the commit message must reference the token.
3. Tokens are never deleted. Resolved tokens carry strikethrough on the token cell. Supersession is recorded by setting `Status = superseded-by-<new-token>` and adding a new row for the replacement.
4. Hashes here are pointers, not the source of truth. If a hash and `git log` disagree, `git log` wins and this file must be corrected.
5. Resolved gaps do not migrate to `CANONICAL_RECAP.md` or `ARCHITECTURE.md`. They stay here with strikethrough.
