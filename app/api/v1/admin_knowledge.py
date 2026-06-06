"""Arc 11 Step 7 — admin knowledge-base API.

The seven admin routes that back the Knowledge Base v1 UI (Step 9
in the frontend repo) plus the `/internal/v1/retrieve` verification
endpoint. All under prefix `/admin/instances/{instance_id}/knowledge`
except the internal retrieve, which lives at `/internal/v1/retrieve`
and is platform_admin-gated.

Layered defenses applied to every admin route
---------------------------------------------

  L1   ``ScopePolicy.enforce_admin_owns_instance`` (inside
       ``_load_active_instance``) — cross-Admin guard.
  L2   ``ScopePolicy.require_knowledge_role`` — role matrix per
       Architecture §3.2.2:
         list / view → owner + manager + operator (operator scoped)
         edit / delete → owner + manager only
  L3   ``TenantScopedDbSession`` — the FastAPI dep that binds
       ``app.admin_id`` + ``app.instance_id`` GUCs onto the session
       so Arc 9 RLS fences fire.
  L4   ``admin_audit_log`` row appended on every list/view/edit/
       delete via the existing ``AdminAuditRepository.record`` chain.

Legacy ingest route — GONE
--------------------------

The pre-Arc-11 ``POST /admin/knowledge/ingest`` route and its
companion instance-scoped legacy routes were deleted in Cleanup B
of the Arc 11 closeout. This module is now the sole knowledge ingest
surface. The ``KnowledgeEmbedding = KnowledgeChunk`` alias is also
gone from ``app/models/knowledge.py``.
"""
from __future__ import annotations

import io
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text as sql_text

from app.api.deps import (
    DbSession,
    TenantScopedDbSession,
    get_admin_audit_repository,
    get_audit_context,
    get_luciel_instance_service,
)
from app.db.tenant_scope import bind_tenant_scope
from app.middleware.rate_limit import (
    get_tier_aware_key,
    get_tier_rate_limit_for_key,
    limiter,
)
from app.models.admin_audit_log import (
    ACTION_KNOWLEDGE_AFFECTED_QUESTIONS_VIEWED,
    ACTION_KNOWLEDGE_CRAWL_ENQUEUED,
    ACTION_KNOWLEDGE_SOURCE_CREATED,
    ACTION_KNOWLEDGE_SOURCE_DELETED,
    ACTION_KNOWLEDGE_SOURCE_LISTED,
    ACTION_KNOWLEDGE_SOURCE_UPDATED,
    ACTION_KNOWLEDGE_SOURCE_VIEWED,
    RESOURCE_KNOWLEDGE_SOURCE,
)
from app.models.instance import Instance
from app.models.knowledge import KnowledgeChunk
from app.models.knowledge_source import KnowledgeSource
from app.policy.entitlements import (
    TIER_ENTITLEMENTS,
    TIER_FREE,
)
from app.policy.scope import ScopePolicy
from app.repositories.admin_audit_repository import (
    AdminAuditRepository,
    AuditContext,
)
from app.repositories.knowledge_repository import KnowledgeRepository
from app.repositories.knowledge_source_repository import (
    KnowledgeSourceNotFound,
    KnowledgeSourceRepository,
)
from app.repositories.trace_repository import TraceRepository
from app.services.instance_service import InstanceService

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/admin/instances/{instance_id}/knowledge",
    tags=["admin-knowledge"],
)


# =====================================================================
# Pydantic schemas
# =====================================================================


class KnowledgeQuota(BaseModel):
    """Per-Admin quota state surfaced on GET /sources."""

    used_bytes: int
    cap_bytes: int | None  # None = unlimited (Enterprise)
    per_file_cap_bytes: int
    tier: str


class KnowledgeSourceRead(BaseModel):
    source_id: int
    source_uuid: str
    filename: str | None
    source_type: str
    size_bytes: int
    s3_key: str | None
    origin_url: str | None
    ingestion_status: str
    ingestion_error: str | None
    ingestion_error_code: str | None = None
    ingested_by: str
    ingested_at: datetime
    last_viewed_at: datetime | None
    source_version: int
    chunk_count: int


class KnowledgeSourceListResponse(BaseModel):
    sources: list[KnowledgeSourceRead]
    quota: KnowledgeQuota


