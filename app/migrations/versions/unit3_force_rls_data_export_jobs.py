"""Unit 3 (tenant-isolation re-verify) — FORCE RLS on data_export_jobs.

The Arc 10 lifecycle migration (arc10_lifecycle_subsystem) created
``data_export_jobs`` with ``ENABLE ROW LEVEL SECURITY`` and the correct
per-tenant isolation policy, but — unlike every other tenant table —
omitted ``FORCE ROW LEVEL SECURITY``. Its own comment claimed "the same
RLS posture as the other tables," so this was an oversight, not a
deliberate exception.

Why it matters: without FORCE, the table OWNER bypasses RLS. Production
connects as the non-owner ``luciel_app`` role (so RLS still applies even
without FORCE), which is why this never produced a live leak — but the
platform's defense-in-depth doctrine (Architecture §3.7.2b; every other
tenant table carries ENABLE + FORCE since arc9_c10_a_force_rls) is that
even an accidental owner/migrator connection must not be able to read
across tenants. This migration brings ``data_export_jobs`` to parity.

Idempotent + reversible.

Revision ID: unit3_force_rls_data_export_jobs
Revises: unit1_excise_deferred_features
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "unit3_force_rls_data_export_jobs"
down_revision = "unit1_excise_deferred_features"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    insp = sa.inspect(op.get_bind())
    return name in insp.get_table_names()


def upgrade() -> None:
    if _table_exists("data_export_jobs"):
        # Idempotent: re-issuing FORCE on an already-forced table is a
        # no-op in Postgres.
        op.execute("ALTER TABLE data_export_jobs FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    if _table_exists("data_export_jobs"):
        op.execute("ALTER TABLE data_export_jobs NO FORCE ROW LEVEL SECURITY")
