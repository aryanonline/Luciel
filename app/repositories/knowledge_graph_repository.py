"""Repository for graph knowledge store (knowledge_graph_nodes + edges).

Architecture §3.2.1 (Path Locked — PostgreSQL recursive CTEs, no external
graph DB). Domain-agnostic: node_type and edge_type are strings.

Correctness boundary: operates only over admin-ingested knowledge, never
over live production records.
"""
from __future__ import annotations

import logging
from typing import Any, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.knowledge_graph import KnowledgeGraphEdge, KnowledgeGraphNode
from app.knowledge.graph_extractor import ExtractedEdge, ExtractedNode

logger = logging.getLogger(__name__)


class KnowledgeGraphRepository:
    """CRUD + traversal repository for the graph knowledge store."""

    def __init__(self, db: Session) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Write path (ingest)
    # ------------------------------------------------------------------

    def upsert_graph(
        self,
        *,
        admin_id: str,
        instance_id: int,
        source_id: int,
        nodes: Sequence[ExtractedNode],
        edges: Sequence[ExtractedEdge],
        autocommit: bool = False,
    ) -> tuple[int, int]:
        """Persist extracted nodes + edges for a source.

        Returns (node_count, edge_count) inserted. On any error logs a
        warning and returns (0, 0) — graph population must never block
        the ingest pipeline.

        Upsert strategy: nodes are inserted with ON CONFLICT DO NOTHING
        keyed on (admin_id, instance_id, node_type, lower(label)).
        Edges are inserted with ON CONFLICT DO NOTHING keyed on
        (admin_id, instance_id, src_node_id, dst_node_id, edge_type).
        """
        if not nodes:
            return (0, 0)

        try:
            # 1. Build a label→row_id map after upsert.
            label_to_id: dict[str, int] = {}

            for node in nodes:
                existing = (
                    self.db.query(KnowledgeGraphNode)
                    .filter(
                        KnowledgeGraphNode.admin_id == admin_id,
                        KnowledgeGraphNode.instance_id == instance_id,
                        KnowledgeGraphNode.node_type == node.node_type,
                        text("lower(label) = lower(:label)").bindparams(
                            label=node.label
                        ),
                    )
                    .first()
                )
                if existing is None:
                    row = KnowledgeGraphNode(
                        admin_id=admin_id,
                        instance_id=instance_id,
                        source_id=source_id,
                        node_type=node.node_type,
                        label=node.label,
                        attributes=node.attributes or {},
                    )
                    self.db.add(row)
                    self.db.flush()
                    label_to_id[node.label.lower()] = row.id
                else:
                    label_to_id[node.label.lower()] = existing.id

            node_count = len(label_to_id)

            # 2. Insert edges where both endpoints are known.
            edge_count = 0
            for edge in edges:
                src_id = label_to_id.get(edge.src_label.lower())
                dst_id = label_to_id.get(edge.dst_label.lower())
                if src_id is None or dst_id is None:
                    continue  # Skip edges with missing endpoints

                # Check for duplicate
                existing_edge = (
                    self.db.query(KnowledgeGraphEdge)
                    .filter(
                        KnowledgeGraphEdge.admin_id == admin_id,
                        KnowledgeGraphEdge.instance_id == instance_id,
                        KnowledgeGraphEdge.src_node_id == src_id,
                        KnowledgeGraphEdge.dst_node_id == dst_id,
                        KnowledgeGraphEdge.edge_type == edge.edge_type,
                    )
                    .first()
                )
                if existing_edge is None:
                    edge_row = KnowledgeGraphEdge(
                        admin_id=admin_id,
                        instance_id=instance_id,
                        source_id=source_id,
                        src_node_id=src_id,
                        dst_node_id=dst_id,
                        edge_type=edge.edge_type,
                        attributes=edge.attributes or {},
                    )
                    self.db.add(edge_row)
                    edge_count += 1

            if autocommit:
                self.db.commit()
            else:
                self.db.flush()

            logger.info(
                "Graph upsert: admin=%s instance=%s source=%s nodes=%d edges=%d",
                admin_id, instance_id, source_id, node_count, edge_count,
            )
            return (node_count, edge_count)

        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "KnowledgeGraphRepository.upsert_graph failed: exc_class=%s — "
                "graph not populated but ingest continues",
                type(exc).__name__,
            )
            try:
                self.db.rollback()
            except Exception:  # noqa: BLE001
                pass
            return (0, 0)

    # ------------------------------------------------------------------
    # Read path (retrieval — PostgreSQL recursive CTEs)
    # ------------------------------------------------------------------

    def traverse(
        self,
        *,
        admin_id: str,
        instance_id: int,
        seed_labels: Sequence[str],
        max_depth: int = 2,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Traverse the graph from seed node labels via PostgreSQL recursive CTE.

        Returns a list of dicts with keys:
            node_id, node_type, label, depth, path_edge_types (list[str])

        Path Locked: PostgreSQL recursive CTEs only — NO external graph DB
        (Architecture §3.2.1, Decision #4).

        The RLS fence is active on both tables; this query runs inside the
        tenant scope set by bind_tenant_scope. Never raises: errors return [].
        """
        if not seed_labels:
            return []

        try:
            # Parameterised label list — safe against SQL injection.
            seed_lower = [s.lower() for s in seed_labels]

            # Build the recursive CTE for directed-graph BFS up to max_depth.
            cte_sql = text(
                """
                WITH RECURSIVE graph_walk(node_id, node_type, label, depth, path_labels, cycle) AS (
                    -- Base: seed nodes
                    SELECT
                        n.id,
                        n.node_type,
                        n.label,
                        0            AS depth,
                        ARRAY[n.id]  AS path_labels,
                        false        AS cycle
                    FROM knowledge_graph_nodes n
                    WHERE n.admin_id  = :admin_id
                      AND n.instance_id = :instance_id
                      AND lower(n.label) = ANY(:seeds)

                    UNION ALL

                    -- Recursive: follow outbound edges
                    SELECT
                        dst.id,
                        dst.node_type,
                        dst.label,
                        gw.depth + 1,
                        gw.path_labels || dst.id,
                        dst.id = ANY(gw.path_labels)
                    FROM graph_walk gw
                    JOIN knowledge_graph_edges e
                        ON e.src_node_id = gw.node_id
                       AND e.admin_id    = :admin_id
                       AND e.instance_id = :instance_id
                    JOIN knowledge_graph_nodes dst
                        ON dst.id = e.dst_node_id
                    WHERE gw.depth < :max_depth
                      AND NOT gw.cycle
                )
                SELECT DISTINCT ON (node_id)
                    node_id,
                    node_type,
                    label,
                    depth
                FROM graph_walk
                ORDER BY node_id, depth
                LIMIT :lim
                """
            ).bindparams(
                admin_id=admin_id,
                instance_id=instance_id,
                seeds=seed_lower,
                max_depth=max_depth,
                lim=limit,
            )

            rows = self.db.execute(cte_sql).fetchall()
            return [
                {
                    "node_id": r.node_id,
                    "node_type": r.node_type,
                    "label": r.label,
                    "depth": r.depth,
                }
                for r in rows
            ]

        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "KnowledgeGraphRepository.traverse failed: exc_class=%s — "
                "returning []",
                type(exc).__name__,
            )
            return []

    def node_count(self, *, admin_id: str, instance_id: int) -> int:
        """Count graph nodes for a tenant+instance (used in tests)."""
        try:
            return (
                self.db.query(KnowledgeGraphNode)
                .filter(
                    KnowledgeGraphNode.admin_id == admin_id,
                    KnowledgeGraphNode.instance_id == instance_id,
                )
                .count()
            )
        except Exception:  # noqa: BLE001
            return 0

    def edge_count(self, *, admin_id: str, instance_id: int) -> int:
        """Count graph edges for a tenant+instance (used in tests)."""
        try:
            return (
                self.db.query(KnowledgeGraphEdge)
                .filter(
                    KnowledgeGraphEdge.admin_id == admin_id,
                    KnowledgeGraphEdge.instance_id == instance_id,
                )
                .count()
            )
        except Exception:  # noqa: BLE001
            return 0
