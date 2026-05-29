"""Arc 12 WU2 — instance_tool_authorizations table + RLS.

Revision ID: arc12_wu2_instance_tool_authorizations
Revises: arc11_closeout_b_ingestion_error_code
Create Date: 2026-05-28

Why this migration exists
-------------------------

Arc 12 WU1 migrated ``LucielTool`` to the §3.3.1 contract and added
``ToolContext`` carrying ``admin_id`` + ``instance_id`` on every
invocation. WU2 makes per-instance tool authorisation a load-bearing
default-deny gate at the broker: a tool can only dispatch on an
instance if there is a non-revoked authorisation row for the tuple
``(admin_id, instance_id, tool_id)``.

The row's mere existence (with ``revoked_at IS NULL``) is the
authorisation signal. A row may carry ``enabled=False`` to express
a paused-but-not-revoked state for future admin UX (e.g.
temporarily disabling a tool without losing the configuration);
the broker treats ``enabled=False`` as denied (Decision: distinct
"paused" vs "revoked" tracks land later; WU2 keeps the broker check
simple — row present AND not revoked AND enabled).

Schema decisions
----------------

* ``admin_id`` is ``String(100)`` matching ``admins.id`` and the
  Wall-1 column convention everywhere else.
* ``instance_id`` is ``Integer`` matching ``instances.id`` per the
  Arc 5 PK doctrine.
* ``tool_id`` is ``String(64)`` — same width as
  ``admin_audit_logs.action`` so the tool catalog can reuse the
  same upper bound without bumping.
* ``authorized_by_user_id`` is ``UUID`` matching ``users.id``
  (PG_UUID); RESTRICT on delete because the audit trail must survive
  user deletion.
* ``revoked_at`` is nullable; soft-delete per §5.5 Pattern E.

Partial unique constraint
-------------------------

``uq_instance_tool_authorizations_active`` enforces at-most-one
non-revoked row per ``(admin_id, instance_id, tool_id)``. Modelled
as a partial unique INDEX (PostgreSQL only) — the Arc 5 pattern
established in ``ix_composition_grants_active`` and the Arc 11
``knowledge_sources`` table use the same shape. Revoking a row
flips ``revoked_at`` to NOW; a fresh authorisation for the same
tuple inserts a new row, and the old + new coexist with one of
them non-NULL on ``revoked_at`` so the partial index is satisfied.

RLS posture (§3.7.5)
--------------------

Mirrors the Arc 9 ``instances`` table policy (file
``arc9_c3_5d_rls_instances.py``):

1. ``ENABLE ROW LEVEL SECURITY`` — turn RLS on.
2. ``FORCE ROW LEVEL SECURITY`` — make RLS apply to the table owner
   too (Arc 9 C10.a doctrine; ``luciel_app`` is NOBYPASSRLS but
   FORCE seals the ownership escape).
3. PERMISSIVE policy ``instance_tool_authorizations_tenant_isolation``
   on the wall column ``admin_id``. USING + WITH CHECK both strict
   so reads and writes are equally fenced (matching the Arc 9 C11
   "strict-tenant tables" shape that ``instances`` and
   ``admin_audit_logs`` use).

When ``app.admin_id`` is unset, ``current_setting(..., true)``
returns NULL; ``admin_id = NULL`` is NULL in three-valued logic;
RLS treats NULL as "deny." Fail-closed by construction (matches
Arc 9 WS4b doctrine — empty/unset GUC = deny, not silent-empty).

Grants
------

``ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT,
UPDATE, DELETE ON TABLES TO luciel_app`` was installed at Arc 9
C10.b. Tables created after that migration inherit the grant; no
explicit grant is issued here (one source of truth).

Stale-column drop (alignment with WU1 Decision #19)
---------------------------------------------------

Arc 12 WU1 retired the ``max_composition_depth`` field from
``TierEntitlement`` (Decision #19 — no depth limit, no edge cap on
the customer-facing composition graph). The corresponding DB
column on ``admin_tier_overrides`` (created at
``arc5_a_admin_instance_additive.py``) was left in place by WU1
with a docstring note saying "deferred drop with the Arc 12 schema
sweep." This is that sweep — the column is now schema drift and is
dropped forward. The downgrade re-adds it as a nullable Integer for
reversibility.

A grep confirmed there is no ``max_composition_depth_override``
column on ``admin_tier_overrides`` — the only references to that
name are comments in ``arc5_a_admin_instance_additive.py`` line 363
and ``app/policy/entitlements.py``. The column itself is named
``max_composition_depth`` per the WIDE-ROW convention (column name
== field name; the "override" semantics ride on the row's presence,
not on column naming).

Rollback contract
-----------------

``alembic downgrade -1`` drops the new table (and re-adds the
``max_composition_depth`` column to ``admin_tier_overrides`` as
nullable Integer). Both moves are data-safe: dropping a default-deny
authorisation table widens visibility — never narrows it — and
adding back a nullable column with no backfill is zero-touch.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID


revision = "arc12_wu2_instance_tool_authorizations"
down_revision = "arc11_closeout_b_ingestion_error_code"
branch_labels = None
depends_on = None


_TABLE = "instance_tool_authorizations"


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. Create the table.
    # ------------------------------------------------------------------
    op.create_table(
        _TABLE,
        sa.Column(
            "id", sa.Integer(), primary_key=True, autoincrement=True
        ),
        sa.Column(
            "admin_id",
            sa.String(100),
            sa.ForeignKey("admins.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
            comment=(
                "Wall-1 tenant boundary. The owning Admin; every row "
                "is scoped to exactly one Admin. RLS fences on this "
                "column."
            ),
        ),
        sa.Column(
            "instance_id",
            sa.Integer(),
            sa.ForeignKey("instances.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
            comment=(
                "Wall-3 instance boundary. The Instance the tool is "
                "authorised on."
            ),
        ),
        sa.Column(
            "tool_id",
            sa.String(64),
            nullable=False,
            comment=(
                "§3.3.1 tool_id (e.g. 'send_email'). Matches the "
                "registry key the broker dispatches against."
            ),
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
            comment=(
                "Active flag. ``enabled=False`` is a paused-but-not-"
                "revoked state — the broker denies dispatch on both "
                "``enabled=False`` and ``revoked_at IS NOT NULL``."
            ),
        ),
        sa.Column(
            "authorized_by_user_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
            comment=(
                "The admin-team member who minted the authorisation "
                "row (audit trail). RESTRICT — soft-delete users, "
                "never lose authorship."
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
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Soft-revoke timestamp (§5.5 Pattern E). NULL means "
                "the row is live; non-NULL means revoked."
            ),
        ),
    )

    # Partial unique index — at-most-one live row per
    # (admin_id, instance_id, tool_id) tuple. Revoked rows are
    # excluded so revoke + re-authorise can coexist as separate rows.
    op.create_index(
        "uq_instance_tool_authorizations_active",
        _TABLE,
        ["admin_id", "instance_id", "tool_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    # Composite covering index for the broker's hot-path lookup.
    op.create_index(
        "ix_instance_tool_authorizations_lookup",
        _TABLE,
        ["admin_id", "instance_id", "tool_id"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    # ------------------------------------------------------------------
    # 2. RLS posture — mirrors arc9_c3_5d_rls_instances.py exactly.
    # ------------------------------------------------------------------
    op.execute(
        f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY;"
    )
    op.execute(
        f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY;"
    )
    op.execute(
        f"""
        CREATE POLICY instance_tool_authorizations_tenant_isolation
        ON {_TABLE}
        AS PERMISSIVE
        FOR ALL
        TO PUBLIC
        USING (admin_id = current_setting('app.admin_id', true))
        WITH CHECK (admin_id = current_setting('app.admin_id', true));
        """
    )

    # ------------------------------------------------------------------
    # 3. Forward drop of stale ``admin_tier_overrides.max_composition_depth``
    #    — alignment with WU1 Decision #19. See module docstring for
    #    full rationale.
    # ------------------------------------------------------------------
    op.drop_column("admin_tier_overrides", "max_composition_depth")


def downgrade() -> None:
    # Reverse order:

    # 3. Re-add the dropped column.
    op.add_column(
        "admin_tier_overrides",
        sa.Column(
            "max_composition_depth",
            sa.Integer(),
            nullable=True,
        ),
    )

    # 2. RLS teardown.
    op.execute(
        f"DROP POLICY IF EXISTS "
        f"instance_tool_authorizations_tenant_isolation "
        f"ON {_TABLE};"
    )
    op.execute(
        f"ALTER TABLE {_TABLE} NO FORCE ROW LEVEL SECURITY;"
    )
    op.execute(
        f"ALTER TABLE {_TABLE} DISABLE ROW LEVEL SECURITY;"
    )

    # 1. Drop indexes + table.
    op.drop_index(
        "ix_instance_tool_authorizations_lookup", table_name=_TABLE
    )
    op.drop_index(
        "uq_instance_tool_authorizations_active", table_name=_TABLE
    )
    op.drop_table(_TABLE)
