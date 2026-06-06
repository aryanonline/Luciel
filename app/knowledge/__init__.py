"""Knowledge subsystem — ingestion + retrieval + chunking + parsers.

Arc 11 Step 3 surfaces ``RetrievedChunk`` here so Step 5 (trace
instrumentation) and Step 8 (orchestrator wiring) can import
without reaching into ``app.runtime.knowledge_retrieval`` directly.

The retrieval symbols now live in ``app.runtime.knowledge_retrieval``
(Unit 12 §8 doctrine-path normalization). They are re-exported here
lazily (PEP 562 ``__getattr__``): the retrieval module imports
``app.knowledge.embedder`` / ``app.knowledge.reranker`` at module load,
so an eager re-export at package-init time would form an import cycle
(``app.knowledge`` ⇄ ``app.runtime.knowledge_retrieval``). Deferring the
import to first attribute access keeps the same public surface without
the cycle.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - type-checkers only
    from app.runtime.knowledge_retrieval import (  # noqa: F401
        KnowledgeRetriever,
        RetrievedChunk,
        collect_source_pks,
    )

__all__ = ["KnowledgeRetriever", "RetrievedChunk", "collect_source_pks"]


def __getattr__(name: str):
    if name in __all__:
        from app.runtime import knowledge_retrieval

        return getattr(knowledge_retrieval, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
