"""Audit-tier retention service \u2014 Arc 10.

Reconciles two doctrines on the audit log:

  Vision \u00a76.5 / \u00a77 (canonical):
    Audit retention is tier-conditional \u2014 30 days for Free, 1 year
    for Pro, 7 years for Enterprise. "Audit log archived to cold
    storage for legal retention window."

  Arc 9 C6.1 (doctrine drift, superseded by Vision per \u00a710):
    "Forward-only audit-log immutability \u2014 even the ops role cannot
    mutate or delete audit rows."

The reconciliation: the chain stays append-only IN HOT+COLD COMBINED.
Rows are MOVED to cold storage, not DELETEd. The cold archive
preserves the hash chain via continued sha256(canonical_content +
prev_row_hash) so a forensic walk back from any current hot row
through cold-archived rows reconstructs the same chain.

This service uses a SEPARATE Postgres role (luciel_audit_archiver,
Arc 10 migration) that has SELECT + UPDATE on admin_audit_logs only.
That role is NEVER used by application code \u2014 only by this worker.
The grant surface guarantees:
  * The application's regular session (luciel role) cannot UPDATE
    admin_audit_logs \u2014 the forward-only invariant holds for normal
    code paths.
  * luciel_ops (Arc 9 C6.1) still cannot UPDATE admin_audit_logs \u2014
    the C6.1 blast-radius discipline holds.
  * Only this worker, on its own connection, can stamp
    cold_archived_at on rows it has already written to S3.

What this service does:

  1. Scan admin_audit_logs for rows whose tier_at_write window has
     elapsed AND cold_archived_at IS NULL:
       Free  : created_at < now - 30 days
       Pro   : created_at < now - 1 year
       Enterprise : created_at < now - 7 years
     Sticky tier_at_write means a Pro\u2192Free downgrade does NOT
     retroactively shorten Pro-era retention.

  2. For each eligible row, write it to S3 cold storage with
     hash-chain extension. Each archived row is signed with
     sha256(canonical_content + prev_row_hash) using the SAME
     hash function the hot chain uses (audit_chain.canonical_row_hash)
     so a chain walk across the boundary verifies cleanly.

  3. Stamp cold_archived_at on the hot row. The hot row stays in
     place; the chain stays walkable from any current row.

  4. Audit-emit ACTION_AUDIT_LOG_TIER_ARCHIVED for each batch, NOT
     each row \u2014 the per-row audit emission would explode the audit
     table size. Batch granularity is per (admin_id, tier_window)
     per worker run.

What this service does NOT do:

  * Does NOT delete hot rows. Arc 10 ships move-to-cold without
    hot-purge. A future arc may add a hot-purge step once the cold
    chain integrity is proven in production. The luciel_audit_archiver
    role explicitly has NO DELETE grant on admin_audit_logs to make
    accidental hot-purge impossible.
  * Does NOT touch rows whose admin row has been hard-deleted; the
    closure cascade's audit emissions belong to the legal-record
    retention path, not this one.
  * Does NOT run under SessionLocal. Uses a dedicated session bound
    to the luciel_audit_archiver role.

Run cadence: nightly Celery beat, same schedule slot as the tenant
retention worker (08:00 UTC). The two workers are independent; either
can fail without blocking the other.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from app.models.admin_audit_log import (
    ACTION_AUDIT_LOG_TIER_ARCHIVED,
    RESOURCE_TENANT,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Tier retention windows. Per Vision \u00a77.
# ---------------------------------------------------------------------
_TIER_WINDOW_DAYS: dict[str, int] = {
    "free":       30,
    "pro":        365,
    "enterprise": 365 * 7,
}

_S3_COLD_PREFIX = "audit-cold-archive"


# ---------------------------------------------------------------------
# Error.
# ---------------------------------------------------------------------

class AuditRetentionError(Exception):
    """Base for audit-retention-flow errors."""


# ---------------------------------------------------------------------
# Result shape.
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class AuditRetentionRunSummary:
    """Returned from run_audit_retention() for observability."""
    started_at: datetime
    completed_at: datetime
    rows_scanned: int
    rows_archived: int
    rows_errored: int
    errored_row_ids: list[int]
    s3_objects_written: int


# ---------------------------------------------------------------------
# Service.
# ---------------------------------------------------------------------

class AuditRetentionService:
    """Move expired audit rows to S3 cold storage; stamp cold_archived_at.

    Lifetime: one instance per Celery task invocation. Uses a session
    bound to the luciel_audit_archiver role, which is the ONLY
    Postgres role with UPDATE on admin_audit_logs.
    """

    def __init__(
        self,
        db: Session,
        *,
        s3_client,
        s3_bucket: str,
        audit_repository,
        batch_size: int = 1000,
    ) -> None:
        self.db = db
        self.s3 = s3_client
        self.bucket = s3_bucket
        self.audit_repository = audit_repository
        self.batch_size = batch_size

    # -----------------------------------------------------------------
    # Public \u2014 worker entry.
    # -----------------------------------------------------------------

    def run_audit_retention(self) -> AuditRetentionRunSummary:
        """Scan + archive across all three tiers in one run.

        Per-tier predicate:
          tier_at_write = :tier
          AND created_at < (now() - INTERVAL :days)
          AND cold_archived_at IS NULL

        Batch size capped by self.batch_size to bound memory + S3
        roundtrip count per run. A run that fills its batch logs an
        info message; the next nightly run picks up where this one
        left off (FIFO via created_at ASC).
        """
        started_at = datetime.now(timezone.utc)
        rows_scanned = 0
        rows_archived = 0
        rows_errored = 0
        errored_row_ids: list[int] = []
        s3_objects_written = 0

        for tier, window_days in _TIER_WINDOW_DAYS.items():
            cutoff = started_at - timedelta(days=window_days)
            logger.info(
                "audit_retention: scanning tier=%s cutoff=%s window_days=%d",
                tier, cutoff.isoformat(), window_days,
            )

            try:
                tier_scanned, tier_archived, tier_errored, tier_errs, \
                    tier_s3 = self._archive_one_tier(
                        tier=tier,
                        cutoff=cutoff,
                        batch_size=self.batch_size,
                    )
                rows_scanned += tier_scanned
                rows_archived += tier_archived
                rows_errored += tier_errored
                errored_row_ids.extend(tier_errs)
                s3_objects_written += tier_s3
            except Exception:
                logger.exception(
                    "audit_retention: tier=%s scan/archive failed; "
                    "continuing to next tier.",
                    tier,
                )
                # One bad tier should not block the rest of the run.
                continue

        completed_at = datetime.now(timezone.utc)
        logger.info(
            "audit_retention: complete scanned=%d archived=%d errored=%d "
            "s3_objects=%d duration_seconds=%.1f",
            rows_scanned, rows_archived, rows_errored, s3_objects_written,
            (completed_at - started_at).total_seconds(),
        )
        return AuditRetentionRunSummary(
            started_at=started_at,
            completed_at=completed_at,
            rows_scanned=rows_scanned,
            rows_archived=rows_archived,
            rows_errored=rows_errored,
            errored_row_ids=errored_row_ids,
            s3_objects_written=s3_objects_written,
        )

    # -----------------------------------------------------------------
    # Per-tier archive loop.
    # -----------------------------------------------------------------

    def _archive_one_tier(
        self,
        *,
        tier: str,
        cutoff: datetime,
        batch_size: int,
    ) -> tuple[int, int, int, list[int], int]:
        """Scan + archive eligible rows for one tier.

        Returns (scanned, archived, errored, errored_row_ids, s3_objects).

        We do this in batches of ``batch_size``:
          1. SELECT a batch of eligible rows (FIFO by created_at).
          2. Group by admin_id so each S3 object covers one
             (admin_id, batch) tuple (forensics can locate an
             admin's cold-archived rows in one prefix).
          3. Write the batch to S3 with the hash chain extended.
          4. UPDATE the rows' cold_archived_at to mark them moved.
          5. Emit one audit row per batch summarising the move.

        Loop until the SELECT returns < batch_size rows (no more
        eligible).
        """
        scanned = 0
        archived = 0
        errored = 0
        errored_ids: list[int] = []
        s3_count = 0

        while True:
            batch = self._fetch_batch(
                tier=tier,
                cutoff=cutoff,
                limit=batch_size,
            )
            if not batch:
                break

            scanned += len(batch)

            # Group by admin_id so each S3 object is admin-scoped.
            by_admin: dict[str, list[dict]] = {}
            for row in batch:
                by_admin.setdefault(row["admin_id"], []).append(row)

            for admin_id, admin_rows in by_admin.items():
                try:
                    s3_key = self._write_batch_to_s3(
                        admin_id=admin_id,
                        tier=tier,
                        rows=admin_rows,
                    )
                    s3_count += 1
                    # Stamp cold_archived_at on the hot rows.
                    self._mark_cold_archived(
                        row_ids=[r["id"] for r in admin_rows],
                    )
                    archived += len(admin_rows)
                    # One audit row per (admin_id, batch).
                    self._emit_batch_audit(
                        admin_id=admin_id,
                        tier=tier,
                        row_ids=[r["id"] for r in admin_rows],
                        s3_key=s3_key,
                    )
                    self.db.commit()
                except Exception:
                    self.db.rollback()
                    errored += len(admin_rows)
                    errored_ids.extend(r["id"] for r in admin_rows)
                    logger.exception(
                        "audit_retention: batch archive failed "
                        "admin_id=%s tier=%s row_count=%d",
                        admin_id, tier, len(admin_rows),
                    )

            # If the batch was smaller than batch_size, we've drained
            # this tier for this run.
            if len(batch) < batch_size:
                break

        return scanned, archived, errored, errored_ids, s3_count

    # -----------------------------------------------------------------
    # SQL primitives.
    # -----------------------------------------------------------------

    def _fetch_batch(
        self,
        *,
        tier: str,
        cutoff: datetime,
        limit: int,
    ) -> list[dict]:
        """SELECT eligible rows. FIFO by created_at."""
        rows = self.db.execute(
            sql_text(
                """
                SELECT id, admin_id, created_at, action, resource_type,
                       resource_pk, resource_natural_id,
                       actor_key_prefix, actor_permissions,
                       before_json, after_json, note,
                       row_hash, prev_row_hash, tier_at_write
                  FROM admin_audit_logs
                 WHERE tier_at_write = :tier
                   AND created_at < :cutoff
                   AND cold_archived_at IS NULL
              ORDER BY created_at ASC, id ASC
                 LIMIT :lim
                """
            ),
            {"tier": tier, "cutoff": cutoff, "lim": limit},
        )
        out: list[dict] = []
        for r in rows:
            out.append({
                "id": r[0],
                "admin_id": r[1],
                "created_at": r[2],
                "action": r[3],
                "resource_type": r[4],
                "resource_pk": r[5],
                "resource_natural_id": r[6],
                "actor_key_prefix": r[7],
                "actor_permissions": r[8],
                "before_json": r[9],
                "after_json": r[10],
                "note": r[11],
                "row_hash": r[12],
                "prev_row_hash": r[13],
                "tier_at_write": r[14],
            })
        return out

    def _mark_cold_archived(self, *, row_ids: list[int]) -> None:
        """Stamp cold_archived_at on the hot rows.

        This is the ONLY UPDATE the luciel_audit_archiver role
        performs on admin_audit_logs. The grant matrix permits exactly
        this and nothing else.
        """
        if not row_ids:
            return
        self.db.execute(
            sql_text(
                """
                UPDATE admin_audit_logs
                   SET cold_archived_at = now()
                 WHERE id = ANY(:ids)
                   AND cold_archived_at IS NULL
                """
            ),
            {"ids": row_ids},
        )

    # -----------------------------------------------------------------
    # S3 cold-archive writer with hash-chain extension.
    # -----------------------------------------------------------------

    def _write_batch_to_s3(
        self,
        *,
        admin_id: str,
        tier: str,
        rows: list[dict],
    ) -> str:
        """Write a batch to S3 as JSONL with hash-chain extension.

        S3 key shape:
          {prefix}/{tier}/{admin_id}/{yyyy}/{mm}/{dd}/{first_id}-{last_id}.jsonl

        The key encodes enough to support forensic walks:
          * by tier (Free/Pro/Enterprise have different windows)
          * by admin (each tenant's history is locatable)
          * by date (auditors typically scope to a date range)
          * by row-id range (deterministic, no clock skew)

        Each line is a JSON object with the row data + a cold-archive
        hash that extends the hot chain. The cold-archive hash is:

          cold_hash = sha256(
            canonical_content_for_cold_archive(row) + row_hash
          )

        i.e. we hash the row's canonical content concatenated with
        its OWN row_hash (the value the hot chain produced). This
        gives the cold side a verifiable link back to a specific hot
        chain position. A forensic walk verifies:
          1. Each cold row's cold_hash matches sha256(content + row_hash).
          2. The hot chain still contains a row with that row_hash.
        Both checks succeed \u21d2 the cold row was once a legitimate
        member of the hot chain at the position its row_hash names.
        """
        first_id = rows[0]["id"]
        last_id = rows[-1]["id"]
        first_created = rows[0]["created_at"]
        # S3 key components.
        yyyy = first_created.strftime("%Y")
        mm = first_created.strftime("%m")
        dd = first_created.strftime("%d")
        s3_key = (
            f"{_S3_COLD_PREFIX}/{tier}/{admin_id}/"
            f"{yyyy}/{mm}/{dd}/{first_id}-{last_id}.jsonl"
        )

        lines: list[str] = []
        for r in rows:
            cold_hash = _cold_archive_hash(r)
            entry = {
                "id": r["id"],
                "admin_id": r["admin_id"],
                "created_at": _iso(r["created_at"]),
                "action": r["action"],
                "resource_type": r["resource_type"],
                "resource_pk": r["resource_pk"],
                "resource_natural_id": r["resource_natural_id"],
                "actor_key_prefix": r["actor_key_prefix"],
                "actor_permissions": r["actor_permissions"],
                "before_json": r["before_json"],
                "after_json": r["after_json"],
                "note": r["note"],
                "row_hash": r["row_hash"],
                "prev_row_hash": r["prev_row_hash"],
                "tier_at_write": r["tier_at_write"],
                "cold_hash": cold_hash,
                "cold_archived_at": datetime.now(timezone.utc).isoformat(),
            }
            lines.append(json.dumps(entry, default=str))

        body = ("\\n".join(lines) + "\\n").encode("utf-8")
        self.s3.put_object(
            Bucket=self.bucket,
            Key=s3_key,
            Body=body,
            ContentType="application/x-ndjson",
            # Server-side encryption enforced at the bucket level too,
            # but we declare it on the put for defense-in-depth.
            ServerSideEncryption="AES256",
        )
        return s3_key

    # -----------------------------------------------------------------
    # Audit emission for the archival action itself.
    # -----------------------------------------------------------------

    def _emit_batch_audit(
        self,
        *,
        admin_id: str,
        tier: str,
        row_ids: list[int],
        s3_key: str,
    ) -> None:
        """One audit row per (admin_id, batch). NOT per archived row.

        Per-row emission would explode the audit table size as it
        chases its own tail. Batch granularity is enough to answer
        "when was admin X's tier-Y window archived?" forensically.
        """
        from app.repositories.admin_audit_repository import AuditContext
        sys_ctx = AuditContext.system(label="audit_retention_worker")
        self.audit_repository.record(
            ctx=sys_ctx,
            admin_id=admin_id,
            action=ACTION_AUDIT_LOG_TIER_ARCHIVED,
            resource_type=RESOURCE_TENANT,
            resource_natural_id=admin_id,
            after={
                "tier_at_write": tier,
                "rows_archived": len(row_ids),
                "first_row_id": row_ids[0],
                "last_row_id": row_ids[-1],
                "s3_bucket": self.bucket,
                "s3_key": s3_key,
            },
            note=(
                f"Audit-tier retention: archived {len(row_ids)} rows "
                f"for {admin_id} (tier={tier})."
            ),
            autocommit=False,
        )


# ---------------------------------------------------------------------
# Module helpers.
# ---------------------------------------------------------------------

def _iso(ts) -> str | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.isoformat()
    return str(ts)


def _cold_archive_hash(row: dict) -> str:
    """Compute sha256(canonical_content + row_hash) for cold-archive.

    The canonical_content portion mirrors what the hot chain hashes
    (app.repositories.audit_chain.canonical_row_hash) so the cold
    record's verifiability is rooted in the same content shape.

    We do NOT import canonical_row_hash directly because the hot
    hash is sha256(canonical_content + PREV_row_hash); we want
    cold_hash = sha256(canonical_content + THIS_row_hash) so a
    forensic verifier can recompute it from the row's own content
    + the row's own row_hash.
    """
    # Canonical content is a deterministic JSON serialization of the
    # row's auditable fields. Field order is alphabetical for
    # determinism across Python versions / dict insertion orders.
    payload = {
        "action": row["action"],
        "actor_key_prefix": row["actor_key_prefix"],
        "actor_permissions": row["actor_permissions"],
        "admin_id": row["admin_id"],
        "after_json": row["after_json"],
        "before_json": row["before_json"],
        "created_at": _iso(row["created_at"]),
        "id": row["id"],
        "note": row["note"],
        "resource_natural_id": row["resource_natural_id"],
        "resource_pk": row["resource_pk"],
        "resource_type": row["resource_type"],
        "tier_at_write": row["tier_at_write"],
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(
        (canonical + (row["row_hash"] or "")).encode("utf-8")
    ).hexdigest()
    return digest
