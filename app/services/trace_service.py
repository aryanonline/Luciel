"""
Trace service.

Creates and stores trace records for each chat turn (Architecture
v1 §5.1 — Observability).

Arc 12 EX1d (founder-directed agent_id/domain_id excision): the
``agent_id`` / ``domain_id`` parameters were removed from
``record_trace``. v2 has a single Admin→Instance boundary
(Architecture §3.7.2); ``traces.agent_id`` / ``traces.domain_id``
ORM columns persist until EX3 drops them and are written NULL
on every new row.

Arc 11 Step 5 update — ``source_ids_used``:
    Each trace now carries the list of ``knowledge_sources.id``
    rows whose chunks contributed to this turn's retrieval. This
    backs the Architecture v1 §3.2.2 delete-confirm modal preview
    ("a preview of what customer questions referenced this source
    recently, drawn from the trace store"). The column itself is
    a ``BIGINT[]`` with a GIN index (Arc 11 Step 1 schema;
    ``ix_traces_source_ids_used``), so the read path
    (``TraceRepository.list_recent_traces_using_source``, used by
    the Step 7 ``GET /sources/{id}/affected-questions`` endpoint)
    can do a fast ``@>`` containment lookup per source.

The orchestrator (Step 8) computes the list by calling
``app.runtime.knowledge_retrieval.collect_source_pks(chunks)`` between
the retrieve step and the trace write. The legacy ``memories_used``
column is unrelated — memories are conversation-history items;
sources are knowledge-base sources. They stay independent.
"""

from __future__ import annotations

import logging
import uuid

from app.models.trace import Trace
from app.repositories.trace_repository import TraceRepository

logger = logging.getLogger(__name__)


class TraceService:

    def __init__(self, repository: TraceRepository) -> None:
        self.repository = repository

    def record_trace(
        self,
        *,
        session_id: str,
        user_id: str | None,
        admin_id: str,
        user_message: str,
        assistant_reply: str,
        llm_provider: str | None = None,
        llm_model: str | None = None,
        memories_retrieved: int = 0,
        memories_used: list[str] | None = None,
        tool_called: bool = False,
        tool_name: str | None = None,
        escalated: bool = False,
        policy_flags: list[str] | None = None,
        memories_extracted: int = 0,
        luciel_instance_id: int | None = None,   # Step 24.5 File 15
        source_ids_used: list[int] | None = None,  # Arc 11 Step 5
    ) -> str:
        """Create and persist a trace record.

        Arc 11 Step 5: ``source_ids_used`` is an optional list of
        ``knowledge_sources.id`` rows that contributed chunks to
        this turn. ``None`` is normalised to ``[]`` to match the
        DB ``server_default '{}'``. The orchestrator (Step 8)
        builds the list by passing the chunks
        ``KnowledgeRetriever.retrieve_with_sources(...)`` returned
        through ``app.runtime.knowledge_retrieval.collect_source_pks``,
        which dedupes and filters out string / None identifiers.

        Returns the trace_id for reference.
        """
        trace_id = str(uuid.uuid4())

        trace = Trace(
            trace_id=trace_id,
            session_id=session_id,
            user_id=user_id,
            admin_id=admin_id,
            user_message=user_message,
            assistant_reply=assistant_reply,
            llm_provider=llm_provider,
            llm_model=llm_model,
            memories_retrieved=memories_retrieved,
            memories_used=memories_used,
            tool_called=tool_called,
            tool_name=tool_name,
            escalated=escalated,
            policy_flags=policy_flags,
            memories_extracted=memories_extracted,
            luciel_instance_id=luciel_instance_id,   # Step 24.5 File 15
            # Arc 11 Step 5 — Architecture v1 §5.1. ``None`` becomes
            # ``[]`` so the ORM write matches the column's
            # ``NOT NULL DEFAULT '{}'`` shape; a stored ``[]`` is
            # also what the §3.2.2 modal preview expects to see for
            # any trace that fired before the orchestrator wiring
            # in Step 8.
            source_ids_used=list(source_ids_used) if source_ids_used else [],
        )

        try:
            self.repository.save_trace(trace)
            logger.info("Trace recorded: %s", trace_id)
        except Exception as exc:
            logger.warning("Failed to save trace: %s", exc)

        return trace_id
