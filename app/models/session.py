from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.message import MessageModel


class SessionModel(Base, TimestampMixin):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    domain_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String(100), index=True, nullable=True)

    # Step 24.5 File 15 — nullable FK to the LucielInstance that served this session.
    # NULL = legacy/unbound (chat resolved via tenant/domain/agent config path).
    luciel_instance_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "luciel_instances.id",
            ondelete="SET NULL",
            name="fk_sessions_luciel_instance_id",
        ),
        nullable=True,
        index=True,
    )

    user_id: Mapped[str | None] = mapped_column(String(100), index=True, nullable=True)
    channel: Mapped[str] = mapped_column(String(50), default="web", nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="active", nullable=False)

    messages: Mapped[list["MessageModel"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="MessageModel.created_at",
    )