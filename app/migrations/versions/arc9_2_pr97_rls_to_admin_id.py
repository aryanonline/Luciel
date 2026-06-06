"""Arc 9.2 PR #97 — Rewrite Wall-1 RLS policies from tenant_id to admin_id.

Option A step 2.

PR #96 added an additive ``admin_id`` column on 15 tables, backfilled from
``tenant_id``.  This migration rewrites every Wall-1 RLS policy that
currently filters on ``tenant_id`` so it filters on ``admin_id`` instead.
The two columns hold the same value during the alias window (the
``before_insert`` dual-write installed in PR #96 keeps them in lock-step),
so the rewrite is a no-op from a security standpoint -- it just shifts
which column the RLS engine references.

Why do it now (before PR #98 + PR #101)?
  PR #101 will DROP ``tenant_id``.  At that moment, every policy that
  still references ``tenant_id::text`` would error with "column does not
  exist".  Doing the rewrite here, while both columns are valid, lets us
  reach a state where (a) the dual-write keeps ``admin_id`` populated,
  (b) the RLS engine reads ``admin_id``, and (c) PR #101's column drop
  becomes a clean operation with no policy work in the same transaction.

Policy inventory (rebuilt for every table)
-------------------------------------------
Wall-1 RESTRICTIVE  ``<table>_tenant_isolation``       (11 tables)
Wall-1 PERMISSIVE   ``<table>_tenant_permissive``      (6 tables)
Nullable-tenant     ``<table>_tenant_isolation``       (4 tables)

Note on naming: the policy names retain ``_tenant_`` in this PR because
renaming live policies risks transient gaps under FORCE RLS.  A purely
cosmetic rename to ``_admin_`` can happen post-PR-101 if desired; the
behaviour is unchanged either way.

Revision ID: arc9_2_pr97_rls_to_admin_id
Revises: arc9_2_pr96_additive_admin_id
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = "arc9_2_pr97_rls_to_admin_id"
down_revision = "arc9_2_pr96_additive_admin_id"
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------------
# Manifests.  Each tuple is (table, current_fk_column).  The current FK is
# always ``tenant_id`` because if it were already ``admin_id`` this PR would
# have nothing to do for that table.  We pass it explicitly so the DROP
# statements match the on-disk state and the new policy is built against
# ``admin_id`` uniformly.
# ---------------------------------------------------------------------------

# Tables that today have a RESTRICTIVE tenant policy (arc9_c11 + messages
# via the C5.1 split).  Instances + admin_widget_domains are intentionally
# absent here -- their policies already filter on admin_id.
RESTRICTIVE_TENANT_TABLES: tuple[str, ...] = (
    "admin_audit_logs",
    "traces",
    "memory_items",
    "conversations",
    "sessions",
    "subscriptions",
    "scope_assignments",
    "user_invites",
    "user_consents",
    "identity_claims",
    "messages",
)

# Tables that ALSO carry a paired PERMISSIVE tenant policy (arc9_c14
# default-deny rescue).
PERMISSIVE_TENANT_TABLES: tuple[str, ...] = (
    "conversations",
    "subscriptions",
    "scope_assignments",
    "user_invites",
    "user_consents",
    "identity_claims",
)

# Nullable-tenant tables that have a single PERMISSIVE policy with an
# "IS NULL" platform escape hatch.  These have richer predicates, so they
# get bespoke SQL.
NULLABLE_TENANT_TABLES: tuple[str, ...] = (
    "api_keys",
    "knowledge_embeddings",
    "retention_policies",
    "deletion_logs",
)


# ---------------------------------------------------------------------------
# Policy DDL.  Each block: DROP IF EXISTS the old policy, then CREATE the
# admin_id-flavoured replacement.  Idempotent.
# ---------------------------------------------------------------------------

def _restrictive_admin(table: str) -> str:
    return f"""
        DROP POLICY IF EXISTS {table}_tenant_isolation ON {table};
        CREATE POLICY {table}_tenant_isolation
        ON {table}
        AS RESTRICTIVE
        FOR ALL
        TO PUBLIC
        USING (admin_id::text = current_setting('app.admin_id', true))
        WITH CHECK (admin_id::text = current_setting('app.admin_id', true));
    """


def _permissive_admin(table: str) -> str:
    return f"""
        DROP POLICY IF EXISTS {table}_tenant_permissive ON {table};
        CREATE POLICY {table}_tenant_permissive
        ON {table}
        AS PERMISSIVE
        FOR ALL
        TO PUBLIC
        USING (admin_id::text = current_setting('app.admin_id', true))
        WITH CHECK (admin_id::text = current_setting('app.admin_id', true));
    """


# Each nullable table has its own original predicate; replacements below
# keep the same shape but read admin_id instead of tenant_id.

_NULLABLE_DDL = {
    "api_keys": """
        DROP POLICY IF EXISTS tenant_isolation ON api_keys;
        CREATE POLICY tenant_isolation ON api_keys
            FOR ALL
            USING (TRUE)
            WITH CHECK (
                (admin_id IS NULL
                 AND current_setting('app.admin_id', true) = 'platform')
                OR admin_id::text = current_setting('app.admin_id', true)
            );
    """,
    "knowledge_embeddings": """
        DROP POLICY IF EXISTS knowledge_embeddings_tenant_isolation
            ON knowledge_embeddings;
        CREATE POLICY knowledge_embeddings_tenant_isolation
        ON knowledge_embeddings
        AS PERMISSIVE
        FOR ALL
        TO PUBLIC
        USING (
            admin_id IS NULL
            OR admin_id::text = current_setting('app.admin_id', true)
        )
        WITH CHECK (
            (
                admin_id IS NULL
                AND current_setting('app.admin_id', true) = 'platform'
            )
            OR admin_id::text = current_setting('app.admin_id', true)
        );
    """,
    "retention_policies": """
        DROP POLICY IF EXISTS retention_policies_tenant_isolation
            ON retention_policies;
        CREATE POLICY retention_policies_tenant_isolation
        ON retention_policies
        AS PERMISSIVE
        FOR ALL
        TO PUBLIC
        USING (
            admin_id IS NULL
            OR admin_id::text = current_setting('app.admin_id', true)
        )
        WITH CHECK (
            (admin_id IS NULL
             AND current_setting('app.admin_id', true) = 'platform')
            OR admin_id::text = current_setting('app.admin_id', true)
        );
    """,
    "deletion_logs": """
        DROP POLICY IF EXISTS deletion_logs_tenant_isolation
            ON deletion_logs;
        CREATE POLICY deletion_logs_tenant_isolation
        ON deletion_logs
        AS PERMISSIVE
        FOR ALL
        TO PUBLIC
        USING (
            admin_id IS NULL
            OR admin_id::text = current_setting('app.admin_id', true)
        )
        WITH CHECK (
            (admin_id IS NULL
             AND current_setting('app.admin_id', true) = 'platform')
            OR admin_id::text = current_setting('app.admin_id', true)
        );
    """,
}


# Reverse direction: same policies but reading tenant_id again.
def _restrictive_tenant(table: str) -> str:
    return f"""
        DROP POLICY IF EXISTS {table}_tenant_isolation ON {table};
        CREATE POLICY {table}_tenant_isolation
        ON {table}
        AS RESTRICTIVE
        FOR ALL
        TO PUBLIC
        USING (tenant_id::text = current_setting('app.admin_id', true))
        WITH CHECK (tenant_id::text = current_setting('app.admin_id', true));
    """


def _permissive_tenant(table: str) -> str:
    return f"""
        DROP POLICY IF EXISTS {table}_tenant_permissive ON {table};
        CREATE POLICY {table}_tenant_permissive
        ON {table}
        AS PERMISSIVE
        FOR ALL
        TO PUBLIC
        USING (tenant_id::text = current_setting('app.admin_id', true))
        WITH CHECK (tenant_id::text = current_setting('app.admin_id', true));
    """


_NULLABLE_DDL_DOWN = {
    "api_keys": """
        DROP POLICY IF EXISTS tenant_isolation ON api_keys;
        CREATE POLICY tenant_isolation ON api_keys
            FOR ALL
            USING (TRUE)
            WITH CHECK (
                (tenant_id IS NULL
                 AND current_setting('app.admin_id', true) = 'platform')
                OR tenant_id = current_setting('app.admin_id', true)
            );
    """,
    "knowledge_embeddings": """
        DROP POLICY IF EXISTS knowledge_embeddings_tenant_isolation
            ON knowledge_embeddings;
        CREATE POLICY knowledge_embeddings_tenant_isolation
        ON knowledge_embeddings
        AS PERMISSIVE
        FOR ALL
        TO PUBLIC
        USING (
            tenant_id IS NULL
            OR tenant_id = current_setting('app.admin_id', true)
        )
        WITH CHECK (
            (
                tenant_id IS NULL
                AND current_setting('app.admin_id', true) = 'platform'
            )
            OR tenant_id = current_setting('app.admin_id', true)
        );
    """,
    "retention_policies": """
        DROP POLICY IF EXISTS retention_policies_tenant_isolation
            ON retention_policies;
        CREATE POLICY retention_policies_tenant_isolation
        ON retention_policies
        AS PERMISSIVE
        FOR ALL
        TO PUBLIC
        USING (
            tenant_id IS NULL
            OR tenant_id = current_setting('app.admin_id', true)
        )
        WITH CHECK (
            (tenant_id IS NULL
             AND current_setting('app.admin_id', true) = 'platform')
            OR tenant_id = current_setting('app.admin_id', true)
        );
    """,
    "deletion_logs": """
        DROP POLICY IF EXISTS deletion_logs_tenant_isolation
            ON deletion_logs;
        CREATE POLICY deletion_logs_tenant_isolation
        ON deletion_logs
        AS PERMISSIVE
        FOR ALL
        TO PUBLIC
        USING (
            tenant_id IS NULL
            OR tenant_id = current_setting('app.admin_id', true)
        )
        WITH CHECK (
            (tenant_id IS NULL
             AND current_setting('app.admin_id', true) = 'platform')
            OR tenant_id = current_setting('app.admin_id', true)
        );
    """,
}


def upgrade() -> None:
    for table in RESTRICTIVE_TENANT_TABLES:
        op.execute(_restrictive_admin(table))

    for table in PERMISSIVE_TENANT_TABLES:
        op.execute(_permissive_admin(table))

    for table in NULLABLE_TENANT_TABLES:
        op.execute(_NULLABLE_DDL[table])


def downgrade() -> None:
    for table in NULLABLE_TENANT_TABLES:
        op.execute(_NULLABLE_DDL_DOWN[table])

    for table in PERMISSIVE_TENANT_TABLES:
        op.execute(_permissive_tenant(table))

    for table in RESTRICTIVE_TENANT_TABLES:
        op.execute(_restrictive_tenant(table))
