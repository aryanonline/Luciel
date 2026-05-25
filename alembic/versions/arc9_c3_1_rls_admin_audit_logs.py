"""Arc 9 C3.1 -- RLS policy on admin_audit_logs (Wall 1 Layer 2).

First per-table RLS rollout in the Arc 9 C3 sequence. admin_audit_logs
is chosen as the lead because:

  * It is the LOWEST-BLAST-RADIUS customer-data table. A misbehaving
    RLS policy here cannot block foreground user actions -- the table
    is append-only from the application (writes happen via the
    AdminAuditRepository's chained-hash insert; reads are admin-only
    via /admin/forensics endpoints).

  * It already has the strongest tenant-isolation discipline: every
    write explicitly sets tenant_id (never inferred), and the
    'platform' literal sentinel cleanly distinguishes system actions
    from per-admin actions. RLS policy can split on this cleanly.

  * It is the structural foundation of Arc 9's audit-immutability
    work (C6). Locking down read-paths first lets C6 layer the
    write-path role separation on top of a known-good baseline.

Three-layer Wall 1 design (Arc 9 C2 + this commit):

  L1  Service-layer ``WHERE tenant_id = :admin_id`` (already in place
      across AdminAuditRepository -- Arc 5 Revision C).
  L2  RLS policies (THIS COMMIT, for admin_audit_logs).
  L3  In-app connection-pool wrapper sets ``app.admin_id`` GUC per
      request (Arc 9 C2 -- already merged, feature-flagged off until
      this migration lands).

Behaviour after deploy (with rls_tenant_context_enabled=True):

  * Authenticated admin request: ``app.admin_id`` is set to that
    admin's slug. Policy matches rows WHERE tenant_id = current_setting.
    Cross-admin SELECT/UPDATE/DELETE returns/affects zero rows.

  * Platform/internal action (no admin context, GUC = ''): policy
    DENIES (tenant_id can never equal empty string -- it's NOT NULL).
    This is the structurally correct fail-closed posture for
    customer-data reads from background jobs that haven't explicitly
    bound an admin context.

  * Platform-tier read (the 'platform' literal sentinel rows): the
    GUC must be explicitly set to 'platform' to read them. We do
    NOT create a permissive policy for the platform sentinel because
    that would let a forgotten SET LOCAL silently expose platform-
    level audit rows to ordinary admins. Operator forensics jobs MUST
    explicitly set the admin context to 'platform' to access these.

Why USING + WITH CHECK both:

  * USING gates SELECT / UPDATE / DELETE (read-side enforcement)
  * WITH CHECK gates INSERT / UPDATE (write-side enforcement)
  Without WITH CHECK, an admin authenticated as 'acme-corp' could
  INSERT a row with tenant_id='globex' -- a write-side leak that's
  symmetric in severity to the read-side one. We close both.

Why ENABLE (not FORCE) at this commit:

  * ``ENABLE ROW LEVEL SECURITY`` makes the policy apply to ordinary
    roles (the luciel app role).
  * ``FORCE ROW LEVEL SECURITY`` makes it ALSO apply to the table
    owner (which is also the luciel role in our setup, but is
    bypassed in some migration contexts).
  We start with ENABLE so Alembic migrations (which run as the table
  owner) can continue to operate without an explicit GUC SET. C9
  flips to FORCE in the envelope-close commit as the final hardening
  step, once all per-table policies are in place and proven.

The bypass role for ops/migration:

  Postgres' superuser BYPASSRLS attribute lets a designated role
  (typically the migration role) ignore RLS entirely. We do NOT
  configure that here -- the luciel app role IS NOT a superuser
  in prod, and migration scripts run as the SAME role as app traffic.
  Instead, migrations get correct behaviour by virtue of running
  outside the FastAPI dep chain (no GUC set) -- the after_begin
  listener writes empty string, the policy denies SELECT/UPDATE
  on customer rows, and that's CORRECT because migrations should
  never read or update specific admins' rows by tenant_id.

  The Alembic ``op.execute()`` calls in THIS migration touch table
  schema only (no row reads/writes), so they are unaffected.

Reversibility:

  downgrade() drops policy + disables RLS. Audit log rows are
  unchanged. Service-layer L1 filtering remains in place. This is a
  zero-data-impact rollback.

Cross-cutting checks performed before authoring this migration:

  - app/models/admin_audit_log.py:618 -- tenant_id String(100), NOT NULL
  - app/repositories/admin_audit_repository.py -- every write passes
    tenant_id from AuditContext or 'platform' literal
  - Arc 9 C2 (PR #54 + #55) -- ContextVar plumbing lands, feature-flag
    off in prod, ready to flip in lockstep with this deploy

Refs ARC9_RUNBOOK §C3 (Drive, canonical).
"""

from __future__ import annotations

from alembic import op


revision = "arc9_c3_1_rls_admin_audit_logs"
down_revision = "arc7_b_admins_last_signup_ip"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Step 1: Enable RLS on the table. Until a policy is created,
    # ENABLE WITHOUT a policy means default-deny for ordinary roles
    # (this is Postgres' behaviour: RLS-enabled-no-policy = deny all).
    # We create the policy in the SAME transaction so there's no
    # window of total denial in production.
    op.execute("ALTER TABLE admin_audit_logs ENABLE ROW LEVEL SECURITY;")

    # Step 2: Create the per-admin scoping policy.
    #
    # USING clause: gates which rows are VISIBLE to SELECT/UPDATE/DELETE.
    # WITH CHECK clause: gates which rows are ALLOWED to be inserted/updated.
    #
    # The predicate is intentionally simple text equality. tenant_id
    # is String(100); current_setting returns text. No casts needed.
    # The 'true' second arg to current_setting means "return empty
    # string instead of raising if the GUC is unset" -- crucial for
    # background paths that haven't set tenant context.
    #
    # We name the policy with the table prefix so multiple policies
    # on the same table (if ever needed for platform-tier carveouts)
    # can coexist by name.
    op.execute(
        """
        CREATE POLICY admin_audit_logs_tenant_isolation
        ON admin_audit_logs
        AS PERMISSIVE
        FOR ALL
        TO PUBLIC
        USING (tenant_id = current_setting('app.admin_id', true))
        WITH CHECK (tenant_id = current_setting('app.admin_id', true));
        """
    )


def downgrade() -> None:
    # Order matters: drop policy first, then disable RLS. Disabling RLS
    # while a policy still exists is allowed by Postgres but leaves the
    # policy as a no-op artifact; cleaner to remove it explicitly.
    op.execute(
        "DROP POLICY IF EXISTS admin_audit_logs_tenant_isolation "
        "ON admin_audit_logs;"
    )
    op.execute("ALTER TABLE admin_audit_logs DISABLE ROW LEVEL SECURITY;")
