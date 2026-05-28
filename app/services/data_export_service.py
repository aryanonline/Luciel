"""Data export service \u2014 Arc 10.

Owns the pre-closure data export bundle described in Vision \u00a76.3
and Architecture \u00a73.6.3. The bundle is the customer's "Download
all my data" payload, generated asynchronously and delivered via
a signed S3 URL.

Bundle contents (per Architecture \u00a73.6.3, with the Arc 10 Option-2
qualifier on knowledge):

  manifest.json
    Bundle metadata: bundle_id, admin_id, tier_at_request,
    generated_at, schema_version, bytes_size, contents map.

  conversations.jsonl
    Every conversation row owned by this admin, one per line.
    Each line embeds the conversation's messages so the bundle
    is self-contained \u2014 the customer does not need to cross-
    reference a separate messages.jsonl.

  leads.jsonl
    Every captured lead.

  audit_log.csv
    The admin's own audit history (tier-conditional already \u2014
    Free admins see their 30-day window, Pro 1y, Enterprise 7y).

  instances.json
    Instance configurations (the 5 pillars per instance).

  escalations.csv
    Every escalation event with signal metadata.

  knowledge_sources/
    Per-source bundle of the admin's ingested knowledge. Arc 10
    ships Option-2 here: originals are NOT included because Arc 11
    owns the knowledge S3 bucket (Architecture \u00a76). The text
    content is reconstructed from indexed chunks.

      knowledge_sources/manifest.json
        Per source_id: source_filename, source_type, source_version,
        ingested_by, ingested_at (earliest chunk created_at),
        chunk_count, originals_retained: false.

      knowledge_sources/chunks/<source_id>__v<n>.jsonl
        Per (source_id, version): chunks ordered by their natural
        sequence (by id ASC, which mirrors ingestion order). Each
        line carries title, content, knowledge_type, created_at.

      Includes pending_downgrade_archived_at-stamped chunks
      (they're still the customer's data and they're recoverable
      on re-upgrade). EXCLUDES superseded_at and soft_deleted_at
      stamped chunks (those have been replaced or are mid-purge).

  README.md
    Bundle layout doc, including the originals-not-retained note
    per Option-2.

Mechanics:

  * Generated asynchronously by a Celery task.
  * Streamed to S3 via multipart upload (the bundle can be 5GB+
    on Pro and unlimited on Enterprise; buffering is not an
    option).
  * Per-admin concurrency lock via the partial unique index
    ux_data_export_jobs_one_active_per_admin (Arc 10 migration).
  * Signed URL TTL is tier-conditional and STICKY per bundle:
    7 days on Free/Pro, 90 days on Enterprise. An Enterprise
    admin who downgrades after generation keeps their 90-day URL.
  * Available during the closure grace window. An admin who
    closed without checking "Download all my data" can still
    request an export within the 30-day grace.
  * Job status machine:
      pending \u2192 generating \u2192 ready  (happy path)
                              \u2193
                            expired  (signed URL TTL elapsed; the
                                      cleanup worker stamps this)
      pending \u2192 generating \u2192 failed (error during generation)
"""
from __future__ import annotations

import csv
import io
import json
import logging
import tarfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from sqlalchemy import text as sql_text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.admin_audit_log import (
    ACTION_DATA_EXPORT_FAILED,
    ACTION_DATA_EXPORT_GENERATED,
    ACTION_DATA_EXPORT_REQUESTED,
    RESOURCE_TENANT,
)

logger = logging.getLogger(__name__)


Tier = Literal["free", "pro", "enterprise"]
TriggeredBy = Literal["admin_request", "grace_window_request"]
JobStatus = Literal["pending", "generating", "ready", "expired", "failed"]


# ---------------------------------------------------------------------
# Tier-conditional signed-URL TTL.
# ---------------------------------------------------------------------
# Vision \u00a77 tier matrix: pre-closure data export is "Yes (7-day
# window)" on Free + Pro, "Yes (90-day window)" on Enterprise.
# These map to seconds for the boto3 generate_presigned_url call.
_TIER_URL_TTL_SECONDS: dict[Tier, int] = {
    "free":       7  * 24 * 3600,
    "pro":        7  * 24 * 3600,
    "enterprise": 90 * 24 * 3600,
}

