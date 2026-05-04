# Luciel Canonical Recap

**Version:** v1.5
**Last updated:** 2026-05-04 ~19:00 EDT, after Commit A (`81b9e5a`), Commit D (`55a36b4`), the repo-hygiene `.gitignore` cleanup (`86239ab`), the **runbook §4 v2 revision** (`374912a`), and the **runbook §4.2 follow-up patch** (this commit, adding mandatory `-WorkerHost` argument to both dry-run and real-ceremony examples after the operator caught the drift on the first dry-run attempt). New drift entry `D-runbook-mint-missing-workerhost-arg-2026-05-04` logs the fix-on-fix honestly. The runbook revision rewrites `docs/runbooks/step-28-phase-2-deploy.md` §4 to mandate the Option 3 ceremony (`scripts/mint-with-assumed-role.ps1`) for Commit 4, removes the old §4.7 (`luciel_admin` rotation already done by P3-H on 2026-05-03 23:56 UTC), corrects SSM-path casing to canonical lowercase `/luciel/production/worker_database_url`, and adds a §4.0 pre-mint checklist + four-row prerequisite gate table (P3-J/K/G/H all ✅). v1 of §4 is preserved in commit `925c64a` and explicitly superseded in the revision-history block at the top of §4. Commit A fixed a one-line auth-middleware typo (`user_id = agent.user_id` shadowed the never-read local while leaving `request.state.actor_user_id` bound to `None`) that caused **every Pillar 13 A3 legitimate setup-turn `MemoryItem` insert to fail Postgres D11 NOT NULL and be silently swallowed** by the extractor's broad `except Exception`. Post-fix verification ran **19/19 GREEN** including Pillars 11 (async memory), 13 (spoof + legit), 16 (D11). Drift entry `D-pillar-13-a3-real-root-cause-2026-05-04` resolved by `81b9e5a`. Commit D archived the 19/19 run to `docs/verification-reports/` and removed the P13_DIAG instrumentation. Five new Phase-3 items (P3-M, P3-N, P3-O, P3-P, P3-Q) logged for the compounding observability/hygiene gaps the bug exposed. The repo-hygiene commit pulled forward `D-gitignore-duplicate-stanzas-2026-05-01` from Phase 4 (corrupted UTF-16 line + 6 duplicate patterns + stray quote). Broader repo audit found no other deletable orphans — historical runbooks/recaps and root-level task-def JSONs were preserved as audit evidence per the canonical "don't delete audit history" protocol. v1.5 supersedes v1.4's pre-fix Pillar-13 framing.
**Supersedes:** v1.4 (2026-05-03 23:56 UTC, post P3-H); v1.3 (2026-05-03 late-evening, post P3-J/G/K); v1.2 (2026-05-03 evening, mid-Phase-2 docs reconciliation); v1.1 (2026-05-03 Phase 2 mid-stream close); v1 (2026-05-03 Phase 1 close)
**Next update:** at Phase 2 full close (Commits 4–7 live in prod) OR when a strategic-question answer changes
**Source-of-truth rule:** if a chat recap or session summary contradicts this document, this document wins. Update via PR with rationale; do not produce contradicting recaps inline.

---

## Section 0 — Locked items (re-derivation prohibited)

These are settled. Any future recap must read from this section, not infer.

### 0.1 Locked: Strategic question answers (Q1–Q8)

| Q | Answer | Roadmap slot | Status |
|---|---|---|---|
| **Q1** Two-key confusion → unified scope creation | Single admin permission; caller's scope dictates what they can create. Agent ↔ LucielInstance split. One tenant-admin key at onboarding | Steps 23, 24.5/25a | ✅ Done |
| **Q2** Tenant/domain/agent dashboards showing business value | Three-tier views driven by trace aggregations + `DomainConfig.value_metrics` + workflow actions | Step 31 (after 34) | 📋 Planned |
| **Q3** Vector + graph hybrid retrieval | Yes; pg-recursive CTEs first, opt-in per domain via `DomainConfig.entity_schema`; graduate to Neo4j/AGE at 100 tenants | Step 37 (after 35) | 🔬 Decision-gate |
| **Q4** Luciels communicating within a scope (Councils) | Yes; orchestrator Luciel; inter-Luciel tool calls; ScopePolicy policed at tool-call time; widget key can resolve to council_id; optional column on LucielInstance | Step 36 (after 33 eval gate) | 📋 Planned |
| **Q5** Bottom-up expansion (Sarah → her domain → her company) | Email-stable User identity; tenant-merge endpoint re-parents Luciels/knowledge/memories/sessions; pricing tiers + pro-rated credit | Step 38 (after 35) | 📋 Planned |
| **Q6** Role changes (promote/demote/depart) | Data lives with scope, not person; `users` + `scope_assignments`; mandatory hard key rotation; immutable audit log; Luciels and knowledge owned by scope | Step 24.5b, commit `adc5ba0` | ✅ Done |
| **Q7** Multi-channel delivery (widget/phone/email/SMS) | Channel adapter framework; inbound webhooks + outbound Tool registrations scoped via ScopePolicy; channels emergent from config, not a column | Step 34a candidate (between 33 and 34) | 📋 Candidate |
| **Q8** Cross-channel session continuity (widget ↔ phone) | Add `conversation_id` FK on sessions (session-linking, not session-merging); cross-session retriever pulls recent messages from other open sessions in same conversation; phone/email become identity claims linked to `users.id` | Step 24.5c candidate (after 28, before 30) | 📋 Candidate |

### 0.2 Locked: Pricing tier structure (2026-05-01 v3 reframe)

| Tier | Price | Audience | Scope |
|---|---|---|---|
| **Individual** | $30–80/mo | Single agent ("Sarah") | One agent scope, her LucielInstances only |
| **Team / Domain** | $300–800/mo | A team within a brokerage | Domain scope, multiple agents under it |
| **Company / Tenant** | $2,000+/mo | Whole brokerage | Tenant scope, all domains + agents |

**Structural insight:** Pricing tiers map 1:1 to the scope hierarchy `agent → domain → tenant`. The architecture diagram and the price card are the same diagram. **Scope-correctness IS the price-card enforcer** — leaked agent → sibling-agent read at Individual tier gives away Team-tier value at Individual-tier price.

**Tier B (Step 33b):** Per-customer dedicated AWS account/RDS/ECS. Build only when first prospect demands. Most enterprise needs handled by Tier 3 multi-tenant.

**Tier C (on-prem):** Excluded unless paid prospect asks. Tier B is highest pre-built isolation tier.

### 0.3 Locked: Roadmap (Steps 24.5c–38)

| Category | Step | Description | Status |
|---|---|---|---|
| Hardening | **28** | Operational maturity sprint — security/compliance, observability, hygiene, cosmetic — **4 phases**, 13–15 commits | **Phase 1 complete** at `bd9446b` (tag `step-28-phase-1-complete`). **Phase 2 code-only portion shipped** (Commits 2/2b/3/8/9 on `step-28-hardening-impl`); Phase 2 prod-touching commits 4–7 packaged as code+IaC+runbook (`docs/runbooks/step-28-phase-2-deploy.md`), pending hands-on prod execution. Phases 3/4 pending |
| Identity | **24.5c** | User identity claims (phone/email/SSO) + conversation grouping (Q8) | Candidate, slot before Step 30 |
| Testing | **29** | Automated testing suite (pytest wrap of `app.verification`) | Planned |
| Billing | **30a** | Stripe billing (subscription tiers, webhooks, tier-gated features) | Candidate, slot between 29 and 30 |
| Frontend | **30b** | Embeddable chat widget (JS drop-in for tenant sites) — **highest-leverage commit, REMAX trial unblock** | Planned |
| Frontend | **31** | Hierarchical tenant dashboards (Q2) | Planned (after 34) |
| Frontend | **32** | Agent self-service (Sarah spins up her own LucielInstances under her agent scope) | Planned |
| Frontend | **32a** | File input support (per-agent config, builds on 25b parsers) | Planned (may merge with 32) |
| Intelligence | **33** | Evaluation framework (relevance, persona consistency, escalation precision scoring) | Planned |
| Enterprise | **33b** | Dedicated tenant infrastructure tier (per-customer AWS account) | Candidate, build when prospect demands |
| Intelligence | **34** | Workflow actions (book appointment, send email, create lead, query DB) — unlocks Step 31 with real business-value data | Planned |
| Intelligence | **34a** | Channel adapter framework (SMS/email/phone, Q7) | Candidate, slot between 33 and 34 |
| Intelligence | **35** | Multi-vertical expansion framework (repeatable playbook for legal/mortgage/engineering) | Planned |
| Advanced | **36** | Luciel Council (multi-Luciel orchestration within scope, ScopePolicy at tool-call time, Q4) | Planned (after 33 eval gate) |
| Advanced | **37** | Hybrid retrieval (graph + vector, pg-recursive CTEs first, Neo4j/AGE at 100 tenants, Q3) | Planned (after 35) |
| Advanced | **38** | Bottom-up expansion / tenant merge (email-stable identity re-parents Luciels/knowledge/memories/sessions, Q5) | Planned |

