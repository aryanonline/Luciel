"""Arc 9 C6.2 -- admin_audit_logs forward-only immutability policy.

Layers two RESTRICTIVE row-level security policies on admin_audit_logs
that make UPDATE and DELETE structurally impossible for every role
except ``luciel_ops`` (the BYPASSRLS role from C6.1).

Defense-in-depth model
----------------------

The audit chain has THREE layers of write-side protection:

  L-grant   GRANT discipline. The C6.1 migration grants:
              - luciel       -> INSERT/SELECT (regular app role)
              - luciel_worker-> INSERT/SELECT (worker role, Step 28)
              - luciel_ops   -> SELECT only (C6.1)
            No role has been granted UPDATE or DELETE. If somebody
            ever runs ``GRANT UPDATE ON admin_audit_logs TO luciel``
            in a panic, this layer falls. We need defence below it.

  L-policy  THIS COMMIT. RESTRICTIVE policies on admin_audit_logs
            FOR UPDATE and FOR DELETE with USING (false) for every
            role EXCEPT luciel_ops. Restrictive policies are AND'd
            with the existing permissive C3.1 + C4.3f policies, so
            this strictly NARROWS what's allowed -- never broadens.
            Even if grants leak, the policy refuses the operation.

  L-chain   SQLAlchemy session event handler in
            app/repositories/audit_chain.py computes row_hash from
            prev_row_hash + payload at INSERT time. An UPDATE that
            slipped past L-grant AND L-policy would break the hash
            chain, and the C7 integrity check would scream within
            minutes. This is the alarm layer, not the prevention
            layer, but it makes silent corruption impossible.

Why RESTRICTIVE not PERMISSIVE
-------------------------------

Postgres combines policies as:
  ROW VISIBLE = (OR of all permissive policies)
                AND (AND of all restrictive policies)

The C3.1 policy ``admin_audit_logs_tenant_isolation`` and the
C4.3f policy ``admin_audit_logs_instance_isolation`` are both
PERMISSIVE -- the row is visible if EITHER tenant_id matches the
GUC OR instance_id matches. Adding a third PERMISSIVE policy with
``current_user = 'luciel_ops'`` would BROADEN access -- exactly
the opposite of what we want.

RESTRICTIVE policies AND with the permissive result. So even if a
row passes both PERMISSIVE policies (admin's own row), the
RESTRICTIVE policy on UPDATE refuses the operation unless the
caller is luciel_ops. That's the shape we need.

Why two policies (one per command) not one FOR ALL
----------------------------------------------------

We deliberately do NOT touch INSERT or SELECT. INSERT is the
write-path that builds the chain (must remain open for luciel +
luciel_worker). SELECT is the read-path for forensics (open for
all three roles via the existing PERMISSIVE policies).

A single RESTRICTIVE FOR ALL policy with USING (current_user = ...)
would silently break SELECT for non-ops callers because USING
gates the read side too. We only want to gate UPDATE and DELETE.
Hence two separate policies.

Why current_user not session_user
-----------------------------------

session_user is the role you connected as; current_user is the
role you have SET ROLE'd to. We use current_user so that future
ops jobs that connect as luciel and then SET ROLE luciel_ops
(if we ever do that) still pass the check. Today luciel_ops
connects directly via its own credential, so the two are equal,
but current_user is the more robust choice -- it tracks the
effective identity, not the connection identity.

Flag-gating: NOT in the migration
----------------------------------

The migration ALWAYS installs the policy. The Settings flag
``audit_log_immutability_enabled`` (C6.3) controls whether the
APP CODE expects and enforces it -- specifically:

  * C7 CloudWatch alarm "audit_chain_integrity_failure" fires only
    when the flag is True (otherwise we'd alarm during the
    transition window before any role separation lands).
  * Future operator runbooks that depend on "UPDATE admin_audit_logs
    will always fail" assertions check the flag first.

Putting the flag inside the migration would create a chicken-and-
egg: the app reads the flag from the DB, but the migration runs
before the app is up. Keep policy installation deterministic;
let app code adapt to the policy's presence via the flag.

Rollout safety
--------------

In production at the moment of this migration's deploy:
  * No application code runs UPDATE or DELETE on admin_audit_logs
    today. Verified 2026-05-24 via grep across app/ for any
    admin_audit_log update/delete call site -- zero hits in
    non-test code.
  * luciel_ops role exists (from C6.1) but is not yet wired into
    any code path (C6.3 wires get_ops_db_session()).
  * Therefore: installing these RESTRICTIVE policies is a no-op
    for all current traffic. Future code paths that attempt
    UPDATE/DELETE will fail loudly, which is exactly what we want.

Downgrade
---------

Drops both policies. Existing PERMISSIVE policies (C3.1, C4.3f)
remain untouched. Audit rows themselves untouched. Zero data
impact.

Refs:
  ARC9_RUNBOOK §C6 (Drive, canonical)
  alembic/versions/arc9_c6_1_luciel_ops_role.py (the role)
  alembic/versions/arc9_c3_1_rls_admin_audit_logs.py (Wall-1 base)
  alembic/versions/arc9_c4_3f_rls_instance_admin_audit_logs.py (Wall-3)
  app/repositories/audit_chain.py (L-chain layer)
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = "arc9_c6_2_admin_audit_immutability"
down_revision = "arc9_c6_1_luciel_ops_role"
branch_labels = None
depends_on = None


# Policy names are kept stable across upgrade/downgrade so operator
# inspection (``\d+ admin_audit_logs`` in psql) shows a predictable
# label.
UPDATE_POLICY_NAME = "admin_audit_logs_no_update"
DELETE_POLICY_NAME = "admin_audit_logs_no_delete"


def upgrade() -> None:
    # RESTRICTIVE FOR UPDATE: refuse all UPDATEs except from luciel_ops.
    #
    # Today, luciel_ops has SELECT-only grant on admin_audit_logs
    # (C6.1), so even with this policy passing for current_user =
    # 'luciel_ops', any UPDATE attempt from that role would fail at
    # the grant layer with "permission denied for table". This is
    # the intentional defence-in-depth shape: grant + policy must
    # BOTH be permissive for the operation to succeed, so we have
    # to break two locks to mutate an audit row.
    #
    # We still allow current_user = 'luciel_ops' in the policy
    # itself because if a future migration ever grants UPDATE to
    # luciel_ops for a one-off forensic correction (under explicit
    # human approval), the policy should not be the blocker --
    # the grant change should be. Policies are dataset-shape rules;
    # grants are operator-decision rules. Keep them aligned, not
    # duplicated.
    op.execute(
        f"""
        CREATE POLICY {UPDATE_POLICY_NAME}
        ON admin_audit_logs
        AS RESTRICTIVE
        FOR UPDATE
        TO PUBLIC
        USING (current_user = 'luciel_ops')
        WITH CHECK (current_user = 'luciel_ops');
        """
    )

    # RESTRICTIVE FOR DELETE: refuse all DELETEs except from luciel_ops.
    #
    # Same shape as the UPDATE policy. DELETE has no WITH CHECK
    # (Postgres only supports USING on DELETE), so this is USING-only.
    op.execute(
        f"""
        CREATE POLICY {DELETE_POLICY_NAME}
        ON admin_audit_logs
        AS RESTRICTIVE
        FOR DELETE
        TO PUBLIC
        USING (current_user = 'luciel_ops');
        """
    )


def downgrade() -> None:
    # Drop in reverse order. Postgres doesn't care about order
    # for policy drops, but symmetric ordering matches the upgrade
    # which makes diffing the alembic history easier.
    op.execute(
        f"DROP POLICY IF EXISTS {DELETE_POLICY_NAME} ON admin_audit_logs;"
    )
    op.execute(
        f"DROP POLICY IF EXISTS {UPDATE_POLICY_NAME} ON admin_audit_logs;"
    )
