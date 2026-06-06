"""arc11 cleanup c: promote user_invites.role to scope_role PG enum

Revision ID: arc11_cleanup_c_user_invites_role_enum
Revises: arc11_cleanup_c_scope_assignment_role_enum
Create Date: 2026-05-28

Cleanup C tail of Arc 11 no-deferrals closeout.

The Cleanup C subagent flagged that ``app/services/invite_service.py``
mints ``role="teammate"`` for Pro-tier invites, but the canonical role
taxonomy locked by Architecture §3.2.2 is the four-value enum
``scope_role`` (admin_owner / admin_manager / instance_operator /
read_only_viewer). Production has 0 invite rows today, so the latent
enum-violation bug never fired in the wild — but under the founder's
no-deferrals rule it cannot ship with Arc 11.

This migration promotes ``user_invites.role`` to the same ``scope_role``
PG enum that ``scope_assignments.role`` was promoted to in the prior
Cleanup C migration. The ``scope_role`` type already exists, so this
migration only alters the column type, drops the legacy string default,
and installs a new ``instance_operator`` default (matching Customer
Journey §10.3 where Marcus's 12 agents receive instance_operator scope).

No data coercion is needed beyond the USING clause; production has 0
rows. Any dev/test fixture that minted ``role='teammate'`` rows will
hit a USING-clause failure here — fix the fixture, not the migration.
"""
from __future__ import annotations

from alembic import op


# Alembic identifiers.
revision = "arc11_cleanup_c_user_invites_role_enum"
down_revision = "arc11_cleanup_c_scope_assignment_role_enum"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Drop the legacy 'teammate' default so the ALTER TYPE doesn't
    #    try to coerce the default value.
    op.execute("ALTER TABLE user_invites ALTER COLUMN role DROP DEFAULT")

    # 2. Coerce the column to the existing scope_role enum. The USING
    #    clause attempts to cast each row's text value to the enum;
    #    production has 0 rows, so this is a no-op against live data.
    #    Any dev/test row whose legacy value cannot cast (e.g.
    #    'teammate', 'department_lead') will raise here — that is the
    #    intended fail-loud behaviour. Fix the fixture, not the
    #    migration.
    op.execute(
        "ALTER TABLE user_invites "
        "ALTER COLUMN role TYPE scope_role USING role::scope_role"
    )

    # 3. Install the new canonical default. instance_operator is the
    #    Pro-tier invite role per Customer Journey §10.3.
    op.execute(
        "ALTER TABLE user_invites "
        "ALTER COLUMN role SET DEFAULT 'instance_operator'::scope_role"
    )


def downgrade() -> None:
    # Symmetric reversal: type back to VARCHAR(100), default back to
    # the legacy 'teammate' string. The ``USING role::text`` clause
    # writes the enum value's text representation, so admin_owner →
    # 'admin_owner', etc. The downgrade is lossless.
    op.execute(
        "ALTER TABLE user_invites "
        "ALTER COLUMN role DROP DEFAULT"
    )
    op.execute(
        "ALTER TABLE user_invites "
        "ALTER COLUMN role TYPE VARCHAR(100) USING role::text"
    )
    op.execute(
        "ALTER TABLE user_invites "
        "ALTER COLUMN role SET DEFAULT 'teammate'"
    )
