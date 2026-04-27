# VantageMind AI / Luciel — Mid-Phase-1 Canonical Recap

**As of Monday, 2026-04-27, 3:30 PM EDT — immediately after Step 28 Phase 1 planning artifacts pushed to origin (`330d975`)**

This document is the durable re-anchor target for Luciel. When this
conversation gets long or context drifts, restart from this document.
It synthesizes the prior recaps with current execution state.

Companion documents (still authoritative for their domains):
- `docs/recaps/2026-04-27-post-step-24-5b-canonical.md` — Step 24.5b close
- `docs/recaps/2026-04-27-step-28-master-plan.md` — Step 28 strategic spine
- `docs/recaps/2026-04-27-step-28-phase-1-plan.md` — Phase 1 tactical execution
- `docs/runbooks/step-28-d1-rotation-2026-04-27.md` — D1 closure forensic record

---

## 1. Company & Founder

- **Company:** VantageMind AI
- **Product:** Luciel — a deployable AI judgment layer sold as B2B SaaS
- **Founder & sole operator:** Aryan Singh (aryans.www@gmail.com)
- **Location:** Markham, Ontario, Canada
- **Jurisdiction:** Canadian. PIPEDA non-negotiable. All infrastructure
  in AWS `ca-central-1`.
- **Repository:** https://github.com/aryanonline/Luciel — private,
  default branch `main`
- **Production endpoint:** https://api.vantagemind.ai (TLS 1.3,
  post-quantum KEX, ACM cert valid until 2026-10-29)

---

## 2. Business Model

### 2.1 Pricing ladder (locked from Step 24.5b canonical recap §2)

| Tier | Price | What it is |
|---|---|---|
| Individual | $30–80/month | One person, one tenant, limited Luciels/knowledge/messages |
| Team | $300–800/month | One domain, unlimited agents within it |
| Company | $2k+/month | Full tenant with multiple domains |

**Bottom-up upgrade path** (Step 38 in roadmap, prerequisite shipped at
Step 24.5b): when an individual's company joins, their personal
subscription pro-rates toward the company tier. This incentivizes
Sarah-the-individual-agent to champion the upgrade internally rather
than churning out as their company adopts.

### 2.2 Revenue streams beyond subscription

- Tenant onboarding fees
- Per-message / per-workflow-action billing
- Vertical domain packages (pre-built knowledge + tools per industry)

### 2.3 First vertical & first tenant

- **First vertical:** Real estate
- **First tenant target:** REMAX Crossroads, Markham GTA
- **First individual user target:** Sarah, senior listings agent
- **Vertical expansion rule:** stay in real estate until 35 paying
  tenants before expanding to legal/engineering/healthcare/etc.
- **GTA outreach status:** unblocked from product side since Step 26b
  (April 22, 2026). Engineering side gated until Step 28 Phase 1
  closes (currently in progress, D1 done, 5 commits remaining + prod
  rollout).

### 2.4 The five-layer moat (none replicable by a single-tenant chatbot)

1. **Judgment quality** — fixed Luciel Core persona that does not
   drift across deployments
2. **Three-level hierarchical data layer** — tenant → domain → agent
   → luciel-instance
3. **Cross-client feedback loops** — aggregated, never raw, isolation
   preserved
4. **Deep workflow integration** (Step 34) — real tool calls, not
   just chat
5. **Domain-agnostic architecture** — ships to new verticals as
   config/knowledge, not code

### 2.5 Outreach channels

Landing page, 2-min demo video, one-pager PDF, LinkedIn. Targets:
GTA brokerages, independent brokerages, property-management
companies, mortgage brokers.

---

## 3. Luciel's Core Identity (never changes across deployments)

### 3.1 Doctrine — fixed across every deployment

- Luciel exists to **understand before acting**
- Luciel asks **only what improves judgment**
- Luciel uses tools for **truth, not theater**
- Luciel recommends with **reasons and tradeoffs**
- Luciel **escalates before false confidence**
- Luciel remains **Luciel in every domain**

### 3.2 What is fixed vs configurable

**Fixed (cannot be overridden by tenant config):**
- Persona
- Communication method (understand → infer → verify → guide)
- Trust/safety rules
- Escalation-before-guessing principle

**Configurable per deployment:**
- Domain knowledge
- Tool access
- Escalation contacts
- Conversation policies
- Persona additions (tone, display name)

This is the **"one fixed mind, infinitely many scoped instances"**
architecture. Tenant configs ADD to the doctrine, never override.

### 3.3 Why this matters for the moat

A single-tenant chatbot can fake any persona, but cannot reliably
prevent persona drift across customers. Luciel's doctrine layer is
implemented as code in `app/persona/` and is enforced at LLM prompt
assembly time — every chat turn for every customer pulls from the
same doctrine source. A REMAX Sarah and a future legal-vertical
paralegal both get the same fixed mind, with their own scoped
configuration on top.

---

## 4. Architecture

### 4.1 Stack

- **API:** FastAPI on Python 3.14-slim base image (flagged — Python
  3.14 is experimental, kombu SQS transport showed compat issues at
  Step 27b; possible downgrade to 3.13 deferred to a later step)
- **Database:** PostgreSQL 16 with pgvector 0.8.1 extension
- **Vectors:** `pgvector.sqlalchemy.Vector(1536)` for knowledge
  embeddings
