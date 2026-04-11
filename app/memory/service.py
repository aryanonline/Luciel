"""
Memory service.

Coordinates memory extraction and retrieval for Luciel.

This service sits between the chat flow and the memory repository.
It decides when to extract, what to store, and what to retrieve.

Responsibilities:
- After a chat turn: extract and persist new memories.
- Before a chat turn: retrieve relevant memories for context.

Future improvements:
- Deduplication: check if a memory already exists before saving.
- Relevance scoring: rank memories by relevance to current query.
- Memory limits: cap total memories per user to control context size.
- Vector search: use embeddings for semantic memory retrieval.
"""

from __future__ import annotations

import logging

from app.integrations.llm.router import ModelRouter
from app.memory.extractor import extract_memories
from app.repositories.memory_repository import MemoryRepository

logger = logging.getLogger(__name__)


class MemoryService:

    def __init__(
        self,
        repository: MemoryRepository,
        model_router: ModelRouter,
    ) -> None:
        self.repository = repository
        self.model_router = model_router

    def retrieve_memories(
        self,
        *,
        user_id: str,
        tenant_id: str,
        limit: int = 20,
    ) -> list[str]:
        """
        Load existing memories for a user, formatted as plain text strings.

        Returns a list of strings like:
            ["[preference] Prefers 2-bedroom condos",
             "[constraint] Budget is under 700k"]

        These strings are designed to be injected directly into
        the LLM context so Luciel can reference them during generation.
        """
        items = self.repository.get_user_memories(
            user_id=user_id,
            tenant_id=tenant_id,
            limit=limit,
        )
        return [f"[{item.category}] {item.content}" for item in items]

    def extract_and_save(
        self,
        *,
        user_id: str,
        tenant_id: str,
        session_id: str,
        messages: list[dict],
    ) -> int:
        """
        Extract durable memories from recent messages and persist them.

        Args:
            user_id:    The user these memories belong to.
            tenant_id:  The tenant context.
            session_id: Which session these memories came from.
            messages:   Recent conversation messages as dicts.

        Returns:
            Number of memories saved.
        """
        extracted = extract_memories(messages, self.model_router)

        saved_count = 0
        for item in extracted:
            try:
                self.repository.save_memory(
                    user_id=user_id,
                    tenant_id=tenant_id,
                    category=item["category"],
                    content=item["content"],
                    source_session_id=session_id,
                )
                saved_count += 1
            except Exception as exc:
                logger.warning("Failed to save memory: %s", exc)

        if saved_count:
            logger.info(
                "Extracted %d memories for user %s in session %s",
                saved_count, user_id, session_id,
            )

        return saved_count