class KnowledgeSourceCreateResponse(BaseModel):
    source_id: int
    source_uuid: str
    ingestion_status: str
    filename: str | None
    size_bytes: int
    ingested_at: datetime
    s3_key: str | None
    warning: str | None = None


class KnowledgeSourcePatchRequest(BaseModel):
    filename: str | None = None
    reingest: bool = False


class KnowledgeChunkRead(BaseModel):
    ordinal: int
    content: str
    content_length_chars: int


class AffectedQuestion(BaseModel):
    trace_id: str
    session_id: str
    user_message: str
    created_at: datetime


class CrawlRequest(BaseModel):
    url: str = Field(..., min_length=8, max_length=2048)
    respect_robots: bool = True


class QuotaExceededDetail(BaseModel):
    """Structured 413 payload per ARC11_PLAN.md §0.4."""

    error: Literal["knowledge_quota_exceeded"]
    scope: Literal["total", "per_file"]
    current_bytes: int
    incoming_bytes: int
    cap_bytes: int
    tier: str
    remediation: Literal["delete_or_upgrade"]


class FeatureNotOnTierDetail(BaseModel):
    error: Literal["feature_not_available_on_tier"]
    tier: str
    feature: str


# =====================================================================
# Helpers
# =====================================================================


def _load_active_instance(
    *,
    request: Request,
    instance_id: int,
    instance_service: InstanceService,
) -> Instance:
    """Same shape as ``app.api.v1.admin._load_active_instance`` but
    declared locally so this module does not have a circular import
    with ``admin.py``. The two stay in sync by convention; if the
    behaviour diverges, surface a refactor PR to lift this helper to
    ``app.api.deps``.
    """
    instance = instance_service.get_by_pk(instance_id)
    if instance is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Instance {instance_id} not found",
        )
    ScopePolicy.enforce_admin_owns_instance(request, instance)
    if not instance.active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Instance {instance_id} is inactive",
        )
    return instance


def _resolve_tier_entitlement(admin_id: str, db) -> tuple[str, Any]:
    """Look up the Admin's tier and return ``(tier, TierEntitlement)``.

    Falls back to Free if the Admin row is missing or the tier value is
    not in ``TIER_ENTITLEMENTS`` (fail-closed posture — unknown tier =
    most-restrictive).
    """
    from app.models.admin import Admin

    row = db.execute(
        select(Admin.tier).where(Admin.id == admin_id)
    ).scalar_one_or_none()
    tier = row if row in TIER_ENTITLEMENTS else TIER_FREE
    return tier, TIER_ENTITLEMENTS[tier]


def _current_usage_bytes(admin_id: str, db) -> int:
    """Sum of ``size_bytes`` over the Admin's non-soft-deleted
    knowledge_sources. Used for the per-Admin total quota check.
    Belt-and-suspenders with RLS — explicit ``WHERE admin_id`` keeps
    the query semantics audit-able in isolation."""
    result = db.execute(
        select(func.coalesce(func.sum(KnowledgeSource.size_bytes), 0))
        .where(
            KnowledgeSource.admin_id == admin_id,
            KnowledgeSource.soft_deleted_at.is_(None),
        )
    ).scalar_one()
    return int(result or 0)


def _resolve_s3_bucket_or_503() -> str:
    """Read ``KNOWLEDGE_S3_BUCKET`` env. Step 11 stamps the SSM
    parameter; absence here is an operational config drift, not a
    user error — return 503."""
    bucket = os.environ.get("KNOWLEDGE_S3_BUCKET")
    if not bucket:
        try:
            from app.core.config import settings
            bucket = getattr(settings, "knowledge_s3_bucket", None)
        except Exception:  # noqa: BLE001
            bucket = None
    if not bucket:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Knowledge service unavailable: bucket not configured. "
                "Operator: set KNOWLEDGE_S3_BUCKET or run the Step 11 "
                "deploy."
            ),
        )
    return bucket


def _upload_bytes_to_s3(
    *, bucket: str, key: str, payload: bytes, content_type: str,
) -> None:
    """Wraps the boto3 PutObject call. Errors surface as 503 — the
    source row should not be created if the upload failed."""
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="boto3 unavailable",
        ) from exc

    try:
        client = boto3.client("s3")
        client.put_object(
            Bucket=bucket, Key=key, Body=payload, ContentType=content_type,
        )
    except Exception as exc:  # noqa: BLE001 — boto raises many shapes
        logger.exception(
            "S3 put_object failed: exc_class=%s bucket=%s",
            type(exc).__name__, bucket,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to upload source bytes to S3",
        ) from exc


