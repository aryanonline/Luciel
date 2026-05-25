"""Arc 9 C5.1 -- RLS policy on messages (Wall 1 Layer 2 -- the strict shape).

Per-tenant row isolation on the messages table. This is the FOURTH
wall conceptually (intra-tenant session isolation) but mechanically
identical in shape to the seven C3.2 Wall-1 tables: strict
PERMISSIVE/ALL/PUBLIC policy keyed on tenant_id matching the
``app.admin_id`` GUC.

WHY STRICT (no NULL-permissive carveout):
    messages.tenant_id is NOT NULL after C5.0a's Phase 3 backfill.
    Every message row has a single legitimate tenant. No legacy or
    cross-tenant message rows exist. We therefore use the strict C3.2
    shape (no NULL exception) rather than the asymmetric C3.3/C4.3
    NULL-permissive shape.

This complements C5.2 (Wall 3 on messages.luciel_instance_id, which
DOES use the NULL-permissive shape). PostgreSQL AND's multiple
permissive policies on the same table for the same command, so the
two policies bind together: a row is visible only if it matches both
the tenant predicate AND the instance predicate.

The matching SessionRepository.add_message update (C5.3) populates
both columns from the parent session row, so application writes
respect both policies without service-layer changes.

Refs ARC9_RUNBOOK §C5.1.
"""

from __future__ import annotations

from alembic import op


revision = "arc9_c5_1_rls_messages"
down_revision = "arc9_c5_0b_messages_instance_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE messages ENABLE ROW LEVEL SECURITY;")
    op.execute(
        """
        CREATE POLICY messages_tenant_isolation
        ON messages
        AS PERMISSIVE
        FOR ALL
        TO PUBLIC
        USING (tenant_id = current_setting('app.admin_id', true))
        WITH CHECK (tenant_id = current_setting('app.admin_id', true));
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS messages_tenant_isolation ON messages;"
    )
    op.execute("ALTER TABLE messages DISABLE ROW LEVEL SECURITY;")
