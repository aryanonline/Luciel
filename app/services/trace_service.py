"""
Trace service.

Creates and stores trace records for each chat turn.
The trace captures the full decision path so you can
debug and improve Luciel over time.

Usage:
    The ChatService calls trace_service.record_trace() at the end
    of each turn with all the metadata from that request.
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
        tenant_id: str,
        user_message: str,
        assistant_reply: str,
        llm_provider: str | None = None,
        llm_model: str | None = None,
        memories_retrieved: int = 0,
        tool_called: bool = False,
        tool_name: str | None = None,
        escalated: bool = False,
        policy_flags: list[str] | None = None,
        memories_extracted: int = 0,
    ) -> str:
        """
        Create and persist a trace record.

        Returns the trace_id for reference.
        """
        trace_id = str(uuid.uuid4())

        trace = Trace(
            trace_id=trace_id,
            session_id=session_id,
            user_id=user_id,
            tenant_id=tenant_id,
            user_message=user_message,
            assistant_reply=assistant_reply,
            llm_provider=llm_provider,
            llm_model=llm_model,
            memories_retrieved=memories_retrieved,
            tool_called=tool_called,
            tool_name=tool_name,
            escalated=escalated,
            policy_flags=policy_flags,
            memories_extracted=memories_extracted,
        )

        try:
            self.repository.save_trace(trace)
            logger.info("Trace recorded: %s", trace_id)
        except Exception as exc:
            # Trace failure should never break the chat flow.
            logger.warning("Failed to save trace: %s", exc)

        return trace_id