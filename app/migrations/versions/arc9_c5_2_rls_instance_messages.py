"""Arc 9 C5.2 -- Wall 3 RLS policy on messages (luciel_instance_id).

Adds the second of two RLS policies on the messages table, completing
the three-wall posture (L1 service-layer + L2 RLS x2 + L3 GUC). C5.1
delivered the Wall-1 (tenant_id strict) policy; this migration adds
the Wall-3 (instance_id NULL-permissive) policy alongside it.

Both policies are PERMISSIVE -- PostgreSQL ANDs multiple PERMISSIVE
policies on the same table for the same command. A row is visible if
AND ONLY IF:
    * Its tenant_id matches the bound admin_id (C5.1 USING), AND
    * Its luciel_instance_id matches the bound instance_id OR is NULL
      (this migration's USING).

Same shape as C4.3 sibling migrations (api_keys, knowledge_embeddings,
memory_items, sessions, traces, admin_audit_logs). See C4.3d for the
full design rationale; this file deliberately mirrors that shape so
the 7 Wall-3 NULL-permissive migrations form a coherent series.

DOWNGRADE BEHAVIOUR (important difference from C4.3 siblings):

C4.3 siblings all execute ``ALTER TABLE ... DISABLE ROW LEVEL SECURITY``
in downgrade because those tables had no pre-existing C3 policy.

For messages, C5.1 already enabled RLS and installed the tenant policy.
If THIS migration disabled RLS on downgrade, the C5.1 policy would
become inert (RLS-disabled tables ignore all policies). We therefore
ONLY drop our own policy in downgrade -- leaving RLS enabled and the
C5.1 policy active. This is the same pattern as a future C3 migration
adding a second policy to an already-RLS-enabled table.

Refs ARC9_RUNBOOK §C5.2, C4 NULL-permissive doctrine.
"""

from __future__ import annotations

from alembic import op


revision = "arc9_c5_2_rls_instance_messages"
down_revision = "arc9_c5_1_rls_messages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # RLS is already enabled on messages by C5.1. The ENABLE here is
    # idempotent at the table level (PG no-ops a re-enable) and keeps
    # the migration self-contained against any out-of-order replay.
    op.execute("ALTER TABLE messages ENABLE ROW LEVEL SECURITY;")
    op.execute(
        """
        CREATE POLICY messages_instance_isolation
        ON messages
        AS PERMISSIVE
        FOR ALL
        TO PUBLIC
        USING (
            luciel_instance_id::text = current_setting('app.instance_id', true)
            OR luciel_instance_id IS NULL
        )
        WITH CHECK (
            luciel_instance_id::text = current_setting('app.instance_id', true)
            OR (
                luciel_instance_id IS NULL
                AND current_setting('app.instance_id', true) = ''
            )
        );
        """
    )


def downgrade() -> None:
    # CRITICAL: do NOT disable RLS here. C5.1 enabled it and installed
    # a sibling policy that must remain in force. Disabling RLS would
    # silently neuter the Wall-1 policy too.
    op.execute(
        "DROP POLICY IF EXISTS messages_instance_isolation ON messages;"
    )
