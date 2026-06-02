"""Knowledge graph repository — recursive-CTE traversal (ARC 16).

The graph query layer for hybrid retrieval. Two capabilities the runtime
needs at the RETRIEVE step (Architecture §3.4.1) when the PLAN phase
detects structured-filter intent:

  1. ``find_nodes`` — multi-attribute / membership filter over typed
     entities (answers "which Listings have bedrooms>=3 AND price<1M").
  2. ``traverse`` — recursive-CTE relationship walk from a seed set
     (answers "which Neighborhoods are these Listings IN", and
     multi-hop chains) up to a bounded depth.

Design constraints (locked):
  * PostgreSQL recursive CTEs only — no graph vendor (§3.2.1).
  * Depth-bounded + visited-set cycle guard, so a cyclic graph cannot
    spin the CTE forever. The bound is a cost ceiling, not a behavioural
    cap (same doctrine as the 5-iteration loop bound, §3.4.1).
  * Tenant isolation is enforced by RLS (the bound session sets
    app.admin_id + app.instance_id). Every query ALSO carries explicit
    admin_id/instance_id predicates as defence-in-depth (the §3.7
    three-layer model: app-filter + RLS + per-request GUC), so a
    mis-bound session still cannot cross tenants.
  * Active-only: superseded_at IS NULL AND soft_deleted_at IS NULL,
    mirroring the vector retriever's lifecycle filter.

This layer never embeds or returns live tool data — it operates only
over nodes/edges derived from ingested knowledge (correctness boundary,
§3.2.1).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Cost ceiling on recursive traversal depth. Sized so legitimate
# relational questions (1–3 hops) are unconstrained while a pathological
# or cyclic graph cannot run away. Parallel to the 5-iteration loop bound.
MAX_TRAVERSAL_DEPTH = 5


@dataclass(frozen=True)
class GraphNode:
    """A graph node returned to the hybrid retriever, carrying its full
    scope triple (admin_id, instance_id, source_id) — same retrieval
    contract as RetrievedChunk (ARC 16 #4)."""

    id: int
    entity_type: str
    entity_label: str
    attributes: dict[str, Any] | None
    admin_id: str
    luciel_instance_id: int
    source_id: int
    depth: int = 0


class KnowledgeGraphRepository:
    """Recursive-CTE queries over the knowledge graph store."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Node filter — multi-attribute / type / membership.
    # ------------------------------------------------------------------
    def find_nodes(
        self,
        *,
        admin_id: str,
        luciel_instance_id: int,
        entity_type: str | None = None,
        attribute_filters: dict[str, Any] | None = None,
        limit: int = 50,
    ) -> list[GraphNode]:
        """Return active nodes matching type + attribute predicates.

        ``attribute_filters`` is matched against the JSONB ``attributes``
        column. Scalar values use containment (``@>``); for range
        queries the caller composes them via ``find_nodes`` + post-filter
        or a dedicated method — v1 supports equality/containment, which
        covers membership and exact-attribute intersection.
        """
        clauses = [
            "admin_id = :admin_id",
            "luciel_instance_id = :instance_id",
            "superseded_at IS NULL",
            "soft_deleted_at IS NULL",
        ]
        params: dict[str, Any] = {
            "admin_id": admin_id,
            "instance_id": luciel_instance_id,
            "limit": limit,
        }
        if entity_type is not None:
            clauses.append("entity_type = :entity_type")
            params["entity_type"] = entity_type
        if attribute_filters:
            # JSONB containment: attributes @> :attrs
            import json

            clauses.append("attributes @> :attrs ::jsonb")
            params["attrs"] = json.dumps(attribute_filters)

        sql = text(
            f"""
            SELECT id, entity_type, entity_label, attributes,
                   admin_id, luciel_instance_id, source_id
              FROM knowledge_graph_nodes
             WHERE {' AND '.join(clauses)}
             ORDER BY id
             LIMIT :limit
            """
        )
        rows = self._session.execute(sql, params).mappings().all()
        return [
            GraphNode(
                id=r["id"],
                entity_type=r["entity_type"],
                entity_label=r["entity_label"],
                attributes=r["attributes"],
                admin_id=r["admin_id"],
                luciel_instance_id=r["luciel_instance_id"],
                source_id=r["source_id"],
                depth=0,
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Recursive traversal from a seed set of node ids.
    # ------------------------------------------------------------------
    def traverse(
        self,
        *,
        admin_id: str,
        luciel_instance_id: int,
        seed_node_ids: list[int],
        relationship_type: str | None = None,
        max_depth: int = 2,
        limit: int = 100,
    ) -> list[GraphNode]:
        """Walk directed edges forward from ``seed_node_ids`` up to
        ``max_depth`` hops, returning the reachable nodes.

        Implemented as a PostgreSQL recursive CTE with:
          * a depth counter bounded by ``max_depth`` (clamped to
            MAX_TRAVERSAL_DEPTH),
          * a visited-path cycle guard (the node id is accumulated into
            an int[] path; an edge to an already-visited node is not
            re-expanded),
          * tenant+instance predicates on BOTH the edge and the
            destination node at every level (defence-in-depth atop RLS).
        """
        if not seed_node_ids:
            return []
        depth = max(1, min(max_depth, MAX_TRAVERSAL_DEPTH))

        params: dict[str, Any] = {
            "admin_id": admin_id,
            "instance_id": luciel_instance_id,
            "seed_ids": seed_node_ids,
            "max_depth": depth,
            "limit": limit,
        }
        rel_edge_clause = ""
        if relationship_type is not None:
            rel_edge_clause = "AND e.relationship_type = :rel_type"
            params["rel_type"] = relationship_type

        # NOTE: edges and nodes are both tenant/instance filtered inside
        # the CTE. The visited-path guard uses an int[] accumulator;
        # ``NOT (n.id = ANY(path))`` prevents re-expanding a cycle.
        sql = text(
            f"""
            WITH RECURSIVE walk AS (
                -- base: seed nodes at depth 0
                SELECT n.id,
                       n.entity_type,
                       n.entity_label,
                       n.attributes,
                       n.admin_id,
                       n.luciel_instance_id,
                       n.source_id,
                       0 AS depth,
                       ARRAY[n.id] AS path
                  FROM knowledge_graph_nodes n
                 WHERE n.id = ANY(:seed_ids)
                   AND n.admin_id = :admin_id
                   AND n.luciel_instance_id = :instance_id
                   AND n.superseded_at IS NULL
                   AND n.soft_deleted_at IS NULL

                UNION ALL

                -- step: follow active edges to active dst nodes
                SELECT dst.id,
                       dst.entity_type,
                       dst.entity_label,
                       dst.attributes,
                       dst.admin_id,
                       dst.luciel_instance_id,
                       dst.source_id,
                       walk.depth + 1 AS depth,
                       walk.path || dst.id AS path
                  FROM walk
                  JOIN knowledge_graph_edges e
                    ON e.src_node_id = walk.id
                   AND e.admin_id = :admin_id
                   AND e.luciel_instance_id = :instance_id
                   AND e.superseded_at IS NULL
                   AND e.soft_deleted_at IS NULL
                   {rel_edge_clause}
                  JOIN knowledge_graph_nodes dst
                    ON dst.id = e.dst_node_id
                   AND dst.admin_id = :admin_id
                   AND dst.luciel_instance_id = :instance_id
                   AND dst.superseded_at IS NULL
                   AND dst.soft_deleted_at IS NULL
                 WHERE walk.depth < :max_depth
                   AND NOT (dst.id = ANY(walk.path))   -- cycle guard
            )
            SELECT DISTINCT ON (id)
                   id, entity_type, entity_label, attributes,
                   admin_id, luciel_instance_id, source_id, depth
              FROM walk
             ORDER BY id, depth
             LIMIT :limit
            """
        )
        rows = self._session.execute(sql, params).mappings().all()
        return [
            GraphNode(
                id=r["id"],
                entity_type=r["entity_type"],
                entity_label=r["entity_label"],
                attributes=r["attributes"],
                admin_id=r["admin_id"],
                luciel_instance_id=r["luciel_instance_id"],
                source_id=r["source_id"],
                depth=r["depth"],
            )
            for r in rows
        ]
