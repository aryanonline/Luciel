"""Arc 9 C4.3d -- Wall 3 RLS policy on sessions (luciel_instance_id).

Continues the Wall 3 Layer 2 rollout from C4.1/C4.2. C4.1 introduced
the instance_id ContextVar and the engine-level after_begin listener
that emits ``SELECT set_config('app.instance_id', '<int|empty>', true)``
on every BEGIN. C4.2 wired ``get_tenant_scoped_db`` to bind the
ContextVar from ``request.state.luciel_instance_id``. This migration
materialises Wall 3's Layer 2 (PostgreSQL RLS) on sessions.

Policy shape (NULL-permissive asymmetric, mirroring C3.3 doctrine):

  USING (read-side):
    ``luciel_instance_id::text = current_setting('app.instance_id', true)
       OR luciel_instance_id IS NULL``

    A row is visible if:
      (a) its instance matches the requester's bound instance_id, OR
      (b) it has no instance binding (luciel_instance_id IS NULL) --
          which represents admin-level / cross-instance rows that
          should remain visible to admin-level API keys and to any
          instance-scoped request that needs cross-instance shared
          state (e.g. account-wide configuration).

  WITH CHECK (write-side -- strict):
    ``luciel_instance_id::text = current_setting('app.instance_id', true)
       OR (luciel_instance_id IS NULL
            AND current_setting('app.instance_id', true) = '')``

    Writers can:
      (a) write a row scoped to their bound instance, OR
      (b) write a NULL-instance row ONLY when no instance is bound
          (i.e., admin-level API key path -- the legitimate writer of
          cross-instance rows).

    Crucially, instance-A CANNOT write a NULL-instance row because its
    GUC would be set to its own instance id (non-empty), so branch (b)
    is closed to it. This prevents the most dangerous Wall-3 write
    leak: an instance-scoped tenant injecting a NULL row that other
    instances would then see via the NULL-permissive USING clause.

Cast note:
    instances.id is an Integer PK. We compare via
    ``luciel_instance_id::text = current_setting(...)`` because
    current_setting() always returns text. The cast is explicit so
    the planner does not have to coerce; also matches the listener
    which serialises via ``str(int)``.

current_setting missing-OK:
    The second arg ``true`` is the ``missing_ok`` flag. When the GUC
    has not been set on this connection's transaction, the call
    returns ``''`` (empty string) instead of raising
    ``undefined_object``. Empty does not equal any real instance id
    nor compare as NULL, so the row falls back to the IS-NULL branch.

    No pre-existing C3 policy on this table; this migration is the FIRST
    RLS policy on it for the instance_id (Wall 3) dimension. Wall-1
    enforcement on this table is delivered by service-layer L1 filtering
    via tenant_id (the table has no nullable tenant scope at Wall 1).

Reversibility: drop policy only then disable RLS.

Refs ARC9_RUNBOOK §C4.3.
"""

from __future__ import annotations

from alembic import op


revision = "arc9_c4_3d_rls_instance_sessions"
down_revision = "arc9_c4_3c_rls_instance_memory_items"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ENABLE ROW LEVEL SECURITY is idempotent at the table level --
    # PG silently no-ops the re-enable if the C3 migration already
    # enabled it. Issuing it here keeps each migration self-contained.
    op.execute("ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;")
    op.execute(
        """
        CREATE POLICY sessions_instance_isolation
        ON sessions
        AS PERMISSIVE
        FOR ALL
        TO PUBLIC
        USING (
            luciel_instance_id::text = current_setting('app.instance_id', true)
            OR luciel_instance_id IS NULL
        )
        WITH CHECK (
            luciel_instance_id::text = current_setting('app.instance_id', true)
            OR (
                luciel_instance_id IS NULL
                AND current_setting('app.instance_id', true) = ''
            )
        );
        """
    )


def downgrade() -> None:
    # Arc 9 C5.4 fix: the previous downgrade DISABLED RLS on this
    # table, which would have neutered the sibling Wall-1 policy
    # ``sessions_tenant_isolation`` shipped by C3.2e. Drop ONLY our
    # own Wall-3 policy; leave RLS enabled so the Wall-1 sibling
    # keeps enforcing tenant isolation under rollback.
    op.execute(
        "DROP POLICY IF EXISTS sessions_instance_isolation "
        "ON sessions;"
    )
