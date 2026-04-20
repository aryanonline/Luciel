"""
Pydantic CRUD schemas for knowledge ingestion (Step 25b, File 7).

Covers:
    - KnowledgeIngestRequest   POST body for text-blob ingest
    - KnowledgeUploadMeta      multipart form fields for file upload
    - KnowledgeRead            single-chunk read payload
    - KnowledgeSourceRead      per-source summary (groups chunks by source_id)
    - KnowledgeListResponse    paginated list of sources
    - KnowledgeReplaceRequest  PUT body for replacing a source_id
    - KnowledgeDeleteResponse  DELETE response

Scope binding: every create/read/update targets a luciel_instance_id.
Scope authorization lives in app.policy.scope.ScopePolicy — these
schemas carry no authorization logic, only shape validation.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.knowledge.chunker import SUPPORTED_STRATEGIES
from app.knowledge.parsers import SUPPORTED_SOURCE_TYPES

# ---- Canonical knowledge_type values ----
KNOWLEDGE_TYPES: tuple[str, ...] = (
    "domain_knowledge",
    "tenant_document",
    "role_instruction",
    "agent_knowledge",       # legacy (pre-Step-24.5)
    "luciel_knowledge",      # Step 25b: attached to a specific LucielInstance
)


# ============================================================
# Create / ingest
# ============================================================

class KnowledgeIngestRequest(BaseModel):
    """JSON body for POST /admin/luciel-instances/{id}/knowledge (text blob path).

    The multipart file-upload path uses KnowledgeUploadMeta instead; this
    schema is for the direct-text ingest case (legacy compatibility +
    programmatic ingestion of already-extracted content).
    """
    model_config = ConfigDict(extra="forbid")

    content: str = Field(..., min_length=1, max_length=10_000_000)
    knowledge_type: str = Field(..., min_length=1, max_length=50)
    title: str | None = Field(default=None, max_length=500)
    source: str | None = Field(default=None, max_length=500)
    source_id: str | None = Field(default=None, max_length=100)
    source_filename: str | None = Field(default=None, max_length=500)

    @field_validator("knowledge_type")
    @classmethod
    def _validate_knowledge_type(cls, v: str) -> str:
        if v not in KNOWLEDGE_TYPES:
            raise ValueError(
                f"knowledge_type must be one of {KNOWLEDGE_TYPES}, got {v!r}"
            )
        return v


class KnowledgeUploadMeta(BaseModel):
    """Form-field companion to a multipart file upload.

    The file itself is received via FastAPI's UploadFile dependency in
    the admin route (File 10); this schema validates the accompanying
    metadata fields.
    """
    model_config = ConfigDict(extra="forbid")

    knowledge_type: str = Field(default="luciel_knowledge", max_length=50)
    title: str | None = Field(default=None, max_length=500)
    source_id: str | None = Field(default=None, max_length=100)
    # Optional overrides — if omitted, File 9 infers source_type from filename
    # via detect_source_type().
    source_type: str | None = Field(default=None, max_length=20)

    @field_validator("knowledge_type")
    @classmethod
    def _validate_knowledge_type(cls, v: str) -> str:
        if v not in KNOWLEDGE_TYPES:
            raise ValueError(
                f"knowledge_type must be one of {KNOWLEDGE_TYPES}, got {v!r}"
            )
        return v

    @field_validator("source_type")
    @classmethod
    def _validate_source_type(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if v not in SUPPORTED_SOURCE_TYPES:
            raise ValueError(
                f"source_type must be one of {SUPPORTED_SOURCE_TYPES}, got {v!r}"
            )
        return v


# ============================================================
# Read
# ============================================================

class KnowledgeRead(BaseModel):
    """Single chunk row."""
    model_config = ConfigDict(from_attributes=True, extra="ignore")

    id: int
    tenant_id: str | None
    domain_id: str | None
    agent_id: str | None
    luciel_instance_id: int | None

    content: str
    title: str | None
    knowledge_type: str
    source: str | None

    source_id: str | None
    source_version: int
    source_filename: str | None
    source_type: str | None
    ingested_by: str | None

    superseded_at: datetime | None
    created_by: str | None
    created_at: datetime | None
    updated_at: datetime | None


class KnowledgeSourceRead(BaseModel):
    """Per-source summary — groups all chunks sharing one source_id.

    This is what list/detail admin routes return by default. Callers
    who need the raw chunk rows request ?expand=chunks on the detail
    route; that returns a list[KnowledgeRead] instead.
    """
    model_config = ConfigDict(extra="forbid")

    luciel_instance_id: int | None
    source_id: str
    source_version: int
    source_filename: str | None
    source_type: str | None
    knowledge_type: str
    title: str | None
    chunk_count: int
    ingested_by: str | None
    created_at: datetime | None
    superseded_at: datetime | None

    @property
    def is_active(self) -> bool:
        return self.superseded_at is None


class KnowledgeListResponse(BaseModel):
    """Paginated list of source summaries."""
    model_config = ConfigDict(extra="forbid")

    items: list[KnowledgeSourceRead]
    total: int
    limit: int
    offset: int


# ============================================================
# Replace / delete
# ============================================================

class KnowledgeReplaceRequest(BaseModel):
    """JSON body for PUT /admin/.../knowledge/{source_id} (text-blob path).

    Supersedes the current active version of source_id and inserts a
    new version (source_version = old + 1) with the given content.
    Multipart file-based replace shares the same supersede semantics
    but accepts a file instead of content.
    """
    model_config = ConfigDict(extra="forbid")

    content: str = Field(..., min_length=1, max_length=10_000_000)
    title: str | None = Field(default=None, max_length=500)
    source: str | None = Field(default=None, max_length=500)
    source_filename: str | None = Field(default=None, max_length=500)


class KnowledgeDeleteResponse(BaseModel):
    """Response for DELETE /admin/.../knowledge/{source_id}."""
    model_config = ConfigDict(extra="forbid")

    luciel_instance_id: int | None
    source_id: str
    superseded_rows: int
    """Number of previously-active chunks marked superseded by this delete."""


# ============================================================
# Effective chunking config (read-only surface for admin diagnostics)
# ============================================================

class EffectiveChunkingConfigRead(BaseModel):
    """Exposed on GET /admin/luciel-instances/{id}/chunking-config so admins
    can inspect the resolved instance->domain->tenant chain without
    reverse-engineering it from the three rows.
    """
    model_config = ConfigDict(extra="forbid")

    chunk_size: int
    chunk_overlap: int
    chunk_strategy: Literal["paragraph", "sentence", "fixed", "semantic"]
    size_source: Literal["instance", "domain", "tenant"]
    overlap_source: Literal["instance", "domain", "tenant"]
    strategy_source: Literal["instance", "domain", "tenant"]


# Defensive cross-check: the Literal strategy list must match the
# chunker's canonical tuple. Catches accidental drift at import time.
assert set(SUPPORTED_STRATEGIES) == {
    "paragraph",
    "sentence",
    "fixed",
    "semantic",
}, "chunker SUPPORTED_STRATEGIES has drifted from schema Literal"


__all__ = [
    "KNOWLEDGE_TYPES",
    "KnowledgeIngestRequest",
    "KnowledgeUploadMeta",
    "KnowledgeRead",
    "KnowledgeSourceRead",
    "KnowledgeListResponse",
    "KnowledgeReplaceRequest",
    "KnowledgeDeleteResponse",
    "EffectiveChunkingConfigRead",
]