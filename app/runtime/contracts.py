"""Runtime contracts — the in/out shape for ``LucielOrchestrator.run``.

Arc 11 Step 8 extends the request with ``luciel_instance_id`` (the
retriever needs it to scope its query) and the response with
``source_ids_used`` (the per-turn source provenance, also written
to ``traces.source_ids_used`` so the §3.2.2 delete-modal preview
can light up).

Arc 12 EX1d (founder-directed agent_id/domain_id excision): the
v1 ``domain_id`` field is removed from ``RuntimeRequest``. v2 has
a single Admin→Instance boundary (Architecture §3.7.2); the prompt
no longer carries a Domain layer and the orchestrator no longer
threads a Domain value into the trace write.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RuntimeRequest:
    message: str
    session_id: str
    user_id: str | None
    admin_id: str
    channel: str
    # Arc 11 Step 8 — needed by KnowledgeRetriever for Wall-3 scoping.
    # Defaults None so existing call sites (chat path stubs, tests
    # that build RuntimeRequest positionally) keep working unchanged.
    # When None the orchestrator skips the Retrieve step regardless
    # of the feature flag (no instance ⇒ nothing to retrieve from).
    luciel_instance_id: int | None = None
    # Arc 14 U2 — trailing customer-message window for the §3.4.5
    # strong-negative-sentiment intake signal, oldest→newest, NOT
    # including the current ``message``. Defaults empty so every
    # existing call site (which has no history surface yet) keeps
    # working: with an empty window the sentiment signal evaluates the
    # current message alone. A caller that has session history can
    # supply the trailing customer turns here.
    recent_customer_messages: list[str] = field(default_factory=list)


@dataclass
class RuntimeResponse:
    message: str
    trace_id: str
    confidence: float
    session_id: str
    intent_summary: str | None = None
    escalation_flag: bool = False
    # Arc 11 Step 8 — per-turn source provenance. The orchestrator
    # populates it from ``collect_source_pks(chunks)`` when the
    # retriever ran; ``[]`` otherwise. Same value is written into
    # ``traces.source_ids_used`` via TraceService.record_trace.
    source_ids_used: list[int] = field(default_factory=list)
    # Arc 14 U1 — agentic-loop observability. Additive with defaults so
    # every existing positional/keyword call site keeps working.
    #
    #   llm_provider / llm_model — which provider+model the PLAN call
    #       resolved to (None when the loop degraded without an LLM
    #       call, e.g. no provider configured in a unit test).
    #   tool_called / tool_name  — whether ACT dispatched at least one
    #       tool through the broker this turn, and the last tool id.
    #   iterations               — how many PLAN→ACT→REFLECT passes ran
    #       (1..MAX). Bounded by the doctrinal cap of 5 (§3.4.1).
    #   bound_hit                — True iff the loop stopped because it
    #       reached the iteration cap. This is cost-control ONLY and is
    #       explicitly NOT an escalation trigger (§3.4.1 locked #17).
    llm_provider: str | None = None
    llm_model: str | None = None
    tool_called: bool = False
    tool_name: str | None = None
    iterations: int = 0
    bound_hit: bool = False
