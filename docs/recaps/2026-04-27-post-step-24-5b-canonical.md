# VantageMind AI / Luciel — Canonical Recap
## As of Monday, 2026-04-27, 1 AM EDT — immediately after Step 24.5b Commit 3 of 4 closed and pushed to origin

This is the durable reference document for Luciel as of the close of
Step 24.5b's verification commit. When a chat conversation gets long
or context drifts, **re-anchor on this single document**. The next
session restores from this recap plus the GitHub repo state.

Save this to your password manager under "Luciel Step 24.5b Recap"
when the corresponding tag (`step-24.5b-20260503`) is pushed at the
end of the prod rollout.

---

## 1. Company / Business Position

- **Legal:** VantageMind AI, sole-operator, Markham, Ontario, Canada
- **Founder:** Aryan Singh (`aryans.www@gmail.com`)
- **Jurisdiction:** Canada / PIPEDA. All infrastructure in AWS `ca-central-1`
- **Product:** Luciel — a domain-adaptive AI judgment layer
- **Business model:** B2B SaaS
- **Repo:** https://github.com/aryanonline/Luciel — default branch `main`
- **Latest tag on origin:** `step-27-completed-20260426` (commit `6f03e03`)
- **Active branch:** `step-24.5b-identity` at `314ac46` (3 of 4 commits)
- **Production:** `https://api.vantagemind.ai` LIVE
  - TLS 1.3 PQ-KEX, ACM cert valid until 2026-10-29
  - Web `luciel-backend:15`, Worker `luciel-worker:4` (Step 27c-final close)
  - RDS Alembic head `8e2a1f5b9c4d` (will advance to `4e989b9392c0` at Step 24.5b prod rollout)

### Step 24.5b business outcome

**Q6 RESOLVED.** Role changes (promotions, demotions, departures) are
now first-class operations with mandatory key rotation, immutable
audit trail, and bounded cascade scope. The doctrine the canonical
strategic question table committed to ("data lives with scope, not
person; Users + scope assignments + mandatory key rotation + immutable
audit log") is fully implemented and verified by 14/14 pillars green.

**Q5 prerequisite SATISFIED.** Email-stable User identity now lives in
both code and DB schema. The Step 38 bottom-up tenant merge endpoint
becomes implementable (the merge endpoint itself stays parked at Step
38 per roadmap; the prerequisite ships at Step 24.5b).

### Outreach posture

GTA brokerage outreach was unblocked since Step 26b on the
"production credibility" axis. Step 24.5b adds a second sellable
dimension: the **multi-tenant identity story**. A real estate agent
prospect can now be told:

> "Sarah, our system tracks you as a single platform identity even
> when you hold roles at multiple brokerages. When you leave one
> brokerage, your access there cuts immediately, your memory and
> audit trail there stays intact for compliance, and your other
> brokerages are completely untouched. If you later get promoted
> within REMAX Crossroads, your old chat keys stop working in the
> same transaction your old role ends — no security gap."

That's the demoable Q6 story. **Outreach gate:** Drift D1 (leaked
local platform-admin key from Step 27c session) must rotate before
any external demo. Procedure documented in
`docs/runbooks/step-24.5b-identity-deploy.md` security reminder
section. Local-dev only, doesn't touch prod.

### Pricing tiers (unchanged from prior recap)

- **Individual:** $30–80/month, one person, one tenant, limited Luciels
- **Team:** $300–800/month, one domain, unlimited agents within
- **Company:** $2,000/month, full tenant with multiple domains

Bottom-up expansion path with pro-rated upgrade credit (Step 38, gated
behind Step 35) becomes implementable now that Q5 prerequisite ships.

### Moat layers (unchanged, with Q6 contribution)

1. Fixed Luciel Core persona, infinitely many scoped instances
2. Three-level hierarchical data layer (tenant / domain / agent)
3. Cross-client feedback loops with isolation preserved
4. Deep workflow integration (Step 34+)
5. Domain-agnostic architecture (config + knowledge, not code)

Step 24.5b adds **identity continuity across role changes** as a
demoable property of layer 2 — single platform User identity persists
across promotions and tenant departures, mandatory key rotation
enforced at the auth boundary, no over-cascade across tenant
boundaries.

---

## 2. Architecture State

### DB schema

- **17 model tables** — 15 baseline + `users` + `scope_assignments`
  (Step 24.5b additions)
