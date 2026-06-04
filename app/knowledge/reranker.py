"""Deterministic rerank stage for vector retrieval (Architecture §3.2, §3.5).

This rerank stage sits between the HNSW vector search and the top-k cutoff
(§3.2 retrieval flow step 3). It applies to vector retrieval on ALL tiers
(NOT graph-gated). It is deterministic — no LLM call.

Rerank method
-------------
Two-component weighted score, higher is better:

    rerank_score = w_recency * recency_score + w_ordinal * ordinal_score

where:

  recency_score   = normalised chunk age. Chunks ingested more recently
                    score higher. Computed as:
                        1.0 - (rank_by_source_age / n_sources)
                    where rank_by_source_age is the 0-based ordinal of the
                    chunk's source_id among the retrieved set sorted by
                    source_id DESC (higher source_id = more recently
                    created source row, since source PKs are monotonically
                    increasing bigserial). This is a proxy for recency
                    without needing a created_at timestamp on every chunk
                    (the source PK is a sufficient proxy: sources are
                    created in chronological order by bigserial).

  ordinal_score   = chunk position within its source. Chunks earlier in
                    the document (lower chunk ordinal) tend to contain
                    the most important content (title, abstract, intro).
                    Computed as:
                        1.0 - (chunk_id_rank / n_same_source_chunks)
                    where chunk_id_rank is the 0-based rank of this chunk
                    within the group of all retrieved chunks sharing the
                    same source_id, sorted by chunk_id ASC. Chunk PKs are
                    bigserial assigned at insert time — within a single
                    ingest a lower PK means an earlier chunk (the repository
                    inserts in natural order from the chunker).

Weights (tunable constants):
    w_recency = 0.4
    w_ordinal = 0.3
    w_vector  = 0.3  (the original HNSW cosine similarity, 1 - distance)

The combined score preserves the primary vector relevance signal while
promoting recent content and document-leading positions as tie-breakers.
Never raises: any failure returns the input list unchanged (preserving HNSW
order as the safe fallback).
"""
from __future__ import annotations

import logging
from typing import Sequence, TypeVar

logger = logging.getLogger(__name__)

# Rerank weights (sum = 1.0)
_W_RECENCY: float = 0.4
_W_ORDINAL: float = 0.3
_W_VECTOR: float = 0.3

# Minimum distance → vector score = 1.0 (perfect match).
# Maximum distance → vector score approaches 0.0.
_MAX_USEFUL_DISTANCE: float = 2.0  # cosine distances are in [0, 2]


T = TypeVar("T")


def rerank(
    chunks: Sequence[T],
    *,
    get_source_id=None,
    get_chunk_id=None,
    get_distance=None,
    top_k: int | None = None,
) -> list[T]:
    """Rerank a list of retrieved chunks.

    Parameters
    ----------
    chunks:
        The chunks returned by vector search. Can be any type; accessors
        below extract the fields needed for scoring.
    get_source_id:
        Callable(chunk) → int. Default: ``lambda c: c.source_identifier``.
    get_chunk_id:
        Callable(chunk) → int. Default: ``lambda c: c.chunk_id``.
    get_distance:
        Callable(chunk) → float | None. Default: ``lambda c: c.distance``.
        None distances are treated as _MAX_USEFUL_DISTANCE (worst relevance).
    top_k:
        If set, truncate output to top_k entries after reranking.

    Returns
    -------
    list[T] — the input chunks reordered by the composite rerank score,
    highest score first. If ``top_k`` is set the list is truncated.
    Never raises: on any error returns the input as-is.
    """
    if not chunks:
        return list(chunks)

    if get_source_id is None:
        get_source_id = lambda c: c.source_identifier  # noqa: E731
    if get_chunk_id is None:
        get_chunk_id = lambda c: c.chunk_id  # noqa: E731
    if get_distance is None:
        get_distance = lambda c: c.distance  # noqa: E731

    try:
        return _rerank_impl(
            list(chunks),
            get_source_id=get_source_id,
            get_chunk_id=get_chunk_id,
            get_distance=get_distance,
            top_k=top_k,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "rerank failed: exc_class=%s — returning input order",
            type(exc).__name__,
        )
        result = list(chunks)
        if top_k is not None:
            result = result[:top_k]
        return result


def _rerank_impl(
    chunks: list,
    *,
    get_source_id,
    get_chunk_id,
    get_distance,
    top_k: int | None,
) -> list:
    """Core rerank logic. Assumes non-empty input."""
    n = len(chunks)

    # -- Collect raw values ---------------------------------------------------
    source_ids = [get_source_id(c) for c in chunks]
    chunk_ids = [get_chunk_id(c) for c in chunks]
    distances = [get_distance(c) for c in chunks]

    # -- Recency score: rank by source_id DESC (higher PK = newer source) ----
    # Sort unique source_ids descending; map each to a 0-based rank.
    unique_sources_desc = sorted(set(source_ids), reverse=True)
    source_rank: dict[int, int] = {
        sid: idx for idx, sid in enumerate(unique_sources_desc)
    }
    n_sources = max(len(unique_sources_desc), 1)

    # -- Ordinal score: rank by chunk_id ASC within each source --------------
    # Group chunk indices by source_id; sort by chunk_id ascending within group.
    source_to_chunk_indices: dict[int, list[int]] = {}
    for i, sid in enumerate(source_ids):
        source_to_chunk_indices.setdefault(sid, []).append(i)
    for sid in source_to_chunk_indices:
        source_to_chunk_indices[sid].sort(key=lambda i: chunk_ids[i])

    # Map chunk index → 0-based ordinal within its source group.
    chunk_ordinal: list[int] = [0] * n
    for sid, indices in source_to_chunk_indices.items():
        for ordinal, idx in enumerate(indices):
            chunk_ordinal[idx] = ordinal

    # -- Compute composite scores --------------------------------------------
    scored: list[tuple[float, int]] = []
    for i in range(n):
        # Vector relevance: convert distance to similarity (clamped to [0,1]).
        dist = distances[i]
        if dist is None:
            vector_score = 0.0
        else:
            vector_score = max(
                0.0, min(1.0, 1.0 - float(dist) / _MAX_USEFUL_DISTANCE)
            )

        # Recency: 0-based rank among unique sources descending → normalised.
        src_rank = source_rank[source_ids[i]]
        recency_score = 1.0 - (src_rank / n_sources)

        # Ordinal: 0-based position within source → normalised.
        n_same = len(source_to_chunk_indices[source_ids[i]])
        ordinal_score = 1.0 - (chunk_ordinal[i] / max(n_same, 1))

        composite = (
            _W_VECTOR * vector_score
            + _W_RECENCY * recency_score
            + _W_ORDINAL * ordinal_score
        )
        scored.append((composite, i))

    # Sort by composite score descending (highest first).
    scored.sort(key=lambda x: x[0], reverse=True)

    reranked = [chunks[i] for _, i in scored]
    if top_k is not None:
        reranked = reranked[:top_k]

    logger.debug(
        "Reranked %d chunks → top score=%.3f",
        n,
        scored[0][0] if scored else 0.0,
    )
    return reranked
