"""Pillar 23 - Audit-log hash chain integrity (P3-E.2).

Step 28 Phase 3 - C6. Resolves PHASE_3_COMPLIANCE_BACKLOG P3-E.2.

# Why this pillar exists

Pillar 22 proves the application's worker DSN cannot UPDATE or DELETE
admin_audit_logs at the engine layer. That closes one tampering path.

It does NOT close the path of a database superuser (or anyone with
the postgres-role credentials) who can UPDATE/DELETE freely. PIPEDA
P5 / SOC 2 CC7.2 expect the audit log to be tamper-evident even
against operators with full DB access.

Migration 8ddf0be96f44 adds two columns to every admin_audit_logs row:

  row_hash       = sha256(canonical_content + prev_row_hash)
  prev_row_hash  = the row_hash of the row with the next-lower id;
                   genesis row uses '0' * 64.

The chain links rows in id ASC order. Tampering with any historical
row's content invalidates that row's row_hash; any rows chained off
it then also fail to recompute, surfacing the tamper point at the
earliest divergence.

# What this pillar checks

  1. STRUCTURAL: every row that has hashes uses CHAR(64) hex strings
     (regex-validated) and prev_row_hash references either GENESIS
     ('0'*64) or the row_hash of an earlier row. row_hash MUST be
     unique across the whole table -- the database guards this with
     UNIQUE INDEX ux_admin_audit_logs_row_hash; if Postgres ever
     allows a duplicate, the index is broken and we want to know.

  2. RECOMPUTATION: walk rows in id ASC order; for each row, compute
     canonical_row_hash(row, prev_hash) where prev_hash is either
     GENESIS (genesis row) or the previously-walked row's row_hash.
     Compare against the stored row_hash. Any mismatch -> FAIL with
     the earliest offending id and a diff hint.

  3. DEPLOY-WINDOW NULL TOLERANCE: the migration backfilled all rows
     present at migration time; any subsequent NULL row_hash was
     inserted by an old container during a rolling deploy. We allow
     a contiguous trailing run of NULL rows (no newer non-NULL row
     appears after them) -- but a NULL gap with non-NULL rows on
     both sides indicates code-path drift (some code path is
     bypassing the session event) and FAILs hard.

     STEP 29.y CLUSTER 8 (C-3): once the post-Cluster-3 migration
     drops admin_audit_logs.row_hash to NOT NULL, the deploy-window
     tolerance is no longer compatible with the column constraint --
     the DB itself rejects new NULLs, so the only NULLs that can
     exist are pre-migration leftovers. P23 now probes the live
     column nullability and, when it sees row_hash IS NOT NULL at
     the schema level, switches to STRICT mode: zero trailing NULLs,
     zero unbackfilled prefix. This makes the pillar self-adapting
     across the Cluster-3 deploy: lenient before, strict after, with
     no manual flag flip.

  4. CHAIN HEAD: the genesis row (lowest id with a non-NULL row_hash)
     MUST have prev_row_hash = '0'*64. Anything else means the chain
     was forked or an out-of-order backfill occurred.

# Read-only

This pillar performs only SELECTs. It never mutates a row. Safe to
run pre- or post-teardown. Worker DSN has SELECT, so the strict
branch runs in production.
"""

from __future__ import annotations

import os
import re

from sqlalchemy import create_engine, text

from app.repositories.audit_chain import (
    GENESIS_PREV_HASH,
    canonical_row_hash,
)
from app.verification.fixtures import RunState
from app.verification.runner import Pillar


_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


_ROWS_SQL = """
SELECT
  id,
  tenant_id,
  domain_id,
  agent_id,
  luciel_instance_id,
  actor_key_prefix,
  actor_permissions,
  actor_label,
  action,
  resource_type,
  resource_pk,
  resource_natural_id,
  before_json,
  after_json,
  note,
  created_at,
  row_hash,
  prev_row_hash
FROM admin_audit_logs
ORDER BY id ASC
"""


# Sanity guard: don't try to walk the whole table if it has grown
# beyond a reasonable size. The verify suite is meant to be O(seconds);
# at ~10k rows the recompute is still under a second on Aurora's
# typical IO. If we ever exceed this, raise the cap deliberately or
# add a sliding-window mode.
_MAX_ROWS = 100_000


