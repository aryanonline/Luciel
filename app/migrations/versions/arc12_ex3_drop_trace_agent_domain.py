"""Arc 12 EX3 — drop superseded ``traces.agent_id`` and ``traces.domain_id``.

Revision ID: arc12_ex3_drop_trace_agent_domain
Revises: arc12_ex2_rls_drop_agent_domain_refs
Create Date: 2026-05-29

Context
-------
Per the Arc 12 excision plan, EX2 already re-sealed every live RLS policy
to ``admin_id`` (+ ``luciel_instance_id``) and removed any residual
reference to the v1 ``agent_id`` / ``domain_id`` columns from policy SQL.
EX1 swept the code-level callsites. With nothing live reading or writing
these columns on ``traces`` anymore — and because they are NOT part of
the admin_audit_logs hash chain (that's EX4's territory) — it is safe to
drop them from the ``traces`` table now.

Neither column has an index on ``traces`` (verified against the
original create migration and the later add-column migrations:
``d7f2ad643640_add_traces_table``, ``7fb73e7eb812`` for ``domain_id``,
``8b896ecd5881`` for ``agent_id``). The model has no ``__table_args__``
Index entry for them either. Nothing to drop before the columns.

Downgrade re-adds both as nullable ``String(100)`` with no index, which
matches the pre-EX3 shape.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "arc12_ex3_drop_trace_agent_domain"
down_revision = "arc12_ex2_rls_drop_agent_domain_refs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("traces", "agent_id")
    op.drop_column("traces", "domain_id")


def downgrade() -> None:
    op.add_column(
        "traces",
        sa.Column("domain_id", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "traces",
        sa.Column("agent_id", sa.String(length=100), nullable=True),
    )
