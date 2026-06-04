# Manifest Section 02 — Runtime Orchestrator, Agentic Loop, Cognition, Escalation, LLM Routing, Grounding, Handoff, Memory/Sessions

**Auditor scope:** Runtime orchestrator, agentic loop, cognition, escalation judgment, channel arbitration, LLM routing, grounding/anti-hallucination, human handoff, memory/sessions.

**Source of truth read:**
- `/home/user/workspace/docs_text/ARCHITECTURE.txt` — §3.4.1, §3.4.1b, §3.4.2, §3.4.3, §3.4.5, §3.4.6, §3.4.7, §3.4.8, §3.4.9, §3.4.12, §3.4.13, §8, §9
- `/home/user/workspace/docs_text/VISION.txt` — §2, §3.4, §4.1, §5, §7
- `/home/user/workspace/luciel_repos/backend/ARC15_BACKEND_REPORT.md`, `ARC15_DRIFT_CLEANUP_REPORT.md`, `ARC17_LOOKUP_RECORD_AMENDMENT.md`

**Code read:** `app/runtime/orchestrator.py`, `escalation_judge.py`, `channel_arbiter.py`, `classifiers.py`, `plan_parser.py`, `context_assembler.py`, `budget_meter.py`, `budget_ack.py`, `handoff_ack.py`, `billing_period.py`; `app/integrations/llm/router.py`; `app/cognition/finalizer.py`, `lead_capture.py`, `summarizer.py`; `app/memory/service.py`, `cross_session_retriever.py`; `app/identity/resolver.py`; `app/models/session.py`; `app/policy/entitlements.py`, `escalation.py`, `escalation_config.py`.

---

## Audit Table

