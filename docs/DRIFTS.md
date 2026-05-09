# Luciel — Drifts (Open and Resolved)

**What this document is:** The single ledger of every gap between the design (`ARCHITECTURE.md` + `CANONICAL_RECAP.md`) and the implementation (this repository, and the running production environment in AWS `ca-central-1`). Each gap is a token with a stable id, a status, and a resolution path.

**What this document is not:** A changelog. A version-history dump. A historical narrative. Closed drifts stay in the doc with a strikethrough so the audit chain of decisions remains walkable, but the document does not accumulate sediment beyond that.

**Maintenance protocol:** Surgical edits only. New drifts are appended with a unique id. Closed drifts get the closing commit/tag noted and the heading wrapped in strikethrough (`~~`). Reopened drifts get a new id; the original stays closed.

**Last updated:** 2026-05-09

---

## Section 1 — Status legend and id scheme

**Status values:**

- `OPEN` — gap exists; no fix in flight
- `IN-PROGRESS` — fix is in flight on a named branch or PR
- `RESOLVED` — fix has landed on `main`; closing commit/tag noted; heading wrapped in `~~strikethrough~~`
- `DEFERRED` — gap acknowledged, deliberate decision to not fix this cycle; rationale recorded inline
- `WONTFIX` — gap re-examined and judged not a drift (design changed, or claim was wrong); rationale recorded inline

**Implementation marker scheme** (used in `ARCHITECTURE.md` and `CANONICAL_RECAP.md` once Phase 2 reconciliation begins applying them):

- ✅ Implemented — repo and prod both match the design
- 🔧 Partial — repo or prod implements some of the design; gaps tracked as drifts here
- 📋 Planned — design committed, no implementation yet; tracked as drift here
- 🔬 Decision-gate — design says "we will choose later"; not a drift, an open product decision

**Drift id scheme:** `D-<short-slug>-YYYY-MM-DD`. The date is when the drift was first opened, not when it was discovered later. Slug is short, hyphenated, and stable — never rename a slug after creation.

**Source of truth:** When this document, the architecture document, the canonical recap, the repository, or production disagree, the resolution is recorded here as a drift. The four artifacts converge by closing drifts, not by editing one and hoping the others catch up.

---

## Section 2 — Phase 2A repo reconciliation: how this register was built

This document was rebuilt on 2026-05-09 by walking `ARCHITECTURE.md` and `CANONICAL_RECAP.md` section by section against the repository at commit `c3ed8b6` on `main`. The previous DRIFTS.md (a partial, history-shaped register) was archived to `docs/archive/DRIFTS_pre-reconciliation.md` rather than discarded; that file remains the historical record up to that point.

The comparison covered:

- Architecture §2 — Development environment
- Architecture §3 — Production environment (every subsection)
- Architecture §4 — Cross-cutting properties
- Architecture §5 — Conceptual model
- Canonical Recap §1 — The two layers (Soul and Profession)
- Canonical Recap §2 — Six components
- Canonical Recap §3 — Cognitive abilities
- Canonical Recap §4 — Behavior contracts
- Canonical Recap §6 — Operating loop
- Canonical Recap §10 — Fixed-vs-configurable matrix
- Canonical Recap §11 — Eight strategic answers
- Canonical Recap §13 — End-to-end testing scenarios

Every drift below is tagged with the section that raised it. Production-only drifts (where the repo matches the design but production does not, or where we cannot tell from the repo alone) are tagged `[PROD-PHASE-2B]` and will be confirmed during Phase 2B with the operator at the PowerShell.

---

## Section 3 — Open drifts (repository scope)

### D-channels-only-chat-implemented-2026-05-09