### 0.4 Locked: Architectural decisions

- **Scope hierarchy IS billing boundary** (4 levels: tenant → domain → agent → luciel_instance, plus orthogonal users/api_keys/scope_assignments)
- **Soft-delete model** — all DELETE endpoints flip `active=false`. No hard-delete API surface. Hard-purge is scheduled retention worker (future).
- **Tenant cascade in code** — `PATCH /api/v1/admin/tenants/{id}` with `active=false` triggers atomic in-code cascade through all 7 child resource types. Walker is now backup tool.
- **Driver:** psycopg v3 (not psycopg2)
- **Manifest:** pyproject.toml (not requirements.txt)
- **Schema naming:** `_configs` suffix (tenant_configs, domain_configs, agent_configs)
- **Prod region:** ca-central-1, account 729005488042
- **Prod URL:** api.vantagemind.ai (ALB-fronted, NOT API Gateway)
- **Database:** RDS Postgres, master role `luciel_admin`. New role `luciel_worker` exists with least-privilege grants. **Step 28 Phase 2 Commit 4 will swap the worker to use it AND rotate the `luciel_admin` password** — packaged as runbook, pending hands-on execution.
- **Retention purges are batched** as of Phase 2 Commit 8 (`0d75dfe`). Defaults: 1000 rows/batch, 50 ms inter-batch sleep, 10000 batches/run cap. `FOR UPDATE SKIP LOCKED` keeps purges safe to run alongside live chat traffic. Tunable via `LUCIEL_RETENTION_*` env vars.
- **Operator patterns codified:** E (secrets), N (migrations), O (recon), S (cleanup walker)
- **Three-channel audit** for every prod mutation: CloudTrail + CloudWatch + admin_audit_logs

### 0.5 Locked: Deliberate exclusions

These require **roadmap-level conversation** to add, not commit-level.
- **No mobile app** — chat widget covers surface
- **No marketplace / user-generated Luciels** — verticals are operator-defined
- **No model training / fine-tuning** — foundation models via API; differentiation is judgment + integration depth
- **No internationalization** — ca-central-1, English-only, until customer demand surfaces
- **No Tier C on-prem** — unless paid prospect asks
- **No competitor feature-parity chasing** — if not on roadmap, deliberately out of scope

---

## Section 1 — Business model and identity

**What Luciel is:** B2B SaaS, multi-tenant AI assistant platform. Subscription tiers map 1:1 to scope hierarchy. Architecture and price card are the same diagram, scaled.

**Wedge market:** GTA brokerage. **REMAX Crossroads** = first warm lead, Tier 3 (Company-tenant, $2,000/mo). Markham-local outreach.

