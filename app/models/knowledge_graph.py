"""Knowledge graph models (ARC 16).

The graph store holds the *relational* layer of Luciel's knowledge —
typed entities and the directed relationships between them — extracted
from the admin's ingested knowledge sources at ingest time. It answers
relational / structured-filter questions ("which listings have 3
bedrooms AND are under $1M?", "which practitioners offer both laser and
microneedling?") that pure vector similarity cannot.

Locked decisions (Architecture §3.2.1, Locked Decisions 4–6; Vision
§3.3 "Domain-agnostic entity model"):

  * **PostgreSQL recursive CTEs first** — no Neo4j/Memgraph until a
    measured perf ceiling at ~1M edges. These tables live inside the
    existing Postgres footprint; traversal is recursive-CTE SQL.
  * **Domain-agnostic.** ``entity_type`` and ``relationship_type`` are
    free-text strings *inferred at ingest* from content + the admin's
    business description — NOT enums. A real-estate graph holds
    Listing/Feature nodes; a med-spa graph holds Service/Practitioner;
    an HR graph holds Policy/Department. The schema imposes no vertical
    ontology. Hardcoding an enum here would violate Locked Decision 5.
  * **Correctness boundary (Vision §3.3, Arch §3.2.1).** Nodes/edges are
    derived ONLY from ingested knowledge. Every node and edge carries a
    ``source_id`` back-reference to the ``knowledge_sources`` row that
    asserted it — this is the §3.2.2 trust contract (admins see exactly
    what Luciel reasons on) and the provenance for "based on the
    information you gave me" framing. Live exact-record data stays in
    the ``lookup_property`` tool's data source, never here.

Scoping (Vision §3.3, Architecture §3.7 — same walls as
``knowledge_chunks``):

  * ``admin_id`` (Wall 1) + ``luciel_instance_id`` (Wall 3), both
    NOT NULL on every node and edge.
  * RLS: RESTRICTIVE tenant fence + PERMISSIVE base-grant, both walls,
    installed from birth (see migration arc16_c). These tables never
    carry the deny-all defect that arc16_b fixed on knowledge_sources,
    nor a cross-tenant carve-out (arc16_a) — hard tenant isolation,
    "Across Admins: never."

Lifecycle mirrors ``knowledge_chunks``: ``superseded_at`` (re-ingest of
a source supersedes its old graph rows) and ``soft_deleted_at`` (30-day
deactivation window). Retrieval filters both to NULL.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class KnowledgeGraphNode(Base, TimestampMixin):
    """A typed entity extracted from ingested knowledge.

    Example (real estate): entity_type='Listing', entity_label='123 Main
    St', attributes={'bedrooms': 3, 'price': 950000, 'has_pool': true}.
    The ``attributes`` JSONB holds the structured facts that answer
    multi-attribute filter queries; ``entity_label`` is the canonical
    surface name used for entity resolution at ingest.
    """

    __tablename__ = "knowledge_graph_nodes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # ---- Scope pair (Walls 1 + 3) ----
    admin_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("admins.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    luciel_instance_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "instances.id",
            ondelete="SET NULL",
            name="fk_knowledge_graph_nodes_luciel_instance_id",
        ),
        nullable=False,
        index=True,
    )

    # ---- Domain-agnostic typed entity ----
    entity_type: Mapped[str] = mapped_column(String(120), nullable=False)
    """Inferred entity type (e.g. 'Listing', 'Service', 'Role'). Free
    text inferred at ingest from content + admin business description —
    deliberately NOT an enum (Locked Decision 5: domain-agnostic)."""

    entity_label: Mapped[str] = mapped_column(String(500), nullable=False)
    """Canonical surface name of the entity (e.g. '123 Main St',
    'Microneedling'). Used as the entity-resolution key so re-mentions
    of the same entity collapse onto one node instead of fragmenting."""

    attributes: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    """Structured facts about the entity (e.g. {'bedrooms': 3,
    'price': 950000}). These power multi-attribute filter queries.
    Derived from ingested knowledge only — never a live lookup."""

    # ---- Provenance / trust contract (§3.2.2) ----
    source_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "knowledge_sources.id",
            ondelete="CASCADE",
            name="fk_knowledge_graph_nodes_source_id",
        ),
        nullable=False,
        index=True,
    )
    source: Mapped["KnowledgeSource"] = relationship(  # type: ignore[name-defined]  # noqa: F821
        "KnowledgeSource",
        lazy="select",
        foreign_keys="KnowledgeGraphNode.source_id",
    )

    # ---- Lifecycle (mirrors knowledge_chunks) ----
    superseded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    soft_deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # Entity resolution key: one node per (tenant, instance, type,
        # label). Re-mentions resolve onto the existing node.
        UniqueConstraint(
            "admin_id",
            "luciel_instance_id",
            "entity_type",
            "entity_label",
            name="uq_knowledge_graph_nodes_resolution",
        ),
        Index(
            "ix_knowledge_graph_nodes_scope_type",
            "admin_id",
            "luciel_instance_id",
            "entity_type",
        ),
    )

    @hybrid_property
    def is_active(self) -> bool:
        return self.superseded_at is None and self.soft_deleted_at is None

    @is_active.expression  # type: ignore[no-redef]
    def is_active(cls):  # noqa: N805
        return cls.superseded_at.is_(None) & cls.soft_deleted_at.is_(None)


class KnowledgeGraphEdge(Base, TimestampMixin):
    """A directed, typed relationship between two graph nodes.

    Example (real estate): src=Listing '123 Main St', dst=Neighborhood
    'Riverside', relationship_type='IS_IN'. Directed: a grant of
    src→dst does not imply dst→src.
    """

    __tablename__ = "knowledge_graph_edges"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # ---- Scope pair (Walls 1 + 3) ----
    admin_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("admins.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    luciel_instance_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "instances.id",
            ondelete="SET NULL",
            name="fk_knowledge_graph_edges_luciel_instance_id",
        ),
        nullable=False,
        index=True,
    )

    # ---- Directed edge ----
    src_node_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "knowledge_graph_nodes.id",
            ondelete="CASCADE",
            name="fk_knowledge_graph_edges_src_node_id",
        ),
        nullable=False,
        index=True,
    )
    dst_node_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "knowledge_graph_nodes.id",
            ondelete="CASCADE",
            name="fk_knowledge_graph_edges_dst_node_id",
        ),
        nullable=False,
        index=True,
    )

    relationship_type: Mapped[str] = mapped_column(String(120), nullable=False)
    """Inferred relationship type (e.g. 'IS_IN', 'TREATS', 'REQUIRES').
    Free text inferred at ingest — NOT an enum (domain-agnostic)."""

    # ---- Provenance / trust contract (§3.2.2) ----
    source_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "knowledge_sources.id",
            ondelete="CASCADE",
            name="fk_knowledge_graph_edges_source_id",
        ),
        nullable=False,
        index=True,
    )

    # ---- Lifecycle (mirrors knowledge_chunks) ----
    superseded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    soft_deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # Recursive-CTE traversal indexes: src for forward walks, dst
        # for reverse walks. Scope-prefixed so the planner can prune by
        # tenant+instance before traversing (key to ~1M-edge target).
        Index(
            "ix_knowledge_graph_edges_traverse_src",
            "admin_id",
            "luciel_instance_id",
            "src_node_id",
            "relationship_type",
        ),
        Index(
            "ix_knowledge_graph_edges_traverse_dst",
            "admin_id",
            "luciel_instance_id",
            "dst_node_id",
            "relationship_type",
        ),
        UniqueConstraint(
            "admin_id",
            "luciel_instance_id",
            "src_node_id",
            "dst_node_id",
            "relationship_type",
            name="uq_knowledge_graph_edges_triple",
        ),
    )

    @hybrid_property
    def is_active(self) -> bool:
        return self.superseded_at is None and self.soft_deleted_at is None

    @is_active.expression  # type: ignore[no-redef]
    def is_active(cls):  # noqa: N805
        return cls.superseded_at.is_(None) & cls.soft_deleted_at.is_(None)
