"""Arc 16 (a) — harden knowledge_chunks to strict-tenant RLS.

Founder decision (2026-06-02): cross-tenant / platform-curated knowledge
must not exist. Vision §3.3 — "Across Admins: never. Hard tenant isolation."
A NULL-admin chunk readable by every tenant is a hole in that wall, and it
is the ONLY one of the four C11-excluded tables that holds customer-facing
reasoning content (the other three — retention_policies, deletion_logs,
api_keys — are platform infrastructure rows and are intentionally left
NULL-admin; they are out of scope here).

This also resolves an internal inconsistency: ``knowledge_sources`` is
already RESTRICTIVE + strict ``admin_id = app.admin_id`` (arc11_d1), but its
child ``knowledge_chunks`` was left PERMISSIVE with an ``admin_id IS NULL OR``
read carve-out (arc9_c3_3, kept by arc9_c11). A source and its chunks were
governed by different isolation postures. After this migration they match.

What changes
------------
``knowledge_chunks_tenant_isolation`` is dropped (PERMISSIVE,
``admin_id IS NULL OR admin_id = app.admin_id``) and re-created as
RESTRICTIVE with the strict predicate ``admin_id = app.admin_id`` — the
exact shape arc9_c11 applied to every non-excluded tenant table and that
``knowledge_sources`` already uses. The instance-isolation PERMISSIVE policy
is left as-is (it is the Wall-3 additional fence; tenant isolation is now the
absolute RESTRICTIVE boundary, AND-combined per PostgreSQL RESTRICTIVE
semantics).

Reversibility
-------------
``downgrade()`` restores the pre-Arc16 PERMISSIVE policy WITH the
``admin_id IS NULL OR`` carve-out, so the migration round-trips to the exact
arc9_c3_3/arc9_c11 state.

Refs: Vision §3.3 (hard tenant isolation), arc9_c11_tenant_restrictive.py
(the RESTRICTIVE-flip pattern this follows), arc11_d1 (the target posture
knowledge_sources already has).
"""
from __future__ import annotations

from alembic import op


revision = "arc16_a_knowledge_chunks_strict_tenant"
down_revision = "arc15_c_drop_system_prompt_additions"
branch_labels = None
depends_on = None


_POLICY = "knowledge_chunks_tenant_isolation"
_TABLE = "knowledge_chunks"


def upgrade() -> None:
    # Drop the permissive NULL-permissive policy and replace with the
    # RESTRICTIVE strict-tenant fence. DROP is idempotent via IF EXISTS so a
    # partially-applied state can be re-run.
    op.execute(f"DROP POLICY IF EXISTS {_POLICY} ON {_TABLE};")
    op.execute(
        f"""
        CREATE POLICY {_POLICY}
            ON {_TABLE}
            AS RESTRICTIVE
            FOR ALL
            USING (
                (admin_id)::text = current_setting('app.admin_id', true)
            )
            WITH CHECK (
                (admin_id)::text = current_setting('app.admin_id', true)
            );
        """
    )


def downgrade() -> None:
    # Restore the exact pre-Arc16 permissive carve-out (arc9_c3_3 shape).
    op.execute(f"DROP POLICY IF EXISTS {_POLICY} ON {_TABLE};")
    op.execute(
        f"""
        CREATE POLICY {_POLICY}
            ON {_TABLE}
            AS PERMISSIVE
            FOR ALL
            USING (
                admin_id IS NULL
                OR (admin_id)::text = current_setting('app.admin_id', true)
            );
        """
    )