- **ORM:** SQLAlchemy 2.x with `Mapped[...]` / `mapped_column(...)`
  style
- **Migrations:** Alembic, hand-written ONLY for tables involving
  pgvector / JSONB / UDTs (Invariant 12). `alembic revision
  --autogenerate` is BANNED for those tables.
- **Compute:** AWS ECS Fargate behind ALB
- **Region:** `ca-central-1` (PIPEDA data residency)
- **TLS:** ACM-issued cert for `api.vantagemind.ai`, TLS 1.3 with
  post-quantum KEX
- **Secrets:** AWS SSM Parameter Store (SecureString), injected by
  ECS execution role
- **HTTP client:** `httpx` with pooled client
- **LLM providers:** OpenAI + Anthropic, routed via
  `app.integrations.llm.router.ModelRouter` with provider
  preference per LucielInstance
- **File parsers:** `pypdf`, `python-docx`, `reportlab`,
  `beautifulsoup4`, `markdown`
- **Async (Step 27b+):** Celery 5.6.3 + kombu 5.6.2 with sqs extra.
  Broker URL via `CELERY_BROKER_URL=sqs://` with predefined queues
  `luciel-memory-tasks` and `luciel-memory-dlq`. Redis as broker
  abandoned at Step 27c due to ElastiCache cluster-mode
  incompatibility with Celery's MULTI/EXEC pipeline.

### 4.2 Scope hierarchy — the spine of everything
tenant (REMAX Crossroads)
domain (sales, property-mgmt, support)
agent (the person-role: Sarah, Mike)
luciel-instance (scope-owned child Luciel — the actual chatbot)

- Every LucielInstance belongs to exactly one scope-level (tenant,
  domain, or agent)
- Each carries a triple: `scope_owner_tenant_id`,
  `scope_owner_domain_id`, `scope_owner_agent_id`
- Knowledge, chat keys, sessions, traces all attach to
  `luciel_instance_id`
- Reads inherit upward: instance → domain → tenant → global
- Writes enforced downward by ScopePolicy

### 4.3 Authorization — three independent layers

1. **Authentication** — valid API key (SHA-256 hashed, raw key
   visible only at creation)
2. **Permission** — `admin` or `platform_admin` flag on key's
   `permissions` list
3. **Scope** — which rows the key is allowed to touch (create-at-or-
   below, enforced)

#### Permission matrix

| Caller key scope | Can create Luciels at | Can manage |
|---|---|---|
| `platform_admin` | Any scope, any tenant | Everything |
| Tenant-scoped admin | Tenant/domain/agent under own tenant | Everything under own tenant |
| Domain-scoped admin | Domain or agent within own domain | Luciels in own domain |
| Agent-scoped admin | Agent-level under own agent_id only | Own Luciels only |
| Instance-bound chat | None (cannot create) | Chat only with bound instance |

### 4.4 Identity layer (post-Step-24.5b)

Distinct from the scope hierarchy — **who is the human acting**,
separate from **what scope they have**.

- **`users` table** — durable person identity, tenant-agnostic,
  UUID PK, case-insensitive email uniqueness via `LOWER(email)`
  expression index, `synthetic` flag for Option B onboarding stubs
- **`scope_assignments` table** — first-class durable role binding,
  end-and-recreate doctrine, `EndReason` enum (`PROMOTED`,
  `DEMOTED`, `REASSIGNED`, `DEPARTED`, `DEACTIVATED`), 3 partial
  indexes filtered on `ended_at IS NULL`, DB-level partial check
  constraint enforcing `ended_at IS NULL ⊕ ended_reason IS NULL`
- **`agents.user_id`** — nullable UUID FK to `users.id` ON DELETE
  RESTRICT (NOT NULL flip rolled back per drift D20; deferred
  beyond Step 28 until public routes require user_id explicitly)
- **`memory_items.actor_user_id`** — UUID FK distinct from
  `session.user_id` free-form string (resolves drift D7 semantic
  name collision). Currently nullable (D11 in Step 28 Phase 1
  flips to NOT NULL after orphan sweep).
- **`request.state.actor_user_id`** — middleware injection,
  distinct from `session.user_id`

This identity layer closes Strategic Question 6 (role changes /
promotions / demotions / departures) and unlocks Step 38 bottom-up
tenant merge as a future implementation (4 indexed UPDATEs across
agents, scope_assignments, memory_items, sessions).

### 4.5 Async memory worker (post-Step-27c)

- Separate ECS service `luciel-worker-service` running Celery
  5.6.3 with SQS broker
- Two queues: `luciel-memory-tasks` (4d retention, 30s visibility,
  3-receive redrive), `luciel-memory-dlq` (14d retention)
- **Defense-in-depth gates at task entry (Invariant 8):**
  1. Payload shape validation
  2. API key still active
  3. `session.tenant_id == payload.tenant_id` (cross-tenant guard)
  4. LucielInstance.active when provided
  5. User.active (Step 24.5b addition)
  6. Cross-tenant identity-spoof guard (Step 24.5b addition,
     Pillar 13)
- Audit row in same txn as memory upsert (Invariant 4)
- Audit content is SHA256-only — no raw payload (PIPEDA)
- Result backend disabled (`task_ignore_result=True`)
- Memory writes idempotent via `messages.id` partial unique index

