"""Arc 5 — Revision C: Admin→Instance subtractive cutover.

This is the **forward-only** subtractive half of the Admin→Instance
tenancy collapse. By the time this migration runs in PROD, Revision B
has already:

* Backfilled active rows from ``tenant_configs`` → ``admins``.
* Backfilled active rows from ``luciel_instances`` → ``instances``.
* Renamed legacy tier values (``individual``/``solo`` → ``pro``;
  ``team``/``company`` → ``enterprise``; orphan/unknown → ``free``).
* Emitted ``LEGACY_FIXTURE_PURGED`` audit rows for inactive legacy
  fixtures.

Revision C performs the **aggressive cleanup** the partner authorized:

1. Re-point all live FKs that still reference legacy tables
   (``luciel_instances.id`` and ``tenant_configs.tenant_id``) at the
   V2 tables (``instances.id`` / ``admins.id``). The column names on
   the dependent tables are preserved (``luciel_instance_id`` /
   ``tenant_id``) so app-layer code does not need a rename.
2. Drop the legacy tables themselves: ``tenant_configs``,
   ``luciel_instances``, ``domain_configs``, ``agent_configs``,
   ``agents``. Order matters — leaf-first.
3. Drop the V1 back-pointer columns on ``admins`` and ``instances``
   (``legacy_tenant_id``, ``legacy_luciel_instance_id``,
   ``legacy_agent_id``) and their partial-unique indexes.
4. Tighten the ``admins.tier`` CHECK constraint to the V2 vocabulary
   only (``free``/``pro``/``enterprise``). The transitional CHECK
   ``ck_admins_tier_valid_during_migration`` from Revision A is
   dropped and replaced by the final ``ck_admins_tier_valid``.

Forward-only by design. ``downgrade()`` raises ``NotImplementedError``:
the migration drops tables and data; recovery is via the RDS snapshot
taken immediately before ``alembic upgrade head`` runs Revision C.

Defensive constraint introspection
----------------------------------
Some FKs on ``memory_items``, ``scope_assignments``, and ``user_invites``
were originally declared with inline ``sa.ForeignKey`` (no explicit
``name=``), so PostgreSQL auto-named them. We introspect the live
schema for the FK on each (table, column) pair → (referent_table) and
drop by the discovered name. The new V2 FKs are then created with
explicit, stable names.

Revision: arc5_c_admin_instance_subtractive
Revises: arc5_b_admin_instance_cutover
Create Date: 2026-05-23
"""

from __future__ import annotations

from typing import Iterable

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "arc5_c_admin_instance_subtractive"
down_revision = "arc5_b_admin_instance_cutover"
branch_labels = None
depends_on = None


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _drop_fks_referencing(
    bind,
    *,
    source_table: str,
    source_column: str,
    referent_table: str,
) -> Iterable[str]:
    """Drop every FK on (source_table, source_column) → referent_table.

    Returns the names of dropped constraints (useful for logging). Uses
    ``information_schema`` for portability across PG versions.
    """
    rows = bind.execute(
        sa.text(
            """
            SELECT tc.constraint_name
            FROM information_schema.table_constraints AS tc
            JOIN information_schema.key_column_usage AS kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema    = kcu.table_schema
            JOIN information_schema.referential_constraints AS rc
              ON tc.constraint_name = rc.constraint_name
             AND tc.table_schema    = rc.constraint_schema
            JOIN information_schema.constraint_column_usage AS ccu
              ON rc.unique_constraint_name = ccu.constraint_name
             AND rc.unique_constraint_schema = ccu.constraint_schema
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_name      = :src_table
              AND kcu.column_name    = :src_col
              AND ccu.table_name     = :ref_table
            """
        ),
        {
            "src_table": source_table,
            "src_col": source_column,
            "ref_table": referent_table,
        },
    ).fetchall()
    dropped = []
    for (name,) in rows:
        op.drop_constraint(name, source_table, type_="foreignkey")
        dropped.append(name)
    return dropped