class AuditLogHashChainPillar(Pillar):
    number = 23
    name = "Audit-log hash chain integrity (P3-E.2)"

    def run(self, state: RunState) -> str:
        db_url = os.environ.get("DATABASE_URL") or self._load_database_url_from_dotenv()
        if not db_url:
            raise AssertionError(
                "DATABASE_URL not found in environment nor in project .env file. "
                "Pillar 23 needs a direct DB connection to walk the chain."
            )

        engine = create_engine(db_url)
        try:
            return self._run_with_engine(engine)
        finally:
            engine.dispose()

    def _run_with_engine(self, engine) -> str:
        with engine.connect() as conn:
            # Probe the live column-nullability constraint. After
            # Cluster 3 lands, admin_audit_logs.row_hash is NOT NULL
            # and any leftover NULL is a chain hole, not a deploy-
            # window remnant. Pre-Cluster-3 the column is nullable
            # and we keep the legacy lenient mode.
            row_hash_strict_not_null = self._probe_row_hash_not_null(conn)
            rows = conn.execute(text(_ROWS_SQL)).mappings().all()

        total = len(rows)
        if total == 0:
            # Empty table -- vacuously valid. Pillars 1..22 currently
            # always produce audit rows so we'd expect this branch
            # only on a freshly migrated empty DB.
            return "audit chain: 0 rows in admin_audit_logs (vacuously valid)"

        if total > _MAX_ROWS:
            raise AssertionError(
                f"audit chain: {total} rows exceeds the verify-time cap "
                f"of {_MAX_ROWS}. Either prune historical rows (NEVER -- "
                f"audit logs are append-only) or raise the cap "
                f"deliberately in pillar_23 with a recap entry."
            )

        seen_hashes: set[str] = set()
        prev_hash: str = GENESIS_PREV_HASH
        chained_rows = 0
        trailing_null_rows = 0

        # Walk in id ASC, find the first row that has a non-NULL hash
        # (the chain "head"). Rows before it are pre-chain rows from
        # before migration 8ddf0be96f44 backfilled; the migration
        # backfilled all of them, so in practice the head is always
        # id=1 in production. We tolerate a different head only if all
        # rows before it are NULL (unbackfilled) and we surface that
        # as a soft warning, not a hard fail.
        head_idx = None
        for idx, r in enumerate(rows):
            if r["row_hash"] is not None:
                head_idx = idx
                break

        if head_idx is None:
            # Every single row has NULL row_hash. That means the
            # migration backfill never ran OR the ORM event is not
            # wired up anywhere. Either is a critical drift.
            raise AssertionError(
                f"audit chain: ALL {total} rows have NULL row_hash. "
                f"Migration 8ddf0be96f44 did not backfill, or the "
                f"before_flush event is not installed in the running "
                f"app/worker images. Verify install_audit_chain_event "
                f"is called from app.db.session (Step 29.y C25 install "
                f"location), with defense-in-depth calls also in "
                f"app.main and app.worker.celery_app."
            )

        # Soft note (not fail) for unbackfilled prefix rows in lenient
        # mode; HARD FAIL in strict mode (post-Cluster-3 NOT NULL).
        unbackfilled_prefix = head_idx
        soft_notes: list[str] = []
        if unbackfilled_prefix > 0:
            msg = (
                f"{unbackfilled_prefix} row(s) before chain head have "
                f"NULL row_hash (unexpected: migration 8ddf0be96f44 "
                f"should have backfilled all rows). First chained "
                f"id={rows[head_idx]['id']}."
            )
            if row_hash_strict_not_null:
                # Schema says NOT NULL but rows exist anyway: that
                # would mean a bug in the Cluster-3 migration's
                # backfill step OR a NULL row landed via a path that
                # bypassed the constraint (impossible at the engine
                # layer, but worth surfacing if it ever happens).
                raise AssertionError(
                    "audit chain (strict mode): " + msg
                    + " Schema is NOT NULL post-Cluster-3 yet NULL prefix "
                    "rows exist; investigate the Cluster-3 backfill."
                )
            soft_notes.append(msg)

        # Validate chain head's prev_row_hash points at GENESIS.
        head_row = rows[head_idx]
        if head_row["prev_row_hash"] != GENESIS_PREV_HASH:
            raise AssertionError(
                f"audit chain head (id={head_row['id']}) has "
                f"prev_row_hash={head_row['prev_row_hash']!r}; "
                f"expected GENESIS={GENESIS_PREV_HASH!r}. The chain "
                f"either forked or was backfilled out of order."
            )

        # Walk from head_idx onward.
        for r in rows[head_idx:]:
            row_hash = r["row_hash"]
            row_prev = r["prev_row_hash"]

            # Trailing NULL run is tolerated (deploy-window remnant);
            # NULL gap with non-NULL after is NOT.
            if row_hash is None:
                trailing_null_rows += 1
                continue
            if trailing_null_rows > 0:
                # We saw NULL rows and now see a non-NULL row again.
                # That's a gap -> FAIL.
                raise AssertionError(
                    f"audit chain: NULL row_hash gap detected. "
                    f"Encountered {trailing_null_rows} NULL row(s) "
                    f"before id={r['id']} which has row_hash={row_hash!r}. "
                    f"This indicates a code path inserted audit rows "
                    f"WITHOUT the before_flush event firing, which "
                    f"means the chain has a hole. Investigate which "
                    f"path bypassed install_audit_chain_event."
                )

            # Hex-format guard.
            if not _HEX64_RE.match(row_hash):
                raise AssertionError(
                    f"audit chain: row id={r['id']} has malformed "
                    f"row_hash={row_hash!r} (expected 64 hex chars)."
                )
            if row_prev is not None and not _HEX64_RE.match(row_prev):
                raise AssertionError(
                    f"audit chain: row id={r['id']} has malformed "
                    f"prev_row_hash={row_prev!r} (expected 64 hex chars)."
                )

            # Uniqueness guard (DB has UNIQUE INDEX, but verify here
            # too to catch a corrupt index).
            if row_hash in seen_hashes:
                raise AssertionError(
                    f"audit chain: duplicate row_hash {row_hash!r} "
                    f"first observed before id={r['id']}. UNIQUE INDEX "
                    f"ux_admin_audit_logs_row_hash should have prevented "
                    f"this; the index may be corrupt."
                )
            seen_hashes.add(row_hash)

            # Linkage guard: prev_row_hash must equal the previous
            # walked row's row_hash (or GENESIS for the first one).
            if row_prev != prev_hash:
                raise AssertionError(
                    f"audit chain: row id={r['id']} has "
                    f"prev_row_hash={row_prev!r} but the prior row's "
                    f"row_hash was {prev_hash!r}. Chain forked or rows "
                    f"were inserted out of id order."
                )

            # Recompute hash and compare. This is THE tamper check.
            row_dict = dict(r)
            recomputed = canonical_row_hash(row_dict, prev_hash)
            if recomputed != row_hash:
                raise AssertionError(
                    f"audit chain: TAMPER DETECTED at row id={r['id']}. "
                    f"Stored row_hash={row_hash!r}; recomputed from "
                    f"current row contents={recomputed!r}. Either the "
                    f"row was modified post-insert (UPDATE outside the "
                    f"app contract) OR the canonical_row_hash function "
                    f"in app.repositories.audit_chain has drifted from "
                    f"the migration 8ddf0be96f44 backfill recipe."
                )

            prev_hash = row_hash
            chained_rows += 1

        # Strict NULL gate: post-Cluster-3 the column is NOT NULL, so
        # any trailing NULL is a chain hole, not a deploy-window remnant.
        if row_hash_strict_not_null and trailing_null_rows > 0:
            raise AssertionError(
                f"audit chain (strict mode): {trailing_null_rows} trailing "
                f"NULL row_hash row(s) but admin_audit_logs.row_hash is "
                f"NOT NULL at the schema layer. This is structurally "
                f"impossible -- if this fires, either the schema probe "
                f"misread the constraint or a NULL slipped past the DB. "
                f"Investigate before trusting the chain."
            )

        mode_token = "strict" if row_hash_strict_not_null else "lenient"
        verdict = (
            f"audit chain ({mode_token} mode): {chained_rows} row(s) "
            f"verified clean from id={head_row['id']} to id={rows[-1]['id']}; "
            f"{trailing_null_rows} trailing NULL "
            f"({'forbidden by NOT NULL' if row_hash_strict_not_null else 'deploy-window tolerance'}); "
            f"chain head prev_row_hash=GENESIS"
        )
        if soft_notes:
            verdict = verdict + " ; soft notes: " + " | ".join(soft_notes)
        return verdict

    @staticmethod
    def _probe_row_hash_not_null(conn) -> bool:
        """Return True iff admin_audit_logs.row_hash is NOT NULL at the schema layer.

        The Cluster-3 migration drops the column to NOT NULL after
        backfilling any pre-existing NULLs. Before that migration runs,
        the column is nullable and P23 stays in lenient mode (legacy
        deploy-window tolerance). After it runs, P23 switches to strict
        mode automatically. No flag flip needed.

        Defensive: any exception (DB driver hiccup, permission edge
        case) collapses to False (lenient) so a probe failure cannot
        flip the gate state inadvertently.
        """
        try:
            row = conn.execute(text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = 'admin_audit_logs' "
                "AND column_name = 'row_hash'"
            )).first()
        except Exception:
            return False
        if row is None:
            return False
        return str(row[0]).upper() == "NO"

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
                            if (val.startswith('"') and val.endswith('"')) or (
                                val.startswith("'") and val.endswith("'")
                            ):
                                val = val[1:-1]
                            return val
                except Exception:
                    continue
        return None


PILLAR = AuditLogHashChainPillar()
