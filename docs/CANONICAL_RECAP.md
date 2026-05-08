# Luciel — Canonical Recap (Business)

**Scope:** Business value, product, pricing, roadmap, GTM, moat, locked decisions, exclusions.
**Out of scope:** Code architecture, AWS topology, drift register. See `ARCHITECTURE.md` and `DRIFTS.md`.

**Maintenance protocol:** Surgical edits only. No version-history sediment. When a roadmap step lands, update §3 in place. When a decision changes, update §4 in place and log the prior decision in `DRIFTS.md`.

**Last updated:** 2026-05-08 (Step 29.y close-out)

---

## §0 Strategic Frame

### §0.1 Eight Strategic Questions (Q1–Q8)

| # | Question | Answer | Roadmap slot | Status |
|---|----------|--------|--------------|--------|
| Q1 | What is Luciel? | A domain-agnostic, tenant-isolated AI memory + reasoning platform that brokerages (and later other verticals) plug into existing channels (web, email, voice) so every customer interaction is remembered, audited, and acted on. | Steps 24.5–29 (foundation) | Locked |
| Q2 | Who is the customer? | The **tenant** (a brokerage, agency, or company). End-users (agents, clients) consume but do not pay. | All steps | Locked |
| Q3 | What is the wedge? | GTA real-estate brokerages, anchored by REMAX Crossroads (Markham-local, warm Tier 3 lead, $2,000/mo). | Step 30b widget rollout | In progress |
| Q4 | Why will tenants pay? | Five willingness-to-pay drivers: maintainability, scalability, reliability, security, traceability. (See §8.) | All steps | Locked |
| Q5 | What is the moat? | Integration depth, audit posture, scope-hierarchy correctness, cascade-correct departure. (See §7.) | Compounding | Locked |
| Q6 | What is the pricing shape? | Three-tier: Individual $30–80, Team/Domain $300–800, Company/Tenant $2,000+. Tier B (per-customer dedicated AWS) at Step 33b. Tier C (on-prem) excluded unless paid. (See §0.2.) | Step 30a Stripe → Step 33b dedicated | Locked |
| Q7 | What is the revenue path? | $0 → first paying tenant Q3 2026 → $1M ARR Q4 2028. (See §10.) | Steps 30b–38 | Locked |
| Q8 | What disqualifies a feature? | Anything in §5 exclusions: mobile-first, marketplace, fine-tuning, i18n, feature-parity chasing, on-prem (unless paid). | All steps | Locked |

### §0.2 Pricing Tiers

| Tier | Price | Who | What they get |
|------|-------|-----|---------------|
| Individual | $30–80/mo | Solo agent, indie operator | Single-user memory, single channel |
| Team / Domain | $300–800/mo | Team (5–25 seats) inside a brokerage | Shared memory across team, multi-channel |
| Company / Tenant | $2,000+/mo | Whole brokerage / company | Tenant-isolated platform, all channels, audit, integrations |
| Tier B (Dedicated) | Custom (Step 33b) | Tenants requiring isolated AWS | Per-customer dedicated stack |
| Tier C (On-prem) | Excluded unless paid | Enterprise with legal mandate | Not in roadmap; case-by-case only |

### §0.3 Roadmap (Steps 24.5c–38)

| Step | Title | Status |
|------|-------|--------|
| 24.5c | Identity foundation | Done |
| 25 | Tenant scaffolding | Done |
| 26 | Memory primitives | Done |
| 27 | Security contract | Done |
| 27b | Security contract hardening | Done |
| 27c | Worker deploy | Done |
| 28 | Production hardening (Phases 1–4) | Done |
| 29 | 25-pillar verification harness | Done |
| 29.x | Verification harness gap-fixes | Done |
| 29.y | Verification harness close-out + 3-doc regime | In progress (this commit) |
| 30a | Stripe billing | Deferred (after 30b) |
| 30b | REMAX widget rollout | **Next — highest leverage** |
| 31 | **Pre-launch validation gate (5 tiers — see §1.1)** | Spec needed |
| 32 | Operator dashboards | Pending |
| 33 | Self-service onboarding | Pending |
| 33b | Eval harness (memory precision/recall) | Pending |
| 33c | Tier B dedicated-AWS template | Pending |
| 34 | Workflow actions | Pending |
| 34a | Channels (email, voice) | Pending |
| 35 | Multi-vertical (beyond real estate) | Pending |
| 36 | Council (multi-agent reasoning) | Pending |
| 37 | Hybrid retrieval (vector + keyword) | Pending |
| 38 | Tenant merge / acquisition support | Pending |

