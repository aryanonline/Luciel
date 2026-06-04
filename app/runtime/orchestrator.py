"""Luciel orchestrator — Arc 14 U1: the agentic loop skeleton.

Architecture v1 §3.4.1 names the eight conceptual steps of the
agentic loop, in two halves:

  Half A (decision)
    1. RECEIVE             — trust the resolved (admin, instance,
                             session) the inbound envelope carries.
    2. CONTEXT ASSEMBLY    — flag-gated Retrieve (ARC 11) + prompt
                             composition via ContextAssembler.
    3. ESCALATION GATE 1   — INTAKE (pre-PLAN). PASS-THROUGH in U1;
                             real signals land in U2. The seam is here
                             so U2 drops in without restructuring.
    4. PLAN → ACT → REFLECT — bounded at 5 passes (§3.4.1 locked #17).
                             Cost-control ONLY; hitting the bound is
                             NEVER an escalation trigger.
    5. ESCALATION GATE 2   — OUTCOME (post-REFLECT). PASS-THROUGH in U1.

  Half B (emission)
    6. RESPOND             — emit the reply. (Channel arbiter is U3;
                             U1 returns the reply on the inbound channel.)
    7. COGNITION FINALIZE  — fill the trace the stub left None/False:
                             llm_provider / llm_model / tool_called /
                             tool_name, plus persist via TraceService.

Arc 14 U1 scope
---------------
This unit replaces the hardcoded-message stub with the real loop
wired to ``ModelRouter.generate`` (PLAN) and ``ToolBroker`` (ACT).
The escalation gates are clean pass-through seams (U2). Cognition
finalization here is the *trace* half — folding CognitionService's
escalate/save_memory/summary behaviours is U4.

Determinism / cost discipline
-----------------------------
App code talks to real providers via ``ModelRouter``; tests inject a
fake router (or a real router with ``StubLLMClient``) so CI is
deterministic and free (founder decision #2). When NO provider is
configured (the lazy default in a unit test that injects nothing),
``ModelRouter.generate`` raises ``RuntimeError`` — PLAN catches it and
degrades to a low-confidence no-tool reply rather than crashing the
turn. This keeps every pre-ARC-14 call site (which constructs
``LucielOrchestrator()`` with no LLM) working unchanged.

The Retrieve step + trace-persistence helpers are preserved verbatim
from ARC 11 Step 8.
"""
from __future__ import annotations

import logging
from typing import Sequence
from uuid import uuid4

from app.core.config import settings
from app.integrations.llm.base import LLMMessage, LLMRequest
from app.runtime.context_assembler import ContextAssembler
from app.runtime.contracts import RuntimeRequest, RuntimeResponse
from app.runtime.plan_parser import (
    PLAN_JSON_INSTRUCTION,
    Plan,
    parse_plan,
)

logger = logging.getLogger(__name__)


# §3.4.1 locked decision #17: the plan→act→reflect bound is 5 passes
# per inbound message. Cost-control, NOT an escalation trigger, NOT
# admin-configurable. Pinned here as a module constant so a test can
# assert the exact value without reaching into a settings object.
MAX_LOOP_ITERATIONS: int = 5


