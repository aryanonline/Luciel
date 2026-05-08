# Step 28 — Phase 1 Tactical Plan (Security & Compliance Hardening)

**Created:** 2026-04-27, 2:30 PM EDT
**Branch:** `step-28-hardening`
**Built on:** `81c0088` (D1 closure)
**Master plan:** `docs/recaps/2026-04-27-step-28-master-plan.md`
**Status:** Living document — updates per commit

---

## 1. Phase 1 in One Paragraph

Phase 1 closes every known security and compliance drift item before
GTA brokerage outreach begins. Five commits on `step-28-hardening`
starting from `81c0088` (D1 closure done). Four code/migration
commits — consent route double-prefix fix (D16), `ApiKeyService.deactivate_key`
audit retrofit (D5), memory_items orphan sweep + NOT NULL flip (D11),
separate `luciel_worker` Postgres role + dedicated worker security
group — followed by a Phase 1 close commit that updates the master
plan drift register, ships the prod runbook artifact, and opens
PR #4 for merge into main. Local 14/14 verification stays green
between every commit. Each commit has its own rollback contract; the
phase as a whole rolls back to `81c0088`. After PR #4 merges, prod
rollout runs via `step-28-phase-1-deploy.md` and the phase tags as
`step-28-phase-1-YYYYMMDD`. Estimated 6-8 hours across 2-3 sessions,
ending with GTA outreach kickoff unblocked.

---

## 2. Commit-by-Commit Plan

Sequenced for blast-radius minimization. Smallest, lowest-risk
commits first; AWS-side infrastructure changes last when local
discipline is highest.

### Commit 2 — `fix(28): consent route double-prefix bug` (closes D16)

**What changes:**
- `app/api/v1/consent.py` line where `APIRouter(prefix="/api/v1/consent")`
  is declared: change to `APIRouter(prefix="/consent")`
- Confirm `app/api/v1/__init__.py` or `app/api/router.py` parent
  mounting is unchanged (the parent already adds `/api/v1`)

**Acceptance:**
- `GET /api/v1/consent/status` returns same response as before
  (200 or auth-related 4xx)
- `GET /api/v1/api/v1/consent/status` returns 404
- Local 14/14 still green
- Grep `prefix="/api/v1/` returns zero hits in router files (consent
  was the only offender per canonical recap §13)

**Wall-clock:** 30 min

**Rollback:** `git revert <SHA>`. Single-line change. External callers
using buggy double-prefix path were never legitimate (no documented
operation hits that path per canonical recap). Forward-compatible fix.

**Risk:** Lowest. Only failure mode is a script or runbook hardcoding
the double-prefix path; canonical recap explicitly notes none exist.

---

### Commit 3 — `feat(28): retrofit ApiKeyService.deactivate_key with audit_ctx` (closes D5)

**What changes:**
- `app/services/api_key_service.py` `deactivate_key` method gains
  `audit_ctx: AuditContext | None = None` parameter
- After the `active = False` flip, if `audit_ctx is not None`, emit
  audit row via `AdminAuditRepository(self.db).record(...)` with
  `action="deactivate"`, `resource_type="api_key"`, before/after dict
  capturing `active` and `key_prefix`
- Pattern mirrors `AdminService.deactivate_domain` (admin_service.py:204)
  and `UserService.deactivate_user` (24.5b Commit 2)
- Existing callers that don't pass `audit_ctx` keep working (parameter
  is `None`-defaulted) — backward compatible
- Optional: add `note: str | None = None` parameter so future rotations
  can pass reason strings (e.g. "leaked in chat 2026-04-26")

**Files touched:**
- `app/services/api_key_service.py` (signature + body, ~10 lines)
- `tests/services/test_api_key_service.py` if it exists; otherwise
  defer test addition to Step 29's pytest sprint

**Acceptance:**
- Local 14/14 still green (Pillar 7 cascade tests use deactivate_key
  internally; must not break)
