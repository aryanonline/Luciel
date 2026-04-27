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
import uuid  # noqa: F401  (referenced via string annotation in method signatures)

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
        actor_user_id: "uuid.UUID | None" = None,  # Step 24.5b File 2.6c
    ) -> int:
        """Extract memories from a turn and persist via the repository.

        Step 24.5b File 2.6c: actor_user_id captures the platform User
        identity whose Agent wrote these memories. Distinct from `user_id`
        (free-form end-user identifier string predating Step 24.5b -- the
        brokerage's internal ID for the human chatting). Drift D7
        resolution: both fields coexist with distinct semantics.

        Routed through to MemoryRepository.upsert_by_message_id (idempotent
        path, message_id present) or MemoryRepository.save_memory (legacy
        fallback, message_id absent). Drift D9 note: the save_memory
        fallback path 500s against the current local DB until File 2.7's
        migration lands the actor_user_id column. Not on the hot chat
        path -- ChatService always supplies message_id.
        """
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
                        actor_user_id=actor_user_id,  # Step 24.5b File 2.6c
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
                        actor_user_id=actor_user_id,  # Step 24.5b File 2.6c
                    )
                    saved_count += 1
            except Exception as exc:
                logger.warning(
                    "Failed to save memory: type=%s", type(exc).__name__,
                )

        if saved_count:
            logger.info(
                "Extracted %d memories user=%s session=%s agent=%s "
                "instance=%s actor_user=%s",
                saved_count, user_id, session_id, agent_id,
                luciel_instance_id,
                str(actor_user_id) if actor_user_id else None,
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
        actor_user_id: "uuid.UUID | None" = None,  # Step 24.5b File 2.6c
    ) -> str:
        """Enqueue async memory extraction via Celery worker (Step 27b).

        Returns the Celery task id. Never blocks on extraction.
        Per Security Contract: payload contains ONLY opaque ids.

        Raises ValueError on malformed actor_key_prefix (defense in depth --
        route layer should already have vetted this).

        Step 24.5b File 2.6c: actor_user_id is the platform User identity
        attribution for this memory write. Serialized as a string in the
        SQS payload (UUIDs aren't JSON-native). Worker parses back to
        uuid.UUID at task entry and validates against current DB state
        (defense-in-depth gate per File 2.6d).
        """
        if not _KEY_PREFIX_PATTERN.match(actor_key_prefix or ""):
            # Never log the actual prefix content -- just its class.
            logger.error(
                "enqueue_extraction rejected malformed actor_key_prefix "
                "(len=%d) for tenant=%s session=%s",
                len(actor_key_prefix or ""), tenant_id, session_id,
            )
            raise ValueError("actor_key_prefix failed format validation")

        # Lazy import -- FastAPI process never loads Celery until enqueue fires.
        from app.worker.tasks.memory_extraction import extract_memory_from_turn

        # Serialize actor_user_id as string for JSON transport; worker
        # parses back to uuid.UUID at task entry. None stays None.
        actor_user_id_str = str(actor_user_id) if actor_user_id else None

        async_result = extract_memory_from_turn.delay(
            session_id=session_id,
            user_id=user_id,
            tenant_id=tenant_id,
            message_id=message_id,
            actor_key_prefix=actor_key_prefix,
            agent_id=agent_id,
            luciel_instance_id=luciel_instance_id,
            trace_id=trace_id,
            actor_user_id=actor_user_id_str,  # Step 24.5b File 2.6c
        )
        return async_result.id