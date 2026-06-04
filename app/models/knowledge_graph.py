"""ORM models for the Tier-C graph knowledge store.

Implements Architecture §3.2.1 (Path Locked — PostgreSQL recursive CTEs,
no external graph DB). Domain-agnostic: node_type and edge_type are strings
inferred at ingest (Decision #5), NOT a fixed vertical ontology.

Tables:
    knowledge_graph_nodes  — extracted entities from ingested knowledge
    knowledge_graph_edges  — directed relationships between nodes

RLS:
    Both tables carry the strict-tenant RESTRICTIVE policy installed by the
    rescanc_graph_kb migration, mirroring knowledge_sources. Row reads are
    fenced to admin_id::text = current_setting('app.admin_id', true).

Correctness boundary (§3.2.1):
    Graph operates only over admin-ingested knowledge — it never serves live
    exact-record lookups. The lookup_record tool owns that path.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class KnowledgeGraphNode(Base):
    """A domain-agnostic entity node extracted from ingested knowledge.

    node_type is a free-form string such as "Role", "Skill", "Service",
    "Practitioner", "Listing", or "Feature" — inferred from content at
    ingest time, never hardcoded (Architecture §3.2.1, Decision #5).
    """

    __tablename__ = "knowledge_graph_nodes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # Tenant scope — mandatory (both NOT NULL).
    admin_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("admins.id", ondelete="RESTRICT", name="fk_kg_nodes_admin_id"),
        nullable=False,
        index=True,
    )
    instance_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("instances.id", ondelete="CASCADE", name="fk_kg_nodes_instance_id"),
        nullable=False,
    )

    # Source FK — which knowledge_source this node was extracted from.
    source_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "knowledge_sources.id",
            ondelete="CASCADE",
            name="fk_kg_nodes_source_id",
        ),
        nullable=False,
        index=True,
    )

    # Domain-agnostic type label — inferred at ingest.
    node_type: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    label: Mapped[str] = mapped_column(Text, nullable=False, index=True)

    # Arbitrary metadata (synonyms, confidence, position, etc.)
    attributes: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    # Relationships
    outbound_edges: Mapped[list["KnowledgeGraphEdge"]] = relationship(
        "KnowledgeGraphEdge",
        back_populates="src_node",
        foreign_keys="KnowledgeGraphEdge.src_node_id",
        cascade="all, delete-orphan",
        lazy="select",
    )
    inbound_edges: Mapped[list["KnowledgeGraphEdge"]] = relationship(
        "KnowledgeGraphEdge",
        back_populates="dst_node",
        foreign_keys="KnowledgeGraphEdge.dst_node_id",
        cascade="all, delete-orphan",
        lazy="select",
    )

    __table_args__ = (
        Index("ix_kg_nodes_tenant", "admin_id", "instance_id"),
    )


class KnowledgeGraphEdge(Base):
    """A directed relationship between two knowledge graph nodes.

    edge_type is a free-form string such as "has_skill", "provides",
    "requires", "lists_feature" — inferred from content at ingest time,
    never hardcoded (Architecture §3.2.1, Decision #5).
    """

    __tablename__ = "knowledge_graph_edges"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # Tenant scope — mandatory (both NOT NULL).
    admin_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("admins.id", ondelete="RESTRICT", name="fk_kg_edges_admin_id"),
        nullable=False,
        index=True,
    )
    instance_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("instances.id", ondelete="CASCADE", name="fk_kg_edges_instance_id"),
        nullable=False,
    )

    # Source FK
    source_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "knowledge_sources.id",
            ondelete="CASCADE",
            name="fk_kg_edges_source_id",
        ),
        nullable=False,
        index=True,
    )

    # Directed node pair
    src_node_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "knowledge_graph_nodes.id",
            ondelete="CASCADE",
            name="fk_kg_edges_src_node_id",
        ),
        nullable=False,
        index=True,
    )
    dst_node_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "knowledge_graph_nodes.id",
            ondelete="CASCADE",
            name="fk_kg_edges_dst_node_id",
        ),
        nullable=False,
        index=True,
    )

    # Domain-agnostic relationship type — inferred at ingest.
    edge_type: Mapped[str] = mapped_column(Text, nullable=False, index=True)

    attributes: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    # Relationships
    src_node: Mapped["KnowledgeGraphNode"] = relationship(
        "KnowledgeGraphNode",
        back_populates="outbound_edges",
        foreign_keys="KnowledgeGraphEdge.src_node_id",
        lazy="select",
    )
    dst_node: Mapped["KnowledgeGraphNode"] = relationship(
        "KnowledgeGraphNode",
        back_populates="inbound_edges",
        foreign_keys="KnowledgeGraphEdge.dst_node_id",
        lazy="select",
    )

    __table_args__ = (
        Index("ix_kg_edges_tenant", "admin_id", "instance_id"),
    )