def _enqueue_embed_or_warn(
    *, source_pk: int, admin_id: str, instance_id: int,
) -> str | None:
    """Enqueue the embed_source task. Returns a warning string if the
    enqueue failed (caller stamps it into the 202 response per the
    Step 6 contract); returns None on success."""
    try:
        from app.worker.tasks.embed_source import embed_source

        embed_source.apply_async(
            kwargs={
                "source_pk": source_pk,
                "admin_id": admin_id,
                "instance_id": instance_id,
            },
            queue="luciel-knowledge-tasks",
        )
        return None
    except Exception as exc:  # noqa: BLE001 — broker errors are varied
        # Per the Step 6 carry-forward note: leave the row at
        # 'pending', return 202 with a warning. Log to CloudWatch
        # so a monitoring alarm fires before customers notice.
        logger.warning(
            "embed_source enqueue failed: exc_class=%s source_pk=%d",
            type(exc).__name__, source_pk,
        )
        return "queue_enqueue_failed_will_retry"


def _enqueue_crawl_or_warn(
    *, source_pk: int, admin_id: str, instance_id: int,
    url: str, respect_robots: bool,
) -> str | None:
    """Mirror of ``_enqueue_embed_or_warn`` for the crawl stub task."""
    try:
        from app.worker.tasks.crawl_website import crawl_website

        crawl_website.apply_async(
            kwargs={
                "source_pk": source_pk,
                "admin_id": admin_id,
                "instance_id": instance_id,
                "url": url,
                "respect_robots": respect_robots,
            },
            queue="luciel-knowledge-tasks",
        )
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "crawl_website enqueue failed: exc_class=%s source_pk=%d",
            type(exc).__name__, source_pk,
        )
        return "queue_enqueue_failed_will_retry"


def _chunk_count_for(source_pk: int, db) -> int:
    """Count active chunks linked to this source. Used in the GET
    /sources list response."""
    return int(
        db.execute(
            select(func.count(KnowledgeChunk.id))
            .where(
                KnowledgeChunk.source_id == source_pk,
                KnowledgeChunk.soft_deleted_at.is_(None),
                KnowledgeChunk.superseded_at.is_(None),
            )
        ).scalar_one()
    )


def _serialise_source(s: KnowledgeSource, *, chunk_count: int) -> KnowledgeSourceRead:
    return KnowledgeSourceRead(
        source_id=s.id,
        source_uuid=str(s.source_uuid),
        filename=s.filename,
        source_type=s.source_type,
        size_bytes=int(s.size_bytes),
        s3_key=s.s3_key,
        origin_url=s.origin_url,
        ingestion_status=s.ingestion_status,
        ingestion_error=s.ingestion_error,
        ingestion_error_code=s.ingestion_error_code,
        ingested_by=s.ingested_by,
        ingested_at=s.ingested_at,
        last_viewed_at=s.last_viewed_at,
        source_version=int(s.source_version or 1),
        chunk_count=chunk_count,
    )


# =====================================================================
# 4.1 POST /sources — upload file OR paste-text
# =====================================================================


