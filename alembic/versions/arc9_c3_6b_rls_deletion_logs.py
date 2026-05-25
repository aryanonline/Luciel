"""Arc 9 C3.6b -- RLS on deletion_logs (asymmetric NULL-permissive).

Revision ID: arc9_c3_6b_rls_deletion_logs
Revises: arc9_c3_6a_rls_retention_policies
Create Date: 2026-05-24

WHAT
----
ENABLE ROW LEVEL SECURITY on deletion_logs + CREATE POLICY with the
same asymmetric NULL-permissive shape as retention_policies (C3.6a)
and knowledge_embeddings (C3.3):

    USING       (read):  tenant_id IS NULL
                         OR tenant_id = current_setting('app.admin_id', true)
    WITH CHECK  (write): (tenant_id IS NULL
                          AND current_setting('app.admin_id', true) = 'platform')
                         OR tenant_id = current_setting('app.admin_id', true)

WHY ASYMMETRIC
--------------
deletion_logs is the audit trail of *what got deleted under which
retention policy*. NULL tenant_id rows are platform-issued cross-
tenant deletions (e.g. operator-initiated GDPR-bulk-erase across the
fleet, scheduler sweeps before a tenant exists). A strict USING
would hide platform-issued deletion records from compliance audits
on a per-tenant view. The asymmetric policy preserves auditability:

  - Every admin can READ platform-issued deletions that affected
    their tenant context + deletions tagged with their own tenant_id
  - Only the 'platform' sentinel role can WRITE NULL rows (i.e.
    issue a cross-tenant deletion that is NOT attributable to any
    one admin's request)
  - Regular admins can only INSERT/UPDATE rows tagged with their own
    tenant_id (their own GDPR-erase requests)

CLASSIFICATION NUANCE
---------------------
The C1 audit classifies deletion_logs as 'platform' rather than
'customer-data' because the table records platform-machinery
events (the scheduler running). Applying RLS here is therefore
*defense-in-depth* rather than primary tenancy enforcement: the
service layer already filters by tenant_id in the deletion-log
viewer (app/api/v1/retention.py), and the platform-classification
means there is no GDPR right-to-erasure obligation on the row
itself (it IS the erasure record). RLS is the database-level
backstop against a service-layer bug.

DEPLOY COUPLING (unchanged from C3.1-C3.5)
Migration + rls_tenant_context_enabled=true ECS env MUST ship in the
same deploy bundle.

CHAIN
arc9_c3_6a_rls_retention_policies -> arc9_c3_6b_rls_deletion_logs

This is the final commit in the C3 per-table RLS series. After
deploy: 16 of the 18 Wall-1 customer-data tables are RLS-protected
(messages deferred to C8 schema delta).

Reversibility: zero-data-impact downgrade (drop policy, disable RLS).
"""
from __future__ import annotations

from alembic import op


revision = "arc9_c3_6b_rls_deletion_logs"
down_revision = "arc9_c3_6a_rls_retention_policies"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE deletion_logs ENABLE ROW LEVEL SECURITY;")
    op.execute(
        """
        CREATE POLICY deletion_logs_tenant_isolation
        ON deletion_logs
        AS PERMISSIVE
        FOR ALL
        TO PUBLIC
        USING (
            tenant_id IS NULL
            OR tenant_id = current_setting('app.admin_id', true)
        )
        WITH CHECK (
            (tenant_id IS NULL
             AND current_setting('app.admin_id', true) = 'platform')
            OR tenant_id = current_setting('app.admin_id', true)
        );
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS deletion_logs_tenant_isolation "
        "ON deletion_logs;"
    )
    op.execute("ALTER TABLE deletion_logs DISABLE ROW LEVEL SECURITY;")