### 4.6 App subpackage layout (18 subpackages, post-Step-27b)
app/
├── api/ (HTTP routes)
├── core/ (config, settings, lifecycle)
├── db/ (engine, sessions)
├── domain/ (domain-config services)
├── integrations/ (LLM router, external APIs)
├── knowledge/ (chunking, embedding, ingestion)
├── memory/ (extractor, classifier)
├── middleware/ (auth, scope policy)
├── models/ (SQLAlchemy ORM)
├── persona/ (Luciel core doctrine)
├── policy/ (retention, scope policy)
├── repositories/ (data access — including admin_audit_repository
│ which exports both AdminAuditRepository AND
│ AuditContext from same module)
├── runtime/ (request lifecycle)
├── schemas/ (Pydantic request/response)
├── services/ (business logic — admin, api_key, chat, knowledge,
│ luciel_instance, memory, onboarding, scope_assignment,
│ user)
├── tools/ (tool calls — Step 34+ territory)
├── verification/ (the 14-pillar python -m app.verification suite)
└── worker/ (Celery app + tasks — Step 27b+)

### 4.7 Database tables (17 total post-24.5b)

`tenants`, `tenant_configs`, `domains`, `domain_configs`, `agents`,
`luciel_instances`, `users`, `scope_assignments`, `api_keys`,
`sessions`, `messages`, `memory_items`, `knowledge_embeddings`,
`retention_policies`, `retention_categories`, `user_consents`,
`deletion_logs`, `admin_audit_logs`.

(That's 18 — the canonical recap §2 said 17; one is double-counted.
Will reconcile during Phase 1 close commit.)

### 4.8 Verification suite — 14 pillars

Single command produces the go/no-go artifact: `python -m
app.verification`. Pillars:

1. Onboarding (Option B)
2. Scope hierarchy + agent admin key
3. Multi-format ingestion + versioning
4. Chat-key binding + blast radius
5. Chat resolution (scope-correct LLM round-trip)
6. Retention round-trip (category isolation)
7. Cascade deactivation (all four levels)
8. Scope-policy negatives
9. Migration integrity (bidirectional + per-column)
10. Teardown integrity (zero residue for this tenant)
11. Async memory extraction (Step 27b+, MODE=full vs MODE=degraded)
12. Identity stability under role change (Q6, Step 24.5b)
13. Cross-tenant identity-spoof guard (Q6, Step 24.5b)
14. Departure semantics (Q6 bounded cascade, Step 24.5b)

Pillar 15 (consent route regression guard) ships in Step 28 Phase 3.

---

## 5. What Has Been Accomplished

This section traces the production-credibility arc from "first working
local prototype" (Step 23) through "Step 28 Phase 1 planning durable
on origin" (today). Each step has a tag, a closing commit, and a one-
paragraph what-shipped summary.

### 5.1 Tagged releases on origin (chronological)

| Tag | Commit | Date | What shipped |
|---|---|---|---|
| `step-26b-20260422` | `00d7e79` | 2026-04-22 | First production release. Verification suite expanded to 10 pillars. ECS+RDS rollout pattern established. |
| `step-27a-20260422` | `16daf61` | 2026-04-22 | 5 hardening fixes: retention tenant-scope leak, admin_audit_log nullability, API key permission validator, SSM-direct admin key mint, dev key rotation. |
| `step-27-20260425` | `1c3b058` | 2026-04-25 | Combined 27a + 27b infrastructure release. Async-memory queues, IAM, log group, migration `8e2a1f5b9c4d` provisioned. Activation deferred (kombu/SQS broker config issue). |
| `step-27c-deployed-20260425` | `1dc291d` | 2026-04-25 | Async memory activation. Redis-as-broker abandoned (cluster-mode incompatibility); switched to SQS-direct. Worker boots clean. |
| `step-27-completed-20260426` | `6f03e03` | 2026-04-26 | Step 27 close. 11/11 prod gate green. Async memory extraction live in MODE=full on prod. |
| `step-24.5b-20260503` | `adc5ba0` | 2026-04-27 | Durable User identity layer. Q6 RESOLVED, Q5 prerequisite SHIPPED. Pillars 12/13/14 added. 14/14 prod gate green. |

(Note: tag dates are commit-tag dates, which don't always match
chronological work order — Step 24.5b was tagged with 2026-05-03 even
though work happened 2026-04-27, because the tag was scheduled at
plan time. Convention going forward: tag dates match shipping date,
not planned date.)

### 5.2 Step-by-step summary

**Steps 1–22: Luciel Core MVP.** Local-only development from concept
through working chat with scope hierarchy, knowledge ingestion,
retention, and audit logging. Pre-production. Architecture decisions
locked: AgentLucielInstance split, vector-first retrieval, councils
deferred, bottom-up expansion deferred.

**Step 23: Onboarding.** Tenant/Domain/Retention/AdminKey creation
flow shipped as Option A and Option B variants. Option B (synthetic
user stub) became the canonical path for Step 24.5+ identity layer.

**Step 24: Scope hierarchy enforcement.** ScopePolicy implemented as
middleware. Permission matrix locked. `request.state` snake_case bug
fixed (had silently weakened scope enforcement).

**Step 24.5: AgentLucielInstance split.** Locked the doctrine that
"Agents are person-roles, LucielInstances are scope-owned children."
Knowledge bound to LucielInstance, not Agent. Old `AgentConfig`
retained read-only as legacy with a Step 28+ removal window.

