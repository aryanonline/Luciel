"""Arc 11 Step 6 — Celery task that drives a knowledge_sources row
from ``pending`` to ``ready``.

Contract
========

The API boundary (Step 7) does the synchronous part of an ingest:
write a ``knowledge_sources`` row at ``ingestion_status='pending'``,
upload the raw bytes to S3, then enqueue this task. The task does
the slow part — download bytes, parse, chunk, embed, persist — and
flips ``ingestion_status`` to ``ready`` (or ``failed``).

Payload (opaque ids only — NO content):

    source_pk            int   knowledge_sources.id (the row to drive)
    admin_id             str   tenant binding (Wall 1)
    instance_id          int   instance binding (Wall 3)

No filename, no content, no S3 key in the payload — the S3 key is
stored on the source row by Step 7 and read by the worker. This
keeps payload PII out of the broker entirely (matching the
``memory_extraction.py`` doctrine: opaque ids only).

RLS posture
===========

Arc 9 C4.4: ``bind_tenant_scope(admin_id=..., instance_id=...)``
binds the Wall-1 + Wall-3 GUC ContextVars BEFORE the DB session
opens. The first BEGIN on that session then carries the GUCs and
every RLS policy fences correctly. ``OpsSessionLocal`` / the
``luciel_ops`` BYPASSRLS role is the wrong primitive for this work
— per-tenant ingestion must be RLS-fenced, never bypass-RLS.

S3 layout
=========

Every source — including paste-text — has an ``s3_key``. Step 7 is
responsible for uploading the raw bytes to S3 before enqueueing
this task; the worker uniformly downloads + processes. Paste-text
uses keys of the form ``paste-{source_uuid}.txt``; file uploads
use ``{admin_id}/{source_uuid}/{filename}``. The exact prefix
scheme is owned by Step 7's API; the worker treats ``s3_key`` as
opaque.

Retry semantics
===============

Transient failures (S3 read timeout, embedding-provider 429, DB
operational error) retry up to ``max_retries=3`` with exponential
backoff. The retry list lives in ``_TRANSIENT_EXC`` so future
additions are explicit. Permanent failures (parse error, bad
``source_type``, source row missing, etc.) flip the row to
``failed`` with a sanitised error string and return — they do NOT
retry because retrying a corrupt source just wastes worker
budget.

Idempotency
===========

A task fired against a source already in ``ready`` state is a
no-op. This makes DLQ redrive and at-least-once delivery safe.
Sources in ``processing`` state are re-driven (the chunks-with-
matching-``source_id`` are deleted first via a tight chunk wipe so
we don't accumulate duplicates across retries — see
``_clear_existing_chunks_for_source``).

PII discipline
==============

The task never logs ``source.filename``, never logs S3 keys (which
embed filenames in their suffix), never logs chunk content. It
logs ONLY:

  * ``source_pk`` (the integer PK — opaque outside the DB).
  * The first 8 chars of ``admin_id`` (tenant-prefix only — never
    the full id, which can match against external systems).
  * The exception class name on failure (not the message — exception
    messages have a habit of carrying caller-supplied text).
  * Counters: chunk_count, byte_count, duration_ms.

Matches the Arc 9 worker log-hygiene doctrine spelled out in
``celery_app.py:14``.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import redis.exceptions as _redis_exc
from celery import shared_task
from sqlalchemy.exc import OperationalError as _SAOperationalError

from app.db.session import SessionLocal
from app.db.tenant_scope import bind_tenant_scope  # Arc 9 C4.4
from app.knowledge.chunker import chunk_text, resolve_effective_config
from app.knowledge.embedder import embed_texts
from app.knowledge.parsers import (
    ParserError,
    UnsupportedSourceType,
    detect_source_type,
    get_parser,
)
from app.models.admin import Admin
from app.models.instance import Instance
from app.repositories.knowledge_repository import KnowledgeRepository
from app.repositories.knowledge_source_repository import (
    KnowledgeSourceNotFound,
    KnowledgeSourceRepository,
)

logger = logging.getLogger(__name__)


# ---------- exception taxonomy ----------
# Anything in ``_TRANSIENT_EXC`` triggers a retry via autoretry_for.
# Anything else is permanent: flips the source row to 'failed' and
# returns.


class TransientIngestionError(Exception):
    """Raised by the task body when a network / infrastructure
    failure should be retried."""


class IngestionPermanentError(Exception):
    """Parser failure, empty text, bad source_type, missing admin —
    anything that won't get better on retry. Subclasses are treated
    identically by the task body's except clauses."""


class IngestionConfigError(IngestionPermanentError):
    """Configuration drift (missing bucket name, missing s3_key on
    the row). Permanent — retries won't fix it; an operator has to
    repair the config."""


