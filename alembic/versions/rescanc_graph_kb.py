"""Rescan Tier-C — Graph knowledge store (Architecture §3.2.1, §3.4.1 RETRIEVE).

Revision ID: rescanc_graph_kb
Revises: rescanc_handoff_session_mode
Create Date: 2026-06-10

What this migration adds
------------------------

Two new tables implementing the Path-Locked PostgreSQL-native graph store
(Locked Decision #4 — NO Neo4j/Memgraph). Domain-agnostic node/edge types
(strings inferred at ingest, NOT a fixed vertical ontology, Decision #5).

knowledge_graph_nodes
    Stores extracted entities from ingested knowledge. node_type is a
    free-form string (e.g. "Role", "Skill", "Service", "Practitioner",
    "Listing", "Feature" — derived from content at ingest, never hardcoded).

    Columns:
        id               bigserial PK
        admin_id         text NOT NULL FK admins.id
        instance_id      integer NOT NULL FK instances.id
        source_id        bigint NOT NULL FK knowledge_sources.id
        node_type        text NOT NULL  — inferred at ingest
        label            text NOT NULL  — canonical name of the entity
        attributes       jsonb DEFAULT '{}'
        created_at       timestamptz NOT NULL DEFAULT now()

    Indexes:
        (admin_id, instance_id)   — tenant-scoped reads
        (node_type)               — type-filtered traversal
        (label)                   — label lookup

knowledge_graph_edges
    Stores directed relationships between nodes. edge_type is also a
    free-form string (e.g. "has_skill", "provides", "lists_feature",
    "requires").

    Columns:
        id               bigserial PK
        admin_id         text NOT NULL FK admins.id
        instance_id      integer NOT NULL FK instances.id
        source_id        bigint NOT NULL FK knowledge_sources.id
        src_node_id      bigint NOT NULL FK knowledge_graph_nodes.id
        dst_node_id      bigint NOT NULL FK knowledge_graph_nodes.id
        edge_type        text NOT NULL
        attributes       jsonb DEFAULT '{}'
        created_at       timestamptz NOT NULL DEFAULT now()

    Indexes:
        (admin_id, instance_id)        — tenant-scoped reads
        (src_node_id)                  — outbound traversal
        (dst_node_id)                  — inbound traversal
        (edge_type)                    — type-filtered traversal

RLS
---
Both tables mirror the knowledge_sources RLS posture:
    * ENABLE ROW LEVEL SECURITY
    * FORCE ROW LEVEL SECURITY
    * RESTRICTIVE FOR ALL policy: admin_id::text = current_setting('app.admin_id', true)

This is the strict-tenant shape from Arc 9 C11. The fail-closed property:
when app.admin_id is unset, current_setting(..., true) returns NULL;
admin_id = NULL evaluates to NULL (SQL three-value logic); RLS denies.

Expand-contract design
----------------------
Tables are new (additive). No existing rows are touched. Downgrade drops
both tables completely (no data loss for the core pipeline).

Down-revision
-------------
Drops both tables and all associated indexes and policies.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text as sa_text

revision = "rescanc_graph_kb"
down_revision = "rescanc_handoff_session_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------ #
    # 1. knowledge_graph_nodes                                             #
    # ------------------------------------------------------------------ #
    op.create_table(
        "knowledge_graph_nodes",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "admin_id",
            sa.Text(),
            sa.ForeignKey("admins.id", ondelete="RESTRICT", name="fk_kg_nodes_admin_id"),
            nullable=False,
        ),
        sa.Column(
            "instance_id",
            sa.Integer(),
            sa.ForeignKey(
                "instances.id",
                ondelete="CASCADE",
                name="fk_kg_nodes_instance_id",
            ),
            nullable=False,
        ),
        sa.Column(
            "source_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "knowledge_sources.id",
                ondelete="CASCADE",
                name="fk_kg_nodes_source_id",
            ),
            nullable=False,
        ),
        # Domain-agnostic: inferred at ingest, NOT a fixed ontology.
        sa.Column("node_type", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column(
            "attributes",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa_text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa_text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_knowledge_graph_nodes"),
        comment=(
            "Rescan Tier-C §3.2.1 — domain-agnostic knowledge graph nodes. "
            "node_type is a free-form string inferred at ingest."
        ),
    )
    op.create_index(
        "ix_kg_nodes_tenant",
        "knowledge_graph_nodes",
        ["admin_id", "instance_id"],
    )
    op.create_index(
        "ix_kg_nodes_node_type",
        "knowledge_graph_nodes",
        ["node_type"],
    )
    op.create_index(
        "ix_kg_nodes_label",
        "knowledge_graph_nodes",
        ["label"],
    )

    # ------------------------------------------------------------------ #
    # 2. knowledge_graph_edges                                             #
    # ------------------------------------------------------------------ #
    op.create_table(
        "knowledge_graph_edges",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "admin_id",
            sa.Text(),
            sa.ForeignKey("admins.id", ondelete="RESTRICT", name="fk_kg_edges_admin_id"),
            nullable=False,
        ),
        sa.Column(
            "instance_id",
            sa.Integer(),
            sa.ForeignKey(
                "instances.id",
                ondelete="CASCADE",
                name="fk_kg_edges_instance_id",
            ),
            nullable=False,
        ),
        sa.Column(
            "source_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "knowledge_sources.id",
                ondelete="CASCADE",
                name="fk_kg_edges_source_id",
            ),
            nullable=False,
        ),
        sa.Column(
            "src_node_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "knowledge_graph_nodes.id",
                ondelete="CASCADE",
                name="fk_kg_edges_src_node_id",
            ),
            nullable=False,
        ),
        sa.Column(
            "dst_node_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "knowledge_graph_nodes.id",
                ondelete="CASCADE",
                name="fk_kg_edges_dst_node_id",
            ),
            nullable=False,
        ),
        # Domain-agnostic: e.g. "has_skill", "provides", "requires" — inferred
        sa.Column("edge_type", sa.Text(), nullable=False),
        sa.Column(
            "attributes",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa_text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa_text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_knowledge_graph_edges"),
        comment=(
            "Rescan Tier-C §3.2.1 — directed edges between knowledge graph nodes. "
            "edge_type is a free-form string inferred at ingest."
        ),
    )
    op.create_index(
        "ix_kg_edges_tenant",
        "knowledge_graph_edges",
        ["admin_id", "instance_id"],
    )
    op.create_index(
        "ix_kg_edges_src",
        "knowledge_graph_edges",
        ["src_node_id"],
    )
    op.create_index(
        "ix_kg_edges_dst",
        "knowledge_graph_edges",
        ["dst_node_id"],
    )
    op.create_index(
        "ix_kg_edges_edge_type",
        "knowledge_graph_edges",
        ["edge_type"],
    )

    # ------------------------------------------------------------------ #
    # 3. RLS on knowledge_graph_nodes                                      #
    # ------------------------------------------------------------------ #
    # Mirror knowledge_sources RLS posture: RESTRICTIVE FOR ALL,
    # admin_id::text = current_setting('app.admin_id', true). Fail-closed.
    op.execute(
        "ALTER TABLE knowledge_graph_nodes ENABLE ROW LEVEL SECURITY;"
    )
    op.execute(
        "ALTER TABLE knowledge_graph_nodes FORCE ROW LEVEL SECURITY;"
    )
    op.execute(
        """
        CREATE POLICY kg_nodes_admin_isolation
        ON knowledge_graph_nodes
        AS RESTRICTIVE
        FOR ALL
        TO PUBLIC
        USING (admin_id::text = current_setting('app.admin_id', true))
        WITH CHECK (admin_id::text = current_setting('app.admin_id', true));
        """
    )

    # ------------------------------------------------------------------ #
    # 4. RLS on knowledge_graph_edges                                      #
    # ------------------------------------------------------------------ #
    op.execute(
        "ALTER TABLE knowledge_graph_edges ENABLE ROW LEVEL SECURITY;"
    )
    op.execute(
        "ALTER TABLE knowledge_graph_edges FORCE ROW LEVEL SECURITY;"
    )
    op.execute(
        """
        CREATE POLICY kg_edges_admin_isolation
        ON knowledge_graph_edges
        AS RESTRICTIVE
        FOR ALL
        TO PUBLIC
        USING (admin_id::text = current_setting('app.admin_id', true))
        WITH CHECK (admin_id::text = current_setting('app.admin_id', true));
        """
    )


def downgrade() -> None:
    # Drop in reverse creation order (edges depend on nodes).

    # RLS policies
    op.execute(
        "DROP POLICY IF EXISTS kg_edges_admin_isolation ON knowledge_graph_edges;"
    )
    op.execute(
        "ALTER TABLE knowledge_graph_edges NO FORCE ROW LEVEL SECURITY;"
    )
    op.execute(
        "ALTER TABLE knowledge_graph_edges DISABLE ROW LEVEL SECURITY;"
    )
    op.execute(
        "DROP POLICY IF EXISTS kg_nodes_admin_isolation ON knowledge_graph_nodes;"
    )
    op.execute(
        "ALTER TABLE knowledge_graph_nodes NO FORCE ROW LEVEL SECURITY;"
    )
    op.execute(
        "ALTER TABLE knowledge_graph_nodes DISABLE ROW LEVEL SECURITY;"
    )

    # Tables (drop edges first — FK dependency)
    op.drop_table("knowledge_graph_edges")
    op.drop_table("knowledge_graph_nodes")