**Step 25b: Knowledge layer migration.** Multi-format ingestion (txt,
md, html, pdf, docx, csv, json), chunking inheritance through scope
hierarchy, versioning, vector embeddings via pgvector 0.8.1.

**Step 26: 10-pillar verification suite.** Single command produces
go/no-go artifact. Bidirectional migration integrity, teardown
integrity, scope-policy negatives all asserted. Foundation for every
prod rollout pattern from this point forward.

**Step 26b: First prod release.** ECS Fargate behind ALB,
`api.vantagemind.ai` live with TLS 1.3, Alembic head `3447ac8b45b4`
on prod RDS. Verification gate passed end-to-end on prod. Tag
`step-26b-20260422` on `00d7e79`.

**Step 27a: Hardening backlog (P0).** Five fixes in one release:
retention scope-leak (PIPEDA-critical), admin_audit_log.tenant_id
nullable (Invariant 5), Pydantic permissions enum validator,
SSM-direct admin key mint pattern, dev platform-admin key rotation
to tenant_id=NULL.

**Step 27b: Async memory infrastructure.** New `app/worker`
subpackage. SQS queues + IAM + log group + migration
`8e2a1f5b9c4d` (memory_items.message_id + lucid_instance_id +
composite partial unique index). Pillar 11 added in MODE=degraded.
Code shipped, async path disabled in prod via feature flag while
broker config issue investigated.

**Step 27c-final: Async activation.** Discovered ElastiCache cluster-
mode incompatibility with Celery's Redis-broker MULTI/EXEC pipeline
(crash-loop with `ClusterCrossSlotError`). Switched to
SQS-as-broker, `pyproject.toml` updated to `kombu[sqs]` extra. New
IAM role `luciel-ecs-worker-role` scoped to SQS+Bedrock+OpenAI/
Anthropic+SSM, no ALB ingress. Worker reaches READY, async path
flipped on, Pillar 11 in MODE=full on prod.

**Step 24.5b: Durable User identity layer (Q6 resolution).** 4-commit
arc: schema (`users` + `scope_assignments` tables, `agents.user_id`
FK additive nullable), services (UserService, ScopeAssignmentService,
ApiKeyService.rotate_keys_for_agent for Q6 mandatory rotation),
verification (Pillars 12/13/14, backfill script idempotent +
audit-emitting), close (runbook + canonical recap). Migration head
advanced from `3447ac8b45b4` → `8e2a1f5b9c4d` →
`3ad39f9e6b55` → `4e989b9392c0`. 7,179 cumulative insertions across
34 files. PR #3 squash-merged to main as `adc5ba0`. Tag
`step-24.5b-20260503`. 20 drift items surfaced (10 RESOLVED,
8 deferred to Step 28, 2 cosmetic).

### 5.3 Step 28 progress (in flight, today)

**Phase 1 of 4 — Security & Compliance Hardening, currently mid-phase.**

- **Commit 1 (`81c0088`, 2026-04-27 14:04 EDT):** D1 closure —
  rotated leaked local platform-admin key id=158. Audit row 1997.
  Replacement key id=539. 30-hour security gap closed. GTA outreach
  unblocked at security layer.

- **Commit 2 (`330d975`, 2026-04-27 15:29 EDT):** Step 28 master
  plan + Phase 1 tactical plan + drift register seed. 2 files, 895
  insertions. 4-phase strategic spine, 6-commit Phase 1 execution
  plan with per-commit acceptance/rollback/risk/known-unknowns,
  drift register with 8 verbatim D-rows from 24.5b canonical recap.

- **(this commit, in flight):** Mid-Phase-1 canonical recap. Single
  re-anchor doc for future sessions when context drifts.

- **Remaining Phase 1 work:** 5 commits (consent route fix D16,
  D5 deactivate_key audit retrofit, D11 sweep + NOT NULL flip,
  worker DB role, worker SG) + Phase 1 close commit, then PR #4
  merge, prod rollout via runbook, tag `step-28-phase-1-YYYYMMDD`.

### 5.4 Cumulative repo state at this checkpoint

- **5 tags on origin** (steps 26b, 27a, 27, 27c-deployed,
  27-completed, 24.5b)
- **`main`** at `adc5ba0` (Step 24.5b close), unchanged since
  2026-04-27 morning
- **`step-28-hardening`** at `330d975` (or successor SHA after
  this canonical recap commit)
- **Local 14/14 verification** green with platform-admin dev
  key id=539 (`luc_sk_lsxv7`)
- **Prod state:** `luciel-backend:N` and `luciel-worker:N` (latest
  task-defs from Step 27c-final close), Alembic head
  `4e989b9392c0`, no rollouts since Step 24.5b
- **Drift register:** 25 items tracked, 2 RESOLVED, 23 OPEN
  across Phases 1–4
- **GTA outreach:** gated until Phase 1 prod ships

---

## 6. Step 28 Plan (in flight)

Step 28 is the hardening sprint that converts Luciel from "shipped
and working" to "defensibly shipped and working." 4 phases, 13-15
commits estimated, 2-3 weeks total. Prod rollout per phase. GTA
outreach kicks off after Phase 1 prod ships.

### 6.1 Four phases summary

