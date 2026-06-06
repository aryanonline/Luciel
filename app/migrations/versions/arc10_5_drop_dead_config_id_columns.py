"""Arc 10.5: drop dead *_config_id columns from traces.

Revision ID: arc10_5_drop_dead_config_id_columns
Revises: arc10_gap7_audit_loosen_instance_for_admin_scope
Create Date: 2026-05-27

Anchored to Vision v1 \u00a73 (five configuration pillars live on the
Instance row; no Domain or Agent layer) and Architecture v1 \u00a73.2
(Instance subsystem). The legacy
TenantConfig -> DomainConfig -> AgentConfig chain was eliminated
before Arc 10. The underlying tables (``tenant_configs``,
``domain_configs``, ``agent_configs``) were DROPPED at
``arc5_c_admin_instance_subtractive``.

The ``traces`` table still carried three columns pointing at those
gone tables:

  - ``traces.tenant_config_id``  -> dropped target ``tenant_configs``
  - ``traces.domain_config_id``  -> dropped target ``domain_configs``
  - ``traces.agent_config_id``   -> dropped target ``agent_configs``

These columns have no FK constraint (the FKs were dropped along
with the parent tables) and no consumer in V2 code. Every new
trace row gets ``NULL`` for all three because no code path sets
them. Historical content is pure dead data shape \u2014 the
config rows they refer to are gone.

Architecture \u00a73.7.5 (RLS / minimization) supports leaving no
content-less columns on customer-data tables. Drop them.

NOTE: ``traces.domain_id`` and ``traces.agent_id`` (free-text
columns) are NOT dropped here. They carry pre-V2 historical
forensic content (free-text IDs of the now-gone tables' rows)
that an auditor walking the trace history might still want to see.
Future arc may revisit; for now, kept as legacy free-text columns
that always receive NULL on new rows.

Idempotency: each DROP COLUMN is guarded by IF EXISTS via the
PostgreSQL syntax through op.execute(). Safe to re-run.
"""
from __future__ import annotations

from alembic import op


revision = "arc10_5_drop_dead_config_id_columns"
down_revision = "arc10_gap7_audit_loosen_instance_for_admin_scope"
branch_labels = None
depends_on = None


_DEAD_COLUMNS = (
    "tenant_config_id",
    "domain_config_id",
    "agent_config_id",
)


def upgrade() -> None:
    for col in _DEAD_COLUMNS:
        op.execute(f"ALTER TABLE traces DROP COLUMN IF EXISTS {col}")


def downgrade() -> None:
    """Restore the columns (NOT the content; the parent tables are
    gone). Provided for migration symmetry only; the columns will
    be NULL on every row after downgrade. Forward-only in practice.
    """
    import sqlalchemy as sa
    for col in _DEAD_COLUMNS:
        op.add_column(
            "traces",
            sa.Column(col, sa.Integer(), nullable=True),
        )
