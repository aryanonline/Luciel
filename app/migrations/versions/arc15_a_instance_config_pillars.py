"""Arc 15 A — instance config pillars (Vision §3.5, Journey Phase 3/4).

Revision ID: arc15_a_instance_config_pillars
Revises: arc14_u4_leads
Create Date: 2026-06-02

Why this migration exists
-------------------------
Arc 15 WU1 lands the structured instance-configuration pillars that
replace the free-text ``system_prompt_additions`` authoring path
(Vision §3.5 / Architecture §3.5.1 — "never raw prompt authoring"):

  * ``website``            — String(255) nullable. Journey Phase 3.
  * ``personality_preset`` — PG enum ``personality_preset`` (5 values),
                             default ``warm_concierge``. ``custom`` is
                             Pro/Enterprise-only, gated at the API, NOT
                             at the DB (the enum admits it everywhere).
  * ``personality_axes``   — JSONB nullable. ``{tone, verbosity,
                             formality, pace}`` only when preset=custom.
  * ``business_context``   — Text nullable. Tier-capped at Pydantic
                             (280 Free/Pro, 2000 Ent); NOT capped here.
  * ``lead_routing``       — JSONB nullable. Pro/Enterprise only.
  * ``escalation_config``  — JSONB nullable (Arc 15 WU3). Contact +
                             routing only; NEVER trigger config.

Deprecation (NOT a drop)
------------------------
``system_prompt_additions`` is left in place. The Arc 15 composer no
longer reads it and the personality API never writes it, but dropping
the column mid-flight would break the legacy 4-layer composer for any
instance still carrying a value. The column drop is a follow-up
(Arc 16+) once no instance relies on it.

RLS posture
-----------
``instances`` already carries its tenant-isolation RLS from the table's
creating migration; ALTER TABLE ADD COLUMN inherits the existing policy.
No RLS changes are needed here.

Rollback contract
-----------------
``downgrade`` drops the five WU1 columns + the WU3 escalation_config
column and the ``personality_preset`` enum type. Data-safe (narrows).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM, JSONB


revision = "arc15_a_instance_config_pillars"
down_revision = "arc14_u4_leads"
branch_labels = None
depends_on = None


_TABLE = "instances"
_PRESET_ENUM = "personality_preset"
_PRESET_VALUES = (
    "warm_concierge",
    "professional_advisor",
    "friendly_expert",
    "trusted_authority",
    "custom",
)


def upgrade() -> None:
    # 1. Create the personality_preset PG enum type.
    preset_enum = ENUM(*_PRESET_VALUES, name=_PRESET_ENUM, create_type=False)
    preset_enum.create(op.get_bind(), checkfirst=True)

    # 2. Add the pillar columns.
    op.add_column(
        _TABLE,
        sa.Column("website", sa.String(255), nullable=True),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "personality_preset",
            preset_enum,
            nullable=False,
            server_default="warm_concierge",
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column("personality_axes", JSONB(), nullable=True),
    )
    op.add_column(
        _TABLE,
        sa.Column("business_context", sa.Text(), nullable=True),
    )
    op.add_column(
        _TABLE,
        sa.Column("lead_routing", JSONB(), nullable=True),
    )
    # Arc 15 WU3 — escalation contact + routing config (contact only;
    # never trigger config). Landed alongside the WU1 pillars so the
    # personality + escalation APIs share one migration boundary.
    op.add_column(
        _TABLE,
        sa.Column("escalation_config", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, "escalation_config")
    op.drop_column(_TABLE, "lead_routing")
    op.drop_column(_TABLE, "business_context")
    op.drop_column(_TABLE, "personality_axes")
    op.drop_column(_TABLE, "personality_preset")
    op.drop_column(_TABLE, "website")

    ENUM(name=_PRESET_ENUM).drop(op.get_bind(), checkfirst=True)
