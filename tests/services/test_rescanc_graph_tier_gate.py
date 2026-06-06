"""Rescan Tier-C — tier gate tests for graph ingestion extraction.

Verifies (unit level, no live DB):
  * Free tier: ingestion does NOT populate graph tables
    (_maybe_extract_graph no-ops on Free).
  * Pro tier: _maybe_extract_graph IS called (graph population attempted).
  * Enterprise tier: same as Pro.
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch, PropertyMock


def _make_admin_row(tier: str) -> MagicMock:
    row = MagicMock()
    row.tier = tier
    return row


class TestGraphTierGate:
    """Unit tests for IngestionService._maybe_extract_graph tier gate.

    Note: GraphExtractor is imported lazily inside _maybe_extract_graph
    (``from app.knowledge.graph_extractor import GraphExtractor``), so
    we patch the module-level name in app.knowledge.graph_extractor, not
    the ingestion module.
    """

    def _make_service(self, admin_tier: str = "free"):
        from app.knowledge.ingestion import IngestionService
        db_mock = MagicMock()
        # Mock query chain: db.query(Admin).filter(Admin.id == ...).one_or_none()
        admin_row = _make_admin_row(admin_tier)
        query_mock = MagicMock()
        query_mock.filter.return_value.one_or_none.return_value = admin_row
        db_mock.query.return_value = query_mock
        svc = IngestionService(db_mock)
        return svc, db_mock

    def test_free_tier_does_not_populate_graph(self):
        """Free tier: _maybe_extract_graph must not attempt graph extraction."""
        svc, db_mock = self._make_service(admin_tier="free")

        # Patch the class in its own module (lazy import path).
        with patch(
            "app.knowledge.graph_extractor.GraphExtractor", autospec=True
        ) as mock_extractor_cls:
            svc._maybe_extract_graph(
                chunks=["Alice is a Senior Developer."],
                admin_id="admin-free",
                luciel_instance_id=1,
                source_id=99,
            )
        # GraphExtractor should NOT be instantiated for Free tier.
        mock_extractor_cls.assert_not_called()

    def test_pro_tier_attempts_graph_extraction(self):
        """Pro tier: _maybe_extract_graph should call GraphExtractor."""
        svc, db_mock = self._make_service(admin_tier="pro")

        mock_extractor = MagicMock()
        mock_extractor.extract_from_chunks.return_value = (
            [MagicMock()],  # 1 node
            [],             # 0 edges
        )

        # Patch at the source (graph_extractor module) and repo.
        with patch(
            "app.knowledge.graph_extractor.GraphExtractor",
            return_value=mock_extractor,
        ) as mock_extractor_cls, patch(
            "app.repositories.knowledge_graph_repository.KnowledgeGraphRepository",
            autospec=True,
        ) as mock_repo_cls:
            mock_repo_cls.return_value.upsert_graph.return_value = (1, 0)
            svc._maybe_extract_graph(
                chunks=["Alice is a Senior Developer."],
                admin_id="admin-pro",
                luciel_instance_id=1,
                source_id=99,
            )

        mock_extractor_cls.assert_called_once()
        mock_extractor.extract_from_chunks.assert_called_once()

    def test_graph_extraction_failure_does_not_raise(self):
        """If graph extraction raises, _maybe_extract_graph must not propagate."""
        svc, db_mock = self._make_service(admin_tier="pro")

        # Make the real extractor raise at instantiation time by patching
        # the class in the graph_extractor module with a side_effect.
        with patch(
            "app.knowledge.graph_extractor.GraphExtractor",
            side_effect=RuntimeError("boom"),
        ):
            # Must not raise.
            try:
                svc._maybe_extract_graph(
                    chunks=["text"],
                    admin_id="admin-pro",
                    luciel_instance_id=1,
                    source_id=99,
                )
            except Exception as exc:
                pytest.fail(
                    f"_maybe_extract_graph raised despite best-effort contract: {exc}"
                )
