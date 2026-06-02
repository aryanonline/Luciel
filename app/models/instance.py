"""Instance ORM model — V2 unit replacing LucielInstance + Agent (Arc 5 B1).

Mirrors the ``instances`` table created at Revision A
(``alembic/versions/arc5_a_admin_instance_additive.py``). The Instance
entity is the V2 config carrier per Architecture v1 §3.2 (Instance
subsystem) — it replaces the V1 LucielInstance + Agent split and
holds the five configuration pillars per Vision v1 §3.

Schema anchors
--------------
* ``instances.id`` is INTEGER autoincrement mirroring legacy
  ``luciel_instances.id``.
* ``instances.admin_id`` is FK to ``admins.id`` (RESTRICT — soft-delete
  Admins, never hard-delete).
* ``instance_slug`` is unique within an Admin.
* Back-pointers ``legacy_luciel_instance_id`` and ``legacy_agent_id``
  were dropped at Revision C alongside the legacy tables (Arc 9 C15:
  ORM declarations removed to match production schema).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.instance_status import InstanceStatus
from app.persona.presets import ALL_PRESETS, DEFAULT_PRESET


class Instance(Base):
    __tablename__ = "instances"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    admin_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("admins.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    instance_slug: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        server_onupdate=text("now()"),
    )

    # Arc 6 Commit 8.5b — Deferred-downgrade overflow archive stamp.
    # Set when this Instance is one of the LRU losers at a downgrade
    # boundary (Pro→Free or Ent→Pro/Free). Pairs with active=false at
    # the same moment. Re-upgrade within the audit_retention window
    # (Free=30d) rehydrates rows that still carry this stamp.
    # NULL = not archived for downgrade reasons.
    pending_downgrade_archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Arc 10 (Alembic arc10_lifecycle_subsystem) — instance soft-delete
    # clock per Architecture §3.6.1 ("soft-delete window measured from
    # soft_deleted_at (locked)"). Set when the instance is deactivated.
    # The soft-delete worker reads this column to find instances 30 days
    # past deactivation and hard-deletes their knowledge embeddings.
    # Distinct from active=false (operational flag) and from
    # pending_downgrade_archived_at (downgrade-archived).
    soft_deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Arc 11 Closeout PR-A — instance lifecycle status enum per
    # Customer Journey §4.5 Phase 8 (Pause / Delete / Restore) and
    # Architecture §3.6.1 (soft-delete grace window measured from
    # soft_deleted_at). The PG enum ``instance_status`` is created by
    # migration ``arc11_closeout_a_instance_lifecycle``. Replaces the
    # legacy ``active`` boolean as the source of truth; ``active`` is
    # kept as a deprecated mirror through Arc 11 and dropped in Arc 12.
    instance_status: Mapped[InstanceStatus] = mapped_column(
        SAEnum(
            InstanceStatus,
            name="instance_status",
            values_callable=lambda x: [m.value for m in x],
            create_type=False,
            native_enum=True,
        ),
        nullable=False,
        server_default=InstanceStatus.ACTIVE.value,
        index=True,
    )

    # Arc 13 — per-instance channel enablement. ``enabled_channels`` is
    # the set of channel ids structurally enabled on this Instance
    # (widget always present; email / sms added when provisioned).
    # Read by the chat_widget gating + by admin_tools'
    # _instance_channels_enabled chokepoint. Default {widget} backfills
    # every row to the entitlement floor.
    enabled_channels: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        server_default=text("ARRAY['widget']::text[]"),
    )
    # The E.164 number provisioned to this Instance for SMS (NULL until
    # SMS enabled + bound). The channel_routes row (channel='sms') is
    # the routing record; this is the instance-side reference.
    sms_provisioned_number: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    # 'dedicated' | 'shared' — the SMS number provisioning mode. NULL
    # until SMS is enabled. See app/policy/entitlements.py
    # dedicated-number helper for the per-tier policy.
    sms_number_mode: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )

    # --- Arc 15 WU1 — instance configuration pillars (Vision §3.5) ---
    # The website the instance's widget will live on (Journey Phase 3).
    website: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Personality preset name. PG enum ``personality_preset`` with values
    # from app.persona.presets.ALL_PRESETS; default warm_concierge. The
    # ``custom`` value is Pro/Enterprise-only, enforced at the API layer
    # (NOT at the DB — the enum admits ``custom`` on every tier).
    personality_preset: Mapped[str] = mapped_column(
        SAEnum(
            *ALL_PRESETS,
            name="personality_preset",
            create_type=False,
            native_enum=True,
        ),
        nullable=False,
        server_default=DEFAULT_PRESET,
    )
    # Custom axis values ``{tone, verbosity, formality, pace}``. Populated
    # ONLY when personality_preset == 'custom'; NULL for named presets
    # (their axis tuple lives in app.persona.presets.PRESET_AXES).
    personality_axes: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True
    )
    # Business context — composed verbatim into the BUSINESS_CONTEXT
    # stanza (WU2 composer). Tier-capped at the Pydantic boundary
    # (280 Free/Pro, 2000 Enterprise — Vision §3.5); NOT capped at DB.
    business_context: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Lead routing config. Shape:
    #   {"strategy": "round_robin|geographic|specialty_match|single_contact",
    #    "rules": [...]}
    # Pro/Enterprise only (enforced at API); Free instances leave NULL.
    lead_routing: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Arc 15 WU3 — escalation CONTACT + ROUTING config (Vision §3.4).
    # Stores WHO is notified and HOW, per fixed runtime signal — NEVER
    # the trigger conditions themselves (the four escalation signals are
    # runtime cognition, not admin-configurable). Tier-shaped at the API.
    # NULL = no escalation contact configured yet.
    escalation_config: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "admin_id", "instance_slug", name="uq_instances_admin_id_slug"
        ),
        Index("ix_instances_active", "active"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Instance id={self.id} admin_id={self.admin_id} "
            f"slug={self.instance_slug} active={self.active}>"
        )
