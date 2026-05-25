from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.session import SessionModel


class MessageModel(Base, TimestampMixin):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    # Arc 9 C5.0a/C5.3 -- denormalised tenant_id for Wall-1 RLS
    # (messages_tenant_isolation policy). Populated from the parent
    # session row at insert time by SessionRepository.add_message.
    # NOT NULL post-C5.0a Phase 3. Indexed via the composite
    # ix_messages_tenant_id_session_id (created in C5.0a Phase 4).
    tenant_id: Mapped[str] = mapped_column(
        String(100), nullable=False, index=False,
    )

    # Arc 9 C5.0b/C5.3 -- denormalised instance scope for Wall-3 RLS
    # (messages_instance_isolation policy). Nullable -- NULL means the
    # parent session row also has NULL luciel_instance_id (legacy /
    # pre-Step 24.5 unbound). Populated from the parent session row
    # at insert time by SessionRepository.add_message. Indexed via
    # ix_messages_luciel_instance_id_session_id (C5.0b Phase 3).
    luciel_instance_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=False,
    )

    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    session: Mapped["SessionModel"] = relationship(back_populates="messages")