| Phase | Name | Items | Commits | Wall-clock | Business value at close |
|---|---|---|---|---|---|
| 1 | Security & Compliance Hardening | D1 (done), D5, D11, D16, luciel_worker DB role, worker SG | 5 remaining | 6-8 hrs across 2-3 sessions | Defensible PIPEDA posture; outreach unblocked |
| 2 | Observability & Reliability | CloudWatch alarms, ECS auto-scaling, container healthchecks, batched retention deletes | 4 | 4-6 hrs | Detect-before-customer-notices baseline |
| 3 | Operational Hygiene | Test-residue tenant sweep, runbook JSON gitignore, Pillar 15 (consent regression), AgentConfig removal, SSM param naming standardization, Step 26 archive bug, DELETE /admin/tenants endpoint | 4 | 3-5 hrs | Demo-clean repo + prod state |
| 4 | Cosmetic & Deferred Closure | D3, D4, any new cosmetic discoveries, retrospective | 2 | 2 hrs | Drift register zero open P1+ items |

Total: 13-15 commits, 15-21 hours wall-clock, 2-3 weeks calendar.

### 6.2 Phase 1 commit-by-commit (currently in flight)

| # | Commit | Closes | Wall-clock | Risk | Status |
|---|---|---|---|---|---|
| 1 | `fix(28): D1 closure - rotate leaked local platform-admin key` | D1 | 30 min | low | DONE `81c0088` |
| 2 | `docs(28): master plan + Phase 1 tactical plan + drift register seed` | (planning) | 2 hrs | low | DONE `330d975` |
| 3 | `docs(28): mid-phase-1 canonical recap` | (memory anchor) | 30 min | low | IN PROGRESS |
| 4 | `fix(28): consent route double-prefix bug` | D16 | 30 min | lowest (1-line change) | OPEN |
| 5 | `feat(28): retrofit ApiKeyService.deactivate_key with audit_ctx` | D5 | 90 min | medium (Pillar 7 cascade) | OPEN |
| 6 | `chore(28): memory_items.actor_user_id orphan sweep + NOT NULL flip` | D11 | 90 min | medium (split rule if >10 orphans) | OPEN |
| 7 | `feat(28): separate luciel_worker Postgres role` | DB role standing item | 2 hrs | high (DB grants + AWS secrets) | OPEN |
| 8 | `feat(28): dedicated luciel-worker-sg security group` | SG standing item | 1 hr | medium (add-before-remove) | OPEN |
| 9 | `docs(28): Phase 1 close - drift register update + prod runbook` | (close) | 60 min | low | OPEN |

### 6.3 Drift register state at this checkpoint

**RESOLVED in arc:**
- D1 (`81c0088`) — leaked local platform-admin key rotated
- D10 (24.5b convention) — Alembic full-file template retired

**OPEN, Phase 1 (5 items):**
- D5 — `ApiKeyService.deactivate_key` lacks audit_ctx
- D11 — `memory_items.actor_user_id` 10 historical orphans
- D16 — consent route double-prefix
- "luciel_worker DB role" standing item
- "luciel-worker-sg" standing item

**OPEN, Phase 2 (5 items):**
- CloudWatch alarms (queue/DLQ/RDS/ECS/ALB) + SNS email
- ECS auto-scaling target-tracking
- Web task-def container healthcheck
- Worker task-def container healthcheck
- Batched retention deletes

**OPEN, Phase 3 (10 items):**
- D2 — admin_audit_log.py duplicate `ACTION_KNOWLEDGE_*`
- D14 — synthetic emails 422 on PATCH through public API
- D18 — local LLM extractor produces 0 memory rows for some shapes
- Test-residue tenant sweep (`step27-prodgate-*`,
  `step27-syncverify-7064`)
- Runbook-artifact JSON gitignore cleanup
- Pillar 15 — consent route regression guard
- AgentConfig legacy model removal (Step 24.5 audit window)
- SSM param naming standardization (`/luciel/production/<NAME>`)
- Step 26 JSON archive write-path bug
- `DELETE /admin/tenants/<id>` endpoint

**OPEN, Phase 4 (2 items):**
- D3 (cosmetic per 24.5b drift table)
- D4 (cosmetic per 24.5b drift table)

**RESOLVED meta-discoveries (3 items, all from D1 closure):**
- AuditContext lives in `app.repositories.admin_audit_repository`,
  not `app.middleware.audit_context` (recap was descriptive, not
  verbatim)
- `AdminAuditRepository.record()` uses `autocommit=False` (no
  underscore)
- PowerShell `python -c "...f-string..."` collides with PS parser
  on `in` keyword inside escaped quotes — use here-string-to-
  tempfile pattern (`@'...'@ | Set-Content`)

**Tally:** 25 tracked items, 2 RESOLVED (8%), 22 OPEN, 3 RESOLVED meta.

### 6.4 Sequencing rules (16 rules in 5 categories)

**Branch & PR discipline:**
1. One branch for the whole step (`step-28-hardening`)
2. One PR per phase, not per commit
3. Each phase ends with a tag (`step-28-phase-N-YYYYMMDD`)
4. Step 28 closes with `step-28-YYYYMMDD` on merge commit of Phase 4 PR

**Commit discipline:**
5. Every commit closes one drift item or one logical change
6. Commit message format: `type(28): <summary> (closes <D-id> | standing-item-N)`
7. Last commit of each phase updates drift register with closing SHAs
8. No code change without a drift register entry

**Verification discipline:**
9. Local N/N green before any commit
10. Each phase ends with a prod rollout
11. Each phase has a rollback contract documented in the runbook

