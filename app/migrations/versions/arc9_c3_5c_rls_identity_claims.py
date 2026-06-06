"""Arc 9 C3.5c -- RLS policy on identity_claims (Wall 1 Layer 2).

Identity claim rows (verified email -> user binding). Cross-tenant leak would let admin A see admin B's verified identity bindings. RLS denies the read at the row gate.

Identical shape to Arc 9 C3.2 sibling batch. See arc9_c3_2a_rls_traces
for full design rationale -- this file deliberately mirrors it so
the C3.5 sibling Wall 1 RLS migrations form a coherent series.

Wall column on this table is ``tenant_id`` (NOT NULL). The RLS policy
compares it directly to ``current_setting('app.admin_id', true)`` --
the C2 listener writes the in-process admin slug into that GUC on
every transaction BEGIN.

Why USING + WITH CHECK *both* strict (no asymmetry):

  * USING alone leaks writes: an admin could INSERT a row with
    another admin's tenant_id and the policy would accept it
    because USING is only evaluated on read-back.
  * WITH CHECK alone leaks reads: SELECTs would not be filtered.
  * Both strict closes both halves. This is the standard pattern
    for the 6 NOT-NULL Wall 1 tables in C3.2 + C3.5; only NULL-
    permissive tables (knowledge_embeddings in C3.3) and the
    auth-perimeter table (api_keys in C3.4) deviate from this
    shape, and each carries a docstring explaining the deviation.

DEPLOY COUPLING (unchanged from C3.1-C3.4)
Migration + rls_tenant_context_enabled=true ECS env MUST ship in the
same deploy bundle.

Reversibility: zero-data-impact downgrade (drop policy, disable RLS).

Refs ARC9_RUNBOOK §C3.
"""

from __future__ import annotations

from alembic import op


revision = "arc9_c3_5c_rls_identity_claims"
down_revision = "arc9_c3_5b_rls_user_consents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE identity_claims ENABLE ROW LEVEL SECURITY;")
    op.execute(
        """
        CREATE POLICY identity_claims_tenant_isolation
        ON identity_claims
        AS PERMISSIVE
        FOR ALL
        TO PUBLIC
        USING (tenant_id = current_setting('app.admin_id', true))
        WITH CHECK (tenant_id = current_setting('app.admin_id', true));
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS identity_claims_tenant_isolation ON identity_claims;"
    )
    op.execute("ALTER TABLE identity_claims DISABLE ROW LEVEL SECURITY;")
