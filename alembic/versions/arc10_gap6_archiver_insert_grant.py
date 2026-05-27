"""Arc 10 Gap 6 close: grant INSERT on admin_audit_logs to
luciel_audit_archiver so the move-to-cold task can write its own
per-batch audit row in the same transaction that stamps cold_archived_at.

Revision ID: arc10_gap6_archiver_insert_grant
Revises: arc10_lifecycle_subsystem
Create Date: 2026-05-27

D-arc10-audit-archiver-cannot-insert-batch-audit-row-2026-05-27.

Original Arc 10 migration granted SELECT + UPDATE only to
luciel_audit_archiver, reasoning that "the chain is append-only in
hot+cold combined" and that the worker only needs to (a) SELECT
eligible rows and (b) UPDATE cold_archived_at. That analysis missed
the third operation the worker actually performs:

  AuditRetentionService._archive_one_tier(...)
    -> writes the S3 cold-archive object
    -> UPDATEs cold_archived_at on each archived hot row
    -> _emit_batch_audit(...) INSERTs one admin_audit_logs row per
       (admin_id, tier_window) batch, with action=
       'audit_log_tier_archived', so a forensic auditor can answer
       'when was admin X's tier-Y window archived, by whom, to what
       S3 key' from the audit chain itself.

That INSERT is forward-only (append-only chain extension) and is in
the same transaction as the cold_archived_at UPDATE, so the
atomicity guarantee the original design intended -- "archive write
+ stamp + audit emit either all land or none land" -- requires the
INSERT to succeed on the same connection.

Without INSERT, the worker successfully:
  * SELECTs the eligible row
  * Writes the S3 cold-archive object
  * Attempts the batch audit emission -> psycopg.errors.
    InsufficientPrivilege -> transaction rolls back -> S3 object
    orphaned, cold_archived_at stays NULL, next worker tick
    re-archives the same row.

Doctrine alignment: Arc 9 C6.1 ("forward-only forever; even ops
cannot mutate audit rows") is preserved -- INSERT is forward-only;
the no-DELETE, no-UPDATE-by-app-code surface is unchanged. The
single controlled UPDATE exception (cold_archived_at stamping) is
unchanged. The new INSERT permission is on the same narrowly-scoped
worker role, used only by app/worker/tasks/audit_retention.py.

Idempotency: this migration is safe to run multiple times; PostgreSQL
GRANT is additive and a no-op if the privilege already exists.

Rollback: the downgrade revokes only the INSERT privilege, leaving
the existing SELECT + UPDATE in place (matching post-arc10-pre-this-
patch state).
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = "arc10_gap6_archiver_insert_grant"
down_revision = "arc10_lifecycle_subsystem"
branch_labels = None
depends_on = None


_ARCHIVER_ROLE = "luciel_audit_archiver"
_TARGET_TABLE = "admin_audit_logs"


def upgrade() -> None:
    """Grant INSERT on admin_audit_logs to luciel_audit_archiver.

    The worker needs INSERT to write the per-batch audit emission row
    (action='audit_log_tier_archived') in the same transaction that
    stamps cold_archived_at, preserving the all-or-nothing semantics
    the AuditRetentionService._archive_one_tier method relies on.
    """
    # Defensive role existence check -- a fresh dev/CI env that ran
    # arc10_lifecycle_subsystem has the role; a partial env may not.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_ARCHIVER_ROLE}') THEN
                RAISE EXCEPTION '{_ARCHIVER_ROLE} role missing; run arc10_lifecycle_subsystem first';
            END IF;
        END $$;
        """
    )
    op.execute(f"GRANT INSERT ON {_TARGET_TABLE} TO {_ARCHIVER_ROLE}")


def downgrade() -> None:
    """Revoke the INSERT privilege; leave SELECT + UPDATE in place.

    Note: after downgrade the audit_retention worker will again fail
    its batch-audit emission with InsufficientPrivilege. Downgrade is
    provided for migration symmetry only; in practice this fix is
    forward-only because the worker requires it.
    """
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_ARCHIVER_ROLE}') THEN
                EXECUTE 'REVOKE INSERT ON {_TARGET_TABLE} FROM {_ARCHIVER_ROLE}';
            END IF;
        END $$;
        """
    )
