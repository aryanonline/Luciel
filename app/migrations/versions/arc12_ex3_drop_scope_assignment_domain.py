"""Arc 12 EX3 — drop scope_assignments.domain_id (NOT NULL legacy column).

Revision ID: arc12_ex3_drop_scope_assignment_domain
Revises: arc12_ex3_drop_user_invite_domain
Create Date: 2026-05-29

Single-table cleanup: removes the legacy ``domain_id`` String(100) NOT
NULL column from ``scope_assignments``. v2 scopes a role binding by
``(user_id, admin_id, role)`` with optional instance-level operator
scoping handled at the policy layer (``ScopePolicy._resolve_role_on_instance``
keys on ``admin_id`` + ``instance.id`` + ``role``). The domain half is
residue from the pre-Arc-12 (tenant_id, domain_id) three-layer scaffold.

Cross-refs: Architecture §3.7.2 (Wall-2 cross-team role+scope primitive);
ARC11_PLAN.md §0.6 (role matrix); D-arc12-ex3-residual-domain-column-map.

Constraint / index decisions
----------------------------

Pre-state (after the Step 24.5b create migration
``3ad39f9e6b55_add_users_scope_assignments_and_agent_`` + the Arc 9.2
PR #101 tenant_id strip):

  * ``ix_scope_assignments_user_id_active`` — partial INDEX on
    ``(user_id, active) WHERE ended_at IS NULL``. Does NOT reference
    ``domain_id``; leave untouched.

  * ``ix_scope_assignments_tenant_id_active`` — partial INDEX on
    ``(admin_id, active) WHERE ended_at IS NULL`` (renamed in spirit
    to ``admin_id`` by PR #101's column drop; index name preserved
    for migration symmetry). Does NOT reference ``domain_id``; leave
    untouched.

  * ``ix_scope_assignments_user_tenant_domain_role_active`` — partial
    INDEX on ``(user_id, admin_id, domain_id, role) WHERE ended_at
    IS NULL``. This is the load-bearing duplicate-assignment guard
    used by the promotion path ("is this user currently assigned to
    this (admin, role)?" — repo
    ``get_active_for_user_in_tenant``). Drop and RE-CREATE on
    ``(user_id, admin_id, role) WHERE ended_at IS NULL`` — the v2
    natural key. Duplicate-assignment protection MUST survive this
    migration; only the scope shape narrows. The index name is
    preserved so existing operator runbooks keep working.

No UNIQUE constraint, CHECK constraint, or FK references
``scope_assignments.domain_id`` in any prior migration; only the
partial index above. EX2 already swept any RLS policy that mentioned
``domain_id``. ``IF EXISTS`` guards every drop so the migration is
idempotent.

arc9_c22 bootstrap SECDEF function
----------------------------------

``public.arc9_c22_bootstrap_identity(uuid)`` (Arc 9.2 PR #101 form)
returns a row shape that includes ``domain_id varchar`` and SELECTs
``sa.domain_id`` from ``scope_assignments``. Dropping the column would
silently break the function. We ``CREATE OR REPLACE`` the function
without ``domain_id`` in either the RETURNS shape or the SELECT list.
The Python caller (``app/identity/bootstrap.py``) is updated to the
narrower select-list in the same commit.

Downgrade
---------

Re-adds ``domain_id`` as NULLABLE (downgrade does not restore the
original NOT NULL — there is no backfill source for the dropped
values). Drops the v2 narrow form of
``ix_scope_assignments_user_tenant_domain_role_active`` and
re-creates the original (user_id, admin_id, domain_id, role) form.
Restores the wide arc9_c22 function shape.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "arc12_ex3_drop_scope_assignment_domain"
down_revision = "arc12_ex3_drop_user_invite_domain"
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------
# arc9_c22 bootstrap SECDEF — v2 (post-domain-drop) form
# ---------------------------------------------------------------------
#
# Identical to the Arc 9.2 PR #101 form except ``domain_id`` is removed
# from the RETURNS TABLE shape and from both SELECT lists in the body.
# Ownership / grants / SET search_path are unchanged.
_CREATE_FN_V2_SQL = """
CREATE OR REPLACE FUNCTION public.arc9_c22_bootstrap_identity(
    p_user_id uuid
)
RETURNS TABLE (
    canonical_tenant_id varchar,
    canonical_tier      varchar,
    scope_assignment_id uuid,
    admin_id            varchar,
    role                varchar,
    started_at          timestamptz,
    ended_at            timestamptz,
    ended_reason        varchar,
    ended_note          text,
    ended_by_api_key_id integer,
    active              boolean
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    WITH active_scopes AS (
        SELECT
            sa.id,
            sa.user_id,
            sa.admin_id,
            sa.role,
            sa.started_at,
            sa.ended_at,
            sa.ended_reason,
            sa.ended_note,
            sa.ended_by_api_key_id,
            sa.active
        FROM public.scope_assignments AS sa
        WHERE sa.user_id = p_user_id
          AND sa.active = true
          AND sa.ended_at IS NULL
    ),
    canonical AS (
        SELECT admin_id
        FROM active_scopes
        ORDER BY (role = 'admin_owner') DESC, started_at DESC
        LIMIT 1
    ),
    canonical_with_tier AS (
        SELECT
            c.admin_id AS aid,
            COALESCE(a.tier, '')::varchar AS tier
        FROM canonical c
        LEFT JOIN public.admins a ON a.id = c.admin_id
    )
    SELECT
        COALESCE((SELECT aid  FROM canonical_with_tier), '')::varchar,
        COALESCE((SELECT tier FROM canonical_with_tier), '')::varchar,
        s.id,
        s.admin_id,
        s.role,
        s.started_at,
        s.ended_at,
        s.ended_reason,
        s.ended_note,
        s.ended_by_api_key_id,
        s.active
    FROM active_scopes s
    ORDER BY s.started_at ASC
$$;
"""


# Wide (pre-drop) form for downgrade: matches the Arc 9.2 PR #101
# definition exactly so the function returns the original column set
# if the migration is reversed.
_CREATE_FN_V1_SQL = """
CREATE OR REPLACE FUNCTION public.arc9_c22_bootstrap_identity(
    p_user_id uuid
)
RETURNS TABLE (
    canonical_tenant_id varchar,
    canonical_tier      varchar,
    scope_assignment_id uuid,
    admin_id            varchar,
    domain_id           varchar,
    role                varchar,
    started_at          timestamptz,
    ended_at            timestamptz,
    ended_reason        varchar,
    ended_note          text,
    ended_by_api_key_id integer,
    active              boolean
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    WITH active_scopes AS (
        SELECT
            sa.id,
            sa.user_id,
            sa.admin_id,
            sa.domain_id,
            sa.role,
            sa.started_at,
            sa.ended_at,
            sa.ended_reason,
            sa.ended_note,
            sa.ended_by_api_key_id,
            sa.active
        FROM public.scope_assignments AS sa
        WHERE sa.user_id = p_user_id
          AND sa.active = true
          AND sa.ended_at IS NULL
    ),
    canonical AS (
        SELECT admin_id
        FROM active_scopes
        ORDER BY (role = 'admin_owner') DESC, started_at DESC
        LIMIT 1
    ),
    canonical_with_tier AS (
        SELECT
            c.admin_id AS aid,
            COALESCE(a.tier, '')::varchar AS tier
        FROM canonical c
        LEFT JOIN public.admins a ON a.id = c.admin_id
    )
    SELECT
        COALESCE((SELECT aid  FROM canonical_with_tier), '')::varchar,
        COALESCE((SELECT tier FROM canonical_with_tier), '')::varchar,
        s.id,
        s.admin_id,
        s.domain_id,
        s.role,
        s.started_at,
        s.ended_at,
        s.ended_reason,
        s.ended_note,
        s.ended_by_api_key_id,
        s.active
    FROM active_scopes s
    ORDER BY s.started_at ASC
$$;
"""


def _reapply_fn_grants() -> None:
    """Preserve the original owner + grant matrix after CREATE OR REPLACE.

    CREATE OR REPLACE preserves owner and ACLs in modern Postgres but
    the signature changes (RETURNS TABLE columns differ) — when that
    happens we DROP + CREATE rather than REPLACE, which loses ACL. We
    explicitly re-apply the grant matrix to keep the migration
    re-runnable against a hand-fixed prod.
    """
    op.execute(
        "ALTER FUNCTION public.arc9_c22_bootstrap_identity(uuid) "
        "OWNER TO luciel_ops"
    )
    op.execute(
        "REVOKE EXECUTE ON FUNCTION "
        "public.arc9_c22_bootstrap_identity(uuid) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "public.arc9_c22_bootstrap_identity(uuid) TO luciel_app"
    )


def upgrade() -> None:
    # 1. Redefine the arc9_c22 SECDEF function to the v2 shape FIRST.
    #    A function whose body SELECTs sa.domain_id will refuse to be
    #    replaced if domain_id has already been dropped (Postgres
    #    validates the new body, not the old). Replacing first avoids
    #    any in-flight reader getting a "column does not exist" error
    #    on the cached plan after the column drop. Signature changes
    #    (RETURNS TABLE shape narrows by one column) force a
    #    DROP+CREATE; reapply the grant matrix.
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "public.arc9_c22_bootstrap_identity(uuid) CASCADE"
    )
    op.execute(_CREATE_FN_V2_SQL)
    _reapply_fn_grants()

    # 2. Drop the load-bearing duplicate-assignment partial index that
    #    spans (user_id, admin_id, domain_id, role). RE-CREATE below
    #    on the v2 shape so the duplicate guard survives.
    op.execute(
        "DROP INDEX IF EXISTS "
        "public.ix_scope_assignments_user_tenant_domain_role_active"
    )

    # 3. Defensive: drop any other index that might reference
    #    domain_id (none in the create migration, but environments
    #    may carry hand-added ones). IF EXISTS keeps the migration
    #    idempotent.
    op.execute(
        "DROP INDEX IF EXISTS public.ix_scope_assignments_domain_id"
    )

    # 4. RE-CREATE the duplicate-assignment guard on the v2 shape
    #    (user_id, admin_id, role) WHERE ended_at IS NULL. Same name
    #    so existing runbooks / observability dashboards keep working.
    op.execute(
        "CREATE INDEX ix_scope_assignments_user_tenant_domain_role_active "
        "ON public.scope_assignments "
        "(user_id, admin_id, role) "
        "WHERE ended_at IS NULL"
    )

    # 5. Finally drop the column. CASCADE is unnecessary — every
    #    dependent index was removed above and the SECDEF function
    #    no longer references the column.
    op.drop_column("scope_assignments", "domain_id")


def downgrade() -> None:
    # 1. Re-add the column as NULLABLE. The original column was NOT
    #    NULL but no backfill source exists post-drop; downgrade is a
    #    structural restore only.
    op.add_column(
        "scope_assignments",
        sa.Column(
            "domain_id",
            sa.String(length=100),
            nullable=True,
        ),
    )

    # 2. Drop the v2 narrow form so the original wide form can be
    #    recreated under the same index name.
    op.execute(
        "DROP INDEX IF EXISTS "
        "public.ix_scope_assignments_user_tenant_domain_role_active"
    )

    # 3. Recreate the original wide partial index on
    #    (user_id, admin_id, domain_id, role) WHERE ended_at IS NULL.
    op.execute(
        "CREATE INDEX ix_scope_assignments_user_tenant_domain_role_active "
        "ON public.scope_assignments "
        "(user_id, admin_id, domain_id, role) "
        "WHERE ended_at IS NULL"
    )

    # 4. Restore the wide arc9_c22 function shape (with domain_id).
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "public.arc9_c22_bootstrap_identity(uuid) CASCADE"
    )
    op.execute(_CREATE_FN_V1_SQL)
    _reapply_fn_grants()
