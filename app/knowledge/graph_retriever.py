"""Graph retriever + structured-filter intent detector.

Architecture §3.4.1 RETRIEVE, Decision #6:
    - Structured-filter-intent queries: graph retrieval FIRST, then vector,
      then MERGE.
    - Pure-semantic queries: vector only (graph retriever NOT called).

Intent detection is DETERMINISTIC (no LLM). The detector looks for
structural cues in the query that indicate multi-attribute intersection /
relationship traversal / membership queries:

    Structured-filter intent signals (ANY of these triggers graph path):
        1. Multi-attribute intersection: query contains 2+ attribute-value
           pairs joined by AND/WITH/BOTH/WHO/THAT keywords, or comma
           enumeration of attributes.
        2. Relationship traversal: query contains relationship verbs
           (works with, reports to, has, provides, requires, uses) with
           named entities.
        3. Membership / role queries: query uses IS/ARE/WERE + classifier
           (e.g. "who is a Senior Developer", "find all nurses who").
        4. Explicit enumeration intent: query contains keywords like
           "find all", "list all", "show all", "who has", "which [noun]".

    Pure semantic = any query that does NOT match those patterns.

Path Locked: PostgreSQL recursive CTEs only (Architecture §3.2.1,
Decision #4). No external graph DB.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Sequence

from sqlalchemy.orm import Session

from app.repositories.knowledge_graph_repository import KnowledgeGraphRepository

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Intent detection
# ---------------------------------------------------------------------------

# Relationship traversal verbs
_TRAVERSAL_VERBS_RE = re.compile(
    r"\b(works?\s+with|reports?\s+to|has|have|provides?|requires?|uses?|"
    r"contains?|includes?|belongs?\s+to|assigned?\s+to|managed?\s+by)\b",
    re.IGNORECASE,
)

# Explicit enumeration / filter intent keywords
_ENUM_INTENT_RE = re.compile(
    r"\b(find\s+all|list\s+all|show\s+all|who\s+has|who\s+is|who\s+are|"
    r"which\s+\w+|what\s+\w+\s+has|give\s+me\s+all|what\s+are\s+all)\b",
    re.IGNORECASE,
)

# Multi-attribute conjunction markers
_CONJUNCTION_RE = re.compile(
    r"\b(and|with|both|as\s+well\s+as|along\s+with|plus|also)\b",
    re.IGNORECASE,
)

# Named-entity-like tokens: capitalised 2+ char words that are not at
# sentence start and not common English words.
_NAMED_ENTITY_RE = re.compile(r"\b([A-Z][a-z]{1,}(?:\s+[A-Z][a-z]{1,})*)\b")
_COMMON_CAPS_STOP: frozenset[str] = frozenset(
    {
        "I", "The", "A", "An", "In", "On", "At", "To", "Of", "For", "By",
        "Is", "Are", "Was", "Were", "Be", "Do", "Does", "Did", "Have",
        "Has", "Had", "Can", "Could", "Will", "Would", "Should", "May",
        "Might", "Must", "Shall", "Also", "And", "Or", "But", "Not",
        "With", "From", "That", "This", "These", "Those", "What", "Who",
        "Where", "When", "Why", "How", "Which", "All", "Any", "Each",
        "Every", "Some", "Many", "Most", "Find", "List", "Show", "Give",
        "Tell", "Help", "Please",
    }
)


def detect_structured_filter_intent(query: str) -> bool:
    """Return True if the query has structured-filter intent.

    Decision #6: graph retriever is invoked ONLY when this returns True.
    Pure-semantic queries (no structural cues) bypass the graph entirely.

    Deterministic: no LLM call. Works on query string alone.
    """
    if not query or not query.strip():
        return False

    # Signal 1: explicit enumeration / filter keywords
    if _ENUM_INTENT_RE.search(query):
        return True

    # Signal 2: relationship traversal verbs
    if _TRAVERSAL_VERBS_RE.search(query):
        # Require at least one named-entity-like token alongside the verb
        # to avoid false-positive on "Do you have any questions?"
        candidates = [
            m.group(1)
            for m in _NAMED_ENTITY_RE.finditer(query)
            if m.group(1) not in _COMMON_CAPS_STOP
        ]
        if candidates:
            return True

    # Signal 3: multi-attribute conjunction with 2+ named entities
    conjunctions = len(_CONJUNCTION_RE.findall(query))
    named_entities = [
        m.group(1)
        for m in _NAMED_ENTITY_RE.finditer(query)
        if m.group(1) not in _COMMON_CAPS_STOP
    ]
    if conjunctions >= 1 and len(named_entities) >= 2:
        return True

    return False


def extract_seed_labels(query: str) -> list[str]:
    """Extract candidate seed node labels from a structured-filter query.

    Returns a list of capitalised noun phrases that are likely node labels.
    These are passed as seeds to the recursive CTE traversal.
    """
    seeds = []
    for m in _NAMED_ENTITY_RE.finditer(query):
        label = m.group(1).strip()
        if label not in _COMMON_CAPS_STOP and len(label) >= 2:
            seeds.append(label)
    return seeds


# ---------------------------------------------------------------------------
# Graph retriever
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GraphRetrievedNode:
    """One node returned by the graph retriever."""
    node_id: int
    node_type: str
    label: str
    depth: int
    formatted: str


class GraphRetriever:
    """Retrieve relevant graph nodes for a structured-filter query.

    Uses the KnowledgeGraphRepository's PostgreSQL recursive CTE traversal.
    Never raises — graph retrieval failure degrades to [].
    """

    def __init__(self, db: Session) -> None:
        self.db = db
        self._repo = KnowledgeGraphRepository(db)

    def retrieve(
        self,
        *,
        query: str,
        admin_id: str,
        instance_id: int,
        max_depth: int = 2,
        limit: int = 20,
    ) -> list[GraphRetrievedNode]:
        """Traverse the graph from seeds extracted from the query.

        Returns a list of GraphRetrievedNode in BFS order (closest first).
        Never raises.
        """
        if not query or not query.strip():
            return []

        try:
            seeds = extract_seed_labels(query)
            if not seeds:
                return []

            raw = self._repo.traverse(
                admin_id=admin_id,
                instance_id=instance_id,
                seed_labels=seeds,
                max_depth=max_depth,
                limit=limit,
            )

            results = []
            for r in raw:
                label = r["label"]
                node_type = r["node_type"]
                formatted = f"[graph:{node_type}] {label}"
                results.append(
                    GraphRetrievedNode(
                        node_id=r["node_id"],
                        node_type=node_type,
                        label=label,
                        depth=r["depth"],
                        formatted=formatted,
                    )
                )

            logger.info(
                "GraphRetriever: admin=%s instance=%s seeds=%s → %d nodes",
                admin_id, instance_id, seeds, len(results),
            )
            return results

        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "GraphRetriever.retrieve failed: exc_class=%s — returning []",
                type(exc).__name__,
            )
            return []
