"""Knowledge subsystem — ingestion + retrieval + chunking + parsers.

Arc 11 Step 3 surfaces ``RetrievedChunk`` here so Step 5 (trace
instrumentation) and Step 8 (orchestrator wiring) can import
without reaching into ``app.knowledge.retriever`` directly.
"""
from __future__ import annotations

from app.knowledge.retriever import (
    KnowledgeRetriever,
    RetrievedChunk,
    collect_source_pks,
)

__all__ = ["KnowledgeRetriever", "RetrievedChunk", "collect_source_pks"]
