# Luciel — Canonical Recap (Business)

**Scope:** Business value, product, pricing, roadmap, GTM, moat, locked decisions, exclusions.
**Out of scope:** Code architecture, AWS topology, drift register. See `ARCHITECTURE.md` and `DRIFTS.md`.

**Maintenance protocol:** Surgical edits only. No version-history sediment. When a roadmap step lands, update §0.3 in place. When a decision changes, update §0.4 in place and log the prior decision in `DRIFTS.md`. Per-commit narrative belongs in git history; per-drift narrative belongs in `DRIFTS.md`; this doc holds the strategic frame and the locked items.

**Last updated:** 2026-05-08 (Step 29.y close-out — three-doc regime, recap restored to v3.4 strategic content)

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
| Hardening | **28** | Operational maturity sprint — security/compliance, observability, hygiene, cosmetic — **4 phases**, 13–15 commits | **Phase 1 complete** at `bd9446b` (tag `step-28-phase-1-complete`). **Phase 2 closed** at tag `step-28-phase-2-complete` (full §8 prod-hardening evidence captured incl. live `pg_stat_activity` worker-role witness). **Phase 3 closed** at `737a56c` (tag `step-28-complete`, 11-commit C1→C11.b sweep, suite lifted to 23/23 GREEN via Pillars 20/21/22/23). **Phase 4 partial close** at `d425ede` (tag `step-28-phase-4-partial`, P4-A circuit-breaker + P4-B service-name wrapper); P3-I (WAF Count→Block flip) calendar-gated to ≥ 2026-05-13. |
| Identity | **24.5c** | User identity claims (phone/email/SSO) + conversation grouping (Q8) | Candidate, slot before Step 30 |
| Testing | **29** | Automated testing suite (pytest wrap of `app.verification`) + pillar registry + CI gate + verify-debt closure (P14 forensic reads migrated to HTTP forensics_get wrapper; P11 F10 direct ORM write migrated to new platform-admin POST `/luciel_instances_step29c/{id}/toggle_active`); cross-pillar cleanup via new `_infra_probes` module | **CLOSED** at tag `step-29-complete` (`89afbae`, 2026-05-07). Step 29.x + 29.y landed past that tag on `step-29y-impl` (44 commits / ~5407 ins / 279 del). Gap-fix series on `step-29y-gapfix` (10 commits, 2026-05-07) aligns work with doctrine. **Tag `step-29y-complete` on `5f297b7` (main, 2026-05-08)** anchors the close. Pillar count is now 25 on disk (P1–P25); `luciel-verify:20` returns **25/25 FULL** (rev32 digest `sha256:22b2a029...`). |
| Billing | **30a** | Stripe billing (subscription tiers, webhooks, tier-gated features) **+ integration with operator's existing company website** (checkout flow + marketing-site→app handoff; canonical website URL captured at 30a entry) | Candidate, slot between 29 and 30b in original sequencing; **deferred to AFTER 30b per 2026-05-06 ordering decision** — first revenue (REMAX Tier 3 trial) is gated on 30b only and manual invoicing is acceptable, so 30a + website integration design happens once 30b proves the product surface |
| Frontend | **30b** | Embeddable chat widget (JS drop-in for tenant sites) — **highest-leverage commit, REMAX trial unblock** | Planned |
| Frontend | **31** | Hierarchical tenant dashboards (Q2) + pre-launch validation gate (5 tiers: isolation, customer journey, memory quality, ops readiness, compliance) | Planned (after 34) |
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
- **Database:** RDS Postgres, master role `luciel_admin`. Role `luciel_worker` exists with least-privilege grants. **Step 28 Phase 2 Commit 4 SHIPPED 2026-05-05 14:51:27 UTC** via Pattern N variant (`luciel-mint:3` Fargate task in-VPC), rotating the `luciel_admin` password and minting the worker password into SSM SecureString `/luciel/production/worker_database_url`. Worker process now connects as `luciel_worker` (read-mostly, zero writes on identity tables); backend web tier (`luciel-backend:29`) connects as `luciel_admin` and owns identity tables. The B.3 task-identity boundary is between task definitions, NOT between handler types within one task. Independently re-witnessed 2026-05-07 via the C.5 grant-check diagnostic (`luciel-grant-check:4` forked from backend TD :29) which returned `IDENTITY: (luciel_admin, luciel_admin, none)` from inside the backend image, confirming the C.5 single-engine design is correct.
- **Retention purges are batched** as of Phase 2 Commit 8 (`0d75dfe`). Defaults: 1000 rows/batch, 50 ms inter-batch sleep, 10000 batches/run cap. `FOR UPDATE SKIP LOCKED` keeps purges safe to run alongside live chat traffic. Tunable via `LUCIEL_RETENTION_*` env vars.
- **Operator patterns codified:** E (secrets), N (migrations), O (recon), S (cleanup walker)
- **Three-channel audit** for every prod mutation: CloudTrail + CloudWatch + admin_audit_logs
- **Audit emission posture:** Two append-only streams. `admin_audit_logs` = control-plane and data-plane control events (hash-chained per Pillar 23, append-only at the DB-grant level per Pillar 22). `deletion_logs` = canonical record for retention purge events. Cascade events emit one bulk-summary row in `admin_audit_logs` with full per-resource detail in `after_json`. Regulator-facing exports merge the two streams ordered by `created_at` and expand bulk rows on demand. Full rationale lives in `docs/compliance/audit-emission-posture.md`.

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