@router.post(
    "/sources",
    response_model=KnowledgeSourceCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE: {
            "model": QuotaExceededDetail,
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "S3 bucket unconfigured or upload failure",
        },
    },
)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
async def create_source(
    request: Request,
    instance_id: int,
    db: TenantScopedDbSession,
    instance_service: Annotated[InstanceService, Depends(get_luciel_instance_service)],
    audit_repo: Annotated[AdminAuditRepository, Depends(get_admin_audit_repository)],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
    file: Annotated[UploadFile | None, File()] = None,
    text: Annotated[str | None, Form()] = None,
    filename: Annotated[str | None, Form()] = None,
) -> KnowledgeSourceCreateResponse:
    """Upload a file OR paste text. Exactly one of (file, text) must
    be present.

    Flow per ARC11_PLAN.md §3.1:
      1. Resolve tier + entitlements.
      2. Per-file quota check (413 with scope=per_file).
      3. Total quota check (413 with scope=total).
      4. Upload bytes to S3 FIRST.
      5. INSERT knowledge_sources row at ``pending``.
      6. Enqueue embed_source (or warn if broker is down).
    """
    instance = _load_active_instance(
        request=request, instance_id=instance_id,
        instance_service=instance_service,
    )
    ScopePolicy.require_knowledge_role(request, instance, action="edit")

    if (file is None) == (text is None):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Exactly one of 'file' or 'text' must be provided",
        )

    # ---- Resolve the bytes + filename + source_type ----
    if file is not None:
        if not file.filename:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="File upload missing filename",
            )
        payload = await file.read()
        effective_filename = filename or file.filename
        # Extension drives source_type at the parser layer; the
        # worker calls detect_source_type if unset.
        ext = os.path.splitext(effective_filename)[1].lower().lstrip(".")
        source_type = ext or "txt"
        content_type = file.content_type or "application/octet-stream"
    else:
        payload = (text or "").encode("utf-8")
        # Paste-text default filename per ARC11_PLAN.md §13 carry-forward
        # (Step 3 note): synthetic filename for UX consistency in the
        # source list.
        now_iso = datetime.now(tz=timezone.utc).isoformat()
        effective_filename = filename or f"paste-{now_iso}.txt"
        source_type = "paste"
        content_type = "text/plain; charset=utf-8"

    incoming_bytes = len(payload)
    admin_id = instance.admin_id

    # ---- Tier + entitlements ----
    tier, ent = _resolve_tier_entitlement(admin_id, db)

    # 2. Per-file cap.
    if incoming_bytes > ent.knowledge_per_file_bytes_cap:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=QuotaExceededDetail(
                error="knowledge_quota_exceeded",
                scope="per_file",
                current_bytes=0,
                incoming_bytes=incoming_bytes,
                cap_bytes=ent.knowledge_per_file_bytes_cap,
                tier=tier,
                remediation="delete_or_upgrade",
            ).model_dump(),
        )

    # 3. Total cap (skip when cap_bytes is None → Enterprise unlimited).
    if ent.knowledge_bytes_cap is not None:
        used = _current_usage_bytes(admin_id, db)
        if used + incoming_bytes > ent.knowledge_bytes_cap:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=QuotaExceededDetail(
                    error="knowledge_quota_exceeded",
                    scope="total",
                    current_bytes=used,
                    incoming_bytes=incoming_bytes,
                    cap_bytes=ent.knowledge_bytes_cap,
                    tier=tier,
                    remediation="delete_or_upgrade",
                ).model_dump(),
            )

    # ---- Compose S3 key + upload first ----
    source_uuid = uuid.uuid4()
    ext_for_key = (
        os.path.splitext(effective_filename)[1]
        if effective_filename else ""
    )
    s3_key = f"{admin_id}/{source_uuid}{ext_for_key or '.bin'}"
    bucket = _resolve_s3_bucket_or_503()
    _upload_bytes_to_s3(
        bucket=bucket, key=s3_key, payload=payload,
        content_type=content_type,
    )

    # ---- Create the source row ----
    source_repo = KnowledgeSourceRepository(db)
    ingested_by = (
        getattr(request.state, "actor_label", None)
        or getattr(request.state, "key_prefix", None)
        or "unknown"
    )
    source = source_repo.create_source(
        admin_id=admin_id,
        luciel_instance_id=instance.id,
        filename=effective_filename,
        source_type=source_type,
        size_bytes=incoming_bytes,
        s3_key=s3_key,
        ingested_by=ingested_by,
        ingestion_status="pending",
    )
    # Override the auto-generated UUID so the S3 key and the row stay
    # in sync (we already used the synthesised one for the S3 key).
    source.source_uuid = source_uuid
    db.flush()

    # ---- Audit row ----
    audit_repo.record(
        ctx=audit_ctx,
        admin_id=admin_id,
        action=ACTION_KNOWLEDGE_SOURCE_CREATED,
        resource_type=RESOURCE_KNOWLEDGE_SOURCE,
        resource_pk=source.id,
        resource_natural_id=str(source_uuid),
        luciel_instance_id=instance.id,
        after={
            "source_type": source_type,
            "size_bytes": incoming_bytes,
        },
    )
    db.commit()

    # ---- Enqueue worker ----
    warning = _enqueue_embed_or_warn(
        source_pk=source.id,
        admin_id=admin_id,
        instance_id=instance.id,
    )

    return KnowledgeSourceCreateResponse(
        source_id=source.id,
        source_uuid=str(source_uuid),
        ingestion_status=source.ingestion_status,
        filename=source.filename,
        size_bytes=source.size_bytes,
        ingested_at=source.ingested_at,
        s3_key=source.s3_key,
        warning=warning,
    )