| # | Requirement | Doc cite | Implementing artifact(s) | Status | Notes / evidence |
|---|---|---|---|---|---|
| 1 | Loop step order: RECEIVE → BUDGET GATE → ESCALATION GATE 1 → RETRIEVE → PLAN/ACT/REFLECT → ESCALATION GATE 2 → RESPOND → COGNITION FINALIZATION | Architecture §3.4.1 (loop diagram) | `orchestrator.py:128–214` | DRIFTED | **Ordering deviation:** code runs RETRIEVE before both gates. Actual order: RECEIVE → RETRIEVE → ESCALATION GATE 1 → BUDGET GATE → PLAN. The doc specifies BUDGET GATE first, then ESCALATION GATE 1, then RETRIEVE. The code justifies the budget gate position (comment line 156: "LOAD-BEARING: AFTER intake Gate 1") but transposes RETRIEVE to before both gates. Consequence: retrieval cost is incurred even on sessions that will be budget-capped or intake-escalated. |
| 2 | PLAN/ACT/REFLECT bounded at EXACTLY 5 iterations on ALL tiers | Architecture §3.4.1; Locked Decision #17 | `orchestrator.py:71` (`MAX_LOOP_ITERATIONS = 5`); `orchestrator.py:836–882` (`_run_plan_act_reflect`) | CONFORMS | `MAX_LOOP_ITERATIONS = 5` is a module-level constant. Loop iterates `range(1, MAX_LOOP_ITERATIONS + 1)`. Hitting the bound is recorded on `result.bound_hit` but explicitly never surfaced as escalation (`orchestrator.py:181`: "NEVER reads loop.bound_hit"). |
| 3 | TWO escalation gates: Gate 1 INTAKE (pre-PLAN; explicit_human_request, strong_negative_sentiment) and Gate 2 OUTCOME (post-REFLECT; cannot_answer, high_value_lead) | Architecture §3.4.1, §3.4.5 | `orchestrator.py:149–183`; `escalation_judge.py:117–302` | CONFORMS | `_intake_gate` calls `judge.evaluate_intake` (signals a+b). `_outcome_gate` calls `judge.evaluate_outcome` (signals c+d). Two-gate split is explicit and correct. Gate 1 skips PLAN; Gate 2 fires post-REFLECT and still reaches RESPOND/COGNITION. |
| 4 | Explicit human request: `intent_class == "request_human"` AND `confidence >= 0.85` | Architecture §3.4.5 (signal table) | `escalation_judge.py:56–57` (`INTENT_CONFIDENCE_THRESHOLD = 0.85`) | CONFORMS | Constant 0.85 is pinned. Judge checks `result.intent_class == INTENT_REQUEST_HUMAN and result.confidence >= INTENT_CONFIDENCE_THRESHOLD`. |
| 5 | Strong negative sentiment: score `<= -0.7` over `>= 2` of trailing 3 messages | Architecture §3.4.5 | `escalation_judge.py:58–60` (`SENTIMENT_NEGATIVE_THRESHOLD = -0.7`, `SENTIMENT_POLARITY_WINDOW = 3`, `SENTIMENT_POLARITY_MIN_CONSISTENT = 2`) | CONFORMS | All three thresholds are pinned constants. Window + min_consistent logic is correct. |
| 6 | Cannot-confidently-answer: loop confidence `< 0.6` AND grounding score below per-tier floor | Architecture §3.4.5 | `escalation_judge.py:63` (`LOW_CONFIDENCE_THRESHOLD = 0.6`); `escalation_judge.py:230–266` | CONFORMS | Both conditions checked. Retrieval failure treated as grounding 0.0 (below every floor). Judge never reads `loop.bound_hit`. |
| 7 | Per-tier grounding floors: Free 0.45 / Pro 0.50 / Enterprise 0.55 | Architecture §3.4.13 (floor table); §9 items 21-23 (AUTHORED — pending ratification) | `escalation_judge.py:74–77` | DRIFTED | Code sets ALL tiers to 0.50 (`"free": 0.5, "pro": 0.5, "enterprise": 0.5`). The doc specifies Free 0.45, Pro 0.50, Enterprise 0.55. Code comment acknowledges: "The floor is FLAT at v1 (0.5 for every tier)." This flattening deviates from the authored values in §9 items 21/23. These are AUTHORED (not ratified), but the code implements a different set of authored values. |
| 8 | High-value lead heuristic: weighted score (budget 0.5, time-constrained decision 0.3, purchase/booking intent 0.4), capped 1.0 | Architecture §3.4.5 (signal table) | `escalation_judge.py:272–297`; `cognition/lead_capture.py` | DRIFTED | Code uses a binary threshold (`lead_value >= FREE_LEAD_BUDGET_THRESHOLD` where threshold = 750,000). No weighted composite scoring is implemented. The spec's weighted heuristic (budget 0.5/time 0.3/intent 0.4, capped 1.0) producing a normalized lead-score is absent. The code only checks extracted budget figure. Also: `lead_capture.py` triggers are real-estate-specific (`_LISTING_REF_RE` with "street/avenue/road/drive/boulevard", "MLS #", etc.) despite Architecture §3.4.5 note: "domain-agnostic, does not assume real estate." |
| 9 | No admin-configurable escalation triggers | Architecture §3.4.5; ARC15 report; Vision §3.4 | `escalation_config.py:1–55` (`ESCALATION_SIGNALS` frozenset); ARC15 report: "No escalation-trigger configuration is accepted" | CONFORMS | `escalation_config.py` validates the signal vocabulary as a closed frozenset and explicitly rejects any payload attempting to set thresholds, enable/disable signals, or add new signals. Four signals are fixed in `escalation_judge.py` as constants. |
| 10 | Budget counter keyed `(admin_id, instance_id, billing_period_start)`, Redis, increment once per session at first LLM call, idempotent across REFLECT iterations | Architecture §3.4.1b | `budget_meter.py:63` (`_counter_key`); `budget_meter.py:173–197` (`count_session_once` with SETNX idempotency) | CONFORMS | Key structure matches spec. SETNX per-session marker ensures single increment. 70-day TTL. |
| 11 | Budget reset on Stripe `invoice.paid` / `subscription.renewed`, NOT calendar month | Architecture §3.4.1b | `billing_period.py:1–34` (doc comment); `budget_meter.py:199–204` (`reset` method) | CONFORMS | `billing_period.py` explicitly documents Stripe webhook as reset trigger for paying tiers. `free_period_start` uses signup-day-anchored monthly window for Free (no Stripe cycle). |
| 12 | No rollover; per-instance capacity | Architecture §3.4.1b | `billing_period.py`; `budget_meter.py` | CONFORMS | New billing period uses a new `period_start` key — old key is deleted, not rolled forward. Counter is per `(admin_id, instance_id, period_start)`. |
| 13 | Free at cap: NO LLM call, graceful acknowledgement, escalate | Architecture §3.4.1b (tier table); Locked Decision #13 | `orchestrator.py:534–542` (budget gate check); `orchestrator.py:628–721` (`_finalize_budget_escalation`); `budget_ack.py` | CONFORMS | `ctx.tier == TIER_FREE and count > cap` → `_finalize_budget_escalation`. `llm_provider=None, llm_model=None` in the response. `budget_exhausted_acknowledgement()` returns a templated reply. No `ModelRouter.generate` call on the budget short-circuit path. |
| 14 | Pro monthly 2000 / annual 2500 / Enterprise 10000 default | Architecture §3.4.1b (tier table) | `entitlements.py:716–721` | CONFORMS | Exact values match: `(TIER_FREE, CADENCE_MONTHLY): 200`, `(TIER_PRO, CADENCE_MONTHLY): 2000`, `(TIER_PRO, CADENCE_ANNUAL): 2500`, `(TIER_ENTERPRISE, CADENCE_MONTHLY): 10000`. |
| 15 | Anthropic primary / OpenAI fallback; no primary retry on non-200 | Architecture §3.4.3; Locked Decisions #7, #8 | `router.py:37–42` (registration order); `router.py:79–105` (fallback loop) | DRIFTED | **Registration ordering is key-presence dependent, not locked.** `openai` is registered before `anthropic` (lines 37–42): `if settings.openai_api_key: self._register("openai")` then `if settings.anthropic_api_key: self._register("anthropic")`. `_fallback_order` is built in registration order. When `default_llm_provider` is not `"anthropic"`, OpenAI could be tried first. The spec says Anthropic is primary. **No explicit provider fallback on non-200:** the router iterates ALL providers on ANY exception (not just non-200); this is broader than the spec's "non-200 or response-time SLA exceeded" condition, but the effect is correct (fallback occurs on failure). No primary retry. |
| 16 | Per-tier model class: Free→Haiku, Pro→Sonnet, Enterprise→Sonnet-or-Opus | Architecture §3.4.3 (tier table); Locked Decision #7 | `router.py`; `config.py:32–33` | MISSING | `config.py` sets `default_anthropic_model = "claude-sonnet-4-20250514"` and `default_openai_model = "gpt-4o"` as global defaults. No per-tier model selection exists in the router. The router takes `preferred_provider` but no tier parameter. Tier-aware model class routing (Haiku for Free, Sonnet for Pro, Sonnet/Opus for Enterprise) is absent. All tiers use the same model. |
| 17 | Intra-tier fast model routing: no tools + ≤4K ctx + low complexity → fast/cheap model | Architecture §3.4.3 (intra-tier routing); Locked Decision #9 | `router.py` | MISSING | No intra-tier routing logic exists anywhere in the codebase. The router has no complexity scoring, no context-length check, no fast-model selection path. |
| 18 | `app/runtime/llm_router.py` (doctrine-anchored path in §8) | Architecture §8 | — | RESIDUE/PATH DRIFT | Doctrine-anchored path is `app/runtime/llm_router.py`. Actual implementation lives at `app/integrations/llm/router.py`. Per briefing rule, functionality EXISTS — mark as path drift, not missing. |
| 19 | Grounding score = retrieval relevance (cosine similarity) + citation overlap | Architecture §3.4.13 | `orchestrator.py:1101–1130` (`_grounding_from_chunks`) | DRIFTED | Code computes grounding as `1 - best_cosine_distance` (retrieval relevance only). The spec requires a composite of retrieval relevance AND answer-citation overlap ("fraction of answer claims that can be traced to retrieved chunk content"). Citation overlap is absent. Code comment acknowledges: "a richer grounding scorer (answer-vs-source overlap) is a later unit's hook." |
| 20 | `app/runtime/grounding.py` (doctrine-anchored path in §8) | Architecture §8 | — | RESIDUE/PATH DRIFT | Doctrine-anchored path is `app/runtime/grounding.py`. Grounding computation lives in `orchestrator.py:1101–1130` (`_grounding_from_chunks`). Per briefing rule, not MISSING — path drift. |
| 21 | Below-floor AND no tool resolves → `cannot_answer` fires; canonical phrase "I don't have that information, let me get someone who does" | Architecture §3.4.13 | `escalation_judge.py:228–268`; `handoff_ack.py` | DRIFTED | The `cannot_answer` escalation fires correctly. However, the canonical phrase is NOT in the codebase. The `handoff_ack.py` templates contain generic handoff language ("I understand. I'm escalating this..."); the doc-mandated exact phrase "I don't have that information, let me get someone who does" does not appear anywhere in the codebase. |
| 22 | Graph retrieval invoked only on structured-filter-intent; pure semantic → vector only | Architecture §3.4.1 (RETRIEVE step); §3.2; Locked Decision #6 | `orchestrator.py:1046–1095` (`_retrieve`) | MISSING | Graph retrieval is entirely absent. The `_retrieve` method calls `KnowledgeRetriever.retrieve_with_sources` which does vector similarity only. No graph retriever, no structured-intent detection for routing, no graph/vector merge. Architecture Arc 16 owns graph (§3.2), which is a separate deliverable, but the agentic loop's RETRIEVE step does not include even a feature-flag stub for the graph leg. |
| 23 | `app/runtime/knowledge_retrieval.py` (doctrine-anchored path in §8) | Architecture §8 | — | RESIDUE/PATH DRIFT | Doctrine-anchored path is `app/runtime/knowledge_retrieval.py`. Vector retrieval lives in `app/knowledge/retriever.py` (called from `orchestrator.py:1083`). Graph retrieval module does not exist. |
| 24 | Human handoff: `human_controlled` session mode; Luciel stops auto-replying; admin replies via same channel adapter; human turns don't consume LLM budget but session = 1 budget unit | Architecture §3.4.12 | `orchestrator.py:403–434` (`_finalize_intake_escalation`); `cognition/finalizer.py:198–213` | DRIFTED | **`human_controlled` session state is not implemented.** There is no `human_controlled` field on `SessionModel` (`models/session.py` has `status` as a generic `String(50)` but the `human_controlled` state is not set anywhere). The orchestrator fires the escalation and emits a handoff acknowledgement, but no mechanism exists to prevent the agentic loop from processing subsequent inbound messages on that session. The "Luciel stops auto-responding" doctrine is unimplemented. |
| 25 | `human_takeover_started` / `human_takeover_ended` audit events with `actor_user_id` + `trigger` | Architecture §3.4.12 (audit events table) | `models/admin_audit_log.py` | MISSING | `grep -rn "human_takeover"` across the entire backend returns zero results. The two audit event types (`human_takeover_started`, `human_takeover_ended`) with their payloads (`session_id`, `resolved_lead_id`, `instance_id`, `actor_user_id`, `trigger`, `channel`, `duration_seconds`) are not implemented anywhere. |
| 26 | `app/runtime/handoff.py` (doctrine-anchored path in §8) | Architecture §8 | — | RESIDUE/PATH DRIFT | Doctrine-anchored path is `app/runtime/handoff.py`. Handoff acknowledgement templates live at `app/runtime/handoff_ack.py`; handoff bundle assembly lives in `app/cognition/finalizer.py`. Full `human_controlled` session machinery does not exist anywhere. |
| 27 | Voice takeover deferred to v2 | Architecture §3.4.12 | `channel_arbiter.py:56–60` | CONFORMS | `CHANNEL_VOICE` is explicitly marked deferred/never-available in v1 at `channel_arbiter.py:56–60`. |
| 28 | Session key: `(instance_id, resolved_lead_id, channel)` | Architecture §3.4.8 | `models/session.py` | DRIFTED | `SessionModel` has `luciel_instance_id`, `channel`, and `user_id` (free-form string, nullable). There is no `resolved_lead_id` column. The spec requires `resolved_lead_id` as the second dimension of the session key — the identity-resolved lead identifier per §3.4.9. `user_id` is a free-form string, not the structured `resolved_lead_id` from the identity system. |
| 29 | Inactivity timeouts: widget 30m / SMS 4h / email 24h (platform constants, not admin-configurable) | Architecture §3.4.8 | — | MISSING | No session inactivity timeout constants exist in the codebase. `grep -rn "SESSION_TIMEOUT\|TIMEOUT_WIDGET\|widget.*1800\|sms.*14400\|email.*86400"` returns zero results in `app/`. The `cross_session_retriever.py` notes that session TTL is set in Redis keyed by channel inactivity timeout but no code sets or enforces these TTLs. |
| 30 | Session START/END deterministic (no LLM judgment) | Architecture §3.4.8 | `models/session.py`; `cross_session_retriever.py` | AMBIGUOUS | The session model has `status` but no explicit START/END lifecycle management. Session creation in `api/v1/chat_widget.py:362` sets `user_id=None`. No deterministic TTL-based END is enforced in observed code. The summarization pipeline (§3.4.7) is referenced in docs as the TTL-triggered finalization but no Celery task enforcing TTL-based session close was identified. |
| 31 | Cross-session memory only across STRONG identifiers (email/phone); anonymous widget token never inherits history (HARD RULE) | Architecture §3.4.9 | `identity/resolver.py`; `memory/cross_session_retriever.py:152–164` (feature-flag gate) | DRIFTED | The identity system distinguishes `ClaimType.EMAIL` / `ClaimType.PHONE` / `ClaimType.SSO_SUBJECT` — no `WIDGET_TOKEN` claim type exists. The anonymous widget path (`chat_widget.py:362`) sets `user_id=None`. However, `cross_session_retriever.py` is behind a feature-flag gate that raises `RuntimeError` unless `LUCIEL_CROSS_SESSION_RETRIEVER_ENABLED=1` — it has "zero production callers" (per its docstring). The HARD RULE enforcement is structural in the identity model (no widget-token claim type that would match a resolved_lead_id across sessions) but is not explicitly tested as a named invariant. |
| 32 | Cross-session memory injection bound: N=10 most-recent summaries within 12-month window | Architecture §3.4.10 | `memory/service.py`; `memory/cross_session_retriever.py` | MISSING | No N=10 or 12-month window constants exist anywhere. `MemoryService.retrieve_memories` uses `limit=20` (not 10). `CrossSessionRetriever.retrieve` has `MAX_LIMIT=100` and default `limit=20`. Neither the N=10 recency cap nor the 12-month rolling window is implemented. |
| 33 | Recency-precedence on conflicting session facts | Architecture §3.4.10 | `memory/cross_session_retriever.py:157–158` | CONFORMS (partial) | `CrossSessionRetriever` returns messages ordered `messages.created_at DESC` (newest-first). The recency-first ordering is correct. Full conflict-resolution with "most recent fact takes precedence" in the context injection layer was not found — only retrieval ordering. |
| 34 | Budget counter "source of truth is Postgres" on Redis failure | Architecture §4.5 (dependency failure table) | `budget_meter.py`; `models/conversation_overage_ledger.py` | DRIFTED | Architecture §4.5 states: "Budget counter reads from Postgres source of truth" on Redis down. No Postgres live counter exists. `budget_meter.py` Redis failure (`incr_with_ttl` catch) returns 0 (fail-open), meaning on Redis failure the session proceeds uncapped — NOT a Postgres fallback. `conversation_overage_ledger` is a closed-period ledger, not a live counter. This is a documented inconsistency between the arch's resilience narrative and the actual implementation. |
| 35 | `always-on` cognition finalization (lead capture + summarization + handoff bundle) on every turn, every tier | Architecture §3.4.4, §3.4.7; Vision §4.1 | `cognition/finalizer.py:1–60`; `orchestrator.py:744–775` (`_finalize_cognition`) | CONFORMS | `CognitionFinalizer.finalize` is called in `_finalize_cognition` on every turn. Lead capture (`lead_capture.detect`) + summarizer (`summarize`) + handoff bundle assembly all run. Best-effort (never raises). |

