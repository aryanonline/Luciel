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