# =====================================================================
# 4.2 GET /sources — list
# =====================================================================


@router.get(
    "/sources",
    response_model=KnowledgeSourceListResponse,
)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def list_sources(
    request: Request,
    instance_id: int,
    db: TenantScopedDbSession,
    instance_service: Annotated[InstanceService, Depends(get_luciel_instance_service)],
    audit_repo: Annotated[AdminAuditRepository, Depends(get_admin_audit_repository)],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> KnowledgeSourceListResponse:
    instance = _load_active_instance(
        request=request, instance_id=instance_id,
        instance_service=instance_service,
    )
    ScopePolicy.require_knowledge_role(request, instance, action="list")

    source_repo = KnowledgeSourceRepository(db)
    sources = list(source_repo.list_sources_for_instance(
        admin_id=instance.admin_id,
        luciel_instance_id=instance.id,
        limit=limit,
        offset=offset,
    ))

    serialised = [
        _serialise_source(s, chunk_count=_chunk_count_for(s.id, db))
        for s in sources
    ]
    # Touch last_viewed_at on every row returned.
    if sources:
        source_repo.touch_last_viewed(
            [s.id for s in sources],
            admin_id=instance.admin_id,
        )

    tier, ent = _resolve_tier_entitlement(instance.admin_id, db)
    quota = KnowledgeQuota(
        used_bytes=_current_usage_bytes(instance.admin_id, db),
        cap_bytes=ent.knowledge_bytes_cap,
        per_file_cap_bytes=ent.knowledge_per_file_bytes_cap,
        tier=tier,
    )

    audit_repo.record(
        ctx=audit_ctx,
        admin_id=instance.admin_id,
        action=ACTION_KNOWLEDGE_SOURCE_LISTED,
        resource_type=RESOURCE_KNOWLEDGE_SOURCE,
        luciel_instance_id=instance.id,
        note=f"count={len(serialised)}",
    )
    db.commit()

    return KnowledgeSourceListResponse(sources=serialised, quota=quota)


# =====================================================================
# 4.3 GET /sources/{source_id}/chunks — chunk preview
# =====================================================================


@router.get(
    "/sources/{source_id}/chunks",
    response_model=list[KnowledgeChunkRead],
)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def preview_chunks(
    request: Request,
    instance_id: int,
    source_id: int,
    db: TenantScopedDbSession,
    instance_service: Annotated[InstanceService, Depends(get_luciel_instance_service)],
    audit_repo: Annotated[AdminAuditRepository, Depends(get_admin_audit_repository)],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
    limit: int = Query(default=10, ge=1, le=50),
) -> list[KnowledgeChunkRead]:
    instance = _load_active_instance(
        request=request, instance_id=instance_id,
        instance_service=instance_service,
    )
    ScopePolicy.require_knowledge_role(request, instance, action="view")

    source_repo = KnowledgeSourceRepository(db)
    source = source_repo.get_source(source_id, admin_id=instance.admin_id)
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Knowledge source {source_id} not found",
        )

    rows = db.execute(
        select(KnowledgeChunk.id, KnowledgeChunk.content)
        .where(
            KnowledgeChunk.source_id == source_id,
            KnowledgeChunk.admin_id == instance.admin_id,
            KnowledgeChunk.soft_deleted_at.is_(None),
            KnowledgeChunk.superseded_at.is_(None),
        )
        .order_by(KnowledgeChunk.id.asc())
        .limit(limit)
    ).all()

    out = [
        KnowledgeChunkRead(
            ordinal=idx,
            content=row.content,
            content_length_chars=len(row.content or ""),
        )
        for idx, row in enumerate(rows)
    ]

    audit_repo.record(
        ctx=audit_ctx,
        admin_id=instance.admin_id,
        action=ACTION_KNOWLEDGE_SOURCE_VIEWED,
        resource_type=RESOURCE_KNOWLEDGE_SOURCE,
        resource_pk=source_id,
        luciel_instance_id=instance.id,
        note=f"chunks_returned={len(out)}",
    )
    db.commit()

    return out


