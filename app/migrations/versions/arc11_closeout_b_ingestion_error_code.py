"""Arc 11 Closeout PR-B — structured ingestion_error_code column.

Revision ID: arc11_closeout_b_ingestion_error_code
Revises: arc11_closeout_a_instance_lifecycle
Create Date: 2026-05-28

Doctrine anchor
---------------

Founder principle: no-internal-arc-strings-in-user-facing-contracts.

During Arc 11 the crawl-website route was stubbed (the real fetcher
ships in Arc 14). The stub signalled "coming soon" by writing the
literal substring ``"Arc-14"`` into ``knowledge_sources.ingestion_error``
and the frontend keyed its "Coming soon" badge on
``ingestion_error.includes("Arc-14")``. That is hygienic debt: an
internal arc identifier leaked into the cross-repo data contract. If
Arc 14 is ever renamed, split, or reorganised the frontend silently
breaks.

What this migration adds
------------------------

1. ``knowledge_sources.ingestion_error_code`` — nullable VARCHAR(64).
   Canonical values live in
   ``app.models.knowledge_source_errors.IngestionErrorCode``. The
   crawl stub now writes
   ``IngestionErrorCode.CRAWL_NOT_YET_AVAILABLE.value`` here; the
   human-readable ``ingestion_error`` column keeps a plain-English
   message for ops debugging. The frontend keys badge rendering on
   the structured code.

2. Partial index ``ix_knowledge_sources_ingestion_error_code`` over
   ``ingestion_error_code`` filtered to ``IS NOT NULL``. Same shape
   as the existing ``ix_knowledge_sources_soft_delete`` partial
   index — supports the (rare) admin-UI filter "show me sources
   that failed for reason X" without bloating the index across the
   common case of ``ingestion_error_code IS NULL``.

3. Backfill: any pre-existing row whose ``ingestion_error`` text
   contains the literal substring ``"Arc-14"`` is updated to the
   canonical code. Idempotent — re-running the migration on a
   database where the backfill already ran is a no-op because
   the WHERE clause includes ``ingestion_error_code IS NULL``.

   This is the ONE place the legacy ``"Arc-14"`` substring is
   permitted to remain in the codebase — it is grandfathered
   legacy data only. The crawl stub no longer writes the
   substring on new rows; only the historical rows from before
   this PR carry it.

Production safety
-----------------

* The column add is non-blocking — nullable, no default backfill.

* The partial index is created with the standard ``CREATE INDEX``
  form (not ``CONCURRENTLY``) because production currently has zero
  rows where ``ingestion_error_code IS NOT NULL`` (the stub has not
  been exercised in prod — Pro/Enterprise tier crawl is gated
  off). If a future revision needs to retro-fit this on a larger
  failed-row population, switch to ``CONCURRENTLY``.

* The backfill UPDATE is keyed on ``ingestion_error LIKE '%Arc-14%'``.
  No supporting index — but the ``knowledge_sources`` table is
  small (hundreds of rows at most across all tenants in current
  production) so a sequential scan is acceptable for a one-shot
  migration.

Downgrade
---------

Symmetric: drop the partial index, drop the column. The
``ingestion_error`` text column is untouched on both up and down —
it predates this migration.
"""
from __future__ import annotations

from alembic import op


# ---------------------------------------------------------------------
# Alembic identifiers.
# ---------------------------------------------------------------------
revision = "arc11_closeout_b_ingestion_error_code"
down_revision = "arc11_closeout_a_instance_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE knowledge_sources "
        "ADD COLUMN ingestion_error_code VARCHAR(64) NULL"
    )

    op.execute(
        "CREATE INDEX ix_knowledge_sources_ingestion_error_code "
        "ON knowledge_sources (ingestion_error_code) "
        "WHERE ingestion_error_code IS NOT NULL"
    )

    # Grandfathered legacy backfill — the ONE place the literal
    # "Arc-14" substring is permitted to remain in the codebase
    # after this PR. New rows never carry the substring; the crawl
    # stub now writes the structured code instead.
    op.execute(
        "UPDATE knowledge_sources "
        "   SET ingestion_error_code = 'CRAWL_NOT_YET_AVAILABLE' "
        " WHERE ingestion_error LIKE '%Arc-14%' "
        "   AND ingestion_error_code IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_knowledge_sources_ingestion_error_code")
    op.execute("ALTER TABLE knowledge_sources DROP COLUMN ingestion_error_code")