class LucielOrchestrator:
    """Runtime entry point. The ARC 14 agentic loop lives in ``run``.

    Dependencies are injectable so tests can drive cognition through a
    fake/stub LLM and a fake broker (deterministic, no network, no API
    cost — founder decision #2). Every constructor arg is optional so
    pre-ARC-14 call sites that build ``LucielOrchestrator()`` keep
    working: the LLM router and tool broker are built lazily on first
    use.
    """

    def __init__(
        self,
        *,
        trace_service=None,
        model_router=None,
        tool_broker=None,
        escalation_judge=None,
        escalation_service=None,
        escalation_delivery_service=None,
        channel_arbiter=None,
        cognition_finalizer=None,
        budget_meter=None,
        budget_alert_service=None,
    ) -> None:
        self.context = ContextAssembler()
        self._trace_service = trace_service
        self._model_router = model_router
        self._tool_broker = tool_broker
        # Arc 14 U4 — §3.4.4/§3.4.6/§3.4.7 COGNITION FINALIZATION (lead
        # capture + summary + live handoff). Always-on cognition; built
        # lazily so pre-U4 call sites keep working. Injectable for tests.
        self._cognition_finalizer = cognition_finalizer
        # Arc 14 U3 — the §3.4.2 channel arbiter (pure decision). The
        # RESPOND step calls it to pick the outbound channel. Injectable
        # for tests; built lazily (it has no dependencies).
        self._channel_arbiter = channel_arbiter
        # Arc 14 U2 — the §3.4.5 judge (pure decision) and the
        # escalation side-effect service (event store + routing + audit).
        # Injectable so tests drive deterministic fakes; built lazily so
        # pre-ARC-14 call sites keep working. A judge with no classifiers
        # and no configured LLM provider degrades to non-firing, so a
        # missing provider NEVER invents an escalation.
        self._escalation_judge = escalation_judge
        self._escalation_service = escalation_service
        # Rescan Tier-C — §3.5 escalation delivery service (notification
        # send-with-retry + idempotency + audit). Injectable so tests can
        # drive a fake. Built lazily when first needed. Called AFTER the
        # customer reply is sent — never blocks or crashes the turn.
        self._escalation_delivery_service = escalation_delivery_service
        # Arc 18 — conversation budget meter (Redis counter) + alert
        # service (80%/100% notifications). Injectable so tests drive an
        # InMemoryBackend / fake alerter; built lazily so pre-Arc-18 call
        # sites keep working.
        self._budget_meter = budget_meter
        self._budget_alert_service = budget_alert_service

    # ------------------------------------------------------------------
    # Public entry point — the §3.4.1 agentic loop
    # ------------------------------------------------------------------

    def run(self, req: RuntimeRequest) -> RuntimeResponse:
        # 1. RECEIVE — the resolved (admin, instance, session) is on req.
        # 2. CONTEXT ASSEMBLY — flag-gated Retrieve + prompt composition.
        chunks: list = []
        source_ids: list[int] = []
        retrieval_attempted = False
        if (
            settings.knowledge_retrieval_enabled
            and req.luciel_instance_id is not None
        ):
            retrieval_attempted = True
            chunks = self._retrieve(req)
            source_ids = self._collect_source_pks(chunks)

        base_prompt = self.context.build_prompt(req, retrieved_chunks=chunks)

        # 3. ESCALATION GATE 1 — INTAKE (pre-PLAN). §3.4.5 signals (a)+(b).
        #    If it fires we SKIP plan/act/reflect and emit a templated
        #    handoff acknowledgement instead of an LLM-generated reply
        #    (the LLM may be exactly what's failing the customer).
        intake_decision = self._intake_gate(req)
        if intake_decision is not None:
            return self._finalize_intake_escalation(
                req=req, decision=intake_decision, source_ids=source_ids
            )

        # 3b. CONVERSATION BUDGET GATE — Arc 18 (§3.4.1b). Position is
        #     LOAD-BEARING: AFTER intake Gate 1 (an intake-escalated
        #     session returned above and NEVER reaches here, so it never
        #     consumes budget) and BEFORE PLAN (the first LLM call). The
        #     gate counts this conversation ONCE (idempotent across the
        #     REFLECT loop). For Free at cap it returns a budget_exhausted
        #     EscalationDecision → short-circuit with NO LLM call. For
        #     Pro/Enterprise it never blocks (returns None) — it only
        #     increments the counter and fires 80%/100% alerts.
        budget_decision = self._budget_gate(req)
        if budget_decision is not None:
            return self._finalize_budget_escalation(
                req=req, decision=budget_decision, source_ids=source_ids
            )

        # 4. PLAN → ACT → REFLECT — bounded at MAX_LOOP_ITERATIONS.
        loop = self._run_plan_act_reflect(req, base_prompt)

        # Thread the CONTEXT-step retrieval outcome onto the loop result
        # so the OUTCOME gate can read grounding + retrieval-failure
        # (spec item 5). retrieval_failed = retrieval RAN but yielded no
        # usable chunks; grounding is a composite of retrieval relevance
        # + citation overlap (§3.4.13) using the loop's final reply.
        loop.retrieval_failed = retrieval_attempted and not chunks
        loop.grounding_score = self._grounding_from_chunks(
            chunks, answer=loop.reply or ""
        )

        # RESCAN TIER-C — detect lead candidate now so the OUTCOME gate
        # has the weighted composite lead_score before it evaluates the
        # HIGH-VALUE LEAD signal. This is a pure read (no DB, no LLM);
        # the finalizer later persists the same candidate as a lead row.
        self._detect_lead_for_outcome_gate(req, loop)

        # 5. ESCALATION GATE 2 — OUTCOME (post-REFLECT). §3.4.5 (c)+(d).
        #    NEVER reads loop.bound_hit (§3.4.1 locked #17).
        outcome_decision = self._outcome_gate(req, loop)
        escalation_flag = outcome_decision is not None
        if outcome_decision is not None:
            self._record_escalation_best_effort(outcome_decision)

        # 6. RESPOND — the §3.4.2 channel arbiter picks the outbound
        #    channel (replaces U1's "emit on inbound channel"). The pick
        #    defaults safely to the inbound channel when channel info is
        #    sparse, so this never breaks the turn.
        #    When SIGNAL_CANNOT_CONFIDENTLY_ANSWER fired, replace the LLM
        #    reply with the §3.4.13 canonical phrase so the anti-hallucination
        #    promise (Vision §1) is upheld: we never send an ungrounded answer.
        from app.models.escalation_event import SIGNAL_CANNOT_CONFIDENTLY_ANSWER
        if (
            outcome_decision is not None
            and outcome_decision.signal == SIGNAL_CANNOT_CONFIDENTLY_ANSWER
        ):
            from app.runtime.handoff_ack import cannot_answer_reply
            message = cannot_answer_reply()
        else:
            message = loop.reply
        choice = self._arbitrate_channel(
            req, reply=message, escalation_fired=escalation_flag
        )

        # 7. COGNITION FINALIZATION — fill the trace fields the stub
        #    left None/False, then run always-on cognition (§3.4.4 lead
        #    capture + §3.4.7 summary + §3.4.6 live handoff).
        trace_id = self._record_trace_best_effort(
            req=req,
            assistant_reply=message,
            source_ids=source_ids,
            llm_provider=loop.llm_provider,
            llm_model=loop.llm_model,
            tool_called=loop.tool_called,
            tool_name=loop.tool_name,
            escalated=escalation_flag,
        )
        self._finalize_cognition(
            req=req,
            assistant_reply=message,
            escalation_fired=escalation_flag,
            escalation_decision=outcome_decision,
        )

        return RuntimeResponse(
            message=message,
            trace_id=trace_id,
            confidence=loop.confidence,
            session_id=req.session_id,
            intent_summary="Initial user intent captured",
            escalation_flag=escalation_flag,
            source_ids_used=source_ids,
            llm_provider=loop.llm_provider,
            llm_model=loop.llm_model,
            tool_called=loop.tool_called,
            tool_name=loop.tool_name,
            iterations=loop.iterations,
            bound_hit=loop.bound_hit,
            response_channel=choice.channel,
            prompt_channel_switch=choice.prompt_channel_switch,
        )

    # ------------------------------------------------------------------
    # Channel arbiter — Arc 14 U3 (§3.4.2)
    # ------------------------------------------------------------------

    def _arbitrate_channel(
        self, req: RuntimeRequest, *, reply: str, escalation_fired: bool
    ):
        """RESPOND step — §3.4.2 channel arbiter pick.

        Resolves the instance's enabled-channel set and runs the
        decision tree. Never raises: any failure degrades to the inbound
        channel so the turn always emits somewhere safe."""
        from app.runtime.channel_arbiter import ArbiterInput, ChannelChoice

        try:
            enabled = self._resolve_enabled_channels(req)
            return self._arbiter().pick(
                ArbiterInput(
                    inbound_channel=req.channel,
                    enabled_channels=enabled,
                    response_length=len(reply or ""),
                    escalation_fired=escalation_fired,
                    customer_requested_channel=req.customer_requested_channel,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "channel arbiter failed: exc_class=%s — defaulting to inbound "
                "channel",
                type(exc).__name__,
            )
            return ChannelChoice(
                channel=req.channel, reason="arbiter_error_fallback_inbound"
            )

    def _resolve_enabled_channels(self, req: RuntimeRequest) -> set[str]:
        """Read the per-Instance enabled-channel set (ARC 13
        ``instances.enabled_channels`` + the widget floor).

        Best-effort: when no instance id is on the request, or the lookup
        fails, return just the inbound channel so the arbiter degrades to
        a same-channel reply rather than crashing the turn. The arbiter
        also adds the widget floor + inbound channel defensively."""
        from app.policy.entitlements import CHANNEL_WIDGET

        if req.luciel_instance_id is None:
            return {req.channel, CHANNEL_WIDGET}

        try:
            from app.db.session import SessionLocal
            from app.models.instance import Instance

            db = SessionLocal()
            try:
                instance = db.get(Instance, req.luciel_instance_id)
                if instance is None:
                    return {req.channel, CHANNEL_WIDGET}
                enabled = set(instance.enabled_channels or ())
                enabled.add(CHANNEL_WIDGET)
                return enabled
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "enabled-channel resolution failed: exc_class=%s — defaulting "
                "to inbound channel only",
                type(exc).__name__,
            )
            return {req.channel, CHANNEL_WIDGET}

    def _arbiter(self):
        """Return the injected ChannelArbiter, or build one lazily."""
        if self._channel_arbiter is None:
            from app.runtime.channel_arbiter import ChannelArbiter

            self._channel_arbiter = ChannelArbiter()
        return self._channel_arbiter

    # ------------------------------------------------------------------
    # Escalation gates — Arc 14 U2 (§3.4.5)
    # ------------------------------------------------------------------

    def _intake_gate(self, req: RuntimeRequest):
        """ESCALATION GATE 1 — INTAKE (pre-PLAN). §3.4.1 step 3.

        Evaluates the two signals knowable from the inbound message
        alone (explicit human request; strong negative sentiment) via
        the §3.4.5 judge. Returns the firing ``EscalationDecision`` (the
        caller short-circuits plan/act/reflect) or ``None`` to proceed
        into PLAN. Never raises — a judge failure degrades to None (no
        escalation) so the turn proceeds rather than crashing.
        """
        try:
            return self._judge().evaluate_intake(req)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "intake gate evaluation failed: exc_class=%s — proceeding to PLAN",
                type(exc).__name__,
            )
            return None

    def _outcome_gate(self, req: RuntimeRequest, loop: "_LoopResult"):
        """ESCALATION GATE 2 — OUTCOME (post-REFLECT). §3.4.1 step 5.

        Evaluates the two signals needing loop output (cannot
        confidently answer; high-value lead) via the §3.4.5 judge.
        Returns the firing ``EscalationDecision`` or ``None``.

        Doctrine guard (§3.4.1 locked #17): hitting the iteration bound
        is cost-control, NOT an escalation trigger. The ``OutcomeContext``
        built here NEVER carries ``loop.bound_hit`` and the judge never
        reads it, so the invariant holds. Never raises — a judge failure
        degrades to None.
        """
        from app.runtime.escalation_judge import OutcomeContext

        try:
            outcome = OutcomeContext(
                confidence=loop.confidence,
                tier=self._resolve_tier(req),
                grounding_score=loop.grounding_score,
                retrieval_failed=loop.retrieval_failed,
                lead_value=loop.lead_value,
                # RESCAN TIER-C — weighted composite lead score + Pro/Ent
                # business-context custom rules (Free = None).
                lead_score=loop.lead_score,
                business_context_rules=self._resolve_business_context_rules(req),
            )
            return self._judge().evaluate_outcome(req, outcome)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "outcome gate evaluation failed: exc_class=%s — no escalation",
                type(exc).__name__,
            )
            return None

    def _resolve_tier(self, req: RuntimeRequest) -> str:
        """Resolve the Admin's tier for the OUTCOME grounding floor.

        Reuses the escalation-routing contact resolution (tier-only,
        fail-closed to Free), so the grounding floor and the
        notification channel set agree on the tier. Never raises.
        """
        from app.policy.entitlements import TIER_FREE

        try:
            from app.db.session import SessionLocal
            from app.policy.escalation_routing import resolve_contact

            db = SessionLocal()
            try:
                contact = resolve_contact(
                    db,
                    admin_id=req.admin_id,
                    luciel_instance_id=req.luciel_instance_id,
                )
                return contact.tier
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "tier resolution failed: exc_class=%s — defaulting to Free floor",
                type(exc).__name__,
            )
            return TIER_FREE

    def _detect_lead_for_outcome_gate(
        self, req: RuntimeRequest, loop: "_LoopResult"
    ) -> None:
        """Populate ``loop.lead_score`` + ``loop.lead_value`` from the
        domain-agnostic lead detector so the OUTCOME gate can evaluate
        the HIGH-VALUE LEAD signal before the finalizer persists the row.

        RESCAN TIER-C: replaces the old path where lead_value was always
        None here (set only inside the finalizer, which ran AFTER the
        gate). The detector is deterministic, cheap, and never raises.
        """
        try:
            from app.cognition.lead_capture import detect

            candidate = detect(
                message=req.message,
                prior_customer_messages=req.recent_customer_messages,
                inbound_channel=req.channel,
            )
            if candidate is not None:
                loop.lead_score = candidate.lead_score
                loop.lead_value = candidate.lead_value
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "lead detection for outcome gate failed: exc_class=%s — "
                "lead_score stays 0 (no high-value-lead escalation this turn)",
                type(exc).__name__,
            )

    def _resolve_business_context_rules(
        self, req: RuntimeRequest
    ) -> list[dict] | None:
        """Return the Pro/Enterprise custom-value rules from the admin's
        business-context field, or ``None`` for Free.

        RESCAN TIER-C: §3.4.5 — Pro and Enterprise admins can define
        custom value rules in the business-context field; the escalation
        judgment module incorporates context + those rules into its
        scoring logic. Free uses the built-in heuristic only.

        Rules shape: list of dicts ``{"pattern": str, "weight_boost": float}``.
        Any DB/tier lookup failure degrades to ``None`` (built-in only).
        """
        try:
            tier = self._resolve_tier(req)
            from app.policy.entitlements import TIER_FREE

            if tier == TIER_FREE:
                return None

            # Pro/Enterprise: look up business-context rules from the
            # instance config. This is a best-effort lookup; missing or
            # malformed config degrades to None (built-in only).
            return self._load_business_context_rules(req)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "business_context_rules resolution failed: exc_class=%s — "
                "using built-in lead scoring only",
                type(exc).__name__,
            )
            return None

    def _load_business_context_rules(
        self, req: RuntimeRequest
    ) -> list[dict] | None:
        """Load custom lead-scoring rules from the per-instance
        business-context field (Pro/Enterprise only).

        The business-context field lives on the ``AgentConfig`` or
        ``TenantConfig`` table as a JSON blob. The expected shape for
        lead rules is:
          {"lead_rules": [{"pattern": "<regex>", "weight_boost": 0.2}]}

        Missing field, missing table, or unrecognised shape → ``None``
        (built-in heuristic only, no crash).
        """
        try:
            from app.db.session import SessionLocal

            db = SessionLocal()
            try:
                # Attempt to load from AgentConfig first (instance-level),
                # falling back to None when not found.
                from app.models.agent_config import AgentConfig  # type: ignore

                config = db.query(AgentConfig).filter_by(
                    luciel_instance_id=req.luciel_instance_id
                ).first() if req.luciel_instance_id else None

                if config is None:
                    return None

                business_context = getattr(config, "business_context", None)
                if not isinstance(business_context, dict):
                    return None

                rules = business_context.get("lead_rules")
                if not isinstance(rules, list):
                    return None

                # Validate shape: each rule must have pattern + weight_boost.
                valid_rules = [
                    r for r in rules
                    if isinstance(r, dict)
                    and isinstance(r.get("pattern"), str)
                    and isinstance(r.get("weight_boost"), (int, float))
                ]
                return valid_rules or None
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "load_business_context_rules failed: exc_class=%s — "
                "built-in scoring only",
                type(exc).__name__,
            )
            return None

    def _finalize_intake_escalation(
        self,
        *,
        req: RuntimeRequest,
        decision,
        source_ids: list[int],
    ) -> RuntimeResponse:
        """Build the Gate-1 short-circuit response: record the
        escalation (best-effort) and emit a templated handoff
        acknowledgement INSTEAD of running plan/act/reflect."""
        from app.runtime.handoff_ack import handoff_acknowledgement

        self._record_escalation_best_effort(decision)

        message = handoff_acknowledgement()
        # Gate-1 IS an escalation: let the arbiter pick the outbound
        # channel for the handoff acknowledgement too (escalation rule →
        # highest-priority enabled channel, fallback inbound).
        choice = self._arbitrate_channel(
            req, reply=message, escalation_fired=True
        )
        trace_id = self._record_trace_best_effort(
            req=req,
            assistant_reply=message,
            source_ids=source_ids,
            llm_provider=None,
            llm_model=None,
            tool_called=False,
            tool_name=None,
            escalated=True,
        )
        # COGNITION FINALIZATION on the short-circuit path too: a Gate-1
        # escalation IS an escalation, so lead capture + summary still
        # run and a live handoff bundle is built (Gate-1 fires on an
        # explicit human request or strong negative sentiment — both
        # warrant a real-time takeover).
        self._finalize_cognition(
            req=req,
            assistant_reply=message,
            escalation_fired=True,
            escalation_decision=decision,
        )
        return RuntimeResponse(
            message=message,
            trace_id=trace_id,
            # Gate-1 fired pre-PLAN: the loop never ran, so confidence is
            # the signal confidence (or 1.0 for an explicit human request
            # with no probabilistic score) — but we report 0 iterations
            # and no provider to make the short-circuit observable.
            confidence=decision.signal_confidence or 0.0,
            session_id=req.session_id,
            intent_summary="Initial user intent captured",
            escalation_flag=True,
            source_ids_used=source_ids,
            llm_provider=None,
            llm_model=None,
            tool_called=False,
            tool_name=None,
            iterations=0,
            bound_hit=False,
            response_channel=choice.channel,
            prompt_channel_switch=choice.prompt_channel_switch,
        )

    def _record_escalation_best_effort(self, decision) -> None:
        """Record an escalation via EscalationService + trigger delivery.

        Best-effort: the decision to escalate has already been made;
        persistence + notify are observability/delivery side-effects and
        must never crash the turn (Architecture §5.1).

        Rescan Tier-C: after recording the event, calls the delivery
        service to send the actual admin notification (email/SMS/Slack)
        for the four real signals. The customer reply is sent BEFORE
        this method is called, so delivery NEVER blocks the turn.
        """
        routing = None
        try:
            routing = self._escalation_svc().record_escalation(decision)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "record_escalation failed: exc_class=%s — escalation flagged "
                "on the turn but side-effects degraded",
                type(exc).__name__,
            )
        # Rescan Tier-C — wire the real notification send for the four signals
        # (budget_exhausted uses _notify_budget_exhausted; the four real signals
        # use the delivery service). Best-effort: never raises.
        from app.models.escalation_event import SIGNAL_BUDGET_EXHAUSTED
        if decision.signal != SIGNAL_BUDGET_EXHAUSTED:
            try:
                event_id = routing.event_id if routing is not None else None
                contact = None
                if routing is not None:
                    # Rebuild EscalationContact from the routing decision.
                    from app.policy.escalation_routing import EscalationContact
                    contact = EscalationContact(
                        admin_id=decision.admin_id,
                        tier=routing.tier,
                        channels=routing.channels,
                    )
                if contact is not None:
                    self._escalation_delivery_svc().deliver(
                        event_id=event_id,
                        admin_id=decision.admin_id,
                        luciel_instance_id=decision.luciel_instance_id,
                        session_id=decision.session_id,
                        signal=decision.signal,
                        gate=decision.gate,
                        contact=contact,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "escalation delivery failed: exc_class=%s — "
                    "notification not sent but turn is unaffected",
                    type(exc).__name__,
                )

    # ------------------------------------------------------------------
    # Conversation budget gate — Arc 18 (§3.4.1b)
    # ------------------------------------------------------------------

    def _budget_meter_inst(self):
        """Lazy BudgetMeter accessor. Built from settings.redis_url on
        first use; injectable for tests."""
        if self._budget_meter is None:
            from app.runtime.budget_meter import BudgetMeter

            self._budget_meter = BudgetMeter()
        return self._budget_meter

    def _budget_gate(self, req: RuntimeRequest):
        """CONVERSATION BUDGET GATE — Arc 18 §3.4.1b.

        Counts this conversation ONCE per session (idempotent across the
        REFLECT loop). Returns a ``budget_exhausted`` EscalationDecision
        (Free at/over cap → short-circuit, no LLM call) or ``None``
        (proceed to PLAN).

        Pro/Enterprise NEVER block: capacity is never cut off
        mid-conversation (Vision §2). When a paying instance is over cap
        the counter still increments (overage) and the 80%/100% alerts
        fire — but the turn proceeds to PLAN. Never raises: any failure
        degrades to None (proceed) so a metering hiccup never blocks a
        legitimate turn.
        """
        # No per-instance scope → no per-instance budget to meter. Proceed.
        if req.luciel_instance_id is None:
            return None

        try:
            from app.policy.entitlements import (
                ALERT_THRESHOLD_80,
                ALERT_THRESHOLD_100,
                TIER_FREE,
                conversation_budget,
            )
            from app.runtime.billing_period import resolve_billing_context

            from app.db.session import SessionLocal

            db = SessionLocal()
            try:
                ctx = resolve_billing_context(db, admin_id=req.admin_id)
            finally:
                db.close()

            cap = conversation_budget(ctx.tier, ctx.cadence)
            meter = self._budget_meter_inst()
            count = meter.count_session_once(
                admin_id=req.admin_id,
                instance_id=req.luciel_instance_id,
                period_start=ctx.period_start,
                session_id=req.session_id,
            )

            # Free at/over cap → graceful single-turn handoff, no LLM call.
            if ctx.tier == TIER_FREE and count > cap:
                return self._build_budget_decision(
                    req=req, ctx=ctx, count=count, cap=cap
                )

            # Pro/Enterprise: never block. Fire threshold alerts (idempotent)
            # then proceed. Alerts are best-effort side effects.
            if ctx.tier != TIER_FREE:
                self._maybe_fire_budget_alerts(
                    req=req, ctx=ctx, count=count, cap=cap
                )
            return None
        except Exception as exc:  # noqa: BLE001 — never block a turn on metering
            logger.warning(
                "budget gate evaluation failed: exc_class=%s — proceeding to PLAN",
                type(exc).__name__,
            )
            return None

    def _build_budget_decision(self, *, req: RuntimeRequest, ctx, count: int, cap: int):
        """Construct the budget_exhausted EscalationDecision (GATE_INTAKE,
        like the intake gate, so it short-circuits pre-PLAN)."""
        from app.models.escalation_event import (
            GATE_INTAKE,
            SIGNAL_BUDGET_EXHAUSTED,
        )
        from app.policy.escalation import EscalationDecision

        return EscalationDecision(
            signal=SIGNAL_BUDGET_EXHAUSTED,
            gate=GATE_INTAKE,
            admin_id=req.admin_id,
            session_id=req.session_id,
            luciel_instance_id=req.luciel_instance_id,
            user_id=req.user_id,
            signal_confidence=1.0,
            reasoning_excerpt=(
                f"Free instance at conversation budget cap: count={count} > cap={cap} "
                f"(period_start={ctx.period_start})"
            ),
            signal_inputs={
                "current": count,
                "cap": cap,
                "tier": ctx.tier,
                "cadence": ctx.cadence,
                "billing_period_start": ctx.period_start,
            },
        )

    def _maybe_fire_budget_alerts(self, *, req: RuntimeRequest, ctx, count: int, cap: int):
        """Fire 80%/100% budget alerts for a paying instance, once each
        per period (idempotent via the meter's alert markers). Best-effort."""
        try:
            from app.policy.entitlements import ALERT_THRESHOLD_100, ALERT_THRESHOLD_80

            if cap <= 0:
                return
            pct = (count * 100) // cap
            meter = self._budget_meter_inst()
            for threshold in (ALERT_THRESHOLD_80, ALERT_THRESHOLD_100):
                if pct >= threshold and meter.mark_alert_fired_once(
                    admin_id=req.admin_id,
                    instance_id=req.luciel_instance_id,
                    period_start=ctx.period_start,
                    threshold=threshold,
                ):
                    self._budget_alert_svc().send_budget_alert(
                        admin_id=req.admin_id,
                        instance_id=req.luciel_instance_id,
                        tier=ctx.tier,
                        threshold=threshold,
                        current=count,
                        cap=cap,
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "budget alert dispatch failed: exc_class=%s — turn proceeds",
                type(exc).__name__,
            )

    def _budget_alert_svc(self):
        if self._budget_alert_service is None:
            from app.services.budget_alert_service import BudgetAlertService

            self._budget_alert_service = BudgetAlertService()
        return self._budget_alert_service

    def _finalize_budget_escalation(
        self,
        *,
        req: RuntimeRequest,
        decision,
        source_ids: list[int],
    ) -> RuntimeResponse:
        """Free budget-exhausted short-circuit (§3.4.1b). Mirrors
        ``_finalize_intake_escalation``: record the escalation (→
        escalation_events + admin_audit_log ACTION_ESCALATION_FIRED),
        write the NAMED budget_exhausted audit row, emit a templated
        acknowledgement (NO LLM call), and notify the admin on the
        tier-shaped channel (Free = email). The end customer ALWAYS gets a
        response — no silent drop."""
        from app.runtime.budget_ack import budget_exhausted_acknowledgement

        self._record_escalation_best_effort(decision)
        self._record_budget_exhausted_audit(decision)
        self._notify_budget_exhausted(req=req, decision=decision)

        message = budget_exhausted_acknowledgement()
        choice = self._arbitrate_channel(req, reply=message, escalation_fired=True)
        trace_id = self._record_trace_best_effort(
            req=req,
            assistant_reply=message,
            source_ids=source_ids,
            llm_provider=None,
            llm_model=None,
            tool_called=False,
            tool_name=None,
            escalated=True,
        )
        self._finalize_cognition(
            req=req,
            assistant_reply=message,
            escalation_fired=True,
            escalation_decision=decision,
        )
        return RuntimeResponse(
            message=message,
            trace_id=trace_id,
            confidence=decision.signal_confidence or 0.0,
            session_id=req.session_id,
            intent_summary="Initial user intent captured",
            escalation_flag=True,
            source_ids_used=source_ids,
            llm_provider=None,  # NO LLM call on the budget short-circuit
            llm_model=None,
            tool_called=False,
            tool_name=None,
            iterations=0,
            bound_hit=False,
            response_channel=choice.channel,
            prompt_channel_switch=choice.prompt_channel_switch,
        )

    def _record_budget_exhausted_audit(self, decision) -> None:
        """Write the NAMED budget_exhausted audit row (§3.4.1b requires
        the event on admin_audit_log). Best-effort — never crash the turn."""
        try:
            from app.db.session import SessionLocal
            from app.models.admin_audit_log import (
                ACTION_BUDGET_EXHAUSTED,
                RESOURCE_INSTANCE,
            )
            from app.repositories.admin_audit_repository import (
                AdminAuditRepository,
                AuditContext,
            )

            db = SessionLocal()
            try:
                AdminAuditRepository(db).record(
                    ctx=AuditContext.system(label="budget_meter"),
                    admin_id=decision.admin_id,
                    action=ACTION_BUDGET_EXHAUSTED,
                    resource_type=RESOURCE_INSTANCE,
                    resource_pk=decision.luciel_instance_id,
                    luciel_instance_id=decision.luciel_instance_id,
                    after=dict(decision.signal_inputs or {}),
                    note="Free conversation budget exhausted; graceful handoff (no LLM call).",
                    autocommit=True,
                )
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "budget_exhausted audit write failed: exc_class=%s",
                type(exc).__name__,
            )

    def _notify_budget_exhausted(self, *, req: RuntimeRequest, decision) -> None:
        """Notify the Free admin that the budget was exhausted (Free =
        email only, Vision §7). Best-effort."""
        try:
            from app.policy.entitlements import TIER_FREE

            self._budget_alert_svc().send_budget_alert(
                admin_id=req.admin_id,
                instance_id=req.luciel_instance_id,
                tier=TIER_FREE,
                threshold=100,
                current=int((decision.signal_inputs or {}).get("current", 0)),
                cap=int((decision.signal_inputs or {}).get("cap", 0)),
                exhausted=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "budget_exhausted notify failed: exc_class=%s",
                type(exc).__name__,
            )

    # ------------------------------------------------------------------
    # COGNITION FINALIZATION — Arc 14 U4 (§3.4.4 / §3.4.6 / §3.4.7)
    # ------------------------------------------------------------------

    def _finalize_cognition(
        self,
        *,
        req: RuntimeRequest,
        assistant_reply: str,
        escalation_fired: bool,
        escalation_decision,
    ) -> None:
        """Run always-on COGNITION FINALIZATION (lead + summary + handoff).

        Best-effort: never raises. The reply has already been chosen and
        the trace already written; this is a pure side-effect half (§5.1)
        — a failure degrades to a warning rather than crashing the turn.

        ``handoff_requested`` (the §3.4.6 real-time takeover signal) is
        derived from the firing escalation signal: an EXPLICIT HUMAN
        REQUEST always warrants a live takeover; the other signals flag
        the conversation for follow-up but do not by themselves pull the
        customer into a live transfer. When nothing escalated there is no
        handoff regardless.
        """
        try:
            handoff_requested = self._handoff_warranted(escalation_decision)
            self._finalizer().finalize(
                admin_id=req.admin_id,
                session_id=req.session_id,
                luciel_instance_id=req.luciel_instance_id,
                user_id=req.user_id,
                current_message=req.message,
                prior_customer_messages=req.recent_customer_messages,
                assistant_reply=assistant_reply,
                inbound_channel=req.channel,
                escalation_fired=escalation_fired,
                handoff_requested=handoff_requested,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "cognition finalization failed: exc_class=%s — turn unaffected",
                type(exc).__name__,
            )

    @staticmethod
    def _handoff_warranted(escalation_decision) -> bool:
        """Decide whether the firing escalation warrants a live takeover.

        §3.4.6: a live human handoff happens when the customer asked for
        a person OR Luciel decided a real-time takeover is needed. We map
        that to the EXPLICIT HUMAN REQUEST signal (the customer asking),
        which is the unambiguous takeover trigger today. Other signals
        (negative sentiment, low confidence, high-value lead) flag the
        lead for human follow-up but do not force a live transfer. Never
        raises — an unreadable decision degrades to "no live handoff."
        """
        if escalation_decision is None:
            return False
        try:
            from app.models.escalation_event import (
                SIGNAL_EXPLICIT_HUMAN_REQUEST,
            )

            return escalation_decision.signal == SIGNAL_EXPLICIT_HUMAN_REQUEST
        except Exception:  # noqa: BLE001
            return False

    def _finalizer(self):
        """Return the injected CognitionFinalizer, or build one lazily."""
        if self._cognition_finalizer is None:
            from app.cognition.finalizer import CognitionFinalizer

            self._cognition_finalizer = CognitionFinalizer()
        return self._cognition_finalizer

    # ------------------------------------------------------------------
    # PLAN → ACT → REFLECT
    # ------------------------------------------------------------------

    def _run_plan_act_reflect(
        self,
        req: RuntimeRequest,
        base_prompt: str,
    ) -> "_LoopResult":
        """Drive the bounded plan→act→reflect loop.

        Each pass:
          * PLAN  — one ModelRouter.generate call, JSON-mode prompt +
                    tolerant parse → {reply, tool_calls, confidence}.
          * ACT   — dispatch any tool_calls through ToolBroker (gates
                    1+2). A gate-2 refusal is a structured
                    ToolResult(success=False) REFLECT reasons about.
          * REFLECT — if tools ran and any FAILED and budget remains,
                    re-enter PLAN with the tool outcomes appended so the
                    next plan can react. Otherwise stop.

        The bound is MAX_LOOP_ITERATIONS — a hard cost-control stop.
        Hitting it is recorded on the result (``bound_hit``) but is
        NEVER surfaced as escalation (§3.4.1 locked #17).
        """
        result = _LoopResult(reply="")
        prompt = base_prompt

        for iteration in range(1, MAX_LOOP_ITERATIONS + 1):
            result.iterations = iteration

            # PLAN
            plan = self._plan(prompt, result)

            result.reply = plan.reply
            result.confidence = plan.confidence

            # ACT
            tool_results = self._act(req, plan, result)

            # REFLECT — decide whether to re-enter PLAN.
            if not plan.tool_calls:
                # Nothing to act on: the PLAN reply is the answer. Stop.
                break

            if iteration >= MAX_LOOP_ITERATIONS:
                # Hard cost-control stop. NOT an escalation trigger
                # (§3.4.1 locked #17) — applies whether the trigger to
                # re-enter was a failure OR a pending success-synthesis.
                result.bound_hit = True
                break

            # Tools ran. Re-enter PLAN with the tool outcomes appended so
            # the next pass can react. This covers BOTH axes of "answer
            # satisfactory?":
            #   * FAILURE  — the plan reasons about the (gate-2 or
            #                execution) error and revises.
            #   * SUCCESS  — the SYNTHESIS pass (U4 carry-forward): the
            #                pre-tool draft was composed BEFORE the tool
            #                ran, so it cannot reflect the tool output.
            #                Feed the successful TOOL_RESULTS back and ask
            #                the model to weave them into the final reply.
            # The success path runs the synthesis pass exactly ONCE: the
            # synthesised reply carries no tool_calls (the model answers
            # from the results), so the next iteration's `not plan.tool_calls`
            # branch stops the loop. Synthesis is NOT exempt from the
            # 5-iteration bound (the iteration>=MAX guard above caps it).
            prompt = base_prompt + self._render_tool_feedback(tool_results)

        return result

    def _plan(self, prompt: str, result: "_LoopResult") -> Plan:
        """PLAN step — one ModelRouter.generate call + tolerant parse.

        Layers ``PLAN_JSON_INSTRUCTION`` onto the assembled prompt and
        parses the plain-text ``LLMResponse.content`` into a ``Plan``.
        Records the resolved provider/model on the loop result for the
        trace. On ANY LLM failure (no provider configured, all
        providers down) PLAN degrades to a low-confidence no-tool reply
        rather than crashing the turn (§3.4.1).
        """
        llm_request = LLMRequest(
            messages=[
                LLMMessage(role="system", content=prompt),
                LLMMessage(
                    role="user",
                    content=PLAN_JSON_INSTRUCTION.strip(),
                ),
            ]
        )
        try:
            response = self._router().generate(llm_request)
        except Exception as exc:  # noqa: BLE001
            # Provider-agnostic firewall: a PLAN call must never crash
            # the turn. Degrade to a graceful low-confidence reply.
            logger.warning(
                "PLAN LLM call failed: exc_class=%s — degrading turn",
                type(exc).__name__,
            )
            return Plan(
                reply=(
                    "I'm having trouble forming a response right now. "
                    "Please try again in a moment."
                ),
                tool_calls=[],
                confidence=0.0,
                parsed=False,
            )

        # Record provenance for the trace (last PLAN call wins — the
        # response Luciel actually emits came from this provider/model).
        result.llm_provider = response.provider
        result.llm_model = response.model
        return parse_plan(response.content)

    def _act(
        self,
        req: RuntimeRequest,
        plan: Plan,
        result: "_LoopResult",
    ):
        """ACT step — dispatch PLAN's tool_calls through the broker.

        Gates 1 (action-classification) + 2 (per-instance default-deny
        authorisation) are enforced INSIDE the broker. A gate-2 refusal
        comes back as a structured ``ToolResult(success=False, ...)``
        that REFLECT reasons about — the same shape the future gate-3
        refusal will use (founder decision #1). Gate 3 (connection) is
        ARC 17 and is NOT built here.

        Returns the list of ``ToolResult`` for REFLECT to evaluate.
        """
        if not plan.tool_calls:
            return []

        from app.tools.base import ToolContext

        # Arc 14 U5 — thread the Admin's tier + the per-instance enabled
        # channel set onto the ToolContext so the broker's gate-1
        # dispatch-time re-check (§3.3.3 hardening) can fully enforce.
        # Both are already resolved by the loop (tier for the OUTCOME
        # grounding floor; channels for the arbiter); reusing them here
        # closes the dispatch-time re-check seam in ToolAuthorizer
        # without any new lookup. Resolvers never raise (best-effort), so a failure
        # degrades to the WU2 skip-the-check baseline rather than the
        # turn crashing.
        context = ToolContext(
            admin_id=req.admin_id,
            instance_id=req.luciel_instance_id or 0,
            inbound_message_id=req.session_id,
            admin_tier=self._resolve_tier(req),
            enabled_channels=frozenset(self._resolve_enabled_channels(req)),
        )

        tool_results = []
        broker = self._broker()
        for call in plan.tool_calls:
            tool_result = broker.execute_tool(
                call.tool,
                call.parameters,
                context=context,
            )
            tool_results.append(tool_result)
            result.tool_called = True
            result.tool_name = call.tool
        return tool_results

    @staticmethod
    def _render_tool_feedback(tool_results) -> str:
        """Render tool outcomes as a prompt stanza fed back into the
        next PLAN pass so the model can react to failures (incl. gate-2
        refusals). Kept compact and bounded — one line per tool."""
        lines = ["\n\nTOOL_RESULTS:"]
        for r in tool_results:
            status = "ok" if r.success else "error"
            detail = r.output if r.success else (r.error or "tool refused")
            lines.append(f"- [{status}] {detail}")
        lines.append(
            "Revise your reply taking these tool results into account."
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Lazy dependency builders
    # ------------------------------------------------------------------

    def _router(self):
        """Return the injected ModelRouter, or build one lazily.

        Lazy construction keeps pre-ARC-14 call sites (no LLM kwarg)
        working. A lazily-built router with no configured provider will
        raise inside ``generate``; PLAN catches that and degrades.
        """
        if self._model_router is None:
            from app.integrations.llm.router import ModelRouter

            self._model_router = ModelRouter()
        return self._model_router

    def _broker(self):
        """Return the injected ToolBroker, or build one lazily over the
        production ToolRegistry. Gates 1+2 are enforced inside the
        broker; the loop does not duplicate them."""
        if self._tool_broker is None:
            from app.tools.broker import ToolBroker
            from app.tools.registry import ToolRegistry

            self._tool_broker = ToolBroker(ToolRegistry())
        return self._tool_broker

    def _judge(self):
        """Return the injected EscalationJudge, or build one lazily.

        A lazily-built judge has no injected classifiers; its LLM-backed
        classifiers degrade to neutral (non-firing) when no provider is
        configured, so a missing provider never invents an escalation."""
        if self._escalation_judge is None:
            from app.runtime.escalation_judge import EscalationJudge

            # Share the orchestrator's router so the judge's lazily-built
            # classifiers ride the SAME provider channel as PLAN. In a
            # test that injects a stub/boom router this guarantees no
            # live API call from the judge (boom → neutral → no fire).
            self._escalation_judge = EscalationJudge(model_router=self._router())
        return self._escalation_judge

    def _escalation_svc(self):
        """Return the injected EscalationService, or build one lazily."""
        if self._escalation_service is None:
            from app.policy.escalation import EscalationService

            self._escalation_service = EscalationService()
        return self._escalation_service

    def _escalation_delivery_svc(self):
        """Return the injected EscalationDeliveryService, or build one lazily.

        Rescan Tier-C — the delivery service sends real admin notifications
        (email/SMS/Slack) when CHANNELS_LIVE_PROVISIONING_ENABLED is True;
        records full routing+attempt decision in dry-run when False.
        """
        if self._escalation_delivery_service is None:
            from app.services.escalation_delivery_service import (
                EscalationDeliveryService,
            )

            self._escalation_delivery_service = EscalationDeliveryService()
        return self._escalation_delivery_service

    # ------------------------------------------------------------------
    # Retrieve step (ARC 11 Step 8 — preserved verbatim)
    # ------------------------------------------------------------------

    def _retrieve(self, req: RuntimeRequest) -> list:
        """Open a tenant-scoped session, build the retriever, return
        the chunk list. Architecture v1 §3.2 retrieval flow:

          1. Filter by admin_id, instance_id, ingestion_status=ready
             (already enforced inside ``search_similar``).
          2. Vector similarity (top-k).
          3. Return chunks in relevance order.

        Never raises — retrieval failure must not block the
        conversation per Architecture §3.4. Caught exceptions log
        their class name plus the first 8 chars of admin_id (the
        PII-discipline floor from Step 6); the response continues
        with an empty chunk list.
        """
        try:
            from app.db.session import SessionLocal
            from app.db.tenant_scope import bind_tenant_scope
            from app.knowledge.retriever import KnowledgeRetriever
            from app.repositories.knowledge_repository import KnowledgeRepository
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "Retrieve step deps unavailable: exc_class=%s — "
                "returning empty chunk list",
                type(exc).__name__,
            )
            return []

        admin_prefix = (req.admin_id or "")[:8]
        try:
            with bind_tenant_scope(
                admin_id=req.admin_id,
                instance_id=req.luciel_instance_id,
            ):
                db = SessionLocal()
                try:
                    repo = KnowledgeRepository(db)
                    retriever = KnowledgeRetriever(repo)
                    return retriever.retrieve_with_sources(
                        query=req.message,
                        admin_id=req.admin_id,
                        luciel_instance_id=req.luciel_instance_id,
                        limit=5,
                    )
                finally:
                    db.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Retrieve step failed: exc_class=%s admin_prefix=%s "
                "instance_id=%s — returning empty chunk list",
                type(exc).__name__, admin_prefix, req.luciel_instance_id,
            )
            return []

    @staticmethod
    def _grounding_from_chunks(
        chunks: Sequence, answer: str = ""
    ) -> float | None:
        """Derive a [0,1] composite grounding score from retrieved chunks.

        §3.4.13 requires a COMPOSITE of two components:

          (a) Retrieval relevance  — ``1 - best_cosine_distance`` (best =
              smallest distance = closest match) using the chunk distances
              already computed during retrieval. Measures how well the top
              retrieved chunk matched the query.

          (b) Citation overlap  — fraction of the answer's sentences whose
              token-overlap with any retrieved chunk exceeds a threshold.
              For each sentence we compute Jaccard similarity of its
              unigram set against the unigram set of each chunk's content;
              a sentence is "covered" when any chunk exceeds the threshold.
              This is deterministic, dependency-free, and cheap (no
              embedding call). Coverage = covered_sentences / total_sentences.
              An empty answer or an answer with no extractable sentences
              contributes a citation-overlap of 0.0.

        Combination: weighted average with equal weights 0.5/0.5:

            grounding = 0.5 * retrieval_relevance + 0.5 * citation_overlap

        The combined score is clamped to [0,1]. Returns ``None`` when
        nothing was retrieved (the OUTCOME gate treats None as below every
        floor only in concert with the retrieval-failed flag). If chunks
        exist but none carry a distance, retrieval_relevance is 0.0 (no
        distance information means we cannot claim relevance) and citation
        overlap is still computed against chunk content. It never raises:
        a malformed chunk degrades gracefully.

        Citation-overlap threshold: CITATION_JACCARD_THRESHOLD = 0.10.
        This is deliberately low so that any meaningful vocabulary overlap
        between an answer sentence and a chunk counts as a citation hit;
        a higher threshold would produce false negatives on paraphrased
        answers. The threshold is a module constant so it can be tuned
        from audit data without changing the algorithm.
        """
        if not chunks:
            return None
        try:
            # --- (a) Retrieval relevance ---
            distances = [
                c.distance
                for c in chunks
                if getattr(c, "distance", None) is not None
            ]
            if distances:
                best = min(distances)
                retrieval_relevance = max(0.0, min(1.0, 1.0 - float(best)))
            else:
                retrieval_relevance = 0.0

            # --- (b) Citation overlap ---
            citation_overlap = LucielOrchestrator._citation_overlap(
                answer, chunks
            )

            # --- Combine (0.5 / 0.5 weighted average) ---
            grounding = 0.5 * retrieval_relevance + 0.5 * citation_overlap
            return max(0.0, min(1.0, grounding))
        except Exception:  # noqa: BLE001
            return None

    # Citation-overlap threshold (Jaccard similarity). A sentence is
    # "covered" when its unigram Jaccard against any chunk >= this value.
    _CITATION_JACCARD_THRESHOLD: float = 0.10

    @staticmethod
    def _citation_overlap(answer: str, chunks: Sequence) -> float:
        """Fraction of answer sentences whose unigram Jaccard similarity
        to at least one retrieved chunk exceeds _CITATION_JACCARD_THRESHOLD.

        Algorithm (deterministic, no external deps):
          1. Tokenise by splitting on whitespace/punctuation to lowercase
             unigrams. Strip common punctuation so "fact." and "fact" match.
          2. For each answer sentence, compute Jaccard against every chunk
             and mark it covered if any pair exceeds the threshold.
          3. Return covered_count / total_sentences, or 0.0 when the answer
             has no usable sentences.
        """
        import re

        def _tokens(text: str) -> frozenset:
            return frozenset(w.lower() for w in re.split(r"[\s,.!?;:]+", text) if w)

        # Split answer into sentences on '.', '!', '?' or newlines.
        sentences = [
            s.strip()
            for s in re.split(r"(?<=[.!?])\s+|\n+", answer.strip())
            if s.strip()
        ]
        if not sentences:
            return 0.0

        # Pre-compute chunk token sets (defensive: use content attr or str).
        chunk_token_sets = []
        for c in chunks:
            content = getattr(c, "content", None) or getattr(c, "formatted", "") or ""
            if content:
                chunk_token_sets.append(_tokens(str(content)))
        if not chunk_token_sets:
            return 0.0

        threshold = LucielOrchestrator._CITATION_JACCARD_THRESHOLD
        covered = 0
        for sent in sentences:
            sent_tokens = _tokens(sent)
            if not sent_tokens:
                continue
            for chunk_tokens in chunk_token_sets:
                union = sent_tokens | chunk_tokens
                if not union:
                    continue
                jaccard = len(sent_tokens & chunk_tokens) / len(union)
                if jaccard >= threshold:
                    covered += 1
                    break
        return covered / len(sentences)

    @staticmethod
    def _collect_source_pks(chunks: Sequence) -> list[int]:
        """Step 5 helper: ``int | str | None`` source_identifiers →
        deduped ``list[int]``. Defensive: if the helper itself is
        unavailable (truncated install, broken import), fall back
        to ``[]`` rather than crashing the turn."""
        try:
            from app.knowledge.retriever import collect_source_pks

            return collect_source_pks(chunks)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "collect_source_pks failed: exc_class=%s — using []",
                type(exc).__name__,
            )
            return []

    # ------------------------------------------------------------------
    # Trace persistence (ARC 11 Step 8 — extended to fill ARC 14 fields)
    # ------------------------------------------------------------------

    def _record_trace_best_effort(
        self,
        *,
        req: RuntimeRequest,
        assistant_reply: str,
        source_ids: list[int],
        llm_provider: str | None = None,
        llm_model: str | None = None,
        tool_called: bool = False,
        tool_name: str | None = None,
        escalated: bool = False,
    ) -> str:
        """Persist a trace via TraceService. Returns the trace_id —
        either the one record_trace minted, or a fresh ``uuid4()``
        if the write failed.

        ARC 14 U1 fills the ``llm_provider`` / ``llm_model`` /
        ``tool_called`` / ``tool_name`` / ``escalated`` fields the
        ARC 11 stub left None/False. The write stays best-effort: the
        chat path NEVER breaks because of a trace write failure
        (Architecture §5.1 — observability is a side-effect).
        """
        admin_prefix = (req.admin_id or "")[:8]
        try:
            trace_service = self._resolve_trace_service()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "TraceService unavailable: exc_class=%s admin_prefix=%s",
                type(exc).__name__, admin_prefix,
            )
            return str(uuid4())

        try:
            return trace_service.record_trace(
                session_id=req.session_id,
                user_id=req.user_id,
                admin_id=req.admin_id,
                user_message=req.message,
                assistant_reply=assistant_reply,
                # ARC 14 U1 — the loop now makes a real PLAN call, so
                # the provider/model are known (None only when the loop
                # degraded without an LLM call).
                llm_provider=llm_provider,
                llm_model=llm_model,
                memories_retrieved=0,
                memories_used=None,
                tool_called=tool_called,
                tool_name=tool_name,
                escalated=escalated,
                policy_flags=None,
                memories_extracted=0,
                luciel_instance_id=req.luciel_instance_id,
                source_ids_used=source_ids,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "record_trace failed: exc_class=%s admin_prefix=%s — "
                "returning fresh uuid4 for response.trace_id",
                type(exc).__name__, admin_prefix,
            )
            return str(uuid4())

    def _resolve_trace_service(self):
        """Return the injected TraceService, or build one lazily.

        The lazy path keeps existing tests + chat-path call sites
        that construct ``LucielOrchestrator()`` with no kwargs
        working unchanged. The lazy-built service opens its own
        SessionLocal; the trace write commits independently of
        any caller transaction.
        """
        if self._trace_service is not None:
            return self._trace_service

        from app.db.session import SessionLocal
        from app.repositories.trace_repository import TraceRepository
        from app.services.trace_service import TraceService

        db = SessionLocal()
        repo = TraceRepository(db)
        return TraceService(repo)


