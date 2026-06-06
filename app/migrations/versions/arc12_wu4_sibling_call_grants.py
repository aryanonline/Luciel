"""Arc 12 WU4 — sibling_call_grants table + RLS.

Revision ID: arc12_wu4_sibling_call_grants
Revises: arc12_wu2_instance_tool_authorizations
Create Date: 2026-05-28

Why this migration exists
-------------------------

Arc 12 WU4 introduces sibling-Luciel composition grants per
Architecture §3.3.4. A sibling-call grant authorises the
``call_sibling_luciel`` tool to dispatch a call from one Instance
(``caller_instance_id``) to another Instance under the same Admin
(``callee_instance_id``). The grant rows are the row-level surface
for Wall-2 + Wall-3 enforcement at the sibling-composition layer:

* Wall-1: the ``admin_id`` column scopes the grant to a single Admin
  (cross-Admin composition is structurally impossible by FK + RLS).
* Wall-2: the grant-authoring API enforces ``ScopePolicy
  .enforce_role_on_instance`` on BOTH the caller and the callee
  Instance — a user scoped to only one side cannot author a
  cross-Instance grant. (Policy is enforced at the route layer; the
  table is just the durable record.)
* Wall-3: every dispatch (WU5) re-resolves the grant row at call
  time, so an instance-scoped operator cannot piggy-back on a stale
  cross-Instance authorisation.

WU5 (the sibling runtime) consumes these rows at dispatch time:
cycle detection → fan-out budget → master switch
(``instance_tool_authorizations`` for ``call_sibling_luciel`` on
BOTH endpoints) → grant lookup (this table, ``approval_state='live'``).

Schema decisions
----------------

* ``admin_id`` is ``String(100)`` matching ``admins.id`` and the
  Wall-1 column convention everywhere else.
* ``caller_instance_id`` / ``callee_instance_id`` are ``Integer``
  matching ``instances.id`` per the Arc 5 PK doctrine.
* ``granted_by_user_id`` / ``approved_by_user_id`` are ``UUID``
  matching ``users.id`` (PG_UUID); RESTRICT on delete because the
  audit trail must survive user deletion.
* ``approval_state`` is a ``String(20)`` with a CHECK constraint
  pinning it to ``'live' | 'pending_approval' | 'revoked'``. A
  CHECK rather than a PG ENUM mirrors the Arc 9 admin_audit_logs
  convention (strings + advisory tuple) — new states can be added
  in code without a schema migration, and SQLAlchemy renders the
  CHECK cleanly on both Postgres and SQLite (the latter is what
  the unit tests run against).
* ``granted_at`` is non-null with ``server_default=now()`` — every
  row was granted at some moment.
* ``approved_at`` / ``approved_by_user_id`` / ``revoked_at`` are
  nullable; they're set at state transitions.

Indexes & constraints
---------------------

* **Composite index** on ``(admin_id, caller_instance_id)`` — the
  hot-path lookup the WU5 runtime dispatch uses (filter to the
  caller's Admin + caller Instance, then check the callee).
  Filtered by ``approval_state = 'live'`` so the index is small.
* **Partial unique index** on
  ``(admin_id, caller_instance_id, callee_instance_id)
  WHERE approval_state != 'revoked'``. This enforces the §3.3.4
  invariant: no two non-revoked rows for the same edge. Revoke +
  re-author works because the revoked row is excluded from the
  index. Modelled as a partial unique INDEX following the
  ``ix_composition_grants_active`` (Arc 5) and
  ``uq_instance_tool_authorizations_active`` (Arc 12 WU2)
  precedents.
* CHECK ``caller_instance_id != callee_instance_id`` — a sibling
  call to oneself is a structural error (it would be a self-call,
  which is what the chat loop already does); the cycle detection
  in WU5 catches the multi-step case but the trivial self-edge is
  caught at the DB layer for free.

RLS posture (§3.7.5)
--------------------

Mirrors the Arc 12 WU2 ``instance_tool_authorizations`` policy
exactly (which in turn mirrors Arc 9 ``instances``):

1. ``ENABLE ROW LEVEL SECURITY`` — turn RLS on.
2. ``FORCE ROW LEVEL SECURITY`` — make RLS apply to the table
   owner too (Arc 9 C10.a doctrine; ``luciel_app`` is NOBYPASSRLS
   but FORCE seals the ownership escape).
3. PERMISSIVE policy ``sibling_call_grants_tenant_isolation`` on
   the wall column ``admin_id``. USING + WITH CHECK both strict so
   reads and writes are equally fenced.

When ``app.admin_id`` is unset, ``current_setting(..., true)``
returns NULL; ``admin_id = NULL`` is NULL in three-valued logic;
RLS treats NULL as "deny." Fail-closed by construction.

Grants
------

``ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT,
UPDATE, DELETE ON TABLES TO luciel_app`` was installed at Arc 9
C10.b. Tables created after that migration inherit the grant; no
explicit grant is issued here.

Rollback contract
-----------------

``alembic downgrade -1`` drops the table, indexes, and RLS policy.
Data-safe: dropping a default-deny grant table widens visibility —
never narrows it.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID


revision = "arc12_wu4_sibling_call_grants"
down_revision = "arc12_wu2_instance_tool_authorizations"
branch_labels = None
depends_on = None


_TABLE = "sibling_call_grants"


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
                "Wall-1 tenant boundary. Both caller and callee Instances "
                "belong to this Admin; cross-Admin sibling calls are "
                "structurally impossible."
            ),
        ),
        sa.Column(
            "caller_instance_id",
            sa.Integer(),
            sa.ForeignKey("instances.id", ondelete="RESTRICT"),
            nullable=False,
            comment=(
                "The Instance whose ``call_sibling_luciel`` dispatch "
                "is authorised by this grant."
            ),
        ),
        sa.Column(
            "callee_instance_id",
            sa.Integer(),
            sa.ForeignKey("instances.id", ondelete="RESTRICT"),
            nullable=False,
            comment=(
                "The Instance the caller is permitted to invoke as a "
                "sibling target."
            ),
        ),
        sa.Column(
            "granted_by_user_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
            comment=(
                "The admin-team member who authored the grant. RESTRICT — "
                "soft-delete users, never lose authorship."
            ),
        ),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "approval_state",
            sa.String(20),
            nullable=False,
            comment=(
                "One of 'live' | 'pending_approval' | 'revoked'. "
                "Pro tier authors land 'live' immediately; Enterprise "
                "authors land 'pending_approval' until an admin_owner "
                "approves. Revoke is a terminal transition."
            ),
        ),
        sa.Column(
            "approved_by_user_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
            comment=(
                "The admin_owner who flipped pending_approval → live. "
                "NULL on Pro-tier rows (no approval step) and on "
                "pending/revoked rows."
            ),
        ),
        sa.Column(
            "approved_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Set when this grant transitions to approval_state="
                "'revoked' (either via the revoke API on a live row, "
                "or via the reject API on a pending_approval row, or "
                "via the instance-deactivation cascade). Stays NULL "
                "while the grant is live or pending."
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
        sa.CheckConstraint(
            "caller_instance_id <> callee_instance_id",
            name="ck_sibling_call_grants_no_self_edge",
        ),
        sa.CheckConstraint(
            "approval_state IN ('live', 'pending_approval', 'revoked')",
            name="ck_sibling_call_grants_approval_state",
        ),
    )

    # Composite covering index for the WU5 runtime dispatch hot path:
    # "given the caller's admin + caller instance, find live callees".
    op.create_index(
        "ix_sibling_call_grants_dispatch",
        _TABLE,
        ["admin_id", "caller_instance_id"],
        postgresql_where=sa.text("approval_state = 'live'"),
    )

    # Partial unique index — at-most-one non-revoked row per
    # (admin_id, caller_instance_id, callee_instance_id) triple. A
    # revoke (terminal state) excludes the row from the index so
    # re-authoring after revoke is allowed.
    op.create_index(
        "uq_sibling_call_grants_active",
        _TABLE,
        ["admin_id", "caller_instance_id", "callee_instance_id"],
        unique=True,
        postgresql_where=sa.text("approval_state <> 'revoked'"),
    )

    # ------------------------------------------------------------------
    # 2. RLS posture — mirrors arc12_wu2_instance_tool_authorizations
    #    exactly (which in turn mirrors arc9_c3_5d_rls_instances).
    # ------------------------------------------------------------------
    op.execute(
        f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY;"
    )
    op.execute(
        f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY;"
    )
    op.execute(
        f"""
        CREATE POLICY sibling_call_grants_tenant_isolation
        ON {_TABLE}
        AS PERMISSIVE
        FOR ALL
        TO PUBLIC
        USING (admin_id = current_setting('app.admin_id', true))
        WITH CHECK (admin_id = current_setting('app.admin_id', true));
        """
    )


def downgrade() -> None:
    # Reverse order:

    # 2. RLS teardown.
    op.execute(
        f"DROP POLICY IF EXISTS "
        f"sibling_call_grants_tenant_isolation "
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
        "uq_sibling_call_grants_active", table_name=_TABLE
    )
    op.drop_index(
        "ix_sibling_call_grants_dispatch", table_name=_TABLE
    )
    op.drop_table(_TABLE)
