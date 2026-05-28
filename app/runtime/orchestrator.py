"""Luciel orchestrator — Arc 11 Step 8: Retrieve step + trace wiring.

Architecture v1 §6 (Arc Delta) names Arc 11 the home of "retriever-
into-orchestrator wiring." The full agentic loop (PLAN, ACT,
REFLECT, escalation judgment, tool dispatch) is Arc 14. Step 8
threads the Retrieve step through the existing stub and adds the
TraceService wiring so traces — including the per-turn
``source_ids_used`` — actually get persisted.

Behaviour summary
-----------------

  * Retrieval is gated by ``settings.knowledge_retrieval_enabled``
    (defaults False). When closed, the Retrieve step short-circuits
    and the orchestrator returns the same stub message it always
    did, with ``source_ids_used=[]``.
  * When the flag is open AND the request carries a
    ``luciel_instance_id``, the orchestrator builds a
    ``KnowledgeRetriever`` against a freshly-opened ``SessionLocal()``
    INSIDE a ``bind_tenant_scope(...)`` block. The session lives
    for the duration of the retrieve call and is closed before any
    other work. This keeps Wall-1 + Wall-3 GUCs bound from the
    very first BEGIN — required by Arc 9 C4.4 doctrine for non-
    HTTP callers (Celery workers, future background jobs).
  * Retrieval failure does NOT block the conversation. The
    Architecture §3.4 stance is that Luciel still replies; the
    "couldn't answer confidently" escalation lives in Arc 14, not
    here. Step 8 catches the exception, logs the class name +
    admin prefix (no PII), and returns an empty chunk list.
  * After the stub response is composed, ``TraceService.record_trace``
    is invoked with the per-turn ``source_ids_used``. The trace
    write is best-effort — if the DB rejects it (RLS misconfig,
    transient connection loss), the orchestrator logs and falls
    back to a fresh ``uuid4()`` for the response's ``trace_id`` so
    the chat path is never broken by an observability failure.
"""
from __future__ import annotations

import logging
from typing import Sequence
from uuid import uuid4

from app.core.config import settings
from app.runtime.context_assembler import ContextAssembler
from app.runtime.contracts import RuntimeRequest, RuntimeResponse

logger = logging.getLogger(__name__)


class LucielOrchestrator:
    """Runtime entry point. Arc-11-aware: optionally retrieves and
    persists a trace on every turn."""

    def __init__(self, *, trace_service=None) -> None:
        """``trace_service`` is optional so existing callers that
        construct ``LucielOrchestrator()`` with no args keep working.
        When ``None``, the orchestrator builds a fresh TraceService
        on demand from a transient ``SessionLocal()``."""
        self.context = ContextAssembler()
        self._trace_service = trace_service

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self, req: RuntimeRequest) -> RuntimeResponse:
        chunks: list = []
        source_ids: list[int] = []

        # 1. Retrieve step — flag-gated.
        if (
            settings.knowledge_retrieval_enabled
            and req.luciel_instance_id is not None
        ):
            chunks = self._retrieve(req)
            source_ids = self._collect_source_pks(chunks)

        # 2. Compose the prompt (always — chunks may be empty).
        _prompt = self.context.build_prompt(req, retrieved_chunks=chunks)

        # 3. Stub response. Arc 14 replaces this with the agentic loop.
        message = (
            "I understand. I will help clarify what matters most and "
            "guide the next step with precision. "
            f"For now, I have received your request: {req.message}"
        )

        # 4. Persist trace. Best-effort — never blocks the response.
        trace_id = self._record_trace_best_effort(
            req=req,
            assistant_reply=message,
            source_ids=source_ids,
        )

        return RuntimeResponse(
            message=message,
            trace_id=trace_id,
            confidence=0.72,
            session_id=req.session_id,
            intent_summary="Initial user intent captured",
            escalation_flag=False,
            source_ids_used=source_ids,
        )

    # ------------------------------------------------------------------
    # Retrieve step
    # ------------------------------------------------------------------

    def _retrieve(self, req: RuntimeRequest) -> list:
        """Open a tenant-scoped session, build the retriever, return
        the chunk list. Architecture v1 §3.2 retrieval flow:

          1. Filter by admin_id, instance_id, ingestion_status=ready
             (already enforced inside ``search_similar``).
          2. Vector similarity (top-k).
          3. Return chunks in relevance order.

        ``top_k = 5`` is the v1 ceiling per the brief; Arc 14 may
        promote it to a per-tier setting once the agentic loop's
        prompt budget arithmetic is real.

        Never raises — retrieval failure must not block the
        conversation per Architecture §3.4. Caught exceptions log
        their class name plus the first 8 chars of admin_id (the
        PII-discipline floor from Step 6); the response continues
        with an empty chunk list.
        """
        # Lazy imports so the module is importable without the
        # knowledge / DB stack being initialised (matters for the
        # existing tests that import LucielOrchestrator without a
        # full app boot).
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
            # Architecture §3.4: do not block on retrieval failure.
            # Log opaque metadata only — admin_id is truncated, the
            # exception message is NOT logged (it can carry caller-
            # supplied content).
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
    # Trace persistence
    # ------------------------------------------------------------------

    def _record_trace_best_effort(
        self,
        *,
        req: RuntimeRequest,
        assistant_reply: str,
        source_ids: list[int],
    ) -> str:
        """Persist a trace via TraceService. Returns the trace_id —
        either the one record_trace minted, or a fresh ``uuid4()``
        if the write failed.

        Failure modes are best-effort logged; the chat path NEVER
        breaks because of a trace write failure (Architecture §5.1
        observability is a side-effect, not a critical path).
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
                domain_id=req.domain_id,
                agent_id=None,
                user_message=req.message,
                assistant_reply=assistant_reply,
                # Arc 14 will fill these. For now the orchestrator
                # has no LLM call so provider / model are None.
                llm_provider=None,
                llm_model=None,
                memories_retrieved=0,
                memories_used=None,
                tool_called=False,
                tool_name=None,
                escalated=False,
                policy_flags=None,
                memories_extracted=0,
                luciel_instance_id=req.luciel_instance_id,
                # Arc 11 Step 5 — the per-turn source provenance.
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

        # Lazy import to keep the orchestrator importable even if
        # the trace stack is unavailable (test isolation).
        from app.db.session import SessionLocal
        from app.repositories.trace_repository import TraceRepository
        from app.services.trace_service import TraceService

        db = SessionLocal()
        repo = TraceRepository(db)
        return TraceService(repo)
