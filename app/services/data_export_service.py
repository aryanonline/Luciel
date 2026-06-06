"""Data export service — Arc 10, RESCAN TIER-DE(widget+export).

Owns the data export bundle described in Vision §6.3, Architecture §5.10,
and Architecture §3.6.6. The bundle is the customer's "Download all my
data" payload, generated asynchronously and delivered via a signed S3 URL.

RESCAN TIER-DE: switched from .tar.gz/JSONL to open ZIP per §5.10 and
the Enterprise security-review evidence package (customer-facing
commitment). Format is now:

  export_bundle_{admin_id}_{timestamp}.zip
   README.txt
   conversations/{session_id}.json        (one file per session)
   conversations/conversations.csv        (flattened CSV)
   leads.json
   leads.csv
   knowledge/{original_source_filename}   (originals if available)
   knowledge/manifest.json
   instances.json  (provider + non_secret_config + status; NEVER secrets)
   audit_log.jsonl
   manifest.json   (bundle metadata)

Originals note: VantageMind v1 does not retain uploaded source files
after ingestion (Arc 10 Option-2 deferral; Arc 11 owns the knowledge S3
bucket). When originals are unavailable, chunk reconstructions are written
under knowledge/chunks/{src_id}__v{n}.jsonl and the README documents this.

Free-tier gate (RESCAN TIER-DE §5.10): self-serve export is restricted to
admins in closure/grace-window state on the Free tier. Pro/Enterprise may
export at any time. The gate is enforced in enqueue().

Audit event data_export_self_serve (§5.2) is emitted for Pro/Enterprise
self-serve exports so the forensics team can trace non-closure export usage.

Bundle contents unchanged from Arc 10:
  * Generated asynchronously by a Celery task.
  * Uploaded to S3 via upload_fileobj.
  * Per-admin concurrency lock via ux_data_export_jobs_one_active_per_admin.
  * Signed URL TTL: 7 days on Free/Pro, 90 days on Enterprise.
  * Available during the closure grace window.
  * Job status machine: pending → generating → ready / expired / failed.

Originals note (Arc 10 Option-2 / RESCAN TIER-DE):
  Original uploaded knowledge files are not retained at ingestion time by
  VantageMind v1. Arc 11 owns the knowledge S3 bucket and will add
  original-file retention. The text content of each source is
  reconstructed from indexed chunks and written to the ZIP under
  knowledge/chunks/. The README.txt documents this limitation.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import uuid
import zipfile
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
    ACTION_DATA_EXPORT_SELF_SERVE,
    RESOURCE_TENANT,
)

logger = logging.getLogger(__name__)


Tier = Literal["free", "pro"]
TriggeredBy = Literal["admin_request", "grace_window_request"]
JobStatus = Literal["pending", "generating", "ready", "expired", "failed"]


# ---------------------------------------------------------------------
# Tier-conditional signed-URL TTL.
# ---------------------------------------------------------------------
# Vision §7 tier matrix: pre-closure data export is "Yes (7-day
# window)" on Free + Pro. (Enterprise tier deferred -- Open Decision
# #8; removed in Unit 1.)
_TIER_URL_TTL_SECONDS: dict[Tier, int] = {
    "free":       7  * 24 * 3600,
    "pro":        7  * 24 * 3600,
}

# S3 bucket name comes from settings.
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
    """Internal failure during bundle generation — wraps the cause."""


class ExportFreeGateError(DataExportError):
    """Free-tier self-serve export blocked outside closure window.

    RESCAN TIER-DE §5.10: Free admins may only export data when their
    account is in the closure / grace-window state. Pro and Enterprise
    admins may export at any time.
    """


# ---------------------------------------------------------------------
# Result shape.
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class DataExportJob:
    """Returned from enqueue() and used as the route's response body."""
    id: str
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
    """Orchestrates data export.

    Lifetime: one instance per request OR per Celery task invocation.
    s3_client and audit_repository are injected so unit tests can stub
    the AWS surface and verify audit emissions independently.
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
    # Public — enqueue (route layer entry).
    # -----------------------------------------------------------------

    def enqueue(
        self,
        *,
        admin_id: str,
        triggered_by: TriggeredBy,
        tier_at_request: Tier,
        audit_ctx,
        closure_initiated_at: datetime | None = None,
    ) -> DataExportJob:
        """Insert a pending data_export_jobs row.

        RESCAN TIER-DE §5.10 — Free-tier gate:
          * Free admins: only allowed when closure_initiated_at is set
            (i.e. account is in closure or grace-window state).
          * Pro/Enterprise: allowed at any time; emits
            data_export_self_serve audit if not closure-triggered.

        Raises ExportFreeGateError for out-of-window Free requests.
        """
        # Free-tier gate: closure-only.
        if tier_at_request == "free" and closure_initiated_at is None:
            raise ExportFreeGateError(
                "Free-tier accounts may only export data during the "
                "account closure / grace window. Upgrade to Pro "
                "for self-serve export at any time."
            )

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
            if "ux_data_export_jobs_one_active_per_admin" in str(exc.orig):
                raise ExportAlreadyInFlightError(
                    f"An export job is already in flight for admin "
                    f"{admin_id!r}. Wait for it to complete or fail."
                ) from exc
            raise

        # Audit row.
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

        # RESCAN TIER-DE §5.10: emit data_export_self_serve for
        # Pro non-closure exports so the forensics team can identify
        # self-serve data-portability requests. (Enterprise tier
        # deferred -- Open Decision #8; removed in Unit 1.)
        if tier_at_request == "pro" and triggered_by == "admin_request":
            self.audit_repository.record(
                ctx=audit_ctx,
                admin_id=admin_id,
                action=ACTION_DATA_EXPORT_SELF_SERVE,
                resource_type=RESOURCE_TENANT,
                resource_natural_id=admin_id,
                after={
                    "job_id": str(job_id),
                    "tier_at_request": tier_at_request,
                },
                note=(
                    f"Self-serve data export requested by {admin_id} "
                    f"({tier_at_request} tier)."
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
    # Public — generate_bundle (Celery task entry).
    # -----------------------------------------------------------------

    def generate_bundle(self, job_id: str) -> None:
        """Generate the ZIP bundle for a pending job and upload to S3.

        RESCAN TIER-DE: produces a ZIP per §5.10 / §3.6.6.
        """
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
            logger.info(
                "data_export_service: generate_bundle skipped — "
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

        ttl_seconds = _TIER_URL_TTL_SECONDS[tier_at_request]
        ready_at = datetime.now(timezone.utc)
        signed_url_expires_at = ready_at + timedelta(seconds=ttl_seconds)

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
    # Public — get_signed_url (route layer entry).
    # -----------------------------------------------------------------

    def get_signed_url(
        self,
        *,
        job_id: str,
        admin_id: str,
    ) -> tuple[str, datetime]:
        """Return (signed_url, expires_at) for a ready job."""
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
            raise ExportNotReadyError(
                f"Export job {job_id!r} signed URL has expired "
                f"({expires_at.isoformat()})."
            )

        remaining = int(
            (expires_at - datetime.now(timezone.utc)).total_seconds()
        )
        url = self.s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": s3_bucket, "Key": s3_key},
            ExpiresIn=max(remaining, 60),
        )
        return url, expires_at

    # -----------------------------------------------------------------
    # Internals — bundle building.
    # -----------------------------------------------------------------

    def _build_and_upload_bundle(
        self,
        *,
        admin_id: str,
        tier_at_request: Tier,
        job_id: str,
    ) -> tuple[str, int]:
        """Build the ZIP bundle and upload to S3.

        RESCAN TIER-DE: changed from tarfile w:gz to zipfile.ZipFile per
        §5.10 / §3.6.6 (Enterprise security-review evidence commitment).
        S3 key uses .zip extension; ContentType is application/zip.

        Returns (s3_key, bytes_written).
        """
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        zip_name = f"export_bundle_{admin_id}_{ts}.zip"
        s3_key = f"{_S3_KEY_PREFIX}/{admin_id}/{job_id}/{zip_name}"

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            self._write_manifest(zf, admin_id=admin_id,
                                 tier_at_request=tier_at_request,
                                 job_id=job_id, zip_name=zip_name)
            self._write_readme(zf)
            self._write_conversations(zf, admin_id=admin_id)
            self._write_leads(zf, admin_id=admin_id)
            self._write_audit_log(zf, admin_id=admin_id)
            self._write_instances(zf, admin_id=admin_id)
            self._write_knowledge(zf, admin_id=admin_id)

        bytes_written = buf.tell()
        buf.seek(0)
        self.s3.upload_fileobj(
            Fileobj=buf,
            Bucket=self.bucket,
            Key=s3_key,
            ExtraArgs={"ContentType": "application/zip"},
        )
        return s3_key, bytes_written

    def _write_manifest(
        self,
        zf: zipfile.ZipFile,
        *,
        admin_id: str,
        tier_at_request: Tier,
        job_id: str,
        zip_name: str,
    ) -> None:
        """§5.10 manifest.json — bundle metadata."""
        manifest = {
            "schema_version": 2,
            "bundle_id": job_id,
            "bundle_filename": zip_name,
            "admin_id": admin_id,
            "tier_at_request": tier_at_request,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "format": "zip",
            "contents": [
                "README.txt",
                "manifest.json",
                "conversations/{session_id}.json",
                "conversations/conversations.csv",
                "leads.json",
                "leads.csv",
                "knowledge/manifest.json",
                "knowledge/chunks/*.jsonl",
                "instances.json",
                "audit_log.jsonl",
            ],
            "originals_retained_in_bundle": False,
            "originals_retention_note": (
                "Original uploaded knowledge files are not retained at "
                "ingestion time by VantageMind v1 (Arc 10 Option-2 "
                "deferral). Arc 11 will add original-file retention. "
                "The text content of each source has been reconstructed "
                "from indexed chunks and is available under "
                "knowledge/chunks/. See README.txt for details."
            ),
        }
        self._add_text_entry(zf, "manifest.json",
                             json.dumps(manifest, indent=2))

    def _write_readme(self, zf: zipfile.ZipFile) -> None:
        self._add_text_entry(zf, "README.txt", _README_TEXT)

    def _write_conversations(
        self,
        zf: zipfile.ZipFile,
        *,
        admin_id: str,
    ) -> None:
        """§5.10: conversations/{session_id}.json + conversations.csv.

        One JSON file per session with transcript + metadata, plus a
        flattened CSV for spreadsheet consumers.
        """
        rows = self.db.execute(
            sql_text(
                """
                SELECT s.id            AS session_id,
                       s.created_at    AS session_created_at,
                       s.channel       AS channel,
                       s.luciel_instance_id AS instance_id,
                       c.id            AS conversation_id,
                       c.created_at    AS conv_created_at,
                       c.updated_at    AS conv_updated_at,
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
                  FROM sessions s
             LEFT JOIN conversations c ON c.id = s.conversation_id
             LEFT JOIN messages m ON m.session_id = s.id
                 WHERE s.admin_id = :aid
              GROUP BY s.id, c.id
              ORDER BY s.id ASC
                """
            ),
            {"aid": admin_id},
        )

        # CSV header for the flattened conversations.csv.
        csv_buf = io.StringIO()
        csv_writer = csv.writer(csv_buf)
        csv_writer.writerow([
            "session_id", "session_created_at", "channel",
            "instance_id", "conversation_id",
            "conv_created_at", "conv_updated_at",
            "message_count",
        ])

        for row in rows:
            session_id = str(row[0])
            session_created_at = _iso(row[1])
            channel = row[2]
            instance_id = row[3]
            conversation_id = str(row[4]) if row[4] else None
            conv_created_at = _iso(row[5])
            conv_updated_at = _iso(row[6])
            messages = row[7] if row[7] else []

            # Per-session JSON.
            session_obj = {
                "session_id": session_id,
                "session_created_at": session_created_at,
                "channel": channel,
                "instance_id": instance_id,
                "conversation_id": conversation_id,
                "conv_created_at": conv_created_at,
                "conv_updated_at": conv_updated_at,
                "messages": messages,
            }
            safe_session_id = session_id.replace("/", "_").replace("..", "_")
            self._add_text_entry(
                zf,
                f"conversations/{safe_session_id}.json",
                json.dumps(session_obj, default=str, indent=2),
            )

            # CSV row.
            csv_writer.writerow([
                session_id,
                session_created_at,
                channel,
                instance_id,
                conversation_id,
                conv_created_at,
                conv_updated_at,
                len(messages) if isinstance(messages, list) else 0,
            ])

        self._add_text_entry(zf, "conversations/conversations.csv",
                             csv_buf.getvalue())

    def _write_leads(self, zf: zipfile.ZipFile, *, admin_id: str) -> None:
        """§5.10: leads.json + leads.csv."""
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
        leads = [r[0] for r in rows]

        # leads.json
        self._add_text_entry(
            zf, "leads.json",
            json.dumps(leads, default=str, indent=2),
        )

        # leads.csv — flatten the JSONB rows.
        if leads:
            # Use first row's keys as headers.
            first = leads[0] if isinstance(leads[0], dict) else {}
            headers = list(first.keys())
            csv_buf = io.StringIO()
            csv_writer = csv.DictWriter(csv_buf, fieldnames=headers,
                                        extrasaction="ignore")
            csv_writer.writeheader()
            for lead in leads:
                if isinstance(lead, dict):
                    csv_writer.writerow(
                        {k: json.dumps(v, default=str) if isinstance(v, (dict, list)) else v
                         for k, v in lead.items()}
                    )
            self._add_text_entry(zf, "leads.csv", csv_buf.getvalue())
        else:
            self._add_text_entry(zf, "leads.csv", "")

    def _write_audit_log(
        self,
        zf: zipfile.ZipFile,
        *,
        admin_id: str,
    ) -> None:
        """§5.10: audit_log.jsonl — within retention window.

        RESCAN TIER-DE: changed from CSV to JSONL per §5.10.
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
        lines = []
        for row in rows:
            lines.append(json.dumps({
                "id": row[0],
                "created_at": _iso(row[1]),
                "action": row[2],
                "resource_type": row[3],
                "resource_pk": row[4],
                "resource_natural_id": row[5],
                "actor_key_prefix": row[6],
                "tier_at_write": row[7],
                "cold_archived_at": _iso(row[8]),
                "before_json": row[9],
                "after_json": row[10],
                "note": row[11],
            }, default=str))
        self._add_text_entry(
            zf, "audit_log.jsonl",
            "\n".join(lines) + ("\n" if lines else ""),
        )

    def _write_instances(
        self,
        zf: zipfile.ZipFile,
        *,
        admin_id: str,
    ) -> None:
        """§5.10: instances.json — provider + non_secret_config + status.

        RESCAN TIER-DE security invariant: secret_ref and any
        secret-bearing columns MUST NEVER be written here. We query the
        instance_connections table with an explicit column allowlist and
        strip secret_ref entirely. The instances table itself carries
        no secrets (those ride behind secret_ref on instance_connections).
        """
        # Read instance rows (no secrets on the instances table itself).
        inst_rows = self.db.execute(
            sql_text(
                """
                SELECT id, admin_id, display_name, instance_status,
                       created_at, updated_at, soft_deleted_at,
                       instance_status_note
                  FROM instances
                 WHERE admin_id = :aid
              ORDER BY id ASC
                """
            ),
            {"aid": admin_id},
        )

        # Read instance_connections — provider + non_secret_config + status.
        # secret_ref is EXCLUDED per §5.10 security invariant.
        conn_rows = self.db.execute(
            sql_text(
                """
                SELECT instance_id, connection_type, provider,
                       non_secret_config, status,
                       last_health_check_at, created_at, updated_at
                  FROM instance_connections
                 WHERE admin_id = :aid
              ORDER BY instance_id ASC, id ASC
                """
            ),
            {"aid": admin_id},
        )
        # Group connections by instance_id.
        conn_by_instance: dict[int, list[dict]] = {}
        for cr in conn_rows:
            iid = int(cr[0])
            conn_by_instance.setdefault(iid, []).append({
                "connection_type": cr[1],
                "provider": cr[2],
                # non_secret_config is non_secret_config (no secret_ref).
                "non_secret_config": cr[3],
                "status": cr[4],
                "last_health_check_at": _iso(cr[5]),
                "created_at": _iso(cr[6]),
                "updated_at": _iso(cr[7]),
                # secret_ref intentionally omitted — NEVER secret material.
            })

        instances_out = []
        for ir in inst_rows:
            iid = int(ir[0])
            instances_out.append({
                "id": iid,
                "admin_id": ir[1],
                "display_name": ir[2],
                "instance_status": ir[3].value if hasattr(ir[3], "value") else ir[3],
                "created_at": _iso(ir[4]),
                "updated_at": _iso(ir[5]),
                "soft_deleted_at": _iso(ir[6]),
                "instance_status_note": ir[7],
                # §5.10: include provider + non_secret_config + status.
                "connections": conn_by_instance.get(iid, []),
            })

        self._add_text_entry(
            zf, "instances.json",
            json.dumps(instances_out, default=str, indent=2),
        )

    def _write_knowledge(
        self,
        zf: zipfile.ZipFile,
        *,
        admin_id: str,
    ) -> None:
        """§5.10: knowledge/manifest.json + originals or chunk reconstructions.

        RESCAN TIER-DE originals-first preference: attempt to pull the
        original file from S3 (settings.knowledge_bucket). If the original
        is not available (Arc 10 Option-2 — originals not retained at
        ingestion time), fall through to chunk reconstruction and note the
        limitation in the manifest entry.

        Currently VantageMind v1 does NOT retain original uploaded files
        (Arc 10 Option-2; Arc 11 owns the knowledge S3 bucket). Originals
        will be added in a future release. This method documents the
        limitation and writes chunk reconstructions under
        knowledge/chunks/.
        """
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
                         WHERE c.source_id = s.id
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
        per_source: list[tuple[int, str, int]] = []  # (pk, filename, version)
        for r in ks_rows:
            source_pk = int(r.source_pk)
            source_filename = r.source_filename or f"source_{source_pk}"
            source_version = int(r.source_version)
            manifest.append({
                "source_pk": source_pk,
                "source_uuid": str(r.source_uuid),
                "source_id": f"src-{source_pk}",
                "source_filename": source_filename,
                "source_type": r.source_type,
                "source_version": source_version,
                "ingested_by": r.ingested_by,
                "ingested_at": _iso(r.ingested_at),
                "size_bytes": int(r.size_bytes or 0),
                "chunk_count": int(r.chunk_count or 0),
                "ingestion_status": r.ingestion_status,
                # RESCAN TIER-DE: originals not retained in v1 (Arc 10
                # Option-2). Arc 11 will add original-file retention.
                # When originals ARE available, this field becomes True
                # and knowledge/{source_filename} contains the original.
                "originals_retained": False,
                "originals_note": (
                    "Original file not retained at ingestion time (Arc 10 "
                    "Option-2). Chunk reconstruction available in "
                    f"knowledge/chunks/src-{source_pk}__v{source_version}.jsonl."
                ),
                "archived_on_downgrade": bool(r.archived_on_downgrade),
            })
            per_source.append((source_pk, source_filename, source_version))

        self._add_text_entry(
            zf,
            "knowledge/manifest.json",
            json.dumps(manifest, default=str, indent=2),
        )

        # Per-source chunk files.  Originals are not available (v1).
        # Future: when Arc 11 adds original-file retention, pull from
        # settings.knowledge_bucket and write under
        # knowledge/{source_filename} instead.
        for source_pk, source_filename, source_version in per_source:
            chunk_rows = self.db.execute(
                sql_text(
                    """
                    SELECT id, title, content, knowledge_type, created_at
                      FROM knowledge_chunks
                     WHERE admin_id = :aid
                       AND source_id = :sid
                       AND source_version = :sv
                       AND superseded_at IS NULL
                       AND soft_deleted_at IS NULL
                  ORDER BY id ASC
                    """
                ),
                {"aid": admin_id, "sid": source_pk, "sv": source_version},
            )
            file_key = f"src-{source_pk}"
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
                zf,
                f"knowledge/chunks/"
                f"{file_key}__v{source_version}.jsonl",
                "\n".join(lines) + ("\n" if lines else ""),
            )

    # -----------------------------------------------------------------
    # Internals — small utilities.
    # -----------------------------------------------------------------

    @staticmethod
    def _add_text_entry(
        zf: zipfile.ZipFile,
        name: str,
        content: str,
    ) -> None:
        """Add a text file to the ZIP from an in-memory string."""
        zf.writestr(name, content.encode("utf-8"))

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
    """Build an AuditContext.system() for export-worker rows."""
    from app.repositories.admin_audit_repository import AuditContext
    return AuditContext.system(label="data_export_worker")


