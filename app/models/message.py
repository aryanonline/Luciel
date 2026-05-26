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


    admin_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("admins.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # Arc 9 C5.0b/C5.3 -- denormalised instance scope for Wall-3 RLS.
    # Arc 9.1 Phase A (2026-05-25): NOT NULL. The session row this message
    # belongs to is now guaranteed NOT NULL on luciel_instance_id, so the
    # add_message denormalisation is no longer best-effort. Indexed via
    # ix_messages_luciel_instance_id_session_id (C5.0b Phase 3).
    luciel_instance_id: Mapped[int] = mapped_column(
        Integer, nullable=False, index=False,
    )

    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    session: Mapped["SessionModel"] = relationship(back_populates="messages")