"""
Trace service.

Creates and stores trace records for each chat turn.
Now includes tenant/domain config references and
the actual memories that were used.
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
        domain_id: str | None = None,
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
        tenant_config_id: int | None = None,
        domain_config_id: int | None = None,
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
            domain_id=domain_id,
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
            tenant_config_id=tenant_config_id,
            domain_config_id=domain_config_id,
        )

        try:
            self.repository.save_trace(trace)
            logger.info("Trace recorded: %s", trace_id)
        except Exception as exc:
            logger.warning("Failed to save trace: %s", exc)

        return trace_id