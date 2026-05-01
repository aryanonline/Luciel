"""
prod_readonly_query.py - Pattern O read-only prod recon utility.

Codifies the Pattern O discipline: ad-hoc read-only queries against prod RDS
run via an ECS one-shot Fargate task in-VPC. The operator never connects a
laptop directly to prod RDS (Pattern N).

Security boundary: psycopg connection-level read_only = True, which causes
Postgres to issue SET TRANSACTION READ ONLY at transaction begin. Any DML/DDL
attempt inside that transaction is rejected by Postgres itself with
"cannot execute <op> in a read-only transaction". This is the only security
layer; do not add Python-side regex theater that misleads future readers
about where the real gate lives.

SQL provenance is preserved in three independent channels:
  1. CloudTrail run-task event captures containerOverrides.command (default
     90-day retention, extendable).
  2. CloudWatch Logs receive a "_query_input" line emitted at startup with
     the verbatim SQL plus its SHA256 (CloudWatch retention per log group).
  3. Every result emission and the final "_meta" line carry the SHA256 so
     correlation across logs is deterministic.

Usage (typically as ECS task command override):
    python scripts/prod_readonly_query.py --sql-literal "SELECT count(*) FROM memory_items"

Required env: DATABASE_URL (injected via ECS task-def secrets from SSM).
The URL may be either a SQLAlchemy-style "postgresql+psycopg://..." or a
plain "postgresql://..."; psycopg accepts the plain form, and the
"+psycopg" dialect prefix is stripped if present.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import decimal
import hashlib
import json
import os
import sys
import time
import uuid
from typing import Any

import psycopg
from psycopg.rows import dict_row


def _json_default(value: Any) -> Any:
    """Stringify types psycopg may return that json.dumps cannot handle."""
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, _dt.timedelta):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, set):
        return sorted(value, key=str)
    return str(value)


def _emit(obj: dict) -> None:
    """One JSON object per line to stdout. CloudWatch-Insights friendly."""
    sys.stdout.write(json.dumps(obj, default=_json_default, ensure_ascii=True))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _normalize_url(url: str) -> str:
    """
    Strip SQLAlchemy dialect prefix if present. psycopg.connect() expects
    a libpq-style URL ("postgresql://..."), not the SQLAlchemy
    "postgresql+psycopg://..." form.
    """
    if url.startswith("postgresql+psycopg://"):
        return "postgresql://" + url[len("postgresql+psycopg://"):]
    if url.startswith("postgresql+psycopg2://"):
        return "postgresql://" + url[len("postgresql+psycopg2://"):]
    return url


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a read-only SQL query against prod RDS via ECS one-shot task (Pattern O)."
    )
    parser.add_argument(
        "--sql-literal",
        required=True,
        help="The SQL query to execute. Will run inside a READ ONLY transaction.",
    )
    parser.add_argument(
        "--row-limit",
        type=int,
        default=1000,
        help="Hard cap on rows emitted to stdout (default: 1000). Belt-and-suspenders against accidental scans.",
    )
    args = parser.parse_args()

    sql = args.sql_literal
    query_sha = hashlib.sha256(sql.encode("utf-8")).hexdigest()

    # Audit channel 2: emit the verbatim SQL + SHA256 to stdout BEFORE
    # any database interaction. Captured by CloudWatch Logs regardless of
    # whether the query succeeds, fails, or the connection itself fails.
    _emit({
        "_query_input": sql,
        "_query_sha256": query_sha,
    })

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        _emit({"_error": "DATABASE_URL env var not set", "query_sha256": query_sha})
        return 2

    started = time.monotonic()
    row_count = 0
    truncated = False

    conninfo = _normalize_url(database_url)

    try:
        conn = psycopg.connect(conninfo, autocommit=False)
    except Exception as exc:
        _emit({"_error": "connect_failed", "detail": str(exc), "query_sha256": query_sha})
        return 3

    try:
        # Security boundary: the actual read-only gate.
        # Must be set before any transaction begins. psycopg translates this
        # to "SET TRANSACTION READ ONLY" on the next BEGIN.
        conn.read_only = True

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql)

            if cur.description is None:
                _emit({
                    "_meta": {
                        "row_count": 0,
                        "elapsed_ms": int((time.monotonic() - started) * 1000),
                        "query_sha256": query_sha,
                        "note": "statement returned no resultset",
                    }
                })
                conn.rollback()
                return 0

            for row in cur:
                if row_count >= args.row_limit:
                    truncated = True
                    break
                _emit(dict(row))
                row_count += 1

        conn.rollback()
    except psycopg.Error as exc:
        _emit({
            "_error": "query_failed",
            "sqlstate": getattr(exc, "sqlstate", None),
            "detail": str(exc).strip(),
            "query_sha256": query_sha,
        })
        try:
            conn.rollback()
        except Exception:
            pass
        return 4
    finally:
        try:
            conn.close()
        except Exception:
            pass

    _emit({
        "_meta": {
            "row_count": row_count,
            "truncated": truncated,
            "row_limit": args.row_limit,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "query_sha256": query_sha,
        }
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())