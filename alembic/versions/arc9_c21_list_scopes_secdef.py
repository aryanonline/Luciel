"""Arc 9 C21 -- SETOF SECURITY DEFINER resolver for pre-tenant-context list_for_user.

Background (2026-05-25, Phase A.5 continuation of C20)
======================================================

C20 fixed the cookied LOGIN path -- the user's tenant can be resolved
from their UUID before app.admin_id is set, via a single-row SECDEF
function. That fix unblocked auth resolution and the
SessionCookieAuthMiddleware free-tier fallback.

C20 did NOT fix every other call site that needs the user's scope
assignments BEFORE the tenant GUC is set. Concretely, on the first
request after a cookied login the frontend calls /billing/me, which
runs:

    sar = ScopeAssignmentRepository(db)
    active = sar.list_for_user(user.id, active_only=True)

That direct ORM query is gated by RLS on scope_assignments. With
``app.admin_id`` empty -- which it is on every "discovery" read where
we are precisely trying to learn the tenant -- the query returns []
silently. /billing/me then responds with ``tenant_id=""``, the
frontend renders the dashboard with an empty tenant id, the next
call to ``GET /admin/luciel-instances?tenant_id=`` hits the route
without a query match and the response is 405, and the subsequent
``POST /admin/luciel-instances`` with ``scope_owner_tenant_id=""``
fails Pydantic ``min_length=2`` for admin_id and returns 422.

list_for_user is also called pre-tenant-context from:

* app/services/user_service.py:327  -- audit_role_change resolver
* app/api/v1/billing.py:629         -- GET /me   (the demo bug above)
* app/api/v1/billing.py:799         -- POST /upgrade
* app/api/v1/billing.py:902         -- POST /downgrade
* app/api/v1/admin.py:1061          -- invite acceptance path

Every one of these returns [] under FORCE RLS when ``app.admin_id``
is unset. This is the same class of identity-layer bug as C20: the
app needs to *discover* which tenants a freshly-authenticated user
belongs to before it can SET app.admin_id, but FORCE RLS blocks the
discovery.

Function contract
-----------------

Name:     public.arc9_c21_list_scopes_for_user(p_user_id uuid, p_active_only boolean)
Returns:  SETOF (id uuid, user_id uuid, tenant_id varchar, domain_id varchar,
                 role varchar, started_at timestamptz, ended_at timestamptz,
                 ended_reason varchar, ended_note text,
                 ended_by_api_key_id integer, active boolean)
Owner:    luciel_ops (BYPASSRLS)
Security: SECURITY DEFINER
Volatility: STABLE
Grants:   EXECUTE to luciel_app; REVOKE from PUBLIC

Returns ScopeAssignment rows for one user, optionally filtered to
active rows, ordered by started_at ASC -- identical row shape and
ordering to ScopeAssignmentRepository.list_for_user(). The
repository will fall through to this function when ``app.admin_id``
is empty (i.e. discovery mode); when the GUC is set the existing
ORM path runs and RLS works normally.

Doctrine:
    * Returns ONLY scope_assignments rows for ONE user_id. No
      cross-user enumeration, no joins to users/admins. The escape
      hatch is as narrow as it can be while still serving every
      discovery caller.
    * STABLE + read-only -- the function never writes.
    * No new role grant matrix changes -- C20 already gave
      luciel_ops the SELECT grant on scope_assignments which is
      everything we need here too.

Rollback safety
---------------

``alembic downgrade -1`` drops only this function. C20 stays in
place. Callers in app/repositories/scope_assignment_repository.py
fall back to the direct ORM query, which is RLS-blocked under
discovery conditions -- so rolling back this migration WITHOUT
rolling the application image will break /billing/me on the first
cookied request after login (and the four other call sites). The
joint rollback sequence is documented in the C12-C21 corrigendum.
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "arc9_c21_list_scopes_secdef"
down_revision = "arc9_c20_resolve_tenant_secdef"
branch_labels = None
depends_on = None


_CREATE_FN_SQL = """
CREATE OR REPLACE FUNCTION public.arc9_c21_list_scopes_for_user(
    p_user_id uuid,
    p_active_only boolean
)
RETURNS TABLE (
    id uuid,
    user_id uuid,
    tenant_id varchar,
    domain_id varchar,
    role varchar,
    started_at timestamptz,
    ended_at timestamptz,
    ended_reason varchar,
    ended_note text,
    ended_by_api_key_id integer,
    active boolean
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
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
      AND (
          p_active_only = false
          OR (sa.active = true AND sa.ended_at IS NULL)
      )
    ORDER BY sa.started_at ASC
$$;
"""


_DROP_FN_SQL = """
DROP FUNCTION IF EXISTS public.arc9_c21_list_scopes_for_user(uuid, boolean);
"""


def upgrade() -> None:
    # luciel_ops already has SELECT on scope_assignments from C20.
    # Create the function (under the migration role) and re-assign
    # ownership to luciel_ops so SECURITY DEFINER bypasses RLS for
    # the duration of the call.
    op.execute(_CREATE_FN_SQL)
    op.execute(
        "ALTER FUNCTION "
        "public.arc9_c21_list_scopes_for_user(uuid, boolean) "
        "OWNER TO luciel_ops"
    )
    # Lock the grant matrix: revoke from PUBLIC, grant only to
    # luciel_app. luciel_worker / luciel_ops can still run it
    # because luciel_ops is the owner.
    op.execute(
        "REVOKE EXECUTE ON FUNCTION "
        "public.arc9_c21_list_scopes_for_user(uuid, boolean) "
        "FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "public.arc9_c21_list_scopes_for_user(uuid, boolean) "
        "TO luciel_app"
    )


def downgrade() -> None:
    op.execute(_DROP_FN_SQL)
