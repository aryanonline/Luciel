"""Arc 9 C6.1 -- create luciel_ops Postgres role (BYPASSRLS).

The retention-purge worker and (future) deletion-sweep jobs need to
delete rows across ALL tenants and ALL instances of a tenant. With
the C3/C4/C5 RLS policies in place, the default app role (`luciel`)
and the worker role (`luciel_worker`) are correctly fenced -- they
can only see/affect rows matching the bound ``app.admin_id`` /
``app.instance_id`` GUCs. That fencing is the wrong shape for the
retention purge:

  * The nightly retention scan crosses ALL tenants (sequence of
    per-tenant purges, each binding scope to that tenant's id).
  * Within one tenant's purge, ``app.instance_id`` is bound to '',
    which under Wall-3's NULL-permissive USING clause admits ONLY
    rows where ``luciel_instance_id IS NULL``. Instance-scoped rows
    (memory_items, sessions, traces, messages) with a non-NULL
    luciel_instance_id would SURVIVE the purge -- documented as the
    "C6 BYPASSRLS gap" in app/worker/tasks/retention.py:177-191.

The clean fix is a dedicated Postgres role with the BYPASSRLS
attribute, so RLS policies are not consulted at all for this role's
queries. RLS still gates every other code path.

Doctrine choice (Aryan approved 2026-05-24):

  * GLOBAL `luciel_ops` role -- one role for all "operations that
    legitimately need to cross RLS boundaries" (retention,
    future deletion-sweep, future GDPR-right-to-erasure workflow).
    Rejected alternative: per-concern roles
    (luciel_audit_retention / luciel_deletion_sweep). Reasons:
      - Fewer SSM secrets to rotate
      - Single audit trail when grep'ing pg_stat_activity by usename
      - All ops jobs share the same fail-closed posture; per-concern
        roles would only differ in their grant lists, which is a
        weak isolation boundary because Postgres role grants are
        not transactional.

  * FORWARD-ONLY audit-log immutability -- this migration does NOT
    grant UPDATE or DELETE on admin_audit_logs to luciel_ops.
    Even the ops role cannot mutate or delete audit rows. The
    audit chain stays append-only forever; any "forgotten old
    rows" left after a tenant purge is a known-and-accepted
    trade-off. PIPEDA principle 5 (retention limits) does not
    apply to AdminAuditLog rows -- those are the *legal record*
    that purges happened, governed by audit-retention rules
    (CANONICAL_RECAP §14 future-debt), not customer-data rules.

  * RLS bypass is the ONLY privilege escalation luciel_ops has over
    luciel. Specifically NOT granted:
      - NOCREATEDB, NOCREATEROLE, NOSUPERUSER, NOREPLICATION
      - NOINHERIT (must explicitly SET ROLE; cannot ride on a
        membership grant)
      - No grants outside the public schema
      - No grants on auth-perimeter tables (admins, tenant_configs,
        users, user_invites, user_consents) -- the ops role cannot
        delete a tenant's identity, only its data.

Operational notes:

  * Password is NOT set in this migration. The deploy script runs
    ``python -m scripts.mint_ops_db_password_ssm --ssm`` (to be added
    in C6.3 alongside the settings wiring) which rotates a random
    32-char password and writes it to
    ``/luciel/<env>/luciel_ops/password`` for the worker task-def to
    pick up via the existing SSM injection pattern.
  * Application code MUST NOT pick up this role automatically. The
    C6.3 ``get_ops_db_session()`` helper is the single entry point;
    it requires the caller to import explicitly. Default
    ``SessionLocal`` continues to use the regular ``luciel`` role.

Tables the ops role can DELETE (matches the 12-step DELETE chain in
``admin_service.hard_delete_tenant_after_retention``, verified
2026-05-24 against admin_service.py:1365-1432):

  sessions, conversations, identity_claims, memory_items, api_keys,
  luciel_instances, agents, agent_configs

Tables explicitly EXCLUDED from DELETE grant:

  admins / tenant_configs -- the parent identity row. Retention's
    step 11 deletes this via the regular ``luciel`` role, BEFORE
    the ops-role purge is needed. Removing this from the ops grant
    list narrows blast radius: a compromised ops credential cannot
    nuke an active tenant's identity row.
  admin_audit_logs -- forward-only immutability (see doctrine).
    Granted SELECT only for forensic reads. The C6.2 migration
    will add a RESTRICTIVE policy that *also* blocks UPDATE/DELETE
    at the row level, so even if a future grant ALTER mistakenly
    adds UPDATE here, the policy continues to refuse.
  messages -- cascades from sessions via ON DELETE CASCADE (per
    Step 30a.2 retention design); no direct DELETE needed.
  users -- identity-tier table. PIPEDA right-to-erasure runs
    through a separate workflow (future); retention does not
    touch users directly.
  subscriptions, email_send_events, email_suppressions --
    tax/accounting retention (separate clock; CANONICAL_RECAP §14).

Reversibility:

  downgrade() revokes grants and drops the role. If the role still
  owns objects (which it should not -- it is a login role with no
  object ownership), Postgres will refuse the DROP with a clear
  message naming the holdouts.

Refs:
  ARC9_RUNBOOK §C6 (Drive, canonical)
  app/worker/tasks/retention.py:177-191 (the gap this closes)
  alembic/versions/f392a842f885_step28_create_luciel_worker_role.py
    (template; see Pillar 23 GRANT discipline -- worker has NO
    UPDATE on admin_audit_logs, ops likewise has NO UPDATE here)
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = "arc9_c6_1_luciel_ops_role"
down_revision = "arc9_c5_2_rls_instance_messages"
branch_labels = None
depends_on = None


# Tables the ops role can SELECT but never write.
#
# admin_audit_logs is the audit chain. Forensic reads only -- writes
# stay through AdminAuditRepository.record() running under the regular
# luciel role with the chained-hash session event handler attached.
SELECT_ONLY_TABLES = (
    "admin_audit_logs",
)


# Tables the ops role can SELECT + DELETE for retention/deletion sweeps.
# Order matches the 12-step DELETE chain in
# admin_service.hard_delete_tenant_after_retention -- not because
# Postgres cares (it doesn't), but so reviewers can cross-check
# against the service-layer source of truth.
#
# Verified 2026-05-24 against:
#   app/services/admin_service.py:1365-1402
SELECT_DELETE_TABLES = (
    "sessions",
    "conversations",
    "identity_claims",
    "memory_items",
    "api_keys",
    "luciel_instances",
    "agents",
    "agent_configs",
)


def upgrade() -> None:
    # 1. Create the role idempotently with BYPASSRLS.
    #
    # BYPASSRLS is the whole point of this role -- it lets queries
    # cross the per-tenant RLS fence that C3/C4/C5 installed. Every
    # other attribute is the same fail-closed posture as luciel_worker:
    # NOINHERIT, NOCREATEDB, NOCREATEROLE, NOSUPERUSER, NOREPLICATION.
    #
    # LOGIN is required because the ops connections come in over the
    # network from the worker container; SET ROLE from luciel_worker
    # would be cleaner but requires GRANT luciel_ops TO luciel_worker
    # which then conflates blast radii. We keep them as separate
    # login roles with separate SSM passwords.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_roles WHERE rolname = 'luciel_ops'
            ) THEN
                CREATE ROLE luciel_ops WITH
                    LOGIN
                    BYPASSRLS
                    NOINHERIT
                    NOCREATEDB
                    NOCREATEROLE
                    NOSUPERUSER
                    NOREPLICATION;
            END IF;
        END
        $$;
        """
    )

    # 2. Schema usage. Without USAGE on public, even granted table
    #    privileges return permission denied.
    op.execute("GRANT USAGE ON SCHEMA public TO luciel_ops;")

    # 3. SELECT-only grants on audit/read-surface tables.
    for table in SELECT_ONLY_TABLES:
        op.execute(f"GRANT SELECT ON {table} TO luciel_ops;")

    # 4. SELECT + DELETE grants on retention-purge surface tables.
    #    Deliberately NO INSERT, NO UPDATE -- the ops role removes
    #    data, never creates or mutates it. Combined with BYPASSRLS,
    #    this is the minimum surface area that retention needs.
    for table in SELECT_DELETE_TABLES:
        op.execute(
            f"GRANT SELECT, DELETE ON {table} TO luciel_ops;"
        )

    # 5. No sequence grants. The ops role does not INSERT, so it
    #    never calls nextval(). Explicit absence of sequence USAGE
    #    is the database-enforced version of "ops cannot fabricate
    #    new rows". If a future change accidentally grants INSERT,
    #    the missing sequence USAGE will surface as a clear
    #    "permission denied for sequence" error rather than a
    #    silent data injection.


def downgrade() -> None:
    # Symmetric teardown. REVOKE before DROP -- Postgres refuses to
    # DROP a role that still holds privileges on objects.

    for table in SELECT_DELETE_TABLES + SELECT_ONLY_TABLES:
        op.execute(f"REVOKE ALL ON {table} FROM luciel_ops;")

    op.execute("REVOKE ALL ON SCHEMA public FROM luciel_ops;")

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_roles WHERE rolname = 'luciel_ops'
            ) THEN
                DROP ROLE luciel_ops;
            END IF;
        END
        $$;
        """
    )
