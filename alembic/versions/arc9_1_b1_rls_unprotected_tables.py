"""Arc 9.1 Phase B1 -- RLS on the 6 previously-unprotected tables (P1).

Six tables in prod had ZERO RLS coverage as of the 2026-05-25 prod-alignment
verification. Phase A sealed the seven isolation-bearing tables that DO have
luciel_instance_id; this Phase B1 seals the remaining six:

  Admin-scoped (Wall-1, via app.admin_id GUC):
    - metering_emissions       (per-Admin usage records)
    - instance_composition_grants (per-Admin grant ledger)
    - knowledge_share_grants   (per-Admin share ledger)
    - admin_tier_overrides     (per-Admin tier flags)

  Platform-only (no per-tenant scope, but luciel_app must NOT read):
    - email_send_event         (Postmark webhook receipts -- contain email
                                addresses from other tenants)
    - email_suppression        (bounce/complaint suppression list)

For the four Admin-scoped tables the policy is:
    USING (admin_id::text = current_setting('app.admin_id', true))

For the two platform-only tables the policy denies all access unless
the session has explicitly opted into the platform-admin GUC
``app.is_platform_admin``. The default ContextVar wiring in
``app/db/session.py`` does NOT set this GUC, so luciel_app sessions
get zero rows. The Postmark webhook handler will set the GUC via
``SET LOCAL app.is_platform_admin = 'true'`` for the duration of its
transaction (follow-up code change documented in the migration's
trailing comment block).

This migration is doctrine-compliant with Phase A:
  - No "IS NULL" disjunct anywhere.
  - FORCE row level security so even table owners get filtered.
  - Policies named with the ``arc9_1_b1_`` prefix so they're easy
    to grep / inventory.

Approach:
  1. ENABLE ROW LEVEL SECURITY + FORCE on all 6 tables
  2. CREATE POLICY per table
  3. luciel_app retains GRANTs; RLS is the gate

Recovery: ``downgrade()`` raises (Phase A doctrine).
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = "arc9_1_b1_rls_unprotected_tables"
down_revision = "arc9_1_a_tenant_isolation_seal"
branch_labels = None
depends_on = None


ADMIN_SCOPED = [
    "metering_emissions",
    "instance_composition_grants",
    "knowledge_share_grants",
    "admin_tier_overrides",
]

PLATFORM_ONLY = [
    "email_send_event",
    "email_suppression",
]


def upgrade() -> None:
    # --- Admin-scoped tables (Wall 1 enforcement) ---
    for tbl in ADMIN_SCOPED:
        op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY")
        # Drop any pre-existing policy with our name (idempotent).
        op.execute(f"DROP POLICY IF EXISTS arc9_1_b1_{tbl}_admin_isolation ON {tbl}")
        op.execute(
            f"""
            CREATE POLICY arc9_1_b1_{tbl}_admin_isolation ON {tbl}
                FOR ALL
                TO luciel_app
                USING (admin_id::text = current_setting('app.admin_id', true))
                WITH CHECK (admin_id::text = current_setting('app.admin_id', true))
            """
        )

    # --- Platform-only tables (deny-by-default to luciel_app) ---
    # These tables have no tenant column. The policy requires the
    # ``app.is_platform_admin`` GUC to be exactly the string 'true'.
    # Default ContextVar wiring does NOT set this GUC, so a plain
    # luciel_app session sees zero rows. The Postmark webhook handler
    # is the only legitimate caller and will SET LOCAL the GUC for
    # the duration of its transaction.
    for tbl in PLATFORM_ONLY:
        op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS arc9_1_b1_{tbl}_platform_only ON {tbl}")
        op.execute(
            f"""
            CREATE POLICY arc9_1_b1_{tbl}_platform_only ON {tbl}
                FOR ALL
                TO luciel_app
                USING (current_setting('app.is_platform_admin', true) = 'true')
                WITH CHECK (current_setting('app.is_platform_admin', true) = 'true')
            """
        )


def downgrade() -> None:
    raise RuntimeError(
        "arc9_1_b1_rls_unprotected_tables is non-reversible. "
        "Recovery path is RDS PITR to a moment before this migration ran."
    )
