"""Arc 9 C22 -- Consolidated identity-bootstrap SECDEF.

Background (2026-05-25, Phase A.5 vision-alignment cleanup)
===========================================================

C20 and C21 added narrow SECURITY DEFINER escape hatches for two
specific discovery reads:

  * arc9_c20_resolve_tenant_for_user(uuid) -> varchar
    Single tenant_id, used by auth resolver + middleware.
  * arc9_c21_list_scopes_for_user(uuid, boolean) -> SETOF ...
    All scope rows for a user, used by list_for_user.

Each of those was correct, but together they document a pattern: any
caller that needs to read ``scope_assignments`` BEFORE ``app.admin_id``
has been set has to either (a) call its own ad-hoc SECDEF function or
(b) silently get ``[]`` from FORCE RLS and produce a downstream bug.

C22 collapses that pattern into ONE bootstrap function whose payload
carries enough information to drive every discovery caller:

  * the canonical tenant_id (owner-first, then most-recent-active);
    same priority as C20.
  * the full ordered list of active ScopeAssignment rows for the
    user; same shape and ordering as C21.
  * the user's tier (sourced from the canonical Admin row tied to
    the canonical tenant_id) so the V2 tier-enforcement guard can
    operate on Free users who have no Subscription row.

The single-shot payload means every authenticated cookied request
performs at most ONE pre-RLS read. Once that's done the request sets
``app.admin_id`` via the existing ContextVar -> after_begin listener
pipe and every subsequent query runs under normal FORCE RLS.

Function contract
-----------------

Name:     public.arc9_c22_bootstrap_identity(p_user_id uuid)
Returns:  TABLE (
              canonical_tenant_id   varchar,   -- '' if no scope
              canonical_tier        varchar,   -- '' if no scope
              scope_assignment_id   uuid,
              tenant_id             varchar,
              domain_id             varchar,
              role                  varchar,
              started_at            timestamptz,
              ended_at              timestamptz,
              ended_reason          varchar,
              ended_note            text,
              ended_by_api_key_id   integer,
              active                boolean
          )
Owner:    luciel_ops (BYPASSRLS)
Security: SECURITY DEFINER
Volatility: STABLE
Grants:   EXECUTE to luciel_app; REVOKE from PUBLIC

Returns one row PER active scope_assignment the user holds. The two
header columns (canonical_tenant_id, canonical_tier) repeat on every
row -- callers pluck them from row[0] and iterate the rest as the
scope list. Empty result = user has no active scope at all (genuine
zero-state, not RLS-blocked).

Doctrine: C20 + C21 functions are kept in place this release for
safety; new code MUST use C22. A future migration will drop the
C20/C21 functions after one stable release.

Rollback safety
---------------

``alembic downgrade -1`` drops the C22 function. C20 + C21 are still
on disk; callers that have not yet been migrated to C22 keep working
unchanged. Joint rollback sequence: revert the application image to
arc9-c21-* THEN downgrade the migration; do NOT downgrade first or
the application loses access to the function while still referencing
it.
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "arc9_c22_identity_bootstrap"
down_revision = "arc9_c21_list_scopes_secdef"
branch_labels = None
depends_on = None


_CREATE_FN_SQL = """
CREATE OR REPLACE FUNCTION public.arc9_c22_bootstrap_identity(
    p_user_id uuid
)
RETURNS TABLE (
    canonical_tenant_id varchar,
    canonical_tier      varchar,
    scope_assignment_id uuid,
    tenant_id           varchar,
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
            sa.tenant_id,
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
        SELECT tenant_id
        FROM active_scopes
        ORDER BY (role = 'owner') DESC, started_at DESC
        LIMIT 1
    ),
    canonical_with_tier AS (
        SELECT
            c.tenant_id AS tid,
            COALESCE(a.tier, '')::varchar AS tier
        FROM canonical c
        LEFT JOIN public.admins a ON a.id = c.tenant_id
    )
    SELECT
        COALESCE((SELECT tid  FROM canonical_with_tier), '')::varchar,
        COALESCE((SELECT tier FROM canonical_with_tier), '')::varchar,
        s.id,
        s.tenant_id,
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


_DROP_FN_SQL = """
DROP FUNCTION IF EXISTS public.arc9_c22_bootstrap_identity(uuid);
"""


def upgrade() -> None:
    # luciel_ops already has SELECT on scope_assignments from C20.
    # We also need SELECT on admins for the tier join. Add it here
    # (idempotent — re-running this against a hand-fixed prod is safe).
    op.execute("GRANT SELECT ON public.admins TO luciel_ops")

    op.execute(_CREATE_FN_SQL)
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


def downgrade() -> None:
    op.execute(_DROP_FN_SQL)
    op.execute("REVOKE SELECT ON public.admins FROM luciel_ops")
