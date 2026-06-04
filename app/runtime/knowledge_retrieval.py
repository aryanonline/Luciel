"""Hybrid knowledge retrieval (ARC 16).

The runtime RETRIEVE step (Architecture §3.4.1). Routes a query through:

    PLAN-phase intent check
        ├─ structured-filter intent  → GRAPH filter → VECTOR → MERGE
        └─ pure semantic             → VECTOR only (graph bypassed)

This is the architecture-named artifact ``app/runtime/knowledge_retrieval.py``
(Locked Decisions index). It is the *consumption* side of the graph
store; ingestion/population is the extraction pipeline (separate unit).

Locked behaviour (Architecture §3.2.1, Locked Decision 6):
  * The graph is invoked ONLY when the query has structured-filter
    intent — multi-attribute intersection, relationship traversal, or
    membership check. "Mixing graph results into a pure-semantic query
    introduces retrieval noise without accuracy benefit", so pure
    semantic queries bypass the graph entirely and go straight to
    vector. This is the fallback lane (Arc 11 vector path), preserved
    intact.
  * Both stores operate ONLY over ingested knowledge (correctness
    boundary). Live records are tool-served, never from here.
  * Never raises. Any failure in the graph stage falls through to
    vector-only; any failure in vector returns []. Retrieval failure
    must not block the conversation (Architecture §3.4) — and a graph
    failure must never be worse than the pre-ARC-16 vector-only path.

Output contract: a ``list[RetrievedChunk]`` — the SAME shape the
orchestrator already consumes (``.formatted`` for context assembly,
``.distance`` for grounding, the scope triple for isolation). The graph
stage influences WHICH chunks are returned (by surfacing the sources its
matched entities came from); it does not introduce a parallel node
format the context assembler would not understand. Merged context =
chunks, graph-informed.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.knowledge.retriever import KnowledgeRetriever, RetrievedChunk
from app.repositories.knowledge_graph_repository import (
    KnowledgeGraphRepository,
)
from app.repositories.knowledge_repository import KnowledgeRepository

logger = logging.getLogger(__name__)


# Heuristic markers of structured-filter intent. The architecture frames
# this as a PLAN-phase decision; v1 uses a deterministic classifier (no
# LLM dependency, fully testable). It is intentionally conservative —
# when in doubt it returns False, so the system defaults to the proven
# vector-only path rather than speculatively engaging the graph.
_MULTI_ATTR = re.compile(
    r"\b(and|both|also|as well as|plus)\b", re.IGNORECASE
)
_RANGE_NUM = re.compile(
    r"(under|over|less than|more than|at least|at most|between|below|above"
    r"|cheaper than|<=?|>=?)\s*\$?\d", re.IGNORECASE
)
_RELATIONAL = re.compile(
    r"\b(which|that have|with both|offer both|require both|in the same"
    r"|related to|connected to|part of|belongs? to)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class HybridResult:
    """Retrieval result + provenance of how it was produced.

    ``graph_engaged`` lets the verification surface (admin raw view /
    internal retrieve endpoint) report whether the graph filter actually
    fired for a given query — part of the trust contract (§3.2.2).
    """

    chunks: list[RetrievedChunk]
    graph_engaged: bool
    graph_node_count: int = 0


def has_structured_filter_intent(query: str) -> bool:
    """True when the query looks like a multi-attribute intersection,
    relationship traversal, or membership check.

    Conservative: requires a relatively explicit structured signal.
    Pure semantic questions ("what's your refund policy?", "does this
    listing have a pool?") return False and route to vector-only.
    """
    if not query or not query.strip():
        return False
    signals = 0
    if _MULTI_ATTR.search(query):
        signals += 1
    if _RANGE_NUM.search(query):
        signals += 1
    if _RELATIONAL.search(query):
        signals += 1
    # Require at least two distinct structured signals OR an explicit
    # numeric range (the strongest single signal). A lone "and" is not
    # enough — that appears in plenty of pure-semantic questions.
    return signals >= 2 or bool(_RANGE_NUM.search(query))


class HybridRetriever:
    """Graph-then-vector hybrid retrieval over a tenant-scoped session.

    Construct with an already-tenant-scoped SQLAlchemy session (the
    orchestrator opens it via ``bind_tenant_scope``). Both the vector
    and graph repositories run on that session, so RLS applies to both.
    """

    def __init__(self, session) -> None:  # noqa: ANN001
        self._session = session
        self._vector = KnowledgeRetriever(KnowledgeRepository(session))
        self._graph = KnowledgeGraphRepository(session)

    def retrieve(
        self,
        *,
        query: str,
        admin_id: str,
        luciel_instance_id: int,
        limit: int = 5,
    ) -> HybridResult:
        """Hybrid retrieve. Never raises.

        Path:
          1. If the query has no structured-filter intent → vector only.
          2. Else: run the graph to find relevant entities, collect the
             knowledge sources those entities came from, run vector
             search, and re-order so chunks from graph-matched sources
             come first (graph-informed merge). If the graph stage
             yields nothing or errors, fall through to the same
             vector-only result — never worse than Arc 11.
        """
        # --- vector lane (always computed; it is the base + fallback) ---
        try:
            vector_chunks = self._vector.retrieve_with_sources(
                query=query,
                admin_id=admin_id,
                luciel_instance_id=luciel_instance_id,
                limit=limit,
            )
        except Exception as exc:  # noqa: BLE001 — never-raise contract
            logger.warning(
                "Hybrid: vector lane failed exc_class=%s — returning []",
                type(exc).__name__,
            )
            return HybridResult(chunks=[], graph_engaged=False)

        if not has_structured_filter_intent(query):
            # Pure semantic: bypass graph entirely (Locked Decision 6).
            return HybridResult(chunks=vector_chunks, graph_engaged=False)

        # --- graph lane (structured-filter intent detected) ---
        graph_source_ids: set[int] = set()
        graph_node_count = 0
        try:
            nodes = self._graph.find_nodes(
                admin_id=admin_id,
                luciel_instance_id=luciel_instance_id,
                limit=max(limit * 4, 20),
            )
            graph_node_count = len(nodes)
            if nodes:
                # Expand one hop so related entities' sources are also
                # surfaced (relationship-traversal intent).
                seed_ids = [n.id for n in nodes]
                reached = self._graph.traverse(
                    admin_id=admin_id,
                    luciel_instance_id=luciel_instance_id,
                    seed_node_ids=seed_ids,
                    max_depth=2,
                    limit=max(limit * 8, 40),
                )
                for n in list(nodes) + list(reached):
                    graph_source_ids.add(n.source_id)
        except Exception as exc:  # noqa: BLE001 — graph never worsens vector
            logger.warning(
                "Hybrid: graph lane failed exc_class=%s — vector-only "
                "fallback",
                type(exc).__name__,
            )
            return HybridResult(chunks=vector_chunks, graph_engaged=False)

        if not graph_source_ids:
            # Graph engaged but matched no entities → vector-only result,
            # but record that we *tried* the graph (graph_engaged True).
            return HybridResult(
                chunks=vector_chunks,
                graph_engaged=True,
                graph_node_count=graph_node_count,
            )

        # --- merge: chunks whose source the graph matched come first ---
        graph_first: list[RetrievedChunk] = []
        rest: list[RetrievedChunk] = []
        for c in vector_chunks:
            if c.source_identifier in graph_source_ids:
                graph_first.append(c)
            else:
                rest.append(c)
        merged = (graph_first + rest)[:limit]
        return HybridResult(
            chunks=merged,
            graph_engaged=True,
            graph_node_count=graph_node_count,
        )
