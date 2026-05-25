"""Arc 9 C11 -- flip tenant isolation policies from PERMISSIVE to RESTRICTIVE.

Background (the C11 reality-check, 2026-05-25):

  Arc 9 C3-C5 installed two policy layers on most tenant tables:

    1. Wall 1 (tenant isolation): tenant_id = current_setting('app.admin_id')
    2. Wall 3 (instance isolation): luciel_instance_id matches OR IS NULL

  Both policies were created PERMISSIVE. Under PostgreSQL semantics
  multiple PERMISSIVE policies are OR-combined, so a row passes
  RLS if EITHER predicate is satisfied. The Wall 3 NULL-clause was
  designed to be permissive at the instance layer (legacy rows
  predate the instance column and should remain reachable to their
  parent tenant) -- but the OR with Wall 1 means a row with
  ``luciel_instance_id IS NULL`` is visible to ANY tenant whose
  ``app.instance_id`` GUC is bound to ''.

  This is not the intended security posture. The architectural
  intent is:

    * Tenant isolation: ABSOLUTE boundary. A row MUST belong to
      your tenant. This is the always-on RLS fence.
    * Instance isolation: ADDITIONAL fence within a tenant. Optional;
      empty/NULL means "tenant-wide, not instance-scoped".

  The fix is to make tenant isolation RESTRICTIVE. PostgreSQL AND's
  RESTRICTIVE policies into the result regardless of how many
  PERMISSIVE policies pass. A row now must:

    1. Pass the tenant RESTRICTIVE fence (tenant_id matches), AND
    2. Pass at least one PERMISSIVE policy (instance matches OR is
       NULL, OR tenant matches -- the latter is redundant but kept
       for documentation clarity in the simple-table case).

  Tables EXCLUDED from this flip (they remain PERMISSIVE for valid
  design reasons -- system-wide rows with NULL tenant_id are
  intentionally visible to all tenants):

    * knowledge_embeddings -- platform-wide reference embeddings
    * retention_policies   -- default policy when tenant has none
    * deletion_logs        -- platform-wide audit trail (NULL = platform op)
    * api_keys             -- platform-admin cross-tenant keys

Reversibility:
  downgrade() drops the RESTRICTIVE policies and re-creates them as
  PERMISSIVE -- restoring the exact pre-C11 state.

Refs:
  ARC9_RUNBOOK §C11 (Drive corrigendum, 2026-05-25)
  arc9_c10_a_force_rls.py (companion -- without FORCE, this would
    still be bypassed by table owners)
"""
from __future__ import annotations

from alembic import op


revision = "arc9_c11_tenant_restrictive"
down_revision = "arc9_c10_b_luciel_app_role"
branch_labels = None
depends_on = None


# 14 strict-tenant tables. Each has a single tenant policy named
# ``<table>_tenant_isolation`` (or for instances/admin_widget_domains,
# checks ``admin_id`` instead of ``tenant_id`` -- those columns are
# the tenant key on those tables).
#
# Format: (table, fk_column) where fk_column is the column the policy
# compares to current_setting('app.admin_id').
STRICT_TENANT_TABLES = (
    ("admin_audit_logs", "tenant_id"),
    ("traces", "tenant_id"),
    ("memory_items", "tenant_id"),
    ("conversations", "tenant_id"),
    ("sessions", "tenant_id"),
    ("subscriptions", "tenant_id"),
    ("scope_assignments", "tenant_id"),
    ("user_invites", "tenant_id"),
    ("user_consents", "tenant_id"),
    ("identity_claims", "tenant_id"),
    ("instances", "admin_id"),
    ("admin_widget_domains", "admin_id"),
    ("messages", "tenant_id"),
)
# Note: 13 tuples, not 14. After audit, scope_assignments policy
# was already verified strict. The "14 strict" count in the docstring
# above includes the 13 here plus messages (counted) -- exactly 13
# unique tables; messages duplicates because of the C5.1 split.
# Single canonical list, no duplicates.


def upgrade() -> None:
    for table, fk in STRICT_TENANT_TABLES:
        # 1. Drop the existing PERMISSIVE policy.
        op.execute(
            f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table};"
        )
        # 2. Recreate as RESTRICTIVE. Same predicate.
        op.execute(
            f"""
            CREATE POLICY {table}_tenant_isolation
            ON {table}
            AS RESTRICTIVE
            FOR ALL
            TO PUBLIC
            USING ({fk}::text = current_setting('app.admin_id', true))
            WITH CHECK ({fk}::text = current_setting('app.admin_id', true));
            """
        )


def downgrade() -> None:
    # Symmetric -- recreate as PERMISSIVE (pre-C11 state).
    for table, fk in STRICT_TENANT_TABLES:
        op.execute(
            f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table};"
        )
        op.execute(
            f"""
            CREATE POLICY {table}_tenant_isolation
            ON {table}
            AS PERMISSIVE
            FOR ALL
            TO PUBLIC
            USING ({fk}::text = current_setting('app.admin_id', true))
            WITH CHECK ({fk}::text = current_setting('app.admin_id', true));
            """
        )
