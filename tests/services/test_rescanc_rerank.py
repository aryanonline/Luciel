"""Rescan Tier-C — unit tests for the deterministic rerank stage.

Tests (§3.2 / §3.5):
  * Rerank changes ordering as designed (recency/ordinal).
  * Recent source (higher source_id) promoted over older source.
  * Earlier chunk (lower chunk_id) promoted within same source.
  * Vector-only path still returns top-k after reranking.
  * Rerank never raises (returns input on failure).
  * Empty input returns empty.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from app.knowledge.reranker import rerank


@dataclass
class _FakeChunk:
    chunk_id: int
    source_identifier: int
    distance: Optional[float]
    content: str = "test content"


def _chunks(*specs) -> list[_FakeChunk]:
    """Create fake chunks from (chunk_id, source_id, distance) tuples."""
    return [_FakeChunk(chunk_id=c, source_identifier=s, distance=d) for c, s, d in specs]


class TestRerank:

    def test_empty_input_returns_empty(self):
        assert rerank([]) == []

    def test_single_chunk_returns_unchanged(self):
        chunks = _chunks((1, 1, 0.1))
        result = rerank(chunks)
        assert result == chunks

    def test_recent_source_promoted_over_older_source(self):
        """A chunk from a newer source (higher source_id) should rank
        higher than a chunk with worse recency, all else equal.

        Setup: two chunks, same distance, but different source_ids.
        The one with higher source_id is more recent.
        """
        # chunk 1: source 100 (older), distance 0.2
        # chunk 2: source 200 (newer), distance 0.2 — same vector relevance
        chunks = _chunks(
            (1, 100, 0.2),   # older source
            (2, 200, 0.2),   # newer source
        )
        result = rerank(chunks)
        # Newer source (source_id=200) should be first.
        assert result[0].source_identifier == 200, (
            f"Expected newer source first. Got source_ids: "
            f"{[c.source_identifier for c in result]}"
        )

    def test_earlier_chunk_promoted_within_same_source(self):
        """Within the same source, a chunk with a lower chunk_id
        (earlier in the document) should rank higher, all else equal.
        """
        # Both from source 100, same distance, different chunk_ids.
        chunks = _chunks(
            (50, 100, 0.2),   # later chunk (higher id)
            (10, 100, 0.2),   # earlier chunk (lower id)
        )
        result = rerank(chunks)
        # Earlier chunk (chunk_id=10) should be first.
        assert result[0].chunk_id == 10, (
            f"Expected earlier chunk first. Got chunk_ids: "
            f"{[c.chunk_id for c in result]}"
        )

    def test_top_k_truncates_result(self):
        """top_k parameter must truncate the output."""
        chunks = _chunks(
            (1, 100, 0.1),
            (2, 200, 0.2),
            (3, 300, 0.3),
            (4, 400, 0.4),
            (5, 500, 0.5),
        )
        result = rerank(chunks, top_k=3)
        assert len(result) == 3

    def test_vector_relevance_primary_signal(self):
        """A very high vector relevance (distance≈0) should still dominate
        over an older source with poor relevance."""
        # Chunk A: newest source, poor vector relevance (high distance).
        # Chunk B: oldest source, excellent vector relevance (distance≈0).
        # Chunk B should rank higher due to dominant vector score.
        chunks = _chunks(
            (1, 999, 1.8),   # newest but terrible vector match
            (2, 1,   0.01),  # oldest but near-perfect vector match
        )
        result = rerank(chunks, top_k=2)
        # Chunk B (chunk_id=2, near-perfect match) should rank first.
        assert result[0].chunk_id == 2, (
            f"Expected near-perfect vector match first. Got chunk_ids: "
            f"{[c.chunk_id for c in result]}"
        )

    def test_none_distance_treated_as_worst_relevance(self):
        """Chunks with distance=None should be treated as worst relevance
        (not crash the reranker)."""
        chunks = _chunks(
            (1, 100, None),   # no distance
            (2, 200, 0.1),    # good match
        )
        result = rerank(chunks)
        # The chunk with a real distance should rank higher.
        assert result[0].chunk_id == 2, (
            f"Expected chunk with distance to rank higher. Got: "
            f"{[c.chunk_id for c in result]}"
        )

    def test_rerank_changes_hnsw_order(self):
        """Rerank must change ordering relative to pure HNSW distance.

        Setup: HNSW returns [chunk A (distance 0.1, old source),
                              chunk B (distance 0.15, new source)].
        After reranking, chunk B (newer) may rank above A despite
        slightly worse vector relevance.
        """
        chunks = _chunks(
            (1, 1, 0.1),    # HNSW top result, old source
            (2, 100, 0.15), # HNSW second, new source
        )
        # With w_recency=0.4, w_vector=0.3, w_ordinal=0.3:
        # chunk 1: vector=0.95, recency=0.0, ordinal=1.0 → 0.3*0.95+0.4*0.0+0.3*1.0 = 0.585
        # chunk 2: vector=0.925, recency=1.0, ordinal=1.0 → 0.3*0.925+0.4*1.0+0.3*1.0 = 0.278+0.4+0.3 = 0.978
        # So chunk 2 (newer) ranks above chunk 1 even with slightly worse distance.
        result = rerank(chunks)
        assert result[0].source_identifier == 100, (
            f"Expected newer source to be reranked to top. "
            f"Got source_ids: {[c.source_identifier for c in result]}"
        )

    def test_rerank_never_raises_on_bad_input(self):
        """rerank must never raise, even on broken accessors."""
        broken_chunks = [object(), object()]  # no .source_identifier etc.

        # With default accessors these will fail; result should be input order.
        result = rerank(
            broken_chunks,
            get_source_id=lambda c: (_ for _ in ()).throw(ValueError("boom")),
            get_chunk_id=lambda c: 1,
            get_distance=lambda c: 0.1,
        )
        assert len(result) == len(broken_chunks)

    def test_custom_accessors_work(self):
        """rerank should work with custom accessor callables."""
        class MyChunk:
            def __init__(self, sid, cid, dist):
                self.sid = sid
                self.cid = cid
                self.dist = dist

        chunks = [MyChunk(1, 10, 0.5), MyChunk(2, 5, 0.2)]
        result = rerank(
            chunks,
            get_source_id=lambda c: c.sid,
            get_chunk_id=lambda c: c.cid,
            get_distance=lambda c: c.dist,
        )
        # Should not crash and should return 2 items.
        assert len(result) == 2
