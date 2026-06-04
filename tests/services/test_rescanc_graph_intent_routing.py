"""Rescan Tier-C — unit tests for structured-filter intent detection.

Decision #6: graph retriever invoked ONLY on structured-filter intent.
Pure-semantic queries bypass graph entirely.

Tests:
  * Structured-filter queries route to graph+vector merge.
  * Pure-semantic queries are vector-only (graph retriever NOT called).
  * Intent detector is deterministic (no LLM).
"""
from __future__ import annotations

import pytest

from app.knowledge.graph_retriever import detect_structured_filter_intent, extract_seed_labels


# ---------------------------------------------------------------------------
# detect_structured_filter_intent
# ---------------------------------------------------------------------------


class TestDetectStructuredFilterIntent:

    # --- Should return True (structured-filter intent) ---

    def test_find_all_keyword(self):
        assert detect_structured_filter_intent("find all Senior Developers") is True

    def test_list_all_keyword(self):
        assert detect_structured_filter_intent("list all Practitioners in Cardiology") is True

    def test_show_all_keyword(self):
        assert detect_structured_filter_intent("show all Services available") is True

    def test_who_has_keyword(self):
        assert detect_structured_filter_intent("who has Python Language skills") is True

    def test_who_is_keyword(self):
        assert detect_structured_filter_intent("who is a Senior Developer") is True

    def test_traversal_verb_with_entity(self):
        assert detect_structured_filter_intent(
            "Alice works with Bob on Machine Learning"
        ) is True

    def test_provides_traversal(self):
        assert detect_structured_filter_intent(
            "Which services provides Telehealth Service"
        ) is True

    def test_multi_attribute_conjunction(self):
        assert detect_structured_filter_intent(
            "Developers with Python Language and Java Language"
        ) is True

    def test_requires_traversal(self):
        assert detect_structured_filter_intent(
            "Roles that require Deep Learning and Cloud Platform"
        ) is True

    def test_which_noun_pattern(self):
        assert detect_structured_filter_intent(
            "which Practitioners are available"
        ) is True

    # --- Should return False (pure-semantic queries) ---

    def test_pure_semantic_general_question(self):
        assert detect_structured_filter_intent(
            "What are the benefits of using AI for customer service?"
        ) is False

    def test_pure_semantic_opinion_question(self):
        assert detect_structured_filter_intent(
            "How does machine learning improve outcomes?"
        ) is False

    def test_pure_semantic_greeting(self):
        assert detect_structured_filter_intent("Hello, how are you?") is False

    def test_pure_semantic_open_question(self):
        assert detect_structured_filter_intent(
            "Tell me about your company's approach to customer support."
        ) is False

    def test_empty_string(self):
        assert detect_structured_filter_intent("") is False

    def test_none_returns_false(self):
        # None is not a valid query but should not raise.
        assert detect_structured_filter_intent(None) is False  # type: ignore[arg-type]

    def test_whitespace_only(self):
        assert detect_structured_filter_intent("   ") is False


# ---------------------------------------------------------------------------
# extract_seed_labels
# ---------------------------------------------------------------------------


class TestExtractSeedLabels:

    def test_extracts_capitalised_phrases(self):
        seeds = extract_seed_labels("find all Senior Developers with Python Language")
        # Should extract at least one of these
        labels_lower = [s.lower() for s in seeds]
        assert any("developer" in l or "python" in l or "language" in l for l in labels_lower), (
            f"Expected seed labels. Got: {seeds}"
        )

    def test_does_not_extract_stop_words(self):
        seeds = extract_seed_labels("What is the best approach here?")
        # Stop words like "What", "the" etc. should not appear.
        assert "What" not in seeds
        assert "The" not in seeds

    def test_empty_string_returns_empty(self):
        seeds = extract_seed_labels("")
        assert seeds == []


# ---------------------------------------------------------------------------
# Orchestrator intent routing integration test (unit-level, no DB)
# ---------------------------------------------------------------------------


class TestOrchestratorIntentRouting:
    """Verify that the orchestrator's _retrieve_graph_if_applicable uses
    detect_structured_filter_intent to gate graph retrieval.

    These are unit-level tests: we mock the DB and dependencies to verify
    the routing logic without a real database.
    """

    def _make_req(self, message: str, admin_id: str = "admin1", instance_id: int = 1):
        """Build a minimal RuntimeRequest-like object."""
        from types import SimpleNamespace
        return SimpleNamespace(
            message=message,
            admin_id=admin_id,
            luciel_instance_id=instance_id,
            session_id="sess1",
            user_id="user1",
            channel="widget",
            recent_customer_messages=[],
            customer_requested_channel=None,
        )

    def test_pure_semantic_query_does_not_call_graph_retriever(self):
        """For a pure-semantic query, _retrieve_graph_if_applicable must
        return [] WITHOUT calling GraphRetriever.retrieve."""
        from unittest.mock import MagicMock, patch

        from app.runtime.orchestrator import LucielOrchestrator

        orch = LucielOrchestrator()
        req = self._make_req("Tell me about your services.")

        # Patch detect_structured_filter_intent to return False.
        with patch(
            "app.knowledge.graph_retriever.detect_structured_filter_intent",
            return_value=False,
        ) as mock_detect, patch(
            "app.knowledge.graph_retriever.GraphRetriever.retrieve"
        ) as mock_retrieve:
            db_mock = MagicMock()
            result = orch._retrieve_graph_if_applicable(req=req, db=db_mock)

        # detect was called
        mock_detect.assert_called_once_with(req.message)
        # graph retriever was NOT called (short-circuit on False)
        mock_retrieve.assert_not_called()
        assert result == []

    def test_structured_filter_query_pro_tier_calls_graph_retriever(self):
        """For a structured-filter query on Pro tier, the graph retriever
        MUST be called."""
        from unittest.mock import MagicMock, patch

        from app.runtime.orchestrator import LucielOrchestrator

        orch = LucielOrchestrator()
        req = self._make_req("find all Senior Developers with Python Language")

        fake_graph_results = [MagicMock(formatted="[graph:Role] Senior Developer")]

        with patch(
            "app.knowledge.graph_retriever.detect_structured_filter_intent",
            return_value=True,
        ), patch.object(
            orch, "_resolve_tier", return_value="pro"
        ), patch(
            "app.knowledge.graph_retriever.GraphRetriever.retrieve",
            return_value=fake_graph_results,
        ) as mock_retrieve:
            db_mock = MagicMock()
            result = orch._retrieve_graph_if_applicable(req=req, db=db_mock)

        mock_retrieve.assert_called_once()
        assert result == fake_graph_results

    def test_structured_filter_query_free_tier_skips_graph(self):
        """For a structured-filter query on Free tier, graph retriever must
        NOT be called (knowledge_graph_enabled=False for Free)."""
        from unittest.mock import MagicMock, patch

        from app.runtime.orchestrator import LucielOrchestrator

        orch = LucielOrchestrator()
        req = self._make_req("find all Roles with Python Language")

        with patch(
            "app.knowledge.graph_retriever.detect_structured_filter_intent",
            return_value=True,
        ), patch.object(
            orch, "_resolve_tier", return_value="free"
        ), patch(
            "app.knowledge.graph_retriever.GraphRetriever.retrieve"
        ) as mock_retrieve:
            db_mock = MagicMock()
            result = orch._retrieve_graph_if_applicable(req=req, db=db_mock)

        mock_retrieve.assert_not_called()
        assert result == []
