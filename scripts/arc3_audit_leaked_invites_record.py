"""scripts/arc3_audit_leaked_invites_record.py — Arc 3 Work-Unit A.2b.

Pairs with scripts/arc3_revoke_leaked_invites.sql to write the matching
admin_audit_logs entries for each invite the SQL block flipped from
'pending' to 'revoked'.

Why a separate Python step instead of folding the audit-row INSERT into
the SQL block:

  admin_audit_logs carries a hash-chain integrity contract
  (row_hash = sha256(canonical_content + prev_row_hash)) implemented in
  AdminAuditRepository.record(). Raw SQL INSERTs would silently break the
  chain, which is exactly the kind of audit-trail compromise this drift
  closure is supposed to prevent.

Input: the SQL block's pipe-delimited RETURNING output, captured into a
.psv file:

  psql ... -f scripts\arc3_revoke_leaked_invites.sql > arc3-out\flipped-invites.psv

Skipped rows (header, blank lines, psql echo lines) are ignored.

Idempotency: before recording each row, the script checks whether an
admin_audit_logs row already exists with the same (resource_natural_id,
note containing the closure_drift token) — if so, it skips. Safe to
re-run.

Usage:
  python scripts/arc3_audit_leaked_invites_record.py arc3-out/flipped-invites.psv

Env:
  DATABASE_URL must be set to the prod (or restore-staging) Postgres
  URL. The script uses the same SQLAlchemy session bootstrap as the
  backend ECS task.
"""
from __future__ import annotations

import csv
import os
import sys
from datetime import datetime, timezone
from typing import Iterable

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
NOTE_REASON = "arc-3-token-backlog-cloudwatch-audit"

PSQL_HEADER_COLS = (
    "invite_id",
    "tenant_id",
    "domain_id",
    "invited_email",
    "token_jti",
)


def _parse_psv(path: str) -> Iterable[dict[str, str]]:
    """Yield {col: value} dicts for each data row in the psql .psv output.

    The .psv file produced by `psql ... > out.psv` with the
    `\\pset format unaligned` + `\\pset fieldsep '|'` shape contains:
      * a leading line with the pipe-delimited header
      * one data row per flipped invite
      * a trailing '(N rows)' summary line (skipped)
      * \\echo lines from the SQL block (these go to stderr in unaligned
        mode but we still skip any line that doesn't tokenize to exactly
        len(PSQL_HEADER_COLS) fields).
    """
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.reader(f, delimiter="|")
        header_seen = False
        for row in reader:
            if not row or all(not cell.strip() for cell in row):
                continue
            if len(row) != len(PSQL_HEADER_COLS):
                # Skip \echo banners, '(N rows)' tails, blanks.
                continue
            if not header_seen and tuple(c.strip() for c in row) == PSQL_HEADER_COLS:
                header_seen = True
                continue
            if not header_seen:
                # Unexpected — bail rather than silently mis-record.
                raise RuntimeError(
                    f"Expected header row {PSQL_HEADER_COLS} first; got {row}"
                )
            yield {col: cell.strip() for col, cell in zip(PSQL_HEADER_COLS, row)}


def _already_recorded(db, invite_id: str) -> bool:
    """Idempotency check: has the audit-row for this invite already landed?"""
    stmt = select(AdminAuditLog).where(
        AdminAuditLog.action == ACTION_INVITE_REVOKED,
        AdminAuditLog.resource_type == RESOURCE_USER_INVITE,
        AdminAuditLog.resource_natural_id == invite_id,
        AdminAuditLog.note.like(f"%{NOTE_REASON}%"),
    )
    return db.execute(stmt).first() is not None


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2

    psv_path = sys.argv[1]
    if not os.path.isfile(psv_path):
        print(f"input file not found: {psv_path}", file=sys.stderr)
        return 2

    db = SessionLocal()
    repo = AdminAuditRepository(db)
    ctx = AuditContext.system(label="arc3_token_backlog_audit")

    seen = 0
    recorded = 0
    skipped_existing = 0

    try:
        for row in _parse_psv(psv_path):
            seen += 1
            invite_id = row["invite_id"]
            if _already_recorded(db, invite_id):
                skipped_existing += 1
                continue

            repo.record(
                ctx=ctx,
                tenant_id=row["tenant_id"],
                action=ACTION_INVITE_REVOKED,
                resource_type=RESOURCE_USER_INVITE,
                resource_pk=None,
                resource_natural_id=invite_id,
                domain_id=row["domain_id"] or None,
                after={
                    "invited_email": row["invited_email"],
                    "token_jti": row["token_jti"],
                    "flipped_from": "pending",
                    "flipped_to": "revoked",
                    "closure_drift": CLOSURE_DRIFT,
                    "closing_tag": CLOSING_TAG,
                    "trigger": "cloudwatch_token_backlog_audit",
                    "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
                },
                note=(
                    f"Arc 3 token-backlog CloudWatch audit "
                    f"({NOTE_REASON}): invite token_jti was emitted "
                    f"to /ecs/luciel-backend in the discovery window "
                    f"2026-05-13 -> 2026-05-20 and is now revoked. "
                    f"Closure of {CLOSURE_DRIFT}."
                ),
                autocommit=False,
            )
            recorded += 1

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    print(
        f"seen={seen}  recorded={recorded}  "
        f"skipped_already_recorded={skipped_existing}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
