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
    # Arc 14 U3 — §3.4.2 channel arbiter. When the customer explicitly
    # asks to switch channel ("text me", "email me instead"), the
    # resolved channel id lands here and the arbiter's customer-initiated
    # switch rule honours it (always wins, subject to enablement).
    # Defaults None so every existing call site keeps working: no
    # explicit request ⇒ the arbiter falls through to its other rules.
    customer_requested_channel: str | None = None
    # RESCAN CORE(serving-path) — HYBRID persona threading. ChatService
    # is now a thin adapter: it resolves the per-turn persona (the §3.5.1
    # composed PRESET + BUSINESS_CONTEXT stanzas, the instance display
    # name, the preferred provider) and the consent-gated user memories,
    # then hands them to the orchestrator on the request so the PLAN
    # prompt is persona-aware. ALL additive with defaults so every
    # existing call site (and all 14 orchestrator test files, which build
    # RuntimeRequest without these) keeps working byte-identically: with
    # the defaults the orchestrator's prompt is exactly the pre-rewiring
    # generic ContextAssembler prompt.
    persona_preset_stanza: str | None = None
    persona_business_context_stanza: str | None = None
    assistant_name: str = "Luciel"
    memories: list[str] = field(default_factory=list)
    # Preferred LLM provider for the PLAN call (instance.preferred_provider
    # or caller override). None ⇒ ModelRouter picks its default, the
    # pre-rewiring behaviour.
    provider: str | None = None
    # RESCAN CORE(serving-path) GAP-6/R1 — opt-in lifecycle enforcement.
    # The live serving path (the ChatService adapter) sets this True so
    # the orchestrator runs the §3.6.1/§3.6.2 lifecycle gate FIRST (before
    # human-controlled / budget / PLAN): a non-active or missing instance
    # short-circuits to a lifecycle no-op with NO LLM call and NO budget
    # accrual. Defaults False so the 14 orchestrator unit tests — which
    # build RuntimeRequest with a luciel_instance_id but no live instance
    # row — are unaffected (they exercise the gates that come AFTER this
    # one and must not be blocked by a missing test fixture row). The
    # channel webhooks (widget/SMS) already gate at their route layer via
    # check_instance_lifecycle and need not re-set this, but doing so is
    # harmless (idempotent — an active instance returns None).
    enforce_lifecycle_gate: bool = False


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
    # Arc 14 U3 — §3.4.2 channel arbiter outcome. ``response_channel`` is
    # the channel the RESPOND step emitted on (the arbiter's pick, which
    # defaults to the inbound channel). ``prompt_channel_switch`` is the
    # permission-prompt marker: True only when a long SMS reply was moved
    # to email and the customer should be asked before delivery on the
    # new channel. Additive with defaults so existing call sites keep
    # working; ``response_channel`` None means the arbiter did not run
    # (e.g. a pre-loop short-circuit path that has no inbound channel).
    response_channel: str | None = None
    prompt_channel_switch: bool = False
    # RESCAN CORE(serving-path) GAP-6/R1 — lifecycle no-op marker. The
    # orchestrator's pre-run lifecycle gate (§3.6.1/§3.6.2) sets this True
    # and returns an EMPTY-message response when the resolved instance is
    # not ACTIVE (paused / deactivating / grace_window / inactive /
    # missing). The channel/adapter layer maps that onto its documented
    # no-op shape (widget 204 empty, SMS 204 drop, /chat empty reply) and
    # makes NO LLM call and accrues NO budget. Defaults False so every
    # existing call site and the 14 orchestrator tests are unaffected:
    # the gate only fires when an instance id resolves to a non-active
    # row, which the deterministic unit tests never set up.
    lifecycle_blocked: bool = False
    lifecycle_status: str | None = None
