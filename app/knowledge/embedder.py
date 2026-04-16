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

import logging

from openai import OpenAI

from app.core.config import settings

logger = logging.getLogger(__name__)

# OpenAI embedding model — produces 1536-dimension vectors
EMBEDDING_MODEL = "text-embedding-3-small"


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