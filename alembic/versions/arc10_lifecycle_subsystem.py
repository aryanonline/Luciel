"""Arc 10: Lifecycle Subsystem — closure, downgrade-grace, knowledge axis,
audit-tier retention, data-export jobs, C6 BYPASSRLS role.

Revision ID: arc10_lifecycle_subsystem
Revises: arc9_2_pr101_drop_tenant_id_column
Create Date: 2026-05-27

Migration-graph note: Arc 10 was originally drafted against down_revision
b2e5f17a3d9c (Arc 8 WU-6 email_suppression), which was the head at the
time of branch creation. Arc 9.2 PR #101
(arc9_2_pr101_drop_tenant_id_column) landed on main between branch
creation and Arc 10 ready-to-apply, creating a second head and a fork
in the migration graph. Per Vision Section 6 / 7 'no inconsistent
deployments at completion', Arc 10 is rebased to chain after the
later head so the graph stays linear. Arc 10 does not reference
tenant_id (PR #101 dropped it), so the rebase is semantically safe.

Why this migration exists
-------------------------

Arc 10 owns the full lifecycle subsystem per Vision §6 and Architecture §3.6.
This migration is the single schema diff that supports:

  * Account closure with a 30-day grace clock (Vision §6.3 / §6.5).
    Supersedes the prior 90-day PIPEDA-only retention lock dated
    2026-05-14 09:55 EDT. Strictly tighter window; PIPEDA Principle 5
    remains satisfied because 30 days is still a defined retention
    period — and matches the founder-approved Vision verbatim.

  * Instance deactivation with an explicit soft-delete clock
    (Architecture §3.6.1 — "soft-delete window measured from
    ``soft_deleted_at`` (locked)").

  * Downgrade-archive knowledge axis (Customer Journey Phase 8 Pro:
    "oldest knowledge sources over the cap are archived (not deleted)
    until he upgrades again"). Operates on knowledge_embeddings grouped
    by source_id because there is no knowledge_sources table.

  * Downgrade read-only grace window with day-30 enforcement worker
    (Customer Journey Phase 8 Pro).

  * Audit-log tier-conditional retention (Vision §6.5, §7) with
    cold-storage archival and tier-at-write stickiness across downgrades.

  * Pre-closure data export (Architecture §3.6.3) — the data_export_jobs
    table backs the asynchronous bundle generator.

  * C6 absorbed from Arc 9: a dedicated luciel_retention_worker
    Postgres role with BYPASSRLS, so the retention worker and the
    audit-tier-retention worker can DELETE/UPDATE across many admins
    without binding to an admin_id per row. The existing
    rls_tenant_context_enabled guard in retention.py is removed in the
    paired code change because this role makes it unnecessary.

Drift entries closed
--------------------

  * ``D-arc10-admins-deactivated-at-missing-from-rename-2026-05-27``
      Arc 5's tenant_configs → admins rename added every column except
      deactivated_at, which still lives only on the legacy
      tenant_configs table. The cascade in admin_service.py uses a
      try/except fallback between the two table names. This migration
      adds deactivated_at to admins, backfills from tenant_configs,
      and the paired code change removes the fallback.

  * ``D-arc10-no-closure-clock-distinct-from-deactivation-2026-05-27``
      Hard-delete keys on deactivated_at today, but deactivation can
      fire from multiple sources (admin action, platform-admin ToS
      action, webhook). Closure is the only source that should advance
      a tenant toward hard-delete. This migration adds
      closure_initiated_at as the distinct hard-delete clock; the
      retention worker's scan predicate (paired code change) reads
      from this column, not from deactivated_at.

  * ``D-arc10-audit-tier-retention-missing-2026-05-27``
      Vision §6.5 mandates tier-conditional audit retention
      (30d Free / 1y Pro / 7y Enterprise). The retention worker today
      explicitly does not delete AdminAuditLog rows. This migration
      adds tier_at_write (sticky per row) and cold_archived_at so the
      new audit_retention_service can run per-tier purges with cold
      archival without losing the hash chain.

  * ``D-arc10-c61-vision-divergence-on-audit-immutability-2026-05-27``
      Arc 9 C6.1's docstring ("FORWARD-ONLY audit-log immutability…
      Even the ops role cannot mutate or delete audit rows… PIPEDA
      principle 5 (retention limits) does not apply to AdminAuditLog
      rows") is a doctrine drift from Vision §6.5 ("audit log archived
      to cold storage for legal retention window") and §7 (tier-
      conditional retention: 30d / 1y / 7y). Per Vision §10 doctrine-
      anchor: "if code, doctrine, or roadmap diverges from this
      vision, this document wins." Vision is canonical. We preserve
      C6.1's blast-radius discipline (luciel_ops still does NOT get
      audit UPDATE) by giving the audit-tier work its own, even-more-
      narrowly-granted role: luciel_audit_archiver, SELECT + UPDATE
      on admin_audit_log only, no DELETE.

  * ``D-arc10-retention-worker-still-on-default-session-2026-05-27``
      Arc 9 C6.1 created luciel_ops with BYPASSRLS and C6.3 wired
      OpsSessionLocal, but the retention worker today still uses
      SessionLocal with the rls_tenant_context_enabled guard. The
      paired code change in this PR switches the worker to
      OpsSessionLocal and removes the guard. No schema change here —
      this drift is closed by paired code, surfaced in the migration
      docstring for the doctrine trail.

  * ``D-arc10-data-export-greenfield-2026-05-27``
      No data-export service or table existed prior to Arc 10.
      data_export_jobs is created here with the same RLS posture as
      every other Arc 9 customer-data table.

What this migration adds
------------------------

Tables:
  * data_export_jobs — async bundle-generation job tracking

Columns on existing tables:
  * admins:
      - deactivated_at         (drift reconciliation from Arc 5 rename)
      - closure_initiated_at   (Arc 10 grace clock)
      - closure_cancel_mode    ('immediate' | 'period_end')
      - hard_deleted_at        (tombstone terminal stamp)
  * admin_audit_log:
      - tier_at_write          (sticky per row for L5 invariant)
      - cold_archived_at       (chain-of-custody marker)
  * api_keys:
      - revoked_at             (forensic timestamp; complements 'active')
  * instances:
      - soft_deleted_at        (Architecture §3.6.1 window clock)
  * knowledge_embeddings:
      - soft_deleted_at        (lifecycle, distinct from superseded_at)
      - pending_downgrade_archived_at  (5th axis on downgrade-archive)
  * subscriptions:
      - pending_downgrade_initiated_at   (grace window start)
      - pending_downgrade_enforced_at    (idempotency stamp)

Roles:
  * luciel_audit_archiver  (BYPASSRLS; SELECT + UPDATE on
    admin_audit_log ONLY; no DELETE, no DDL, no other table access.
    Single-purpose role for the audit-tier-retention worker. Reconciles
    Vision §6.5/§7 with C6.1's blast-radius discipline.)
  * (luciel_ops, from Arc 9 C6.1, is REUSED for the tenant hard-delete
    cascade. Not created or modified by this migration.)

Indexes:
  * ix_admins_closure_clock_eligible
  * ix_admins_closure_initiated_at
  * ix_admin_audit_log_tier_at_write_created
  * ix_admin_audit_log_cold_archived
  * ix_api_keys_revoked_at
  * ix_instances_soft_deleted_at
  * ix_knowledge_embeddings_soft_deleted
  * ix_knowledge_embeddings_pending_downgrade
  * ix_knowledge_embeddings_lru_source
  * ix_subscriptions_downgrade_grace_eligible
  * ix_data_export_jobs_admin
  * ix_data_export_jobs_status_active
  * ux_data_export_jobs_one_active_per_admin

RLS policies:
  * data_export_jobs_admin_isolation       (SELECT)
  * data_export_jobs_admin_isolation_write (INSERT)

Rollback
--------

The down-migration removes columns, the table, the role, the policies,
and (in the paired code change) reverts RETENTION_WINDOW_DAYS to 90.
The tombstone redaction performed by hard_delete_tenant_after_retention
on any admin row hard-deleted in production CANNOT be reversed; the PII
is gone permanently. This is a known one-way door. Acceptable because
the migration runs in staging first.

Paired code changes — landing in the same PR
--------------------------------------------

  * app/worker/tasks/retention.py:
      - RETENTION_WINDOW_DAYS: 90 → 30 (lock-date comment updated)
      - Scan predicate: tenant_configs.deactivated_at → admins.closure_initiated_at
      - SessionLocal → RetentionSessionLocal (BYPASSRLS role)
      - rls_tenant_context_enabled guard REMOVED
  * app/services/admin_service.py:
      - hard_delete_tenant_after_retention step 11: DELETE → UPDATE
        (tombstone with hard_deleted_at + PII redaction)
      - tenant_configs fallback in cascade REMOVED
  * app/db/session.py:
      - New RetentionSessionLocal factory bound to RETENTION_DATABASE_URL
  * app/core/config.py:
      - New setting retention_database_url
  * app/models/{admin,instance,api_key,knowledge,subscription,admin_audit_log}.py:
      - ORM-side column declarations matching the new schema
  * New service modules:
      - closure_service, reactivation_service, data_export_service,
        audit_retention_service, downgrade_grace_service
      - downgrade_archive_service: AXIS_KNOWLEDGE added
  * New routes in admin.py and billing.py per Architecture §3.6 contract

Founder locks reflected here
----------------------------

L1   single 30-day clock (this thread, 2026-05-27)
L2   knowledge as 5th downgrade-archive axis (this thread)
L3   read-only grace + day-30 enforcement (this thread)
L4   audit-tier retention in scope (this thread)
L5   tier-at-write sticky across downgrades (this thread)
L10  cascade target = admins; tenant_configs fallback removed (this thread)
L11  C6 BYPASSRLS — reuse existing luciel_ops for tenant hard-delete;
     add new luciel_audit_archiver for audit-tier work (Path B refined
     post-recon) (this thread)
L13  tombstone at hard-delete (this thread, default per Vision §6.5)
L14  no knowledge_sources table; embeddings carry the column (this thread)
"""
from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