- Manual probe: `ApiKeyService(db).deactivate_key(key_id=X,
  audit_ctx=AuditContext.system(label="test"))` lands a fresh row in
  `admin_audit_logs` with correct fields
- Backward-compat probe: `ApiKeyService(db).deactivate_key(key_id=Y)`
  (no audit_ctx) still works, no audit row written
- D1 closure (audit row 1997) is NOT modified — historical record
  stays intact

**Wall-clock:** 90 min

**Rollback:** `git revert <SHA>`. Backward-compatible signature change.

**Risk:** Medium. `deactivate_key` is used in Step 24.5b's Q6 cascade
(`rotate_keys_for_agent`). Risk vector: breaking Pillar 7 cascade test
by inadvertently changing existing audit row count or shape.
Mitigation: run `python -m app.verification` after every line change.

**Known unknown:** Does `rotate_keys_for_agent` write its own audit
rows OR call `deactivate_key` and let it write? Grep before patching.
If the latter, this commit also updates `rotate_keys_for_agent` to
pass `audit_ctx` through.

---

### Commit 4 — `chore(28): memory_items.actor_user_id orphan sweep + NOT NULL flip` (closes D11)

**What changes (two-part single commit):**

Part A — orphan sweep:
- Run `python -m scripts.backfill_user_id --phase b` (existing script
  per 24.5b Commit 3, classifies orphans into `no_agent_id`,
  `no_agent`, `no_user_id` buckets per drift D11 narrative)
- For each orphan: backfill from agent → user mapping if chain is
  complete, otherwise hard-delete the orphan row (10 historical rows
  per recap)
- Result: `SELECT COUNT(*) FROM memory_items WHERE actor_user_id IS
  NULL` returns 0

Part B — NOT NULL flip migration:
- Hand-written Alembic migration (Invariant 12: JSONB-adjacent table
  ⇒ no autogenerate)
- `ALTER TABLE memory_items ALTER COLUMN actor_user_id SET NOT NULL`
- Migration head advances from `4e989b9392c0` to new SHA
- Verified against fresh DB before commit per Invariant 12

**Files touched:**
- `alembic/versions/<new_sha>_memory_items_actor_user_id_not_null.py`
  (~30 lines)
- No code changes

**Acceptance:**
- Local 14/14 still green after sweep + migration
- DB query confirms 0 NULL `actor_user_id` rows pre-flip
- Migration applies cleanly on fresh DB
- Pillars 11/12/13/14 re-pass with NOT NULL enforced
- `downgrade()` cleanly reverses to nullable

**Wall-clock:** 90 min (30 min sweep + 60 min migration)

**Rollback:**
- DB-level: `alembic downgrade -1` reverses NOT NULL
- Sweep-level: orphan deletions cannot be reversed without snapshot.
  Mitigation: local `pg_dump` before sweep; on prod, RDS snapshot
  in runbook pre-flight per 24.5b pattern
- Code-level: nothing to revert

**Risk:** Medium. Two operations in one commit. **Split-decision rule:**
if sweep finds >10 orphan rows OR any post-Step-24.5b timestamp,
split into Commit 4a (sweep) + Commit 4b (flip) with diagnostic
pause — non-historical orphans mean a write path is still landing
NULLs and the flip would create new failures.

**Pre-commit gates:**
1. `--dry-run` shows what sweep would do (if supported)
2. Confirm count = 10
3. Confirm timestamps pre-2026-04-24
4. Live sweep, confirm count=0
5. Hand-write migration
6. Verify against fresh DB
7. Apply locally, confirm 14/14 green
8. Stage + commit

**Known unknown:** Does `scripts.backfill_user_id` support
`--phase b --dry-run` or only `--phase a`? Read script header before
relying on it; if no dry-run, manual `SELECT` of orphan rows first.

---

### Commit 5 — `feat(28): separate luciel_worker Postgres role` (closes "luciel_worker DB role" standing item)