---

## CONFLICTS

### CONFLICT-1: Loop step ordering (Architecture §3.4.1 diagram vs orchestrator.py)

**Doc says:** RECEIVE → BUDGET GATE → ESCALATION GATE 1 → RETRIEVE → PLAN

**Code does:** RECEIVE → RETRIEVE → ESCALATION GATE 1 → BUDGET GATE → PLAN

Evidence: `orchestrator.py:131–165`. The comment on the budget gate at line 155 asserts it is "LOAD-BEARING: AFTER intake Gate 1" without acknowledging the ordering divergence from the diagram. This is a production concern: retrieval is performed even on conversations that will be budget-capped (Free at cap) or intake-escalated, consuming unnecessary vector DB resources.

### CONFLICT-2: Grounding floor values (Architecture §3.4.13 table + §9 items 21-23 vs escalation_judge.py)

**Doc says:** Free 0.45, Pro 0.50, Enterprise 0.55 (AUTHORED pending ratification, §9 items 21-23)

**Code implements:** All tiers = 0.50 (`escalation_judge.py:74–77`)

The code comment explicitly acknowledges the flattening: "The floor is FLAT at v1 (0.5 for every tier)". The authored values are pending ratification (§9), but the code's deviation means Free instances have a more restrictive floor (0.50 vs authored 0.45) and Enterprise instances have a less restrictive floor (0.50 vs authored 0.55). The code preserves the per-tier dict structure for future tuning.