# =====================================================================
# Internal loop-state carrier
# =====================================================================


class _LoopResult:
    """Mutable accumulator for one ``run`` invocation's loop state.

    Carries what RESPOND + FINALIZE need: the final reply + confidence,
    the resolved provider/model, whether a tool was dispatched, the
    iteration count, and whether the cost-control bound was hit. Kept
    as a plain mutable object (not a frozen dataclass) because the loop
    updates it across passes.
    """

    __slots__ = (
        "reply",
        "confidence",
        "llm_provider",
        "llm_model",
        "tool_called",
        "tool_name",
        "iterations",
        "bound_hit",
        # Arc 14 U2 — inputs the §3.4.5 OUTCOME gate reads. NONE of
        # these is bound_hit: hitting the iteration cap is cost-control,
        # never an escalation trigger (§3.4.1 locked #17).
        "grounding_score",
        "retrieval_failed",
        "lead_value",
        # RESCAN TIER-C — weighted composite lead score [0, 1] from
        # lead_capture.detect(), populated before the OUTCOME gate.
        "lead_score",
    )

    def __init__(self, *, reply: str) -> None:
        self.reply = reply
        self.confidence: float = 0.0
        self.llm_provider: str | None = None
        self.llm_model: str | None = None
        self.tool_called: bool = False
        self.tool_name: str | None = None
        self.iterations: int = 0
        self.bound_hit: bool = False
        # §3.4.5 OUTCOME-gate inputs. grounding_score None = retrieval
        # did not run; retrieval_failed True = CONTEXT-step Retrieve leg
        # errored or returned nothing (spec item 5 contributing signal);
        # lead_value = extracted high-value-lead budget figure (e.g. 5000);
        # lead_score = weighted composite [0, 1] (RESCAN TIER-C).
        self.grounding_score: float | None = None
        self.retrieval_failed: bool = False
        self.lead_value: float | None = None
        self.lead_score: float = 0.0
