"""Admin ORM model — V2 billing entity and permissions root (Arc 5 B1).

Mirrors the ``admins`` table created at Revision A
(``alembic/versions/arc5_a_admin_instance_additive.py``). The Admin
entity is the V2 billing root per Architecture v1 §3.2 (Instance
subsystem).

Schema anchors
--------------
* ``admins.id`` is ``VARCHAR(100)`` semantic slug key per Q1 lock
  (mirrors legacy ``tenant_configs.tenant_id``).
* ``admins.tier`` defaults ``'free'`` per Q2 lock; permissive CHECK
  during the migration window accepts legacy + V2 values; Revision C
  tightens to ``('free', 'pro', 'enterprise')``.
* ``admins.stripe_customer_id`` is NULL on Free tier per Gap 1 lock
  (lazy-created on upgrade); UNIQUE among non-NULL values.
* Back-pointer ``legacy_tenant_id`` was dropped at Revision C
  (arc5_c_admin_instance_subtractive); the column attribute on this
  model was removed at this hotfix (demo-day-2026-05-25) to align the
  model with the post-arc5_c schema. Prior to the hotfix, the model
  still declared ``legacy_tenant_id`` which caused every SELECT on
  admins to crash with UndefinedColumn against any DB at arc5_c or
  later, including production. See Phase A of the C10 demo-day plan.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


TIER_FREE = "free"
TIER_PRO = "pro"
TIER_ENTERPRISE = "enterprise"
ALLOWED_TIERS_V2 = (TIER_FREE, TIER_PRO, TIER_ENTERPRISE)

TIER_SOURCE_STRIPE_WEBHOOK = "stripe_webhook"
TIER_SOURCE_SALES_OPS = "sales_ops_provisioned"
TIER_SOURCE_FREE_SIGNUP = "free_signup"
TIER_SOURCE_REVB_BACKFILL = "revision_b_backfill"
TIER_SOURCE_MANUAL = "manual"
ALLOWED_TIER_SOURCES = (
    TIER_SOURCE_STRIPE_WEBHOOK,
    TIER_SOURCE_SALES_OPS,
    TIER_SOURCE_FREE_SIGNUP,
    TIER_SOURCE_REVB_BACKFILL,
    TIER_SOURCE_MANUAL,
)


class Admin(Base):
    __tablename__ = "admins"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    tier: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=TIER_FREE
    )
    tier_source: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=TIER_SOURCE_MANUAL
    )
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    stripe_customer_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    # ``legacy_tenant_id`` Mapped column removed at hotfix
    # demo-day-2026-05-25 (Phase A). The DB column was dropped at
    # arc5_c; this model attribute had survived the cleanup and was
    # the cause of the UndefinedColumn 500s observed on /signup-free
    # on 2026-05-25. See git blame for the previous declaration.

    # Arc 7 Commit 6 (2026-05-24) -- Free-signup soft gate. Postgres
    # INET column captured at signup_free mint time. NULL for paid
    # Stripe Checkout flows (Pro / Enterprise) and for every
    # pre-migration row. Read by the 24h 1-per-IP gate in
    # ``app/api/v1/billing.py:signup_free`` before the next Free
    # mint succeeds; written immediately post-onboard.
    last_signup_ip: Mapped[str | None] = mapped_column(
        INET(), nullable=True
    )
    # Arc 9 C12 hotfix (demo-day-2026-05-25): server_default + onupdate added.
    # Prior to this fix, the model lacked any default for the timestamp
    # columns, so SQLAlchemy emitted ``INSERT ... (created_at, updated_at)
    # VALUES (NULL, NULL)`` and Postgres rejected with NotNullViolation
    # before the DB-side ``DEFAULT now()`` could fire (explicit NULL beats
    # column default). The contract in onboarding_service.py:137 already
    # assumed these were server-defaulted; the model just hadn't caught up.
    # See ARC9_C12_HOTFIX section of ARC9_ENVELOPE_CORRIGENDUM (follow-up).
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # -----------------------------------------------------------------
    # Arc 10 Lifecycle Subsystem (Alembic arc10_lifecycle_subsystem).
    # -----------------------------------------------------------------
    # See Vision §6 and Architecture §3.6 for the full lifecycle
    # spec. The four columns below describe the closure / hard-delete
    # lifecycle of an Admin row.
    #
    # deactivated_at: drift reconciliation — the Arc 5 rename of
    # tenant_configs → admins did not carry this column. Backfilled
    # from legacy tenant_configs in the Arc 10 migration. Stamped by
    # AdminService.deactivate_tenant_with_cascade.
    #
    # closure_initiated_at: distinct from deactivated_at. Set ONLY by
    # POST /api/v1/admin/account/close. Starts the 30-day grace clock.
    # The retention worker (app/worker/tasks/retention.py) keys hard-
    # delete eligibility off THIS column, not off deactivated_at. This
    # is what enforces "closure is the only trigger for hard-delete;
    # platform-admin deactivation does NOT advance toward hard-delete."
    #
    # closure_cancel_mode: the admin's Stripe-cancel choice at closure.
    # 'immediate' or 'period_end'. Read by _on_subscription_deleted.
    #
    # hard_deleted_at: terminal tombstone stamp. Set by
    # hard_delete_tenant_after_retention (Arc 10 changed step 11 from
    # row-delete to tombstone UPDATE per Vision §6.5 "minimal compliance
    # record"). Once set, the row is audit-only: name, stripe_customer_id,
    # last_signup_ip are redacted to NULL / '[REDACTED]'. The row
    # persists so the audit chain's row_natural_id references stay
    # walkable.
    deactivated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    closure_initiated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    closure_cancel_mode: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )
    hard_deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "tier IN ('free', 'pro', 'enterprise', 'individual', 'solo', 'team', 'company')",
            name="ck_admins_tier_valid_during_migration",
        ),
        CheckConstraint(
            "tier_source IN ('stripe_webhook', 'sales_ops_provisioned', "
            "'free_signup', 'revision_b_backfill', 'manual')",
            name="ck_admins_tier_source_valid",
        ),
        # Arc 10: closure_cancel_mode is a free-text VARCHAR(16) at the
        # column level but only two values are legal. The CHECK
        # constraint is created in the Arc 10 migration; this Index
        # block must not redeclare it (SQLAlchemy would try to issue
        # CREATE on table-create and conflict with the migration).
        Index("ix_admins_tier", "tier"),
        Index("ix_admins_active", "active"),
        # ix_admins_last_signup_ip is a PARTIAL index created in
        # the migration (``WHERE last_signup_ip IS NOT NULL AND
        # active = true``); we do not redeclare it here because
        # SQLAlchemy's ``Index(..., postgresql_where=...)`` would
        # try to create it on table-create, which conflicts with
        # the migration owning the predicate. The model carries
        # only the column declaration; the migration owns the
        # partial index.
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Admin id={self.id} tier={self.tier} active={self.active}>"
        )


AdminConfig = Admin