# ---------------------------------------------------------------------
# Alembic identifiers.
# ---------------------------------------------------------------------
revision = "arc10_lifecycle_subsystem"
# Rebased from b2e5f17a3d9c to arc9_2_pr101_drop_tenant_id_column on
# 2026-05-27 to collapse two heads back to one. See module docstring.
down_revision = "arc9_2_pr101_drop_tenant_id_column"
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------
# Constants surfaced for the down-migration symmetry.
# ---------------------------------------------------------------------
# Arc 10 reuses the EXISTING luciel_ops role (created in Arc 9 C6.1) for
# the tenant hard-delete cascade. That role already has BYPASSRLS and
# already grants SELECT + DELETE on the post-arc5_c cascade tables
# (sessions, conversations, identity_claims, memory_items, api_keys,
# instances). Building a second cascade-purpose role would be the
# "redundant components" violation. No new role for that work.
#
# Arc 10 DOES add a NEW role, luciel_audit_archiver, narrowly scoped
# to the audit-tier-retention work that Vision §6.5 + §7 require. The
# stance in Arc 9 C6.1's docstring ("forward-only audit-log
# immutability — even the ops role cannot mutate audit rows") is a
# doctrine drift from the founder-approved Vision. Per Vision §10,
# Vision wins. We reconcile by giving the audit-tier work its OWN role:
#
#   * Separate from luciel_ops (preserves C6.1 blast-radius discipline:
#     luciel_ops STILL cannot mutate audit rows; only this new,
#     single-purpose role can).
#   * Narrowly granted to SELECT + UPDATE on admin_audit_log ONLY
#     (no DELETE in this arc; chain stays append-only in hot+cold
#     combined; we move rows to S3 cold storage, not delete them).
#   * BYPASSRLS so the worker can scan rows across all admins.
#   * No grants on any other table; no DDL.
_ARCHIVER_ROLE = "luciel_audit_archiver"

