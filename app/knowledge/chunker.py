"""
Text chunker.

Splits raw text into smaller chunks suitable for embedding.
Smaller chunks produce better search results because each chunk
is more focused on a single topic.

Chunking strategy:
- Split on paragraph boundaries first.
- If a paragraph is too long, split on sentence boundaries.
- Each chunk overlaps slightly with the previous one to preserve context.

This is the standard approach for RAG (retrieval augmented generation)
systems and can be tuned later based on retrieval quality.
"""

from __future__ import annotations

import re


def chunk_text(
    text: str,
    max_chunk_size: int = 800,
    overlap: int = 100,
) -> list[str]:
    """
    Split text into chunks.

    Args:
        text:           The raw text to chunk.
        max_chunk_size: Maximum characters per chunk.
        overlap:        Number of characters to overlap between chunks.

    Returns:
        List of text chunks.
    """
    if not text or not text.strip():
        return []

    # Clean up whitespace
    text = text.strip()
    text = re.sub(r"\n{3,}", "\n\n", text)

    # First split on double newlines (paragraphs)
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    chunks = []
    current_chunk = ""

    for paragraph in paragraphs:
        # If adding this paragraph would exceed max size, save current chunk
        if current_chunk and len(current_chunk) + len(paragraph) + 2 > max_chunk_size:
            chunks.append(current_chunk.strip())
            # Start new chunk with overlap from end of previous
            if overlap > 0 and len(current_chunk) > overlap:
                current_chunk = current_chunk[-overlap:] + "\n\n" + paragraph
            else:
                current_chunk = paragraph
        else:
            if current_chunk:
                current_chunk += "\n\n" + paragraph
            else:
                current_chunk = paragraph

        # If a single paragraph exceeds max size, split on sentences
        if len(current_chunk) > max_chunk_size:
            sentences = re.split(r"(?<=[.!?])\s+", current_chunk)
            current_chunk = ""
            for sentence in sentences:
                if current_chunk and len(current_chunk) + len(sentence) + 1 > max_chunk_size:
                    chunks.append(current_chunk.strip())
                    if overlap > 0 and len(current_chunk) > overlap:
                        current_chunk = current_chunk[-overlap:] + " " + sentence
                    else:
                        current_chunk = sentence
                else:
                    if current_chunk:
                        current_chunk += " " + sentence
                    else:
                        current_chunk = sentence

    # Add the last chunk
    if current_chunk.strip():
        chunks.append(current_chunk.strip())

    return chunks