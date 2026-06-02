"""Arc 16 (b) — fix knowledge_sources deny-all: add missing PERMISSIVE base-grant.

Pre-existing Arc 11 defect, surfaced by running the live RLS tests for the
first time (they had never executed — the harness referenced a dropped
``admins.email`` column and used non-parameterizable ``SET LOCAL = %s``, so
they errored before asserting anything).

The bug
-------
PostgreSQL RLS returns a row to a non-BYPASSRLS role only if it passes
**at least one PERMISSIVE policy AND all RESTRICTIVE policies**. With zero
PERMISSIVE policies, the table is deny-all for every tenant role — no
PERMISSIVE policy means no base grant for the AND to build on.

``arc11_d1`` created ``knowledge_sources`` with TWO RESTRICTIVE policies
(``_admin_isolation`` FOR ALL, ``_admin_isolation_write`` FOR INSERT) and
**no PERMISSIVE policy**. Its own docstring says "Restrictive policies AND
together with permissive [policies]" — the intent was correct, but the
permissive base-grant was never created. Result: under the real
``luciel_app`` role, every tenant read of knowledge_sources returns zero
rows, including the admin raw-knowledge-view source list (Architecture
§3.2.2). It went unnoticed because (a) the live tests never ran and
(b) ``knowledge_retrieval_enabled`` is off, so nothing exercised it live.

Every other working tenant table (``instances``, ``conversations``,
``knowledge_chunks`` post arc16_a) has the RESTRICTIVE-fence +
PERMISSIVE-base-grant pair. ``knowledge_sources`` was the sole outlier.

The fix
-------
Add ``knowledge_sources_tenant_permissive`` — a PERMISSIVE FOR ALL policy
with the same strict ``admin_id = app.admin_id`` predicate, mirroring
``instances_tenant_permissive`` exactly. The RESTRICTIVE fence stays as the
absolute boundary; the PERMISSIVE policy provides the base grant the AND
needs. Net effect: a tenant sees its own sources (and ONLY its own), which
is what arc11_d1 intended.

Reversibility
-------------
``downgrade()`` drops the permissive policy, restoring the (broken) pre-fix
all-RESTRICTIVE state.

Refs: arc11_d1_rls_knowledge_sources.py (the incomplete original),
arc9_c11_tenant_restrictive.py (the RESTRICTIVE+PERMISSIVE pattern),
instances_tenant_permissive (the live shape this mirrors), Architecture
§3.2.2 (raw knowledge view depends on source reads working).
"""
from __future__ import annotations

from alembic import op


revision = "arc16_b_knowledge_sources_permissive_fix"
down_revision = "arc16_a_knowledge_chunks_strict_tenant"
branch_labels = None
depends_on = None


_POLICY = "knowledge_sources_tenant_permissive"
_TABLE = "knowledge_sources"


def upgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS {_POLICY} ON {_TABLE};")
    op.execute(
        f"""
        CREATE POLICY {_POLICY}
            ON {_TABLE}
            AS PERMISSIVE
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
    op.execute(f"DROP POLICY IF EXISTS {_POLICY} ON {_TABLE};")
