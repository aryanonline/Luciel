"""Pillar 9 - Migration integrity (gap-7 fix).

Landed suite's pillar 9 did a one-directional table-name diff:
  missing = model_tables - db_tables
  assert not missing

That misses two real regression classes:
  1. Columns declared on the SQLAlchemy model but missing from the DB
     (exactly the bug 028d27a repaired -- pgvector embedding column
     was silently dropped by autogenerate; one-directional table diff
     couldn't see it because the table itself still existed).
  2. DB-only tables/columns that no model claims (orphan schema,
     silent drift).

Redo does a bidirectional table diff PLUS a per-column comparison for
every shared table. Type-match is not asserted (pgvector Vector vs
reflected UserDefinedType would false-positive); only column NAMES are
required to match.

Runs in a subprocess so SQLAlchemy metadata is a clean slate (avoids
'table already defined in Base.metadata' errors on re-runs) and so any
connection-pool state from the suite's HTTP calls does not leak in.

Read-only. No DDL, no DML. Safe to run post-teardown.
"""

from __future__ import annotations

import os
import subprocess
import sys

from app.verification.fixtures import RunState
from app.verification.runner import Pillar



_CHECK_SCRIPT = r"""
import os, sys, json
from sqlalchemy import create_engine, inspect
from app.models.base import Base
import app.models  # force model registration

db_url = os.environ.get("DATABASE_URL")
if not db_url:
    print("FAIL:subprocess_missing_database_url", flush=True)
    sys.exit(2)

engine = create_engine(db_url)
insp = inspect(engine)

db_tables = set(insp.get_table_names())
model_tables = set(Base.metadata.tables.keys())

# Legacy tables that Step 24.5 kept read-only for one release cycle:
# they exist in both model and DB, so they pass the bidirectional check,
# but if they ever fall out of model registration it's expected and
# flagged (see report).
# Known-infrastructure tables that legitimately exist in the DB but have
# no SQLAlchemy model. Keep this list tight -- any NEW orphan should
# still fire.
DB_ONLY_WHITELIST = {
    "alembic_version",  # Alembic migration head tracker
}

shared = db_tables & model_tables
only_model = sorted(model_tables - db_tables)
only_db = sorted((db_tables - model_tables) - DB_ONLY_WHITELIST)

column_mismatches = {}
for tname in sorted(shared):
    model_cols = set(c.name for c in Base.metadata.tables[tname].columns)
    db_cols = set(c["name"] for c in insp.get_columns(tname))
    missing_in_db = sorted(model_cols - db_cols)
    missing_in_model = sorted(db_cols - model_cols)
    if missing_in_db or missing_in_model:
        column_mismatches[tname] = {
            "missing_in_db": missing_in_db,
            "missing_in_model": missing_in_model,
        }

report = {
    "db_table_count": len(db_tables),
    "model_table_count": len(model_tables),
    "shared_count": len(shared),
    "only_in_model": only_model,
    "only_in_db": only_db,
    "db_only_whitelisted": sorted(DB_ONLY_WHITELIST & db_tables),
    "column_mismatches": column_mismatches,
}

ok = (not only_model) and (not only_db) and (not column_mismatches)
print(("OK:" if ok else "FAIL:") + json.dumps(report), flush=True)
sys.exit(0 if ok else 1)
"""


class MigrationIntegrityPillar(Pillar):
    number = 9
    name = "migration integrity (bidirectional + per-column)"

    def run(self, state: RunState) -> str:
        db_url = os.environ.get("DATABASE_URL") or self._load_database_url_from_dotenv()
        if not db_url:
            raise AssertionError(
                "DATABASE_URL not found in environment nor in project .env file. "
                "Either export it or ensure .env is readable from the project root."
            )

        env = os.environ.copy()
        env["DATABASE_URL"] = db_url

        proc = subprocess.run(
            [sys.executable, "-c", _CHECK_SCRIPT],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()

        if not out:
            raise AssertionError(
                f"migration-integrity subprocess produced no output. "
                f"returncode={proc.returncode} stderr={err[:500]}"
            )

        if out.startswith("OK:"):
            import json as _json
            rpt = _json.loads(out[3:])
            return (
                f"db_tables={rpt['db_table_count']} "
                f"model_tables={rpt['model_table_count']} "
                f"shared={rpt['shared_count']} "
                f"no drift (bidirectional + per-column clean)"
            )
        if out.startswith("FAIL:"):
            raise AssertionError(
                f"migration integrity drift detected. body={out[5:][:1500]}"
            )
        raise AssertionError(
            f"migration-integrity subprocess unexpected output: {out[:500]} "
            f"stderr={err[:500]}"
        )

    @staticmethod
    def _load_database_url_from_dotenv() -> str | None:
        """Walk up from CWD looking for a .env file; return DATABASE_URL if present."""
        from pathlib import Path
        here = Path.cwd().resolve()
        for candidate_dir in (here, *here.parents):
            env_path = candidate_dir / ".env"
            if env_path.is_file():
                try:
                    for raw in env_path.read_text(encoding="utf-8").splitlines():
                        line = raw.strip()
                        if not line or line.startswith("#"):
                            continue
                        if line.startswith("DATABASE_URL=") and "://" in line:
                            val = line.split("=", 1)[1].strip()
                            # Strip surrounding quotes if present
                            if (val.startswith('"') and val.endswith('"')) or (
                                val.startswith("'") and val.endswith("'")
                            ):
                                val = val[1:-1]
                            return val
                except Exception:
                    continue
        return None


PILLAR = MigrationIntegrityPillar()