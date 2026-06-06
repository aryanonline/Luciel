"""Unit 9 (part 2) — widen escalation signal CHECK to admit 'llm_unavailable'.

Revision ID: unit9_escalation_signal_llm_unavailable
Revises: unit5_rename_connection_secret_cols
Create Date: 2026-06-06

Why this migration exists
-------------------------

Architecture line 1354 (normative): when BOTH LLM providers are down the
orchestrator fires an escalation with ``signal_type = llm_unavailable``,
notifies the admin, and returns the canonical "I've let the team know"
phrase rather than fabricating a response. That escalation reuses the
``escalation_events`` machinery, so the row it writes carries
``signal = 'llm_unavailable'``.

``ck_escalation_events_signal`` enumerates the allowed signal values, so
the new value must be admitted at the DB level. Postgres CHECK constraints
are immutable in place, so we DROP + recreate (mirroring exactly how
arc18_conversation_budget_metering widened the same CHECK to admit
``'budget_exhausted'``). SQLite test DBs build their schema from
``Base.metadata``, which already carries the widened CHECK in the model —
this op is the Postgres-side reconciliation.

Rollback contract
-----------------
``alembic downgrade -1`` restores the narrower CHECK (the post-arc18 set,
WITHOUT ``'llm_unavailable'``). Data-safe in the widening direction;
re-narrowing would FAIL if any ``llm_unavailable`` rows exist — intentional
(no silent constraint violation).
"""
from __future__ import annotations

from alembic import op


revision = "unit9_escalation_signal_llm_unavailable"
down_revision = "unit5_rename_connection_secret_cols"
branch_labels = None
depends_on = None


_ESC = "escalation_events"
_ESC_CHECK = "ck_escalation_events_signal"

_SIGNALS_WITH_LLM_UNAVAILABLE = (
    "'explicit_human_request', 'strong_negative_sentiment', "
    "'cannot_confidently_answer', 'high_value_lead', "
    "'budget_exhausted', 'llm_unavailable'"
)
_SIGNALS_WITHOUT_LLM_UNAVAILABLE = (
    "'explicit_human_request', 'strong_negative_sentiment', "
    "'cannot_confidently_answer', 'high_value_lead', 'budget_exhausted'"
)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if _is_postgres():
        op.execute(f"ALTER TABLE {_ESC} DROP CONSTRAINT IF EXISTS {_ESC_CHECK};")
        op.execute(
            f"ALTER TABLE {_ESC} ADD CONSTRAINT {_ESC_CHECK} "
            f"CHECK (signal IN ({_SIGNALS_WITH_LLM_UNAVAILABLE}));"
        )


def downgrade() -> None:
    # Restore the narrower CHECK. Will FAIL if any llm_unavailable rows
    # exist — intentional (no silent violation).
    if _is_postgres():
        op.execute(f"ALTER TABLE {_ESC} DROP CONSTRAINT IF EXISTS {_ESC_CHECK};")
        op.execute(
            f"ALTER TABLE {_ESC} ADD CONSTRAINT {_ESC_CHECK} "
            f"CHECK (signal IN ({_SIGNALS_WITHOUT_LLM_UNAVAILABLE}));"
        )