# =====================================================================
# 4.4 PATCH /sources/{source_id} — rename and/or reingest
# =====================================================================


@router.patch(
    "/sources/{source_id}",
    response_model=KnowledgeSourceRead,
)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def update_source(
    request: Request,
    instance_id: int,
    source_id: int,
    payload: KnowledgeSourcePatchRequest,
    db: TenantScopedDbSession,
    instance_service: Annotated[InstanceService, Depends(get_luciel_instance_service)],
    audit_repo: Annotated[AdminAuditRepository, Depends(get_admin_audit_repository)],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> KnowledgeSourceRead:
    instance = _load_active_instance(
        request=request, instance_id=instance_id,
        instance_service=instance_service,
    )
    ScopePolicy.require_knowledge_role(request, instance, action="edit")

    source_repo = KnowledgeSourceRepository(db)

    before = {}
    try:
        if payload.filename is not None:
            source = source_repo.get_source(source_id, admin_id=instance.admin_id)
            if source is None:
                raise KnowledgeSourceNotFound(
                    f"knowledge_sources.id={source_id}"
                )
            before["filename"] = source.filename
            source = source_repo.rename(
                source_id,
                admin_id=instance.admin_id,
                new_filename=payload.filename,
            )

        if payload.reingest:
            source = source_repo.bump_version(
                source_id, admin_id=instance.admin_id,
            )
        elif payload.filename is None:
            # Neither field set — just return the current state.
            source = source_repo.get_source(source_id, admin_id=instance.admin_id)
            if source is None:
                raise KnowledgeSourceNotFound(
                    f"knowledge_sources.id={source_id}"
                )
    except KnowledgeSourceNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Knowledge source {source_id} not found",
        )

    chunk_count = _chunk_count_for(source.id, db)

    audit_repo.record(
        ctx=audit_ctx,
        admin_id=instance.admin_id,
        action=ACTION_KNOWLEDGE_SOURCE_UPDATED,
        resource_type=RESOURCE_KNOWLEDGE_SOURCE,
        resource_pk=source.id,
        luciel_instance_id=instance.id,
        before=before or None,
        after={
            "filename": source.filename,
            "reingest": payload.reingest,
            "source_version": source.source_version,
        },
    )
    db.commit()

    # Enqueue another embed run if the caller asked for a reingest.
    # Done AFTER commit so the worker sees the bumped row.
    if payload.reingest:
        _enqueue_embed_or_warn(
            source_pk=source.id,
            admin_id=instance.admin_id,
            instance_id=instance.id,
        )

    return _serialise_source(source, chunk_count=chunk_count)


# =====================================================================
# 4.5 DELETE /sources/{source_id} — soft-delete with chunk cascade
# =====================================================================


@router.delete(
    "/sources/{source_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def delete_source(
    request: Request,
    instance_id: int,
    source_id: int,
    db: TenantScopedDbSession,
    instance_service: Annotated[InstanceService, Depends(get_luciel_instance_service)],
    audit_repo: Annotated[AdminAuditRepository, Depends(get_admin_audit_repository)],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> None:
    instance = _load_active_instance(
        request=request, instance_id=instance_id,
        instance_service=instance_service,
    )
    ScopePolicy.require_knowledge_role(request, instance, action="delete")

    source_repo = KnowledgeSourceRepository(db)
    chunk_repo = KnowledgeRepository(db)

    try:
        source_repo.soft_delete(source_id, admin_id=instance.admin_id)
    except KnowledgeSourceNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Knowledge source {source_id} not found",
        )

    chunks_soft_deleted = chunk_repo.soft_delete_chunks_for_source_id(
        source_id=source_id, admin_id=instance.admin_id,
    )

    audit_repo.record(
        ctx=audit_ctx,
        admin_id=instance.admin_id,
        action=ACTION_KNOWLEDGE_SOURCE_DELETED,
        resource_type=RESOURCE_KNOWLEDGE_SOURCE,
        resource_pk=source_id,
        luciel_instance_id=instance.id,
        note=f"chunks_soft_deleted={chunks_soft_deleted}",
    )
    db.commit()


# =====================================================================
# 4.6 GET /sources/{source_id}/affected-questions — modal preview
# =====================================================================


