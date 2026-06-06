"""step28_create_luciel_worker_role

Step 28 Phase 1, Commit 7 -- D-worker-role.

Creates a least-privilege Postgres role `luciel_worker` for the async
worker (Celery process running in the worker ECS task). The role can:

  - SELECT from context tables: agents, api_keys, luciel_instances,
    messages, sessions, users
  - SELECT + INSERT into the two write-surface tables: memory_items
    and admin_audit_logs (plus their PK sequences)

The role explicitly CANNOT:

  - INSERT/UPDATE/DELETE on api_keys (cannot mint or deactivate keys --
    closes the worker's blast radius if its credential leaks)
  - Run any DDL (NOCREATEDB + NOSUPERUSER + NOCREATEROLE on the role)
  - Read SSM bootstrap params (enforced at the IAM layer in Commit 8,
    not at the DB layer; the worker has no application-level SSM
    code paths today -- verified via U7 pre-commit grep on
    2026-04-30: zero functional boto3/ssm hits in app/worker/)

Operational notes:

  - Password is NOT set in this migration. Operator runs
    `python -m scripts.mint_worker_db_password_ssm --ssm` after this
    migration lands in a given environment to set the password and
    write the connection string to SSM
    (/luciel/<env>/worker_database_url).
  - Web tier continues to use the existing role via DATABASE_URL
    (unchanged). Worker tier picks up the new role via the new
    SSM param injected by the worker ECS task-def in Commit 8's
    task-def diff.
  - Pillar 11 (async memory verification) is the regression signal:
    after the worker task-def is repointed, Pillar 11 must stay green.

Recon ground truth (2026-04-30):
  - Read tables verified via app/models/*.py __tablename__ scan.
  - Sequence names verified via information_schema.sequences probe:
    ['admin_audit_logs_id_seq', 'memory_items_id_seq'].
  - Worker write surface verified via U7 grep: single db.commit() at
    app/worker/tasks/memory_extraction.py:448; only MemoryItem and
    AdminAuditLog INSERTs flow through it.

Drift register: closes "worker DB role" Phase 1 OPEN item.
Cross-ref: Commit 8 follows with luciel-worker-sg + task-def diff.
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = "f392a842f885"
down_revision = "4e4c2c0fb572"
branch_labels = None
depends_on = None


# Tables the worker reads (no writes).
READ_ONLY_TABLES = (
    "agents",
    "api_keys",
    "luciel_instances",
    "messages",
    "sessions",
    "users",
)

# Tables the worker reads and writes (INSERT only, no UPDATE/DELETE).
WRITE_TABLES = (
    "memory_items",
    "admin_audit_logs",
)

# Sequences backing the WRITE_TABLES PKs. Verified against live
# information_schema.sequences on 2026-04-30; do not change names
# without re-running the U7 sequence-name probe.
WRITE_TABLE_SEQUENCES = (
    "memory_items_id_seq",
    "admin_audit_logs_id_seq",
)


def upgrade() -> None:
    # 1. Create the role idempotently. Using a DO block so re-running
    #    the migration on an env where it already exists doesn't
    #    explode (e.g., re-applying after a rollback in dev).
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_roles WHERE rolname = 'luciel_worker'
            ) THEN
                CREATE ROLE luciel_worker WITH
                    LOGIN
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

    # 2. Schema usage. Without USAGE on the schema itself, even
    #    granted table SELECTs return permission denied. Granted
    #    first so the table grants below can take effect.
    op.execute("GRANT USAGE ON SCHEMA public TO luciel_worker;")

    # 3. Read-only grants on context tables.
    for table in READ_ONLY_TABLES:
        op.execute(f"GRANT SELECT ON {table} TO luciel_worker;")

    # 4. Read + INSERT on write-surface tables. Deliberately NO
    #    UPDATE, NO DELETE -- worker appends, never mutates or removes.
    #    This makes the "memory and audit logs are append-only"
    #    invariant database-enforced, not just policy-enforced.
    for table in WRITE_TABLES:
        op.execute(f"GRANT SELECT, INSERT ON {table} TO luciel_worker;")

    # 5. Sequence usage so INSERT can call nextval(). Without this,
    #    INSERTs fail with "permission denied for sequence" even
    #    though the table grant looks correct -- this is the silent
    #    failure mode the U7 sequence probe was designed to catch.
    for seq in WRITE_TABLE_SEQUENCES:
        op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {seq} TO luciel_worker;")


def downgrade() -> None:
    # Symmetric teardown. REVOKE before DROP -- Postgres will refuse
    # to DROP a role that still owns privileges on objects.

    for seq in WRITE_TABLE_SEQUENCES:
        op.execute(f"REVOKE ALL ON SEQUENCE {seq} FROM luciel_worker;")

    for table in WRITE_TABLES + READ_ONLY_TABLES:
        op.execute(f"REVOKE ALL ON {table} FROM luciel_worker;")

    op.execute("REVOKE ALL ON SCHEMA public FROM luciel_worker;")

    # Drop the role. If anything else has been granted to it
    # out-of-band (shouldn't happen, but defense in depth), this will
    # error with a clear message naming the holdout grant.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_roles WHERE rolname = 'luciel_worker'
            ) THEN
                DROP ROLE luciel_worker;
            END IF;
        END
        $$;
        """
    )