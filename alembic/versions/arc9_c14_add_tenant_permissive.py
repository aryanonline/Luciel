"""Arc 9 C14 -- add tenant-isolation PERMISSIVE policy alongside RESTRICTIVE.

Background (C14 reality-check, 2026-05-25, demo day):

  Arc 9 C11 flipped tenant-isolation policies from PERMISSIVE to
  RESTRICTIVE on 13 tables, on the assumption that each of those
  tables ALSO carried a Wall-3 (instance_isolation) PERMISSIVE
  policy that would satisfy PostgreSQL's "at least one PERMISSIVE
  policy must allow access" rule. That assumption held for 5 of the
  13 tables (admin_audit_logs, traces, memory_items, sessions,
  messages) but failed silently on the other 8 -- which never had a
  Wall-3 policy in the first place.

  Result: under FORCE ROW LEVEL SECURITY (Arc 9 C10) and the
  luciel_app role (NOBYPASSRLS), 8 tables became permanently
  uninsertable and unreadable regardless of GUC, because PostgreSQL
  semantics are:

      "If no PERMISSIVE policy exists, access is denied by default,
       regardless of RESTRICTIVE policy evaluation."

  Affected tables (caught during Phase A.5 Free signup demo when
  scope_assignments INSERT failed inside premint_for_tier):

      conversations, subscriptions, scope_assignments, user_invites,
      user_consents, identity_claims, instances, admin_widget_domains

  Diagnostic chain:
    * Phase A.5 step 1 attempt #3 returned 200 OK, but no welcome
      email arrived -- ECS logs showed signup_free.premint_failed
      with "new row violates row-level security policy for table
      'scope_assignments'".
    * Synthetic reproduction proved app.admin_id GUC was correctly
      set to the new tenant slug, same txid, same backend PID, same
      connection -- yet INSERT denied.
    * Raw policy expression evaluated to true in a manual probe; PG
      never reached it because no PERMISSIVE gate opened first.
    * Policy permissivity audit across all 13 C11 tables exposed
      the 8-table default-deny set.

The fix:
  ADD a PERMISSIVE policy named ``<table>_tenant_permissive`` with
  the SAME predicate as the existing RESTRICTIVE policy. This
  satisfies "at least one PERMISSIVE must allow" while the
  RESTRICTIVE policy continues to enforce the tenant fence (PG ANDs
  RESTRICTIVE policies into the final result). The combined effect
  is identical to the original C5 PERMISSIVE policy alone -- with
  the bonus that any future Wall-3 PERMISSIVE policies layered on
  these tables will be OR-combined with this Wall-1 PERMISSIVE,
  giving the architectural shape C11 was reaching for.

Security analysis:
  PG combination rule: row allowed IFF
    (any PERMISSIVE passes) AND (all RESTRICTIVE pass)

  Before C14 (broken):
    - 0 PERMISSIVE policies => never allowed.
  After C14:
    - 1 PERMISSIVE (predicate P) AND 1 RESTRICTIVE (predicate P)
    - Row allowed IFF P AND P  ==  P
    - Identical to pre-C11 single-PERMISSIVE behaviour.

  No security boundary expanded. The 8 tables return to the same
  tenant-fence posture they had before C11. The 5 tables that
  retained a Wall-3 PERMISSIVE are unaffected (this migration
  skips them).

Reversibility:
  downgrade() drops the added PERMISSIVE policies. This restores
  the broken default-deny state, which only makes sense if C11
  itself is also being reverted -- callers should run
  ``alembic downgrade arc9_c11_tenant_restrictive`` if they need
  the pre-C11 behaviour.

Refs:
  ARC9_RUNBOOK §C14 (Drive corrigendum, ARC9_C12_C13_C14_HOTFIX)
  arc9_c11_tenant_restrictive.py (the migration this corrects)
"""
from __future__ import annotations

from alembic import op


revision = "arc9_c14_add_tenant_permissive"
down_revision = "arc9_c11_tenant_restrictive"
branch_labels = None
depends_on = None


# The 8 tables that C11 left in default-deny -- they have a
# RESTRICTIVE tenant policy but no PERMISSIVE policy of any kind.
#
# Format: (table, fk_column) -- matches the C11 manifest. fk_column
# is what the policy compares to current_setting('app.admin_id').
DEFAULT_DENY_TABLES = (
    ("conversations", "tenant_id"),
    ("subscriptions", "tenant_id"),
    ("scope_assignments", "tenant_id"),
    ("user_invites", "tenant_id"),
    ("user_consents", "tenant_id"),
    ("identity_claims", "tenant_id"),
    ("instances", "admin_id"),
    ("admin_widget_domains", "admin_id"),
)


def upgrade() -> None:
    for table, fk in DEFAULT_DENY_TABLES:
        # Idempotent guard so re-runs against partial DBs don't blow up.
        op.execute(
            f"DROP POLICY IF EXISTS {table}_tenant_permissive ON {table};"
        )
        # Same predicate as the C11 RESTRICTIVE policy. PG will AND
        # the two policies together; since they share the predicate,
        # the combined effect is exactly that predicate.
        op.execute(
            f"""
            CREATE POLICY {table}_tenant_permissive
            ON {table}
            AS PERMISSIVE
            FOR ALL
            TO PUBLIC
            USING ({fk}::text = current_setting('app.admin_id', true))
            WITH CHECK ({fk}::text = current_setting('app.admin_id', true));
            """
        )


def downgrade() -> None:
    for table, _fk in DEFAULT_DENY_TABLES:
        op.execute(
            f"DROP POLICY IF EXISTS {table}_tenant_permissive ON {table};"
        )
