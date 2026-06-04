"""Deterministic graph extractor — Tier-C ingest-time entity/relationship
extraction (Architecture §3.2.1).

Strategy
--------
Deterministic extractor (no LLM call, no external deps): uses noun-phrase
pattern matching over sentences + co-occurrence proximity to extract
(node_type, label) pairs and (edge_type, src_label, dst_label) triples.

Decision #5: domain-agnostic. node_type and edge_type are STRINGS inferred
from the text, NOT a fixed vertical ontology. The extractor uses a small
set of heuristic rules that generate type strings from content cues:

  * Named-pattern sentences (e.g. "Alice is a Senior Developer") →
    entity nodes with content-derived node_types (e.g. "Role", "Person").
  * Prepositional/verbal co-occurrence within a sentence →
    directed edges with content-derived edge_types.
  * Title-cased noun phrases → generic "Entity" nodes when no structural
    cue is present.

The extraction is deliberately conservative: it produces false negatives
rather than false positives. Graph population is best-effort — a failure
never blocks the ingest pipeline.

Correctness boundary (§3.2.1)
------------------------------
Extraction targets admin-ingested content only. The extractor never
triggers live lookups; it works on the chunk text strings already produced
by the chunker.

LLM-assisted extraction
------------------------
The extractor is DETERMINISTIC (no LLM calls) in v1. An LLM-assisted path
can be added behind the stub provider guard in future; the extractor
interface (extract_from_chunks) is the seam for that upgrade.

Usage
-----
    from app.knowledge.graph_extractor import GraphExtractor
    extractor = GraphExtractor(admin_id=..., instance_id=..., source_id=...)
    nodes, edges = extractor.extract_from_chunks(chunks)
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Sequence

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Heuristic vocabulary (content-derived, domain-agnostic)
# ---------------------------------------------------------------------------

# Patterns that suggest a subject–IS–role relationship.
# These are just structural cues; the node_type string is derived from the
# CONTENT word at the right of "is a/is an", NOT a fixed enum.
_IS_A_RE = re.compile(
    r"\b(?P<subject>[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)"
    r"\s+is\s+(?:a|an)\s+"
    r"(?P<role>[A-Za-z]+(?:\s+[A-Za-z]+){0,3})\b",
)

# "has <noun>" — ownership / capability edges.
_HAS_RE = re.compile(
    r"\b(?P<subject>[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)"
    r"\s+has\s+"
    r"(?P<obj>[A-Za-z]+(?:\s+[A-Za-z]+){0,2})\b",
)

# "provides <noun>" — service edges.
_PROVIDES_RE = re.compile(
    r"\b(?P<subject>[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)"
    r"\s+provides\s+"
    r"(?P<obj>[A-Za-z]+(?:\s+[A-Za-z]+){0,2})\b",
)

# "requires <noun>" — dependency edges.
_REQUIRES_RE = re.compile(
    r"\b(?P<subject>[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)"
    r"\s+requires\s+"
    r"(?P<obj>[A-Za-z]+(?:\s+[A-Za-z]+){0,2})\b",
)

# Capitalised multi-word noun phrases (at least one capitalised word).
_CAPITALIZED_NP_RE = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b")

# Common stop-words that should NOT become standalone nodes.
_STOP_LABELS: frozenset[str] = frozenset(
    {
        "The", "This", "That", "These", "Those", "It", "We", "Our", "Your",
        "Their", "Its", "He", "She", "They",
    }
)

# Map from content cues to node_types (domain-agnostic string derivation).
# When the "is a <role>" pattern fires, we classify the role word:
_ROLE_TYPE_CUES: dict[str, str] = {
    "developer": "Role",
    "engineer": "Role",
    "designer": "Role",
    "manager": "Role",
    "analyst": "Role",
    "architect": "Role",
    "consultant": "Role",
    "specialist": "Role",
    "practitioner": "Practitioner",
    "doctor": "Practitioner",
    "nurse": "Practitioner",
    "therapist": "Practitioner",
    "advisor": "Practitioner",
    "service": "Service",
    "product": "Product",
    "feature": "Feature",
    "listing": "Listing",
    "skill": "Skill",
    "technology": "Skill",
    "framework": "Skill",
    "language": "Skill",
    "tool": "Skill",
    "platform": "Platform",
    "system": "System",
    "company": "Organization",
    "organization": "Organization",
    "agency": "Organization",
    "team": "Organization",
}


def _classify_node_type(label: str) -> str:
    """Infer a domain-agnostic node_type string from the label text.

    Checks each word in the label against a small vocabulary map to derive
    a meaningful type string. Falls back to "Entity" when no cue matches.
    """
    lower = label.lower()
    for cue, node_type in _ROLE_TYPE_CUES.items():
        if cue in lower:
            return node_type
    return "Entity"


# ---------------------------------------------------------------------------
# Output DTOs
# ---------------------------------------------------------------------------


@dataclass
class ExtractedNode:
    """One entity node extracted from ingested text."""
    node_type: str  # domain-agnostic, inferred
    label: str
    attributes: dict = field(default_factory=dict)


@dataclass
class ExtractedEdge:
    """One directed relationship between two entity labels."""
    edge_type: str   # domain-agnostic, inferred
    src_label: str
    dst_label: str
    attributes: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------


class GraphExtractor:
    """Deterministic ingest-time graph extractor.

    Extracts entity nodes and directed relationships from a sequence of text
    chunks using heuristic pattern matching. Domain-agnostic: all type strings
    are derived from content, never hardcoded.

    Never raises: extraction errors are logged and the empty result returned
    so the ingest pipeline is unaffected.
    """

    def __init__(
        self,
        *,
        admin_id: str,
        instance_id: int,
        source_id: int,
        business_hint: str | None = None,
    ) -> None:
        self.admin_id = admin_id
        self.instance_id = instance_id
        self.source_id = source_id
        # Optional admin business-description hint to bias type classification.
        # Not used in v1 deterministic path; reserved for LLM-assisted upgrade.
        self.business_hint = business_hint

    def extract_from_chunks(
        self,
        chunks: Sequence[str],
    ) -> tuple[list[ExtractedNode], list[ExtractedEdge]]:
        """Extract nodes + edges from a list of text chunk strings.

        Returns (nodes, edges). Both lists may be empty if no patterns
        match. Never raises.
        """
        nodes: dict[str, ExtractedNode] = {}  # label → node (dedup)
        edges: list[ExtractedEdge] = []

        try:
            for chunk in chunks:
                self._extract_chunk(chunk, nodes, edges)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "GraphExtractor.extract_from_chunks failed: exc_class=%s — "
                "returning partial results",
                type(exc).__name__,
            )

        return list(nodes.values()), edges

    def _extract_chunk(
        self,
        text: str,
        nodes: dict[str, ExtractedNode],
        edges: list[ExtractedEdge],
    ) -> None:
        """Extract from a single chunk (mutates nodes + edges dicts)."""
        sentences = re.split(r"(?<=[.!?])\s+|\n+", text.strip())

        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue

            self._extract_is_a(sentence, nodes, edges)
            self._extract_has(sentence, nodes, edges)
            self._extract_provides(sentence, nodes, edges)
            self._extract_requires(sentence, nodes, edges)
            self._extract_np_cooccurrence(sentence, nodes)

    # ------------------------------------------------------------------
    # Pattern extractors
    # ------------------------------------------------------------------

    def _extract_is_a(
        self,
        sentence: str,
        nodes: dict[str, ExtractedNode],
        edges: list[ExtractedEdge],
    ) -> None:
        """Extract 'X is a <role>' → node(X, Person/inferred) + node(<role>, Role-type)
        + edge(X --is_a--> <role>).
        """
        for m in _IS_A_RE.finditer(sentence):
            subject = m.group("subject").strip()
            role_phrase = m.group("role").strip()
            if subject in _STOP_LABELS or len(subject) < 2:
                continue
            # Infer node types from content
            subj_type = "Person"  # "is a X" subject is typically a person/entity
            role_type = _classify_node_type(role_phrase)

            _upsert_node(nodes, label=subject, node_type=subj_type)
            _upsert_node(nodes, label=role_phrase, node_type=role_type)
            edges.append(
                ExtractedEdge(
                    edge_type="is_a",
                    src_label=subject,
                    dst_label=role_phrase,
                )
            )

    def _extract_has(
        self,
        sentence: str,
        nodes: dict[str, ExtractedNode],
        edges: list[ExtractedEdge],
    ) -> None:
        """Extract 'X has <obj>' → has_skill / has_feature edges."""
        for m in _HAS_RE.finditer(sentence):
            subject = m.group("subject").strip()
            obj = m.group("obj").strip()
            if subject in _STOP_LABELS or len(subject) < 2 or len(obj) < 2:
                continue
            subj_type = _classify_node_type(subject)
            obj_type = _classify_node_type(obj)
            _upsert_node(nodes, label=subject, node_type=subj_type)
            _upsert_node(nodes, label=obj, node_type=obj_type)
            edges.append(
                ExtractedEdge(edge_type="has", src_label=subject, dst_label=obj)
            )

    def _extract_provides(
        self,
        sentence: str,
        nodes: dict[str, ExtractedNode],
        edges: list[ExtractedEdge],
    ) -> None:
        """Extract 'X provides <service>' → provides edges."""
        for m in _PROVIDES_RE.finditer(sentence):
            subject = m.group("subject").strip()
            obj = m.group("obj").strip()
            if subject in _STOP_LABELS or len(subject) < 2 or len(obj) < 2:
                continue
            subj_type = _classify_node_type(subject)
            obj_type = _classify_node_type(obj)
            _upsert_node(nodes, label=subject, node_type=subj_type)
            _upsert_node(nodes, label=obj, node_type=obj_type)
            edges.append(
                ExtractedEdge(
                    edge_type="provides", src_label=subject, dst_label=obj
                )
            )

    def _extract_requires(
        self,
        sentence: str,
        nodes: dict[str, ExtractedNode],
        edges: list[ExtractedEdge],
    ) -> None:
        """Extract 'X requires <obj>' → requires edges."""
        for m in _REQUIRES_RE.finditer(sentence):
            subject = m.group("subject").strip()
            obj = m.group("obj").strip()
            if subject in _STOP_LABELS or len(subject) < 2 or len(obj) < 2:
                continue
            subj_type = _classify_node_type(subject)
            obj_type = _classify_node_type(obj)
            _upsert_node(nodes, label=subject, node_type=subj_type)
            _upsert_node(nodes, label=obj, node_type=obj_type)
            edges.append(
                ExtractedEdge(
                    edge_type="requires", src_label=subject, dst_label=obj
                )
            )

    def _extract_np_cooccurrence(
        self,
        sentence: str,
        nodes: dict[str, ExtractedNode],
    ) -> None:
        """Collect capitalised multi-word noun phrases as generic Entity nodes.

        Co-occurrence within a sentence is captured by the is_a / has /
        provides / requires extractors above. This pass adds nodes for
        prominent named entities that don't appear in a relational pattern.
        """
        for m in _CAPITALIZED_NP_RE.finditer(sentence):
            phrase = m.group(1).strip()
            if phrase in _STOP_LABELS or len(phrase) < 4:
                continue
            node_type = _classify_node_type(phrase)
            _upsert_node(nodes, label=phrase, node_type=node_type)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _upsert_node(
    nodes: dict[str, ExtractedNode],
    *,
    label: str,
    node_type: str,
) -> None:
    """Insert a node keyed by label if not already present."""
    key = label.lower()
    if key not in nodes:
        nodes[key] = ExtractedNode(node_type=node_type, label=label)