**The platform contract (what the tenant gets):**
1. **Conversations don't reset.** A client emailing Monday and chatting Friday is the same person, with the same context, to every agent on the team.
2. **Agents don't forget.** Notes, preferences, prior offers, and constraints persist across sessions and across channels.
3. **Operators trust the audit trail.** Every memory write, every tool call, every actor action is captured in three-channel audit (DB row + hash chain + CloudWatch).
4. **Tenants don't bleed.** Memory is tenant-isolated at every query boundary; cross-tenant access is verified by attack-test (Pillar 13 A5).
5. **Departure is clean.** When a tenant leaves, cascade deactivation removes their data without breaking the audit chain (Pattern E).

---

## Section 2 — Pre-launch validation gate (Step 31, five tiers)

Before the first external tenant goes live, the platform must pass a five-tier validation gate. The 25-pillar harness (Step 29) covers Tier 1 partially; Step 31 closes the gaps.

| Tier | Question | Status |
|---|---|---|
| 1 — Isolation | Is memory truly scoped per-tenant, per-agent, per-domain, per-instance? | Per-tenant ✓ (P13 A5). Per-agent: schema enforces, no live attack test (gap). Per-domain: not a memory_items column today (product-intent gap). Per-instance: schema FK, weak coverage. |
| 2 — Customer journey | Does end-to-end signup → multi-session → cross-session memory work for a real customer? | Not yet tested |
| 3 — Memory quality | What is precision/recall of memory retrieval on held-out conversations? | No eval harness (Step 33b) |
| 4 — Ops readiness | DLQ alarms, 5xx alarms, worker-failure alarms, rollback runbook tested? | Partial (CloudWatch live; alarms not yet bound; rollback runbook not rehearsed) |
| 5 — Compliance | Tenant data-deletion request flow + tenant offboarding rehearsed? | Not yet documented |

Step 31 produces the spec; subsequent steps close each tier. **No external tenant goes live until all five tiers pass.**

---

## Section 3 — Current state

- Platform foundation (Steps 24.5c–28) complete and live at `api.vantagemind.ai`.
- 25-pillar verification harness (Step 29 + 29.x + 29.y) closed at tag `step-29y-complete` on main `5f297b7` (2026-05-08). `luciel-verify:20` (rev32 digest `sha256:22b2a029...`) returns **25/25 FULL**.
- Worker autoscaling live (CFN stack `luciel-prod-worker-autoscaling`, capacity 1–4, CPU 60% target) since 2026-05-05.
- Backend autoscaling deferred to Step 30 (deliberate, ALB-fronted steady-state design).
- No external tenants yet. REMAX Crossroads (warm Tier 3 lead, $2,000/mo, Markham) is the first target; widget rollout is Step 30b.
- Three-doc regime adopted 2026-05-08: `CANONICAL_RECAP.md` (business), `ARCHITECTURE.md` (technical), `DRIFTS.md` (open + resolved).

---

## Section 4 — Roadmap commentary (near-term)

See §0.3 for the full table. Notes on near-term steps:

- **Step 30b (widget rollout) — next.** Highest leverage because it unblocks REMAX Crossroads and produces the first revenue-bearing tenant. Stripe (30a) is deferred behind it because we don't need automated billing for one tenant; we can invoice manually until tenant #2.
- **Step 31 (validation gate)** must follow 30b but precede any second tenant. Five-tier gate per Section 2.
- **Step 32 (operator dashboards)** unblocks self-service support; required before Step 33 onboarding.
- **Step 33b (Tier B dedicated AWS)** is gated on a tenant willing to pay the dedicated-stack premium; not built speculatively.
- **Step 24.5c (cross-channel session continuity, Q8)** is candidate slot between 28 and 30; pulls forward if REMAX needs widget-and-phone before launch.

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
- **Domain as scoping dimension on memory_items** — product-intent gap (see Section 2 Tier 1). May or may not become a column; alternative is to scope via agent → domain mapping.

---

## Section 6 — Pricing strategy (detail)

### 6.1 Tier card

Locked in §0.2. Prices and audience map there; this section covers per-tier characteristics and unit economics.

### 6.2 Per-tier characteristics

**Tier 1 Individual ($30–80/mo):**
- Customer IS the end-user
- Self-service onboarding
- Cancellation fully automatic via Stripe webhook
- Memory cascade automatic
- **Hard dependency: code-level cascade** (✅ Commit 12, `f9f6f79`, shipped 2026-05-02)
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
2. **Audit posture** — brokerage-DD-defensible operator discipline (every Step 28 Phase 1+ tag is real proof, not a slide)
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

**Critical-path:** First revenue does NOT require Commit 12 OR Step 30a Stripe billing (REMAX is Tier 3, manual invoicing acceptable for trial). First revenue IS gated on Step 30b (chat widget). Milestone 2 ($5K MRR volume play) DOES require both Commit 12 (shipped 2026-05-02) AND Step 30a Stripe billing.

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
- **Audit-emission gaps for IAM-side privileged actions** — AssumeRole calls into `luciel-mint-operator-role` land in CloudTrail, but Luciel's `admin_audit_logs` does not yet ingest CloudTrail. Acceptable for current posture; explicit gap for Tier B / SOC2 readiness.

### 11.3 Brokerage DD answer template
"When a brokerage cancels their subscription, every memory data point, every API key, every agent persona, every domain, every Luciel instance flips to inactive in a single atomic transaction. Audit logs in admin_audit_logs show exactly what was deactivated, when, by whom, and what cascade reason. Soft-deleted rows scheduled for hard-purge within [N days] (future retention worker)."

### 11.4 Audit emission posture (canonical)
Locked in §0.4 final bullet. Two append-only streams (`admin_audit_logs` + `deletion_logs`); regulator-facing exports merge by `created_at` and expand bulk rows on demand. Full rationale in `docs/compliance/audit-emission-posture.md`.

---

## Section 12 — Working memory anchors (drift recovery)

If conversation context is lost, these are the most important facts to preserve:

1. **Pricing is scope-aligned:** Individual=agent, Team=domain, Company=tenant. Architecture diagram = price card.
2. **Commit 12 (`f9f6f79`) unblocks the Individual tier** — without code-level cascade, $30/mo tier cannot ship.
3. **REMAX Crossroads is Tier 3 / $2K/mo**, gated by Step 30b widget, NOT by Commit 12.
4. **Step 30b is the highest-leverage commit on the roadmap** — REMAX trial unblock.
5. **Step 28 has 4 phases** (security/compliance, observability, hygiene, cosmetic). Phase 1 complete at `bd9446b`; Phase 2 closed at tag `step-28-phase-2-complete`; Phase 3 closed at `737a56c` (tag `step-28-complete`); Phase 4 partial close at `d425ede` (tag `step-28-phase-4-partial`); only P3-I (WAF flip) carries to ≥ 2026-05-13. **Step 29 closed at `step-29-complete` (`89afbae`, 2026-05-07). Step 29.y close-out tagged `step-29y-complete` on main (`5f297b7`, 2026-05-08); pillar count is 25/25 FULL on `luciel-verify:20`.**
6. **Pillar 13 A3 fixed by Phase 2 Commit 3** (`56bdab8`) — was a test-design issue (sentinel-not-extractable), never a security gap. Phase 3 lifted suite to 23/23 green via Pillars 20/21/22/23 (C2/C4/C5/C6); Step 29.x added P24/P25.
7. **Worker DB role swap is Phase 2 Commit 4** — ✅ **SHIPPED 2026-05-05 14:51:27 UTC** via Pattern N variant (`luciel-mint:3` Fargate task in-VPC, after P3-S architectural rework). Worker process runs as `luciel_worker` with least-privilege grants — directly witnessed at `2026-05-06T01:24:17 UTC` via `pg_stat_activity` snapshot.
8. **Five willingness-to-pay drivers:** maintainability, scalability, reliability, security, traceability.
9. **Three deliberate exclusions:** no mobile, no marketplace, no model training. Adding any requires roadmap conversation.
10. **Operator patterns codified:** E (secrets), N (migrations), O (recon), S (cleanup, now backup). Runbooks at `docs/runbooks/`.
11. **Locked strategic-question answers:** Q1 ✅, Q2 → Step 31, Q3 → Step 37, Q4 → Step 36, Q5 → Step 38, Q6 ✅, Q7 → Step 34a, Q8 → Step 24.5c.
12. **Three-doc regime is source of truth** — `CANONICAL_RECAP.md` (business), `ARCHITECTURE.md` (technical), `DRIFTS.md` (open + resolved). If any chat recap contradicts these, these documents win.
13. **Tag chronology:** `step-28-complete` (`737a56c`) → `step-28-phase-4-partial` (`d425ede`) → `step-29-complete` (`89afbae`) → `step-29y-complete` (`5f297b7`, on main).