**Status:** OPEN — tracked against roadmap Step 34a
**Raised by:** Architecture §3.2.1, §3.3 step 1; Canonical Recap §13 T8
**Design claim:** Customers reach Luciel through chat widget, voice, email, SMS, and a programmatic API. A channel adapter normalizes each channel into the same internal format.
**Repo state:** Only the chat / programmatic API surface exists (`app/api/v1/chat.py`, sessions, etc.). There is no voice gateway, no email gateway, no SMS gateway, and no channel-adapter layer. `RuntimeRequest.channel` exists as a string field but no dispatch logic differentiates by it.
**Owning roadmap step:** Step 34a (Channel adapter framework — SMS, voice, email, governed by the same scope policy as the chat widget). This drift closes when Step 34a closes.
**Doc-truthing this pass:** Architecture §3.2.1, §3.3 step 1, §3.6 receive 📋 markers pointing to Step 34a. Recap §13 T8 receives a footnote that the multi-channel scenario currently demonstrates chat + programmatic only. No code change.

### D-external-integrations-llm-only-2026-05-09

**Status:** OPEN — tracked against roadmap Step 34
**Raised by:** Architecture §2.2 ("calendar, CRM, email, SMS, voice, payments") and §3.2 (production tools); Canonical Recap §2 (six components: tools), §6 (operating loop step 5)
**Design claim:** External integrations include calendar, CRM, email, SMS, voice, and payments, sandboxed in development and real in production.
**Repo state:** `app/integrations/` contains only `llm/` (Anthropic and OpenAI clients). `app/tools/implementations/` has three internal tools (`escalate_tool`, `save_memory_tool`, `session_summary_tool`) — none of them external integrations. The tool registry shape (`app/tools/registry.py`, `app/tools/broker.py`) is in place — the gap is the integrations themselves, not the framework.
**Owning roadmap step:** Step 34 (Workflow actions — book appointments, send emails, create leads, query business systems on behalf of the user). This drift closes when Step 34 closes.
**Doc-truthing this pass:** Architecture §3.2 gets a "what integrations exist today" subsection so the doc stops implying the full slate is present. Recap §2 (tools component) and Recap §6 step 5 receive 🔧 markers pointing to Step 34.

### D-confirmation-gate-not-enforced-2026-05-09

**Status:** OPEN — tracked against new roadmap Step 30c (action classification, tiered)
**Raised by:** Architecture §3.3 step 8 ("Policy gate"), §4.9 (synchronous-only invocation rejected); Canonical Recap §4 (behavior contracts: "Luciel does not take consequential action without permission")
**Design claim (revised in this pass):** Tool invocations are classified into three tiers: **routine** (just do it, audited), **notify-and-proceed** (execute and surface visibly to the customer), and **approval-required** (return a confirmation request before executing). Approval is required only when an action is genuinely consequential — irreversible, high-blast-radius, off-pattern relative to the customer's established usage, or where Luciel itself is uncertain. Routine and reversible external-facing actions are not gated, because gating them would make Luciel feel timid and bureaucratic and would violate the senior-advisor voice in Recap §3.
**Repo state:** `app/policy/` does not contain an action-classification module. None of the three tiers exist. No `consequential_action` predicate, no off-pattern detector, no confidence-threshold check, no confirmation-pending state on sessions or messages, no model-side instruction in `LUCIEL_SYSTEM_PROMPT` describing the tiered contract. The escalate tool is the closest existing mechanism but it is voluntary and unstructured.
**Owning roadmap step:** Step 30c (Action classification — tiered approval discipline). Positioned between Step 30b (embeddable chat widget) and Step 31 (dashboards + validation gate) so the tiered contract holds before the first paying customer arrives. The off-pattern tier leans on the memory layers being correct, so it has a soft dependency on the four-kinds memory architecture being usable. This drift closes when Step 30c closes.
**Doc-truthing this pass:** Architecture §3.3 step 8 rewrites from a binary gate to tier-aware classification. Architecture §4.9 softens the synchronous-only rejection to acknowledge the tier split. Recap §4 receives a one-sentence clarification of what "consequential" means (irreversible, high-blast-radius, or off-pattern — not merely external-facing). Recap §12 gets the new Step 30c line.

### D-scope-assignments-history-table-missing-2026-05-09

