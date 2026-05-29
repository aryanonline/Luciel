"""Arc 12b — permission catalog + custom_roles + role_permissions + user_role_assignments.

Revision ID: arc12b_custom_roles_permission_model
Revises: arc12_ex4_reseal_audit_chain_drop_agent_domain
Create Date: 2026-05-29

Architecture §3.7.2 ("Permission-based custom roles (Enterprise, Arc 12b)")
and §6 Arc-12b row, Vision §9 Decision #8 (Path B locked).

This migration is the schema half of Arc 12b. Application code adds:

  * ``app/models/permission_model.py`` — ORM rows for the four tables.
  * ``app/policy/permissions.py`` — the unified permission resolver
    that ``ScopePolicy.enforce_role_on_instance`` and
    ``ScopePolicy.enforce_action`` both consult.
  * ``app/api/v1/admin_custom_roles.py`` — the Enterprise-only role
    authoring CRUD.

Tables created
--------------

* ``permissions`` — atomic permission catalog. Platform-managed
  (admins NEVER author rows here). One row per atomic action with a
  stable ``key`` (e.g. ``can_configure_channels``). NOT tenant-scoped;
  global to the platform.

* ``custom_roles`` — Enterprise-authored custom roles. Columns:
  ``id``, ``admin_id``, ``role_key`` (stable string; admin-chosen
  identifier like ``"office_manager"``), ``display_name``,
  ``description``, ``authored_by_user_id``, ``authored_at``,
  ``revoked_at`` (soft-delete). Scoped by ``admin_id`` — TENANT-SCOPED
  with fail-closed RLS.

* ``role_permissions`` — join binding a role (locked OR custom) to a
  permission. Two row shapes share the table:
    - Locked-role rows: ``admin_id IS NULL`` + ``locked_role`` populated
      with a ``scope_role`` enum value. Platform-seeded; IMMUTABLE
      (the migration writes them and they are not exposed via the
      role-authoring API). Same row participates in the resolver's
      lookup so the policy code has ONE uniform table to query.
    - Custom-role rows: ``custom_role_id`` FK + ``admin_id``
      populated. Admin-authored via the role-authoring API.
  CHECK constraint enforces XOR: exactly one of the two halves is
  populated. NOT tenant-scoped at the table level for the locked-role
  rows (admin_id IS NULL by design), but every custom-role row carries
  ``admin_id`` and inherits the wall-1 fence through the FK +
  application-layer enforcement. (RLS on this table would conflict
  with the locked-role rows that intentionally have NULL admin_id;
  see §3.7.5 — RLS is reserved for tables whose every row is
  tenant-scoped.)

* ``user_role_assignments`` — binds a User → role + scope. Replaces
  the role-resolution ROLE of ``scope_assignments`` ONLY for users
  assigned to custom roles; ``scope_assignments`` remains the
  identity/scope binding for locked-role users so Free/Pro see no
  behavioural change. Columns: ``id``, ``admin_id``, ``user_id``,
  ``locked_role`` XOR ``custom_role_id``, ``scope_type``
  (``'all_instances' | 'instance_specific'``), ``instance_id``
  (nullable; set only when ``scope_type='instance_specific'``),
  ``assigned_by_user_id``, ``assigned_at``, ``revoked_at``.
  TENANT-SCOPED with fail-closed RLS — every row carries
  ``admin_id``.

RLS posture
-----------

Follows §3.7.5 / Arc 9 doctrine, mirrored from
``arc12_wu2_instance_tool_authorizations``:

  * ``custom_roles``: ``ENABLE`` + ``FORCE`` RLS, PERMISSIVE policy
    on ``admin_id = current_setting('app.admin_id', true)``.
  * ``user_role_assignments``: same.
  * ``permissions``: no RLS — global platform-managed reference data.
  * ``role_permissions``: no RLS — locked-role rows have NULL admin_id
    by design; the custom-role rows carry admin_id and are joined to
    ``custom_roles`` which IS RLS-fenced; the application layer
    enforces wall-1 on writes to this table.

Grants on the new tables are inherited from the ``ALTER DEFAULT
PRIVILEGES`` installed at Arc 9 C10.b — no explicit grant is issued.

Seeds
-----

The ``upgrade()`` function plants two idempotent seed blocks:

  1. ``_seed_permission_catalog()`` — full permission catalog. ON
     CONFLICT (key) DO UPDATE so re-running is safe.

  2. ``_seed_locked_role_permissions()`` — every (locked_role,
     permission_key) pair that reproduces TODAY's behavior exactly.
     Pre-aggregated below for review:

     admin_owner    → ALL of the catalog except platform_admin
                      (full Wall-2 surface).
     admin_manager  → everything admin_owner has EXCEPT
                      can_approve_sibling_grants (approve narrows to
                      owner only per §3.3.4),
                      can_author_custom_roles (Enterprise authoring
                      is owner-only per §3.7.2),
                      can_view_billing (owner-only stewardship),
                      can_assign_roles (owner-only).
     instance_operator → read-only operator on their bound Instance:
                      can_view_knowledge, can_view_tools.
     read_only_viewer → can_view_tools only (read-only view).

     The exact mapping reproduces today's
     ``_KNOWLEDGE_ACTION_ROLES`` matrix in ``app/policy/scope.py``
     (list/view → owner+manager+operator; edit/delete →
     owner+manager) and the per-route role sets in
     ``app/api/v1/admin_tools.py`` / ``admin_sibling_grants.py``.

Rollback contract
-----------------

``alembic downgrade -1`` reverses everything in the inverse order:
drop RLS policies, drop tables, drop the enum if defined.
``user_role_assignments`` references ``custom_roles``; the order
matters.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID


revision = "arc12b_custom_roles_permission_model"
down_revision = "arc12_ex4_reseal_audit_chain_drop_agent_domain"
branch_labels = None
depends_on = None


# =====================================================================
# Permission catalog — single source of truth for the seed.
#
# `key` is the stable identifier referenced by the resolver and by
# every callsite that says "this action requires permission X".
# `display_name` + `description` + `category` are admin-facing strings
# the role-authoring UI renders. CATEGORY is one of:
#   "knowledge", "tools", "channels", "connections", "audit",
#   "billing", "team_admin", "platform_admin"
# =====================================================================


PERMISSION_CATALOG: tuple[dict, ...] = (
    # ---- knowledge ----
    dict(
        key="can_view_knowledge",
        display_name="View knowledge",
        description="List and read knowledge sources for an Instance.",
        category="knowledge",
    ),
    dict(
        key="can_edit_knowledge",
        display_name="Edit knowledge",
        description="Update / replace existing knowledge sources.",
        category="knowledge",
    ),
    dict(
        key="can_delete_knowledge",
        display_name="Delete knowledge",
        description="Delete knowledge sources (soft-delete; per §3.6.4).",
        category="knowledge",
    ),
    dict(
        key="can_ingest_knowledge",
        display_name="Ingest knowledge",
        description=(
            "Add new knowledge sources to an Instance "
            "(upload, paste, or website crawl per tier)."
        ),
        category="knowledge",
    ),

    # ---- tools ----
    dict(
        key="can_view_tools",
        display_name="View tools",
        description="View the per-Instance tool authorization matrix.",
        category="tools",
    ),
    dict(
        key="can_configure_tools",
        display_name="Configure tools",
        description=(
            "Toggle per-Instance tool authorizations on or off "
            "(default-deny; §3.3.1)."
        ),
        category="tools",
    ),

    # ---- channels ----
    dict(
        key="can_configure_channels",
        display_name="Configure channels",
        description=(
            "Connect / configure inbound and outbound channels "
            "(email, SMS, web widget) for an Instance."
        ),
        category="channels",
    ),

    # ---- connections (Arc 17 subsystem — permission seeded NOW) ----
    dict(
        key="can_configure_connections",
        display_name="Configure connections",
        description=(
            "Configure external connections (CRM, property feeds, etc.) "
            "that tools depend on. The Connections subsystem ships in "
            "Arc 17; the permission is seeded now so the locked-role "
            "matrix matches §3.7.2 / §3.8.6 from day one."
        ),
        category="connections",
    ),

    # ---- audit ----
    dict(
        key="can_view_audit_log",
        display_name="View audit log",
        description="Read the per-Admin audit trail.",
        category="audit",
    ),

    # ---- billing ----
    dict(
        key="can_view_billing",
        display_name="View billing",
        description=(
            "View subscription, invoices, and payment status. "
            "Owner-only by default per §3.7.2 example."
        ),
        category="billing",
    ),

    # ---- team admin ----
    dict(
        key="can_author_sibling_grants",
        display_name="Author sibling-call grants",
        description=(
            "Author cross-Instance sibling-Luciel composition grants "
            "(§3.3.4). Wall-2 still requires scope on BOTH endpoints."
        ),
        category="team_admin",
    ),
    dict(
        key="can_approve_sibling_grants",
        display_name="Approve sibling-call grants",
        description=(
            "Approve pending sibling-Luciel grants on Enterprise tier "
            "(§3.3.4). Owner-only by default."
        ),
        category="team_admin",
    ),
    dict(
        key="can_author_custom_roles",
        display_name="Author custom roles",
        description=(
            "Create / edit / revoke custom roles built from atomic "
            "permissions. Enterprise-only writes; owner-only by default. "
            "The 'no privilege escalation' rule applies."
        ),
        category="team_admin",
    ),
    dict(
        key="can_assign_roles",
        display_name="Assign roles",
        description=(
            "Assign locked or custom roles to team members "
            "(create / revoke user_role_assignments rows)."
        ),
        category="team_admin",
    ),
)


# Locked-role → permission-set mapping. Reproduces TODAY's behavior
# (Free/Pro see zero change). The four locked role values match the
# `scope_role` Postgres enum installed at
# `arc11_cleanup_c_scope_assignment_role_enum`.

LOCKED_ROLE_VALUES: tuple[str, ...] = (
    "admin_owner",
    "admin_manager",
    "instance_operator",
    "read_only_viewer",
)

LOCKED_ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    # admin_owner: every permission (modulo platform_admin, which is
    # the cross-Admin operator permission and not a Wall-2 permission).
    "admin_owner": tuple(p["key"] for p in PERMISSION_CATALOG),

    # admin_manager: owner's set MINUS the four owner-stewardship
    # permissions (approve grants, author roles, view billing, assign
    # roles). Reproduces Wall-2 today.
    "admin_manager": tuple(
        p["key"]
        for p in PERMISSION_CATALOG
        if p["key"]
        not in (
            "can_approve_sibling_grants",
            "can_author_custom_roles",
            "can_view_billing",
            "can_assign_roles",
        )
    ),

    # instance_operator: read-only operator scoped to one Instance.
    # Knowledge list/view + tool view. Reproduces
    # _KNOWLEDGE_ACTION_ROLES + admin_tools._READ_ROLES today.
    "instance_operator": (
        "can_view_knowledge",
        "can_view_tools",
    ),

    # read_only_viewer: tool view only. Reproduces _READ_ROLES.
    "read_only_viewer": (
        "can_view_tools",
    ),
}


# Sanity: every permission referenced in the locked-role map exists
# in the catalog. Asserted at module import so a bad edit fails fast.
_CATALOG_KEYS = {p["key"] for p in PERMISSION_CATALOG}
for _role, _perms in LOCKED_ROLE_PERMISSIONS.items():
    for _k in _perms:
        assert _k in _CATALOG_KEYS, (
            f"locked-role seed references unknown permission {_k!r} "
            f"for role {_role!r}"
        )


_T_PERMISSIONS = "permissions"
_T_CUSTOM_ROLES = "custom_roles"
_T_ROLE_PERMISSIONS = "role_permissions"
_T_USER_ROLE_ASSIGNMENTS = "user_role_assignments"


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. permissions — platform-managed catalog.
    # ------------------------------------------------------------------
    op.create_table(
        _T_PERMISSIONS,
        sa.Column(
            "id", sa.Integer(), primary_key=True, autoincrement=True
        ),
        sa.Column(
            "key",
            sa.String(64),
            nullable=False,
            unique=True,
            comment=(
                "Stable identifier referenced by the resolver and the "
                "API. E.g. 'can_configure_channels'."
            ),
        ),
        sa.Column(
            "display_name",
            sa.String(128),
            nullable=False,
        ),
        sa.Column(
            "description",
            sa.Text(),
            nullable=False,
        ),
        sa.Column(
            "category",
            sa.String(32),
            nullable=False,
            comment=(
                "Group label rendered in the role-authoring UI "
                "(e.g. 'knowledge', 'tools', 'channels')."
            ),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            server_onupdate=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_permissions_category",
        _T_PERMISSIONS,
        ["category"],
    )

    # ------------------------------------------------------------------
    # 2. custom_roles — Enterprise-authored roles. Tenant-scoped, RLS.
    # ------------------------------------------------------------------
    op.create_table(
        _T_CUSTOM_ROLES,
        sa.Column(
            "id", sa.Integer(), primary_key=True, autoincrement=True
        ),
        sa.Column(
            "admin_id",
            sa.String(100),
            sa.ForeignKey("admins.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
            comment="Wall-1 tenant boundary. RLS fences on this column.",
        ),
        sa.Column(
            "role_key",
            sa.String(64),
            nullable=False,
            comment=(
                "Stable admin-chosen identifier for the custom role "
                "within this Admin. E.g. 'office_manager'."
            ),
        ),
        sa.Column(
            "display_name",
            sa.String(128),
            nullable=False,
        ),
        sa.Column(
            "description",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "authored_by_user_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
            comment="Audit-trail authorship. RESTRICT — never lose authorship.",
        ),
        sa.Column(
            "authored_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Soft-delete timestamp; NULL means live.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            server_onupdate=sa.func.now(),
        ),
    )
    # Partial unique: at-most-one live custom-role with a given key
    # within an Admin. Revoked rows excluded so re-author after revoke
    # is permitted.
    op.create_index(
        "uq_custom_roles_admin_role_key_active",
        _T_CUSTOM_ROLES,
        ["admin_id", "role_key"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    op.execute(f"ALTER TABLE {_T_CUSTOM_ROLES} ENABLE ROW LEVEL SECURITY;")
    op.execute(f"ALTER TABLE {_T_CUSTOM_ROLES} FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"""
        CREATE POLICY custom_roles_tenant_isolation
        ON {_T_CUSTOM_ROLES}
        AS PERMISSIVE
        FOR ALL
        TO PUBLIC
        USING (admin_id = current_setting('app.admin_id', true))
        WITH CHECK (admin_id = current_setting('app.admin_id', true));
        """
    )

    # ------------------------------------------------------------------
    # 3. role_permissions — join. Locked-role rows AND custom-role rows.
    # ------------------------------------------------------------------
    op.create_table(
        _T_ROLE_PERMISSIONS,
        sa.Column(
            "id", sa.Integer(), primary_key=True, autoincrement=True
        ),
        sa.Column(
            "admin_id",
            sa.String(100),
            sa.ForeignKey("admins.id", ondelete="RESTRICT"),
            nullable=True,
            index=True,
            comment=(
                "NULL for locked-role rows (platform-managed). "
                "Populated for custom-role rows (Wall-1 tenant)."
            ),
        ),
        sa.Column(
            "locked_role",
            sa.String(64),
            nullable=True,
            comment=(
                "One of admin_owner / admin_manager / instance_operator "
                "/ read_only_viewer; matches scope_role enum values. "
                "NULL when this row binds a custom role."
            ),
        ),
        sa.Column(
            "custom_role_id",
            sa.Integer(),
            sa.ForeignKey("custom_roles.id", ondelete="CASCADE"),
            nullable=True,
            comment="NULL when this row binds a locked role.",
        ),
        sa.Column(
            "permission_id",
            sa.Integer(),
            sa.ForeignKey("permissions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            # XOR: exactly one of locked_role or custom_role_id is set.
            "(locked_role IS NOT NULL)::int + "
            "(custom_role_id IS NOT NULL)::int = 1",
            name="ck_role_permissions_locked_or_custom",
        ),
        sa.CheckConstraint(
            # Locked-role rows: admin_id MUST be NULL.
            # Custom-role rows: admin_id MUST be populated.
            "(locked_role IS NOT NULL AND admin_id IS NULL) OR "
            "(custom_role_id IS NOT NULL AND admin_id IS NOT NULL)",
            name="ck_role_permissions_admin_id_consistency",
        ),
        sa.CheckConstraint(
            "locked_role IS NULL OR locked_role IN ("
            "'admin_owner','admin_manager','instance_operator','read_only_viewer')",
            name="ck_role_permissions_locked_role_valid",
        ),
    )
    # Lookup index: locked-role permission set lookup.
    op.create_index(
        "ix_role_permissions_locked_role",
        _T_ROLE_PERMISSIONS,
        ["locked_role"],
        postgresql_where=sa.text("locked_role IS NOT NULL"),
    )
    # Lookup index: custom-role permission set lookup.
    op.create_index(
        "ix_role_permissions_custom_role_id",
        _T_ROLE_PERMISSIONS,
        ["custom_role_id"],
        postgresql_where=sa.text("custom_role_id IS NOT NULL"),
    )
    # Dedup: no duplicate (locked_role, permission_id) and no
    # duplicate (custom_role_id, permission_id). Two partial unique
    # indexes because Postgres rejects UNIQUE on nullable columns
    # uniformly.
    op.create_index(
        "uq_role_permissions_locked",
        _T_ROLE_PERMISSIONS,
        ["locked_role", "permission_id"],
        unique=True,
        postgresql_where=sa.text("locked_role IS NOT NULL"),
    )
    op.create_index(
        "uq_role_permissions_custom",
        _T_ROLE_PERMISSIONS,
        ["custom_role_id", "permission_id"],
        unique=True,
        postgresql_where=sa.text("custom_role_id IS NOT NULL"),
    )

    # ------------------------------------------------------------------
    # 4. user_role_assignments — bind User to (role + scope). RLS.
    # ------------------------------------------------------------------
    op.create_table(
        _T_USER_ROLE_ASSIGNMENTS,
        sa.Column(
            "id", sa.Integer(), primary_key=True, autoincrement=True
        ),
        sa.Column(
            "admin_id",
            sa.String(100),
            sa.ForeignKey("admins.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
            comment="Wall-1 tenant boundary. RLS fences on this column.",
        ),
        sa.Column(
            "user_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "locked_role",
            sa.String(64),
            nullable=True,
            comment=(
                "Locked-role binding. One of admin_owner / admin_manager "
                "/ instance_operator / read_only_viewer. NULL when "
                "custom_role_id is set."
            ),
        ),
        sa.Column(
            "custom_role_id",
            sa.Integer(),
            sa.ForeignKey("custom_roles.id", ondelete="RESTRICT"),
            nullable=True,
            comment="Custom-role binding. NULL when locked_role is set.",
        ),
        sa.Column(
            "scope_type",
            sa.String(32),
            nullable=False,
            comment=(
                "One of 'all_instances' (Admin-wide) or "
                "'instance_specific' (one Instance)."
            ),
        ),
        sa.Column(
            "instance_id",
            sa.Integer(),
            sa.ForeignKey("instances.id", ondelete="RESTRICT"),
            nullable=True,
            comment=(
                "Set when scope_type='instance_specific'. NULL otherwise."
            ),
        ),
        sa.Column(
            "assigned_by_user_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            server_onupdate=sa.func.now(),
        ),
        sa.CheckConstraint(
            "(locked_role IS NOT NULL)::int + "
            "(custom_role_id IS NOT NULL)::int = 1",
            name="ck_user_role_assignments_locked_or_custom",
        ),
        sa.CheckConstraint(
            "scope_type IN ('all_instances', 'instance_specific')",
            name="ck_user_role_assignments_scope_type_valid",
        ),
        sa.CheckConstraint(
            "(scope_type = 'instance_specific' AND instance_id IS NOT NULL) "
            "OR (scope_type = 'all_instances' AND instance_id IS NULL)",
            name="ck_user_role_assignments_scope_instance_consistency",
        ),
        sa.CheckConstraint(
            "locked_role IS NULL OR locked_role IN ("
            "'admin_owner','admin_manager','instance_operator','read_only_viewer')",
            name="ck_user_role_assignments_locked_role_valid",
        ),
    )
    op.create_index(
        "ix_user_role_assignments_user_admin_active",
        _T_USER_ROLE_ASSIGNMENTS,
        ["user_id", "admin_id"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    op.execute(
        f"ALTER TABLE {_T_USER_ROLE_ASSIGNMENTS} ENABLE ROW LEVEL SECURITY;"
    )
    op.execute(
        f"ALTER TABLE {_T_USER_ROLE_ASSIGNMENTS} FORCE ROW LEVEL SECURITY;"
    )
    op.execute(
        f"""
        CREATE POLICY user_role_assignments_tenant_isolation
        ON {_T_USER_ROLE_ASSIGNMENTS}
        AS PERMISSIVE
        FOR ALL
        TO PUBLIC
        USING (admin_id = current_setting('app.admin_id', true))
        WITH CHECK (admin_id = current_setting('app.admin_id', true));
        """
    )

    # ------------------------------------------------------------------
    # 5. Seeds — idempotent.
    # ------------------------------------------------------------------
    _seed_permission_catalog()
    _seed_locked_role_permissions()


def _seed_permission_catalog() -> None:
    """Insert (or update) every row in PERMISSION_CATALOG."""
    bind = op.get_bind()
    for row in PERMISSION_CATALOG:
        bind.execute(
            sa.text(
                f"""
                INSERT INTO {_T_PERMISSIONS}
                    (key, display_name, description, category)
                VALUES (:key, :display_name, :description, :category)
                ON CONFLICT (key) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    description = EXCLUDED.description,
                    category = EXCLUDED.category,
                    updated_at = now();
                """
            ),
            row,
        )


def _seed_locked_role_permissions() -> None:
    """For every (locked_role, permission_key) pair, INSERT IGNORE.

    Idempotent via the ``uq_role_permissions_locked`` partial unique
    index — re-running is a no-op for existing pairs.
    """
    bind = op.get_bind()
    # Build a permission_key → permission_id map once.
    rows = bind.execute(
        sa.text(f"SELECT id, key FROM {_T_PERMISSIONS}")
    ).fetchall()
    perm_id_by_key = {r.key: r.id for r in rows}

    for role, keys in LOCKED_ROLE_PERMISSIONS.items():
        for key in keys:
            perm_id = perm_id_by_key[key]
            bind.execute(
                sa.text(
                    f"""
                    INSERT INTO {_T_ROLE_PERMISSIONS}
                        (admin_id, locked_role, custom_role_id, permission_id)
                    VALUES (NULL, :role, NULL, :perm_id)
                    ON CONFLICT DO NOTHING;
                    """
                ),
                {"role": role, "perm_id": perm_id},
            )


def downgrade() -> None:
    # Reverse order: drop user_role_assignments first (FK to custom_roles),
    # then role_permissions (FKs to custom_roles + permissions), then
    # custom_roles, then permissions.

    # user_role_assignments — RLS down then drop.
    op.execute(
        f"DROP POLICY IF EXISTS user_role_assignments_tenant_isolation "
        f"ON {_T_USER_ROLE_ASSIGNMENTS};"
    )
    op.execute(
        f"ALTER TABLE {_T_USER_ROLE_ASSIGNMENTS} NO FORCE ROW LEVEL SECURITY;"
    )
    op.execute(
        f"ALTER TABLE {_T_USER_ROLE_ASSIGNMENTS} DISABLE ROW LEVEL SECURITY;"
    )
    op.drop_index(
        "ix_user_role_assignments_user_admin_active",
        table_name=_T_USER_ROLE_ASSIGNMENTS,
    )
    op.drop_table(_T_USER_ROLE_ASSIGNMENTS)

    # role_permissions — no RLS to tear down.
    op.drop_index(
        "uq_role_permissions_custom", table_name=_T_ROLE_PERMISSIONS
    )
    op.drop_index(
        "uq_role_permissions_locked", table_name=_T_ROLE_PERMISSIONS
    )
    op.drop_index(
        "ix_role_permissions_custom_role_id", table_name=_T_ROLE_PERMISSIONS
    )
    op.drop_index(
        "ix_role_permissions_locked_role", table_name=_T_ROLE_PERMISSIONS
    )
    op.drop_table(_T_ROLE_PERMISSIONS)

    # custom_roles — RLS down then drop.
    op.execute(
        f"DROP POLICY IF EXISTS custom_roles_tenant_isolation "
        f"ON {_T_CUSTOM_ROLES};"
    )
    op.execute(
        f"ALTER TABLE {_T_CUSTOM_ROLES} NO FORCE ROW LEVEL SECURITY;"
    )
    op.execute(
        f"ALTER TABLE {_T_CUSTOM_ROLES} DISABLE ROW LEVEL SECURITY;"
    )
    op.drop_index(
        "uq_custom_roles_admin_role_key_active",
        table_name=_T_CUSTOM_ROLES,
    )
    op.drop_table(_T_CUSTOM_ROLES)

    # permissions — global, no RLS.
    op.drop_index("ix_permissions_category", table_name=_T_PERMISSIONS)
    op.drop_table(_T_PERMISSIONS)
