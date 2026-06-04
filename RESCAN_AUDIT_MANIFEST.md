# VantageMind — Full Re-Scan Audit Manifest (v1.3 Alignment)

**Date:** 2026-06-04 · **Phase:** AUDIT (read-only) — no destructive changes made.
**Source of truth (ranked):** VISION v1.3 > ARCHITECTURE v1.3 > CUSTOMER_JOURNEY v1.3 > code.
**Method:** Full read of all three documents + parallel read-only audit of backend (FastAPI, 130
migrations, ~150 modules, 26 routers, 36 models, 182 test files) and frontend (React/TS, ~150 files).
Six per-domain manifest sections are in `manifest_sections/01..06`; this is the consolidated contract.

This manifest is the contract for the post-approval execution phase. **Nothing destructive
(code/file deletion, schema drop/alter, infra change) happens before founder approval of this manifest.**

---

## 0. BASELINE REALITY (read this first — it reframes the task)

### 0.1 Access actually held
- **GitHub:** Full read/write via `gh`/`git` CLI to both repos (`aryanonline/Luciel`, `aryanonline/Luciel-Website`). Confirmed; sufficient for all code/schema/CI work.
- **AWS:** Only the `aws__pipedream` connector, which exposes **data-plane primitives only** (S3, SQS, SNS, DynamoDB, Lambda, Redshift, CloudWatch-put). It exposes **no** ECS/RDS/CloudFormation/ECR/IAM/Secrets Manager/ALB control-plane verbs.
  - **Consequence:** I cannot read or reconcile live AWS infrastructure state (deployed task defs, RDS schema-on-server, CFN stack status, ECR image digests, secret ARNs). All "deployed code matches branch / image matches build / schema-on-server matches migration / secret path matches IAM" checks are **BLOCKED-EXTERNAL**. I audited infra **as-declared-in-repo** instead.
  - The credentials PDF in the Space contains a live AWS key with an unrestricted `*/*` IAM policy. Founder elected to handle rotation/scoping personally; I have **not** used those pasted keys.

### 0.2 Environment reality — THE decision-critical finding
- **There is no staging environment.** Confirmed in repo: zero staging CFN stacks, zero staging parameter files, only `/luciel/production/` SSM path prefixes exist (manifest §06). Architecture §4.3 declares a `dev → staging → prod` pipeline — that pipeline is **undeclared in IaC** (DRIFTED).
- **Implication for the "STAGING ONLY, never touch production" guardrail:** today every AWS resource that exists *is* production. There is nothing non-production to "only touch." Therefore **standing up an isolated, tagged, separately-named staging environment is the precondition** that makes all subsequent infra work safe — it is the first post-approval infra action, before any other infra change, and no mutating command will run against existing (prod) resources.

### 0.3 Naming drift (system-wide, AMBIGUOUS→needs a ruling)
The docs use `vm-*` / `vantagemind.com` (e.g. `vm-control-plane`, `vm-data-plane`, `vm-knowledge-staging`, `embed.vantagemind.com`, `vm/{admin_id}/...` secret prefix). The code/infra use `luciel-*` / `luciel-mail.com` / `luciel/connections/...`. This is almost certainly an un-back-ported brand rename, not a defect — but it touches IAM secret-path conventions (§0.1) and IaC resource names. **Recorded as a CONFLICT for a one-line founder ruling** (treat `luciel-*` as canonical and amend docs, OR rename infra to `vm-*`). I will NOT mass-rename without that ruling.

---

## 1. HEADLINE — WHAT IS TRUE

