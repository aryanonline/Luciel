"""
Memory repository.

Handles all database operations for memory items.

This layer only deals with persistence — no extraction logic,
no LLM calls, no business rules about what should be remembered.

Memories are scoped to user + admin + luciel_instance (Wall-3 §3.7.3).
Per Arc 12 EX1b excision (v2 = single Admin→Instance boundary, §3.7.2),
agent_id is no longer a query-filter or a method parameter. Arc 12 EX3
(``arc12_ex3_drop_memory_agent_id``) dropped the column from
``memory_items``; the ORM no longer carries an ``agent_id`` attribute.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.memory import MemoryItem
import uuid  # noqa: F401  (referenced via string annotation in method signatures)


class MemoryRepository:

    def __init__(self, db: Session) -> None:
        self.db = db

    def save_memory(
        self,
        *,
        user_id: str,
        admin_id: str,
        category: str,
        content: str,
        source_session_id: str | None = None,
        actor_user_id: "uuid.UUID | None" = None,  # Step 24.5b File 2.6b
    ) -> MemoryItem:
        """Save a single memory item to the database.

        Step 24.5b File 2.6b: actor_user_id captures the platform User
        identity whose Agent wrote this row. Distinct from `user_id`
        (the free-form end-user identifier string predating Step 24.5b).
        Drift D7 resolution: both fields coexist with distinct semantics.
        Nullable until Commit 3 backfill flips the column to NOT NULL
        alongside agents.user_id (Invariant 12).
        """
        item = MemoryItem(
            user_id=user_id,
            admin_id=admin_id,
            category=category,
            content=content,
            source_session_id=source_session_id,
            actor_user_id=actor_user_id,  # Step 24.5b File 2.6b
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
        admin_id: str,
        category: str | None = None,
        limit: int = 50,
    ) -> list[MemoryItem]:
        """
        Retrieve active memories for a user.

        Scoping rules (Arc 12 EX1b — v2 single Admin→Instance boundary):
          - Always filters by user_id + admin_id (Wall-1, §3.7.2).
          - Optionally filter by category.
          - Returns newest memories first.

        agent_id filtering removed: in v1 this carve-out partitioned
        memories per-agent, but Arc 5 collapsed Agent into the single
        Admin→Instance plane. Per-instance scoping is enforced by
        Wall-3 RLS (memory_items.luciel_instance_id NOT NULL) at the
        DB layer, not by an in-app filter on the read path.
        """
        stmt = (
            select(MemoryItem)
            .where(
                MemoryItem.user_id == user_id,
                MemoryItem.admin_id == admin_id,
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
    def upsert_by_message_id(
        self,
        *,
        user_id: str,
        admin_id: str,
        category: str,
        content: str,
        message_id: int,
        source_session_id: str | None = None,
        luciel_instance_id: int | None = None,
        actor_user_id: "uuid.UUID | None" = None,  # Step 24.5b File 2.6b
    ) -> bool:
        """Insert a memory row keyed by (admin_id, message_id).

        Idempotent: if a row already exists for the same (admin_id, message_id),
        this is a no-op and returns False. Relies on the composite partial
        unique index added in migration <step-27b add_memory_items_message_id>.

        Returns True if a new row was inserted, False if the row already existed.

        Invariant 13: admin_id is in the conflict key, not just message_id,
        so two tenants cannot collision-block each other's message ids.

        Invariant 4: caller commits (worker owns the transaction so the
        memory row + admin_audit_logs row land together).
        """
        from sqlalchemy import select
        from sqlalchemy.exc import IntegrityError

        from app.models.memory import MemoryItem

        # Fast-path existence check (avoids a wasted INSERT on replay/redrive).
        existing = self.db.scalars(
            select(MemoryItem.id)
            .where(
                MemoryItem.admin_id == admin_id,
                MemoryItem.message_id == message_id,
            )
            .limit(1)
        ).first()
        if existing is not None:
            return False

        item = MemoryItem(
            user_id=user_id,
            admin_id=admin_id,
            category=category,
            content=content,
            source_session_id=source_session_id,
            message_id=message_id,
            luciel_instance_id=luciel_instance_id,
            actor_user_id=actor_user_id,  # Step 24.5b File 2.6b
            active=True,
        )
        self.db.add(item)
        try:
            self.db.flush()
        except IntegrityError:
            # Race: another worker inserted between our check and flush.
            # Partial unique index caught it. Safe no-op.
            self.db.rollback()
            return False
        return True