# Boto3's S3 client raises botocore.exceptions.ClientError + a
# small zoo of EndpointConnectionError variants for transient
# blips. We catch them broadly inside ``_download_bytes`` and
# re-raise as TransientIngestionError so the retry surface stays
# explicit at the task level.
_TRANSIENT_EXC: tuple[type[Exception], ...] = (
    TransientIngestionError,
    _SAOperationalError,
    _redis_exc.ConnectionError,
)


# ---------- helpers ----------
def _log_prefix(admin_id: str, source_pk: int) -> str:
    """Build an opaque log prefix. ``admin_id`` is hashed-style
    truncated; the full id never appears."""
    aid_prefix = (admin_id or "")[:8]
    return f"embed_source[source_pk={source_pk} admin_prefix={aid_prefix}]"


def _sanitise_error(exc: BaseException, cap: int = 1000) -> str:
    """Build the string we write to ``knowledge_sources.ingestion_error``.

    Uses ONLY the exception class name + a short repr. We do not
    log this string to CloudWatch (only the class name goes to the
    log) — but it IS persisted to the row so an admin can see what
    went wrong via the source-list view. ``str(exc)`` would surface
    caller-supplied content (filename, parsed text fragments) so we
    use ``repr(exc)`` capped at ``cap`` chars, which keeps the
    class name + arg tuple inspectable without dumping unbounded
    payload."""
    body = repr(exc)
    if len(body) > cap:
        body = body[: cap - 3] + "..."
    return body


def _download_bytes(s3_key: str, bucket: str) -> bytes:
    """Pull the raw upload bytes out of S3. Wraps boto3 transient
    errors into TransientIngestionError so the autoretry surface
    is uniform.

    Lazy boto3 import to keep cold-start fast — the worker process
    is the only caller; the chat path never imports this module.
    """
    try:
        import boto3
        from botocore.exceptions import (
            ClientError,
            EndpointConnectionError,
        )
    except ImportError as exc:  # pragma: no cover — prod has boto3
        raise TransientIngestionError(
            f"boto3 unavailable: {type(exc).__name__}"
        ) from exc

    s3 = boto3.client("s3")
    try:
        resp = s3.get_object(Bucket=bucket, Key=s3_key)
        return resp["Body"].read()
    except EndpointConnectionError as exc:
        raise TransientIngestionError(
            f"s3 endpoint connection failure: {type(exc).__name__}"
        ) from exc
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        # 5xx + throttling -> transient. 4xx (NoSuchKey, AccessDenied,
        # InvalidObjectState) -> permanent.
        if code in ("InternalError", "SlowDown", "ServiceUnavailable", "RequestTimeout"):
            raise TransientIngestionError(
                f"s3 transient error: {code}"
            ) from exc
        raise  # permanent — caller marks 'failed'.


def _clear_existing_chunks_for_source(
    db, *, admin_id: str, source_pk: int,
) -> int:
    """Hard-delete any chunks already linked to this ``source_id``
    before re-running the pipeline. Used when a retry / DLQ redrive
    fires against a source that was mid-processing.

    Returns the number of rows deleted. Uses raw SQL because the
    ORM relationship is a viewonly+lazy-select setup — bulk delete
    is straightforward in raw SQL and avoids the per-row ORM
    overhead. RLS still applies: the connection's
    ``app.admin_id`` GUC fences the delete.
    """
    from sqlalchemy import text as sql_text

    result = db.execute(
        sql_text(
            """
            DELETE FROM knowledge_chunks
             WHERE source_id = :sid
               AND admin_id = :aid
            """
        ),
        {"sid": source_pk, "aid": admin_id},
    )
    return result.rowcount or 0


def _resolve_bucket_name() -> str | None:
    """The S3 bucket name lives in env (``KNOWLEDGE_S3_BUCKET``)
    or settings. Returning ``None`` is fine for tests / dev paths
    that never actually call S3; the task fails loudly only when
    a real source is processed without a bucket configured."""
    import os
    val = os.environ.get("KNOWLEDGE_S3_BUCKET")
    if val:
        return val
    # Optional fallback to settings if the team prefers SSM-driven
    # config — read defensively so absence is not a hard import error.
    try:
        from app.core.config import settings
        candidate = getattr(settings, "knowledge_s3_bucket", None)
        if candidate:
            return str(candidate)
    except Exception:  # noqa: BLE001
        pass
    return None


