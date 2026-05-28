"""Arc 11 Step 7 — website crawl Celery task (deferred-feature stub).

The /admin/instances/{instance_id}/knowledge/crawl route enqueues
this task. For Arc 11 the implementation is a deliberate stub: the
task immediately flips the source row to ``failed`` with the
canonical ``CRAWL_NOT_YET_AVAILABLE`` error code and a
human-readable ``ingestion_error`` text, then returns. The route
itself, its tier-gating, and its 202/403 status codes are all
real; the worker side of the contract is what's deferred.

Why ship the route now if the worker is a stub
----------------------------------------------

Three reasons:

  1. Tier gating gets a real test surface today. Free returns
     ``403 feature_not_available_on_tier`` and Pro/Enterprise
     returns ``202`` with the source row at ``pending`` /
     ``failed`` — both observable from integration tests without
     waiting for the crawler.

  2. The frontend can render the "Crawl a website" card with its
     full UX (upgrade gate, URL input, robots toggle) and the
     API contract is stable. When the real crawler lands this
     file fills in; no route changes.

  3. The CFN bucket, IAM grants, and SQS queue from Step 6 are
     also already in place — the real crawler just has to put
     crawled bytes into ``s3_key`` and call into the shared
     embed path, same as a file upload.

Cross-repo contract
-------------------

Arc 11 Closeout PR-B replaced the earlier "Arc-14 substring"
contract with a structured error code. The frontend in
``Luciel-Website/src/components/knowledge/SourceList.tsx`` keys
its "⏱ Coming soon" badge on
``ingestion_error_code === "CRAWL_NOT_YET_AVAILABLE"``. The
human-readable ``_DEFERRED_ERROR_MESSAGE`` below is for ops
debugging only and carries no internal arc identifier.

When the real crawler ships this stub is replaced with a real
fetcher that respects ``robots.txt``, sets a Luciel-identifying
User-Agent, caps page count + bytes, and writes the crawled HTML
to S3 under ``crawl-{source_uuid}.html`` before flipping the
source row to ``ready`` via the existing embed_source pipeline.
"""
from __future__ import annotations

import logging

from celery import shared_task

from app.db.session import SessionLocal
from app.db.tenant_scope import bind_tenant_scope
from app.models.knowledge_source_errors import IngestionErrorCode
from app.repositories.knowledge_source_repository import (
    KnowledgeSourceNotFound,
    KnowledgeSourceRepository,
)

logger = logging.getLogger(__name__)


# Human-readable text persisted to ``knowledge_sources.ingestion_error``
# for ops debugging. Contains NO internal arc identifier — the
# user-facing contract is the structured ``ingestion_error_code``
# column. See ``app.models.knowledge_source_errors.IngestionErrorCode``.
_DEFERRED_ERROR_MESSAGE = (
    "Website crawl is not yet available. Coming soon."
)

# Cross-repo contract: the canonical machine-readable code the
# frontend keys badge rendering on. Defined as a module-level
# constant so the cross-repo contract test can assert on it
# without importing the enum.
_DEFERRED_ERROR_CODE = IngestionErrorCode.CRAWL_NOT_YET_AVAILABLE.value


@shared_task(
    name="app.worker.tasks.crawl_website.crawl_website",
    bind=True,
    max_retries=0,           # the stub never retries — deferred is final
    acks_late=True,
    ignore_result=True,
    queue="luciel-knowledge-tasks",
)
def crawl_website(
    self,
    *,
    source_pk: int,
    admin_id: str,
    instance_id: int,
    url: str,                # noqa: ARG001 — accepted for API parity
    respect_robots: bool,    # noqa: ARG001 — same
) -> None:
    """Deferred-feature stub. Marks the source row 'failed' with the
    canonical ``CRAWL_NOT_YET_AVAILABLE`` code and returns.

    Payload mirrors ``embed_source``'s opaque-ids-only convention
    plus the two crawl-specific fields the API surfaces. The URL is
    NOT logged here — paste-text PII discipline (Step 6) extends
    to crawl URLs.
    """
    log_pfx = (
        f"crawl_website[source_pk={source_pk} "
        f"admin_prefix={(admin_id or '')[:8]}]"
    )
    logger.info(
        "%s STUB run; flipping source to 'failed' with code=%s",
        log_pfx, _DEFERRED_ERROR_CODE,
    )

    with bind_tenant_scope(admin_id=admin_id, instance_id=instance_id):
        db = SessionLocal()
        try:
            repo = KnowledgeSourceRepository(db)
            try:
                repo.mark_status(
                    source_pk,
                    admin_id=admin_id,
                    status="failed",
                    error=_DEFERRED_ERROR_MESSAGE,
                    error_code=_DEFERRED_ERROR_CODE,
                    autocommit=True,
                )
            except KnowledgeSourceNotFound:
                # Source was deleted between enqueue and execution —
                # treat as no-op. Matches embed_source's posture.
                logger.info(
                    "%s source row missing — no-op", log_pfx,
                )
        finally:
            db.close()
