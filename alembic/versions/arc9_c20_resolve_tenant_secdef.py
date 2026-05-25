"""Arc 9 C20 -- SECURITY DEFINER resolver for cookied tenant discovery.

Background (2026-05-25, Phase A.5 Free signup demo continuation)
================================================================

The first Free-tier user to complete signup + login on prod (demo6)
hit a 401 "No active subscription for this user." when clicking
*Create Luciel*. Root cause is deeper than the C20 middleware
shim alone: the cookied login path has a chicken-and-egg with
RLS on ``scope_assignments``.

  1. ``arc9_c10_a_force_rls`` flipped ``FORCE ROW LEVEL SECURITY``
     on every tenant-scoped table including ``scope_assignments``.
  2. ``arc9_c3_2g_rls_scope_assignments`` + ``arc9_c14_add_tenant_permissive``
     installed RLS policies that gate visibility on
     ``(tenant_id)::text = current_setting('app.admin_id', true)``.
  3. At cookied LOGIN time the User has just authenticated by email +
     password; the app does NOT yet know which tenant they belong to
     (this lookup is the whole point of ``_resolve_tenant_for_user``).
     So ``app.admin_id`` is empty. RLS reports 0 visible rows.
  4. Same blocker applies on every subsequent cookied request, where
     ``SessionCookieAuthMiddleware`` needs to re-resolve tenant from
     the user_id in the cookie payload.

This is a foundational identity-layer bug. The luciel_app role has
all SELECT/INSERT/UPDATE/DELETE grants on scope_assignments, but
FORCE RLS makes them invisible until app.admin_id is set, and
app.admin_id cannot be set until we read the row.

The other escape hatches don't work here:
  * SECURITY DEFINER owned by luciel_admin: luciel_admin owns the
    table but FORCE RLS binds the owner too, so this provides no
    extra visibility.
  * SET LOCAL row_security = off: requires the executing role to be
    table owner AND no FORCE -- FORCE blocks it.
  * BYPASSRLS ops session: luciel_ops has BYPASSRLS but no grants on
    scope_assignments and no ops_database_url is provisioned on the
    web task today; standing up that channel widens the
    web -> BYPASSRLS attack surface unnecessarily.

The right tool is a tightly-scoped SECURITY DEFINER SQL function
owned by ``luciel_ops`` (the BYPASSRLS role). Functions invoked
via SECURITY DEFINER execute with the OWNER's privileges, which
means RLS is bypassed for the duration of the call only. The
function returns ONLY the tenant_id of the user's owner-role
ScopeAssignment -- no other columns, no other tables, no
unbounded scans. The web role gains exactly one capability:
"given a user_id I already have in my session cookie, tell me
which tenant they own." This is the minimum information needed
to set app.admin_id for the rest of the request.

Function contract
-----------------

Name:     public.arc9_c20_resolve_tenant_for_user(p_user_id uuid)
Returns:  varchar (Admin.id semantic key, or '' if no owner scope)
Owner:    luciel_ops (BYPASSRLS)
Security: SECURITY DEFINER
Volatility: STABLE (no writes, no side effects, deterministic per snapshot)
Grants:   EXECUTE to luciel_app (revoke from PUBLIC for defence in depth)

The function filters on ``active = true AND ended_at IS NULL`` and
prefers ``role = 'owner'`` (ORDER BY role='owner' DESC) so an owner's
tenant_id is returned even when other (non-owner) scopes exist. If
no owner scope exists, the most-recently-started active assignment
wins -- matching the priority logic in
``app/api/v1/auth.py::_resolve_tenant_for_user`` so the in-app
resolver and the SQL function agree on the same answer for every
user, including teammates redeeming invites.

Audit / observability
---------------------

The function does NOT write admin_audit_logs. Callers (the auth
resolver and middleware) are responsible for audit attribution
the same way they already are for the normal ScopeAssignment ORM
path. This is a discovery read; the cookied request that follows
will write its own audit rows under the resolved tenant_id.

Rollback safety
---------------

``alembic downgrade -1`` drops the function. Callers (added in
the C20 code change) fall back to direct ORM reads which are
RLS-blocked -- so rolling back this migration WITHOUT also
rolling the application image leaves Free cookied logins broken
again. The doctrine comment in
``app/api/v1/auth.py::_resolve_tenant_for_user`` documents this
coupling. Use the C12-C20 corrigendum doc for the joint rollback
sequence.
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "arc9_c20_resolve_tenant_secdef"
down_revision = "arc9_c17_instances_system_prompt"
branch_labels = None
depends_on = None


_CREATE_FN_SQL = """
CREATE OR REPLACE FUNCTION public.arc9_c20_resolve_tenant_for_user(
    p_user_id uuid
)
RETURNS varchar
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT tenant_id
    FROM public.scope_assignments
    WHERE user_id = p_user_id
      AND active = true
      AND ended_at IS NULL
    ORDER BY (role = 'owner') DESC, started_at DESC
    LIMIT 1
$$;
"""


_DROP_FN_SQL = """
DROP FUNCTION IF EXISTS public.arc9_c20_resolve_tenant_for_user(uuid);
"""


def upgrade() -> None:
    # Step 1: create the function with the current role (luciel_admin)
    # as the temporary owner. We immediately re-assign ownership.
    op.execute(_CREATE_FN_SQL)

    # Step 2: re-assign ownership to luciel_ops (BYPASSRLS). The
    # migration role (luciel_admin) is a member of rds_superuser so
    # this ALTER is permitted on RDS.
    op.execute(
        "ALTER FUNCTION public.arc9_c20_resolve_tenant_for_user(uuid) "
        "OWNER TO luciel_ops"
    )

    # Step 3: lock the grant matrix. Default in PG is EXECUTE to
    # PUBLIC for newly created functions -- revoke that and grant
    # EXECUTE only to luciel_app. luciel_worker / luciel_ops can
    # still run it because luciel_ops is the owner.
    op.execute(
        "REVOKE EXECUTE ON FUNCTION "
        "public.arc9_c20_resolve_tenant_for_user(uuid) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION "
        "public.arc9_c20_resolve_tenant_for_user(uuid) TO luciel_app"
    )


def downgrade() -> None:
    op.execute(_DROP_FN_SQL)