**What changes:**
- New Alembic migration creating Postgres role with least-privilege:
  - `CREATE ROLE luciel_worker WITH LOGIN PASSWORD '<SSM-injected>'`
  - `GRANT SELECT, INSERT ON memory_items, admin_audit_logs`
  - `GRANT SELECT ON messages, sessions, users, api_keys, tenants,
    agents, luciel_instances, scope_assignments`
  - `GRANT USAGE ON SCHEMA public`
  - `GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public`
  - Explicit deny by omission: `retention_policies`, `deletion_logs`,
    `user_consents`, `knowledge_*`, `retention_categories`
- New SSM SecureString `/luciel/production/worker_database_url` with
  the worker role's connection string (separate password from web)
- Worker ECS task-def updated: `secrets` field references new SSM
  param, `WORKER_DATABASE_URL` env var injected at container start
- `app/worker/celery_app.py` reads `WORKER_DATABASE_URL` if set,
  falls back to `DATABASE_URL` for local dev compatibility
- `.env.example` documents the new var (no value)

**Files touched:**
- `alembic/versions/<new_sha>_create_luciel_worker_role.py` (~50 lines)
- `app/worker/celery_app.py` (~5 lines — env var fallback)
- `app/db/session.py` (possibly — separate engine for worker; decide
  by grep)
- `.env.example` (1 line)
- AWS-side: SSM param + ECS task-def revision (out-of-git artifacts,
  documented in runbook)

**Acceptance:**
- Worker boots with new role: CloudWatch shows `psycopg connected as
  luciel_worker`
- Pillar 11 (async memory) passes — worker writes to `memory_items` +
  `admin_audit_logs`
- Pillar 6 (retention) passes — web role unchanged, full access
- Negative test: temporarily point worker at retention_policies,
  confirm `permission denied`
- `downgrade()` revokes grants + drops role cleanly

**Wall-clock:** 2 hr (longest in Phase 1; AWS coord + negative-test
setup + dual-engine code if needed)

**Rollback:**
- DB-level: `alembic downgrade -1` revokes + drops role. Caveat: if
  worker session is using role, drop fails. Mitigation: scale worker
  to desired=0 before downgrade.
- Service-level: revert worker task-def to previous revision (uses
  `DATABASE_URL` web role); worker functions, just over-privileged
- SSM: delete `/luciel/production/worker_database_url`

**Risk:** High. DB-role grants + AWS secrets. Mitigation: test
exhaustively locally — create local `luciel_worker` role with same
grants, set local `WORKER_DATABASE_URL`, run full 14/14, manually
verify negative case (worker denied on retention_policies), only
then build prod migration.

**Known unknowns:**
- Does worker code make cross-table joins into retention/knowledge?
  Grep `app/worker/` before defining grants
- Is sequence-level `USAGE ON SEQUENCE` needed for `memory_items.id`
  autoincrement? Postgres requires it separately
- `op.execute("CREATE ROLE...")` is the canonical Alembic pattern;
  confirm no third-party plugin needed

---

### Commit 6 — `feat(28): dedicated luciel-worker-sg security group` (closes "worker SG" standing item)

**What changes:**
- New AWS security group `luciel-worker-sg`, egress-only:
  - 5432/tcp → RDS endpoint (`luciel-db.<...>.rds.amazonaws.com`)
  - 6379/tcp → ElastiCache (if still referenced; per Step 27c Redis
    was abandoned for SQS, possibly remove)
  - 443/tcp → SQS endpoint (`sqs.ca-central-1.amazonaws.com`)
  - 443/tcp → SSM endpoint (`ssm.ca-central-1.amazonaws.com`)
  - 443/tcp → Anthropic + OpenAI APIs (or VPC endpoint if available)
  - No ingress rules (worker not addressable from outside)
  - No ALB rule (web SG keeps that)
- Worker ECS task-def network config updated: new SG instead of
  `sg-0f2e317f987925601` (web SG)
- Web SG untouched

**Files touched:**
- AWS console / CLI / IaC artifacts (out-of-git per current convention)
- `docs/runbooks/step-28-phase-1-deploy.md` documents SG creation +
  task-def update sequence

