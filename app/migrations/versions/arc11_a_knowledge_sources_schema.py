"""Arc 11 Step 1 — knowledge_sources table + additive FK on
knowledge_embeddings + traces.source_ids_used.

Revision ID: arc11_a_knowledge_sources_schema
Revises: arc10_5_drop_dead_config_id_columns
Create Date: 2026-05-28

Why this migration exists
-------------------------

Arc 11 (Knowledge Base v1) splits provenance from chunks per
Architecture v1 §3.2 — two tables (``knowledge_sources`` for the
provenance/lifecycle record, ``knowledge_chunks`` for the vectors).
The current code uses one table (``knowledge_embeddings``) with
provenance flattened into chunk rows via a string ``source_id``
column. The ARC 11 plan §2.2 sequences the cutover in 11 steps so
each is independently reversible; this migration is **Step 1**.

Step 1 is schema-only and strictly additive:

  * Create the new ``knowledge_sources`` table per ARC11_PLAN.md §2.1.
  * Add a nullable ``source_fk BIGINT`` FK column on the legacy
    ``knowledge_embeddings`` table pointing at the new table. The
    column is named ``source_fk`` (not ``source_id``) because the
    legacy string ``source_id`` column is still in use on this table
    and will not be dropped until Step 11. Step 11 also renames
    ``source_fk`` → ``source_id``.
  * Add ``traces.source_ids_used BIGINT[]`` with a GIN index, per
    ARC11_PLAN.md §2.5 — backs the delete-confirm modal's
    "affected questions" preview (Architecture §3.2.2).

What this migration does NOT do (covered by later Arc 11 steps):

  * Step 2 (``arc11/b-rename``): rename ``knowledge_embeddings`` →
    ``knowledge_chunks``.
  * Step 3 (``arc11/c-repository``): repoint ingestion / retriever
    / repository to read & write ``source_fk``.
  * Step 4 (``arc11/d-rls``): RLS policies on ``knowledge_sources``.
  * Step 5 (``arc11/e-trace-extension``): retriever instrumentation
    that populates ``traces.source_ids_used`` on each turn.
  * Step 11 (``arc11/k-close``): drop legacy string ``source_id`` and
    rename ``source_fk`` → ``source_id``; set FK NOT NULL.

Production safety
-----------------

Per ARC11_PLAN.md §12, the production tables involved are empty
(``knowledge_embeddings`` and ``traces`` both at 0 rows; ``admins``
and ``instances`` at 0). The migration is therefore strictly
additive with no backfill work. The ``downgrade()`` cleanly reverses
the schema; no data preservation is required.

pgcrypto note
-------------

``gen_random_uuid()`` requires the ``pgcrypto`` extension. The
extension was enabled by migration ``3ad39f9e6b55`` (Step 24.5
users/scope_assignments) and so is already present on every
environment whose graph includes that ancestor. ``CREATE EXTENSION
IF NOT EXISTS`` is re-issued here as a defensive idempotent guard
so this single file remains self-contained against a fresh DB
that has somehow lost the extension.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


# ---------------------------------------------------------------------
# Alembic identifiers.
# ---------------------------------------------------------------------
revision = "arc11_a_knowledge_sources_schema"
down_revision = "arc10_5_drop_dead_config_id_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Apply the Arc 11 Step 1 schema."""

    # -----------------------------------------------------------------
    # 0. pgcrypto (idempotent; already enabled by 3ad39f9e6b55).
    # -----------------------------------------------------------------
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # -----------------------------------------------------------------
    # 1. knowledge_sources — provenance + lifecycle record per source.
    # -----------------------------------------------------------------
    op.create_table(
        "knowledge_sources",
        sa.Column(
            "id",
            sa.BigInteger(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "source_uuid",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            unique=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "admin_id",
            sa.String(length=100),
            sa.ForeignKey("admins.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "luciel_instance_id",
            sa.BigInteger(),
            sa.ForeignKey("instances.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        # ---- Provenance ----
        sa.Column("filename", sa.Text(), nullable=True),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("s3_key", sa.Text(), nullable=True),
        sa.Column("origin_url", sa.Text(), nullable=True),
        # ---- Ingestion lifecycle ----
        sa.Column(
            "ingestion_status",
            sa.Text(),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("ingestion_error", sa.Text(), nullable=True),
        sa.Column("ingested_by", sa.Text(), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_viewed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        # ---- Versioning ----
        sa.Column(
            "source_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "superseded_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        # ---- Soft delete ----
        sa.Column(
            "soft_deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        # ---- Downgrade archive (Arc 10 pattern preserved) ----
        sa.Column(
            "pending_downgrade_archived_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        # ---- Audit timestamps ----
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "ingestion_status IN ('pending','processing','ready','failed')",
            name="ck_knowledge_sources_ingestion_status_valid",
        ),
        comment=(
            "Arc 11: per-source provenance + lifecycle record. One row "
            "per uploaded/pasted/crawled source. knowledge_embeddings "
            "(soon to be knowledge_chunks) FKs here via source_fk. "
            "Anchored to Architecture v1 §3.2 (two-table model)."
        ),
    )

    # Indexes per ARC11_PLAN.md §2.1.
    op.create_index(
        "ix_knowledge_sources_tenant_scope",
        "knowledge_sources",
        ["admin_id", "luciel_instance_id"],
    )
    op.execute(
        """
        CREATE INDEX ix_knowledge_sources_status
            ON knowledge_sources (admin_id, luciel_instance_id, ingestion_status)
            WHERE soft_deleted_at IS NULL
        """
    )
    op.execute(
        """
        CREATE INDEX ix_knowledge_sources_soft_delete
            ON knowledge_sources (soft_deleted_at)
            WHERE soft_deleted_at IS NOT NULL
        """
    )

    # -----------------------------------------------------------------
    # 2. knowledge_embeddings.source_fk — additive nullable FK.
    # -----------------------------------------------------------------
    # Named source_fk (not source_id) because the legacy string
    # source_id column is still in use here. Step 11 drops the legacy
    # string column and renames this column to source_id. Until then,
    # source_fk coexists with the legacy string source_id.
    op.add_column(
        "knowledge_embeddings",
        sa.Column(
            "source_fk",
            sa.BigInteger(),
            sa.ForeignKey(
                "knowledge_sources.id",
                ondelete="CASCADE",
                name="fk_knowledge_embeddings_source_fk",
            ),
            nullable=True,
            comment=(
                "Arc 11 Step 1 additive FK to knowledge_sources.id. "
                "Will be renamed to source_id and made NOT NULL in "
                "Step 11 once the legacy string source_id column is "
                "dropped. Nullable for the duration of the cutover."
            ),
        ),
    )
    op.create_index(
        "ix_knowledge_embeddings_source_fk",
        "knowledge_embeddings",
        ["source_fk"],
    )

    # -----------------------------------------------------------------
    # 3. traces.source_ids_used — BIGINT[] populated by retriever.
    # -----------------------------------------------------------------
    op.add_column(
        "traces",
        sa.Column(
            "source_ids_used",
            postgresql.ARRAY(sa.BigInteger()),
            nullable=False,
            server_default=sa.text("'{}'::bigint[]"),
            comment=(
                "Arc 11: list of knowledge_sources.id rows that "
                "contributed chunks to this turn. Populated by the "
                "retriever in Step 5. Queried by the delete-confirm "
                "modal (Architecture §3.2.2) to preview customer "
                "questions affected by a source deletion."
            ),
        ),
    )
    op.execute(
        """
        CREATE INDEX ix_traces_source_ids_used
            ON traces USING GIN (source_ids_used)
        """
    )


def downgrade() -> None:
    """Reverse the Arc 11 Step 1 schema.

    Production is empty at the time this migration first applies
    (ARC11_PLAN.md §12), so no data preservation is needed.
    """

    # -----------------------------------------------------------------
    # 3. traces.source_ids_used.
    # -----------------------------------------------------------------
    op.execute("DROP INDEX IF EXISTS ix_traces_source_ids_used")
    op.drop_column("traces", "source_ids_used")

    # -----------------------------------------------------------------
    # 2. knowledge_embeddings.source_fk.
    # -----------------------------------------------------------------
    op.drop_index(
        "ix_knowledge_embeddings_source_fk",
        table_name="knowledge_embeddings",
    )
    op.drop_constraint(
        "fk_knowledge_embeddings_source_fk",
        "knowledge_embeddings",
        type_="foreignkey",
    )
    op.drop_column("knowledge_embeddings", "source_fk")

    # -----------------------------------------------------------------
    # 1. knowledge_sources table + indexes.
    # -----------------------------------------------------------------
    op.execute("DROP INDEX IF EXISTS ix_knowledge_sources_soft_delete")
    op.execute("DROP INDEX IF EXISTS ix_knowledge_sources_status")
    op.drop_index(
        "ix_knowledge_sources_tenant_scope",
        table_name="knowledge_sources",
    )
    op.drop_table("knowledge_sources")

    # pgcrypto extension is intentionally NOT dropped — earlier
    # migrations rely on it (gen_random_uuid() on users, conversations,
    # data_export_jobs, etc.).