### §0.4 Locked Decisions

- **Database:** PostgreSQL via psycopg v3 driver
- **Project layout:** `pyproject.toml` (no `setup.py`)
- **Config naming:** `_configs` suffix on all config tables
- **Region:** `ca-central-1` (data residency: Canada)
- **Domain:** `api.vantagemind.ai`
- **DB roles:** `luciel_admin` (DDL/migrations), `luciel_worker` (runtime DML)
- **Deletion:** Soft-delete only (`is_active`/`deleted_at`); no row deletions
- **Cascade:** Tenant cascade enforced in code, not DB FK
- **Audit:** Three-channel (audit table + hash chain + CloudWatch)
- **Operator patterns:** E (deactivate, never delete), N (no-op safety), O (operator-tagged), S (secrets in SSM)
- **Auth model:** Tenant + agent + actor; actor permissions JSONB (Step 30b migration to typed format)

### §0.5 Exclusions (what we will NOT build)

- **No mobile-first apps.** Web-first, channel-agnostic; mobile via responsive web only.
- **No marketplace.** No third-party plugin store.
- **No fine-tuning.** Use frozen foundation models + retrieval; no per-tenant model training.
- **No i18n** until first non-English-speaking tenant pays.
- **No on-prem (Tier C)** unless explicitly paid for as a custom engagement.
- **No feature-parity chasing.** We do not ship features just because competitors have them.

---

## §1 What Luciel Does (Business Value)

Luciel is the **memory and reasoning layer** that sits between a brokerage's existing channels (website chat, email, voice) and the foundation models (Anthropic, OpenAI). For each tenant, every customer interaction is captured, scoped (per-tenant, per-agent, per-domain, per-instance), persisted in a queryable memory store, and made available to subsequent interactions. The platform is designed so that:

1. **Conversations don't reset.** A client emailing Monday and chatting Friday is the same person, with the same context, to every agent on the team.
2. **Agents don't forget.** Notes, preferences, prior offers, and constraints persist across sessions and across channels.
3. **Operators trust the audit trail.** Every memory write, every tool call, every actor action is captured in a three-channel audit (DB row + hash chain + CloudWatch) so a tenant can answer "who did what when" at any time.
4. **Tenants don't bleed.** Memory is tenant-isolated at every query boundary; cross-tenant access is verified by attack-test (Pillar 13 A5).
5. **Departure is clean.** When a tenant leaves, cascade deactivation removes their data without breaking the audit chain (Pattern E).

### §1.1 Pre-Launch Validation Gate (Step 31 — five tiers)

Before the first external tenant goes live, the platform must pass a five-tier validation gate. The 25-pillar harness (Step 29) covers Tier 1 partially; Step 31 closes the gaps.

| Tier | Question | Status |
|------|----------|--------|
| 1 — Isolation | Is memory truly scoped per-tenant, per-agent, per-domain, per-instance? | Per-tenant ✓ (P13 A5). Per-agent: schema enforces, no live attack test (gap). Per-domain: not a memory_items column today (product-intent gap). Per-instance: schema FK, weak coverage. |
| 2 — Customer journey | Does end-to-end signup → multi-session → cross-session memory work for a real customer? | Not yet tested |
| 3 — Memory quality | What is precision/recall of memory retrieval on held-out conversations? | No eval harness (Step 33b) |
| 4 — Ops readiness | DLQ alarms, 5xx alarms, worker-failure alarms, rollback runbook tested? | Partial (CloudWatch live; alarms not yet bound; rollback runbook not rehearsed) |
| 5 — Compliance | Tenant data-deletion request flow + tenant offboarding rehearsed? | Not yet documented |

Step 31 produces the spec; subsequent steps close each tier. **No external tenant goes live until all five tiers pass.**

---

## §2 Current State