### CONFLICT-3: Budget counter "Postgres source of truth" (Architecture §4.5 vs budget_meter.py)

**Doc says (§4.5):** "Budget counter reads from Postgres source of truth" when Redis is down; Redis is "a cache."

**Code implements:** Redis is the ONLY live counter. On Redis failure `incr_with_ttl` returns 0 (fail-open, budget uncapped). No Postgres live counter exists. `conversation_overage_ledger` is a per-period closed ledger, not a live fallback counter.

The `budget_meter.py` docstring says: "Redis is EPHEMERAL — the durable billing record of a closed period's overage is the Postgres `conversation_overage_ledger`." This contradicts the architecture's resilience claim. On Redis outage, Free instances are effectively uncapped rather than reading from Postgres.

### CONFLICT-4: High-value lead heuristic (Architecture §3.4.5 weighted spec vs escalation_judge.py + lead_capture.py)

**Doc says:** Weighted composite: explicit budget figure 0.5 + time-constrained decision 0.3 + purchase/booking intent 0.4, capped 1.0, emitting a normalized score in [0,1].

**Code implements:** Binary threshold only (`lead_value >= 750_000`). No weighting, no time-constrained-decision scoring, no purchase/booking intent scoring. Also `lead_capture.py` is real-estate-specific (listing references, MLS, address patterns) despite the architecture's explicit domain-agnostic requirement.