**Discovery discipline:**
12. Triage on detection, not later
13. P0 discoveries halt the current phase
14. Phase scope is fixed at phase start

**Re-planning discipline:**
15. Master plan is a living document; drift register updates per
    commit; Sections 1-2 + 5-7 update only at phase-close
16. Re-planning is a deliberate act with its own commit shape
    (`docs(28): re-plan after <discovery>`)

### 6.5 Step 28 close gate

Step 28 is done when ALL of:
1. All P0 items RESOLVED (currently: D1 done)
2. All P1 items RESOLVED or explicitly DEFERRED-with-reason
3. All four phases tagged on origin
4. Local verification green at current pillar count (15/15 by
   Step 28 close, since Pillar 15 lands in Phase 3)
5. Prod verification green in MODE=full at same pillar count
6. Drift register has zero open P1+ items
7. Master plan retrospective committed
8. Tag `step-28-YYYYMMDD` on Phase 4 PR merge commit

### 6.6 Re-planning trigger conditions

**Triggers that REQUIRE re-planning:**
- P0 discovery during a phase
- Discovery that adds >1 commit to a phase's scope
- Discovery that crosses phases
- A phase's wall-clock exceeds 2x the original
- A drift item turns out to need its own step

**Triggers that do NOT require re-planning:**
- Memory corrections (e.g. import path drift)
- Cosmetic discoveries (P3)
- Estimate misses within 2x

### 6.7 Phase 1 known unknowns (require pre-commit grep)

- **U1** — `rotate_keys_for_agent` audit-emission pattern (blocks
  Commit 5)
- **U2** — `scripts.backfill_user_id` `--dry-run` support on
  `--phase b` (blocks Commit 6)
- **U3** — Worker code cross-table joins into retention/knowledge
  (blocks Commit 7)
- **U4** — VPC endpoints for SQS/SSM vs internet-routed egress
  (blocks Commit 8)
- **U5** — Step 27c worker IAM grants (blocks Commits 7-8)

---

## 7. Future Roadmap (post-Step-28)

The full canonical step list runs from Step 1 to Step 50+, with each
step gated by a specific business or technical milestone. Below is
the post-Step-28 forward view, condensed.

### 7.1 Steps 29 through 50 — annotated

| Step | Name | Purpose | Business outcome |
|---|---|---|---|
| 29 | Pytest test suite | Wraps `app.verification` as integration backbone for CI; adds unit-test layer below | Engineering velocity; safe refactor surface |
| 30 | Stripe billing integration | Subscription billing wired to tenant tier (Individual/Team/Company) | First revenue capture; closes the "how do they pay you" question |
| 31 | Hierarchical dashboards | Per-tenant + per-domain + per-agent activity views; aggregated metrics with isolation preserved | Customer self-service visibility; expansion-conversation surface |
| 32 | Agent self-service | Sarah-the-agent can spin up writer/tester/debugger LucielInstances under her own scope without admin help | Bottom-up adoption mechanic; reduces sales friction |
| 33 | Multi-LLM routing per tenant | Tenant-level provider preference (OpenAI vs Anthropic vs future); cost vs quality optimization | Enterprise-tier differentiator; cost control |
| 34 | Real workflow actions / tool calls | LucielInstance can call domain-specific APIs (CRM, MLS, calendar, email) — not just chat | Moat layer 4 lit up; "Luciel does the work, not just talks about it" |
| 35 | Hybrid retrieval (vector + graph) | Add knowledge graph alongside pgvector for relational queries (e.g. "who at REMAX has handled luxury condos in Yorkville") | Quality differentiator vs vector-only competitors |
| 36 | Councils — multi-Luciel reasoning within scope | Multiple LucielInstances within a scope can reason together (e.g. listing-Luciel + market-analysis-Luciel + compliance-Luciel) | Decision-quality story; specialty-Luciel model |
| 37 | Multi-vertical rollout | Ship legal / engineering / healthcare / accounting verticals as config + knowledge packages | Revenue scale; vertical TAM expansion |
| 38 | Bottom-up tenant merge | The "Sarah champions her brokerage's adoption" mechanic implemented as 4 indexed UPDATEs | Closes Q5 strategic question; drives expansion revenue |
| 39 | Cross-tenant learning (anonymized aggregates) | Industry-wide patterns surfaced without any single tenant's data leaking | Moat layer 3 lit up; "Luciel gets smarter for everyone" story |
| 40+ | Beyond Step 39 | Compliance certifications (SOC 2, HIPAA for healthcare vertical), enterprise SSO, audit log exports, tenant data residency variants | Enterprise sales motion |

### 7.2 The 6 strategic questions

These were the architectural questions locked early in development.
Each has a status as of today.

| # | Question | Status | Notes |
|---|---|---|---|
| Q1 | Agent vs LucielInstance split — should Agents own knowledge directly, or should LucielInstance be the scope owner? | RESOLVED | LucielInstance owns. Locked at Step 24.5. |
| Q2 | Vector-first vs graph-first knowledge retrieval | RESOLVED | Vector-first (pgvector). Graph deferred to Step 35 hybrid. |
| Q3 | Councils now or later? | DEFERRED | Step 36. Single-LucielInstance is the MVP unit; councils are a Step-36 expansion. |
| Q4 | Bottom-up adoption mechanic now or later? | DEFERRED | Step 38. Prerequisite (durable User identity) shipped at Step 24.5b. |
| Q5 | Email-stable User identity for cross-scope identity continuity | RESOLVED prerequisite at 24.5b | Endpoint itself ships at Step 38 |
| Q6 | Role changes / promotions / demotions / departures — whose memory does background work write to? | RESOLVED at Step 24.5b | End-and-recreate ScopeAssignments; mandatory key rotation on role change; cross-tenant identity-spoof guard |

