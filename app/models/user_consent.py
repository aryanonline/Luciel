"""
User consent model.

Tracks whether a user has granted or withdrawn consent
for Luciel to persist memories and personal data.

PIPEDA requires meaningful consent before collecting,
using, or disclosing personal information.
"""
from __future__ import annotations

from sqlalchemy import Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class UserConsent(Base, TimestampMixin):
    __tablename__ = "user_consents"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    user_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)

    # What the user consented to
    consent_type: Mapped[str] = mapped_column(
        String(50), nullable=False, default="memory_persistence",
    )

    # Current state
    granted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # How consent was obtained (e.g., "chat_prompt", "settings_page", "api")
    collection_method: Mapped[str] = mapped_column(
        String(50), nullable=False, default="api",
    )

    # Optional note (e.g., the exact prompt shown to user)
    consent_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # IP or session context at time of consent (for audit)
    consent_context: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        # One consent record per user + tenant + type
        {"comment": "PIPEDA consent tracking"},
    )