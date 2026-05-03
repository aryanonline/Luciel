# Luciel Canonical Recap

**Version:** v1
**Last updated:** 2026-05-03, on Phase 1 close (commit `bd9446b`, tag `step-28-phase-1-complete`)
**Supersedes:** all prior recap versions including v3 reframe of 2026-05-01 11 PM
**Next update:** at Phase 2 close OR when a strategic-question answer changes
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
| Hardening | **28** | Operational maturity sprint — security/compliance, observability, hygiene, cosmetic — **4 phases**, 13–15 commits | **Phase 1 complete** at `bd9446b` (tag `step-28-phase-1-complete`). Phases 2/3/4 pending |
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
- **Database:** RDS Postgres, master role `luciel_admin`. New role `luciel_worker` exists with least-privilege grants (Step 28 Phase 2 will swap worker to use it).
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

## Section 3 — What's accomplished (Step 28 Phase 1 complete)

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

### 3.2 Prod state at Phase 1 close
- Branch: `step-28-hardening`, HEAD `bd9446b`, tag `step-28-phase-1-complete`
- Backend service: `luciel-backend:17` (digest `sha256:39fecc49...95193`)
- Alembic head: `f392a842f885`
- memory_items.actor_user_id NOT NULL enforced
- Cascade-in-code verified end-to-end via prod smoke test on `step28-smoke-cascade-372779`
- Pillar 18 (tenant cascade end-to-end) green on dev
- Dev verification: 17/18 pillars green, Pillar 13 only red (A3 — sentinel-not-extractable, deferred to Phase 2)

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

### 4.1 Step 28 Phase 2 — Operational hardening
1. **Worker DB role swap** — mint `luciel_worker` password via `scripts/mint_worker_db_password_ssm.py` (run via ECS one-shot or temporary SG ingress); write `worker_database_url` SSM; update worker task-def; deploy
2. **Rotate `luciel_admin` password** — drift `D-prod-superuser-password-leaked-to-terminal-2026-05-03`
3. **Pillar 13 A3 fix** — rewrite test setup turn with extractable user-fact wrapping the sentinel; gets to 18/18 green
4. **Audit-log API mount** — `/api/v1/admin/audit-log` returns 404 currently
5. **Worker scope-assignment cascades** — Q6 doctrine extension for edge cases

Estimated: 3–5 commits, ~4–6 hours total. Runs in parallel with Steps 29/30.

### 4.2 Step 28 Phase 3 — Hygiene hardening (opportunistic)
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
6. **Pillar 13 A3 deferred to Phase 2** — test-design issue (sentinel-not-extractable), not a security gap.
7. **Worker DB role swap deferred to Phase 2** — canonical placement per recap §9.4 / §11.3.
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

Block 5 — Verification (expect 17/18 with Pillar 13 only red):
`python -m app.verification`

If Block 5 returns anything other than 17/18 with Pillar 13 only red, **diagnosis is the only acceptable next action.** Do not proceed to prod work on a red dev.

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
- Worker DB role swap (former Commit 13 work)
- D-prod-superuser-password-leaked-to-terminal-2026-05-03 (rotate `luciel_admin` as part of worker role swap)
- D-pillar-13-a3-sentinel-not-extractable-content-2026-05-02 (rewrite test setup turn with extractable user-fact)
- D-celery-worker-not-running-locally-2026-05-02 (codify in operator-patterns.md or pre-flight check)
- D-pillar-10-suite-internal-only-2026-05-01 (deploy-time teardown contract)
- D-cloudwatch-no-retention-policy-2026-05-01 (365-day retention cap)
- D-recon-task-role-reuses-migrate-role-2026-05-01 (dedicated `luciel-ecs-recon-role`)
- Audit-log API mount (`/api/v1/admin/audit-log` returns 404)
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
- D-gitignore-duplicate-stanzas-2026-05-01
- D-runbook-code-fences-stripped-by-ps-heredoc-2026-05-01
- D-jmespath-dash-quoting-2026-05-01
- D-jmespath-sizemb-mislabel-2026-05-01
- D-ecr-describe-images-filter-quirk-2026-05-01
- D-powershell-aws-cli-json-arg-quoting-2026-05-01
- D-powershell-selectstring-simplematch-anchors-2026-05-01
- D-powershell-heredoc-angle-bracket-after-quote-2026-05-01
- D-powershell-question-mark-in-string-interpolation-2026-05-01
- D-double-8a-commits-2026-05-01

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

## End of Canonical Recap v1