@router.get(
    "/sources/{source_id}/affected-questions",
    response_model=list[AffectedQuestion],
)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def affected_questions(
    request: Request,
    instance_id: int,
    source_id: int,
    db: TenantScopedDbSession,
    instance_service: Annotated[InstanceService, Depends(get_luciel_instance_service)],
    audit_repo: Annotated[AdminAuditRepository, Depends(get_admin_audit_repository)],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
    limit: int = Query(default=5, ge=1, le=20),
) -> list[AffectedQuestion]:
    instance = _load_active_instance(
        request=request, instance_id=instance_id,
        instance_service=instance_service,
    )
    # Reading the preview is a "view" action — operators see it too,
    # not just owners/managers, because the modal preview surfaces
    # alongside any list/view interaction.
    ScopePolicy.require_knowledge_role(request, instance, action="view")

    trace_repo = TraceRepository(db)
    rows = trace_repo.list_recent_traces_using_source(
        admin_id=instance.admin_id,
        luciel_instance_id=instance.id,
        source_id=source_id,
        limit=limit,
    )

    out = [
        AffectedQuestion(
            trace_id=t.trace_id,
            session_id=t.session_id,
            user_message=t.user_message,
            created_at=t.created_at,
        )
        for t in rows
    ]

    audit_repo.record(
        ctx=audit_ctx,
        admin_id=instance.admin_id,
        action=ACTION_KNOWLEDGE_AFFECTED_QUESTIONS_VIEWED,
        resource_type=RESOURCE_KNOWLEDGE_SOURCE,
        resource_pk=source_id,
        luciel_instance_id=instance.id,
        note=f"matches={len(out)}",
    )
    db.commit()

    return out


# =====================================================================
# 4.7 POST /crawl — website crawl (Pro/Enterprise only; deferred-feature stub)
# =====================================================================


@router.post(
    "/crawl",
    response_model=KnowledgeSourceCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        status.HTTP_403_FORBIDDEN: {"model": FeatureNotOnTierDetail},
    },
)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def start_crawl(
    request: Request,
    instance_id: int,
    payload: CrawlRequest,
    db: TenantScopedDbSession,
    instance_service: Annotated[InstanceService, Depends(get_luciel_instance_service)],
    audit_repo: Annotated[AdminAuditRepository, Depends(get_admin_audit_repository)],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> KnowledgeSourceCreateResponse:
    instance = _load_active_instance(
        request=request, instance_id=instance_id,
        instance_service=instance_service,
    )
    ScopePolicy.require_knowledge_role(request, instance, action="edit")

    admin_id = instance.admin_id
    tier, ent = _resolve_tier_entitlement(admin_id, db)
    if not ent.knowledge_website_crawl_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=FeatureNotOnTierDetail(
                error="feature_not_available_on_tier",
                tier=tier,
                feature="website_crawl",
            ).model_dump(),
        )

    # Create a knowledge_sources row at 'pending' with source_type='crawl'
    # and the origin URL. The crawl_website stub task flips it to
    # 'failed' with the CRAWL_NOT_YET_AVAILABLE deferred-feature code.
    source_uuid = uuid.uuid4()
    source_repo = KnowledgeSourceRepository(db)
    ingested_by = (
        getattr(request.state, "actor_label", None)
        or getattr(request.state, "key_prefix", None)
        or "unknown"
    )
    source = source_repo.create_source(
        admin_id=admin_id,
        luciel_instance_id=instance.id,
        filename=None,
        source_type="crawl",
        # Crawl sources have no incoming bytes at enqueue time.
        # When Arc 14 lands the real crawler, it'll update this
        # row's size_bytes after the fetch completes.
        size_bytes=0,
        # No S3 key yet — the crawler writes one on success.
        s3_key=None,
        origin_url=payload.url,
        ingested_by=ingested_by,
        ingestion_status="pending",
    )
    source.source_uuid = source_uuid
    db.flush()

    audit_repo.record(
        ctx=audit_ctx,
        admin_id=admin_id,
        action=ACTION_KNOWLEDGE_CRAWL_ENQUEUED,
        resource_type=RESOURCE_KNOWLEDGE_SOURCE,
        resource_pk=source.id,
        resource_natural_id=str(source_uuid),
        luciel_instance_id=instance.id,
        # URL is NOT logged in the audit note per the PII discipline
        # propagated from Step 6 — store only the source_pk + uuid.
    )
    db.commit()

    warning = _enqueue_crawl_or_warn(
        source_pk=source.id,
        admin_id=admin_id,
        instance_id=instance.id,
        url=payload.url,
        respect_robots=payload.respect_robots,
    )

    return KnowledgeSourceCreateResponse(
        source_id=source.id,
        source_uuid=str(source_uuid),
        ingestion_status=source.ingestion_status,
        filename=None,
        size_bytes=0,
        ingested_at=source.ingested_at,
        s3_key=None,
        warning=warning,
    )