_README_TEXT = """VantageMind — Data Export Bundle
=================================

This ZIP archive contains every piece of data VantageMind holds for
your account at the moment of generation.

Layout
------

  README.txt
    This file.

  manifest.json
    Bundle metadata: when it was generated, your tier, contents.
    schema_version: 2 (ZIP format, per §5.10).

  conversations/{session_id}.json
    One JSON file per widget/chat session, containing the transcript
    (all messages), session metadata, and the linked conversation ID.

  conversations/conversations.csv
    Flattened CSV view of all sessions for spreadsheet import.

  leads.json
    Every lead captured by VantageMind (identity claims), as JSON.

  leads.csv
    Same data as leads.json in CSV format.

  knowledge/manifest.json
    Per-source metadata for your ingested knowledge: original filename,
    source type, ingestion date, chunk count, originals_retained flag.

  knowledge/chunks/{src_id}__v{n}.jsonl
    Chunk-level text reconstruction for each knowledge source.
    One JSON object per line (title, content, knowledge_type, created_at).

  instances.json
    Configuration of every VantageMind instance you created.
    Includes: provider, non_secret_config, connection status.
    Does NOT include secret material (API keys, passwords, tokens).

  audit_log.jsonl
    Every admin action recorded against your account within the
    retention window for your tier (Free: 30 days, Pro: 1 year,
    Enterprise: 7 years). One JSON object per line.

A note on knowledge originals
------------------------------

VantageMind v1 does not retain the original uploaded files (PDFs, DOCX,
CSV) after ingestion. The text content of each source has been
reconstructed from indexed chunks under knowledge/chunks/. The
knowledge/manifest.json lists each source's original filename so you
can match it back to your local copy.

Future versions of VantageMind will retain originals; if you re-ingest
a file after that upgrade, your next export will include both the
original and the reconstructed text.

Security note
-------------

This bundle does NOT contain any API keys, passwords, OAuth tokens, or
other secret credentials. Connection secrets are stored separately and
are never included in data exports.

Schema version
--------------

This bundle is schema_version: 2 (ZIP format, open standard).
Previous bundles (schema_version: 1) were in .tar.gz/JSONL format.
Both formats carry the same customer data; the ZIP format was adopted
to align with the §5.10 open-standard commitment and to ease import
by third-party tooling.
"""
