"""
Memory repository.

Handles all database operations for memory items.
This layer only deals with persistence — no extraction logic,
no LLM calls, no business rules about what should be remembered.

To query memories differently later (e.g., vector similarity search),
add new methods here without changing the service layer.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.memory import MemoryItem


class MemoryRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def save_memory(
        self,
        *,
        user_id: str,
        tenant_id: str,
        category: str,
        content: str,
        source_session_id: str | None = None,
    ) -> MemoryItem:
        """Save a single memory item to the database."""
        item = MemoryItem(
            user_id=user_id,
            tenant_id=tenant_id,
            category=category,
            content=content,
            source_session_id=source_session_id,
            active=True,
        )
        self.db.add(item)
        self.db.commit()
        self.db.refresh(item)
        return item

    def get_user_memories(
        self,
        *,
        user_id: str,
        tenant_id: str,
        category: str | None = None,
        limit: int = 50,
    ) -> list[MemoryItem]:
        """
        Retrieve active memories for a user.
        Optionally filter by category.
        Returns newest memories first.
        """
        stmt = (
            select(MemoryItem)
            .where(
                MemoryItem.user_id == user_id,
                MemoryItem.tenant_id == tenant_id,
                MemoryItem.active.is_(True),
            )
            .order_by(MemoryItem.created_at.desc())
            .limit(limit)
        )

        if category:
            stmt = stmt.where(MemoryItem.category == category)

        return list(self.db.scalars(stmt).all())

    def deactivate_memory(self, memory_id: int) -> None:
        """Soft-delete a memory by marking it inactive."""
        item = self.db.get(MemoryItem, memory_id)
        if item:
            item.active = False
            self.db.commit()