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
    ) -> None:
        self.context = ContextAssembler()
        self._trace_service = trace_service
        self._model_router = model_router
        self._tool_broker = tool_broker

    # ------------------------------------------------------------------
    # Public entry point — the §3.4.1 agentic loop
    # ------------------------------------------------------------------

    def run(self, req: RuntimeRequest) -> RuntimeResponse:
        # 1. RECEIVE — the resolved (admin, instance, session) is on req.
        # 2. CONTEXT ASSEMBLY — flag-gated Retrieve + prompt composition.
        chunks: list = []
        source_ids: list[int] = []
        if (
            settings.knowledge_retrieval_enabled
            and req.luciel_instance_id is not None
        ):
            chunks = self._retrieve(req)
            source_ids = self._collect_source_pks(chunks)

        base_prompt = self.context.build_prompt(req, retrieved_chunks=chunks)

        # 3. ESCALATION GATE 1 — INTAKE (pre-PLAN). PASS-THROUGH in U1.
        if self._intake_gate(req):  # pragma: no cover — always False in U1
            # U2 fills this branch: emit a Gate-1 handoff acknowledgement
            # (templated per persona preset) and SKIP plan/act/reflect.
            pass

        # 4. PLAN → ACT → REFLECT — bounded at MAX_LOOP_ITERATIONS.
        loop = self._run_plan_act_reflect(req, base_prompt)

        # 5. ESCALATION GATE 2 — OUTCOME (post-REFLECT). PASS-THROUGH.
        escalation_flag = self._outcome_gate(req, loop)

        # 6. RESPOND — U1 emits on the inbound channel; the §3.4.2
        #    channel arbiter is U3.
        message = loop.reply

        # 7. COGNITION FINALIZATION — fill the trace fields the stub
        #    left None/False. (Folding CognitionService behaviours is U4.)
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
        )

    # ------------------------------------------------------------------
    # Escalation gate seams — PASS-THROUGH in U1, U2 fills them
    # ------------------------------------------------------------------

    def _intake_gate(self, req: RuntimeRequest) -> bool:
        """ESCALATION GATE 1 — INTAKE (pre-PLAN). §3.4.1 step 3.

        Evaluates the two signals knowable from the inbound message
        alone (explicit human request; strong negative sentiment).
        PASS-THROUGH in U1 (always returns False); U2 wires the real
        NLU + sentiment classifiers. Returning False means "do not
        short-circuit — proceed into PLAN".
        """
        return False

    def _outcome_gate(self, req: RuntimeRequest, loop: "_LoopResult") -> bool:
        """ESCALATION GATE 2 — OUTCOME (post-REFLECT). §3.4.1 step 5.

        Evaluates the two signals needing loop output (cannot
        confidently answer; high-value lead). PASS-THROUGH in U1
        (always returns False). U2 wires the real signals and the
        escalation flow.

        Doctrine guard (§3.4.1 locked #17): hitting the iteration bound
        is cost-control, NOT an escalation trigger. This stub returns
        False regardless of ``loop.bound_hit`` so the invariant holds
        from U1 onward and U2 inherits it.
        """
        return False

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
            any_failure = any(not r.success for r in tool_results)
            if not plan.tool_calls or not any_failure:
                # Either nothing to act on, or every tool succeeded:
                # the answer is taken as satisfactory. Stop.
                break

            if iteration >= MAX_LOOP_ITERATIONS:
                # Hard cost-control stop. NOT an escalation trigger.
                result.bound_hit = True
                break

            # Re-enter PLAN with the tool outcomes appended so the next
            # plan can reason about the (gate-2 or execution) failures.
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

        context = ToolContext(
            admin_id=req.admin_id,
            instance_id=req.luciel_instance_id or 0,
            inbound_message_id=req.session_id,
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