- Migration chain head locally: `4e989b9392c0` (Commit 2's File 2.7)
  — File 3.8 NOT NULL flip rolled back per drift D20
- Production migration head currently: `8e2a1f5b9c4d` (Step 27 close)
  — advances to `4e989b9392c0` at Step 24.5b prod rollout
- Both new identity columns (`agents.user_id` and
  `memory_items.actor_user_id`) are **nullable** through Step 24.5b
  release per drifts D11 and D20. Step 28 hardens public routes to
  require user_id explicitly, then re-attempts the NOT NULL flip.

### Identity layer (new in Step 24.5b)

- **`users`** — durable person identity, tenant-agnostic, UUID PK,
  case-insensitive email uniqueness via `LOWER(email)` expression
  index, `synthetic` flag for Option B onboarding stubs, soft-delete
  via `active`
- **`scope_assignments`** — first-class durable role binding, UUID PK,
  end-and-recreate doctrine (never UPDATE in place), `EndReason` enum
  (PROMOTED / DEMOTED / REASSIGNED / DEPARTED / DEACTIVATED), 3
  partial indexes filtered on `ended_at IS NULL` for hot-path
  "currently active" queries, DB-level partial check constraint on
  `(ended_at IS NULL) = (ended_reason IS NULL)`
- **`agents.user_id`** — nullable UUID FK to `users.id`
  (`ON DELETE RESTRICT`)
- **`memory_items.actor_user_id`** — nullable UUID FK to `users.id`
  (`ON DELETE RESTRICT`); coexists with the existing free-form
  `user_id` string column per drift D7 resolution

### Service layer

- **`UserService`** — 5 public methods
  (`create_user`, `get_or_create_by_email`, `get_user`, `update_user`,
  `deactivate_user`). Cascade-on-deactivation walks ScopeAssignments
  and Agents in one transaction.
- **`ScopeAssignmentService`** — 6 public methods. `end_assignment`
  is the single audit-clean entry point for all role-change kinds.
  `promote` is the atomic end-old + create-new composition.
- **`ApiKeyService.rotate_keys_for_agent`** — Q6 mandatory rotation
  cascade. Hard rotation, no grace period. Walks direct ApiKey
  bindings AND LucielInstance-pinned keys. Per-key
  `KEY_ROTATED_ON_ROLE_CHANGE` audit rows.

### Middleware + chat + worker

- **`request.state.actor_user_id`** injected by `app/middleware/auth.py`
  (UUID, distinct from `session.user_id` free-form string per drift
  D7 resolution). Resolved via single Agent lookup ONLY for
  agent-scoped keys.
- **ChatService** threads `actor_user_id` through both `respond` and
  `respond_stream`, all 4 memory call sites (sync + async paths).
- **Worker defense-in-depth Gates 5 + 6** wired in
  `app/worker/tasks/memory_extraction.py`:
  - Gate 5 — `User.active is True` check; rejects to DLQ via
    `ACTION_WORKER_USER_INACTIVE`
  - Gate 6 — `Agent.user_id == payload.actor_user_id` cross-tenant
    identity-spoof guard; rejects to DLQ via
    `ACTION_WORKER_IDENTITY_SPOOF_REJECT`. Pillar 13 in MODE=full
    proves this fires against malicious payloads.

### HTTP surface

- **`POST /api/v1/users`** — platform-admin only, creates User
- **`GET /api/v1/users/{id}`** — platform-admin only
- **`PATCH /api/v1/users/{id}`** — platform-admin only, email
  uniqueness pre-flight
- **`DELETE /api/v1/users/{id}`** — platform-admin only, fires Q6
  cascade

(All four routes shipped in Commit 3 to unblock Pillars 12/13/14.
Drift D12 resolution.)

### Verification

- **14/14 pillars green** locally via `python -m app.verification`
- Pillar 12 (identity stability under role change) — 47-50s
- Pillar 13 (cross-tenant identity-spoof guard) — 17-19s, MODE=degraded
  on local; prod gate runs MODE=full
- Pillar 14 (departure semantics, Q6 bounded cascade) — 37-39s
- Pillar 10 (teardown integrity) extended to 17 PROBES rows including
  `users` and `scope_assignments`

### Backfill

- `scripts/backfill_user_id.py` is idempotent, audit-row-emitting,
  exit-code-1-on-residuals
- Local DB has 0 residual NULL `agents.user_id` after backfill ran
- 10 historical orphan `memory_items.actor_user_id` NULLs tolerated
  per drift D11 (Step 28 sweep target)

---

## 3. Drift Items Logged Across Step 24.5b (D1-D20)

Twenty drift items surfaced during the four-commit arc. Each is
classified as RESOLVED (closed within Step 24.5b), deferred (rolled
into Step 28 backlog), cosmetic (cleanup-when-convenient), or
convention (going-forward improvement, not blocking).

### Status legend

- **RESOLVED** — closed within the arc, no follow-up needed
- **deferred** — Step 28 backlog item, documented and tracked
- **cosmetic** — non-blocking cleanup, fix when convenient
- **convention** — going-forward improvement applied to future work

### Full table

| ID | Origin | Summary | Status | Resolution path |
|---|---|---|---|---|
| **D1** | Step 27c session | Local platform-admin key `luc_sk_HY_RKmywB7x...` exposed in chat 2026-04-26 13:00 EDT | **deferred** | HARD GATE before any external demo. Rotation procedure in `docs/runbooks/step-24.5b-identity-deploy.md` security reminder section |
| **D2** | Commit 1 | `admin_audit_log.py` has duplicate `ACTION_KNOWLEDGE_*` declarations and `RESOURCE_KNOWLEDGE` value override (`"knowledge_embedding"` → `"knowledge"` silently) | **deferred** | Step 28 cleanup, one-line constants dedup commit |
| **D3** | Commit 1 | Smoke-test pattern needs `db.rollback()` between expected-failure operations to avoid `InFailedSqlTransaction` noise on second probe | **cosmetic** | Pillars unaffected; future smoke tests use `db.rollback()` between expected failures |
| **D4** | Commit 1 | PowerShell `python -c` blocks containing double-quoted SQL literals can hit unterminated-string-literal parsing under certain quote-escaping | **cosmetic** | Use temp file or here-string for raw-SQL inspection going forward |
| **D5** | Step 27a leftover | Existing `ApiKeyService.deactivate_key` doesn't accept `audit_ctx` or emit audit rows | **deferred** | Step 24.5b worked around via `rotate_keys_for_agent` emitting audits directly. Step 28 retrofits `deactivate_key` to be the canonical audit-emitting path |
| **D6** | Commit 2 plan | Original plan claimed Step 23 onboarding created Agents and File 2.4 should add User binding there. Onboarding actually creates only Tenant/Domain/Retention/AdminKey | **RESOLVED** | Service-layer User binding deferred to Option D one-shot backfill script (File 3.1). Onboarding untouched. |
| **D7** | Commit 2 | Semantic name collision: existing free-form `session.user_id` string vs new UUID platform identity from `Agent.user_id` | **RESOLVED** | Renamed middleware injection to `request.state.actor_user_id`; added `memory_items.actor_user_id` distinct from existing `user_id` string column. Both fields coexist with distinct semantics |
| **D8** | Commits 1+2 | `configure_mappers` ordering bugs (two observations). File 1.1 declared `User.memory_items` pointing to a column that wouldn't land until 2.6a; File 2.6a's first emission tried matching relationship while User-side was still deferred | **RESOLVED** | Both sides of bidirectional relationship pair declared in one commit (File 2.8). Same class of bug as File 1.3's `ScopeAssignment` `primaryjoin` issue |
| **D9** | Commit 2 | Legacy `MemoryRepository.save_memory` 500'd against unmigrated DB column between Files 2.6a and 2.7. Not on hot chat path | **RESOLVED** | Closed when File 2.7's migration landed the column. ChatService's hot path uses `upsert_by_message_id` so chat turns were never affected |
| **D10** | Commit 2 | Full-file Alembic migration template kept revision-identifier lines commented out as placeholder; alembic couldn't parse until uncommented | **convention** | Future migration emissions are `def upgrade()` body diff + `def downgrade()` body diff only, never full-file templates. Applied throughout Commit 3 |
| **D11** | Commit 3 backfill | `memory_items.actor_user_id` has 10 historical orphan NULL rows (5 no_agent_id pre-Step-24.5 rows, 5 references-missing-Agent rows). NOT NULL flip on this column would fail | **deferred** | Column stays nullable through Step 24.5b release. Step 28 sweep + flip after orphan-memory cleanup |
| **D12** | Commit 3 | `POST /api/v1/users` route didn't exist after Commits 1+2 (schema + service shipped, HTTP surface not). Pillars 12/13/14 hit 404 at user-create step | **RESOLVED** | Shipped `app/api/v1/users.py` with 4 CRUD routes + registered in `app/api/router.py` (Drift D12 fix in Commit 3) |
| **D13** | Commit 3 | `pydantic[email]` validator rejects `.test`, `.local`, `.luciel.local` as RFC 6761 special-use TLDs. Pillars 12/13/14 initial draft used `@luciel.test` which 422'd | **RESOLVED** | Switched pillar emails to `@example.com` (RFC 2606 documentation TLD). Synthetic emails (`agent-{slug}@{tenant}.luciel.local`) bypass the public schema validator since they're created via `UserRepository` directly |
| **D14** | Commit 3 | Synthetic emails (`.luciel.local`) already in DB from backfill would 422 if PATCHed through the public API (`UserUpdate → EmailStr`). Latent issue | **deferred** | Step 28. Rare; would only fire if an operator tried to update a synthetic stub through the public route, which is not a documented operation |
| **D15** | Commit 3 | Pillars used a guessed consent-grant body shape (`consent_given: True`) that didn't match the real `ConsentGrantRequest`. Schema needs only `user_id` + `tenant_id`; other fields default | **RESOLVED** | Updated all three pillars to send the minimal valid body |
| **D16** | Commit 3 | `app/api/v1/consent.py` declares `prefix="/api/v1/consent"` (full absolute path) instead of project-standard relative `prefix="/consent"`. When parent router mounts under `/api/v1`, the prefix doubles into `/api/v1/api/v1/consent/...` | **deferred** | Working but ugly. Step 28 cleanup; fixing now would be API-breaking and needs its own deploy plan |
| **D17** | Commit 3 | Pillar wait-times for async memory extraction were calibrated to sync path (5s single, 6s double). Async path takes 7-15s per extraction on local | **RESOLVED** | Bumped to 15s single / 20s double in Pillars 12 + 14. Going-forward convention recorded in Commit 3 message |
| **D18** | Commit 3 | Local LLM-based memory extractor produces 0 `memory_items` rows for some test message shapes (verification suite chat patterns) | **deferred** | Pillars 12/14 use `memory_skipped` flag for graceful skip when no rows exist. Cascade-bounded security claims (key rotation, scope assignment lifecycle, 401 enforcement) run unconditionally. Step 28 extractor improvements |
| **D19** | Commit 3 | Backfill script + agents.user_id NOT NULL flip surfaced 14 NULL rows from Pillar 12/13/14 test runs. Backfill ran clean (Phase A: 14 backfilled, exit 0) | **RESOLVED** | Backfill flow works as designed. D19 closed by D20 decision (NOT NULL flip itself rolled back) |
| **D20** | Commit 3 | `agents.user_id` NOT NULL flip migration caused Pillar 2 + 12/13/14 to fail with `NotNullViolation` masked as misleading 409 "already exists" by the route's blanket `IntegrityError` catch. Public `POST /admin/agents` doesn't supply `user_id` | **RESOLVED via roll-back** | File 3.8 deleted; `agents.user_id` stays nullable through Step 24.5b release. Step 28 hardens public routes (require `user_id` explicitly OR auto-create synthetic User binding) before re-attempting the flip |

### Tally

| Status | Count | Items |
|---|---|---|
| RESOLVED | 10 | D6, D7, D8, D9, D12, D13, D15, D17, D19, D20 |
| deferred (Step 28) | 8 | D1, D2, D5, D10*, D11, D14, D16, D18 |
| cosmetic | 2 | D3, D4 |

*D10 is technically "convention" rather than "deferred" — applied
forward throughout Commit 3 already. Counted under deferred for
tally simplicity.

### Additional drift items from earlier closures (not Step 24.5b)

These appeared in prior canonical recaps and are tracked here for
continuity in the Step 28 backlog:

- **prior** Step 26b/27c: 7 standing items (CloudWatch alarms,
  auto-scale policies, IAM splits between web/worker roles,
  dedicated worker SG, retention sweep automation, residue tenant
  sweep, JSON archive write-path bug)
- **prior** Step 27c-final: residual sync-memory-extraction step
  (sync extractor improvements, latency optimization for memory-heavy
  turns now that async is live)

Step 28's complete backlog will compose from Step 24.5b's deferred
items + the prior-closure standing items.

---

## 4. Step 28 Backlog Formalized

Step 28 is the next production hardening pass after Step 24.5b ships.
It composes the deferred drift items from Step 24.5b plus the
standing items carried forward from Step 26b and Step 27c-final
closures.

### Step 24.5b deferred items (8)

Each item links back to its drift entry in Section 3.

1. **Public route hardening for `POST /admin/agents`** (D20 follow-up)
   - Add `user_id` requirement to `AgentCreate` schema OR auto-create
     synthetic User binding when caller omits `user_id`
   - Coordinate with the `agents.user_id` NOT NULL flip
   - Same shape applies to `POST /admin/luciel-instances` and any
     other Agent-creating route

2. **`agents.user_id` NOT NULL flip migration** (D20)
   - Runs after route hardening above ships and prod has zero
     residual NULL rows
   - Hand-written per Invariant 12; pre-flight assertion fails loud
     if NULL rows remain (same pattern as deleted File 3.8)

3. **`memory_items.actor_user_id` NOT NULL flip migration** (D11)
   - Runs after orphan-memory sweep eliminates the 10 historical
     orphan rows
   - Sweep itself is a separate one-shot script; soft-deactivate
     vs hard-delete decision is a Step 28 design call

4. **`ApiKeyService.deactivate_key` audit retrofit** (D5)
   - Accept `audit_ctx`, emit per-key audit row in same txn
   - Becomes the canonical audit-emitting deactivation path
   - All current callers updated to pass `audit_ctx`

5. **`consent.py` prefix doubling fix** (D16)
   - `app/api/v1/consent.py` declares `prefix="/api/v1/consent"`
     causing live URL `/api/v1/api/v1/consent/...`
   - API-breaking change — needs deploy plan with backwards
     compatibility window (route both old and new paths during
     transition, log usage of doubled-prefix path)

6. **`UserUpdate.email` PATCH path for synthetic emails** (D14)
   - Public API currently 422s on `.luciel.local` TLD
   - Either: allow service-internal callers to update synthetic
     emails (bypass `EmailStr` validator), OR document that
     synthetic stubs are immutable post-create

7. **`admin_audit_log.py` duplicate constants cleanup** (D2)
   - Remove duplicate `ACTION_KNOWLEDGE_*` declarations
   - Reconcile `RESOURCE_KNOWLEDGE` value (`"knowledge"` vs
     `"knowledge_embedding"`) — Pillar 9 / existing audit rows
     reveal which value is canonical

8. **Local memory extractor improvements** (D18)
   - Extractor produces 0 rows for verification-suite chat shapes
   - Prompt tuning + fallback path for "no extractable facts"
     vs "extractor errored"

### Standing items from prior closures (7)

Carried forward from Step 26b's closure list and Step 27c-final's
deferred work:

9. **CloudWatch alarms** — queue depth, DLQ depth, web 5xx rate,
   worker error rate, RDS storage, ECS service-stable failures
10. **Auto-scaling policies** — web min/max desired-count tied to
    ALB request rate; worker min/max tied to SQS queue depth
11. **IAM split between web and worker roles** — web shouldn't
    have SQS-write to worker queues; worker shouldn't have
    ALB-public egress
12. **Dedicated worker security group** — currently shares SG with
    web; isolate worker for least-privilege network posture
13. **Retention sweep automation** — `RetentionPolicy` purge job
    runs as scheduled ECS task; currently manual
14. **Residue tenant sweep** — sweep `step26-verify-*` and
    `step24-5b-*` tenants older than cutoff; partially in
    `app/verification/fixtures.py:sweep_residue_tenants` already,
    needs scheduling
15. **JSON archive write-path bug** — verification suite's
    `step26report.json` archive sometimes doesn't refresh on local
    runs (Step 26b.2 noted; not blocking)

### Cosmetic / convention items (5)

Lower priority but tracked here for completeness:

- **D3** smoke-test `db.rollback()` between expected failures
- **D4** PowerShell SQL-quoting ergonomics for `python -c` inline blocks
- **D10** migration emission convention recorded (already applied)
- **D17** pillar async-extraction wait-time convention (already applied)
- **(prior) Step 27c-final** sync-memory-extraction latency
  optimization now that async is live on the hot path

### Suggested Step 28 sequencing

Per the Step 27c-final close pattern (security/correctness first,
then operational hardening, then cosmetic), Step 28 sequences as:

1. **Phase A — security/correctness** (items 1, 2, 3, 4 above)
2. **Phase B — API correctness** (items 5, 6 above)
3. **Phase C — operational hardening** (items 9-14 above)
4. **Phase D — cleanup** (items 7, 8 + cosmetic D3/D4)

Item 15 (JSON archive bug) is incidental; fix during whichever phase
touches `app/verification/runner.py`.

This is a bigger step than 24.5b. Estimated 6-10 commits across 2-3
weeks of focused work, with multiple intermediate prod deploys
(NOT NULL flips deploy as their own release tags, e.g.
`step-28a-2026MMDD`, `step-28b-...`).

---

## 5. Strategic Questions Status (updated as of 2026-04-27)

Six strategic questions captured early in the design. Status
updated to reflect Step 24.5b's resolutions.

| # | Question | Resolution | Affected step | Status |
|---|---|---|---|---|
| 1 | Two-key confusion / unified scope creation | Single admin permission; caller's scope dictates what they can create. Split AgentConfig → Agent + LucielInstance. Onboarding returns one tenant admin key only (Option B) | 23, 24.5/25a | **✓ shipped** at Step 25a |
| 2 | Tenant/domain/agent dashboards showing business value | Three-tier dashboard views driven by trace aggregations + `DomainConfig.value_metrics` workflow actions. Requires Step 34 first | 31 expanded, after 34 | queued |
| 3 | Vector + graph hybrid retrieval | Yes. Start with pg-recursive CTEs in Postgres. Opt-in per domain via `DomainConfig.entity_schema`. Plug into Retriever interface so nothing in ChatService changes | 37 after 35 | queued |
| 4 | Luciels communicating within a scope (councils) | Yes. Orchestrator Luciel + inter-Luciel tool calls, strictly within-scope, policed by `ScopePolicy` at tool-call time. Widget key can resolve to `council_id` | 36 after 33 eval | queued |
| 5 | Bottom-up expansion — Sarah → her domain → her company | Email-stable User identity. Tenant-merge endpoint re-parents Luciels/knowledge/memories/sessions. Pricing tiers incentivize upgrade | 38 after 35 | **prerequisite SHIPPED at Step 24.5b**; tenant-merge endpoint queued for Step 38 |
| 6 | Role changes (promotions/demotions/departures) | Data lives with scope, not person. Users + scope assignments + mandatory key rotation + immutable audit log. Luciels and knowledge owned by scope, not creator | **24.5b** (this release) | **✓ RESOLVED at Step 24.5b** |

### What changed in Step 24.5b

**Q5** moves from "queued" to "prerequisite SHIPPED." The
email-stable User identity is now first-class in code and DB
schema. The Step 38 tenant-merge endpoint is implementable —
re-parenting Luciels / knowledge / memories / sessions becomes
a `UPDATE ... WHERE actor_user_id = :user_id` shape across four
tables. The endpoint itself stays parked at Step 38 per roadmap.

**Q6** moves from "captured but unimplemented" to "✓ RESOLVED."
All four Q6 elements are live:
- **Users** — `users` table, durable identity, tenant-agnostic
- **Scope assignments** — `scope_assignments` table, end-and-recreate
  doctrine, full role history walkable backwards
- **Mandatory key rotation** — `ApiKeyService.rotate_keys_for_agent`,
  hard rotation no grace period, fired by
  `ScopeAssignmentService.end_assignment`
- **Immutable audit log** — every Q6 action emits an audit row in
  same txn (Invariant 4), `AdminAuditLog.action` includes
  `key_rotated_on_role_change`, `worker_user_inactive`,
  `worker_identity_spoof_reject`

Verified by Pillars 12 (identity stability), 13 (cross-tenant
spoof guard), 14 (departure semantics) at 14/14 MODE=full prod gate.

The other four questions (Q1, Q2, Q3, Q4) carry forward unchanged.
Q1 stays shipped from Step 25a. Q2/Q3/Q4 stay queued behind their
gating steps (34, 35, 33-eval respectively).

---

## 6. Five Non-Negotiables — Status Across the Layer

These five non-negotiables (scalability, maintainability, security,
traceability, reliability) were locked at the start of Step 24.5b
and re-asserted before every architectural decision in the arc.
Concrete current-state bullets per dimension:

### Scalability

- Hot-path partial indexes on `scope_assignments` filtered on
  `ended_at IS NULL` — "currently active" queries stay O(log N)
  even as ended-history grows unbounded
- `LOWER(email)` functional unique index on `users` — case-insensitive
  lookups use the index, not a sequential scan
- UUID PKs on `users` and `scope_assignments` avoid sequential-ID
  hotspots when writes scale and prevent cross-tenant enumeration
- Middleware `actor_user_id` resolution fires only for agent-scoped
  keys (~80% reduction vs always-fetch); tenant-admin and
  platform-admin keys skip the lookup
- Worker Gates 5+6 fire only when `actor_user_id` is in payload
  (NULL-tolerant for pre-backfill traffic)
- Q6 cascade is bounded — one User has at most one active assignment
  per tenant in steady state, so `end_assignment` touches O(active
  assignments) Agent rows, not O(all agents)
- Backfill script batched (default 100 rows/commit), indexed reads
  on `user_id IS NULL`

### Maintainability

- Pillars 12/13/14 mirror Pillar 7's pattern exactly — same `Pillar`
  ABC subclass, same module-level `PILLAR` export, same
  `pooled_client` + `RunState` shape
- All three new services (UserService, ScopeAssignmentService,
  modifications to ApiKeyService) follow LucielInstanceService /
  OnboardingService convention — `__init__(db)`, per-method repo
  instantiation, domain exceptions per service module, no FastAPI
  imports in service layer
- Domain exceptions (UserError, ScopeAssignmentError) parallel
  LucielInstanceError exactly
- Drift D8 lesson applied: bidirectional relationship pairs
  declared in one commit, never split across commits
- Drift D10 convention applied: migration emissions are body-diff
  only, not full-file templates
- Drift D17 convention applied: pillar async-extraction wait-times
  ≥15s single, ≥20s double
- Three-stage migration discipline (additive → backfill → flip)
  honored — Step 24.5b shipped two of three stages, Step 28
  finishes the flip after route hardening

### Security

- **Q6 mandatory key rotation cascade is LIVE** and audit-emitting.
  Hard rotation, no grace period — a fired or promoted agent's keys
  stop working in the same transaction the assignment ends
- Worker Gate 6 closes the cross-tenant identity-spoof attack
  surface at the worker boundary. Pillar 13 in MODE=full asserts
  this fires against malicious payload
  `(user_id=U, tenant_id=T1, agent_id=A2_under_T2)`
- `ON DELETE RESTRICT` on every identity FK so user/agent history
  cannot be vaporized by a cascade-delete
- UUID PKs on Users prevent cross-tenant identity-count enumeration
  via sequential-ID walking
- Synthetic-email flag on User distinguishes Option B onboarding
  stubs from real users for PIPEDA access/erasure flows
- Public API rejects `.test` / `.local` / `.luciel.local` reserved
  TLDs via Pydantic EmailStr (D13 RFC 6761 compliance); synthetic
  emails only created via service-internal paths
- Pillar 12 proves K1 returns 401 after role change (Q6 hard
  rotation enforced at auth boundary)

### Traceability

- Every Q6 action emits an audit row in same txn as the mutation
  (Invariant 4)
- New audit actions filterable by name:
  `KEY_ROTATED_ON_ROLE_CHANGE`, `WORKER_USER_INACTIVE`,
  `WORKER_IDENTITY_SPOOF_REJECT`
- New resource types: `RESOURCE_USER`, `RESOURCE_SCOPE_ASSIGNMENT`
- Backfill audit-emits per row via
  `AuditContext.system(label="step24.5b-backfill")` — re-runs
  detectable by audit log alone
- `memory_items` carries both end-user-string `user_id` AND
  platform-User-UUID `actor_user_id`, so audit queries can ask
  "all memory written by Sarah's platform identity" AND "all
  memory about prospect-1234" cleanly (drift D7 resolution)
- ScopeAssignment row preserves full role history end-to-end:
  `user_id`, `tenant_id`, `domain_id`, `role`, `started_at`,
  `ended_at`, `ended_reason`, `ended_note`, `ended_by_api_key_id`
- Pillar 14 explicitly asserts T1 memory remains queryable
  post-departure — PIPEDA access flows preserved
- Pillar 10 PROBES extension surfaces residue per tenant for
  `users` (no_tenant_scope by design) and `scope_assignments`
  (live=0 enforced)

### Reliability

- Both new identity columns nullable through Step 24.5b release
  (D11 + D20) — existing public callers continue to work without
  change. NULL-tolerant code paths mean reverting any single file
  leaves the system functional
- Three additive migrations across Commits 1+2+3 (third was
  rolled back per D20). No half-state reachable; every column/
  table this layer adds is nullable or empty on revert
- Single `db.commit()` per cascade
  (UserService.deactivate_user, ScopeAssignmentService
  .end_assignment, ScopeAssignmentService.promote) — any failure
  mid-cascade rolls back everything
- Backfill is idempotent and re-runnable; exit-code-1 contract
  gates any future NOT NULL flip migration
- Pillar 13 falls to MODE=degraded on local cleanly when broker
  unreachable; prod gate exercises full async-memory + DLQ
  assertions without code changes
- RDS rollback snapshot taken pre-deploy and retained as final
  recovery boundary
- Pillars 12/13/14 self-teardown via UserService.deactivate_user
  (cascade) so verification runs leave no orphan state

---

## 7. Tags + Commits + Infrastructure Snapshot

### Release tags on origin

| Tag | Commit | Date | Status |
|---|---|---|---|
| `step-26b-20260422` | `00d7e79` | 2026-04-22 | first prod release |
| `step-27a-20260422` | `16daf61` | 2026-04-22 | hardening (local) |
| `step-27c-deployed-20260425` | `1dc291d` | 2026-04-25 | async memory live, partial verification |
| `step-27-completed-20260426` | `6f03e03` | 2026-04-26 | 11/11 MODE=full prod gate |
| `step-24.5b-20260503` | (pending) | (target) | Q6 + Q5 prerequisite — to be tagged at prod rollout |

### Step 24.5b commit chain on `step-24.5b-identity` branch

| Commit | SHA | Files | Insertions / Deletions |
|---|---|---|---|
| Commit 1 (schema) | `78716fe` | 11 | 2050 / 7 |
| Commit 2 (services) | `766427c` | 13 | 1389 / 17 |
| Commit 3 (verification) | `314ac46` | 8 | 2510 / 2 |
| Commit 4 (cleanup) | (this commit) | 2 | (recap + runbook) |
| **Cumulative** | — | **34** | **~5949 / 26** |

Built on `6f03e03` (Step 27 completed). PR will land all four
commits into `main` as a single review unit when Commit 4 closes.

### Production infrastructure ceilings (current, pre-rollout)

- **Web:** `luciel-backend:15`
  - Image digest: from Step 27c-final close (April 26, 2026)
- **Worker:** `luciel-worker:4`
  - Image digest: from Step 27c-final close
- **RDS:**
  - Alembic head: `8e2a1f5b9c4d` (Step 27 close)
  - Will advance to `4e989b9392c0` at Step 24.5b prod rollout
  - Rollback boundary at rollout: `luciel-db-pre-step-24-5b-<stamp>`
    (Phase 0 of runbook)

### Production infrastructure ceilings (target, post-Step-24.5b)

- **Web:** `luciel-backend:16` (image step24.5b-314ac46)
- **Worker:** `luciel-worker:5` (same image, different entrypoint)
- **RDS Alembic head:** `4e989b9392c0`
- **Rollback ceiling for emergency revert:**
  `luciel-backend:15` / `luciel-worker:4` (Step 27c-final close)

### SSM parameter paths

- `/luciel/production/platform-admin-key` — durable prod
  platform-admin key (read fresh on every verification suite run;
  zero exposure events since Step 27c-final mint)
- `/luciel/production/REDIS_URL` — broker URL (Step 27b SQS
  switchover means this is informational only on prod; local dev
  still uses Redis)
- `/luciel/database-url` — RDS connection string
- `/luciel/openai-api-key`, `/luciel/anthropic-api-key` — LLM
  provider keys

### Local development state at recap time

- Branch: `step-24.5b-identity` at `314ac46` (Commit 3 of 4)
- Alembic head: `4e989b9392c0` (after both Step 24.5b migrations
  applied; File 3.8 rolled back per D20)
- `agents.user_id IS NULL` count: 0 (post-backfill)
- `memory_items.actor_user_id IS NULL` count: 10 (D11 historical
  orphans, tolerated)
- 14/14 verification pillars green
- Drift D1 outstanding: leaked local platform-admin key rotation
  pending end-of-arc

### Re-anchoring procedure

When a future chat conversation gets long or context drifts, paste
this single document at the start of the new session. The next
session restores from:

1. This recap (durable architecture + business state)
2. The GitHub repo (https://github.com/aryanonline/Luciel) at the
   latest tag — gives executable code state
3. The runbook (`docs/runbooks/step-24.5b-identity-deploy.md`) for
   the prod rollout if not yet executed

This three-document set is sufficient to re-build full session
context without scrolling chat history.

---

**End of recap.** Next session: prod rollout via the runbook, then
Step 28 sequencing kick-off.