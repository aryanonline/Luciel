"""Unit 3 (tenant-isolation re-verify) — add PERMISSIVE base RLS policy
to four tables that had RESTRICTIVE-only policies.

THE DEFECT
==========
PostgreSQL RLS evaluates policies as: (OR of all PERMISSIVE policies)
AND (AND of all RESTRICTIVE policies). When a table has RLS enabled but
*no* PERMISSIVE policy, the permissive set is empty -> the OR is false
-> **every row is denied, including to the owning tenant.**

These four tenant tables shipped with a RESTRICTIVE-only isolation
policy and no permissive base policy:

  * knowledge_sources
  * knowledge_graph_nodes
  * knowledge_graph_edges
  * channel_routes

Under the production ``luciel_app`` role (non-superuser, non-BYPASSRLS)
this means the owning tenant reads ZERO rows from its own knowledge
base, knowledge graph, and channel routes. It was masked because (a)
the local stack historically connected as the ``postgres`` superuser
(which bypasses RLS) and (b) the live-Postgres RLS tests that would
have caught it were env-gated and silently skipped.

The cross-tenant DENIAL property was never at risk (a restrictive
policy denies other tenants correctly); the bug is that the OWNER is
also denied -- a functionality + correctness defect on the isolation
layer.

THE FIX
=======
Add a PERMISSIVE ``*_tenant_isolation`` policy per table with the same
predicate every working tenant table already uses
(``admin_id::text = current_setting('app.admin_id', true)``). The
existing RESTRICTIVE policies are kept: permissive admits the tenant's
own rows; restrictive further constrains. Their AND yields exactly the
tenant's rows -- belt-and-suspenders, matching the ``sessions`` table
which already carries both a permissive and a restrictive policy.

Idempotent (drops the policy if present before recreating) + reversible.

Revision ID: unit3_add_permissive_rls_base_policy
Revises: unit3_force_rls_data_export_jobs
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "unit3_add_permissive_rls_base_policy"
down_revision = "unit3_force_rls_data_export_jobs"
branch_labels = None
depends_on = None


# (table, policy_name). Predicate is identical for all four.
_TABLES = [
    ("knowledge_sources", "knowledge_sources_tenant_isolation"),
    ("knowledge_graph_nodes", "kg_nodes_tenant_isolation"),
    ("knowledge_graph_edges", "kg_edges_tenant_isolation"),
    ("channel_routes", "channel_routes_tenant_isolation"),
]

_PREDICATE = "(admin_id)::text = current_setting('app.admin_id', true)"


def _table_exists(name: str) -> bool:
    insp = sa.inspect(op.get_bind())
    return name in insp.get_table_names()


def upgrade() -> None:
    for table, policy in _TABLES:
        if not _table_exists(table):
            continue
        # Idempotent: drop-if-exists then create as PERMISSIVE FOR ALL.
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
        op.execute(
            f"""
            CREATE POLICY {policy} ON {table}
                AS PERMISSIVE
                FOR ALL
                USING ({_PREDICATE})
                WITH CHECK ({_PREDICATE})
            """
        )


def downgrade() -> None:
    for table, policy in _TABLES:
        if not _table_exists(table):
            continue
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
