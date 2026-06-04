"""RESCAN CORE(serving-path) GAP-5 — add 'inactive' to instance_status enum (§3.6.7).

Revision ID: rescan_core_inactive_status
Revises: rescan_ent_personality_approval
Create Date: 2026-06-04

What this migration does
------------------------

Architecture §3.6.7 (downgrade enforcement) needs a *system-imposed
pause* state distinct from the owner-initiated ``paused`` (§4.5 Phase
8). When a Pro→Free downgrade leaves an Admin with more instances than
the Free instance cap allows, the over-cap instances must go quiet —
they keep their data (reactivatable on upgrade) but must NOT serve or
accrue budget.

Before this revision the downgrade-enforcement path wrote only the
deprecated ``instances.active = False`` boolean and never touched
``instance_status``. But every live lifecycle gate keys off
``instance_status`` (widget ``chat_widget.py``, SMS ``base.py``
``check_instance_lifecycle``), so a downgrade-archived instance kept
``instance_status = 'active'`` and KEPT SERVING. This migration adds
the ``inactive`` enum value so the enforcement path can express the
system pause in the column the gates actually read.

PG ENUM ADD VALUE — transaction isolation
-----------------------------------------

``ALTER TYPE ... ADD VALUE`` cannot run in the same transaction as DDL
that references the new value. We follow the exact pattern established
by ``rescand_lifecycle_states``: run the ADD VALUE via a raw DBAPI
cursor in AUTOCOMMIT mode so the new label is committed before the
surrounding Alembic transaction resumes. ``IF NOT EXISTS`` makes the
statement idempotent (safe to re-run / safe if a prior partial run
already added it).

No index or column references ``inactive`` in this migration, so the
autocommit commit is the only DDL required.

Downgrade — ENUM value removal note
-----------------------------------

PostgreSQL does NOT support ``ALTER TYPE ... DROP VALUE``. The new
``inactive`` value is therefore LEFT IN PLACE on downgrade — the
standard expand-contract reality for PG enums (see
``rescand_lifecycle_states`` for the full DBA rebuild recipe). The
value is harmless when no rows reference it.
"""
from __future__ import annotations

from alembic import op

revision = "rescan_core_inactive_status"
down_revision = "rescan_ent_personality_approval"
branch_labels = None
depends_on = None


_NEW_VALUE = "inactive"


def upgrade() -> None:
    conn = op.get_bind()
    # conn.connection is the SQLAlchemy pool _ConnectionFairy;
    # .dbapi_connection is the actual psycopg.Connection object.
    dbapi_conn = conn.connection.dbapi_connection
    # Alembic wraps upgrade() in a txn (INTRANS). Commit the current
    # (empty) txn to return to IDLE before flipping autocommit on, then
    # restore so Alembic's own transaction tracking resumes cleanly.
    dbapi_conn.commit()  # return to IDLE state
    try:
        dbapi_conn.autocommit = True
        cur = dbapi_conn.cursor()
        cur.execute(
            f"ALTER TYPE instance_status "
            f"ADD VALUE IF NOT EXISTS '{_NEW_VALUE}'"
        )
        cur.close()
    finally:
        dbapi_conn.autocommit = False  # restore default (begin new txn)


def downgrade() -> None:
    # The 'inactive' ENUM value is intentionally LEFT IN PLACE.
    # PostgreSQL does not support ALTER TYPE ... DROP VALUE; full
    # removal requires a DBA type rebuild (see module docstring of
    # rescand_lifecycle_states for the recipe). The value is harmless
    # when no rows reference it.
    pass