Q3 and Q4 are explicit deferrals, not gaps. The architecture
supports them when the steps come; they're not forced now because
the MVP business case (single-tenant adoption with vertical
expansion) doesn't require them.

### 7.3 Deferred concepts (held in mind, not in code)

These are ideas and patterns explicitly held for later steps. Not in
the current drift register because they're not bugs — they're
unscheduled enhancements.

- **Local LLM fallback** — for tenants requiring data-never-leaves-
  premise. Locks Anthropic/OpenAI behind a feature flag, swaps in a
  self-hosted model. Probable Step 40+ for healthcare vertical.
- **Multi-region DR** — currently `ca-central-1` only. Cross-region
  replica + failover would be a separate pre-enterprise step,
  probably Step 45+.
- **Tenant data export** — PIPEDA right-of-portability hasn't been
  exercised yet. Currently we have deletion logs and audit trails
  but no structured export. Probable Step 33-35 alongside billing.
- **API key per-route scoping** — currently a key has full
  `chat`/`sessions`/`admin`/`platform_admin` permissions; finer-
  grained per-endpoint allowlisting is a Step 40+ enterprise feature.
- **Webhook integrations** — for tenants who want Luciel to push to
  their systems rather than be queried. Step 34+ adjacent.
- **In-app notifications + escalation alerts** — Luciel currently
  escalates via persona ("I should escalate this to a human") but
  doesn't actually notify a human. Step 33+ alongside dashboards.
- **Slack / Teams / WhatsApp integrations** — chat surface beyond
  HTTP API. Step 34+ as part of workflow actions.
- **Per-tenant LLM fine-tuning** — currently all tenants share the
  same provider models. Custom-tuned models per tenant or per
  vertical is a Step 40+ enterprise differentiator.

### 7.4 Operational best practices not yet enforced in code

These are doctrines we've committed to verbally but haven't yet
turned into code-level enforcement. Each is a Step 28+ candidate
when prioritized.

- **Two admin keys per tenant, held by different humans** — currently
  policy, not code. Step 33+ candidate.
- **Domain-scoped admins delegate agent-level LucielInstance creation
  to the agent themselves** — Sarah spins up her own
  writer/tester/debugger Luciels under her agent scope. This is the
  Step 32 pattern.
- **Mandatory 2FA for platform_admin keys** — currently all auth is
  bearer-token only. Step 40+ for enterprise tier.
- **Tenant-onboarding rate limiting** — no current ceiling on tenant
  creation rate per platform_admin key. Step 33+ as part of billing.

### 7.5 The roadmap in one paragraph (elevator version)

After Step 28 closes with defensible operations, Step 29 brings
pytest as the testing backbone, Step 30 wires Stripe billing,
Step 31 ships dashboards, Step 32 enables agent self-service, Step
33 adds multi-LLM routing, Step 34 turns Luciel from a chatbot into
a workflow tool with real tool calls, Step 35 adds hybrid vector +
graph retrieval, Step 36 enables specialty-Luciel councils, Step 37
expands to multiple verticals, Step 38 implements bottom-up tenant
merge, Step 39 unlocks anonymized cross-tenant learning, and Step
40+ delivers enterprise features (compliance certs, SSO, audit
exports, regional variants). Estimated 18-24 months from today to
Step 40 territory at solo-operator pace, accelerated if revenue
funds hires.

---

## 8. Non-negotiables, Invariants, Durable Rules, Key Facts

This section is the operational spine — what doesn't change, what
gets enforced, what to remember, how to re-ground a session.

### 8.1 The 13 non-negotiable invariants

Locked across all steps. Violating any one halts work until resolved.

1. **One source of truth per concept.** Persona = `app/persona/`,
   not duplicated in tenant configs. Scope = ScopePolicy, not
   per-route checks.
2. **No raw secrets in stdout, logs, chat, or git.** SSM-direct
   mint pattern is the only sanctioned path for new keys.
3. **Defense in depth.** Auth → permission → scope → service-layer
   gate → DB constraint. Every layer assumes the one above could fail.
4. **Audit row in same transaction as state change.** `record(...)`
   with `autocommit=False` is the canonical pattern.
5. **`tenant_id` nullable for platform-scoped resources.**
   `platform_admin` keys have `tenant_id=NULL`;
   `admin_audit_logs.tenant_id='platform'` for system actors.
6. **Hand-written migrations for pgvector/JSONB/UDT tables.**
   `alembic revision --autogenerate` is BANNED for those.
7. **Verify migrations against fresh DB before commit.**
8. **Worker tasks pre-flight gate.** Defense-in-depth at task entry,
   not just route entry. (Invariant 8 = the 6-gate worker contract.)
9. **No cross-tenant reads or writes.** Every query filtering by
   tenant; cross-tenant data access is a P0 incident.
10. **Local 14/14 (or current N/N) green before any commit.**
11. **Identity immutability.** `tenant_id`, `domain_id`, `agent_id`,
    `luciel_instance_id` never mutate after creation. Deactivate +
    recreate for role changes.