- Platform foundation (Steps 24.5c–28) is complete and in production at `api.vantagemind.ai`.
- 25-pillar verification harness (Step 29) passes 25/25 FULL on `luciel-verify:20` (rev32 digest `sha256:22b2a029...`); gap-fixes C29–C33 closed.
- Worker autoscaling live (CFN stack `luciel-prod-worker-autoscaling`, capacity 1–4, CPU 60% target) since 2026-05-05.
- Backend autoscaling deferred to Step 30 (deliberate, ALB-fronted steady-state design).
- No external tenants yet. REMAX Crossroads (warm Tier 3 lead, $2,000/mo, Markham) is the first target; widget rollout is Step 30b.
- Three-doc regime adopted (this commit): `CANONICAL_RECAP.md` (business), `ARCHITECTURE.md` (technical), `DRIFTS.md` (open + resolved).

---

## §3 Roadmap Detail

See §0.3 table for the full slot list. Notes on near-term steps:

- **Step 30b (widget rollout) — next.** Highest leverage because it unblocks REMAX Crossroads and produces the first revenue-bearing tenant. Stripe (30a) is deferred behind it because we don't need automated billing for one tenant; we can invoice manually until tenant #2.
- **Step 31 (validation gate)** must follow 30b but precede any second tenant.
- **Step 32 (operator dashboards)** unblocks self-service support; required before Step 33 onboarding.
- **Step 33b (Tier B dedicated AWS)** is gated on a tenant willing to pay the dedicated-stack premium; not built speculatively.

---

## §4 Locked Decisions Detail

See §0.4 for the list. When any item changes, log the prior state in `DRIFTS.md` with a closure entry, then update §0.4 in place. Do not keep version history here.

---

## §5 Future Concepts (parking lot — not committed)

- **Council (Step 36):** multi-agent debate where two specialized agents argue a position before producing a tenant-facing answer. Increases reliability for high-stakes brokerage advice; cost is latency + token spend.
- **Hybrid retrieval (Step 37):** combine vector similarity with keyword/BM25 for memory recall; expected to lift precision on rare-keyword queries.
- **Tenant merge (Step 38):** support brokerage acquisitions where two tenants must consolidate without losing audit history or breaking scope isolation.
- **Voice-first channel:** post-Step 34a; depends on real-time STT/TTS economics.
- **Domain as scoping dimension on memory_items:** product-intent gap (see Step 31 Tier 1). May or may not become a column; alternative is to scope via agent → domain mapping.

---

## §6 Per-Tier Characteristics

| Tier | Onboarding | Channels | Audit access | Custom prompts | SLA |
|------|------------|----------|--------------|----------------|-----|
| Individual | Self-serve | 1 channel | Read-only own data | No | None |
| Team / Domain | Self-serve | Up to 3 channels | Team-scoped | Shared templates | Best-effort |
| Company / Tenant | Assisted | All channels | Full tenant audit | Tenant-owned templates | 99.5% target |
| Tier B (Dedicated) | White-glove | All + custom | Full + raw DB read | Full | 99.9% target |

---

## §7 Moat

Four compounding sources:

1. **Integration depth.** Each new channel (web, email, voice, CRM) and each new tenant integration creates switching cost.
2. **Audit posture.** Three-channel audit + hash chain + tenant data-residency in `ca-central-1` is non-trivial to replicate and is a regulated-industry differentiator.
3. **Scope-hierarchy correctness.** Tenant → agent → actor → memory scoping is enforced at schema, query, and attack-test layers; competitors that bolt on multi-tenancy late get this wrong.
4. **Cascade-correct departure.** Pattern E (deactivate, never delete) means tenant offboarding is fast, clean, and audit-safe; tenants trust this because they can verify it.

---

## §8 Five Willingness-to-Pay Drivers

| Driver | What the tenant gets | Where it shows up |
|--------|----------------------|-------------------|
| Maintainability | Code/data they can hand to a successor without surprise | Pyproject.toml, `_configs` naming, soft-delete, runbooks |
| Scalability | Headroom to 10× users without re-architecture | ECS autoscaling (worker), RDS, async memory writes |
| Reliability | Service stays up under load and recovers cleanly | Multi-AZ RDS, ECS health checks, DLQ + retry, Pattern N |
| Security | Tenant data is isolated and access is logged | Tenant-FK NOT NULL, three-channel audit, SSM-only secrets, IAM least-privilege |
| Traceability | Tenant can answer "who did what when" for any audit | Hash-chained audit, CloudWatch retention, actor-permissions JSONB |

---

## §9 GTM Phases

