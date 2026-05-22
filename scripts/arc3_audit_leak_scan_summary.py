"""scripts/arc3_audit_leak_scan_summary.py -- Arc 3 Work-Unit A.2b.

Writes a SINGLE hash-chain-safe entry to admin_audit_logs that records
the outcome of a CloudWatch token-backlog scan -- including scans that
flip zero rows.

Why a sibling script instead of folding this into
arc3_audit_leaked_invites_record.py:

  The row-level recorder takes a PSV of flipped invites and emits one
  audit row per flip. When zero invites flip (which is the actually-
  healthy outcome for a well-designed invite system, where leaked JTIs
  are most likely already-accepted or already-revoked), that script
  emits zero audit rows -- and the investigation itself becomes
  invisible to the audit trail. This script closes that gap by writing
  one summary row that captures the bucket math so dashboards can see
  WHEN we scanned, WHAT we scanned, and WHY no row-level remediation
  was required.

Hash-chain safety: uses AdminAuditRepository.record() (same as the
row-level recorder); raw SQL INSERT would break the
sha256(canonical_content + prev_row_hash) integrity contract.

Idempotency: keyed on (action, resource_type, resource_natural_id).
The resource_natural_id is a deterministic sentinel of the form
"leak-scan-YYYY-MM-DD"; re-running the script the same day with the
same args is a no-op.

Usage:
  python scripts/arc3_audit_leak_scan_summary.py \\
      --scan-date 2026-05-21 \\
      --jti-file arc3-out/leaked-welcome-jtis.txt \\
      --pending 0 --accepted 2 --revoked 4 --expired 0 \\
      --unmatched 13 --total 19

Env:
  DATABASE_URL must be set to the prod (or restore-staging) Postgres
  URL. The script uses the same SQLAlchemy session bootstrap as the
  backend ECS task.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

# These imports require the project venv to be activated and CWD == repo
# root so app.* resolves correctly.
from app.db.session import SessionLocal
from app.models.admin_audit_log import (
    ACTION_INVITE_REVOKED,
    AdminAuditLog,
    RESOURCE_USER_INVITE,
)
from app.repositories.admin_audit_repository import (
    AdminAuditRepository,
    AuditContext,
)

CLOSURE_DRIFT = "D-set-password-token-logged-plaintext-2026-05-17"
CLOSING_TAG = "arc-3-paired-prod-touch"
NOTE_REASON = "arc-3-leak-scan-summary"


def _already_recorded(db, natural_id: str) -> bool:
    """Idempotency: has the summary row for this scan date already landed?"""
    stmt = select(AdminAuditLog).where(
        AdminAuditLog.action == ACTION_INVITE_REVOKED,
        AdminAuditLog.resource_type == RESOURCE_USER_INVITE,
        AdminAuditLog.resource_natural_id == natural_id,
        AdminAuditLog.note.like(f"%{NOTE_REASON}%"),
    )
    return db.execute(stmt).first() is not None


def _load_jtis(path: str) -> list[str]:
    """Read JTIs from the leaked-welcome-jtis file, one per line."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"JTI file not found: {path}")
    jtis = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        jtis.append(line)
    return jtis


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scan-date", required=True,
                    help="ISO date YYYY-MM-DD; used as the sentinel "
                         "resource_natural_id suffix.")
    ap.add_argument("--jti-file", required=True,
                    help="Path to the leaked-JTI list (one UUID per line).")
    ap.add_argument("--pending", type=int, required=True)
    ap.add_argument("--accepted", type=int, required=True)
    ap.add_argument("--revoked", type=int, required=True)
    ap.add_argument("--expired", type=int, required=True)
    ap.add_argument("--unmatched", type=int, required=True,
                    help="JTIs with NO matching row in user_invites; "
                         "almost certainly non-invite tokens (login/reset/welcome).")
    ap.add_argument("--total", type=int, required=True,
                    help="Total unique JTIs scanned; must equal "
                         "pending+accepted+revoked+expired+unmatched.")
    ap.add_argument("--discovery-window", default="2026-05-13..2026-05-20",
                    help="CloudWatch discovery window for traceability.")
    args = ap.parse_args()

    # Internal-consistency check: bucket sum must equal --total.
    bucket_sum = (
        args.pending + args.accepted + args.revoked
        + args.expired + args.unmatched
    )
    if bucket_sum != args.total:
        print(
            f"FATAL: bucket sum {bucket_sum} != --total {args.total}; "
            f"refusing to write an inconsistent audit row.",
            file=sys.stderr,
        )
        return 2

    jtis = _load_jtis(args.jti_file)
    if len(jtis) != args.total:
        print(
            f"FATAL: JTI file has {len(jtis)} unique lines but --total is "
            f"{args.total}; refusing to write a mismatched audit row.",
            file=sys.stderr,
        )
        return 2

    natural_id = f"leak-scan-{args.scan_date}"

    db = SessionLocal()
    repo = AdminAuditRepository(db)
    ctx = AuditContext.system(label="arc3_leak_scan_summary")

    try:
        if _already_recorded(db, natural_id):
            print(
                f"already recorded: natural_id={natural_id} -- no-op."
            )
            return 0

        repo.record(
            ctx=ctx,
            tenant_id="",  # falls through to SYSTEM_ACTOR_TENANT; cross-tenant summary.
            action=ACTION_INVITE_REVOKED,
            resource_type=RESOURCE_USER_INVITE,
            resource_pk=None,
            resource_natural_id=natural_id,
            domain_id=None,
            after={
                "scan_kind": "cloudwatch_token_backlog",
                "scan_date": args.scan_date,
                "discovery_window": args.discovery_window,
                "buckets": {
                    "pending": args.pending,
                    "accepted": args.accepted,
                    "revoked": args.revoked,
                    "expired": args.expired,
                    "unmatched": args.unmatched,
                },
                "total_jtis_scanned": args.total,
                "rows_flipped_pending_to_revoked": 0,
                "leaked_jtis": jtis,  # UUIDs only -- no token material.
                "closure_drift": CLOSURE_DRIFT,
                "closing_tag": CLOSING_TAG,
                "trigger": "cloudwatch_token_backlog_audit",
                "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
                "verdict": (
                    "No row-level remediation required: zero pending "
                    "invites matched. Unmatched JTIs are non-invite "
                    "tokens (login/reset/welcome) revoked at the "
                    "JWT-validation layer via signing-key rotation "
                    "(deferred to Arc 3 Work-Unit B)."
                ),
            },
            note=(
                f"Arc 3 token-backlog CloudWatch scan summary "
                f"({NOTE_REASON}): {args.total} leaked JTIs scanned in "
                f"discovery window {args.discovery_window}; buckets "
                f"pending={args.pending} accepted={args.accepted} "
                f"revoked={args.revoked} expired={args.expired} "
                f"unmatched={args.unmatched}; 0 rows flipped. "
                f"Closure of {CLOSURE_DRIFT}."
            ),
            autocommit=False,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    print(
        f"recorded scan summary: natural_id={natural_id} "
        f"total={args.total} flipped=0"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