12. **Hand-written, fresh-DB-verified migrations.**
13. **Mandatory tenant predicates.** Every query touching tenant-
    scoped data filters by `tenant_id`. Composite uniques include
    `tenant_id`.

### 8.2 The 5 prohibited actions

1. Reading or writing credential stores
2. Pushing to `main` directly (always via PR)
3. `git push --force` to any branch with origin tracking
4. Deleting drift register entries (mark RESOLVED/DEFERRED instead)
5. Running migrations on prod without first running on local + fresh DB

### 8.3 Actions requiring explicit user confirmation

DB writes against prod, ECS task-def registration, ECS service
updates, IAM policy changes, SSM parameter writes, tag creation on
origin, PR merge, file downloads.

### 8.4 Key code/operational facts

**Tag → SHA mapping:**
- `step-26b-20260422` → `00d7e79`
- `step-27a-20260422` → `16daf61`
- `step-27-20260425` → `1c3b058`
- `step-27c-deployed-20260425` → `1dc291d`
- `step-27-completed-20260426` → `6f03e03`
- `step-24.5b-20260503` → `adc5ba0`

**Local platform-admin dev key (post-D1 rotation):**
- id=539, prefix `luc_sk_lsxv7`, `tenant_id=NULL`
- Permissions: chat, sessions, admin, platform_admin
- Raw key in password manager only
- Per session: `$env:LUCIEL_PLATFORM_ADMIN_KEY = "<raw>"`

**Prod platform-admin key:** id=3, prefix `luc_sk_kHqA2`, password
manager only, zero exposure events.

**Other dev keys (untouched, all `tenant_id=NULL`):**
- id=8 `luc_sk_rWQ0a`, id=15 `luc_sk_GsDhB`, id=16 `luc_sk_VzJmX`

**Decommissioned (D1 closure):** id=158 `luc_sk_HY_RK`,
`active=False` since 2026-04-27 17:57:15 UTC, audit row 1997.

**AWS constants:**
- Account 729005488042, region ca-central-1
- Cluster `luciel-cluster`
- Services: `luciel-backend-service`, `luciel-worker-service`
- SQS: `luciel-memory-tasks`, `luciel-memory-dlq`
- Web SG: `sg-0f2e317f987925601` (Phase 1 Commit 8 introduces
  `luciel-worker-sg`)
- Subnets: `subnet-0e54df62d1a4463bc`, `subnet-0e95d953fd553cbd1`
- Alembic head: `4e989b9392c0`

**SSM paths:**
- `/luciel/production/platform-admin-key`
- `/luciel/production/database-url`
- `/luciel/production/openai-api-key`
- `/luciel/production/anthropic-api-key`
- `/luciel/production/celery-broker-url` (=`sqs://`)
- (Phase 1 Commit 7 adds): `/luciel/production/worker-database-url`

### 8.5 PowerShell + Python operational patterns

**A. Multi-line Python from PowerShell:** here-string-to-tempfile,
never `python -c "..."`.

**B. Chunked file build:** `Set-Content` creates, `Add-Content`
appends. Max 3 KB / 80 lines per here-string to avoid chat
truncation.

**C. Clean-prompt verification:** Before pasting any new here-string,
verify prompt is `PS C:\...>` not `>>`. If `>>`, press `Ctrl+C`.

**D. Case-insensitive false positives:** `Select-String` matches
case-insensitively by default. Visually inspect counts.

**E. Credential safety:** Raw keys never in chat. Move via
`Set-Clipboard` then `Set-Clipboard -Value " "`.

### 8.6 Re-grounding procedure

1. Read Section 5 (What Has Been Accomplished)
2. Read Section 6 (Step 28 Plan in flight)
3. `git checkout step-28-hardening; git pull origin step-28-hardening; git log -3 --oneline`
4. `$env:LUCIEL_PLATFORM_ADMIN_KEY = "<from password manager>"; python -m app.verification` — expect 14/14 (or 15/15 post-Phase-3)
5. Open `docs/recaps/2026-04-27-step-28-phase-1-plan.md` for next-up commit
6. Check master plan drift register for status changes
7. Begin work on next OPEN commit

### 8.7 Eight core facts for cold-session re-grounding

1. VantageMind AI / Luciel — domain-adaptive AI judgment layer, B2B
   SaaS, founder Aryan Singh, Markham Ontario, ca-central-1.
2. First tenant target: REMAX Crossroads. First user target: Sarah,
   listings agent. First vertical: real estate.
3. Architecture: tenant → domain → agent → luciel-instance scope
   hierarchy + durable User identity layer (post-24.5b).
4. Production live at https://api.vantagemind.ai. Latest tag
   `step-24.5b-20260503` on main. 14/14 verification green.
5. Step 28 in flight on `step-28-hardening`. Phase 1 of 4. 2 commits
   done (D1 + plans), 6 remaining + prod rollout.
6. GTA outreach gated until Phase 1 prod ships.
7. 13 non-negotiable invariants. PIPEDA defensible at every layer
   is the operating posture.
8. Resume by reading this document plus Phase 1 plan.

---

**End of mid-Phase-1 canonical recap.**

Living document. Next canonical recap is written at Phase 1 close,
superseding this. Authoritative drift register lives in master plan
Section 3.