---

## §9 TOUCHED (AUTHORED commitments this slice's code implements)

| §9 item | Authored value | Value found in code | Notes |
|---|---|---|---|
| §9 item 21 | Free grounding floor 0.45 | 0.50 | `escalation_judge.py:74` — flattened to 0.50 for all tiers |
| §9 item 22 | Pro grounding floor 0.50 | 0.50 | `escalation_judge.py:75` — matches authored value |
| §9 item 23 | Enterprise grounding floor 0.55 | 0.50 | `escalation_judge.py:76` — flattened to 0.50, deviates from authored 0.55 |
| §9 item 24 | Human-controlled turns don't consume LLM budget; session = 1 budget unit | Partially implemented (budget unit = 1 session is correct; `human_controlled` mode itself not implemented) | `budget_meter.py` session-once logic is correct; `human_controlled` session state not enforced |
| §9 item 33 | Human-controlled session budget rule | Same as item 24 | — |

---

## RESIDUE DETAIL

### RESIDUE-1: Path drift — `app/runtime/llm_router.py` (doctrine-anchored)

**Anchor:** Architecture §8 lists `app/runtime/llm_router.py` as a doctrine-anchored file.

**Actual location:** `app/integrations/llm/router.py`

**Impact:** Low functional impact (functionality exists). The `maintainability contract` in §8 requires that changes to doctrine-anchored interfaces update the architecture in the same PR — path drift means the boundary is harder to police. Any PR that changes `app/integrations/llm/router.py` may not trigger the required architecture update because the file path does not match the doctrine anchor.

