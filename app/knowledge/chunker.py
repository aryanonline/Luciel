"""
Pluggable text chunker (Step 25b, File 6).

Four strategies: paragraph | sentence | fixed | semantic.

Effective-config resolution (three-level, Option A):
    instance -> domain -> tenant

The first non-NULL chunk_size / chunk_overlap / chunk_strategy wins at
each level. Every LucielInstance ends up with a fully-resolved
EffectiveChunkingConfig at ingest time — the instance can override any
individual field without having to redeclare the others.

Domain-agnostic by construction: no vertical enums, no tenant-ID
branches. A legal tenant picking 'sentence' for clause-aware chunks,
a real-estate tenant picking 'paragraph' for neighbourhood guides, and
an engineering tenant picking 'semantic' for long-form design docs all
flow through the same dispatcher.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from app.models.domain_config import DomainConfig
from app.models.luciel_instance import LucielInstance
from app.models.tenant import TenantConfig

# Canonical strategy slugs. Stored verbatim in tenant_configs.chunk_strategy
# (NOT NULL) and the nullable override columns on domain_configs /
# luciel_instances.
STRATEGY_PARAGRAPH = "paragraph"
STRATEGY_SENTENCE = "sentence"
STRATEGY_FIXED = "fixed"
STRATEGY_SEMANTIC = "semantic"

SUPPORTED_STRATEGIES: tuple[str, ...] = (
    STRATEGY_PARAGRAPH,
    STRATEGY_SENTENCE,
    STRATEGY_FIXED,
    STRATEGY_SEMANTIC,
)


class ChunkerError(Exception):
    """Raised when chunking cannot proceed (bad config, unknown strategy)."""


@dataclass(frozen=True)
class EffectiveChunkingConfig:
    """Fully-resolved chunking config for a single ingest call.

    Always fully populated: every field is guaranteed non-NULL by the
    resolver. Call-site code never has to check for None.
    """
    chunk_size: int
    chunk_overlap: int
    chunk_strategy: str
    # Provenance for the audit trail — which level supplied each value.
    # Values: 'instance' | 'domain' | 'tenant'.
    size_source: str
    overlap_source: str
    strategy_source: str

    def __post_init__(self) -> None:
        if self.chunk_size <= 0:
            raise ChunkerError(f"chunk_size must be > 0, got {self.chunk_size}")
        if self.chunk_overlap < 0:
            raise ChunkerError(
                f"chunk_overlap must be >= 0, got {self.chunk_overlap}"
            )
        if self.chunk_overlap >= self.chunk_size:
            raise ChunkerError(
                f"chunk_overlap ({self.chunk_overlap}) must be < "
                f"chunk_size ({self.chunk_size})"
            )
        if self.chunk_strategy not in SUPPORTED_STRATEGIES:
            raise ChunkerError(
                f"Unsupported chunk_strategy: {self.chunk_strategy!r}. "
                f"Supported: {SUPPORTED_STRATEGIES}"
            )


def resolve_effective_config(
    *,
    tenant: TenantConfig,
    domain: DomainConfig | None,
    instance: LucielInstance | None,
) -> EffectiveChunkingConfig:
    """Resolve the three-level inheritance chain.

    Precedence per field: instance -> domain -> tenant.
    tenant values are NOT NULL by migration, so they are always the
    non-NULL fallback of last resort.
    """
    # chunk_size
    if instance is not None and instance.chunk_size is not None:
        chunk_size, size_source = instance.chunk_size, "instance"
    elif domain is not None and domain.chunk_size is not None:
        chunk_size, size_source = domain.chunk_size, "domain"
    else:
        chunk_size, size_source = tenant.chunk_size, "tenant"

    # chunk_overlap
    if instance is not None and instance.chunk_overlap is not None:
        chunk_overlap, overlap_source = instance.chunk_overlap, "instance"
    elif domain is not None and domain.chunk_overlap is not None:
        chunk_overlap, overlap_source = domain.chunk_overlap, "domain"
    else:
        chunk_overlap, overlap_source = tenant.chunk_overlap, "tenant"

    # chunk_strategy
    if instance is not None and instance.chunk_strategy is not None:
        chunk_strategy, strategy_source = instance.chunk_strategy, "instance"
    elif domain is not None and domain.chunk_strategy is not None:
        chunk_strategy, strategy_source = domain.chunk_strategy, "domain"
    else:
        chunk_strategy, strategy_source = tenant.chunk_strategy, "tenant"

    return EffectiveChunkingConfig(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        chunk_strategy=chunk_strategy,
        size_source=size_source,
        overlap_source=overlap_source,
        strategy_source=strategy_source,
    )


# ------------------------------------------------------------------
# Strategy implementations
# ------------------------------------------------------------------

# Approx-token heuristic. We don't import tiktoken here — the embedder
# in File 9 will tokenize for real before sending to OpenAI. For
# chunking boundaries, whitespace-split is accurate enough and keeps
# the chunker dependency-free.
def _approx_token_count(s: str) -> int:
    return len(s.split())


def _split_paragraphs(text: str) -> list[str]:
    # Blank-line separated. Collapse runs of 3+ newlines to 2 so edge
    # cases in parser output (e.g., pypdf page joins) produce one break.
    text = re.sub(r"\n{3,}", "\n\n", text)
    paragraphs = [p.strip() for p in text.split("\n\n")]
    return [p for p in paragraphs if p]


# Conservative sentence splitter. Good enough for chunking boundaries
# across English-language prose in real-estate, legal, engineering
# domains. If a future vertical needs locale-aware splitting we swap
# this for pysbd/spaCy behind the same function signature.
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"\'(\[])")


def _split_sentences(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    parts = _SENTENCE_RE.split(text)
    return [p.strip() for p in parts if p.strip()]


def _chunk_paragraph(text: str, cfg: EffectiveChunkingConfig) -> list[str]:
    """Greedily pack paragraphs into chunks of <= cfg.chunk_size tokens.
    Overlap is measured in tokens of trailing paragraphs carried forward.
    """
    paragraphs = _split_paragraphs(text)
    if not paragraphs:
        return []
    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for p in paragraphs:
        p_tokens = _approx_token_count(p)
        if current_tokens + p_tokens <= cfg.chunk_size or not current:
            current.append(p)
            current_tokens += p_tokens
        else:
            chunks.append("\n\n".join(current))
            # Build overlap carry-over from the tail of `current`.
            carry: list[str] = []
            carry_tokens = 0
            for prev in reversed(current):
                prev_tokens = _approx_token_count(prev)
                if carry_tokens + prev_tokens > cfg.chunk_overlap:
                    break
                carry.insert(0, prev)
                carry_tokens += prev_tokens
            current = carry + [p]
            current_tokens = carry_tokens + p_tokens
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _chunk_sentence(text: str, cfg: EffectiveChunkingConfig) -> list[str]:
    sentences = _split_sentences(text)
    if not sentences:
        return []
    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for s in sentences:
        s_tokens = _approx_token_count(s)
        if current_tokens + s_tokens <= cfg.chunk_size or not current:
            current.append(s)
            current_tokens += s_tokens
        else:
            chunks.append(" ".join(current))
            carry: list[str] = []
            carry_tokens = 0
            for prev in reversed(current):
                prev_tokens = _approx_token_count(prev)
                if carry_tokens + prev_tokens > cfg.chunk_overlap:
                    break
                carry.insert(0, prev)
                carry_tokens += prev_tokens
            current = carry + [s]
            current_tokens = carry_tokens + s_tokens
    if current:
        chunks.append(" ".join(current))
    return chunks


def _chunk_fixed(text: str, cfg: EffectiveChunkingConfig) -> list[str]:
    """Token-count-based fixed-window chunking. Word-boundary safe."""
    words = text.split()
    if not words:
        return []
    chunks: list[str] = []
    step = cfg.chunk_size - cfg.chunk_overlap  # validated > 0 by __post_init__
    i = 0
    while i < len(words):
        window = words[i : i + cfg.chunk_size]
        if not window:
            break
        chunks.append(" ".join(window))
        if i + cfg.chunk_size >= len(words):
            break
        i += step
    return chunks


def _chunk_semantic(text: str, cfg: EffectiveChunkingConfig) -> list[str]:
    """Semantic chunking v1: paragraph-first, fall back to sentence
    packing for any paragraph that on its own exceeds chunk_size.

    This is the "good default for long-form" strategy. A later step
    (Step 37 hybrid retrieval) may replace this with embedding-similarity
    break detection — same function signature, drop-in swap.
    """
    paragraphs = _split_paragraphs(text)
    if not paragraphs:
        return []
    # Expand oversized paragraphs into sentences so packing never
    # produces a single chunk larger than chunk_size.
    expanded: list[str] = []
    for p in paragraphs:
        if _approx_token_count(p) <= cfg.chunk_size:
            expanded.append(p)
        else:
            expanded.extend(_split_sentences(p) or [p])
    # Pack greedily as in paragraph mode.
    fake = "\n\n".join(expanded)
    return _chunk_paragraph(fake, cfg)


_STRATEGY_DISPATCH = {
    STRATEGY_PARAGRAPH: _chunk_paragraph,
    STRATEGY_SENTENCE: _chunk_sentence,
    STRATEGY_FIXED: _chunk_fixed,
    STRATEGY_SEMANTIC: _chunk_semantic,
}


def chunk_text(text: str, cfg: EffectiveChunkingConfig) -> list[str]:
    """Chunk `text` according to a fully-resolved effective config.

    Returns a list of non-empty chunk strings. Empty/whitespace-only
    input returns []. Unknown strategy raises ChunkerError (already
    guarded at EffectiveChunkingConfig.__post_init__ time, this is
    defence in depth).
    """
    if not text or not text.strip():
        return []
    impl = _STRATEGY_DISPATCH.get(cfg.chunk_strategy)
    if impl is None:
        raise ChunkerError(
            f"No chunker registered for strategy {cfg.chunk_strategy!r}"
        )
    chunks = impl(text, cfg)
    # Final safety net: drop any empty strings the strategy produced.
    return [c for c in chunks if c and c.strip()]


__all__ = [
    "STRATEGY_PARAGRAPH",
    "STRATEGY_SENTENCE",
    "STRATEGY_FIXED",
    "STRATEGY_SEMANTIC",
    "SUPPORTED_STRATEGIES",
    "ChunkerError",
    "EffectiveChunkingConfig",
    "resolve_effective_config",
    "chunk_text",
]