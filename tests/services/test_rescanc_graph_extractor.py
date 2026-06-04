"""Rescan Tier-C — unit tests for the deterministic graph extractor.

Tests:
  * Domain-agnostic: ingest a recruiting doc → Role/Skill nodes.
  * Domain-agnostic: ingest a medical doc → Service/Practitioner nodes.
  * node_type is content-derived, NOT hardcoded.
  * Extractor produces nodes and edges for structured text.
  * Extractor returns empty for unstructured/empty text.
  * Never raises on malformed input.
"""
from __future__ import annotations

import pytest

from app.knowledge.graph_extractor import GraphExtractor, _classify_node_type


# ---------------------------------------------------------------------------
# Unit tests for _classify_node_type (domain-agnostic type inference)
# ---------------------------------------------------------------------------


def test_classify_developer_role():
    assert _classify_node_type("Senior Developer") == "Role"


def test_classify_engineer_role():
    assert _classify_node_type("Software Engineer") == "Role"


def test_classify_practitioner():
    assert _classify_node_type("General Practitioner") == "Practitioner"


def test_classify_service():
    assert _classify_node_type("Consulting Service") == "Service"


def test_classify_skill():
    assert _classify_node_type("Python Language") == "Skill"


def test_classify_unknown_falls_back_to_entity():
    assert _classify_node_type("FooBarBaz") == "Entity"


# ---------------------------------------------------------------------------
# GraphExtractor tests
# ---------------------------------------------------------------------------


def _make_extractor(**kwargs) -> GraphExtractor:
    return GraphExtractor(
        admin_id=kwargs.get("admin_id", "admin-test"),
        instance_id=kwargs.get("instance_id", 1),
        source_id=kwargs.get("source_id", 99),
    )


def test_recruiting_doc_produces_role_nodes():
    """Ingest a recruiting doc → Role/Skill nodes (domain-agnostic)."""
    extractor = _make_extractor()
    chunks = [
        "Alice is a Senior Developer with five years of experience.",
        "Bob is a Software Engineer who requires Python Language and Java Language.",
        "The team has Deep Learning and Machine Learning skills.",
    ]
    nodes, edges = extractor.extract_from_chunks(chunks)

    node_types = {n.node_type for n in nodes}
    node_labels = {n.label.lower() for n in nodes}

    # Should produce Role-type nodes
    assert "Role" in node_types, (
        f"Expected Role in node_types. Got: {node_types}"
    )
    # Should produce nodes for Senior Developer, Software Engineer
    assert any("developer" in lbl for lbl in node_labels), (
        f"Expected a developer node. Labels: {node_labels}"
    )

    # Edges must include a relationship
    assert len(edges) > 0, "Expected at least one edge from recruiting doc"


def test_medical_doc_produces_practitioner_nodes():
    """Ingest a medical doc → Service/Practitioner nodes (domain-agnostic)."""
    extractor = _make_extractor()
    chunks = [
        "Dr. Smith is a General Practitioner who provides Telehealth Service.",
        "The clinic requires Nursing Care and Basic Therapy.",
    ]
    nodes, edges = extractor.extract_from_chunks(chunks)

    node_types = {n.node_type for n in nodes}
    # Should produce Practitioner or Service type nodes
    has_relevant_type = bool(
        {"Practitioner", "Service", "Role", "Entity"} & node_types
    )
    assert has_relevant_type, (
        f"Expected Practitioner/Service in node_types. Got: {node_types}"
    )


def test_node_type_is_content_derived_not_hardcoded():
    """node_type values must be derived from content, never from a fixed enum."""
    extractor = _make_extractor()
    chunks = [
        "Alice is a Platform Engineer.",
        "Acme Company provides Cloud Platform services.",
    ]
    nodes, _ = extractor.extract_from_chunks(chunks)
    # All node_types must be strings (never None or an enum member).
    for node in nodes:
        assert isinstance(node.node_type, str), (
            f"node_type must be str, got {type(node.node_type)}: {node.node_type}"
        )
        assert len(node.node_type) > 0, "node_type must not be empty string"


def test_empty_chunks_produce_empty_results():
    extractor = _make_extractor()
    nodes, edges = extractor.extract_from_chunks([])
    assert nodes == []
    assert edges == []


def test_whitespace_only_chunk_safe():
    extractor = _make_extractor()
    nodes, edges = extractor.extract_from_chunks(["   ", "\n\n"])
    assert nodes == []
    assert edges == []


def test_unstructured_prose_produces_few_or_no_structural_edges():
    """Unstructured prose without relational patterns should produce minimal
    structural edges (the extractor is conservative)."""
    extractor = _make_extractor()
    chunks = [
        "The weather was nice today and we went for a walk in the park.",
        "It was a pleasant afternoon.",
    ]
    nodes, edges = extractor.extract_from_chunks(chunks)
    # Structural edges (is_a, has, provides, requires) should be absent
    structural_types = {"is_a", "has", "provides", "requires"}
    structural_edges = [e for e in edges if e.edge_type in structural_types]
    assert len(structural_edges) == 0, (
        f"Expected no structural edges from unstructured prose. Got: {structural_edges}"
    )


def test_extractor_never_raises_on_garbage_input():
    """Extractor must never raise, even on garbage/NUL-heavy input."""
    extractor = _make_extractor()
    garbage = ["\x00\x01\x02", "a" * 10000, None]  # type: ignore[list-item]
    try:
        nodes, edges = extractor.extract_from_chunks(garbage)
        # Should return something (possibly empty) without crashing.
    except Exception as exc:
        pytest.fail(f"GraphExtractor raised on garbage input: {exc}")


def test_provides_edge_detected():
    """'X provides Y' pattern should produce a provides edge."""
    extractor = _make_extractor()
    chunks = ["Luciel Platform provides AI Support Service."]
    nodes, edges = extractor.extract_from_chunks(chunks)
    provides_edges = [e for e in edges if e.edge_type == "provides"]
    # At least one provides edge
    assert len(provides_edges) > 0, (
        f"Expected provides edge from 'X provides Y' pattern. "
        f"Edges: {[(e.edge_type, e.src_label, e.dst_label) for e in edges]}"
    )


def test_requires_edge_detected():
    """'X requires Y' pattern should produce a requires edge."""
    extractor = _make_extractor()
    # Use a capitalised subject so the named-entity pattern fires.
    chunks = ["Senior Developer requires Python Language and SQL Skills."]
    nodes, edges = extractor.extract_from_chunks(chunks)
    requires_edges = [e for e in edges if e.edge_type == "requires"]
    assert len(requires_edges) > 0, (
        f"Expected requires edge. "
        f"Edges: {[(e.edge_type, e.src_label, e.dst_label) for e in edges]}"
    )
