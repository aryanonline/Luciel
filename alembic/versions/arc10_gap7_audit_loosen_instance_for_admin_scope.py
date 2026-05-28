"""Arc 10 Gap 7: loosen admin_audit_logs.luciel_instance_id to NULLABLE
so admin-scoped cascade audit rows can be emitted.

Revision ID: arc10_gap7_audit_loosen_instance_for_admin_scope
Revises: arc10_gap6_archiver_sequence_grant
Create Date: 2026-05-27

Anchored to:
  - Architecture v1 \u00a73.7.3 (Wall 3): the non-null instance_id rule
    applies to **customer-data rows**. The doc enumerates the
    customer-data tables that get the \u00a73.7.5 RLS pattern
    (conversations, messages, sessions, memory_items,
    knowledge_embeddings, traces, api_keys, ...). admin_audit_logs
    is **NOT** a customer-data table -- \u00a75.3 calls it out separately
    as "the admin audit log", append-only at the app layer, with a
    distinct DB role separation. Wall 3 does not apply to it.

  - Architecture v1 \u00a73.6.2 (Account Closure Flow): closure emits
    admin-scoped audit rows -- "All team members invalidated" and
    "All embed keys revoked" -- which by their nature span every
    instance under the admin and cannot pick a single instance_id.

  - Architecture v1 \u00a75.3 (Audit Chain Immutability): the audit chain
    is content-addressed; admin-scoped rows are first-class audit
    content. Forcing them to invent a fake instance_id would
    corrupt the forensic shape.

  Source of the over-constraint
  =============================
  The Arc 9.1 Phase A "tenant isolation seal" migration bulk-applied
  NOT NULL on `luciel_instance_id` to every table it considered
  instance-scoped, sweeping `admin_audit_logs` into the same bucket
  as the customer-data tables. That bulk application was stricter
  than Vision/Architecture required. This migration walks it back
  for admin_audit_logs specifically -- nothing else.

  Live evidence of the over-constraint
  ====================================
  ClosureService.initiate_closure -> AdminService.deactivate_tenant_
  with_cascade emits one cascade_deactivate audit row per layer
  recording "I deactivated N rows for admin X across all instances".
  The current schema rejects every such row with NotNullViolation
  on luciel_instance_id, which means the entire /account/close
  flow has been broken since Arc 9.1 -- not just the test E2E.
  Customer Journey v1 \u00a78 (Marcus closes their team's account)
  cannot complete on the current schema.

  RLS policy update
  =================
  STEP 3 of the Arc 9.1 seal rewrote admin_audit_logs's RLS policy
  to drop the IS NULL disjunct (since the column was about to
  become NOT NULL). We restore that disjunct here for admin-scoped
  rows: when luciel_instance_id IS NULL, the row is admin-scoped
  and the policy passes (no instance binding to check). When
  non-NULL, the strict equality check still applies.

  Idempotency: ALTER COLUMN ... DROP NOT NULL is a no-op if the
  column is already nullable. DROP POLICY IF EXISTS is idempotent.

  Downgrade: ALTER COLUMN ... SET NOT NULL would fail if any rows
  with NULL luciel_instance_id exist (because the cascade was
  unblocked by this upgrade and immediately started writing them).
  Downgrade is provided for migration symmetry only; in practice
  forward-only.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "arc10_gap7_audit_loosen_instance_for_admin_scope"
down_revision = "arc10_gap6_archiver_sequence_grant"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Loosen the column.
    op.alter_column(
        "admin_audit_logs",
        "luciel_instance_id",
        existing_type=sa.Integer(),
        nullable=True,
    )

    # 2. Restore IS NULL disjunct on the RLS policy so admin-scoped
    #    audit rows (NULL luciel_instance_id) pass policy.
    op.execute(sa.text(
        "DROP POLICY IF EXISTS admin_audit_logs_instance_isolation "
        "ON admin_audit_logs"
    ))
    op.execute(sa.text("""
        CREATE POLICY admin_audit_logs_instance_isolation
        ON admin_audit_logs
        FOR ALL
        USING (
            luciel_instance_id IS NULL
            OR luciel_instance_id::text
               = current_setting('app.instance_id', true)
        )
        WITH CHECK (
            luciel_instance_id IS NULL
            OR luciel_instance_id::text
               = current_setting('app.instance_id', true)
        )
    """))


def downgrade() -> None:
    # Restore strict NOT NULL + tight policy. Will fail if any rows
    # were written with NULL luciel_instance_id between upgrade and
    # downgrade -- see module docstring on forward-only-in-practice.
    op.execute(sa.text(
        "DROP POLICY IF EXISTS admin_audit_logs_instance_isolation "
        "ON admin_audit_logs"
    ))
    op.execute(sa.text("""
        CREATE POLICY admin_audit_logs_instance_isolation
        ON admin_audit_logs
        FOR ALL
        USING (
            luciel_instance_id::text
            = current_setting('app.instance_id', true)
        )
        WITH CHECK (
            luciel_instance_id::text
            = current_setting('app.instance_id', true)
        )
    """))
    op.alter_column(
        "admin_audit_logs",
        "luciel_instance_id",
        existing_type=sa.Integer(),
        nullable=False,
    )