### RESIDUE-2: Path drift — `app/runtime/grounding.py` (doctrine-anchored)

**Anchor:** Architecture §8 lists `app/runtime/grounding.py`.

**Actual location:** Grounding computation is inline at `orchestrator.py:1101–1130` (`_grounding_from_chunks`). No standalone `grounding.py` module.

**Impact:** The spec envisions a standalone module with "grounding score computation, per-tier floor enforcement, answer-review surface data extraction." The inline helper only computes retrieval-relevance distance; citation overlap and answer-review data extraction are absent. Harder to test, extend, or enforce as a named boundary.

### RESIDUE-3: Path drift — `app/runtime/handoff.py` (doctrine-anchored)

**Anchor:** Architecture §8 lists `app/runtime/handoff.py` — "human_controlled session mode, admin-dashboard dispatch path."

**Actual locations:** Acknowledgement templates at `app/runtime/handoff_ack.py`; handoff bundle at `app/cognition/finalizer.py`. The `human_controlled` session mode machinery does not exist.

**Impact:** High. The `human_controlled` state (Luciel stops auto-replying, admin dispatches via same adapter, audit events `human_takeover_started`/`human_takeover_ended`) is not implemented. A production trigger of the explicit-human-request signal will emit a handoff acknowledgement but will NOT stop Luciel from processing the next inbound message.