**Customer entity:** The tenant (a brokerage). **NOT** the end-user (the brokerage's clients).

**Initial vertical:** GTA real estate. Adjacent verticals later: mortgage, legal, engineering consulting (vertical-Q1 strategic question pending real lead signal).

---

## Section 2 — Architecture

### 2.1 Scope hierarchy (architectural primitive AND monetization primitive)
tenant → domain → agent → luciel_instance
Plus orthogonal: `user` (platform identity, tenant-agnostic, FK target for `actor_user_id`), `api_key` (auth credential, scoped to any level), `scope_assignment` (user-tenant binding).

### 2.2 Data tables

**Live-aware** (have `active` column, soft-delete model):
- tenant_configs, domain_configs, agents, agent_configs (legacy), luciel_instances, api_keys, scope_assignments, **memory_items** (active column added Step 27b)

**Append-only** (no `active` column):
- sessions, messages, traces, admin_audit_logs, knowledge_embeddings, retention_policies, deletion_logs, user_consents

**Identity layer:**
- users (no tenant_id, platform-wide)

### 2.3 memory_items — most sensitive data
Distilled user inferences from Step 27b async extraction. Contains preferences, identity facts, behavioral patterns. PIPEDA P5 (limit retention) target.

Columns: `user_id` (free-form brokerage-supplied), `tenant_id` (NOT NULL), `agent_id` (nullable), `category`, `content`, `active`, `message_id` FK, `luciel_instance_id` FK, `actor_user_id` (UUID FK to users, **NOT NULL** as of `f392a842f885` applied 2026-05-02).

Scope: 3-level (tenant + agent + luciel_instance). Domain inferred via agent (no `domain_id` column on memory_items).

### 2.4 Auth + audit
- API keys: `luc_sk_` prefix + 12-char prefix indexed for audit
- AuditContext factories: `from_request()` (HTTP), `system()` (background jobs), `worker()` (Celery — preserves enqueuing key prefix, fixed Commit 14)
- AdminAuditLog: canonical audit trail, every mutation writes one row in same transaction, ALLOWED_ACTIONS allow-list
- Three-channel audit pattern: CloudTrail + CloudWatch + admin_audit_logs

### 2.5 Cascade discipline (Commit 12, `f9f6f79`)
PATCH `/api/v1/admin/tenants/{id}` with `active=false` triggers atomic in-code cascade through 7 leaves:
1. memory_items (broadest scope)
2. api_keys
3. luciel_instances (all scope levels)
4. agents (new-table)
5. agent_configs (legacy)
6. domain_configs
7. tenant_config itself

Plus sub-tenant cascade on agent/domain/instance deactivation. Pillar 18 enforces end-to-end. Walker is now backup tool.

### 2.6 Operator patterns (codified in `docs/runbooks/operator-patterns.md`)
- **Pattern E** — Secret handling discipline (Commit 9, `bd9446b`)
- **Pattern N** — Prod migrations via `luciel-migrate:N` ECS one-shot
- **Pattern O** — Read-only prod recon via `luciel-recon:1` ECS one-shot
- **Pattern S** — Per-resource cleanup walker (backup tool as of Commit 12)

---

## Section 3 — What's accomplished (Step 28 Phase 1 complete; Phase 2 code-only commits shipped)

### 3.1 Phase 1 commits shipped (chronological, verified from git log)

| Hash | Date | Commit |
|---|---|---|
| `adc5ba0` | 2026-04-27 | Step 24.5b — Durable User Identity Layer (Q6 + Q5 prerequisite) |
| `81c0088` | 2026-04-27 | D1 closure — rotate leaked local platform-admin key |
| `330d975` | 2026-04-27 | Master plan + Phase 1 tactical plan + drift register seed |
| `e024dd4` | 2026-04-27 | Mid-Phase-1 canonical recap |
| `679db3d` | 2026-04-27 | D16 consent route double-prefix fix + dependent test callsites |
| `baf06f7` | 2026-04-27 | D11 memory_items.actor_user_id NOT NULL flip + Pillar 16 |
| `67c65a5` | 2026-04-30 | D5 — retrofit ApiKeyService.deactivate_key with audit_ctx |
| `40d9fb8` | 2026-04-30 | D-worker-role — least-privilege luciel_worker Postgres role |
| `ca55b12` | 2026-04-30 | Commit 8a — luciel-worker mint script + worker-sg runbook |
| `8028699` | 2026-05-01 | Commit 8a (artifacts only second instance) |
| `7560397` | 2026-05-01 | Commit 8b-prereq — fix luciel-migrate task-def + codify Pattern N |
| `9ff2690` | 2026-05-01 | Commit 8b-prereq-data — codify Pattern O + discover prod tenant residue |
| `15bd315` | 2026-05-01 | Commit 8b-prereq-cleanup — clear 18 active verification residue tenants via Pattern S walker |
| `f9f6f79` | 2026-05-02 | **Commit 12: tenant cascade in code (PIPEDA P5)** — deployed + smoke-tested in prod |
| `3d64ca9` | 2026-05-02 | chore — untrack msg.txt session-authoring scratchpad |
| `62a5783` | 2026-05-02 | **Commit 11: orphan cleanup migration** — applied to prod, 3 migrations + NOT NULL flip live |
| `6b71bcb` | 2026-05-02 | **Commit 10: admin memory endpoints + stash integration** — deployed |
| `2e31797` | 2026-05-02 | **Commit 14: worker audit attribution fix** (Pillar 13 A2) |
| `bd9446b` | 2026-05-03 | **Commit 9: Phase 1 close** — Pattern E codified, tag `step-28-phase-1-complete` |

### 3.1b Phase 2 commits shipped (code-only portion)

Branch: `step-28-hardening-impl` (NOT yet merged to `step-28-hardening`).

| Hash | Commit |
|---|---|
| `75f6015` | **Phase 2 Commit 2** — audit-log API mount (`GET /api/v1/admin/audit-log`); closes recap §4.1 item 4 + drift `D-audit-log-api-404` |
| `bfa2591` | **Phase 2 Commit 2b** — audit-log review fixes: H1 (route prefix), H2 (per-resource `_SAFE_DIFF_KEYS` allow-list redaction), H3 (real second-tenant scope guard test), M1/M3, L1/L2 |
| `56bdab8` | **Phase 2 Commit 3** — Pillar 13 A3 sentinel-extractable fix (user-fact-shaped setup turn, A3 keyed on `MemoryItem.message_id` FK, 30 s polling). Brings dev verification 17/18 → 19/19 (Pillar 19 audit-log mount also green). |
| `0d75dfe` | **Phase 2 Commit 8** — batched retention deletes/anonymizes via `FOR UPDATE SKIP LOCKED LIMIT n` chunks with per-batch commit. Settings: `retention_batch_size`, `retention_batch_sleep_seconds`, `retention_max_batches_per_run`. Partial-failure semantics: writes `DeletionLog` with `"PARTIAL: ..."` reason then re-raises. Removes outer `db.rollback()` from `enforce_all_policies` (now harmful with per-batch commits + auto-committing audit log). |
| `925c64a` | **Phase 2 Commit 9** — Phase 2 close (code-only portion): canonical recap v1.1 + new `docs/runbooks/step-28-phase-2-deploy.md` covering all 9 Phase-2 commits incl. runbook sections for prod-touching Commits 4–7. |
| `2c7d0fb` | **Phase 2 HOTFIX** — Pillars 7 (test drift), 17 (real bug), 19 (test design flaw); restores dev verification to green and unblocks Commit 4 attempt. |
| `31e2b16` | **Phase 3 compliance backlog seeded** — `docs/PHASE_3_COMPLIANCE_BACKLOG.md` (P3-A through P3-G). Items surfaced during Phase 2 hotfix diagnosis but deliberately deferred to Phase 3 to keep Phase 2 focused. |
| `81b9e5a` | **Phase 2 Commit A (Pillar 13 A3 real fix)** — one-line auth-middleware binding fix (`user_id = agent.user_id` → `actor_user_id = agent.user_id`, `app/middleware/auth.py:124`) + 12-line forensic comment + 5-test regression guard at `tests/middleware/test_actor_user_id_binding.py` (AST canary + 4 behavioral). Two-way proven (FAIL with bug, PASS with fix). Resolves drift `D-pillar-13-a3-real-root-cause-2026-05-04`. Post-fix verification 19/19 green. |
| `13035da` | **Phase 2 Commit D (P13_DIAG removal + verification archive)** — strips P13_DIAG instrumentation from `app/middleware/auth.py` (17 lines) and `app/services/chat_service.py` (41 lines), deletes `diag_p13_repro.py` (269 lines), archives 19/19 verification report at `docs/verification-reports/step28_phase2_postA_sync_2026-05-04.json` + README. Commit B (extractor B-hybrid) and Commit C (async-flag flip) WITHDRAWN — second post-Commit-A repro proved the system was always architected correctly for prose+tool-call replies and Pillar 11 (async path) was already green. Net −147 lines. See `docs/recaps/2026-05-04-pillar-13-a3-real-root-cause.md` for full forensic narrative including 3 discarded hypotheses. |
| `2b5ff32` | **Phase 2 Commit 4 mint-script hardening** — `scripts/mint_worker_db_password_ssm.py` rebuilt to never log a constructed DSN, never accept admin DSN as runtime input, suppress full-DSN error bodies, and log only sanitized SSM ARN identifiers. Authored after a dry-run of the original mint script leaked the admin DSN (incl. password `LucielDB2026Secure`) into CloudWatch log group `/ecs/luciel-backend` stream `migrate/luciel-backend/d6c927a05eb943b5b343ca1ddef0311c`. |
| `43e2e7a` | **Mint-incident recap** — `docs/recaps/2026-05-03-mint-incident.md`. Five root causes: (1) admin DSN as input parameter; (2) full-DSN error bodies; (3) shared `luciel-ecs-migrate-role` task role with no separation between migrate and mint duties; (4) no MFA enforcement on the human identity (`luciel-admin`) doing privileged ops; (5) no compensating control for accidental log-line leakage. Drove the P3-J / P3-K / P3-H additions to the Phase 3 backlog. |

**Not yet shipped (prod-touching, packaged for hands-on execution):**

| # | Description | Why packaged-not-executed |
|---|---|---|
| 4 | Worker DB role swap to `luciel_worker` + `luciel_admin` password rotation | **BLOCKED on P3-J + P3-K (Option 3 architecture).** First mint attempt (2026-05-03) leaked the admin DSN to CloudWatch via `--dry-run` error body. Hardened mint script (`2b5ff32`) is necessary but not sufficient: prerequisites are (a) MFA enabled on `luciel-admin` per P3-J; (b) dedicated `luciel-mint-operator-role` (MFA-required, scoped to `ssm:GetParameter` on `/luciel/database-url` + KMS Decrypt) per P3-K; (c) leaked password rotation per P3-H (rotate `luciel_admin`, delete the leaking log stream). Migrate task role NEVER receives read on admin DSN. Runbook §4 must be revised to invoke the mint via `aws sts assume-role --serial-number ... --token-code ...` ceremony before re-attempt. |
| 5 | 5 CloudWatch alarms (SQS backlog, DLQ, RDS conn, ECS CPU, ALB 5xx) + SNS pipeline | Touches CloudWatch + SNS. Runbook: §5. CFN template stub `infra/cloudwatch/alarms.yaml` to be authored at execution time. |
| 6 | ECS auto-scaling target tracking (web on CPU, worker on SQS depth) | Touches ECS + Application Auto Scaling. Runbook: §6. |
| 7 | Container-level healthChecks (web `curl localhost:8000/health`, worker `celery inspect ping`) | Touches ECS task-defs. Runbook: §7. |

### 3.2 Prod state at Phase 1 close
- Branch: `step-28-hardening`, HEAD `bd9446b`, tag `step-28-phase-1-complete`
- Backend service: `luciel-backend:17` (digest `sha256:39fecc49...95193`)
- Alembic head: `f392a842f885`
- memory_items.actor_user_id NOT NULL enforced
- Cascade-in-code verified end-to-end via prod smoke test on `step28-smoke-cascade-372779`
- Pillar 18 (tenant cascade end-to-end) green on dev
- Dev verification at Phase 1 close: 17/18 pillars green, Pillar 13 only red (A3 — sentinel-not-extractable, deferred to Phase 2)
- **Dev verification post-Phase-2-Commit-3:** 19/19 green (Pillar 13 A3 fixed + Pillar 19 audit-log mount included)

### 3.3 Phase 1 business impact
- **PIPEDA Principle 5 compliance** real in prod (cascade-in-code)
- **Audit log integrity** — append-only by database grant (worker can't UPDATE/DELETE pending Phase 2 role swap)
- **Atomic cascade** — no half-states on tenant deactivation
- **Brokerage DD audit story** defensible: "what data persists for ended tenancies?" answers cleanly
- **Tier 3 (REMAX) compliance baseline** — Pattern N/O/S/E discipline defensible to compliance officers
- **Stripe (Step 30a) precondition met** — programmatic cascade exists; subscription cancellations safe to wire when Step 30a ships

### 3.4 What Phase 1 did NOT deliver (intentional)
- No new product surface (no widget, no dashboard, no UI)
- No new revenue (zero paying customers — Phase 1 is pre-revenue hardening)
- REMAX trial still blocked on Step 30b (chat widget)
- Worker still runs as `luciel_admin` superuser (Phase 2 swap pending)

---

## Section 4 — What's next

### 4.1 Step 28 Phase 2 — Operational hardening (in progress)

**Code-only portion: SHIPPED on `step-28-hardening-impl`.** See §3.1b.
- ~~Pillar 13 A3 fix~~ ✅ Commit 3 `56bdab8`
- ~~Audit-log API mount~~ ✅ Commit 2 `75f6015` + Commit 2b `bfa2591` review fixes
- ~~Batched retention~~ ✅ Commit 8 `0d75dfe`
- ~~Phase 2 deploy runbook + recap update~~ ✅ Commit 9 (this commit)

**Prod-touching portion: PACKAGED, awaiting hands-on execution.** See `docs/runbooks/step-28-phase-2-deploy.md`.
1. **Commit 4 — Worker DB role swap** — mint `luciel_worker` password via `scripts/mint_worker_db_password_ssm.py` (run via Pattern N one-shot); write `WORKER_DATABASE_URL` SSM; update worker task-def; deploy. Then rotate `luciel_admin` password (drift `D-prod-superuser-password-leaked-to-terminal-2026-05-03`).
2. **Commit 5 — CloudWatch alarms** — 5 alarms (SQS >50, DLQ >0, RDS conn >80%, ECS CPU >80%, ALB 5xx >1%) + SNS topic + email subscription. CFN template `infra/cloudwatch/alarms.yaml`.
3. **Commit 6 — ECS auto-scaling** — web service target-tracks 50% CPU; worker service target-tracks SQS messages-per-task = 10. Min 1, Max 4 each. CFN templates under `infra/autoscaling/`.
4. **Commit 7 — Container healthChecks** — backend `curl -fsS localhost:8000/health`, worker `celery inspect ping`. Belt-and-suspenders against ALB target group health.

**Phase 2 full close gate:** `python -m app.verification` 19/19 green against prod, all 5 alarms `OK`, both auto-scaling targets registered, both services on healthCheck-enabled task-def revisions, `pg_stat_activity` shows zero worker connections as `luciel_admin`, **AND** the following P3 prerequisites for Commit 4 are satisfied:
- **MFA enforced on `luciel-admin`** — `aws iam list-mfa-devices --user-name luciel-admin` returns a non-empty `MFADevices` array (P3-J resolved). ✅ **Verified 2026-05-03 23:48:11 UTC** — `SerialNumber: arn:aws:iam::729005488042:mfa/Luciel-MFA`. Account-wide sweep (`aws iam list-users`) confirmed `luciel-admin` is the only IAM user, so privileged-human MFA boundary is fully closed.
- **Dedicated `luciel-mint-operator-role` exists with MFA-required AssumeRole** — `aws iam get-role --role-name luciel-mint-operator-role` returns a trust policy with `Bool: aws:MultiFactorAuthPresent=true` and `NumericLessThan: aws:MultiFactorAuthAge=3600` (P3-K resolved). The migrate task role is NOT granted read on `/luciel/database-url`. ✅ **Verified 2026-05-04 00:14:10 UTC** (CreateDate). Trust policy, inline permission policy `luciel-mint-operator-permissions`, and `MaxSessionDuration: 3600` all match `infra/iam/*.json` design byte-for-byte. Smoke test (`mint-with-assumed-role.ps1 -DryRun`) succeeded at 2026-05-04 00:19:22 UTC; `aws ssm get-parameter --name /luciel/production/worker_database_url` returned `ParameterNotFound` post-smoke-test, confirming the dry-run wrote nothing.
- **Migrate-role policy diff applied** — `aws iam get-role-policy --role-name luciel-ecs-migrate-role --policy-name luciel-migrate-ssm-write` returns 6 SSM actions including `ssm:GetParameterHistory` (P3-G resolved). ✅ **Verified 2026-05-03 evening.** Live policy matches `infra/iam/luciel-migrate-ssm-write-after-p3-g.json` byte-for-byte.
- **Leaked admin password rotated and leaking log stream deleted** — `aws logs filter-log-events --log-group-name /ecs/luciel-backend --filter-pattern '"LucielDB2026Secure"'` returns zero events (P3-H resolved). ✅ **Verified 2026-05-03 23:56:22 UTC.** RDS rotation 23:18:31 UTC; SSM `/luciel/database-url` v1→v2 at 23:22:54 UTC; §4 SQLAlchemy ECS verification `P3H_VERIFY_OK select=1 user=luciel_admin db=luciel` at 23:31:53 UTC; contaminated stream `migrate/luciel-backend/d6c927a05eb943b5b343ca1ddef0311c` deleted at 23:52:16 UTC; §7 final sweep returned 0 hits across `/ecs/luciel-backend`, `/ecs/luciel-worker`. Residual SSM history v1 plaintext tracked as P3-L (P2, deferred post-Commit-4).

When met, tag `step-28-phase-2-complete`.

Estimated (REVISED 2026-05-03 evening): 4 prod-touching commits + 3 P3 prerequisites (J, K, H), ~5–7 hours total wall-clock across 2–3 sessions for hands-on execution. Runs in parallel with Steps 29/30.

### 4.2 Step 28 Phase 3 — Hygiene + compliance hardening

**Authoritative tracker:** `docs/PHASE_3_COMPLIANCE_BACKLOG.md` (commit `31e2b16` + 2026-05-03 evening rescope). Items below are a flat snapshot; the backlog file is the canonical priority and sequencing source.

- ~~**P3-J (P0, NEW 2026-05-03)**~~ — ✅ **RESOLVED** 2026-05-03 23:48 UTC. MFA on `luciel-admin` (`Luciel-MFA`). Account has only one IAM user; privileged-human MFA boundary is fully closed.
- ~~**P3-K (P1, NEW 2026-05-03)**~~ — ✅ **RESOLVED** 2026-05-04 00:14 UTC (role created + permission policy + smoke-test verified). `luciel-mint-operator-role` live with MFA-required AssumeRole, scoped read on `/luciel/database-url` + KMS Decrypt via SSM. Helper `scripts/mint-with-assumed-role.ps1` shipped in commit `9e48098`; mint script `--admin-db-url-stdin` flag in `ce66d06`.
- ~~**P3-G (P2, RESCOPED 2026-05-03)**~~ — ✅ **RESOLVED** 2026-05-03 ~20:09 EDT. `ssm:GetParameterHistory` added to `luciel-migrate-ssm-write`; live policy matches design byte-for-byte.
- ~~**P3-H (P1, RESOLVED 2026-05-03)**~~ — ✅ **RESOLVED** 2026-05-03 23:56:22 UTC. RDS master pw rotated, SSM v1→v2, §4 ECS SQLAlchemy verification passed, contaminated CloudWatch stream deleted, residual sweep clean. Full timeline + audit metadata in `PHASE_3_COMPLIANCE_BACKLOG.md` P3-H section.
- **P3-L (P2, NEW 2026-05-03, DEFERRED)** — SSM parameter `/luciel/database-url` history v1 retains plaintext `LucielDB2026Secure` after the P3-H rotation. Only `luciel-admin` (MFA-gated per P3-J) can read parameter history. Mitigation: delete-and-recreate the SSM parameter post-Commit-4. See `PHASE_3_COMPLIANCE_BACKLOG.md` P3-L for full rationale and fix shape.
- Dedicated read-only recon role
- Pattern O helper script extraction (`scripts/run_prod_recon.ps1`)
- LucielInstanceRepo `_for_agent`/`_for_domain` cascade autocommit-aware
- RESOURCE_KNOWLEDGE duplicate definition cleanup
- Memory admin endpoints test coverage (dedicated pillar)
- CloudWatch retention policies (365-day cap)
- Mint script accepts SQLAlchemy dialect prefix

### 4.3 Step 28 Phase 4 — Cosmetic (single-sweep candidate)
- `.gitignore` dedup
- Markdown re-fencing in runbooks
- UTF-8 display-name cleanup
- JMESPath label fixes
- PowerShell quoting drift codifications

### 4.4 Post-Phase-1 roadmap (Steps 29–38)

**Step 29 — Automated test suite** (1–2 sessions)
Convert `app.verification` to pytest. CI gate on every push.

**Step 30a — Stripe billing** (2–3 sessions, candidate)
Subscription tiers, signature-validated webhooks, `subscription.deleted` → cascade, tier-gated flags.

**Step 30b — Embeddable chat widget** (3–5 sessions)
**THE highest-leverage commit on the roadmap.** REMAX trial unblock.

**Step 31 — Hierarchical tenant dashboards** (after Step 34)
Three-tier hierarchical business-value attribution.

**Step 32 — Agent self-service**
Sarah spins up her own LucielInstances under her agent scope.

**Step 32a — File input UX**
Drag-drop knowledge ingestion. May merge with Step 32.

**Step 33 — Evaluation framework**
Automated scoring. Decision gate before Step 36 Council.

**Step 33b — Dedicated tenant infrastructure (Tier B)**
Per-customer AWS account. Build only when prospect demands.

**Step 34 — Workflow actions**
External integrations: Calendly, Gmail, HubSpot, listing DB. Step 31 dashboards depend on this.

**Step 34a — Channel adapter framework** (candidate)
SMS, email, phone. Channels emergent from config, not a column.

**Step 35 — Multi-vertical expansion framework**
Repeatable playbook for legal/mortgage/engineering.

**Step 36 — Luciel Council** (after Step 33 eval gate)
Multi-Luciel orchestration within scope. Orchestrator + tool-call ScopePolicy + council_id resolution.

**Step 37 — Hybrid retrieval** (after Step 35)
Vector + graph. CTEs first, opt-in via `DomainConfig.entity_schema`, Neo4j/AGE at 100 tenants.

**Step 38 — Bottom-up expansion / tenant merge** (after Step 35)
Email-stable User identity. Tenant-merge endpoint + pro-rated billing credit.

---

## Section 5 — Future concepts / design surface

NOT on the numbered roadmap. Kept here so they don't get rediscovered as "new ideas":
- **Voice-first Luciel** — channel adapter beyond SMS/phone
- **Real-time co-pilot mode** — with-consent listening, real-time suggestions
- **Cross-tenant referral graph** — network effect at scale
- **Knowledge marketplace** — curated, operator-curated (NOT user-generated)
- **Compliance-as-code** — codified compliance contracts for regulated verticals
- **Anonymous benchmarking across tenants** — aggregated metrics
- **Tenant-side admin AI (meta-Luciel)** — AI helping tenant admins manage own deployments

---

## Section 6 — Pricing strategy

### 6.1 Tier card (locked, see §0.2)

### 6.2 Per-tier characteristics

**Tier 1 Individual ($30–80/mo):**
- Customer IS the end-user
- Self-service onboarding
- Cancellation fully automatic via Stripe webhook
- Memory cascade automatic
- **Hard dependency: code-level cascade** (✅ Commit 12)
- Volume play: 60–170 customers to reach $5K MRR

**Tier 2 Team ($300–800/mo):**
- Team lead within brokerage
- Domain scope, multi-agent
- Operator-onboardable for first 5–10, self-service after Step 32
- **Hard dependency: scope-correctness enforcement** (Pillars 7, 8, 13)

**Tier 3 Company ($2,000+/mo):**
- Brokerage itself
- Whole-tenant scope
- Operator-onboarded indefinitely (high-touch)
- Audit-heavy brokerage DD
- **Hard dependency: Step 28 Phase 1 hardening** (✅ done)
- REMAX Crossroads = first warm lead

### 6.3 Unit economics intent
- Per-tenant gross margin target: 70%+ at scale
- Foundation model API costs: pass-through with margin OR included up to tier cap
- AWS infra: small per-tenant once N>10
- Founder time: 100% Aryan; scales with vertical expansion, not tenant count

### 6.4 Churn target
- Individual + Team: 5%/year
- Company: 2%/year
- Sticky due to: accumulated KB + cascade-correct departure + integration depth

---

## Section 7 — Moat (priority order)

1. **Integration depth per vertical** — 6 months of ingested knowledge + workflows wired into CRM/calendar = high switching cost
2. **Audit posture** — brokerage-DD-defensible operator discipline (this commit's tag = real proof)
3. **Scope hierarchy correctness** — competitors usually treat AI memory as flat; Luciel's `tenant→domain→agent→instance` maps to real org structure
4. **Cascade-correct departure semantics** — when agent leaves or tenant cancels, access deactivates correctly without manual intervention

---

## Section 8 — Five willingness-to-pay drivers

Brokerages don't pay for "we have an LLM wrapper." They pay for:
1. **Maintainability** — codify, don't tribal-knowledge
2. **Scalability** — per-tenant operations generalize
3. **Reliability** — idempotency, no half-states
4. **Security** — Pattern E discipline, no keys leak
5. **Traceability** — three-channel audit per mutation

Every Step 28 Phase 1 commit maps to one of these pillars.

---

## Section 9 — Go-to-market phases

**Phase 1 (current) — REMAX Crossroads warm trial**
1-on-1 outreach via Markham real-estate connections. Free trial in exchange for case study + audit-log demo rights. **Gated by Step 30b (chat widget).**

**Phase 2 — REMAX referral expansion**
Crossroads as reference customer. ~50 brokerages within 30km of Markham. Tier 3 contracts, 6-month minimum.

**Phase 3 — Multi-brand expansion**
Royal LePage, Coldwell Banker, Century 21. Same wedge, next brand.

**Phase 4 — Adjacent verticals**
Insurance, legal, mortgage. (Vertical-Q1 strategic question — which first.)

---

## Section 10 — Revenue milestones

- **Milestone 1 — First paying tenant:** REMAX Crossroads, Tier 3 ($2,000/mo). Gated by Step 30b. ETA Q3 2026.
- **Milestone 2 — $5K MRR:** Achievable shapes:
  - 1 Company + 5 Team + 20 Individual = $5.5K (realistic mix, ✅ Commit 12 dependency met)
  - 60–170 Individual @ $30–80 (volume play, requires Commit 12)
  - 2–3 Company @ $2K (DD-heavy)
- **Milestone 3 — $10K MRR:** ~5 Tier 3, Q1 2027
- **Milestone 4 — $100K MRR:** 50 Tier 3 + 100 Tier 2, Q4 2027
- **Milestone 5 — $1M ARR:** Mixed tiers + Tier 4, Q4 2028

**Critical-path:** First revenue does NOT require Commit 12 (REMAX is Tier 3). Milestone 2 DOES require Commit 12. Both shipped today.

---

## Section 11 — Compliance posture

### 11.1 Defensible NOW
- PIPEDA Principle 5 (limit retention) via cascade-in-code
- PIPEDA Principle 1 (accountability) via AuditContext + worker audit attribution
- Atomic transactions on tenant deactivation
- Operator pattern discipline (Pattern E/N/O/S codified)

### 11.2 NOT defensible yet
- GDPR Article 17 right-to-deletion (per-end-user, future)
- GDPR Article 20 data portability export (future)
- SOC2 / HIPAA (would need security audit + Tier B)
- Encryption-at-rest documentation (RDS encrypts; DD packet doesn't yet codify)
- Hard-purge timing SLA (soft-deleted rows persist; future retention worker)
- **MFA on privileged human identities** — `luciel-admin` IAM user has `MFADevices: []` as of 2026-05-03 evening. P3-J fixes; brokerage DD will fail this check until resolved.
- **Separation-of-duties on operator IAM roles** — `luciel-ecs-migrate-role` is currently used for both Alembic migrations and password mint. P3-K splits mint into a dedicated MFA-required `luciel-mint-operator-role`. Until then the blast radius of a compromised migrate task role includes admin-DSN read.
- **Audit-emission gaps for IAM-side privileged actions** — AssumeRole calls into the future `luciel-mint-operator-role` will land in CloudTrail, but Luciel's `admin_audit_logs` does not yet ingest CloudTrail. Considered acceptable for Phase 2, but explicit gap for Tier B / SOC2 readiness.
- **Plaintext credential rotation hygiene** — leaked `luciel_admin` password (`LucielDB2026Secure`) sits in CloudWatch log group `/ecs/luciel-backend` stream `migrate/luciel-backend/d6c927a05eb943b5b343ca1ddef0311c` until P3-H rotates and deletes.

### 11.3 Brokerage DD answer template
"When a brokerage cancels their subscription, every memory data point, every API key, every agent persona, every domain, every Luciel instance flips to inactive in a single atomic transaction. Audit logs in admin_audit_logs show exactly what was deactivated, when, by whom, and what cascade reason. Soft-deleted rows scheduled for hard-purge within [N days] (future retention worker)."

---

## Section 12 — Working memory anchors (drift recovery)

If conversation context is lost, these are the most important facts to preserve:

1. **Pricing is scope-aligned:** Individual=agent, Team=domain, Company=tenant. Architecture diagram = price card.
2. **Commit 12 (`f9f6f79`) unblocks the Individual tier** — without code-level cascade, $30/mo tier cannot ship.
3. **REMAX Crossroads is Tier 3 / $2K/mo**, gated by Step 30b widget, NOT by Commit 12.
4. **Step 30b is the highest-leverage commit on the roadmap** — REMAX trial unblock.
5. **Step 28 has 4 phases** (security/compliance, observability, hygiene, cosmetic). Phase 1 complete at `bd9446b`.
6. **Pillar 13 A3 fixed by Phase 2 Commit 3** (`56bdab8`) — was a test-design issue (sentinel-not-extractable), never a security gap. Dev now 19/19 green.
7. **Worker DB role swap is Phase 2 Commit 4** — packaged as runbook (`docs/runbooks/step-28-phase-2-deploy.md` §4), but the first mint attempt on 2026-05-03 leaked the admin DSN to CloudWatch. Re-attempt is BLOCKED on three P3 prerequisites: P3-J (MFA on `luciel-admin`), P3-K (dedicated `luciel-mint-operator-role` with MFA-required AssumeRole; migrate task role does NOT get admin-DSN read), and P3-H (rotate leaked `LucielDB2026Secure` + delete leaking log stream). Option 3 architecture is locked: human operator assumes the mint role via `aws sts assume-role --serial-number ... --token-code ...`, runs mint via `scripts/mint-with-assumed-role.ps1`, then the assumed credentials expire in ≤1 hour. Worker still runs as `luciel_admin` until all three resolve.
8. **Five willingness-to-pay drivers:** maintainability, scalability, reliability, security, traceability.
9. **Three deliberate exclusions:** no mobile, no marketplace, no model training. Adding any requires roadmap conversation.
10. **Operator patterns codified:** E (secrets), N (migrations), O (recon), S (cleanup, now backup). Runbooks at `docs/runbooks/`.
11. **Locked strategic-question answers:** Q1 ✅, Q2 → Step 31, Q3 → Step 37, Q4 → Step 36, Q5 → Step 38, Q6 ✅, Q7 → Step 34a, Q8 → Step 24.5c.
12. **This recap is source of truth** — if a chat recap contradicts it, this document wins. Update via PR with rationale.

---

## Section 13 — Resumption protocol

**Every new session begins with this 4-step ritual. No work proposed before completing it.**

### Step 1: Read this canonical recap
Get-Content docs/CANONICAL_RECAP.md

Read the full file. Do not infer from memory; re-read.

### Step 2: Read git state
git log -1 --format=fuller HEAD
git log --oneline -10
git status --short
git stash list


### Step 3: Run 5-block pre-flight (before any prod-touching work)

Block 1 — AWS identity (expect 729005488042):
`aws sts get-caller-identity --query Account --output text`

Block 2 — Git state (expect clean working tree):
`git status --short; git log -1 --oneline; git stash list`

Block 3 — Docker:
`docker info --format "{{.ServerVersion}} {{.OperatingSystem}}"`

Block 4 — Dev admin key (expect True / 50):
`$env:LUCIEL_PLATFORM_ADMIN_KEY.StartsWith("luc_sk_"); $env:LUCIEL_PLATFORM_ADMIN_KEY.Length`

Block 5 — Verification (expect 19/19 post-Phase-2-Commit-3):
`python -m app.verification`

If Block 5 returns anything other than 19/19 green, **diagnosis is the only acceptable next action.** Do not proceed to prod work on a red dev. (Pre-Phase-2 baseline was 17/18 with Pillar 13 A3 red — superseded by Commit 3 `56bdab8`.)

### Step 4: State back to user, in 5 lines
- Where we are (HEAD, phase, milestone)
- What's locked
- What's open
- What session-specific delta exists
- What we're about to do

Only after Step 4, propose work.

---

## Section 14 — Discipline reminders

- **Don't trust narrative recap over commit message** — `git log -1 --format=fuller HEAD` is canonical for code; this document is canonical for strategy
- **Use openapi.json as first source of truth** when prod exposes it
- **Pre-mutation recon is cheap** — $0.0007 per Pattern O query
- **Dry-run before real-run** — walker `-DryRun`, migration dev test, mint `--dry-run`
- **Idempotency** — re-run after mutation proves end-state, costs nothing
- **Three independent audit channels** for every mutation
- **Don't surgically regex-patch tool scripts** — rewrite full file
- **PowerShell quoting is fragile** — file-arg JSON for AWS CLI, never inline-Python via `python -c` from Windows shell
- **Stop sessions when verification goes red unexpectedly** — diagnose fresh, don't forge through
- **Trust-but-verify saves** — every code edit gets method-level / function-level existence check, not just import-success
- **Don't defer indefinitely** — push back on undefined "later"; Phase 2/3/4 IS scheduled, "later" without a phase is not
- **Don't substitute inference for memory** — re-read this recap and the prior commit when context gets long

---

## Section 15 — Drift register

### Phase 2 (operational hardening)
- Worker DB role swap (former Commit 13 work) — packaged as Commit 4, runbook §4 — **UNBLOCKED 2026-05-03 23:56 UTC** (P3-J + P3-K + P3-G + P3-H all resolved; ready for Option 3 ceremony execution)
- ~~D-prod-superuser-password-leaked-to-terminal-2026-05-03~~ — ✅ **RESOLVED** 2026-05-03 23:56:22 UTC via P3-H. RDS master pw rotated 23:18:31 UTC; SSM `/luciel/database-url` v1→v2 at 23:22:54 UTC; §4 SQLAlchemy ECS verify passed at 23:31:53 UTC; contaminated stream `migrate/luciel-backend/d6c927a05eb943b5b343ca1ddef0311c` deleted at 23:52:16 UTC; §7 final sweep 0 hits. See `docs/runbooks/step-28-p3-h-rotate-and-purge.md` (executed end-to-end with three inline runtime corrections); full audit metadata in `PHASE_3_COMPLIANCE_BACKLOG.md` P3-H.
- **D-ssm-parameter-history-retains-plaintext-2026-05-03** (NEW) — SSM `/luciel/database-url` history v1 still contains the rotated-out plaintext password. Tracked as P3-L (P2, deferred to post-Commit-4 cleanup). MFA-gated `luciel-admin` is the only principal that can read parameter history; mint-operator-role and task roles have no `GetParameterHistory` access.
- **D-mint-script-leaks-admin-dsn-via-error-body-2026-05-03** (NEW) — original mint script logged the constructed admin DSN on dry-run error path. Hardened by `2b5ff32`; full incident report at `docs/recaps/2026-05-03-mint-incident.md`. Resolved at code level; operator-side rotation is P3-H.
- **D-luciel-admin-no-mfa-2026-05-03** (NEW) — `aws iam list-mfa-devices --user-name luciel-admin` returns empty. Tracked as P3-J. ✅ **RESOLVED 2026-05-03 23:48:11 UTC** — virtual MFA `Luciel-MFA` enabled. Account-wide sweep confirms `luciel-admin` is the only IAM user; full privileged-human MFA boundary is closed.
- **D-migrate-role-conflated-with-mint-duty-2026-05-03** (NEW) — single `luciel-ecs-migrate-role` covers both Alembic migrations and mint operations. Splitting into dedicated `luciel-mint-operator-role` is P3-K.
- **D-canonical-recap-misdiagnosed-migrate-role-policy-gap-2026-05-03** (NEW, self-referential) — prior session asserted migrate role was missing `ssm:GetParameter` + `ssm:PutParameter`. Real read of `luciel-migrate-ssm-write` shows both are present; only `ssm:GetParameterHistory` is missing. P3-G rescoped P1 → P2 in `31e2b16` follow-up edit (2026-05-03 evening).
- **D-pillar-13-a3-real-root-cause-2026-05-04** (NEW) — auth middleware `app/middleware/auth.py:124` had `user_id = agent.user_id` (typo — never-read local) instead of `actor_user_id = agent.user_id`, leaving `request.state.actor_user_id = None`. Failure chain: chat turn passes `actor_user_id=None` to `MemoryService.extract_and_save` → INSERT violates Postgres D11 NOT NULL → IntegrityError swallowed by `except Exception` at `extract_and_save:116-119` (logs only `type(exc).__name__`, not `repr(exc)`) → chat returns 200 with assistant reply "I'll remember that" while zero `MemoryItem` rows are written. ✅ **RESOLVED 2026-05-04 via `81b9e5a` (Commit A)**. Forensic narrative: `docs/recaps/2026-05-04-pillar-13-a3-real-root-cause.md`. Verification: `docs/verification-reports/step28_phase2_postA_sync_2026-05-04.json` (19/19 green). Compounding observability/hygiene gaps the bug exposed are tracked separately as P3-M / P3-N / P3-O / P3-P / P3-Q.
- **D-extractor-failure-observability-2026-05-04** (NEW) — `app/services/memory_service.py` `extract_and_save:116-119` swallows save-time exceptions with a `type-only` warning. Without `repr(exc)` the IntegrityError that drove the Pillar 13 A3 silent failure was undetectable in logs. Tracked as P3-O.
- **D-preflight-degraded-without-celery-2026-05-04** (NEW) — 5-block pre-flight passes when Celery is down because the sync fallback path in ChatService takes over. Recommend pre-flight gate fails fast if `celery -A app.celery_app inspect ping` returns no responders. Tracked as P3-N. Lifts D-celery-worker-not-running-locally-2026-05-02 from a process drift to an enforceable pre-flight check.
- **D-luciel-instance-admin-delete-returns-500-2026-05-04** (NEW) — anomaly observed during 19/19 verification teardown: `DELETE /api/v1/admin/luciel-instances/354` returned 500. Non-fatal (Pillar 10 still passed); investigation deferred to Phase 3. Tracked as P3-Q.
- **D-dev-key-storage-hygiene-2026-05-04** (NEW) — `LUCIEL_PLATFORM_ADMIN_KEY` stored in operator Notepad rather than a credential manager. Tracked as P3-P.
- **D-pg-client-tools-not-on-operator-path-2026-05-04** (NEW) — `psql` and `pg_dump` not on PowerShell PATH; surfaced repeatedly during diag work. Tracked as P3-M.
- D-celery-worker-not-running-locally-2026-05-02 (codify in operator-patterns.md or pre-flight check) — superseded by D-preflight-degraded-without-celery-2026-05-04 / P3-N
- D-pillar-10-suite-internal-only-2026-05-01 (deploy-time teardown contract)
- D-cloudwatch-no-retention-policy-2026-05-01 (365-day retention cap)
- D-recon-task-role-reuses-migrate-role-2026-05-01 (dedicated `luciel-ecs-recon-role`)
- D-pillar-13-creates-residue-on-failure-2026-05-01

### Phase 3 (hygiene)
- D-luciel-instance-repo-cascade-not-autocommit-aware-2026-05-02
- D-admin-audit-log-resource-knowledge-duplicate-definition-2026-05-02
- D-memory-admin-endpoints-untested-by-pillar-2026-05-02
- D-recap-recon-private-subnet-assumption-2026-05-02
- D-cleanup-via-migration-not-precondition-task-2026-05-02
- D-mint-worker-db-script-doesnt-strip-sqlalchemy-dialect-2026-05-03
- D-pattern-o-helper-script-2026-05-01 (extract worked PowerShell template)
- D-no-tenant-hard-delete-endpoint-2026-05-01
- D-delete-endpoints-are-soft-delete-2026-05-01 (misleading verb)
- D-emit-log-key-order-ps-version-dependent-2026-05-01
- D-walker-loses-delete-error-body-2026-05-01
- D-emdash-corrupted-in-display-names-2026-05-01
- D-recap-table-name-assumptions-2026-05-01
- D-recap-memory-items-scope-shape-2026-05-01
- D-recap-task-def-naming-without-colon-2026-05-01
- D-recap-requirements-txt-assumption-2026-05-01
- D-recap-conflated-total-vs-active-residue-2026-05-01
- D-recap-undercount-phase1-progress-2026-05-01

### Phase 4 (cosmetic, single-sweep candidate)
- D-runbook-code-fences-stripped-by-ps-heredoc-2026-05-01
- D-jmespath-dash-quoting-2026-05-01
- D-jmespath-sizemb-mislabel-2026-05-01
- D-ecr-describe-images-filter-quirk-2026-05-01
- D-powershell-aws-cli-json-arg-quoting-2026-05-01
- D-powershell-selectstring-simplematch-anchors-2026-05-01
- D-powershell-heredoc-angle-bracket-after-quote-2026-05-01
- D-powershell-question-mark-in-string-interpolation-2026-05-01
- D-double-8a-commits-2026-05-01

### Resolved by Phase 2 code-only portion
- D-pillar-13-a3-real-root-cause-2026-05-04 → Commit A (`81b9e5a`); P13_DIAG instrumentation cleaned up by Commit D (`13035da`); evidence archived at `docs/verification-reports/step28_phase2_postA_sync_2026-05-04.json` (19/19 green). Forensic narrative: `docs/recaps/2026-05-04-pillar-13-a3-real-root-cause.md`. Note: this supersedes `D-pillar-13-a3-sentinel-not-extractable-content-2026-05-02` in scope — the May-02 fix correctly hardened the sentinel-extractable contract for the message-text shape, but the deeper auth-binding bug remained latent until the May-04 diagnosis.
- **D-runbook-mint-missing-workerhost-arg-2026-05-04** (NEW, self-referential) — the v2 §4.2 rewrite committed at `374912a` showed the `mint-with-assumed-role.ps1` invocation without its mandatory `-WorkerHost` argument. Caught at the operator side when the dry-run prompted interactively for `WorkerHost` instead of executing. Helper script signature is correct (mandatory parameter declared at lines 97-98); the helper's own example block at lines 73-76 already shows the canonical invocation form. Drift was strictly in the runbook — introduced because the agent rewrote §4.2 without first reading the helper's parameter signature. Resolved by this commit: §4.2 now passes `-WorkerHost "luciel-db.c3oyiegi01hr.ca-central-1.rds.amazonaws.com"` (canonical endpoint cross-checked against `mint_worker_db_password_ssm.py:166`, `step-28-p3-k-execute.md:228`, `step-28-commit-8-luciel-worker-sg.md:42`) on both the dry-run and real-ceremony invocations, plus a new "Required parameter" callout block explaining why we pass it explicitly. Forward-looking guard: any future runbook that wraps a PowerShell helper must read the helper's `param()` block first and reproduce all `Mandatory = $true` parameters in the example.
- D-gitignore-duplicate-stanzas-2026-05-01 → repo-hygiene commit (`86239ab`). Pulled forward from Phase 4 as a freebie alongside the broader hygiene audit. `.gitignore` rewritten: removed corrupted UTF-16 line 28 (`alembic/versions/__pycache__/s t e p 2 6 _ r e p o r t . j s o n`) and stray trailing quote on `_RESUME_MONDAY.md'`; consolidated 6 duplicate patterns across Step-27c-final and Step-27-deploy stanzas; alphabetized within stanzas; tightened section comments. File is now valid UTF-8 (previously binary-detected by git). Behavior verified equivalent: all original patterns still ignore the right files (`step26_report.json`, `overrides-foo.json`, `worker-log-dump.txt`, `RESUMEMONDAY.md`, `_RESUME_MONDAY.md`, `d11_sweep.py`, `mint-overrides.json` all confirmed ignored); no tracked files newly excluded. Audit of the broader repo found no genuine orphans — unreferenced runbooks (`step-27c-worker-deploy.md`, `step-28-prereq-cleanup.md`, `step-28-prereq-data-pattern-o.md`) and recaps (`2026-04-27-step-28-mid-phase-1-canonical.md`) are completed-historical and follow the same "don't delete audit history" protocol as the resolved drift register itself; root-level task-def JSONs (`migrate-td-rev12.json`, `ecs-service.json`, `recon-*.json`, `smoke-overrides.json`) are evidence files cited by commit messages and were preserved to keep the provenance chain intact.
- D-pillar-13-a3-sentinel-not-extractable-content-2026-05-02 → Commit 3 (`56bdab8`)
- D-audit-log-api-404 (Phase 2 §4.1 item 4 in v1) → Commit 2 (`75f6015`) + Commit 2b (`bfa2591`)
- D-retention-unbounded-delete-2026-05-03 (newly named at Phase 2 plan time, see Commit 8 message) → Commit 8 (`0d75dfe`)
- D-pillar-7-test-drift-2026-05-03 → Phase 2 HOTFIX (`2c7d0fb`)
- D-pillar-17-real-bug-2026-05-03 → Phase 2 HOTFIX (`2c7d0fb`)
- D-pillar-19-test-design-flaw-2026-05-03 → Phase 2 HOTFIX (`2c7d0fb`)
- D-mint-script-leaks-admin-dsn-via-error-body-2026-05-03 (code-level hardening only; operator-side rotation P3-H still open) → Commit 4 mint hardening (`2b5ff32`)

### Resolved by Phase 3 prerequisites (executed alongside Phase 2)
- D-luciel-admin-no-mfa-2026-05-03 → P3-J resolved 2026-05-03 23:48:11 UTC. Virtual MFA device `arn:aws:iam::729005488042:mfa/Luciel-MFA` attached to `luciel-admin`. Account-wide IAM-user sweep confirmed `luciel-admin` is the only user; no follow-on MFA work needed for current account state. Forward-looking guard recorded in `PHASE_3_COMPLIANCE_BACKLOG.md` P3-J: every future IAM user must have MFA before first console use.
- D-migrate-role-conflated-with-mint-duty-2026-05-03 → P3-K resolved 2026-05-04 00:14:10 UTC (role create) + 00:19:22 UTC (smoke-test verified). `luciel-mint-operator-role` is the dedicated mint duty principal; trust policy locked to `luciel-admin` user with `aws:MultiFactorAuthPresent=true` and `aws:MultiFactorAuthAge<3600`; permissions limited to `ssm:GetParameter` on `/luciel/database-url` + KMS decrypt via SSM. Migrate task role does NOT receive admin-DSN read. Helper `scripts/mint-with-assumed-role.ps1` (commit `9e48098`) and mint script `--admin-db-url-stdin` flag (commit `ce66d06`) complete the Option 3 ceremony chain. Smoke-test confirmed: `aws ssm get-parameter --name /luciel/production/worker_database_url` returned `ParameterNotFound` post-dry-run, i.e. mechanism proven without executing real mint.
- D-canonical-recap-misdiagnosed-migrate-role-policy-gap-2026-05-03 → P3-G resolved 2026-05-03 ~20:09 EDT. `ssm:GetParameterHistory` added to `luciel-migrate-ssm-write`; live policy now has 6 SSM actions matching `infra/iam/luciel-migrate-ssm-write-after-p3-g.json` byte-for-byte. Original misdiagnosis (claiming `GetParameter` + `PutParameter` were missing) was self-corrected in `31e2b16`; this resolution closes the actual single-action gap.
- **D-iam-changes-applied-out-of-band-with-docs-2026-05-03** (NEW, self-referential) — P3-K execution Steps 2–5 ran on the operator side ~20:09–20:19 EDT without parallel docs-side coordination. Recon pass on 22:54–22:58 EDT confirmed live state matches design byte-for-byte (zero drift) but the resolution-evidence capture was reconstructed post-hoc rather than captured live. Forward-looking guard: when the operator decides to execute multi-step IAM runbooks independently, send a one-line message first so the agent stays in sync and can integrate verbatim outputs into resolution evidence in real time. Not a security drift; a process drift, logged here so the canonical record is honest about how P3-K actually got applied.

### Resolved by Phase 1 (cumulative)
- D-pattern-s-walker-missing-memory-items-leaf-2026-05-01 → Commit 12 (`f9f6f79`)
- D-prod-orphan-memory-items-step27-syncverify-7064-2026-05-01 → Commit 11 (`62a5783`)
- D-tenant-cascade-code-level-pre-stripe-2026-05-01 → Commit 12 (promoted from Step 28x slot)
- D-msg-txt-authoring-residue-2026-05-01 → chore commit `3d64ca9`
- D-tenant-patch-no-audit-row-2026-05-02 → Commit 12
- D-verification-probes-stale-memory-items-active-col-2026-05-02 → Commit 12
- D-pillar-13-worker-audit-attribution-2026-05-02 → Commit 14 (`2e31797`)
- D-pillar-13-spoof-wait-too-short-2026-05-02 → Commit 14
- D-shell-history-key-exposure-2026-05-01 → Pattern E codification (Commit 9, `bd9446b`)
- D-d11-unblock-is-backfill-not-cleanup-2026-05-01 → Commit 11 migration design
- D-stash-c1-cascade-fix-bleed-2026-05-02 → resolved during Commit 10 stash integration
- D-resource-deletion-order-leaf-first-2026-05-01 → codified in Pattern S

---

## Section 16 — Maintenance protocol

This document is a living artifact. Update protocol:

### When to update
- **Always at phase close** (28 Phase 2/3/4, then per-step from 29 onward)
- **When a strategic-question answer changes** (rare; revise §0.1 explicitly)
- **When a deliberate exclusion changes** (rare; revise §0.5 explicitly)
- **When a new strategic question surfaces** (add to §0.1 with status "candidate")
- **When the roadmap changes** (revise §0.3 with rationale in commit message)

### When NOT to update
- Inline session summaries (chat only)
- Per-commit drift entries resolved within the same session (commit messages only)
- Speculative ideas without commitment (§5 only if rising to "design surface")

### Update mechanism
1. Edit `docs/CANONICAL_RECAP.md`
2. Bump "Last updated" header
3. Commit with `recap(<phase or step>): <one-line change description>`
4. Push

### Source of truth precedence
1. Code (running prod = what's actually true)
2. Latest commit message (most recent durable shipping description)
3. This canonical recap (strategy, roadmap, locked answers)
4. Prior recaps (historical reference only)
5. Chat session summaries (ephemeral; subordinate to all above)

If they disagree:
- Code vs commit message → audit code, fix message in next commit
- Commit message vs canonical recap → update canonical recap (it was stale)
- Canonical recap vs prior recap → canonical recap wins (prior is historical)
- Anything vs chat summary → chat summary is wrong; do not propagate

---

## End of Canonical Recap v1.5