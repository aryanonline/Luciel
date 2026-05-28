"""Arc 11 Cleanup C — promote ``scope_assignments.role`` from
``VARCHAR(100)`` to a PostgreSQL ENUM.

Revision ID: arc11_cleanup_c_scope_assignment_role_enum
Revises: arc11_cleanup_c_drop_agent_id_from_knowledge_chunks
Create Date: 2026-05-28

Why this migration exists
-------------------------

Step 24.5b shipped ``ScopeAssignment.role`` as a free-form
``String(100)`` with the comment "may be promoted to an enum once
real-world role taxonomy stabilises." Arc 11 Step 7 codified the
four canonical values for the knowledge subsystem
(``admin_owner``, ``admin_manager``, ``instance_operator``,
``read_only_viewer``) and Cleanup C promotes them to a Postgres
``ENUM`` so the database fences invalid writes the same way the
``scope_assignment_end_reason`` enum already does.

Steps
-----

1. ``CREATE TYPE scope_role AS ENUM (...)``.
2. ``ALTER TABLE scope_assignments ALTER COLUMN role TYPE scope_role
   USING role::scope_role``. The USING clause coerces existing
   string values to enum members; production has 0 rows so no risk,
   but the USING handles any dev/test data that has legitimate
   values in the column.

Production safety
-----------------

``scope_assignments`` has 0 rows in production (ARC11_PLAN.md §12).
Dev / test environments may have rows; the ``USING role::scope_role``
clause coerces each row's text to the enum. If any row holds a
value outside the canonical four, the migration fails loudly at
this step — which is the right behaviour: that row is itself a
schema-level invariant violation.

Rollback
--------

``downgrade()`` symmetrically reverses: ALTER back to VARCHAR(100)
using ``role::text``, then DROP TYPE.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


# ---------------------------------------------------------------------
# Alembic identifiers.
# ---------------------------------------------------------------------
revision = "arc11_cleanup_c_scope_assignment_role_enum"
down_revision = "arc11_cleanup_c_drop_agent_id_from_knowledge_chunks"
branch_labels = None
depends_on = None


_ROLE_VALUES = (
    "admin_owner",
    "admin_manager",
    "instance_operator",
    "read_only_viewer",
)


def upgrade() -> None:
    # 1. Create the enum type. Use ``create_type=False`` semantics by
    #    issuing the DDL directly — bind_metadata flush would create
    #    the type implicitly on first column reference, but doing it
    #    explicitly here keeps the migration self-documenting.
    op.execute(
        "CREATE TYPE scope_role AS ENUM ("
        + ", ".join(f"'{v}'" for v in _ROLE_VALUES)
        + ")"
    )

    # 2. Convert the column. The USING clause coerces each row's
    #    existing text into the enum; rows holding a value outside
    #    the canonical four will surface a clear error here.
    op.execute(
        "ALTER TABLE scope_assignments "
        "ALTER COLUMN role TYPE scope_role "
        "USING role::scope_role"
    )


def downgrade() -> None:
    # Reverse: column back to VARCHAR(100), then drop the type.
    op.execute(
        "ALTER TABLE scope_assignments "
        "ALTER COLUMN role TYPE VARCHAR(100) "
        "USING role::text"
    )
    op.execute("DROP TYPE scope_role")