### RESIDUE-4: Path drift — `app/runtime/knowledge_retrieval.py` (doctrine-anchored)

**Anchor:** Architecture §8 lists `app/runtime/knowledge_retrieval.py` — "hybrid vector + graph retrieval."

**Actual location:** Vector retrieval at `app/knowledge/retriever.py`. Graph retrieval: absent.

**Impact:** Graph retrieval (§3.2, Architecture §3.4.1 RETRIEVE step, Locked Decision #6) is entirely absent. The doctrine-anchored path does not exist.

---

## BLOCKED-EXTERNAL

| Item | What is needed | Why blocked |
|---|---|---|
| BX-1: Redis live counter behavior on production ElastiCache | Verify Redis failover behavior and whether any operational runbook implements Postgres fallback for budget counter | Requires AWS ElastiCache console + ops runbook access; not in code |
| BX-2: `default_llm_provider` setting value in production | Verify whether `settings.default_llm_provider == "anthropic"` in deployed ECS task definition | Requires AWS ECS task definition or Parameter Store access |
| BX-3: Stripe webhook configuration | Verify `invoice.paid` webhook is configured and `billing_period_start` advances correctly in production Stripe | Requires Stripe dashboard access |

---

## Headline Summary (10 lines)

1. **Agentic loop step ordering is drifted**: RETRIEVE runs before BUDGET GATE and ESCALATION GATE 1, inverting the spec's diagram (Architecture §3.4.1). Retrieval cost is wasted on capped/escalated sessions.
2. **5-iteration bound and two-gate split conform exactly** to spec; bound-hit is correctly never an escalation trigger.
3. **All four escalation signals and their thresholds conform** to spec, and the no-admin-configurable-triggers doctrine is cleanly enforced.
4. **Grounding floors are flattened** (all 0.50 vs spec's 0.45/0.50/0.55); only the Pro floor matches the authored §9 values.
5. **Grounding computation is retrieval-relevance only** — the spec-required citation-overlap component is missing; the canonical "I don't have that information, let me get someone who does" phrase is absent from the codebase.
6. **High-value lead heuristic is significantly under-implemented**: binary budget threshold only, no weighted scoring, and the lead-capture detector is real-estate-specific despite the domain-agnostic requirement.
7. **LLM router is missing per-tier model classes and intra-tier fast routing**; registration order makes Anthropic-primary doctrine key-presence-dependent rather than locked.
8. **`human_controlled` session mode is unimplemented**: no session state, no stop-auto-reply gate, no `human_takeover_started/ended` audit events — the §3.4.12 live takeover doctrine exists only as a handoff acknowledgement template.
9. **Session key and timeouts drift**: session model has no `resolved_lead_id` column (uses free-form `user_id`); inactivity timeout constants (30m/4h/24h) exist nowhere in code; N=10/12-month injection bound is absent.
10. **Four doctrine-anchored §8 file paths are drifted** (`llm_router.py`, `grounding.py`, `handoff.py`, `knowledge_retrieval.py`) — undermining the maintainability contract requiring same-PR architecture updates on interface changes.

---

## Status Counts

| Status | Count |
|---|---|
| CONFORMS | 12 |
| DRIFTED | 9 |
| MISSING | 5 |
| AMBIGUOUS | 1 |
| RESIDUE/PATH DRIFT | 4 |
| BLOCKED-EXTERNAL | 3 |
| **Total rows** | **35** |
