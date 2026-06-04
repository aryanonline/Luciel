"""Arc 12b permission-model ORM rows.

Architecture §3.7.2 — permission-based custom roles (Enterprise).

Four tables in one module because they form a single dense object
graph and the closure / scope of "what is a role+permission" is
small:

* :class:`Permission` — atomic permission catalog. Platform-managed.
* :class:`CustomRole` — Enterprise-authored custom role. Tenant-scoped.
* :class:`RolePermission` — join (locked OR custom role → permission).
* :class:`UserRoleAssignment` — User → role + scope binding.
  Tenant-scoped.

Schema source of truth: alembic migrations
``arc12b_custom_roles_permission_model`` and
``rescanb_custom_role_approval``.

The DB has a CHECK constraint pinning ``locked_role`` (when set) to
the four canonical role names. The Python ``LOCKED_ROLE_*`` constants
below mirror those exact strings; they are also the .value of the
``app.models.scope_assignment.ScopeRole`` enum, so a single string can
flow between the two surfaces without coercion.

Rescan Tier-B adds the ``approval_state`` / ``approved_by_user_id`` /
``approved_at`` / ``pending_change_json`` columns to ``CustomRole``.
The approval-state constants follow the same pattern as
``app.models.sibling_call_grant`` (Architecture §3.7.3).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


# -------------------------------------------------------------------
# Rescan Tier-B: custom-role approval-state constants (Architecture §3.7.3).
# Mirrors the sibling_call_grant pattern for approvals.
# -------------------------------------------------------------------

APPROVAL_STATE_LIVE = "live"
APPROVAL_STATE_PENDING = "pending_approval"
APPROVAL_STATE_REVOKED = "revoked"

ALLOWED_CUSTOM_ROLE_APPROVAL_STATES: frozenset[str] = frozenset({
    APPROVAL_STATE_LIVE,
    APPROVAL_STATE_PENDING,
    APPROVAL_STATE_REVOKED,
})

# Permission keys that require a second admin_owner approval before
# the custom role may take effect. The billing permission key in this
# codebase is ``can_view_billing`` (Architecture §3.7.3 calls it
# ``can_manage_billing``; this constant includes both names so a
# future rename is handled automatically). Per the spec, we also
# include ``can_configure_connections``.
SENSITIVE_PERMISSION_KEYS: frozenset[str] = frozenset({
    "can_configure_connections",
    "can_view_billing",
    # Future-proof alias: if a can_manage_billing key is ever added
    # to the permissions catalog, include it here.
    "can_manage_billing",
})


# Canonical locked-role string values. Match the four ``scope_role``
# enum members in ``app.models.scope_assignment.ScopeRole``.
LOCKED_ROLE_ADMIN_OWNER = "admin_owner"
LOCKED_ROLE_ADMIN_MANAGER = "admin_manager"
LOCKED_ROLE_INSTANCE_OPERATOR = "instance_operator"
LOCKED_ROLE_READ_ONLY_VIEWER = "read_only_viewer"
ALL_LOCKED_ROLES: tuple[str, ...] = (
    LOCKED_ROLE_ADMIN_OWNER,
    LOCKED_ROLE_ADMIN_MANAGER,
    LOCKED_ROLE_INSTANCE_OPERATOR,
    LOCKED_ROLE_READ_ONLY_VIEWER,
)

# scope_type values on user_role_assignments.
SCOPE_TYPE_ALL_INSTANCES = "all_instances"
SCOPE_TYPE_INSTANCE_SPECIFIC = "instance_specific"
ALL_SCOPE_TYPES: tuple[str, ...] = (
    SCOPE_TYPE_ALL_INSTANCES,
    SCOPE_TYPE_INSTANCE_SPECIFIC,
)


# ---------------------------------------------------------------------
# Permission — global catalog, platform-managed.
# ---------------------------------------------------------------------


class Permission(Base, TimestampMixin):
    """Atomic permission. Platform-managed (admins never author rows here).

    The ``key`` is the load-bearing identifier — every callsite that
    says "this action requires permission X" references it by string.
    """

    __tablename__ = "permissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        Index("ix_permissions_category", "category"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Permission id={self.id} key={self.key!r}>"


# ---------------------------------------------------------------------
# CustomRole — Enterprise-authored role. Tenant-scoped, RLS-fenced.
# ---------------------------------------------------------------------


class CustomRole(Base, TimestampMixin):
    """Enterprise-authored custom role within a single Admin."""

    __tablename__ = "custom_roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    admin_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("admins.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    role_key: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    authored_by_user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    authored_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # ------------------------------------------------------------------
    # Rescan Tier-B: second-admin approval workflow (Architecture §3.7.3).
    # Mirrors the SiblingCallGrant approval columns.
    # ------------------------------------------------------------------

    # Current approval state. Default 'live' means all existing rows
    # (and non-sensitive new roles) are unaffected. Only sensitive
    # custom roles (containing can_configure_connections or
    # can_view_billing) start as 'pending_approval' until a second
    # admin_owner approves them.
    approval_state: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default="live",
    )

    # Who approved this role (populated on pending_approval -> live).
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )

    # When the approval happened.
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Staged (not yet applied) permission/scope change that is waiting
    # for a second admin_owner to approve. On approval, the change is
    # applied (RolePermission rows synced) and this field is cleared.
    pending_change_json: Mapped[dict | None] = mapped_column(
        PG_JSONB,
        nullable=True,
    )

    # Relationship to RolePermission rows that bind this custom role.
    permissions: Mapped[list["RolePermission"]] = relationship(
        "RolePermission",
        back_populates="custom_role",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        CheckConstraint(
            "approval_state IN ('live', 'pending_approval', 'revoked')",
            name="ck_custom_roles_approval_state",
        ),
        Index(
            "uq_custom_roles_admin_role_key_active",
            "admin_id",
            "role_key",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<CustomRole id={self.id} admin={self.admin_id} "
            f"key={self.role_key!r}>"
        )


# ---------------------------------------------------------------------
# RolePermission — locked-role rows AND custom-role rows.
# ---------------------------------------------------------------------


class RolePermission(Base):
    """Join binding a role (locked OR custom) to a permission.

    The CHECK constraint ``ck_role_permissions_locked_or_custom``
    enforces XOR — exactly one of ``locked_role`` or
    ``custom_role_id`` is populated.
    """

    __tablename__ = "role_permissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    admin_id: Mapped[str | None] = mapped_column(
        String(100),
        ForeignKey("admins.id", ondelete="RESTRICT"),
        nullable=True,
    )
    locked_role: Mapped[str | None] = mapped_column(String(64), nullable=True)
    custom_role_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("custom_roles.id", ondelete="CASCADE"),
        nullable=True,
    )
    permission_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("permissions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    custom_role: Mapped[CustomRole | None] = relationship(
        "CustomRole", back_populates="permissions"
    )
    permission: Mapped[Permission] = relationship("Permission")

    __table_args__ = (
        CheckConstraint(
            "(locked_role IS NOT NULL)::int + "
            "(custom_role_id IS NOT NULL)::int = 1",
            name="ck_role_permissions_locked_or_custom",
        ),
        CheckConstraint(
            "(locked_role IS NOT NULL AND admin_id IS NULL) OR "
            "(custom_role_id IS NOT NULL AND admin_id IS NOT NULL)",
            name="ck_role_permissions_admin_id_consistency",
        ),
        CheckConstraint(
            "locked_role IS NULL OR locked_role IN ("
            "'admin_owner','admin_manager','instance_operator','read_only_viewer')",
            name="ck_role_permissions_locked_role_valid",
        ),
    )


# ---------------------------------------------------------------------
# UserRoleAssignment — User → role + scope. Tenant-scoped, RLS-fenced.
# ---------------------------------------------------------------------


class UserRoleAssignment(Base, TimestampMixin):
    """Bind a User to a role (locked OR custom) with a scope.

    On Free/Pro tiers, this table is unused — the role-resolution path
    falls through to ``scope_assignments`` (the existing locked-role
    binding). On Enterprise, this table is the additive surface for
    assigning custom roles.
    """

    __tablename__ = "user_role_assignments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    admin_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("admins.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    locked_role: Mapped[str | None] = mapped_column(String(64), nullable=True)
    custom_role_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("custom_roles.id", ondelete="RESTRICT"),
        nullable=True,
    )
    scope_type: Mapped[str] = mapped_column(String(32), nullable=False)
    instance_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("instances.id", ondelete="RESTRICT"),
        nullable=True,
    )
    assigned_by_user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    custom_role: Mapped[CustomRole | None] = relationship(
        "CustomRole", foreign_keys=[custom_role_id]
    )

    __table_args__ = (
        CheckConstraint(
            "(locked_role IS NOT NULL)::int + "
            "(custom_role_id IS NOT NULL)::int = 1",
            name="ck_user_role_assignments_locked_or_custom",
        ),
        CheckConstraint(
            "scope_type IN ('all_instances', 'instance_specific')",
            name="ck_user_role_assignments_scope_type_valid",
        ),
        CheckConstraint(
            "(scope_type = 'instance_specific' AND instance_id IS NOT NULL) "
            "OR (scope_type = 'all_instances' AND instance_id IS NULL)",
            name="ck_user_role_assignments_scope_instance_consistency",
        ),
        CheckConstraint(
            "locked_role IS NULL OR locked_role IN ("
            "'admin_owner','admin_manager','instance_operator','read_only_viewer')",
            name="ck_user_role_assignments_locked_role_valid",
        ),
        Index(
            "ix_user_role_assignments_user_admin_active",
            "user_id",
            "admin_id",
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        role = self.locked_role or f"custom:{self.custom_role_id}"
        return (
            f"<UserRoleAssignment id={self.id} user={self.user_id} "
            f"admin={self.admin_id} role={role} scope={self.scope_type}>"
        )