**The crown-jewel security and data-integrity layers genuinely CONFORM.** This is a mature, well-maintained system (prior ARC reports show disciplined cleanups; entitlements.py enforces locked Decision #19 etc.):
- Two-plane auth-subject wall: CONFORMS.
- Three-layer tenant isolation: RLS present on all tenant tables (49 migrations), per-request `app.admin_id` GUC via `SET LOCAL`, app-layer filter: CONFORMS (two doc-name mismatches, below).
- Append-only hash-chained `admin_audit_log` (REVOKE UPDATE/DELETE, RLS WITH CHECK, SHA-256 per-admin chain): CONFORMS.
- Default-deny three-gate tool authorization, full 8-tool v1 catalog, sibling-grant two-layer model with cycle+fan-out guardrails: CONFORMS.
- Single linear Alembic chain, 130 migrations, all with downgrades, single head: CONFORMS.
- Conversation budget metering (per-instance Redis counter, Stripe-cycle reset, no rollover, Free at-cap no-LLM path): CONFORMS.

**Aggregate status across the six slices (≈238 audited requirements):**
CONFORMS ≈ 105 · DRIFTED ≈ 48 · MISSING ≈ 31 · BUG = 4 · AMBIGUOUS ≈ 12 · RESIDUE ≈ 26 items · BLOCKED-EXTERNAL ≈ 17.

**The drift concentrates in the exact v1.3 "trust & product completeness" promises** — handoff, escalation delivery, grounding fidelity, graph KB, analytics, full hard-delete cascade. Several of these are **genuinely unbuilt destination**, not cleanup. Approving this manifest approves a build-and-fix program, not a polish pass.

---

## 2. CRITICAL ITEMS (fix first — risk-ordered)

### TIER A — Correctness / legal / security (do first)
- **BUG-1 (legal exposure):** Tenant hard-delete cascade (`admin_service.py:1152-1239`) issues `DELETE FROM instances` without first clearing `knowledge_sources`/`knowledge_chunks`/`instance_connections`/`traces`, which are `ondelete=RESTRICT`. Any tenant with knowledge or a connection → **FK violation aborts the whole purge silently** → PIPEDA/GDPR Art.17 retention timelines violated. Fix: delegate to the (correct) instance-level retention pre-clear per instance before the tenant `DELETE`, or change FKs to `ON DELETE CASCADE`. (Arch §3.6.5/§3.6.6)
- **BUG-2 (frontend↔backend contract break):** `refreshConnection` frontend path `…/instances/{id}/connections/{id}/refresh` vs backend `…/connections/{id}/refresh` → **404 on every reconnect in prod** (CJ Marcus §7 "Reconnect needed" beat fails). Fix: align path. (§05)
- **BUG-3 (silent data loss in UI):** `ConnectionView` reads `last_verified_at`; backend field is `last_health_check_at` → connection health never displays. Fix: align field name. (§05, Arch §3.8.2)
- **BUG-4 (SSRF — security):** BYO-webhook sandbox enforces FQDN allowlist only; **no RFC1918/link-local/loopback/169.254.169.254 IP blocking and no DNS-rebind guard** (Arch §3.8.6 mandates all of these). Plus timeout is 30s vs spec 10s. Fix: add egress IP-range blocks + DNS-rebind validation; set timeout 10s. (§01)

### TIER B — Security-posture drift (do next)
- **MISSING:** Second-`admin_owner` approval workflow for sensitive custom-role changes (`can_configure_connections`/`can_manage_billing`) — Arch §3.7.3 requires `pending_approval→live`; absent for custom roles (the sibling-grant subsystem already implements the exact pattern to copy). (§01)
- **DRIFTED:** Connections schema vs Arch §3.8.2 — column names (`credential_ref`→`secret_ref`, `config_json`→`non_secret_config`), missing `created_by_user_id`+`status_detail`, status enum missing `revoked`+`dormant`, unique constraint is 4-tuple (incl. `provider`) vs spec 3-tuple. (§01) — schema migration, expand-contract.
- **DRIFTED:** Secret path `luciel/connections/{admin}/{instance}/{type}` vs spec `vm/{admin}/{instance}/*` — gated on the §0.3 naming ruling and on live IAM (BLOCKED-EXTERNAL). (§01)

### TIER C — Unbuilt v1.3 destination (genuine builds)
- **MISSING:** Live human handoff `human_controlled` session mode (Arch §3.4.12) — no session state, no stop-auto-reply gate, no `human_takeover_started/ended` audit events. Only an ack template exists. Frontend takeover UI also MISSING. (§02, §05)
- **MISSING:** Escalation **delivery** layer §3.5 in full — no `NotificationAdapter`, no `app/notifications/`, no per-signal routing/fan-out, no Enterprise chain walker + 5-min SLA + ack + owner-fallback, no retry/backoff, no idempotency key, 5 delivery audit events undefined. (NOTE: escalation *judgment* exists & conforms; only *delivery* is missing.) (§04)
- **MISSING:** Graph knowledge store + hybrid retrieval (Arc 16, Arch §3.2.1, §3.4.1 RETRIEVE) — no graph code; retrieval is vector-only; no rerank stage. (§02, §03)
- **MISSING:** Analytics module `app/analytics/` (Arch §3.9) — read-only dashboard exists but no tier-shaped analytics (Pro per-team-member, Enterprise fleet+SLA+CSV export). Frontend fleet view MISSING. (§04, §05)
- **MISSING:** Per-tier LLM model classes (Haiku/Sonnet/Opus) + intra-tier fast routing (Arch §3.4.3, Decisions #7/#9) — all tiers use one model; Anthropic-primary is registration-order-dependent not locked. (§02)
- **DRIFTED:** Grounding score is retrieval-relevance only; citation-overlap component absent; canonical "I don't have that information, let me get someone who does" phrase absent. (Arch §3.4.13) (§02)
- **DRIFTED:** High-value-lead heuristic is binary ($750k threshold) + real-estate-specific, vs spec's domain-agnostic weighted score (budget .5/time .3/intent .4 capped 1.0). (§02)

### TIER D — Lifecycle / channel / retention drift
- **DRIFTED:** Instance state enum has 3 states (`active/paused/deleted`) vs spec's 5 (`active/paused/deactivating/grace_window/hard_deleted`); grace is a timestamp proxy. (§03)
- **DRIFTED:** Instance-level hard-delete cascade omits `leads`, `summaries`, `sibling_call_grants`, explicit embed-key step. (§03)
- **DRIFTED:** Widget abuse controls — token-bucket (5 burst / 1-per-3s) replaced by flat `30/min`; bot challenge only on signup not widget; auto-block absent. (Arch §3.1.5) (§03)
- **DRIFTED:** Transcript retention is a single global 730d default vs per-tier 30d/1y/7y; S3 cold-archive for conversations missing. (Arch §3.4.10) (§04)
- **DRIFTED:** Data export is tar.gz/JSONL vs spec open-ZIP with per-session JSON + CSVs + original files; Free closure-only gate not enforced at API. (Arch §5.10) (§04)

### TIER E — Tier-matrix & contract drift
- **DRIFTED:** Uptime SLA values one tier low (entitlements Pro 99.5% vs Vision 99.9%; Ent 99.9% vs 99.95%). (Vision §7) (§04)
- **DRIFTED:** Graph-store entitlement axis missing from `entitlements.py`; Enterprise channels missing voice/WhatsApp markers. (§04)
- **DRIFTED:** RBAC permission vocabulary (14 granular names) ≠ §3.7.3's 14 named atomic permissions. (§01)
- **DRIFTED:** Channels UI is a checklist vs spec "multi-select dropdown"; 5 connection beats (email_sender/sms_sender/crm) that CJ describes as live are Arc17-deferred in UI. (§05)

### TIER F — RESIDUE (remove with dependency-impact analysis)
- Root: `td-backend-rev39/46/46-asregistered/47/48/49.json`, `td-worker-rev19/rev33.json` → stale historical task-def snapshots. **KEEP:** `td-backend-rev78.json` (current prod TD), `td-worker-rev34-arc11.json` (referenced by `arc11_close_audit.py`), `verify-td.json` (verify harness). (§06)
- `infra/iam/*.pre-p3-k-followup*`, `*.pre-p3-s-half*` backup files; `app/domain/stubs/__init__.py` empty dirs. (§06)
- `_SIBLING_ROUNDTRIP_SEAM` stub (documented deferred — leave, but note the callee is never invoked → sibling composition is non-functional end-to-end). (§03)
- ARC report .md files, `iam/*-post-patch-*.json`, `worker-deployment-config.json` → **do NOT delete without founder review** (institutional memory / live config). (§06)

### TIER G — Doc-name & path-drift normalizations (low-risk, but governance-relevant)
- §3.7.2b names tables `transcripts`/`session_summaries`; actual are `conversations`+`messages` / `leads.summary` column. Either rename or amend doc. (§01)
- Four §8 doctrine-anchored paths drifted (`llm_router.py`, `grounding.py`, `handoff.py`, `knowledge_retrieval.py`) — functionality exists elsewhere; the maintainability contract (§5.9.3, same-PR doc update) can't bind a path that doesn't exist. (§02)

---

## 3. INFRASTRUCTURE (as-declared) — DRIFTED vs §4.1/§4.3
- Core compute/DB/networking (ECS task defs/services, RDS, ElastiCache, VPC, ALB, Secrets Manager) have **no CloudFormation stack**; `cfn/luciel-twilio-webhook-routing.yaml:17` admits the ALB was console-provisioned — directly contradicting §4.3 "no console-provisioned resources in production."
- ca-central-1 residency **guardrails absent**: no CFN region `Conditions:` fail-on-wrong-region; no S3 `aws:RequestedRegion≠ca-central-1` Deny. (Arch §4.2) → MISSING enforcement.
- CloudWatch alarm set DRIFTED vs §5.1 (missing `DataPlane_ErrorRate_High`, `LLMPrimary_Degraded`, `BothLLMProviders_Down`, `BudgetGate_FreeCap_Anomaly`, `ConnectionHealth_Degraded`, `SmokeProbeFailed`).
- §5.5 smoke-probe suite (6 probes) does not exist as code; CI has no post-deploy probe/rollback step. → MISSING.

---

## 4. CONFLICTS (doc-vs-doc / doc-vs-reality — recorded, not silently resolved)
- **C-1 (naming):** docs `vm-*`/`vantagemind.com` vs code `luciel-*`/`luciel-mail.com`. Needs a one-line ruling (§0.3).
- **C-2 (§3.7.2b table names):** `transcripts`/`session_summaries` named in arch don't exist as tables; data lives in `conversations`+`messages` and `leads.summary`. Doc-vs-code naming conflict.
- **C-3 (§4.5 resilience claim):** arch says budget counter "reads from Postgres source of truth" on Redis-down; code has no Postgres live counter — Redis failure is fail-open (uncapped), `conversation_overage_ledger` is a closed-period ledger. Doc overstates the implemented resilience.
- **C-4 (loop order):** §3.4.1 diagram = BUDGET→GATE1→RETRIEVE; code = RETRIEVE→GATE1→BUDGET. Functional but wastes retrieval on capped/escalated sessions; code comment doesn't acknowledge the divergence.
- **C-5 (staging):** §4.3 declares dev→staging→prod; no staging exists (§0.2).

---

## 5. §9 AUTHORED COMMITMENTS — implementation status ledger
(The task requires tracking every §9 item the code touches, so the docs can be ratified to match what ships.)
- **#1 ca-central-1 region:** declared in all stacks (region-pinned) but enforcement guardrails missing (§3). Effectively implemented-as-default.
- **#12 BYO timeout 10s:** code is **30s** → will conform to 10s in execution.
- **#13 BYO circuit breaker 5 failures:** CONFORMS (FAILURE_THRESHOLD=5).
- **#14 audit hash-chain SHA-256:** CONFORMS.
- **#21/#22/#23 grounding floors 0.45/0.50/0.55:** code is **flat 0.50** → will conform to per-tier floors.
- **#24/#33 human-controlled session budget rule:** session=1-budget-unit CONFORMS; `human_controlled` mode itself MISSING.
- **#25 self-serve export tiering / #26 export ZIP format:** export exists but DRIFTED (tar.gz/JSONL, Free gate not enforced) → conform in execution.
- **#15 Enterprise S3 WORM audit export / #17 72h breach notify / #18 6mo API-deprecation / #27 no-standing-access / #28 break-glass-notify / #30 connector-deprecation notice / #29 SMS honesty:** policy/infra-level; mostly not code-implemented (break-glass §5.11 is policy-only). Will record each touched item with value-found vs authored-value in the final reconciliation report.

---

## 6. BLOCKED-EXTERNAL (cannot self-provision; need founder)
1. **AWS control-plane read access** (ECS/RDS/CFN/ECR/IAM/Secrets) — required to reconcile *deployed* state vs branch/build/migration. Need: scoped AWS CLI creds (read-heavy). Until then, all live-state in-sync checks are unverifiable.
2. **Production Stripe dashboard** — verify `invoice.paid` webhook config + metered Price/Meter IDs (`STRIPE_PRICE_OVERAGE_*`, `STRIPE_METER_EVENT_OVERAGE` are empty in `.env.example`; founder provisions).
3. **Live SSM/Secrets values** — `default_llm_provider`, Twilio/SES creds, secret-path prefix actually used by prod IAM (decides whether the `luciel/` vs `vm/` path is a live break).
4. **`record_source_live_enabled` S3 IAM** — S3 record-source read path correctness needs a live bucket+IAM at deploy.

---

## 7. PROPOSED EXECUTION SEQUENCE (post-approval, staging-only)
0. **Stand up isolated staging** (tagged `env=staging`, separate cluster/RDS/buckets) — precondition for all infra work; nothing mutating touches prod.
1. **TIER A bugs** (BUG-1 legal cascade first; then 2/3/4) — each fixed end-to-end (backend+frontend+test) and verified.
2. **TIER B security drift** (custom-role approval workflow; connections schema via expand-contract migration; secret-path pending C-1 ruling).
3. **TIER C builds** (handoff mode; escalation delivery §3.5; grounding citation-overlap+phrase+per-tier floors; domain-agnostic weighted lead heuristic; per-tier LLM classes+intra-tier routing; graph KB + rerank).
4. **TIER D/E drift** (5-state lifecycle + full cascade; widget token-bucket/auto-block; per-tier retention + cold archive; export ZIP; SLA/graph entitlement values; RBAC vocab; channels-dropdown UI; deferred connection beats).
5. **TIER F residue removal** (with per-item dependency check; keep the 3 live TDs + ARC reports).
6. **Infra/CI** (core CFN stacks for the new staging env; ca-central-1 guardrails; §5.1 alarm set; §5.5 smoke probes + rollback gate; doctrine-anchored §8 path normalization + PR-template enforcement).
7. **Verify in staging** (run the 182-test suite + targeted new tests + smoke probes); produce reconciliation report + §9 ledger; **present deploy-readiness summary and WAIT for explicit prod go/no-go.**

---

## 8. WHAT I NEED FROM YOU AT THIS GATE
1. **Approve / amend this manifest** (the contract for execution).
2. **Naming ruling (C-1):** is `luciel-*`/`luciel-mail.com`/`luciel/...` canonical (I amend docs), or do I rename infra to `vm-*` (larger, touches IAM)? Recommend: `luciel-*` canonical, amend docs — far less blast radius.
3. **AWS read creds** (BLOCKED-EXTERNAL #1) when convenient — unblocks live-state reconciliation. Not required to start TIER A–F code/schema work in staging-as-built.
4. Confirm the staging-first sequencing (§7 step 0) is the intended reading of "STAGING ONLY."
