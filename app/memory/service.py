"""
MemoryService — tenant-scoped memory read/write with async extraction
support (Step 27b).

Public API:
    retrieve_memories(...)        unchanged since Step 18
    extract_and_save(...)         sync extraction (legacy path)
    enqueue_extraction(...)       async extraction via Celery (Step 27b)

The ChatService chooses between sync and async based on
settings.memory_extraction_async. MemoryService exposes both paths.
"""
from __future__ import annotations

import logging
import re

from app.integrations.llm.router import ModelRouter
from app.memory.extractor import extract_memories
from app.repositories.memory_repository import MemoryRepository

logger = logging.getLogger(__name__)

# ApiKeyService mints prefixes as "luc_sk_" + ~5-9 chars from the raw key
# (observed range 12-16 chars total). Regex tolerates 4..32 to catch
# truncation/garbage defects without rejecting valid mints.
_KEY_PREFIX_PATTERN = re.compile(r"^luc_sk_[A-Za-z0-9_-]{4,32}$")


class MemoryService:

    def __init__(
        self,
        repository: MemoryRepository,
        model_router: ModelRouter,
    ) -> None:
        self.repository = repository
        self.model_router = model_router

    # ---------------------------------------------------------------- read
    def retrieve_memories(
        self,
        *,
        user_id: str,
        tenant_id: str,
        agent_id: str | None = None,
        limit: int = 20,
    ) -> list[str]:
        items = self.repository.get_user_memories(
            user_id=user_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            limit=limit,
        )
        return [f"[{item.category}] {item.content}" for item in items]

    # ---------------------------------------------------------- write (sync)
    def extract_and_save(
        self,
        *,
        user_id: str,
        tenant_id: str,
        session_id: str,
        agent_id: str | None = None,
        messages: list[dict],
        message_id: int | None = None,
        luciel_instance_id: int | None = None,
    ) -> int:
        extracted = extract_memories(messages, self.model_router)

        saved_count = 0
        for item in extracted:
            try:
                if message_id is not None:
                    saved = self.repository.upsert_by_message_id(
                        user_id=user_id,
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        category=item["category"],
                        content=item["content"],
                        source_session_id=session_id,
                        message_id=message_id,
                        luciel_instance_id=luciel_instance_id,
                    )
                    if saved:
                        saved_count += 1
                else:
                    self.repository.save_memory(
                        user_id=user_id,
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        category=item["category"],
                        content=item["content"],
                        source_session_id=session_id,
                    )
                    saved_count += 1
            except Exception as exc:
                logger.warning(
                    "Failed to save memory: type=%s", type(exc).__name__,
                )

        if saved_count:
            logger.info(
                "Extracted %d memories user=%s session=%s agent=%s instance=%s",
                saved_count, user_id, session_id, agent_id, luciel_instance_id,
            )
        return saved_count

    # -------------------------------------------------------- write (async)
    def enqueue_extraction(
        self,
        *,
        user_id: str,
        tenant_id: str,
        session_id: str,
        message_id: int,
        actor_key_prefix: str,
        agent_id: str | None = None,
        luciel_instance_id: int | None = None,
        trace_id: str | None = None,
    ) -> str:
        """Enqueue async memory extraction via Celery worker (Step 27b).

        Returns the Celery task id. Never blocks on extraction.
        Per Security Contract: payload contains ONLY opaque ids.

        Raises ValueError on malformed actor_key_prefix (defense in depth —
        route layer should already have vetted this).
        """
        if not _KEY_PREFIX_PATTERN.match(actor_key_prefix or ""):
            # Never log the actual prefix content — just its class.
            logger.error(
                "enqueue_extraction rejected malformed actor_key_prefix "
                "(len=%d) for tenant=%s session=%s",
                len(actor_key_prefix or ""), tenant_id, session_id,
            )
            raise ValueError("actor_key_prefix failed format validation")

        # Lazy import — FastAPI process never loads Celery until enqueue fires.
        from app.worker.tasks.memory_extraction import extract_memory_from_turn

        async_result = extract_memory_from_turn.delay(
            session_id=session_id,
            user_id=user_id,
            tenant_id=tenant_id,
            message_id=message_id,
            actor_key_prefix=actor_key_prefix,
            agent_id=agent_id,
            luciel_instance_id=luciel_instance_id,
            trace_id=trace_id,
        )
        return async_result.id