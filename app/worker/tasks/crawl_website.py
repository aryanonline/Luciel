"""Arc 11 Step 7 — website crawl Celery task (Arc-14 stub).

The /admin/instances/{instance_id}/knowledge/crawl route enqueues
this task. For Arc 11 the implementation is a deliberate stub: the
task immediately flips the source row to ``failed`` with a
``"crawl implementation deferred to Arc 14"`` error string and
returns. The route itself, its tier-gating, and its 202/403 status
codes are all real; the worker side of the contract is what's
deferred.

Why ship the route now if the worker is a stub
----------------------------------------------

Three reasons:

  1. Tier gating gets a real test surface today. Free returns
     ``403 feature_not_available_on_tier`` and Pro/Enterprise
     returns ``202`` with the source row at ``pending`` /
     ``failed`` — both observable from integration tests without
     waiting for the crawler.

  2. The frontend (Step 9) can render the "Crawl a website"
     card with its full UX (upgrade gate, URL input, robots
     toggle) and the API contract is stable. When Arc 14 ships
     the real crawler this file fills in; no route changes.

  3. The CFN bucket, IAM grants, and SQS queue from Step 6 are
     also already in place — Arc 14 just has to put crawled
     bytes into ``s3_key`` and call into the shared embed path,
     same as a file upload.

TODO(Arc-14): replace the body with a real fetcher that respects
``robots.txt``, sets a Luciel-identifying User-Agent, caps page
count + bytes, and writes the crawled HTML to S3 under
``crawl-{source_uuid}.html`` before flipping the source row to
``ready`` via the existing embed_source pipeline.
"""
from __future__ import annotations

import logging

from celery import shared_task

from app.db.session import SessionLocal
from app.db.tenant_scope import bind_tenant_scope
from app.repositories.knowledge_source_repository import (
    KnowledgeSourceNotFound,
    KnowledgeSourceRepository,
)

logger = logging.getLogger(__name__)


# Cross-repo contract: this string is persisted to
# ``knowledge_sources.ingestion_error`` and the frontend's
# ``Luciel-Website/src/lib/knowledge.ts::isCrawlComingSoon`` greps
# for the literal substring ``"Arc-14"`` (with a hyphen) to render
# the "⏱ Coming soon" badge instead of a red "Failed" one. The
# hyphenated spelling MUST stay in this string — Step 10's
# cross-repo contract test (tests/integrity/test_arc11_cross_repo_
# contract.py) fails if it disappears.
_ARC14_DEFERRED_ERROR = "crawl implementation deferred to Arc-14"


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
    """Arc-14 stub. Marks the source row 'failed' with the deferred-
    feature error and returns.

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
        "%s STUB run; flipping source to 'failed' with Arc-14 deferred error",
        log_pfx,
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
                    error=_ARC14_DEFERRED_ERROR,
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
