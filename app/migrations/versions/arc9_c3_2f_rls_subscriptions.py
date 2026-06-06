"""Arc 9 C3.2f -- RLS policy on subscriptions (Wall 1 Layer 2).

Stripe subscription rows.

Identical shape to Arc 9 C3.1 (admin_audit_logs). See that migration
for full design rationale -- this file deliberately mirrors it so
the 8 sibling Wall 1 RLS migrations form a coherent series.

Why a separate Alembic revision per table (not one mega-revision):

  * Independent rollback: ``downgrade -1`` can peel back exactly one
    table without affecting the others. Critical for incident response.
  * Smaller commits in the Alembic version history: any future
    "blame this RLS policy" investigation has a one-table-per-rev
    narrative trail.
  * No migration-time table contention: each ALTER TABLE ENABLE ROW
    LEVEL SECURITY takes a brief ACCESS EXCLUSIVE lock; serialising
    them keeps the lock window per-table small.

The 8 sibling migrations are written as a contiguous chain
(arc9_c3_1 -> arc9_c3_2a -> ... -> arc9_c3_2g). Operators ALWAYS
deploy them as a single bundle alongside the C2 feature-flag flip;
the chain ensures Alembic refuses to apply them out of order.

Reversibility: zero-data-impact downgrade (drop policy, disable RLS).

Refs ARC9_RUNBOOK §C3.
"""

from __future__ import annotations

from alembic import op


revision = "arc9_c3_2f_rls_subscriptions"
down_revision = "arc9_c3_2e_rls_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE subscriptions ENABLE ROW LEVEL SECURITY;")
    op.execute(
        """
        CREATE POLICY subscriptions_tenant_isolation
        ON subscriptions
        AS PERMISSIVE
        FOR ALL
        TO PUBLIC
        USING (tenant_id = current_setting('app.admin_id', true))
        WITH CHECK (tenant_id = current_setting('app.admin_id', true));
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS subscriptions_tenant_isolation ON subscriptions;"
    )
    op.execute("ALTER TABLE subscriptions DISABLE ROW LEVEL SECURITY;")