**Acceptance:**
- Worker boots with new SG: ECS task description shows new SG ID
- Pillar 11 passes — worker reaches SQS, RDS, LLM
- Negative test: revoke 443/tcp egress to `api.anthropic.com`,
  trigger extraction, confirm DLQ catches retries within 3 attempts
- Re-add egress, confirm extraction resumes

**Wall-clock:** 1 hr (AWS console clicks + verification)

**Rollback:**
- Update worker task-def back to web SG `sg-0f2e317f987925601`
- Delete `luciel-worker-sg` (only after worker no longer using it)
- 5-min recovery via `update-service --force-new-deployment` (Step
  26b pattern)

**Risk:** Medium. Misconfigured egress could send all extractions to
DLQ. Mitigation: add new SG with explicit egress to all required
endpoints BEFORE removing old SG; cut over only after worker verifies
connectivity to each endpoint.

**Known unknowns:**
- VPC endpoints for SQS / SSM vs internet-routed egress? If endpoints
  exist, target their ENI; otherwise `0.0.0.0/0` on 443/tcp
- LLM endpoints routed through proxy / NAT? Confirm via
  `aws ec2 describe-route-tables`

---

### Commit 7 — `docs(28): Phase 1 close - drift register update + prod runbook` (Phase 1 close)

**What changes:**
- `docs/recaps/2026-04-27-step-28-master-plan.md` Section 3 drift
  register: D5, D11, D16 marked RESOLVED with closing SHAs from
  Commits 3, 4, 2 respectively. Standing items "luciel_worker
  Postgres role" and "luciel-worker-sg security group" marked
  RESOLVED with SHAs from Commits 5, 6.
- Master plan Section 1 retrospective addition: 2-paragraph
  "What Phase 1 actually shipped" capturing wall-clock vs estimate,
  surprises, drift items added during execution
- `docs/runbooks/step-28-phase-1-deploy.md` NEW — 6-phase prod
  rollout runbook mirroring 24.5b pattern: Phase 0 pre-flight (RDS
  snapshot, ALB health, prod Alembic head verify, SSM resolve),
  Phase 1 migrations via `luciel-migrate:N` ECS one-shot, Phase 2
  SSM param + worker SG create, Phase 3 image rebuild + register
  `luciel-backend:N` + `luciel-worker:N`, Phase 4 service rollout
  web-then-worker, Phase 5 prod 14/14 MODE=full gate, Phase 6 tag
  `step-28-phase-1-YYYYMMDD`. Wall-clock 75-90 min target.

**Files touched:**
- `docs/recaps/2026-04-27-step-28-master-plan.md` (drift register +
  Section 1 retrospective)
- `docs/runbooks/step-28-phase-1-deploy.md` (new, ~500 lines)

**Acceptance:**
- Local 14/14 still green
- All 5 prior Phase 1 commits referenced by SHA in drift register
- Runbook self-contained: any operator can execute Phase 1 prod
  rollout cold from the runbook alone
- Master plan retrospective is honest about what changed vs plan

**Wall-clock:** 60 min

**Rollback:** `git revert <SHA>`. Pure docs commit.

**Post-commit action:** Open PR #4 to main, title "Step 28 Phase 1:
Security & Compliance Hardening", description mirrors 24.5b PR
pattern.

---

## 3. Phase 1 Close Gate

Phase 1 is done when ALL true:

1. All 6 Phase 1 commits durable on `origin/step-28-hardening`
   (D1 closure `81c0088` + Commits 2-7)
2. PR #4 merged to main (squash-merge per 24.5b convention)
3. Local 14/14 pillars green with new id=539 key + post-D11 NOT NULL
   + new luciel_worker role + new worker SG (locally simulated)
4. Prod 14/14 MODE=full green via ECS execute-command from inside
   running web task post-rollout
5. Worker boots cleanly under new role + new SG (CloudWatch shows
   `psycopg connected as luciel_worker`, no permission-denied, no
   SG-routing errors)