**Status:** RESOLVING THIS PASS — closes in commit (b) of this reconciliation
**Raised by:** Architecture §4.3 lists `scope_assignments_history` as one of three append-only tables that matter most.
**Repo state:** `app/models/scope_assignment.py` exists; the model docstring describes append-on-change discipline ("create a new one. Two rows in history, never UPDATE in place") using the same `scope_assignments` table as an append-only log. There is no separate `scope_assignments_history` table or migration anywhere in `alembic/versions/` or `app/models/`.
**Resolution:** The design is option (a) — `scope_assignments` is itself the append-only history. The architecture doc was wrong to imply a separate table. Fix Architecture §4.3 to describe the append-on-change pattern explicitly, removing the separate-table claim. Code is correct; doc was wrong.

### D-celery-task-surface-thin-2026-05-09

**Status:** OPEN — partially tracked against existing roadmap; retention purge is the load-bearing gap
**Raised by:** Architecture §3.2.3 (background workers handle "ingesting documents the customer uploaded, refreshing search indexes, sending follow-up emails, running scheduled retention purges, and similar")
**Repo state:** `app/worker/tasks/` contains only `memory_extraction.py`. Document ingestion is built but as foreground code (`app/knowledge/ingestion.py`), not a worker task. There is no search-index refresh task, no follow-up-email task, no scheduled retention-purge task. `app/policy/retention.py` defines the policy; the worker that runs it does not exist.
**Owning roadmap mapping:** Document ingestion-as-worker and follow-up emails are subsumed by Step 34 (workflow actions). **Retention purge is not currently on the roadmap** and it is the load-bearing gap because Architecture §4.4's soft-delete + scheduled-purge guarantee depends on it actually running. Without a running purge worker, soft-deleted rows accumulate forever and the storage-cost story in §4.4 ("the retention worker handles that") is fiction.
**Doc-truthing this pass:** Architecture §3.2.3 gets per-responsibility markers (✅ memory extraction, 🔧 document ingestion as foreground, 📋 the rest). The retention-purge gap is escalated to its own drift token below (`D-retention-purge-worker-missing-2026-05-09`) because it has a load-bearing guarantee depending on it.

### D-context-assembler-thin-2026-05-09

**Status:** OPEN — code-quality drift, no owning roadmap step yet
**Raised by:** Architecture §4.2 ("at runtime, both layers are composed in a single foundation-model context, with the Soul layer placed structurally so it cannot be overridden by injected Profession-layer content")
**Repo state:** `app/runtime/context_assembler.py` produces a 6-line prompt: identity + tenant id + domain id + channel + user message + a stock instruction. The actual production prompt assembly happens in `app/services/chat_service.py` and is more substantive than the assembler suggests, but is not consolidated. Specifically missing: per-scope Profession-layer loading as a coherent step, a structural Soul-vs-Profession separation that prevents prompt injection from overriding Soul rules, and an output-side policy check that catches a model attempting to violate Soul-layer rules.
**Owning roadmap mapping:** None today. This is a refactor + a small policy addition, not a feature, so it does not naturally belong to one of the existing roadmap steps. Best fit is as a prerequisite of Step 33 (evaluation framework) since evaluation needs a single observable assembly point. Defer assignment; track as a code-quality drift.
**Doc-truthing this pass:** Architecture §4.2 gets a 🔧 marker. The drift stays open as a known refactor with no scheduled date — flagged for a future roadmap-grooming pass.

### D-untracked-diag-files-persist-2026-05-09

