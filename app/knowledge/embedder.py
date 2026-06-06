"""
Text embedder.

Converts text chunks into vector embeddings using OpenAI's
embedding model. These vectors are stored in pgvector for
semantic similarity search.

We use OpenAI's text-embedding-3-small model which produces
1536-dimension vectors. This matches our vector(1536) column.

To switch embedding providers later (e.g., Cohere, local model),
create a new embedder implementing the same interface.
"""

from __future__ import annotations

import hashlib
import logging
import random

from openai import OpenAI

from app.core.config import settings

logger = logging.getLogger(__name__)

# OpenAI embedding model — produces 1536-dimension vectors
EMBEDDING_MODEL = "text-embedding-3-small"

# Dimension of the embedding vectors, matching the vector(1536) column.
EMBEDDING_DIM = 1536

# Set once so the stub WARNING fires only on the first stub call,
# mirroring StubLLMClient's construction-time warning discipline.
_stub_warned = False


def _stub_embed(text: str) -> list[float]:
    """Produce a deterministic 1536-dim stub vector for ``text``.

    The vector is seeded from the sha256 of the text, so the same text
    yields an identical vector across calls and process restarts — a
    hard requirement for the EXPLAIN-plan and retrieval-shape tests to
    be stable. Values are finite unit-normalized floats; the dimension
    matches the vector(1536) column.
    """
    seed = hashlib.sha256(text.encode("utf-8")).hexdigest()
    rng = random.Random(seed)
    vec = [rng.uniform(-1.0, 1.0) for _ in range(EMBEDDING_DIM)]
    norm = sum(v * v for v in vec) ** 0.5
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


def embed_texts(texts: list[str]) -> list[list[float]]:
    """
    Convert a list of text strings into vector embeddings.

    Args:
        texts: List of text strings to embed.

    Returns:
        List of embedding vectors (each is a list of 1536 floats).

    Raises:
        Exception if the OpenAI API call fails.
    """
    if not texts:
        return []

    if settings.enable_stub_embedding_provider:
        global _stub_warned
        if not _stub_warned:
            logger.warning(
                "Stub embedding provider active -- embed_texts returns "
                "deterministic stub vectors and makes NO OpenAI call. This "
                "MUST NOT run in production. Gate this on "
                "settings.enable_stub_embedding_provider=False for any "
                "non-CI environment."
            )
            _stub_warned = True
        return [_stub_embed(text) for text in texts]

    client = OpenAI(api_key=settings.openai_api_key)

    response = client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=texts,
    )

    embeddings = [item.embedding for item in response.data]

    logger.info("Embedded %d texts using %s", len(texts), EMBEDDING_MODEL)

    return embeddings


def embed_single(text: str) -> list[float]:
    """
    Convert a single text string into a vector embedding.

    Convenience wrapper around embed_texts for single items.
    """
    results = embed_texts([text])
    if not results:
        raise ValueError("Embedding returned no results")
    return results[0]