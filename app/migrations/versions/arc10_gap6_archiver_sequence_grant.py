"""Arc 10 Gap 6 close (continued): grant USAGE on
admin_audit_logs_id_seq to luciel_audit_archiver so the worker's
batch-audit INSERT can read the next id from the sequence.

Revision ID: arc10_gap6_archiver_sequence_grant
Revises: arc10_gap6_archiver_insert_grant
Create Date: 2026-05-27

D-arc10-audit-archiver-sequence-grant-missing-2026-05-27.

After arc10_gap6_archiver_insert_grant (PR #108) added INSERT, the
in-cluster E2E surfaced the next-layer drift: PostgreSQL requires
USAGE on the underlying SERIAL sequence to assign the next id on
INSERT. Without it:

  psycopg.errors.InsufficientPrivilege: permission denied for
  sequence admin_audit_logs_id_seq

Same partial-state class of bug as the previous two: the transaction
rolls back AFTER the S3 cold-archive object is written.

This is the third and (modulo further surface I haven't yet
exercised) hopefully final grant the original arc10_lifecycle_subsystem
migration missed for the archiver role. Each surfaced by a real
in-cluster run; each fixed forward-only via a follow-up migration
rather than mutating the original arc10 migration source.

Doctrine alignment: sequence USAGE is required to assign id values
to forward-only INSERTs into admin_audit_logs. It is structurally
implied by the INSERT grant; no additional row-level mutation
authority is conveyed.

Idempotency: PostgreSQL GRANT is additive; safe to re-run.
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = "arc10_gap6_archiver_sequence_grant"
down_revision = "arc10_gap6_archiver_insert_grant"
branch_labels = None
depends_on = None


_ARCHIVER_ROLE = "luciel_audit_archiver"
_TARGET_SEQUENCE = "admin_audit_logs_id_seq"


def upgrade() -> None:
    """Grant USAGE on admin_audit_logs_id_seq to luciel_audit_archiver.

    USAGE is the minimum sequence privilege that allows nextval() in
    the INSERT statement to assign a new id. SELECT on a sequence
    only grants currval/lastval reads (not nextval); UPDATE on a
    sequence allows setval (not needed here). USAGE is the minimal
    correct grant.
    """
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_ARCHIVER_ROLE}') THEN
                RAISE EXCEPTION '{_ARCHIVER_ROLE} role missing; run arc10_lifecycle_subsystem first';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM pg_class WHERE relname = '{_TARGET_SEQUENCE}' AND relkind = 'S'
            ) THEN
                RAISE EXCEPTION 'sequence {_TARGET_SEQUENCE} missing';
            END IF;
        END $$;
        """
    )
    op.execute(f"GRANT USAGE ON SEQUENCE {_TARGET_SEQUENCE} TO {_ARCHIVER_ROLE}")


def downgrade() -> None:
    """Revoke the USAGE privilege.

    After downgrade the worker will once again fail its INSERT with
    'permission denied for sequence'. Provided for migration symmetry
    only; forward-only in practice.
    """
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_ARCHIVER_ROLE}') THEN
                EXECUTE 'REVOKE USAGE ON SEQUENCE {_TARGET_SEQUENCE} FROM {_ARCHIVER_ROLE}';
            END IF;
        END $$;
        """
    )