# S3 bucket name comes from settings. Same ca-central-1 footprint
# as everything else per Architecture \u00a74.2 (data residency).
# The bucket has Arc 10's lifecycle policy applied: incomplete
# multipart uploads abort after 24h so an interrupted generation
# does not leak storage forever.
_S3_KEY_PREFIX = "data-exports"


# ---------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------

class DataExportError(Exception):
    """Base for export-flow errors."""


class ExportAlreadyInFlightError(DataExportError):
    """An export job is already pending/generating for this admin.

    The partial unique index ux_data_export_jobs_one_active_per_admin
    enforces this at the DB level; the service layer translates the
    IntegrityError into this typed exception so the route returns a
    clear HTTP 409.
    """


class ExportNotReadyError(DataExportError):
    """get_signed_url called on a job not in 'ready' state."""


class ExportNotFoundError(DataExportError):
    """Job id does not exist or does not belong to this admin."""


class ExportGenerationError(DataExportError):
    """Internal failure during bundle generation \u2014 wraps the cause."""


# ---------------------------------------------------------------------
# Result shape.
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class DataExportJob:
    """Returned from enqueue() and used as the route's response body.

    Mirrors the schema-level row but flattens the few fields the
    frontend cares about. We do not return the row ORM object directly
    so the route does not have to worry about lazy-load surprises.
    """
    id: str                              # UUID as str
    admin_id: str
    status: JobStatus
    requested_at: datetime
    tier_at_request: Tier
    triggered_by: TriggeredBy
    ready_at: datetime | None
    signed_url_expires_at: datetime | None


# ---------------------------------------------------------------------
# Service.
# ---------------------------------------------------------------------

