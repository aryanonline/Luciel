"""Arc 12 EX3 — drop superseded ``sessions.agent_id`` / ``sessions.domain_id``.

Revision ID: arc12_ex3_drop_session_agent_domain
Revises: arc12_ex3_drop_api_key_agent_domain
Create Date: 2026-05-29

Context
-------
Per the Arc 12 excision plan, EX1b stopped the application layer from
filtering, exposing, or accepting ``sessions.agent_id`` / ``sessions
.domain_id`` at any boundary. EX2 re-sealed every live RLS policy to
``admin_id`` (+ ``luciel_instance_id``); no policy references either
column. Neither column is in the admin_audit_logs hash chain
(EX2 confirmed). The session scope at v2 is ``(admin_id,
luciel_instance_id, session_id)`` per Walls 3/4.

Up to now the application kept ``sessions.domain_id`` NOT NULL alive
with synthetic "instance-{luciel_instance_id}" sentinels at every
session-creation site (chat_widget.py / sessions.py). Those sentinels
are removed in the SAME commit as this migration, so the column drop
and the write-site removal land together — otherwise inserts would
still reference a dropped column.

Indexes
-------
* ``ix_sessions_domain_id`` (from create migration
  ``17ab56bdd913_create_sessions_and_messages_tables``).
* ``ix_sessions_agent_id`` (from
  ``b0b4a3861c4d_add_agent_config_and_agent_id_columns``).

Both are dropped BEFORE the column drops so PostgreSQL does not have
to chase the column-drop cascade across the index.

Downgrade
---------
``agent_id`` is re-added as ``nullable=True`` ``String(100)`` with its
``ix_sessions_agent_id`` index, matching the pre-EX3 shape. ``domain_id``
is re-added as ``nullable=True`` (NOT ``nullable=False``) with its
``ix_sessions_domain_id`` index: the downgrade cannot know the original
values, and a NOT-NULL re-add would fail on every existing row that was
written after the drop. The pre-EX3 NOT-NULL constraint is therefore NOT
restored; operators rolling back must accept that historical rows carry
NULL ``domain_id`` until they backfill out-of-band.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "arc12_ex3_drop_session_agent_domain"
down_revision = "arc12_ex3_drop_api_key_agent_domain"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index(
        op.f("ix_sessions_agent_id"),
        table_name="sessions",
    )
    op.drop_index(
        op.f("ix_sessions_domain_id"),
        table_name="sessions",
    )
    op.drop_column("sessions", "agent_id")
    op.drop_column("sessions", "domain_id")


def downgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("domain_id", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "sessions",
        sa.Column("agent_id", sa.String(length=100), nullable=True),
    )
    op.create_index(
        op.f("ix_sessions_domain_id"),
        "sessions",
        ["domain_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_sessions_agent_id"),
        "sessions",
        ["agent_id"],
        unique=False,
    )