# ---------- task ----------
@shared_task(
    name="app.worker.tasks.embed_source.embed_source",
    bind=True,
    max_retries=3,
    default_retry_delay=2,
    autoretry_for=_TRANSIENT_EXC,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    acks_late=True,
    ignore_result=True,
    queue="luciel-knowledge-tasks",
)
def embed_source(
    self,
    *,
    source_pk: int,
    admin_id: str,
    instance_id: int,
) -> None:
    """Drive a ``knowledge_sources`` row through the ingestion pipeline.

    See module docstring for the full contract. This function:

      1. Binds Wall-1 + Wall-3 scope BEFORE opening any DB session
         (so the very first BEGIN sees the GUCs and RLS fences).
      2. Loads the source row. Missing row -> no-op (source was
         deleted between enqueue and execution). Already-ready
         row -> no-op (idempotent).
      3. Marks the row ``processing``.
      4. Downloads bytes from S3 via ``source.s3_key``.
      5. Parses + chunks + embeds + persists chunks with
         ``source_id = source_pk`` (post-Cleanup-B: the INTEGER FK
         is the sole source binding; the legacy stringy ``source_id``
         column + free-text ``source`` column are gone).
      6. Marks the row ``ready``.

    Any exception in steps 4-5 flips the row to ``failed`` (with a
    sanitised error string) and re-raises so the broker sees the
    failure. Transient exceptions trigger autoretry; permanent
    exceptions land in DLQ once retries are exhausted.
    """
    log_pfx = _log_prefix(admin_id, source_pk)
    started_at = time.monotonic()

    # Arc 9 C4.4: scope BEFORE session. The first BEGIN on this
    # session must see the bound GUCs so RLS works. Opening
    # SessionLocal() outside the with-block would emit empty GUCs
    # on a lazy BEGIN that then linger on the pooled connection.
    with bind_tenant_scope(admin_id=admin_id, instance_id=instance_id):
        db = SessionLocal()
        source_repo = KnowledgeSourceRepository(db)
        chunk_repo = KnowledgeRepository(db)

        try:
            # ---------- 1. Load source row ----------
            try:
                source = source_repo.get_source(
                    source_pk,
                    admin_id=admin_id,
                    include_soft_deleted=True,
                )
            except KnowledgeSourceNotFound:
                source = None

            if source is None:
                logger.info(
                    "%s source row missing — treating as already deleted "
                    "(no-op, ack and return)",
                    log_pfx,
                )
                return

            if source.soft_deleted_at is not None:
                logger.info(
                    "%s source row soft-deleted before worker ran "
                    "(no-op, ack and return)",
                    log_pfx,
                )
                return

            if source.ingestion_status == "ready":
                logger.info(
                    "%s source row already 'ready' (idempotent no-op)",
                    log_pfx,
                )
                return

            # ---------- 2. Flip to processing ----------
            source_repo.mark_status(
                source_pk,
                admin_id=admin_id,
                status="processing",
            )
            db.commit()

            # If we ARE mid-retry after a partial write (chunks
            # already inserted but status never flipped), wipe the
            # stale chunks so the retry doesn't duplicate them.
            cleared = _clear_existing_chunks_for_source(
                db, admin_id=admin_id, source_pk=source_pk,
            )
            if cleared:
                db.commit()
                logger.info(
                    "%s cleared %d stale chunks from prior partial run",
                    log_pfx, cleared,
                )

            # ---------- 3. Download bytes ----------
            bucket = _resolve_bucket_name()
            if not bucket:
                raise IngestionConfigError(
                    "KNOWLEDGE_S3_BUCKET unset; cannot download source"
                )
            if not source.s3_key:
                # Step 7 must ALWAYS set s3_key on the row before
                # enqueueing — paste-text uploads land in S3 too.
                # A NULL s3_key here is a contract violation.
                raise IngestionConfigError(
                    "source.s3_key is NULL; Step 7's API contract "
                    "requires every source to have an S3 object before "
                    "enqueue"
                )
            file_bytes = _download_bytes(source.s3_key, bucket)

            # ---------- 4. Parse ----------
            source_type = source.source_type
            try:
                # Validate the declared source_type the API stored;
                # if it doesn't match a registered parser, treat as
                # permanent failure. ``detect_source_type`` is the
                # filename-suffix path used only when the API didn't
                # declare; here we trust the row.
                parser = get_parser(source_type)
            except UnsupportedSourceType as exc:
                raise IngestionPermanentError(
                    f"unsupported source_type"
                ) from exc

            filename_hint = (
                source.filename if source.filename
                else f"source_{source_pk}.{source_type}"
            )
            try:
                parsed = parser.parse(file_bytes, filename_hint)
            except ParserError as exc:
                raise IngestionPermanentError(
                    f"parser raised {type(exc).__name__}"
                ) from exc

            text = parsed.text
            if not text or not text.strip():
                raise IngestionPermanentError(
                    "parser produced empty text"
                )

            # ---------- 5. Chunk ----------
            tenant = (
                db.query(Admin).filter(Admin.id == admin_id).one_or_none()
            )
            if tenant is None:
                # The admin row was hard-deleted between enqueue
                # and worker. Permanent — no point retrying.
                raise IngestionPermanentError(
                    "admin row missing"
                )
            instance = db.get(Instance, instance_id) if instance_id else None
            cfg = resolve_effective_config(tenant=tenant, instance=instance)
            chunks = chunk_text(text, cfg)
            if not chunks:
                raise IngestionPermanentError(
                    "chunker produced no chunks"
                )

            # ---------- 6. Embed ----------
            try:
                embeddings = embed_texts(chunks)
            except _SAOperationalError:
                # SA OperationalError on the embed call is unusual
                # (embedder is HTTP not DB) but if it happens, treat
                # as transient and retry.
                raise
            except Exception as exc:
                # Embedder HTTP failures are typically transient (429,
                # 503, network). Lift into TransientIngestionError so
                # the autoretry tuple catches them. If a permanent
                # embedder failure surfaces later (e.g., 4xx for an
                # un-embeddable text), we can extend with a sentinel
                # subclass.
                raise TransientIngestionError(
                    f"embedder failure: {type(exc).__name__}"
                ) from exc

            if len(embeddings) != len(chunks):
                raise IngestionPermanentError(
                    "embedder returned mismatched vector count"
                )

            # ---------- 7. Persist chunks ----------
            chunk_repo.add_chunks(
                chunks=chunks,
                embeddings=embeddings,
                admin_id=admin_id,
                domain_id=None,
                luciel_instance_id=instance_id,
                knowledge_type="luciel_knowledge",
                title=source.filename,
                # Post-Cleanup-B: ``source_id`` is the INTEGER FK
                # (NOT NULL). Legacy stringy ``source_id`` and
                # free-text ``source`` columns are gone.
                source_id=source_pk,
                source_version=source.source_version or 1,
                source_filename=source.filename,
                source_type=source_type,
                ingested_by=source.ingested_by,
                created_by=source.ingested_by,
                autocommit=False,
            )

            # ---------- 8. Mark ready ----------
            source_repo.mark_status(
                source_pk,
                admin_id=admin_id,
                status="ready",
            )
            db.commit()

            duration_ms = int((time.monotonic() - started_at) * 1000)
            logger.info(
                "%s SUCCESS chunk_count=%d byte_count=%d duration_ms=%d",
                log_pfx, len(chunks), len(file_bytes), duration_ms,
            )

        except _TRANSIENT_EXC as exc:
            # Autoretry will retry up to max_retries. We DO mark the
            # row as 'failed' on each retry attempt's persistence to
            # surface progress, but a subsequent retry that succeeds
            # will flip it back to 'ready'. mark_status normalises
            # ingestion_error to NULL on non-failed status, so a
            # successful retry clears the prior error.
            db.rollback()
            logger.warning(
                "%s TRANSIENT_FAIL exc_class=%s — autoretry will fire",
                log_pfx, type(exc).__name__,
            )
            _mark_failed_swallow(
                source_repo, source_pk, admin_id, exc,
            )
            raise  # autoretry_for catches.

        except IngestionPermanentError as exc:
            db.rollback()
            logger.warning(
                "%s PERMANENT_FAIL exc_class=%s",
                log_pfx, type(exc).__name__,
            )
            _mark_failed_swallow(
                source_repo, source_pk, admin_id, exc,
            )
            # Re-raise so the broker observes the failure (lands in
            # DLQ via the normal failure path; autoretry_for does NOT
            # match this class).
            raise

        except Exception as exc:  # noqa: BLE001
            # Catch-all for anything we didn't classify above.
            # Treat as permanent — better to surface and investigate
            # than retry-loop on an unknown failure mode.
            db.rollback()
            logger.error(
                "%s UNCLASSIFIED_FAIL exc_class=%s",
                log_pfx, type(exc).__name__,
            )
            _mark_failed_swallow(
                source_repo, source_pk, admin_id, exc,
            )
            raise

        finally:
            db.close()


def _mark_failed_swallow(
    source_repo: KnowledgeSourceRepository,
    source_pk: int,
    admin_id: str,
    exc: BaseException,
) -> None:
    """Helper: flip the source to 'failed' on the original session's
    rollback. Swallows secondary failures so the outer exception
    keeps propagating. Mirrors ``IngestionService._mark_source_failed``
    from Step 3."""
    try:
        # Use a fresh session for the audit write — the outer session
        # is mid-rollback. Same scope binding still applies (we're
        # inside the bind_tenant_scope with-block).
        audit_db = SessionLocal()
        try:
            audit_repo = KnowledgeSourceRepository(audit_db)
            audit_repo.mark_status(
                source_pk,
                admin_id=admin_id,
                status="failed",
                error=_sanitise_error(exc),
                autocommit=True,
            )
        finally:
            audit_db.close()
    except Exception:  # noqa: BLE001 — secondary failure: log and move on
        logger.exception(
            "embed_source[source_pk=%d] FAILED to write 'failed' "
            "status row; primary exception will still propagate",
            source_pk,
        )