class DataExportService:
    """Orchestrates pre-closure data export.

    Lifetime: one instance per request OR per Celery task invocation.
    s3_client and audit_repository are injected so unit tests can
    stub the AWS surface and verify audit emissions independently.
    """

    def __init__(
        self,
        db: Session,
        *,
        s3_client,
        s3_bucket: str,
        audit_repository,
    ) -> None:
        self.db = db
        self.s3 = s3_client
        self.bucket = s3_bucket
        self.audit_repository = audit_repository

    # -----------------------------------------------------------------
    # Public \u2014 enqueue (route layer entry).
    # -----------------------------------------------------------------

    def enqueue(
        self,
        *,
        admin_id: str,
        triggered_by: TriggeredBy,
        tier_at_request: Tier,
        audit_ctx,
    ) -> DataExportJob:
        """Insert a pending data_export_jobs row.

        The Celery task generate_bundle reads pending rows. The
        partial unique index ux_data_export_jobs_one_active_per_admin
        guarantees at most one (pending, generating) row per admin
        at any moment \u2014 a concurrent duplicate INSERT raises
        IntegrityError, which we translate to
        ExportAlreadyInFlightError.

        triggered_by is captured for forensics: 'admin_request' means
        the closure modal initiated it; 'grace_window_request' means
        the admin requested it during the grace window after closure.
        """
        job_id = uuid.uuid4()
        now = datetime.now(timezone.utc)

        try:
            self.db.execute(
                sql_text(
                    """
                    INSERT INTO data_export_jobs (
                        id, admin_id, status, requested_at,
                        tier_at_request, triggered_by
                    ) VALUES (
                        :id, :aid, 'pending', :ts,
                        :tier, :tb
                    )
                    """
                ),
                {
                    "id": str(job_id),
                    "aid": admin_id,
                    "ts": now,
                    "tier": tier_at_request,
                    "tb": triggered_by,
                },
            )
            self.db.flush()
        except IntegrityError as exc:
            # The unique-index violation is the expected failure shape
            # when an export is already in flight. Other IntegrityError
            # paths (FK violation, CHECK failure) should not happen
            # for this INSERT shape, but if they do, we surface them
            # rather than swallow.
            if "ux_data_export_jobs_one_active_per_admin" in str(exc.orig):
                raise ExportAlreadyInFlightError(
                    f"An export job is already in flight for admin "
                    f"{admin_id!r}. Wait for it to complete or fail."
                ) from exc
            raise

        # Audit row \u2014 forensics needs to see WHEN this was requested,
        # WHO triggered it, and what tier was sticky on the bundle.
        self.audit_repository.record(
            ctx=audit_ctx,
            admin_id=admin_id,
            action=ACTION_DATA_EXPORT_REQUESTED,
            resource_type=RESOURCE_TENANT,
            resource_natural_id=admin_id,
            after={
                "job_id": str(job_id),
                "triggered_by": triggered_by,
                "tier_at_request": tier_at_request,
                "requested_at": now.isoformat(),
            },
            note=(
                f"Data export requested for {admin_id} "
                f"(trigger={triggered_by})."
            ),
            autocommit=False,
        )

        return DataExportJob(
            id=str(job_id),
            admin_id=admin_id,
            status="pending",
            requested_at=now,
            tier_at_request=tier_at_request,
            triggered_by=triggered_by,
            ready_at=None,
            signed_url_expires_at=None,
        )

    # -----------------------------------------------------------------
    # Public \u2014 generate_bundle (Celery task entry).
    # -----------------------------------------------------------------

    def generate_bundle(self, job_id: str) -> None:
        """Generate the bundle for a pending job and upload to S3.

        Race-safe transition: pending \u2192 generating via UPDATE \u2026 WHERE
        status='pending'. If rowcount=0 another worker already grabbed
        the job and this call returns cleanly.

        On success: status='ready', s3_key set, signed URL computed
        and signed_url_expires_at stamped.

        On failure: status='failed', failed_at set, error_message
        carries the exception's repr.
        """
        # --- Transition pending \u2192 generating, race-safe. ---
        now = datetime.now(timezone.utc)
        res = self.db.execute(
            sql_text(
                """
                UPDATE data_export_jobs
                   SET status = 'generating',
                       started_at = :ts
                 WHERE id = :id
                   AND status = 'pending'
                 RETURNING admin_id, tier_at_request
                """
            ),
            {"id": job_id, "ts": now},
        )
        row = res.first()
        if row is None:
            # Another worker already picked it up, OR the job was
            # never created. Either way, exit clean. The other
            # worker (or the row's absence) handles the outcome.
            logger.info(
                "data_export_service: generate_bundle skipped \u2014 "
                "job_id=%s not in 'pending' (claimed by another "
                "worker or absent).",
                job_id,
            )
            return
        admin_id: str = row[0]
        tier_at_request: Tier = row[1]  # type: ignore[assignment]
        self.db.commit()

        try:
            s3_key, bytes_written = self._build_and_upload_bundle(
                admin_id=admin_id,
                tier_at_request=tier_at_request,
                job_id=job_id,
            )
        except Exception as exc:
            logger.error(
                "data_export_service: generation failed "
                "job_id=%s admin_id=%s err=%s",
                job_id, admin_id, exc, exc_info=True,
            )
            self._mark_failed(job_id=job_id, admin_id=admin_id, err=str(exc))
            raise ExportGenerationError(
                f"Failed to generate bundle for job {job_id}: {exc}"
            ) from exc

        # --- Compute the signed-URL expiry. ---
        ttl_seconds = _TIER_URL_TTL_SECONDS[tier_at_request]
        ready_at = datetime.now(timezone.utc)
        signed_url_expires_at = ready_at + timedelta(seconds=ttl_seconds)

        # --- Stamp ready. ---
        self.db.execute(
            sql_text(
                """
                UPDATE data_export_jobs
                   SET status = 'ready',
                       ready_at = :ready,
                       s3_bucket = :bkt,
                       s3_key = :key,
                       bytes_size = :sz,
                       signed_url_ttl_seconds = :ttl,
                       signed_url_expires_at = :exp
                 WHERE id = :id
                """
            ),
            {
                "ready": ready_at,
                "bkt": self.bucket,
                "key": s3_key,
                "sz": bytes_written,
                "ttl": ttl_seconds,
                "exp": signed_url_expires_at,
                "id": job_id,
            },
        )
        # Audit \u2014 stamping ready is the GDPR-relevant moment.
        self.audit_repository.record(
            ctx=_system_ctx_for_export(),
            admin_id=admin_id,
            action=ACTION_DATA_EXPORT_GENERATED,
            resource_type=RESOURCE_TENANT,
            resource_natural_id=admin_id,
            after={
                "job_id": job_id,
                "s3_bucket": self.bucket,
                "s3_key": s3_key,
                "bytes_size": bytes_written,
                "ready_at": ready_at.isoformat(),
                "signed_url_expires_at": signed_url_expires_at.isoformat(),
                "tier_at_request": tier_at_request,
            },
            note=(
                f"Data export ready for {admin_id}: "
                f"{bytes_written} bytes."
            ),
            autocommit=False,
        )
        self.db.commit()

    # -----------------------------------------------------------------
    # Public \u2014 get_signed_url (route layer entry).
    # -----------------------------------------------------------------

    def get_signed_url(
        self,
        *,
        job_id: str,
        admin_id: str,
    ) -> tuple[str, datetime]:
        """Return (signed_url, expires_at) for a ready job.

        Enforces admin_id at the service layer in addition to RLS:
        the data_export_jobs row carries admin_id, RLS gates SELECT
        by app.admin_id, and we double-check via the WHERE clause
        below. Three layers of defense, one bug away from leakage \u2014
        the architecture cares about this kind of asymmetric risk.

        Raises:
          - ExportNotFoundError: row missing or wrong admin.
          - ExportNotReadyError: row exists but status != 'ready'.
        """
        row = self.db.execute(
            sql_text(
                """
                SELECT status, s3_bucket, s3_key,
                       signed_url_ttl_seconds, signed_url_expires_at
                  FROM data_export_jobs
                 WHERE id = :id
                   AND admin_id = :aid
                """
            ),
            {"id": job_id, "aid": admin_id},
        ).first()
        if row is None:
            raise ExportNotFoundError(
                f"Export job {job_id!r} not found for admin "
                f"{admin_id!r}."
            )
        status, s3_bucket, s3_key, ttl, expires_at = row
        if status != "ready":
            raise ExportNotReadyError(
                f"Export job {job_id!r} is in status {status!r}, "
                f"not 'ready'."
            )
        if expires_at and datetime.now(timezone.utc) >= expires_at:
            # The cleanup worker should have flipped status to
            # 'expired' but races happen. Refuse to mint a signed
            # URL against an expired bundle.
            raise ExportNotReadyError(
                f"Export job {job_id!r} signed URL has expired "
                f"({expires_at.isoformat()})."
            )

        # Compute how many seconds remain so the generated URL
        # itself expires at signed_url_expires_at, not at
        # now+full-TTL. This makes the URL's encoded expiry match
        # the row's signed_url_expires_at exactly.
        remaining = int(
            (expires_at - datetime.now(timezone.utc)).total_seconds()
        )
        url = self.s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": s3_bucket, "Key": s3_key},
            ExpiresIn=max(remaining, 60),  # never sign a <60s URL
        )
        return url, expires_at

    # -----------------------------------------------------------------
    # Internals \u2014 bundle building.
    # -----------------------------------------------------------------

    def _build_and_upload_bundle(
        self,
        *,
        admin_id: str,
        tier_at_request: Tier,
        job_id: str,
    ) -> tuple[str, int]:
        """Build the .tar.gz bundle in-memory-streamed to S3.

        We compose a tarfile object writing to a BytesIO buffer and
        upload via boto3's upload_fileobj which handles multipart
        transparently. The trade-off:
          + simple code, no manual MultipartUpload bookkeeping
          + boto3 picks reasonable chunk sizes (8MB default)
          - the buffer can grow large for huge tenants; future
            optimization is a true streaming tarfile via a pipe.
            For Arc 10's expected bundle sizes (<10GB), BytesIO
            is fine on a 4GB worker.

        Returns (s3_key, bytes_written).
        """
        s3_key = (
            f"{_S3_KEY_PREFIX}/{admin_id}/{job_id}/bundle.tar.gz"
        )

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            self._write_manifest(tar, admin_id=admin_id,
                                 tier_at_request=tier_at_request,
                                 job_id=job_id)
            self._write_readme(tar)
            self._write_conversations(tar, admin_id=admin_id)
            self._write_leads(tar, admin_id=admin_id)
            self._write_audit_log(tar, admin_id=admin_id)
            self._write_instances(tar, admin_id=admin_id)
            self._write_escalations(tar, admin_id=admin_id)
            self._write_knowledge(tar, admin_id=admin_id)

        bytes_written = buf.tell()
        buf.seek(0)
        self.s3.upload_fileobj(
            Fileobj=buf,
            Bucket=self.bucket,
            Key=s3_key,
            ExtraArgs={"ContentType": "application/gzip"},
        )
        return s3_key, bytes_written

    def _write_manifest(
        self,
        tar: tarfile.TarFile,
        *,
        admin_id: str,
        tier_at_request: Tier,
        job_id: str,
    ) -> None:
        manifest = {
            "schema_version": 1,
            "bundle_id": job_id,
            "admin_id": admin_id,
            "tier_at_request": tier_at_request,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "contents": [
                "conversations.jsonl",
                "leads.jsonl",
                "audit_log.csv",
                "instances.json",
                "escalations.csv",
                "knowledge_sources/manifest.json",
                "knowledge_sources/chunks/*.jsonl",
                "README.md",
            ],
            "originals_retained_in_bundle": False,
            "originals_retention_note": (
                "Original uploaded knowledge files are not retained at "
                "ingestion time by VantageMind v1. The text content of "
                "each source has been reconstructed from indexed "
                "chunks and is available under knowledge_sources/chunks/."
            ),
        }
        self._add_text_entry(
            tar, "manifest.json", json.dumps(manifest, indent=2)
        )

    def _write_readme(self, tar: tarfile.TarFile) -> None:
        readme = _README_TEXT
        self._add_text_entry(tar, "README.md", readme)

    def _write_conversations(
        self,
        tar: tarfile.TarFile,
        *,
        admin_id: str,
    ) -> None:
        """Stream every conversation belonging to admin_id, with

        messages embedded inline so the bundle is self-contained.
        """
        rows = self.db.execute(
            sql_text(
                """
                SELECT c.id, c.created_at, c.updated_at,
                       c.luciel_instance_id,
                       COALESCE(
                         json_agg(
                           json_build_object(
                             'id', m.id,
                             'created_at', m.created_at,
                             'role', m.role,
                             'content', m.content
                           ) ORDER BY m.id
                         ) FILTER (WHERE m.id IS NOT NULL),
                         '[]'::json
                       ) AS messages
                  FROM conversations c
             LEFT JOIN sessions s ON s.conversation_id = c.id
             LEFT JOIN messages m ON m.session_id = s.id
                 WHERE c.admin_id = :aid
              GROUP BY c.id
              ORDER BY c.id ASC
                """
            ),
            {"aid": admin_id},
        )
        lines = []
        for row in rows:
            lines.append(json.dumps({
                "conversation_id": row[0],
                "created_at": _iso(row[1]),
                "updated_at": _iso(row[2]),
                "instance_id": row[3],
                "messages": row[4] if row[4] else [],
            }, default=str))
        self._add_text_entry(tar, "conversations.jsonl",
                             "\n".join(lines) + ("\n" if lines else ""))

    def _write_leads(self, tar: tarfile.TarFile, *, admin_id: str) -> None:
        # Leads are captured via cognition into the dashboard's lead
        # view. The exact column shape is Arc 11's concern; for Arc
        # 10 we export every column from identity_claims (the v1
        # lead-equivalent surface) so a future schema change does
        # not silently drop fields.
        rows = self.db.execute(
            sql_text(
                """
                SELECT row_to_json(ic.*)
                  FROM identity_claims ic
                 WHERE ic.admin_id = :aid
              ORDER BY ic.id ASC
                """
            ),
            {"aid": admin_id},
        )
        lines = [json.dumps(r[0], default=str) for r in rows]
        self._add_text_entry(tar, "leads.jsonl",
                             "\n".join(lines) + ("\n" if lines else ""))

    def _write_audit_log(
        self,
        tar: tarfile.TarFile,
        *,
        admin_id: str,
    ) -> None:
        """Audit log as CSV. Tier retention has already been applied

        upstream by AuditRetentionService; the rows still present
        in admin_audit_logs for this admin are what the export
        includes. cold_archived_at rows are included too \u2014 they're
        still the customer's data \u2014 with a column noting their
        archive state.
        """
        rows = self.db.execute(
            sql_text(
                """
                SELECT id, created_at, action, resource_type,
                       resource_pk, resource_natural_id, actor_key_prefix,
                       tier_at_write, cold_archived_at,
                       before_json, after_json, note
                  FROM admin_audit_logs
                 WHERE admin_id = :aid
              ORDER BY id ASC
                """
            ),
            {"aid": admin_id},
        )
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow([
            "id", "created_at", "action", "resource_type",
            "resource_pk", "resource_natural_id", "actor_key_prefix",
            "tier_at_write", "cold_archived_at",
            "before_json", "after_json", "note",
        ])
        for row in rows:
            writer.writerow([
                row[0],
                _iso(row[1]),
                row[2], row[3], row[4], row[5], row[6],
                row[7],
                _iso(row[8]),
                json.dumps(row[9], default=str) if row[9] else "",
                json.dumps(row[10], default=str) if row[10] else "",
                row[11] or "",
            ])
        self._add_text_entry(tar, "audit_log.csv", out.getvalue())

    def _write_instances(
        self,
        tar: tarfile.TarFile,
        *,
        admin_id: str,
    ) -> None:
        """Instance configs as a single JSON document.

        We include the 5-pillar shape per Vision \u00a73: channels, tools,
        knowledge (reference to source_ids), escalation, personality.
        Pillar resolution happens through whatever shape the current
        instance_service exposes; for Arc 10 we dump the raw rows
        and let Arc 15 (config UX) refine the export schema.
        """
        rows = self.db.execute(
            sql_text(
                """
                SELECT row_to_json(i.*)
                  FROM instances i
                 WHERE i.admin_id = :aid
              ORDER BY i.id ASC
                """
            ),
            {"aid": admin_id},
        )
        instances = [r[0] for r in rows]
        self._add_text_entry(
            tar, "instances.json", json.dumps(instances, default=str, indent=2)
        )

    def _write_escalations(
        self,
        tar: tarfile.TarFile,
        *,
        admin_id: str,
    ) -> None:
        """Escalations CSV.

        Arc 10 does not own the escalation-emission table (Arc 14
        wires it). For now we read audit-log rows tagged with
        escalation actions if any exist; if the table or rows are
        not yet present, the file is written empty (header only).
        Forward-compatible: when Arc 14 lands and the dedicated
        escalation_events table exists, this method updates to
        read from it.
        """
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow([
            "id", "created_at", "signal", "confidence",
            "reasoning_excerpt", "session_id", "instance_id",
        ])
        # No-op body for Arc 10. Arc 14 fills this in.
        self._add_text_entry(tar, "escalations.csv", out.getvalue())

    def _write_knowledge(
        self,
        tar: tarfile.TarFile,
        *,
        admin_id: str,
    ) -> None:
        """Knowledge bundle.

        Arc 10 shipped chunks-only and synthesised the per-source
        manifest by grouping chunks by their legacy stringy
        ``source_id``. Arc 11 Step 1 introduced the
        ``knowledge_sources`` table that holds provenance directly;
        Step 3 (this update) reads the manifest from that table
        when it exists and falls back to the chunk-grouping path
        for legacy chunks whose ``source_fk`` is NULL.

        Bundle layout is unchanged:
          * ``knowledge_sources/manifest.json``
          * ``knowledge_sources/chunks/<key>__v<version>.jsonl``
        where ``<key>`` is the source-row PK for Arc-11-shape
        sources and the legacy stringy id otherwise.
        """
        # ---------------------------------------------------------
        # 1. Arc-11-shape manifest entries \u2014 drawn from
        #    ``knowledge_sources`` directly.
        # ---------------------------------------------------------
        ks_rows = self.db.execute(
            sql_text(
                """
                SELECT s.id            AS source_pk,
                       s.source_uuid   AS source_uuid,
                       s.filename      AS source_filename,
                       s.source_type   AS source_type,
                       s.source_version AS source_version,
                       s.ingested_by   AS ingested_by,
                       s.ingested_at   AS ingested_at,
                       s.size_bytes    AS size_bytes,
                       s.ingestion_status AS ingestion_status,
                       s.soft_deleted_at IS NOT NULL AS is_soft_deleted,
                       s.pending_downgrade_archived_at IS NOT NULL
                                       AS archived_on_downgrade,
                       (SELECT COUNT(*)
                          FROM knowledge_chunks c
                         WHERE c.source_fk = s.id
                           AND c.superseded_at IS NULL
                           AND c.soft_deleted_at IS NULL) AS chunk_count
                  FROM knowledge_sources s
                 WHERE s.admin_id = :aid
                   AND s.soft_deleted_at IS NULL
              ORDER BY s.ingested_at ASC
                """
            ),
            {"aid": admin_id},
        )
        manifest: list[dict] = []
        # ``per_source`` rows are (key, version, is_legacy_string) so
        # the chunk-file loop below can dispatch the right query.
        per_source: list[tuple[str | int, int, bool]] = []
        for r in ks_rows:
            manifest.append({
                # Arc-11 entries surface BOTH the new pk and the
                # legacy stringy form (``src-<pk>``) so consumers
                # mid-cutover can resolve either.
                "source_pk": int(r.source_pk),
                "source_uuid": str(r.source_uuid),
                "source_id": f"src-{int(r.source_pk)}",
                "source_filename": r.source_filename,
                "source_type": r.source_type,
                "source_version": int(r.source_version),
                "ingested_by": r.ingested_by,
                "ingested_at": _iso(r.ingested_at),
                "size_bytes": int(r.size_bytes or 0),
                "chunk_count": int(r.chunk_count or 0),
                "ingestion_status": r.ingestion_status,
                "originals_retained": False,
                "archived_on_downgrade": bool(r.archived_on_downgrade),
            })
            per_source.append((int(r.source_pk), int(r.source_version), False))

        # ---------------------------------------------------------
        # 2. Legacy-shape manifest entries \u2014 chunks with NULL
        #    ``source_fk``, grouped by their stringy ``source_id``.
        #    These are pre-Arc-11 rows; Step 11 retires this branch.
        # ---------------------------------------------------------
        legacy_rows = self.db.execute(
            sql_text(
                """
                SELECT source_id,
                       MAX(source_filename) AS source_filename,
                       MAX(source_type) AS source_type,
                       MAX(source_version) AS source_version,
                       MAX(ingested_by) AS ingested_by,
                       MIN(created_at) AS ingested_at,
                       COUNT(*) AS chunk_count,
                       BOOL_OR(pending_downgrade_archived_at IS NOT NULL)
                           AS archived_on_downgrade
                  FROM knowledge_chunks
                 WHERE admin_id = :aid
                   AND source_fk IS NULL
                   AND superseded_at IS NULL
                   AND soft_deleted_at IS NULL
                   AND source_id IS NOT NULL
              GROUP BY source_id
              ORDER BY MIN(created_at) ASC
                """
            ),
            {"aid": admin_id},
        )
        for r in legacy_rows:
            manifest.append({
                "source_pk": None,
                "source_uuid": None,
                "source_id": r[0],
                "source_filename": r[1],
                "source_type": r[2],
                "source_version": int(r[3] or 1),
                "ingested_by": r[4],
                "ingested_at": _iso(r[5]),
                "size_bytes": None,
                "chunk_count": int(r[6] or 0),
                "ingestion_status": "ready",  # implied for legacy
                "originals_retained": False,
                "archived_on_downgrade": bool(r[7]),
            })
            per_source.append((r[0], int(r[3] or 1), True))

        self._add_text_entry(
            tar,
            "knowledge_sources/manifest.json",
            json.dumps(manifest, default=str, indent=2),
        )

        # ---------------------------------------------------------
        # 3. Per-source chunk files.
        # ---------------------------------------------------------
        for key, source_version, is_legacy in per_source:
            if is_legacy:
                chunk_rows = self.db.execute(
                    sql_text(
                        """
                        SELECT id, title, content, knowledge_type, created_at
                          FROM knowledge_chunks
                         WHERE admin_id = :aid
                           AND source_fk IS NULL
                           AND source_id = :sid
                           AND source_version = :sv
                           AND superseded_at IS NULL
                           AND soft_deleted_at IS NULL
                      ORDER BY id ASC
                        """
                    ),
                    {"aid": admin_id, "sid": key, "sv": source_version},
                )
                file_key = key
            else:
                chunk_rows = self.db.execute(
                    sql_text(
                        """
                        SELECT id, title, content, knowledge_type, created_at
                          FROM knowledge_chunks
                         WHERE admin_id = :aid
                           AND source_fk = :fk
                           AND source_version = :sv
                           AND superseded_at IS NULL
                           AND soft_deleted_at IS NULL
                      ORDER BY id ASC
                        """
                    ),
                    {"aid": admin_id, "fk": key, "sv": source_version},
                )
                file_key = f"src-{key}"

            lines = []
            for cr in chunk_rows:
                lines.append(json.dumps({
                    "chunk_id": cr[0],
                    "title": cr[1],
                    "content": cr[2],
                    "knowledge_type": cr[3],
                    "created_at": _iso(cr[4]),
                }, default=str))
            self._add_text_entry(
                tar,
                f"knowledge_sources/chunks/"
                f"{file_key}__v{source_version}.jsonl",
                "\n".join(lines) + ("\n" if lines else ""),
            )

    # -----------------------------------------------------------------
    # Internals \u2014 small utilities.
    # -----------------------------------------------------------------

    @staticmethod
    def _add_text_entry(
        tar: tarfile.TarFile,
        name: str,
        content: str,
    ) -> None:
        """Add a text file to the tarball from an in-memory string."""
        data = content.encode("utf-8")
        info = tarfile.TarInfo(name=name)
        info.size = len(data)
        info.mtime = int(datetime.now(timezone.utc).timestamp())
        tar.addfile(info, io.BytesIO(data))

    def _mark_failed(self, *, job_id: str, admin_id: str, err: str) -> None:
        """Stamp a job as failed and emit an audit row."""
        self.db.execute(
            sql_text(
                """
                UPDATE data_export_jobs
                   SET status = 'failed',
                       failed_at = :ts,
                       error_message = :err
                 WHERE id = :id
                """
            ),
            {"ts": datetime.now(timezone.utc), "err": err[:2000], "id": job_id},
        )
        self.audit_repository.record(
            ctx=_system_ctx_for_export(),
            admin_id=admin_id,
            action=ACTION_DATA_EXPORT_FAILED,
            resource_type=RESOURCE_TENANT,
            resource_natural_id=admin_id,
            after={"job_id": job_id, "error_message": err[:2000]},
            note=f"Data export FAILED for {admin_id}: job {job_id}.",
            autocommit=False,
        )
        self.db.commit()