# -----------------------------------------------------------------------------
# Upgrade
# -----------------------------------------------------------------------------
def upgrade() -> None:
    bind = op.get_bind()

    # -------------------------------------------------------------------------
    # 1. Drop FKs that still reference legacy tables
    # -------------------------------------------------------------------------
    # Named FKs (declared with explicit name= in earlier migrations)
    op.drop_constraint(
        "fk_api_keys_luciel_instance_id",
        "api_keys",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_knowledge_embeddings_luciel_instance_id",
        "knowledge_embeddings",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_memory_items_luciel_instance_id",
        "memory_items",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_conversations_tenant_id_tenant_configs",
        "conversations",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_identity_claims_tenant_id_tenant_configs",
        "identity_claims",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_scope_assignments_tenant_id_tenant_configs",
        "scope_assignments",
        type_="foreignkey",
    )

    # Unnamed FK on user_invites.tenant_id → tenant_configs.tenant_id.
    # Discovered via information_schema introspection (PG auto-named).
    _drop_fks_referencing(
        bind,
        source_table="user_invites",
        source_column="tenant_id",
        referent_table="tenant_configs",
    )

    # -------------------------------------------------------------------------
    # 2. Re-create FKs pointing at V2 tables (admins / instances).
    #    Column names on dependent tables are preserved.
    # -------------------------------------------------------------------------
    op.create_foreign_key(
        "fk_api_keys_luciel_instance_id",
        source_table="api_keys",
        referent_table="instances",
        local_cols=["luciel_instance_id"],
        remote_cols=["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_knowledge_embeddings_luciel_instance_id",
        source_table="knowledge_embeddings",
        referent_table="instances",
        local_cols=["luciel_instance_id"],
        remote_cols=["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_memory_items_luciel_instance_id",
        source_table="memory_items",
        referent_table="instances",
        local_cols=["luciel_instance_id"],
        remote_cols=["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_conversations_tenant_id_admins",
        source_table="conversations",
        referent_table="admins",
        local_cols=["tenant_id"],
        remote_cols=["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_identity_claims_tenant_id_admins",
        source_table="identity_claims",
        referent_table="admins",
        local_cols=["tenant_id"],
        remote_cols=["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_scope_assignments_tenant_id_admins",
        source_table="scope_assignments",
        referent_table="admins",
        local_cols=["tenant_id"],
        remote_cols=["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_user_invites_tenant_id_admins",
        source_table="user_invites",
        referent_table="admins",
        local_cols=["tenant_id"],
        remote_cols=["id"],
        ondelete="RESTRICT",
    )

    # -------------------------------------------------------------------------
    # 3. Drop legacy tables (leaf-first).
    #    By this point no FK references them.
    # -------------------------------------------------------------------------
    # Drop any FKs that point INTO legacy tables from other legacy tables
    # (we drop the tables themselves below, but introspection-based cleanup
    # protects against orphan FKs left by an interrupted earlier migration).
    for legacy_tbl in (
        "agents",
        "agent_configs",
        "domain_configs",
        "luciel_instances",
        "tenant_configs",
    ):
        bind.execute(sa.text(f'DROP TABLE IF EXISTS "{legacy_tbl}" CASCADE'))

    # -------------------------------------------------------------------------
    # 4. Drop V1 back-pointer columns + their partial-unique indexes.
    # -------------------------------------------------------------------------
    op.drop_index("ix_admins_legacy_tenant_id", table_name="admins")
    op.drop_column("admins", "legacy_tenant_id")

    op.drop_index(
        "ix_instances_legacy_luciel_instance_id",
        table_name="instances",
    )
    op.drop_column("instances", "legacy_luciel_instance_id")

    op.drop_index(
        "ix_instances_legacy_agent_id",
        table_name="instances",
    )
    op.drop_column("instances", "legacy_agent_id")

    # -------------------------------------------------------------------------
    # 5. Tighten admins.tier CHECK to V2 vocabulary only.
    # -------------------------------------------------------------------------
    op.drop_constraint(
        "ck_admins_tier_valid_during_migration",
        "admins",
        type_="check",
    )
    op.create_check_constraint(
        "ck_admins_tier_valid",
        "admins",
        "tier IN ('free', 'pro', 'enterprise')",
    )


# -----------------------------------------------------------------------------
# Downgrade — forward-only by design
# -----------------------------------------------------------------------------
def downgrade() -> None:
    raise NotImplementedError(
        "Revision C (arc5_c_admin_instance_subtractive) is forward-only. "
        "It drops the legacy tenant_configs / luciel_instances / agents / "
        "agent_configs / domain_configs tables and is destructive. "
        "Rollback path is restoring the RDS snapshot taken immediately "
        "before `alembic upgrade head` ran Revision C."
    )
