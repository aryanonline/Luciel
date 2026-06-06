"""step25b_knowledge_ingestion_chunking_versioning

Step 25b — Knowledge Ingestion Pipeline.

Adds:
  - tenant_configs:    chunk_size / chunk_overlap / chunk_strategy
                       (NOT NULL, server defaults 500 / 50 / 'paragraph')
  - domain_configs:    chunk_size / chunk_overlap / chunk_strategy
                       (nullable overrides; NULL means inherit from tenant)
  - luciel_instances:  chunk_size / chunk_overlap / chunk_strategy
                       (nullable overrides; NULL means inherit from
                        domain -> tenant)  [Option A, Step 25b]
  - knowledge_embeddings:
        luciel_instance_id  FK -> luciel_instances.id  (nullable,
            ON DELETE SET NULL — preserves history if instance is removed)
        source_id           String(100)  indexed
        source_version      Integer NOT NULL default 1
        source_filename     String(500)
        source_type         String(20)
        ingested_by         String(100)
        superseded_at       DateTime(tz=True)  nullable
  - composite index ix_knowledge_embeddings_scope_source on
        (tenant_id, domain_id, luciel_instance_id, source_id)
        for fast replace_by_source_id lookups.

Hand-written, NOT autogenerate — pgvector's `vector` type isn't recognized
by Alembic and autogenerate would attempt to drop the embedding column.

Down-revision: 355c69e7fd8b  (Step 24.5 File 15: sessions+traces FK)
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '52e19e8ae552'
down_revision = '355c69e7fd8b'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---------- tenant_configs: default chunking config (NOT NULL) ----------
    op.add_column(
        "tenant_configs",
        sa.Column(
            "chunk_size",
            sa.Integer(),
            nullable=False,
            server_default="500",
        ),
    )
    op.add_column(
        "tenant_configs",
        sa.Column(
            "chunk_overlap",
            sa.Integer(),
            nullable=False,
            server_default="50",
        ),
    )
    op.add_column(
        "tenant_configs",
        sa.Column(
            "chunk_strategy",
            sa.String(length=20),
            nullable=False,
            server_default="paragraph",
        ),
    )

    # ---------- domain_configs: optional overrides ----------
    op.add_column(
        "domain_configs",
        sa.Column("chunk_size", sa.Integer(), nullable=True),
    )
    op.add_column(
        "domain_configs",
        sa.Column("chunk_overlap", sa.Integer(), nullable=True),
    )
    op.add_column(
        "domain_configs",
        sa.Column("chunk_strategy", sa.String(length=20), nullable=True),
    )

    # ---------- luciel_instances: optional overrides (Option A) ----------
    op.add_column(
        "luciel_instances",
        sa.Column("chunk_size", sa.Integer(), nullable=True),
    )
    op.add_column(
        "luciel_instances",
        sa.Column("chunk_overlap", sa.Integer(), nullable=True),
    )
    op.add_column(
        "luciel_instances",
        sa.Column("chunk_strategy", sa.String(length=20), nullable=True),
    )

    # ---------- knowledge_embeddings: instance binding ----------
    op.add_column(
        "knowledge_embeddings",
        sa.Column(
            "luciel_instance_id",
            sa.Integer(),
            sa.ForeignKey(
                "luciel_instances.id",
                ondelete="SET NULL",
                name="fk_knowledge_embeddings_luciel_instance_id",
            ),
            nullable=True,
            index=True,
        ),
    )

    # ---------- knowledge_embeddings: versioning + audit ----------
    op.add_column(
        "knowledge_embeddings",
        sa.Column("source_id", sa.String(length=100), nullable=True, index=True),
    )
    op.add_column(
        "knowledge_embeddings",
        sa.Column(
            "source_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.add_column(
        "knowledge_embeddings",
        sa.Column("source_filename", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "knowledge_embeddings",
        sa.Column("source_type", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "knowledge_embeddings",
        sa.Column("ingested_by", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "knowledge_embeddings",
        sa.Column(
            "superseded_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    # ---------- composite index for fast "find chunks for source" lookups ----------
    op.create_index(
        "ix_knowledge_embeddings_scope_source",
        "knowledge_embeddings",
        ["tenant_id", "domain_id", "luciel_instance_id", "source_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_knowledge_embeddings_scope_source",
        table_name="knowledge_embeddings",
    )
    op.drop_column("knowledge_embeddings", "superseded_at")
    op.drop_column("knowledge_embeddings", "ingested_by")
    op.drop_column("knowledge_embeddings", "source_type")
    op.drop_column("knowledge_embeddings", "source_filename")
    op.drop_column("knowledge_embeddings", "source_version")
    op.drop_column("knowledge_embeddings", "source_id")

    # FK column drop must reference its constraint name on Postgres
    op.drop_constraint(
        "fk_knowledge_embeddings_luciel_instance_id",
        "knowledge_embeddings",
        type_="foreignkey",
    )
    op.drop_column("knowledge_embeddings", "luciel_instance_id")

    op.drop_column("luciel_instances", "chunk_strategy")
    op.drop_column("luciel_instances", "chunk_overlap")
    op.drop_column("luciel_instances", "chunk_size")

    op.drop_column("domain_configs", "chunk_strategy")
    op.drop_column("domain_configs", "chunk_overlap")
    op.drop_column("domain_configs", "chunk_size")

    op.drop_column("tenant_configs", "chunk_strategy")
    op.drop_column("tenant_configs", "chunk_overlap")
    op.drop_column("tenant_configs", "chunk_size")