| Phase | Trigger | Activities |
|-------|---------|------------|
| Phase 0 — Foundation | Pre-Step 30b | Build platform, verification harness, runbooks. (Done) |
| Phase 1 — REMAX wedge | Step 30b ships | Onboard REMAX Crossroads. Manual invoice. Single-tenant operations. |
| Phase 2 — GTA brokerage cohort | Phase 1 stable + Step 31 gate passes | Outbound to 5–10 GTA brokerages. Stripe (30a) becomes worth automating. |
| Phase 3 — Vertical breadth | Phase 2 hits 10 paying tenants | Step 35 multi-vertical: pilot a non-real-estate brokerage-shaped business (insurance, mortgage, legal). |
| Phase 4 — Tier B / Enterprise | Phase 3 hits 25 paying tenants | Step 33b dedicated-AWS for tenants requiring isolated stacks. |

---

## §10 Revenue Milestones

| Quarter | Milestone | Tenant count assumption |
|---------|-----------|-------------------------|
| Q3 2026 | First paying tenant (REMAX Crossroads) | 1 × $2k = $24k ARR |
| Q4 2026 | 3 paying tenants | $72k ARR |
| Q2 2027 | 10 paying tenants | $240k ARR |
| Q4 2027 | 25 paying tenants + first Tier B | $600k–$700k ARR |
| Q4 2028 | $1M ARR | ~40 tenants blended Tier 3 + Tier B |

---

## §11 Compliance

- **Data residency:** `ca-central-1` only. No cross-region replication today (Step 38 cluster 4b carry-forward).
- **Audit retention:** CloudWatch log groups + DB audit table + hash chain. Retention policies set per log group.
- **Access control:** SSM-only secrets, IAM least-privilege per role (`luciel-prod-ops`, `luciel-worker`, `luciel-backend`).
- **Tenant deletion:** Pattern E soft-delete preserves chain; full erasure procedure to be specified in Step 31 Tier 5.
- **PII handling:** Tenant owns the data; Luciel is processor. Customer data flows through foundation-model APIs under their respective DPAs.

---

## §12 Drift Recovery Anchors

If a future operator finds the platform in an unexpected state, the recovery anchors are:

1. **`DRIFTS.md`** — read it first. Resolved drifts (strikethrough) explain prior decisions; open drifts list known gaps.
2. **`ARCHITECTURE.md`** — current dev + prod topology, data model, request flow.
3. **`docs/runbooks/`** — operational runbooks for deploy, rotation, prod access.
4. **`docs/incidents/`** — postmortems for prior incidents (admin-DSN disclosure, prod RDS migration gap, platform-admin-key consolidation).
5. **`Luciel/app/verification/`** — 25-pillar verification harness; if you can't explain a behavior, run the relevant pillar.

---

## §13 Resumption Protocol

When picking up Luciel work after a gap:

1. Read this doc top-to-bottom.
2. Read `DRIFTS.md` open section.
3. Read `ARCHITECTURE.md` if anything in step plan touches code/AWS.
4. Run the verification harness (`luciel-verify:20`) to confirm 25/25 FULL.
5. Check `docs/incidents/` for anything new since last session.
6. Only then start the next roadmap step.

---

## §14 Discipline Reminders

- **No deferring.** If a drift is found, log it in `DRIFTS.md` immediately. Do not "we'll get to it." That is how we drifted.
- **Pattern E always.** Deactivate, never delete. No row deletions.
- **Audit chain stays intact.** No retroactive deletes that break hash chain. Forward-only fixes.
- **Verify after every commit.** Run the relevant verification step before declaring a commit done.
- **Honest assessment.** If a step is degraded but live, say "live in degraded state" — not "live." The honest answer protects the business.
- **No emojis. No exclamation. No "scrape."**

---

## §15 (reserved)

---

## §16 Maintenance Protocol

- **This doc is business-only.** Code/AWS detail goes in `ARCHITECTURE.md`. Drifts go in `DRIFTS.md`.
- **Surgical edits only.** When a roadmap step lands, update §0.3 and §3 in place. When a decision changes, update §0.4 in place and log the prior decision in `DRIFTS.md`.
- **No version history.** No "v1.5", "v2.0", "v3.4" sections. The doc reflects the current state. History lives in git and in `DRIFTS.md` closures.
- **Resolved gaps go to `DRIFTS.md`** with strikethrough, not here.
- **One source of truth per fact.** If you find a fact in two places, delete one.
