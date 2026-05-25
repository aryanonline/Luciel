"""Arc 9 C3.6a -- RLS on retention_policies (asymmetric NULL-permissive).

Revision ID: arc9_c3_6a_rls_retention_policies
Revises: arc9_c3_5e_rls_admin_widget_domains
Create Date: 2026-05-24

WHAT
----
ENABLE ROW LEVEL SECURITY on retention_policies + CREATE POLICY with
asymmetric USING vs WITH CHECK to handle the NULL-permissive
platform-wide policy design.

    USING       (read):  tenant_id IS NULL
                         OR tenant_id = current_setting('app.admin_id', true)
    WITH CHECK  (write): (tenant_id IS NULL
                          AND current_setting('app.admin_id', true) = 'platform')
                         OR tenant_id = current_setting('app.admin_id', true)

WHY ASYMMETRIC
--------------
The retention_policies model docstring states: "Policies can be
platform-wide (tenant_id IS NULL) or tenant-specific." NULL = a
platform-default applied across all tenants (e.g. the default 30-day
session-data retention before any tenant overrides it). A strict
USING would hide platform defaults from every admin and break the
GDPR-policy resolution path. A permissive USING + strict WITH CHECK
gives:

  - Every admin can READ platform-wide defaults + their own overrides
  - Only the 'platform' sentinel role can WRITE the NULL defaults
  - Regular admins can only INSERT/UPDATE rows tagged with their own
    tenant_id

This is the same asymmetric pattern as C3.3 (knowledge_embeddings)
which has structurally identical semantics (NULL = shared cross-
tenant resource, non-NULL = per-tenant private).

DEPLOY COUPLING (unchanged from C3.1-C3.5)
Migration + rls_tenant_context_enabled=true ECS env MUST ship in the
same deploy bundle.

CHAIN
arc9_c3_5e_rls_admin_widget_domains -> arc9_c3_6a_rls_retention_policies

Reversibility: zero-data-impact downgrade (drop policy, disable RLS).
"""
from __future__ import annotations

from alembic import op


revision = "arc9_c3_6a_rls_retention_policies"
down_revision = "arc9_c3_5e_rls_admin_widget_domains"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE retention_policies ENABLE ROW LEVEL SECURITY;"
    )
    op.execute(
        """
        CREATE POLICY retention_policies_tenant_isolation
        ON retention_policies
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
        "DROP POLICY IF EXISTS retention_policies_tenant_isolation "
        "ON retention_policies;"
    )
    op.execute(
        "ALTER TABLE retention_policies DISABLE ROW LEVEL SECURITY;"
    )