**Status:** OPEN — sandbox-side gitignore widening attempted in commit (d) of this pass; operator-side survey deferred to Phase 2B
**Raised by:** Operator observation; not a doc claim, but a repo-hygiene drift carried forward from the previous reconciliation pass.
**Repo state:** Approximately 47 untracked diagnostic files persist in the working tree across recent sessions despite the gitignore-widening commit `cea23af`. The sandbox tree is clean (these accumulate on the operator's local clone during diagnosis sessions); we cannot survey them from here directly. We can only widen `.gitignore` for patterns we anticipate.
**Resolution path:** This pass widens `.gitignore` for any plausibly-missing patterns based on what we know operators produce (PowerShell transcripts, ad-hoc probe outputs, evidence captures). Phase 2B confirms the actual untracked set on the operator's clone and tightens further if needed.

### D-canonical-recap-q1-q8-roadmap-genericized-2026-05-08

**Status:** OPEN (self-correction, will be moved to Section 5 in commit (a) of this pass)
**Raised by:** Operator catch on 2026-05-08; the Q1-Q8 strategic answers and roadmap had been genericized in an earlier pass before being restored from `archive/CANONICAL_RECAP_v3.4.md`.
**Repo state:** Restored verbatim in commit `2903bc4` (Step 29.y postmerge, recap restore from v3.4) and re-incorporated into the product-philosophy-first rewrite in commit `e56887b`. The canonical recap on `main` at `c3ed8b6` carries the substantive answers.
**Resolution path:** Self-correction recorded for audit. Moves to Section 5 in this commit (commit (a) of the reconciliation pass).

### D-embed-key-issuance-workflow-missing-2026-05-09

**Status:** OPEN — blocks Step 30b success criterion
**Raised by:** Step 30b commit (e) end-of-build review.
**Design claim:** CANONICAL_RECAP §12 Step 30b row: "A company adds a few lines of code to their site, and within an hour their visitors are having real conversations with the company's Luciel." The within-an-hour clause requires an operator-runnable issuance path that produces an embed key with `key_kind='embed'`, `permissions=['chat']`, a non-empty `allowed_origins`, a `rate_limit_per_minute`, and a populated `widget_config`.
**Repo state:** `app/services/api_key_service.py:create_key` mints rows on the `api_keys` table but does not validate the embed-key invariants (the four columns added in commit (b) are accepted positionally as kwargs and pass through unchecked). No admin endpoint, no CLI, no runbook produces a key in the embed shape today; an operator would have to write the row by hand and read the raw key out of the create_key return value, which is not a workflow we can hand to ourselves let alone a customer.
**Operational impact:** The widget bundle and the gate are both green, but no one can issue the credential they validate. First paying customer cannot self-serve, and the operator's manual path is fragile (hand-rolled SQL, no audit row, no SSM write).
**Owning roadmap step:** Step 30b. Closes when the issuance path lands and an operator can mint an embed key in under five minutes against any tenant. Likely shape: extend `app/api/v1/admin.py` with `POST /api/v1/admin/embed-keys` that wraps `create_key` with the embed-shape validation + writes the four widget columns, and a small CLI (`scripts/mint-embed-key.py`) for operator use before the admin UI exists.
**Pattern E:** New rows only; no mutation of existing keys. Audit emission via the same `AdminAuditRepository.record` path the rest of `api_key_service` uses.

---

### D-retention-purge-worker-missing-2026-05-09

**Status:** OPEN — load-bearing; needs a roadmap home
**Raised by:** Split out from `D-celery-task-surface-thin-2026-05-09` because it has a load-bearing architectural guarantee depending on it.
**Design claim:** Architecture §4.4 ("The retention worker handles that, in batches sized to coexist with live traffic without lock contention"); Architecture §3.2.3 lists "running scheduled retention purges" as a worker responsibility; Architecture §3.4 commits "a retention purge cannot break the audit chain" which presumes a purge worker exists.
**Repo state:** `app/policy/retention.py` defines the retention policy. There is no scheduled task in `app/worker/tasks/` that runs the purge. Soft-deleted rows accumulate without bound today.
**Operational impact:** Storage cost grows with every soft-delete and never shrinks. Customers cannot be told "data is purged after the contracted retention period" with a straight face until this lands. Compliance posture is also weaker than the architecture claims.
**Owning roadmap step:** None today. Recommend adding to roadmap (this pass does not unilaterally add it because retention purge is enough work — schedule, idempotency, lock semantics, audit emission to `deletion_logs`, end-to-end test — that it deserves its own line and operator buy-in before scheduling). For now, flagged here as a known gap requiring a near-term roadmap decision.
**Doc-truthing this pass:** Architecture §3.2.3 and §4.4 receive 📋 markers; the prose stays unchanged because the design is correct, the implementation is missing.

---

## Section 4 — Drifts pending production confirmation [PROD-PHASE-2B]

These are claims in `ARCHITECTURE.md` whose verification requires operator-side access to the AWS account. Each will be walked together with the operator step by step in Phase 2B. The repository alone cannot confirm or deny any of them.

### D-prod-waf-presence-unverified-2026-05-09

**Status:** OPEN [PROD-PHASE-2B]
**Raised by:** Architecture §3.2.1 and §3.6 diagram (WAF in front of ALB)
**Repo state:** No CFN template in `cfn/` provisions a WAF Web ACL. No infra-as-code claim of a WAF.
**Resolution path:** Operator confirms whether a WAF Web ACL is associated with the production ALB. If yes, capture its arn and the rule set. If no, mark §3.2.1's WAF claim as 📋 Planned and open a roadmap item.

### D-prod-widget-bundle-cdn-unprovisioned-2026-05-09

**Status:** OPEN [PROD-PHASE-2B] — blocks Step 30b success criterion
**Raised by:** Step 30b commit (e) end-of-build review.
**Design claim:** CANONICAL_RECAP §12 Step 30b row: customers add "a few lines of code to their site" and the widget loads. That requires the bundle to be reachable from any customer origin under a stable, cacheable URL.
**Repo state:** The widget bundle is built on every push by the `widget-build-and-size` CI job (commit d) and ships at 9.2 KB gzipped, but it is published nowhere. There is no S3 bucket, no CloudFront distribution, no infrastructure-as-code claim of either, and no deploy step in CI that uploads `widget/dist/luciel-chat-widget.js` to a CDN.
**Resolution path (Phase 2B with operator):**
  1. Provision an S3 bucket (private, with CloudFront OAC). Bucket name follows the existing `cfn/` naming convention if one exists; otherwise pick `luciel-widget-cdn-prod-<region>`.
  2. Provision a CloudFront distribution serving from the bucket with a long max-age on the hashed bundle filename and a short max-age on a stable `widget.js` alias if we want versionless URLs.
  3. Add a CI job (gated to `main`, after the size job passes) that uploads `widget/dist/luciel-chat-widget.js` plus its `.map` to the bucket on every merge.
  4. Document the public URL in `docs/CANONICAL_RECAP.md` Step 30b row only after the URL is reachable from outside our network.
**Pattern E:** No row mutations. CDN artifacts are forward-only; old hashed paths stay reachable for any customer that pinned a specific version.

### D-prod-multi-az-rds-unverified-2026-05-09

**Status:** OPEN [PROD-PHASE-2B]
**Raised by:** Architecture §3.2.4 ("hot standby in a second availability zone"), §3.6 diagram (PG_PRIMARY ↔ PG_STANDBY)
**Repo state:** No CFN template provisions the RDS instance. The repo cannot confirm Multi-AZ.
**Resolution path:** Operator runs `aws rds describe-db-instances` (read-only) and confirms `MultiAZ: true` and the standby AZ. If single-AZ, mark §3.2.4's standby claim as 📋 Planned.

### D-prod-app-tier-pool-unverified-2026-05-09

**Status:** OPEN [PROD-PHASE-2B]
**Raised by:** Architecture §3.2.2 ("a pool of application servers"), §3.5 ("application server crash" recovery story)
**Repo state:** Task definitions exist (`td-backend-rev34.json`) but desired-count and minimum-pool size are an ECS service property, not a task-def property.
**Resolution path:** Operator confirms desired count and minimum healthy count of the backend ECS service. If desired count is 1, mark §3.2.2's pool claim as 🔧 Partial and open a roadmap item to raise the floor.

### D-prod-worker-autoscaling-unverified-2026-05-09

**Status:** OPEN [PROD-PHASE-2B]
**Raised by:** Architecture §3.2.3 ("worker autoscaling on queue depth and CPU")
**Repo state:** `cfn/luciel-prod-worker-autoscaling.yaml` exists. Need to confirm it is deployed and active in the prod account.
**Resolution path:** Operator confirms the worker-autoscaling stack is in `CREATE_COMPLETE` or `UPDATE_COMPLETE` state and the scaling policies are attached to the worker service.

### D-prod-alarms-deployed-unverified-2026-05-09

**Status:** OPEN [PROD-PHASE-2B]
**Raised by:** Architecture §3.2.8 (four signal kinds, alarms thresholds: 1% error, 1000 queue depth, 80% DB connection saturation)
**Repo state:** `cfn/luciel-prod-alarms.yaml` exists. Need to confirm deployment.
**Resolution path:** Operator confirms the alarms stack is deployed and the alarms are in `OK` state (not `INSUFFICIENT_DATA`).

### D-prod-kms-customer-managed-unverified-2026-05-09

**Status:** OPEN [PROD-PHASE-2B]
**Raised by:** Architecture §3.2.7 ("encrypted with a customer-managed KMS key")
**Repo state:** Verify task role (`cfn/luciel-verify-task-role.yaml`) references KMS Decrypt via SSM service principal but does not name the key. Cannot confirm whether the key in use is AWS-managed (`aws/ssm`) or customer-managed.
**Resolution path:** Operator runs `aws ssm describe-parameters` for one of the SecureString parameters, captures the `KeyId`, then `aws kms describe-key` on it. Confirm `KeyManager: CUSTOMER`. If `AWS`, mark as 🔧 Partial — works the same operationally but rotation control is AWS's, not ours.

### D-prod-secrets-pattern-e-unverified-2026-05-09

**Status:** OPEN [PROD-PHASE-2B]
**Raised by:** Architecture §3.2.7 ("Pattern E: secrets are deactivated, never deleted")
**Repo state:** SSM Parameter Store does not natively support Pattern E (deactivated flag) — it supports versioning. The current implementation pattern needs documentation.
**Resolution path:** Operator inventories SSM parameters with `aws ssm describe-parameters` and confirms the operational pattern: are old versions retained (relying on SSM history) or are deactivated rotated parameters retained as separate parameter names with a `-deactivated-YYYY-MM-DD` suffix? The doc should reflect what we actually do; right now §3.2.7 implies a discipline the implementation may not have.

### D-prod-celery-broker-sqs-vs-redis-2026-05-09

**Status:** OPEN [PROD-PHASE-2B]
**Raised by:** Architecture §3.6 diagram (`Job queue Redis/SQS`); §3.2.3 prose does not name SQS.
**Repo state:** `app/worker/celery_app.py` lines 116-138 explicitly document a two-mode broker design: SQS in production, Redis in development. Production task definitions inject `REDIS_URL` from SSM. The architecture document does not commit clearly to SQS-in-prod.
**Resolution path:** Update Architecture §3.2.3 to state SQS as the production broker explicitly, with the rationale already in `app/worker/celery_app.py` lines 132-138 (ElastiCache cluster mode incompatible with kombu's MULTI/EXEC). Confirm with operator that production is in fact running SQS-mode (not Redis broker via SSM `REDIS_URL`). If REDIS_URL is the actual broker, the architecture is wrong; otherwise the architecture is incomplete and we tighten it.

### D-prod-sqs-stale-messages-2026-05-09

**Status:** OPEN [PROD-PHASE-2B] (operator-flagged, symptom description pending)
**Raised by:** Operator on 2026-05-09 ("SQS stale messages drift"); not yet captured in either design doc.
**Repo state:** Pending operator description.
**Resolution path:** Operator describes the symptom — what is stale, how it manifests, what was observed in CloudWatch or in queue inspection. Drift will be expanded with the description, then we decide whether the cause is a design gap (e.g., visibility timeout vs job duration), a code bug (idempotency or ack handling), or an operational incident (poison messages stuck redelivering).

### D-memory-truncation-2026-05-09

**Status:** OPEN [PROD-PHASE-2B] (operator-flagged, symptom description pending)
**Raised by:** Operator on 2026-05-09 ("memory truncation drift"); possibly distinct from the audit-note 256-char cap (test exists in `tests/integrity/test_audit_note_length_cap.py`).
**Repo state:** Pending operator description.
**Resolution path:** Operator describes which memory is being truncated — session, user preference, domain, or operational — and at what boundary (storage column length, retrieval window, model context). Drift will be expanded with the description, then we decide whether the truncation is a column-length cap, a retrieval-window heuristic, or a model-context budget cap.

---

## Section 5 — Resolved drifts (kept for audit)

Closed drifts are kept here permanently with a strikethrough heading. The closing commit, the closing tag (if applicable), and a one-line resolution note are recorded. This makes the audit chain of design decisions walkable backwards; nothing is rewritten or deleted.

### ~~D-step29y-impl-no-close-tag-2026-05-07~~

**Status:** RESOLVED on 2026-05-08
**Closing tag:** `step-29y-complete` on commit `5f297b7`
**Resolution:** Step 29.y implementation work was tagged `step-29y-complete`, providing a stable anchor for "all step-29.y code merged."

### ~~D-canonical-recap-v3.4-omits-step-29x-29y-2026-05-07~~

**Status:** RESOLVED on 2026-05-08
**Closing commit:** `3e8678d` (Step 29.y close-out C34/C35: 3-doc regime adoption + archive of legacy docs)
**Resolution:** The legacy `CANONICAL_RECAP_v3.4.md` (which omitted Step 29.x and 29.y) was archived to `docs/archive/CANONICAL_RECAP_v3.4.md`. The new `CANONICAL_RECAP.md` is the single living canonical recap going forward, written product-philosophy-first per the user's revised framing.

### ~~D-canonical-recap-q1-q8-roadmap-genericized-2026-05-08~~

**Status:** RESOLVED on 2026-05-08; entry retained for audit
**Closing commits:** `2903bc4` (verbatim restore from v3.4) → `e56887b` (rewrite with Q1-Q8 + roadmap intact, "How we'll know" columns filled in business-observable language) → merged to main as `08a830e`
**Resolution:** A pre-restore version of the recap had genericized the eight strategic answers (Q1-Q8) and the roadmap. Operator caught it; restore + targeted rewrite preserved the substantive content while reframing for product-philosophy-first.

### ~~D-untracked-diag-artifacts-no-gitignore-2026-05-08~~

**Status:** RESOLVED on 2026-05-08 (initial pass)
**Closing commit:** `cea23af` (Step 29.y gap-fix C33a: repo cleanup, delete 37 stale operational JSONs and extend .gitignore for diag artifacts)
**Resolution:** Initial sweep of stale operational JSONs and .gitignore widening landed. **Note:** A second wave of untracked diagnostic files has appeared since (see open drift `D-untracked-diag-files-persist-2026-05-09`); the original closure remains valid for the patterns it covered, and the new patterns are tracked separately rather than as a re-open of the same id.

### ~~D-pattern-e-architecture-doc-missing-2026-05-08~~

**Status:** RESOLVED on 2026-05-09
**Closing commit:** `bf7dff4` (Architecture rewrite as design-target document)
**Resolution:** Pattern E (deactivate, never delete) is now an explicit cross-cutting property in Architecture §4.6, with the rationale, and is also referenced in §3.2.7 (secrets) and §4.5 (cascade-correct departure). The implementation pattern in production for SSM specifically is still tracked as open (`D-prod-secrets-pattern-e-unverified-2026-05-09`) until verified.

### ~~D-architecture-doc-implementation-snapshot-not-design-2026-05-08~~

**Status:** RESOLVED on 2026-05-09
**Closing commits:** `bf7dff4` (Architecture rewrite as design-target) → merged to main as `c3ed8b6`
**Resolution:** Architecture is now explicitly a design-target document, with an explicit reconciliation protocol (this DRIFTS.md). The previous implementation-snapshot framing (which conflated "what we built" and "what we intend to build") is gone. Marker scheme adopted but not yet applied — that is Phase 2 work and is the work this document tracks.

### ~~D-tenant-language-vs-scope-language-2026-05-09~~

**Status:** RESOLVED on 2026-05-09
**Closing commit:** `bf7dff4` (architecture rewrite, terminology sweep)
**Resolution:** Operator caught isolation/enforcement claims framed at the tenant level when the actual guarantee is at the scope level (any of tenant, domain, agent, or instance). 7-edit terminology sweep applied: scope language used for isolation guarantees; tenant/domain/agent/instance reserved for cases where the specific scope level genuinely matters (per-tenant DB tier, named memory kinds, hierarchy diagrams).

### Step 29.z — Repo-vs-Design audit (no findings)

**Status:** AUDIT COMPLETE on 2026-05-09; no fix-here drifts surfaced.
**Audit scope:** Treat `CANONICAL_RECAP.md`, `ARCHITECTURE.md`, and `DRIFTS.md` as ground truth; walk the repo top-down; for any divergence, classify as fix-here, new drift, or doc edit. Sandbox-only, no behavior changes, no AWS, no production touched.
**What was checked:** repo top-level layout; `app/` mapped to the six-component model (Persona, Runtime, Memory, Tool, Policy, Observability) plus the four cross-cutting concerns (scope isolation, audit chain, Pattern E, retention); 25 verification pillars file presence; alembic migration count and recency; tenant-vs-scope terminology footprint (113 files, ~2000 occurrences); root-level task-definition manifests; runtime stub depth.
**Result:** Zero material code-vs-doc misalignments that warrant a repo fix in this branch. Every gap that surfaced during the audit was already an open drift in Section 3 (`D-context-assembler-thin`, `D-celery-task-surface-thin`, `D-retention-purge-worker-missing`, `D-confirmation-gate-not-enforced`, `D-channels-only-chat-implemented`, `D-external-integrations-llm-only`) and is owned by an existing roadmap step (30c, 34, 34a). Cosmetic items deliberately not pursued: pillar filename `cross_tenant_*` vs doc concept `scope_leak` (renaming would touch the verification entry points without changing behavior); root-level `td-*.json` task-def manifests (intentionally tracked, referenced by deployment runbooks). The `tenant` term is preserved throughout the codebase as the legitimate top-of-hierarchy scope-level name per the resolution of `D-tenant-language-vs-scope-language-2026-05-09`.
**Why this entry exists:** A no-findings audit is itself a finding worth recording — it certifies that on this date, with these three docs as ground truth, the repo was aligned. Future drift hunts can anchor against this baseline. The `step-29z-repo-reconciliation` branch was cut and abandoned without commits because there was nothing to ship.
**Closing commit:** this commit.

---

## Section 6 — How drifts are added, closed, or reopened

**Adding a drift.** Append to Section 3 (open) or Section 4 (production-pending), with a fresh `D-<slug>-YYYY-MM-DD` id, a status, the section that raised it, the design claim, the repo or prod state, and a resolution path. Do not add to closed-drift section while the drift is still open.

**Closing a drift.** Move the entry verbatim to Section 5, wrap the heading in `~~strikethrough~~`, fill in the closing commit (and tag if relevant) and a one-line resolution. Do not delete the entry from Section 3 — move it. The audit chain depends on the body of each drift remaining readable after closure.

**Reopening a drift.** Open a new drift with a fresh id and a date suffix matching the day of reopening. Reference the closed predecessor in the body. The closed predecessor stays closed; reopening is a new story.

**Marker propagation.** As drifts close, the corresponding section in `ARCHITECTURE.md` or `CANONICAL_RECAP.md` gets the appropriate ✅ / 🔧 / 📋 / 🔬 marker added in surgical edits. The marker is the connective tissue between the design docs and this register; the doc edit and the drift closure are part of the same commit.

**What does NOT belong in the design docs.** The three canonical docs (`CANONICAL_RECAP.md`, `ARCHITECTURE.md`, `DRIFTS.md`) are stable references, not running notebooks. Build-time detail — framework choice, library selection, schema column lists, transport-protocol rationale, branding-knob inventories, sandbox demo plans — belongs in **commit messages** and **code-level docstrings**, not in the canonical docs. The canonical docs grow only when the architectural shape changes: a new cross-cutting property, a new component, a deletion of something that is no longer true, or a roadmap step landing such that its success criterion is provably met. A new roadmap step, on its own, is not a reason to grow the docs — the recap row already exists, and implementation detail is recoverable from `git log` and the code itself. If a build genuinely needs a multi-day shared scratchpad, it lives in `docs/in-flight/<slug>.md` during the build and is **deleted** (not archived) on merge. This principle exists because docs that grow with every roadmap step become unparsable; the discipline of keeping them stable is what makes them useful as references.

---

## Section 7 — Source-of-truth rule

If a chat summary, a session recap, a slide, an email, or any other artifact contradicts this document about the status of a drift, **this document wins**. Update the document; do not let the contradicting artifact stand.
