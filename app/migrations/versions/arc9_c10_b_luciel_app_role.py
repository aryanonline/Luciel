"""Arc 9 C10.b -- create luciel_app Postgres role (non-owner, RLS-fenced).

The Luciel backend has historically connected as `luciel_admin` --
the database master / table owner. Under PostgreSQL semantics, table
owners bypass RLS unless FORCE ROW LEVEL SECURITY is set. Arc 9 C10.a
adds FORCE, but the cleaner long-term posture is for the application
to connect as a non-owner role so that ownership-based escape paths
(e.g. ``ALTER TABLE ... NO FORCE`` from inside the app process,
ownership-implied privilege expansion) are unreachable.

This migration creates that role. Phase B deploy steps that go with
it (handled outside Alembic):

  1. ``python -m scripts.mint_app_db_password_ssm`` rotates a random
     32-char password and stores it at
     ``/luciel/<env>/luciel_app/password`` and a complete
     ``postgresql://luciel_app:...`` URL at
     ``/luciel/<env>/app_database_url``.
  2. The backend task-def is updated so DATABASE_URL is pulled from
     ``/luciel/<env>/app_database_url`` instead of
     ``/luciel/database-url`` (which stays bound to luciel_admin for
     migration jobs only).
  3. Migration / one-shot ops tasks continue to use the original
     ``/luciel/database-url`` (luciel_admin) -- DDL needs ownership.

Doctrine choice -- broad CRUD, narrow surface:

  The backend touches every public table that powers the product
  API. Rather than enumerate per-table grants (which drift), we
  grant SELECT/INSERT/UPDATE/DELETE on every public table at install
  time. With FORCE RLS (arc9_c10_a) in place, the role's effective
  reach is the per-tenant USING/WITH CHECK fence, not the grant
  list. This narrows attack surface to "what RLS allows this
  tenant", which is precisely the fence we built.

  Attributes:
    LOGIN: backend connects over the network from ECS.
    NOINHERIT: explicit SET ROLE required to use any future
      memberships -- defense against accidental privilege ride-along.
    NOBYPASSRLS: explicitly cannot bypass RLS. This is the WHOLE
      POINT of the role; the BYPASSRLS escape hatch lives on
      luciel_ops (arc9_c6_1) and nowhere else.
    NOCREATEDB / NOCREATEROLE / NOSUPERUSER / NOREPLICATION:
      least-privilege fail-closed posture.

  Explicitly NOT granted:
    - Schema CREATE on public (cannot create new tables/types).
    - Any privilege on pg_catalog or information_schema (granted
      by default to PUBLIC -- no extra grant needed).
    - DROP / TRUNCATE on any table -- DROP is owner-only and we
      do not grant TRUNCATE.

Reversibility:
  downgrade() revokes grants and drops the role. Operators MUST
  flip DATABASE_URL back to /luciel/database-url BEFORE running
  this downgrade or the backend will lose database connectivity.

Refs:
  ARC9_RUNBOOK §C10 (Drive corrigendum, 2026-05-25)
  arc9_c10_a_force_rls.py (companion migration)
  f392a842f885_step28_create_luciel_worker_role.py (worker analogue)
"""
from __future__ import annotations

from alembic import op


revision = "arc9_c10_b_luciel_app_role"
down_revision = "arc9_c10_a_force_rls"
branch_labels = None
depends_on = None


# Every public table the backend reads or writes. Verified
# 2026-05-25 against information_schema.tables on prod.
#
# Order is alphabetical for diff stability; Postgres does not care.
APP_CRUD_TABLES = (
    "admin_audit_logs",
    "admin_tier_overrides",
    "admin_widget_domains",
    "admins",
    "api_keys",
    "conversations",
    "deletion_logs",
    "email_send_event",
    "email_suppression",
    "identity_claims",
    "instance_composition_grants",
    "instances",
    "knowledge_embeddings",
    "knowledge_share_grants",
    "memory_items",
    "messages",
    "metering_emissions",
    "retention_policies",
    "scope_assignments",
    "sessions",
    "subscriptions",
    "traces",
    "user_consents",
    "user_invites",
    "users",
)


def upgrade() -> None:
    # 1. Create the role idempotently.
    #
    # NOBYPASSRLS is the defining attribute -- this role is the
    # primary subject of every Arc 9 RLS policy.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_roles WHERE rolname = 'luciel_app'
            ) THEN
                CREATE ROLE luciel_app WITH
                    LOGIN
                    NOBYPASSRLS
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

    # 2. Schema usage. Required for any table access.
    op.execute("GRANT USAGE ON SCHEMA public TO luciel_app;")

    # 3. CRUD grants on all app-surface tables.
    for table in APP_CRUD_TABLES:
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO luciel_app;"
        )

    # 4. Sequence usage. INSERT statements on tables with serial
    #    primary keys call nextval() on the sequence; without USAGE
    #    those INSERTs fail with "permission denied for sequence".
    op.execute(
        """
        GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public
        TO luciel_app;
        """
    )
    # And for sequences created AFTER this migration (future-proof
    # without needing a follow-up migration each time):
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
        GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO luciel_app;
        """
    )
    # Tables created in the future (typically by luciel_admin during
    # alembic upgrade) also need the SELECT/INSERT/UPDATE/DELETE
    # grant -- otherwise every new migration requires a follow-up
    # GRANT statement.
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO luciel_app;
        """
    )


def downgrade() -> None:
    # Operators: flip DATABASE_URL back to /luciel/database-url
    # BEFORE running this downgrade.
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
        REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM luciel_app;
        """
    )
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
        REVOKE USAGE, SELECT, UPDATE ON SEQUENCES FROM luciel_app;
        """
    )
    op.execute(
        """
        REVOKE USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public
        FROM luciel_app;
        """
    )
    for table in APP_CRUD_TABLES:
        op.execute(f"REVOKE ALL ON {table} FROM luciel_app;")
    op.execute("REVOKE ALL ON SCHEMA public FROM luciel_app;")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_roles WHERE rolname = 'luciel_app'
            ) THEN
                DROP ROLE luciel_app;
            END IF;
        END
        $$;
        """
    )