_ARCHIVER_AUDIT_TABLES_RU = (
    # The audit-tier-retention worker SELECTs eligible rows (filtered
    # by tier_at_write + created_at against the per-tier window) and
    # UPDATEs cold_archived_at after the S3 cold-archive write succeeds.
    # No DELETE — the chain is append-only in hot+cold combined.
    "admin_audit_log",
)


def upgrade() -> None:
    """Apply the Arc 10 lifecycle schema."""

    # -----------------------------------------------------------------
    # 1. admins — drift reconciliation + Arc 10 lifecycle columns.
    # -----------------------------------------------------------------
    # deactivated_at is the Arc-5-rename drift fix. It must come before
    # the closure columns because the backfill (next block) reads from
    # the legacy tenant_configs.deactivated_at into admins.deactivated_at.
    op.add_column(
        "admins",
        sa.Column(
            "deactivated_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Soft-deactivation timestamp. Stamped by "
                "deactivate_tenant_with_cascade. Distinct from "
                "closure_initiated_at: closure is admin-initiated; "
                "deactivation can come from platform-admin or webhook."
            ),
        ),
    )

    # Backfill from legacy tenant_configs if it still exists. The
    # DO/EXECUTE block keeps the migration idempotent — if a future
    # cleanup migration drops tenant_configs entirely, this no-ops.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.tables
                 WHERE table_name = 'tenant_configs'
            )
            AND EXISTS (
                SELECT 1 FROM information_schema.columns
                 WHERE table_name = 'tenant_configs'
                   AND column_name = 'deactivated_at'
            )
            THEN
                EXECUTE
                    'UPDATE admins a
                        SET deactivated_at = tc.deactivated_at
                       FROM tenant_configs tc
                      WHERE tc.admin_id = a.id
                        AND a.deactivated_at IS NULL';
            END IF;
        END
        $$;
        """
    )

    op.add_column(
        "admins",
        sa.Column(
            "closure_initiated_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Set when the admin invokes POST /admin/account/close. "
                "Starts the 30-day grace clock. The retention worker "
                "keys hard-delete eligibility off THIS column, not off "
                "deactivated_at — closure is the only path to hard-delete."
            ),
        ),
    )
    op.add_column(
        "admins",
        sa.Column(
            "closure_cancel_mode",
            sa.String(length=16),
            nullable=True,
            comment=(
                "Admin's Stripe-cancel choice at closure: 'immediate' "
                "or 'period_end'. Read by _on_subscription_deleted."
            ),
        ),
    )
    op.add_column(
        "admins",
        sa.Column(
            "hard_deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Terminal tombstone stamp. Set by "
                "hard_delete_tenant_after_retention. Once set, the row "
                "is audit-only (name redacted, stripe_customer_id NULL). "
                "Vision §6.5 'minimal compliance record retained per "
                "legal requirements'."
            ),
        ),
    )
    op.create_check_constraint(
        "ck_admins_closure_cancel_mode",
        "admins",
        "closure_cancel_mode IS NULL "
        "OR closure_cancel_mode IN ('immediate', 'period_end')",
    )

    # Partial index supporting the retention worker scan predicate:
    #   WHERE active = false
    #     AND closure_initiated_at IS NOT NULL
    #     AND closure_initiated_at < (now() - INTERVAL '30 days')
    #     AND hard_deleted_at IS NULL
    op.execute(
        """
        CREATE INDEX ix_admins_closure_clock_eligible
            ON admins (closure_initiated_at)
            WHERE active = false
              AND closure_initiated_at IS NOT NULL
              AND hard_deleted_at IS NULL
        """
    )

    # Partial index for reactivation lookups within the grace window.
    op.execute(
        """
        CREATE INDEX ix_admins_closure_initiated_at
            ON admins (closure_initiated_at)
            WHERE closure_initiated_at IS NOT NULL
        """
    )

    # -----------------------------------------------------------------
    # 2. admin_audit_log — sticky tier + cold-archive marker.
    # -----------------------------------------------------------------
    # tier_at_write is nullable in schema so the backfill below cannot
    # fail on rows whose admin_id no longer exists. Going forward,
    # AdminAuditRepository.record() writes it NOT NULL via the
    # repository contract (paired code change).
    op.add_column(
        "admin_audit_log",
        sa.Column(
            "tier_at_write",
            sa.String(length=16),
            nullable=True,
            comment=(
                "Admin's tier AT THE MOMENT this audit row was written. "
                "Sticky across downgrades — a Pro→Free downgrade does "
                "NOT shorten the retention of Pro-era audit rows."
            ),
        ),
    )

    # Best-effort backfill from current admin tier. Day-0 artifact;
    # historical tier is unknowable from this point in code.
    op.execute(
        """
        UPDATE admin_audit_log aal
           SET tier_at_write = a.tier
          FROM admins a
         WHERE aal.admin_id = a.id
           AND aal.tier_at_write IS NULL
        """
    )

    op.add_column(
        "admin_audit_log",
        sa.Column(
            "cold_archived_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Set when the audit-tier-retention worker archives this "
                "row to S3 cold storage. Hash-chain extended at archive "
                "time so chain-of-custody survives the hot/cold boundary."
            ),
        ),
    )

    op.create_index(
        "ix_admin_audit_log_tier_at_write_created",
        "admin_audit_log",
        ["tier_at_write", "created_at"],
    )
    op.execute(
        """
        CREATE INDEX ix_admin_audit_log_cold_archived
            ON admin_audit_log (cold_archived_at)
            WHERE cold_archived_at IS NOT NULL
        """
    )

    # -----------------------------------------------------------------
    # 3. api_keys — revoke-time forensic stamp.
    # -----------------------------------------------------------------
    # Complements api_keys.active (operational on/off) and
    # api_keys.pending_downgrade_archived_at (downgrade-archived).
    # When ApiKeyService.deactivate_key / .deactivate_all_for_tenant
    # runs, it stamps revoked_at = now() so forensics can answer
    # 'when was this key revoked?' without joining to audit log.
    op.add_column(
        "api_keys",
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Set when an embed/admin key is revoked. Distinct from "
                "'active' (operational) and pending_downgrade_archived_at "
                "(recoverable archive). Once revoked, never reissued."
            ),
        ),
    )
    op.execute(
        """
        CREATE INDEX ix_api_keys_revoked_at
            ON api_keys (revoked_at)
            WHERE revoked_at IS NOT NULL
        """
    )

    # -----------------------------------------------------------------
    # 4. instances — soft-delete clock.
    # -----------------------------------------------------------------
    # Architecture §3.6.1: 'soft-delete window measured from
    # soft_deleted_at (locked). Clean, predictable, not tied to
    # last-active heuristics.' This is what gives the 30-day
    # knowledge-soft-delete window its anchor.
    op.add_column(
        "instances",
        sa.Column(
            "soft_deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Set when the instance is deactivated. The soft-delete "
                "worker reads this column to find instances 30 days "
                "past deactivation and hard-deletes their knowledge."
            ),
        ),
    )
    op.execute(
        """
        CREATE INDEX ix_instances_soft_deleted_at
            ON instances (soft_deleted_at)
            WHERE soft_deleted_at IS NOT NULL
        """
    )

    # -----------------------------------------------------------------
    # 5. knowledge_embeddings — lifecycle columns (no new source table).
    # -----------------------------------------------------------------
    # Three lifecycle flags now coexist:
    #   - superseded_at                  (Arc 11 version supersede)
    #   - soft_deleted_at                (Arc 10 deactivation soft-delete)
    #   - pending_downgrade_archived_at  (Arc 10 5th downgrade-archive axis)
    # The Arc 11 retrieval layer is responsible for extending its filter
    # to read: WHERE superseded_at IS NULL AND soft_deleted_at IS NULL
    #            AND pending_downgrade_archived_at IS NULL.
    op.add_column(
        "knowledge_embeddings",
        sa.Column(
            "soft_deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Lifecycle flag distinct from superseded_at. Set by the "
                "soft-delete worker when the parent instance is 30 days "
                "past deactivation. Retrieval filter excludes immediately."
            ),
        ),
    )
    op.add_column(
        "knowledge_embeddings",
        sa.Column(
            "pending_downgrade_archived_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Set when DowngradeArchiveService archives this chunk's "
                "source at a Pro→Free boundary. Recoverable on re-upgrade. "
                "All chunks sharing a source_id archive together."
            ),
        ),
    )
    op.execute(
        """
        CREATE INDEX ix_knowledge_embeddings_soft_deleted
            ON knowledge_embeddings (soft_deleted_at)
            WHERE soft_deleted_at IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX ix_knowledge_embeddings_pending_downgrade
            ON knowledge_embeddings (pending_downgrade_archived_at)
            WHERE pending_downgrade_archived_at IS NOT NULL
        """
    )
    # LRU sort + per-source aggregation. Supports the knowledge axis
    # in DowngradeArchiveService: per admin, group by source_id, sort
    # sources by their oldest-updated chunk, archive oldest sources
    # first until the cap is met.
    op.execute(
        """
        CREATE INDEX ix_knowledge_embeddings_lru_source
            ON knowledge_embeddings (admin_id, source_id, updated_at)
            WHERE superseded_at IS NULL
              AND soft_deleted_at IS NULL
              AND pending_downgrade_archived_at IS NULL
        """
    )

    # -----------------------------------------------------------------
    # 6. subscriptions — downgrade grace clock.
    # -----------------------------------------------------------------
    # pending_downgrade_target already exists (Arc 6 Commit 8.5b).
    # We add the two clock columns the grace-window middleware and
    # the enforcement worker need.
    op.add_column(
        "subscriptions",
        sa.Column(
            "pending_downgrade_initiated_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Set when POST /billing/downgrade fires. Starts the "
                "30-day read-only grace clock for the downgrade."
            ),
        ),
    )
    op.add_column(
        "subscriptions",
        sa.Column(
            "pending_downgrade_enforced_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Set by the day-30 enforcement worker after it runs "
                "DowngradeArchiveService.archive_overflow_for_admin. "
                "Makes the enforcement idempotent against re-scans."
            ),
        ),
    )
    op.execute(
        """
        CREATE INDEX ix_subscriptions_downgrade_grace_eligible
            ON subscriptions (pending_downgrade_initiated_at)
            WHERE pending_downgrade_target IS NOT NULL
              AND pending_downgrade_enforced_at IS NULL
        """
    )

    # -----------------------------------------------------------------
    # 7. data_export_jobs — the pre-closure data-export tracking table.
    # -----------------------------------------------------------------
    # Schema-level invariants enforced by CHECKs + partial unique index:
    #   * status ∈ {pending, generating, ready, expired, failed}
    #   * tier_at_request ∈ {free, pro, enterprise}
    #   * triggered_by ∈ {admin_request, grace_window_request}
    #   * At most one (pending, generating) job per admin (concurrency lock).
    op.create_table(
        "data_export_jobs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "admin_id",
            sa.String(length=100),
            nullable=False,
            index=False,  # composite index below
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("s3_bucket", sa.Text(), nullable=True),
        sa.Column("s3_key", sa.Text(), nullable=True),
        sa.Column("bytes_size", sa.BigInteger(), nullable=True),
        sa.Column("signed_url_ttl_seconds", sa.Integer(), nullable=True),
        sa.Column(
            "signed_url_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "tier_at_request",
            sa.String(length=16),
            nullable=False,
        ),
        sa.Column(
            "triggered_by",
            sa.String(length=32),
            nullable=False,
            server_default="admin_request",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'generating', 'ready', 'expired', 'failed')",
            name="ck_data_export_jobs_status_valid",
        ),
        sa.CheckConstraint(
            "tier_at_request IN ('free', 'pro', 'enterprise')",
            name="ck_data_export_jobs_tier_valid",
        ),
        sa.CheckConstraint(
            "triggered_by IN ('admin_request', 'grace_window_request')",
            name="ck_data_export_jobs_triggered_by_valid",
        ),
        comment=(
            "Arc 10: async data-export bundle generation. "
            "One row per requested export. The Celery task "
            "data_export_service.generate_bundle reads pending rows, "
            "produces the bundle per Architecture §3.6.3, uploads to "
            "S3, stamps ready_at and the signed-URL fields."
        ),
    )
    op.create_index(
        "ix_data_export_jobs_admin",
        "data_export_jobs",
        ["admin_id", sa.text("requested_at DESC")],
    )
    op.execute(
        """
        CREATE INDEX ix_data_export_jobs_status_active
            ON data_export_jobs (status, requested_at)
            WHERE status IN ('pending', 'generating')
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_data_export_jobs_one_active_per_admin
            ON data_export_jobs (admin_id)
            WHERE status IN ('pending', 'generating')
        """
    )

    # -----------------------------------------------------------------
    # 8. RLS on data_export_jobs.
    # -----------------------------------------------------------------
    # Same fail-closed pattern as every other Arc 9 customer-data table.
    # If app.admin_id is not set on the connection, every policy returns
    # zero rows. The luciel_retention_worker role created below is
    # exempt because it has BYPASSRLS.
    op.execute("ALTER TABLE data_export_jobs ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY data_export_jobs_admin_isolation
            ON data_export_jobs
            USING (admin_id = current_setting('app.admin_id', true)::text)
        """
    )
    op.execute(
        """
        CREATE POLICY data_export_jobs_admin_isolation_write
            ON data_export_jobs
            FOR INSERT
            WITH CHECK (admin_id = current_setting('app.admin_id', true)::text)
        """
    )

    # -----------------------------------------------------------------
    # 9. luciel_audit_archiver role — narrowly-granted BYPASSRLS role
    #    for the audit-tier-retention worker (Vision §6.5 + §7).
    # -----------------------------------------------------------------
    # See module-level constants block for the doctrine reconciliation
    # against Arc 9 C6.1. The tenant hard-delete cascade reuses the
    # existing luciel_ops role; this migration does NOT create a
    # second cascade role.
    #
    # Password sourced from env at apply time. The CI/deploy script
    # exports ARC10_AUDIT_ARCHIVER_PASSWORD from SSM before running
    # alembic upgrade head. The migration never embeds the secret.
    # If the env var is unset, the role is created with no password
    # (cannot log in); deploy must run a follow-up ALTER ROLE.
    # Failing closed is correct.
    archiver_password = os.environ.get("ARC10_AUDIT_ARCHIVER_PASSWORD")

    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_roles WHERE rolname = '{_ARCHIVER_ROLE}'
            )
            THEN
                EXECUTE 'CREATE ROLE {_ARCHIVER_ROLE} WITH LOGIN BYPASSRLS';
            END IF;
        END $$;
        """
    )

    if archiver_password is not None:
        # Set the password via the set_config-then-format pattern.
        # PostgreSQL does not accept bind params on ALTER ROLE
        # PASSWORD directly. set_config(name, value, is_local=true)
        # stores the password in a session-local GUC that EXECUTE
        # format() can read via current_setting(). is_local=true
        # scopes the setting to this transaction so it cannot leak.
        conn = op.get_bind()
        conn.exec_driver_sql(
            "SELECT set_config('arc10.archiver_pw', %s, true)",
            (archiver_password,),
        )
        op.execute(
            f"""
            DO $$
            BEGIN
                EXECUTE format(
                    'ALTER ROLE {_ARCHIVER_ROLE} WITH LOGIN BYPASSRLS PASSWORD %L',
                    current_setting('arc10.archiver_pw', true)
                );
            END $$;
            """
        )
        # Defense in depth: explicitly clear the session-local GUC.
        conn.exec_driver_sql("SELECT set_config('arc10.archiver_pw', '', true)")

    # Grants — strictly admin_audit_log only.
    # CONNECT, then USAGE on schema, then the narrow per-table grant.
    #
    # Database name comes from current_database() rather than from the
    # SQLAlchemy URL fragment. The URL fragment can be empty or have
    # alternative shapes under RDS proxy / IAM auth; current_database()
    # is the authoritative source at execution time.
    op.execute(
        f"""
        DO $$
        DECLARE
            dbname text := current_database();
        BEGIN
            EXECUTE format('GRANT CONNECT ON DATABASE %I TO {_ARCHIVER_ROLE}', dbname);
        END $$;
        """
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {_ARCHIVER_ROLE}")

    for tbl in _ARCHIVER_AUDIT_TABLES_RU:
        op.execute(f"GRANT SELECT, UPDATE ON {tbl} TO {_ARCHIVER_ROLE}")

    # Explicit denial: no schema-altering privileges; no DELETE on
    # admin_audit_log (chain integrity); no access to any other table.
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {_ARCHIVER_ROLE}")

    # -----------------------------------------------------------------
    # 9b. Tenant hard-delete cascade reuses the existing luciel_ops
    #     role from Arc 9 C6.1. Not modified here.
    # -----------------------------------------------------------------
    # Paired code change: app/worker/tasks/retention.py switches from
    # SessionLocal to OpsSessionLocal (already defined in
    # app/db/session.py via Arc 9 C6.3). The rls_tenant_context_enabled
    # guard in the worker is removed because BYPASSRLS makes the
    # underlying gap (instance_id=None filtering out instance-scoped
    # rows under Wall-3 USING) unreachable for that role.
    #
    # If a grant gap on luciel_ops is discovered during staging E2E,
    # it lands in a follow-up migration so the Arc 9 C6.1 source-of-
    # truth doesn't get retroactively edited.


def downgrade() -> None:
    """Roll back the Arc 10 lifecycle schema.

    Note: cannot reverse any production tombstone that has been written.
    Any admin row whose hard_deleted_at has been set has had its name
    and stripe_customer_id redacted; that PII is gone permanently. The
    schema down-migration drops the column, which makes the redaction
    invisible at the column level but the underlying data is still gone.
    """

    # -----------------------------------------------------------------
    # 9. luciel_audit_archiver role drop. Revoke grants first;
    #    PG refuses to drop a role that owns objects or holds grants.
    # -----------------------------------------------------------------
    # luciel_ops is NOT touched on downgrade — it was created by Arc 9
    # C6.1 and is owned by that migration's lifecycle. Arc 10 only
    # reuses it via the paired code change in retention.py.
    for tbl in _ARCHIVER_AUDIT_TABLES_RU:
        op.execute(f"REVOKE ALL ON {tbl} FROM {_ARCHIVER_ROLE}")
    op.execute(f"REVOKE ALL ON SCHEMA public FROM {_ARCHIVER_ROLE}")
    op.execute(
        f"""
        DO $$
        DECLARE
            dbname text := current_database();
        BEGIN
            EXECUTE format('REVOKE ALL ON DATABASE %I FROM {_ARCHIVER_ROLE}', dbname);
        END $$;
        """
    )
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_roles WHERE rolname = '{_ARCHIVER_ROLE}'
            )
            THEN
                EXECUTE 'DROP ROLE {_ARCHIVER_ROLE}';
            END IF;
        END $$;
        """
    )

    # -----------------------------------------------------------------
    # 8. RLS on data_export_jobs.
    # -----------------------------------------------------------------
    op.execute(
        "DROP POLICY IF EXISTS data_export_jobs_admin_isolation_write "
        "ON data_export_jobs"
    )
    op.execute(
        "DROP POLICY IF EXISTS data_export_jobs_admin_isolation "
        "ON data_export_jobs"
    )
    op.execute("ALTER TABLE data_export_jobs DISABLE ROW LEVEL SECURITY")

    # -----------------------------------------------------------------
    # 7. data_export_jobs table.
    # -----------------------------------------------------------------
    op.execute("DROP INDEX IF EXISTS ux_data_export_jobs_one_active_per_admin")
    op.execute("DROP INDEX IF EXISTS ix_data_export_jobs_status_active")
    op.drop_index("ix_data_export_jobs_admin", table_name="data_export_jobs")
    op.drop_table("data_export_jobs")

    # -----------------------------------------------------------------
    # 6. subscriptions.
    # -----------------------------------------------------------------
    op.execute("DROP INDEX IF EXISTS ix_subscriptions_downgrade_grace_eligible")
    op.drop_column("subscriptions", "pending_downgrade_enforced_at")
    op.drop_column("subscriptions", "pending_downgrade_initiated_at")

    # -----------------------------------------------------------------
    # 5. knowledge_embeddings.
    # -----------------------------------------------------------------
    op.execute("DROP INDEX IF EXISTS ix_knowledge_embeddings_lru_source")
    op.execute("DROP INDEX IF EXISTS ix_knowledge_embeddings_pending_downgrade")
    op.execute("DROP INDEX IF EXISTS ix_knowledge_embeddings_soft_deleted")
    op.drop_column("knowledge_embeddings", "pending_downgrade_archived_at")
    op.drop_column("knowledge_embeddings", "soft_deleted_at")

    # -----------------------------------------------------------------
    # 4. instances.
    # -----------------------------------------------------------------
    op.execute("DROP INDEX IF EXISTS ix_instances_soft_deleted_at")
    op.drop_column("instances", "soft_deleted_at")

    # -----------------------------------------------------------------
    # 3. api_keys.
    # -----------------------------------------------------------------
    op.execute("DROP INDEX IF EXISTS ix_api_keys_revoked_at")
    op.drop_column("api_keys", "revoked_at")

    # -----------------------------------------------------------------
    # 2. admin_audit_log.
    # -----------------------------------------------------------------
    op.execute("DROP INDEX IF EXISTS ix_admin_audit_log_cold_archived")
    op.drop_index(
        "ix_admin_audit_log_tier_at_write_created",
        table_name="admin_audit_log",
    )
    op.drop_column("admin_audit_log", "cold_archived_at")
    op.drop_column("admin_audit_log", "tier_at_write")

    # -----------------------------------------------------------------
    # 1. admins.
    # -----------------------------------------------------------------
    op.execute("DROP INDEX IF EXISTS ix_admins_closure_initiated_at")
    op.execute("DROP INDEX IF EXISTS ix_admins_closure_clock_eligible")
    op.drop_constraint("ck_admins_closure_cancel_mode", "admins", type_="check")
    op.drop_column("admins", "hard_deleted_at")
    op.drop_column("admins", "closure_cancel_mode")
    op.drop_column("admins", "closure_initiated_at")
    op.drop_column("admins", "deactivated_at")