# =====================================================================
# 4.8 POST /internal/v1/retrieve — verification endpoint
# (mounted under a separate router; see admin_knowledge_internal below)
# =====================================================================


internal_router = APIRouter(
    prefix="/internal/v1",
    tags=["internal-verification"],
)


class InternalRetrieveRequest(BaseModel):
    admin_id: str = Field(..., min_length=1, max_length=100)
    instance_id: int
    query: str = Field(..., min_length=1, max_length=4000)
    top_k: int = Field(default=5, ge=1, le=50)


class InternalRetrieveChunk(BaseModel):
    chunk_id: int
    content: str
    distance: float | None
    source_identifier: int | str | None


class InternalRetrieveResponse(BaseModel):
    chunks: list[InternalRetrieveChunk]
    explain: str


@internal_router.post(
    "/retrieve",
    response_model=InternalRetrieveResponse,
)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def internal_retrieve(
    request: Request,
    payload: InternalRetrieveRequest,
    db: DbSession,
) -> InternalRetrieveResponse:
    """Verification endpoint (Pillar 2). Platform-admin only.

    Runs the actual ``KnowledgeRetriever.retrieve_with_sources`` under
    the bound ``admin_id`` so the query passes through the same RLS
    fence the chat path will. Returns the retrieved chunks plus an
    ``EXPLAIN ANALYZE`` text dump so a verify run can confirm the
    HNSW index plan is being picked.
    """
    if not ScopePolicy.is_platform_admin(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="POST /internal/v1/retrieve requires platform_admin",
        )

    from app.knowledge.embedder import embed_single
    from app.knowledge.retriever import KnowledgeRetriever
    from app.repositories.knowledge_repository import KnowledgeRepository

    with bind_tenant_scope(
        admin_id=payload.admin_id, instance_id=payload.instance_id,
    ):
        chunk_repo = KnowledgeRepository(db)
        retriever = KnowledgeRetriever(chunk_repo)
        retrieved = retriever.retrieve_with_sources(
            query=payload.query,
            admin_id=payload.admin_id,
            luciel_instance_id=payload.instance_id,
            limit=payload.top_k,
        )

        # EXPLAIN ANALYZE of a representative retriever-shaped query.
        # We rebuild the query embedding once and inline it into the
        # EXPLAIN text — keeps the planner output deterministic
        # without exposing the vector to the response.
        try:
            query_vec = embed_single(payload.query)
            vec_lit = "[" + ",".join(f"{float(x):.7f}" for x in query_vec) + "]"
            explain_rows = db.execute(
                sql_text(
                    "EXPLAIN ANALYZE SELECT id, embedding <=> CAST(:v AS vector) AS d "
                    "FROM knowledge_chunks "
                    "WHERE admin_id = :aid "
                    "  AND luciel_instance_id = :iid "
                    "  AND superseded_at IS NULL "
                    "  AND soft_deleted_at IS NULL "
                    "ORDER BY embedding <=> CAST(:v AS vector) "
                    "LIMIT :n"
                ).bindparams(
                    v=vec_lit,
                    aid=payload.admin_id,
                    iid=payload.instance_id,
                    n=payload.top_k,
                )
            ).all()
            explain_text = "\n".join(r[0] for r in explain_rows)
        except Exception as exc:  # noqa: BLE001
            # In environments without pgvector or without the embedder
            # configured, EXPLAIN can't run. Surface the failure as a
            # short note rather than 500-ing the whole probe.
            explain_text = (
                f"<EXPLAIN unavailable: {type(exc).__name__}>"
            )

    return InternalRetrieveResponse(
        chunks=[
            InternalRetrieveChunk(
                chunk_id=c.chunk_id,
                content=c.content,
                distance=c.distance,
                source_identifier=c.source_identifier,
            )
            for c in retrieved
        ],
        explain=explain_text,
    )