6. Negative tests proven during runbook Phase 5:
   - Worker can't read `retention_policies` (DB role enforces)
   - Worker can't reach non-allowlisted endpoint (SG enforces)
   - Buggy `/api/v1/api/v1/consent/status` returns 404 (D16 fix)
7. Tag `step-28-phase-1-YYYYMMDD` on merge commit, annotated with
   6 commit SHAs, drift items resolved (D1, D5, D11, D16 + 2
   standing items), prod state at close
8. Drift register has zero open P0/P1 items for Phase 1 scope
9. GTA outreach kickoff initiated (operator side: brokerage
   shortlist drafted, first message templated; gate is "outreach
   allowed" not "outreach happened")

---

## 4. Risks & Known Unknowns

### Risks (mitigations baked into per-commit specs)

- **R1** D5 retrofit breaks Pillar 7 cascade. Mitigation: run
  `python -m app.verification` after every line change in Commit 3.
- **R2** D11 sweep finds >10 orphans. Mitigation: split-decision
  rule in Commit 4 — defer flip if non-historical timestamps appear.
- **R3** Worker DB role over-restricts and breaks Pillar 11.
  Mitigation: exhaustive local test in Commit 5 with new role
  before any prod migration.
- **R4** Worker SG egress misconfig sends all extractions to DLQ.
  Mitigation: add-before-remove in Commit 6.
- **R5** Prod rollout surprises from cumulative changes.
  Mitigation: 6-phase runbook with rollback contract per phase;
  `step-24.5b-20260503` is the global rollback ceiling.

### Known unknowns (pre-commit grep / probe required)

- **U1** `rotate_keys_for_agent` audit-emission pattern (Commit 3).
  Grep before patching deactivate_key.
- **U2** `scripts.backfill_user_id` `--dry-run` support on
  `--phase b` (Commit 4). Read script header.
- **U3** Worker code cross-table joins into retention/knowledge
  (Commit 5). Grep `app/worker/` before defining grants.
- **U4** VPC endpoints for SQS/SSM vs internet-routed egress
  (Commit 6). `aws ec2 describe-route-tables`.
- **U5** Step 27c worker IAM grants. Re-confirm post-Step-27c
  worker IAM role allows SQS receive/delete/send on the two queue
  ARNs only.

---

## 5. Sequencing Rationale

1. **Consent fix first (Commit 2)** — smallest change, lowest risk,
   seeds the branch with a quick win that proves planning machinery
   works end-to-end before tackling anything harder.

2. **D5 before D11 (Commit 3 before 4)** — NOT NULL flip's audit
   trail should use the canonical service-layer audit-emitting path,
   not an inline workaround. D11-first would force re-touching the
   commit later.

3. **D11 before infrastructure (Commit 4 before 5+6)** — D11 is pure
   DB+code with all-local validation. Infrastructure changes are
   AWS-side and require most diligence; they go last when local
   discipline is highest.

4. **Worker DB role before worker SG (Commit 5 before 6)** — role
   is more invasive (DB-level grants); broken role surfaces at
   worker boot. SG is network filter; misconfig surfaces only on
   denied endpoint reach. Role-first isolates failures.

5. **Phase 1 close last (Commit 7)** — drift register updates and
   runbook artifact need actual SHAs from Commits 2-6. Writing
   forward would create a forward-reference graveyard.

6. **Single PR #4 for whole phase** — Step 24.5b proved this
   pattern for a 4-commit arc; Phase 1 is structurally similar at
   6 commits. PR review surface = whole security/compliance story
   at once. Squash-merge yields clean single line on main.

---

**End of Phase 1 tactical plan.**

Living document. Updates as Phase 1 commits land. After Phase 1 PR
merges and tag lands, Phase 2 tactical plan
(`docs/recaps/2026-04-27-step-28-phase-2-plan.md`) gets written from
the master plan Section 2 Phase 2 spec, in the same shape.
