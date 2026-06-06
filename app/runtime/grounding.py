"""Answer grounding & anti-hallucination module (Architecture §3.4.13).

Extracted verbatim from ``app.runtime.orchestrator`` (Unit 12 §8
doctrine-path normalization): the doctrine names a standalone grounding
module, but the implementation had been inlined as static methods on
``LucielOrchestrator``. This module holds the composite grounding-score
computation and its citation-overlap helper; the orchestrator imports
them back and re-exposes them as class attributes so every existing call
site (``self._grounding_from_chunks(...)``,
``LucielOrchestrator._citation_overlap(...)``,
``LucielOrchestrator._CITATION_JACCARD_THRESHOLD``) is byte-for-byte
unchanged. Pure extraction — no behavior change.
"""
from __future__ import annotations

from typing import Sequence

# Citation-overlap threshold (Jaccard similarity). A sentence is
# "covered" when its unigram Jaccard against any chunk >= this value.
_CITATION_JACCARD_THRESHOLD: float = 0.10


def _grounding_from_chunks(chunks: Sequence, answer: str = "") -> float | None:
    """Derive a [0,1] composite grounding score from retrieved chunks.

    §3.4.13 requires a COMPOSITE of two components:

      (a) Retrieval relevance  — ``1 - best_cosine_distance`` (best =
          smallest distance = closest match) using the chunk distances
          already computed during retrieval. Measures how well the top
          retrieved chunk matched the query.

      (b) Citation overlap  — fraction of the answer's sentences whose
          token-overlap with any retrieved chunk exceeds a threshold.
          For each sentence we compute Jaccard similarity of its
          unigram set against the unigram set of each chunk's content;
          a sentence is "covered" when any chunk exceeds the threshold.
          This is deterministic, dependency-free, and cheap (no
          embedding call). Coverage = covered_sentences / total_sentences.
          An empty answer or an answer with no extractable sentences
          contributes a citation-overlap of 0.0.

    Combination: weighted average with equal weights 0.5/0.5:

        grounding = 0.5 * retrieval_relevance + 0.5 * citation_overlap

    The combined score is clamped to [0,1]. Returns ``None`` when
    nothing was retrieved (the OUTCOME gate treats None as below every
    floor only in concert with the retrieval-failed flag). If chunks
    exist but none carry a distance, retrieval_relevance is 0.0 (no
    distance information means we cannot claim relevance) and citation
    overlap is still computed against chunk content. It never raises:
    a malformed chunk degrades gracefully.

    Citation-overlap threshold: CITATION_JACCARD_THRESHOLD = 0.10.
    This is deliberately low so that any meaningful vocabulary overlap
    between an answer sentence and a chunk counts as a citation hit;
    a higher threshold would produce false negatives on paraphrased
    answers. The threshold is a module constant so it can be tuned
    from audit data without changing the algorithm.
    """
    if not chunks:
        return None
    try:
        # --- (a) Retrieval relevance ---
        distances = [
            c.distance
            for c in chunks
            if getattr(c, "distance", None) is not None
        ]
        if distances:
            best = min(distances)
            retrieval_relevance = max(0.0, min(1.0, 1.0 - float(best)))
        else:
            retrieval_relevance = 0.0

        # --- (b) Citation overlap ---
        citation_overlap = _citation_overlap(answer, chunks)

        # --- Combine (0.5 / 0.5 weighted average) ---
        grounding = 0.5 * retrieval_relevance + 0.5 * citation_overlap
        return max(0.0, min(1.0, grounding))
    except Exception:  # noqa: BLE001
        return None


def _citation_overlap(answer: str, chunks: Sequence) -> float:
    """Fraction of answer sentences whose unigram Jaccard similarity
    to at least one retrieved chunk exceeds _CITATION_JACCARD_THRESHOLD.

    Algorithm (deterministic, no external deps):
      1. Tokenise by splitting on whitespace/punctuation to lowercase
         unigrams. Strip common punctuation so "fact." and "fact" match.
      2. For each answer sentence, compute Jaccard against every chunk
         and mark it covered if any pair exceeds the threshold.
      3. Return covered_count / total_sentences, or 0.0 when the answer
         has no usable sentences.
    """
    import re

    def _tokens(text: str) -> frozenset:
        return frozenset(w.lower() for w in re.split(r"[\s,.!?;:]+", text) if w)

    # Split answer into sentences on '.', '!', '?' or newlines.
    sentences = [
        s.strip()
        for s in re.split(r"(?<=[.!?])\s+|\n+", answer.strip())
        if s.strip()
    ]
    if not sentences:
        return 0.0

    # Pre-compute chunk token sets (defensive: use content attr or str).
    chunk_token_sets = []
    for c in chunks:
        content = getattr(c, "content", None) or getattr(c, "formatted", "") or ""
        if content:
            chunk_token_sets.append(_tokens(str(content)))
    if not chunk_token_sets:
        return 0.0

    threshold = _CITATION_JACCARD_THRESHOLD
    covered = 0
    for sent in sentences:
        sent_tokens = _tokens(sent)
        if not sent_tokens:
            continue
        for chunk_tokens in chunk_token_sets:
            union = sent_tokens | chunk_tokens
            if not union:
                continue
            jaccard = len(sent_tokens & chunk_tokens) / len(union)
            if jaccard >= threshold:
                covered += 1
                break
    return covered / len(sentences)


__all__ = [
    "_CITATION_JACCARD_THRESHOLD",
    "_grounding_from_chunks",
    "_citation_overlap",
]