---

## Section 13 — Resumption protocol

Every new session begins with this 4-step ritual. No work proposed before completing it.

**Step 1 — Read this canonical recap top-to-bottom.** Do not infer from memory; re-read.

**Step 2 — Read `DRIFTS.md` open section.** Re-read resolved drifts only if the upcoming step touches a previously-drifted surface.

**Step 3 — Read `ARCHITECTURE.md`** if anything in the step plan touches code or AWS.

**Step 4 — 5-block pre-flight (before any prod-touching work).**

- Block 1 — AWS identity (expect `729005488042`):
  `aws sts get-caller-identity --query Account --output text`
- Block 2 — Git state (expect clean working tree):
  `git status --short; git log -1 --oneline; git stash list`
- Block 3 — Docker:
  `docker info --format "{{.ServerVersion}} {{.OperatingSystem}}"`
- Block 4 — Dev admin key (expect True / 50):
  `$env:LUCIEL_PLATFORM_ADMIN_KEY.StartsWith("luc_sk_"); $env:LUCIEL_PLATFORM_ADMIN_KEY.Length`
- Block 5 — Verify harness on prod tip (expect 25/25 FULL):
  Run `luciel-verify:20` Fargate task and confirm `exitCode 0` + `25/25` in CloudWatch.

Then check `docs/incidents/` for anything new since last session. Only then start the next roadmap step.

---

## Section 14 — Discipline reminders

- **No deferring.** If a drift is found, log it in `DRIFTS.md` immediately. Do not "we'll get to it." That is how we drifted.
- **Pattern E always.** Deactivate, never delete. No row deletions.
- **Audit chain stays intact.** No retroactive deletes that break the hash chain. Forward-only fixes.
- **Verify after every commit.** Run the relevant verification step before declaring a commit done.
- **Honest assessment.** If a step is degraded but live, say "live in degraded state" — not "live." The honest answer protects the business.
- **No emojis. No exclamation. No "scrape."**

---

## Section 15 — Source of truth rule

If a chat recap or session summary contradicts this document (or `ARCHITECTURE.md` or `DRIFTS.md`), these documents win. Update via PR with rationale; do not produce contradicting recaps inline.

---

## Section 16 — Maintenance protocol

- **This doc is business-only.** Code/AWS detail goes in `ARCHITECTURE.md`. Drifts go in `DRIFTS.md`.
- **Surgical edits only.** When a roadmap step lands, update §0.3 and §3 in place. When a decision changes, update §0.4 in place and log the prior decision in `DRIFTS.md`.
- **No version history.** No "v1.5", "v2.0", "v3.4" sections. The doc reflects the current state. History lives in git and in `DRIFTS.md` closures.
- **Resolved gaps go to `DRIFTS.md`** with strikethrough, not here.
- **One source of truth per fact.** If a fact appears in two places, delete one.
- **Per-commit narrative does NOT live here.** Commit hashes appear only when they anchor a locked decision (e.g., `f9f6f79` Commit 12 cascade, `adc5ba0` Q6 identity layer, `bd9446b` Step 28 Phase 1 tag). Per-commit detail belongs in git history; per-drift detail belongs in `DRIFTS.md`.