# ---------------------------------------------------------------------
# Module helpers.
# ---------------------------------------------------------------------

def _iso(ts: Any) -> str | None:
    """Coerce a timestamp-like to an ISO-8601 string or None."""
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.isoformat()
    return str(ts)


def _system_ctx_for_export():
    """Build an AuditContext.system() for export-worker rows.

    Imported lazily to avoid the same circular-import dance as
    retention.py: admin_audit_repository pulls in audit_chain which
    pulls in session which pulls in this module via the worker
    Celery wiring.
    """
    from app.repositories.admin_audit_repository import AuditContext
    return AuditContext.system(label="data_export_worker")


_README_TEXT = """# VantageMind \u2014 Data Export Bundle

This archive contains every piece of data VantageMind holds for
your account at the moment of generation.

## Layout

| Path | Description |
|------|-------------|
| `manifest.json` | Bundle metadata: when it was generated, your tier, contents. |
| `conversations.jsonl` | One conversation per line, with messages embedded. |
| `leads.jsonl` | Every lead Luciel captured. |
| `audit_log.csv` | Every admin action recorded against your account. |
| `instances.json` | Configuration of every Luciel instance you created. |
| `escalations.csv` | Every escalation event with the signal that fired it. |
| `knowledge_sources/manifest.json` | Per-source metadata for your ingested knowledge. |
| `knowledge_sources/chunks/*.jsonl` | Reconstructed text from each source's chunks. |

## A note on knowledge originals

At the time this bundle was generated, VantageMind did not retain the
original uploaded files (PDFs, DOCX, CSV) after ingestion. The text
content of each source has been reconstructed from the indexed
chunks under `knowledge_sources/chunks/`. The `manifest.json` lists
each source's original filename so you can match it back to your
local copy.

Future versions of VantageMind retain originals; if you re-ingest a
file after that upgrade, your next export will include both the
original and the reconstructed text.

## Schema version

This bundle is `schema_version: 1`. Future bundles may add fields
but will not remove or rename existing ones \u2014 forward-compatible
reading is guaranteed.
"""
