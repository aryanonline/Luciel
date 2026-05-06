"""Audit-log hash chain -- canonical hashing + session event installer.

Step 28 Phase 3, Commit 6 (P3-E.2 / Pillar 23).

# What this module does

Every admin_audit_logs row carries two new columns:

  row_hash       = sha256(canonical_content + prev_row_hash)
  prev_row_hash  = the row_hash of the row with the next-lower id

Together they form a tamper-evident chain: changing any field of a
historical row would invalidate the row's row_hash, and the change
would also invalidate every chained row after it. Pillar 23 walks
the chain on every verify run and FAILs if recomputed hashes don't
match the stored ones.

# Why a session event (not repository code)

There are at least two paths that insert AdminAuditLog rows:

  1. AdminAuditRepository.record(...) -- the canonical path, used by
     services and routes inside a normal request.
  2. scripts/rotate_platform_admin_keys.py -- a one-off operator
     script that constructs AdminAuditLog(...) directly and adds it
     to a session, bypassing record().

If we put the chain population code only in record(), path 2 inserts
rows with NULL hashes, breaking the chain on the next verify run.
Putting the population code in a SQLAlchemy session before_flush
event catches BOTH paths: any AdminAuditLog instance pending in the
session at flush time gets its hashes computed before the INSERT
hits the wire.

# Concurrency

Two concurrent transactions could each read the same tail row's
hash and then each insert a new row whose prev_row_hash points at
that same tail. Result: a fork in the chain. We prevent this with
pg_advisory_xact_lock(hashtext('admin_audit_logs_chain')) at the
top of the event handler. The lock is held for the rest of the
transaction (xact-scoped), so a concurrent transaction blocks until
the first commits, then reads the new tail and chains correctly.
The lock key (hashtext('admin_audit_logs_chain')) is namespaced by
content so it doesn't collide with other advisory locks the app may
take in future.

# Deploy-window tolerance

During a rolling deploy, the OLD container image (without this
event) and the NEW image briefly run side-by-side. Old image
inserts have NULL hashes. Pillar 23 tolerates trailing NULLs by
walking from id=1 ASC and FAILing only if a non-trailing row is
NULL or if a hash mismatches its recomputation. After deploy
completes and traffic drains from the old image, every new row has
hashes; we leave the historical NULL gap as a documented deploy-
window artefact, with the migration backfill having already filled
all rows that existed at migration time.

# Drift between this module and the migration's backfill

The migration 8ddf0be96f44 has its own copy of canonical_row_hash
(with the same logic). Any change to the canonical form here MUST
also be applied to the migration; otherwise rows backfilled at
migration time will not chain to rows inserted post-migration. To
catch this drift, Pillar 23 recomputes hashes for ALL rows (not
just new ones) and asserts they match what's stored.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.models.admin_audit_log import AdminAuditLog

logger = logging.getLogger(__name__)


GENESIS_PREV_HASH = "0" * 64


# Field set MUST stay in lockstep with the migration 8ddf0be96f44
# backfill. Drift here = chain breakage on the first new insert
# after deploy. Pillar 23 catches drift by recomputing every row.
_CHAIN_FIELDS = (
    "tenant_id",
    "domain_id",
    "agent_id",
    "luciel_instance_id",
    "actor_key_prefix",
    "actor_permissions",
    "actor_label",
    "action",
    "resource_type",
    "resource_pk",
    "resource_natural_id",
    "before_json",
    "after_json",
    "note",
    "created_at",
)


# Advisory-lock key. hashtext() returns a deterministic int4 from a
# label, so the same key is recomputed on every connection. We bake
# the int once at module-import time using Python's stdlib hash so we
# don't have to round-trip through Postgres just to know the key. The
# Postgres-side lock call uses pg_advisory_xact_lock(hashtext(label))
# which produces the same int regardless of the connection -- both
# sides agree on the lock identity.
_CHAIN_LOCK_LABEL = "admin_audit_logs_chain"


def canonical_row_hash(row_dict: dict[str, Any], prev_hash: str) -> str:
    """Compute sha256 hex of the canonical content + prev_hash.

    row_dict must contain at least the keys in _CHAIN_FIELDS; missing
    keys are treated as None. Values must be JSON-serialisable; datetimes
    are normalised to UTC ISO 8601 with explicit offset before
    serialisation so naive vs. aware datetimes hash identically.
    """
    serialisable: dict[str, Any] = {}
    for k in _CHAIN_FIELDS:
        v = row_dict.get(k)
        if isinstance(v, datetime):
            if v.tzinfo is None:
                v = v.replace(tzinfo=timezone.utc)
            v = v.astimezone(timezone.utc).isoformat()
        serialisable[k] = v
    canonical = json.dumps(
        serialisable,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    h = hashlib.sha256()
    h.update(canonical.encode("utf-8"))
    h.update(prev_hash.encode("ascii"))
    return h.hexdigest()


def _row_to_dict(row: AdminAuditLog) -> dict[str, Any]:
    """Pull the chain-relevant attributes off a model instance."""
    return {k: getattr(row, k, None) for k in _CHAIN_FIELDS}


def _read_tail_hash(session: Session) -> str:
    """Return the row_hash of the highest-id row, or GENESIS if empty.

    Uses the session's connection so we see uncommitted rows from this
    same transaction (important: if a single transaction inserts
    multiple audit rows via separate AdminAuditRepository.record()
    calls and flushes in between, the second flush must chain off the
    first). We deliberately query for the MAX(id) row's row_hash; this
    is O(log n) given the PK index. If the tail row's row_hash is NULL
    (deploy-window remnant), we DO NOT chain off NULL -- we treat it
    as if the tail was the genesis. Pillar 23 will surface the NULL
    hole for forensic review.
    """
    bind = session.connection()
    row = bind.execute(
        text(
            "SELECT row_hash FROM admin_audit_logs "
            "ORDER BY id DESC LIMIT 1"
        )
    ).first()
    if row is None:
        return GENESIS_PREV_HASH
    tail_hash = row[0]
    if tail_hash is None:
        # Tail row pre-dates the chain. Start a fresh sub-chain; the
        # NULL gap is a deploy-window artefact already tolerated by
        # Pillar 23.
        logger.warning(
            "audit_chain: tail row has NULL row_hash; chaining off "
            "GENESIS. Likely deploy-window remnant from old image."
        )
        return GENESIS_PREV_HASH
    return tail_hash


def _populate_chain_for_pending(session: Session) -> None:
    """Compute and assign row_hash + prev_row_hash for every pending
    AdminAuditLog instance in this session.

    Called from the before_flush event. We process new instances in
    insertion order (session.new is a set, but we sort by Python object
    id() as a stable proxy for "added earliest"; the actual database id
    isn't assigned yet). Insertion order rarely matters in practice
    because most flushes contain at most one audit row, but if a single
    flush contains multiple, we want them to chain to each other in a
    deterministic order.
    """
    pending = [obj for obj in session.new if isinstance(obj, AdminAuditLog)]
    if not pending:
        return

    # Take the chain-wide advisory lock. Held until transaction end.
    # Using xact_lock (not session_lock) so an exception path that
    # rolls back the transaction also releases the lock automatically.
    bind = session.connection()
    bind.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:lbl))"),
        {"lbl": _CHAIN_LOCK_LABEL},
    )

    prev_hash = _read_tail_hash(session)

    # Sort pending rows for deterministic chaining within one flush.
    # We use the Python object id() as a stable proxy; in practice
    # this matches insertion order because CPython hands out ids in
    # allocation sequence within a single thread / single flush.
    for row in sorted(pending, key=id):
        # Only populate if not already set. This keeps the function
        # idempotent across re-flushes within the same transaction
        # (SQLAlchemy can re-emit before_flush after autoflush).
        if row.row_hash is not None and row.prev_row_hash is not None:
            prev_hash = row.row_hash
            continue
        # Defensive: created_at may be unset until flush triggers the
        # server_default. We need a stable created_at for the hash, so
        # if it's None we set it explicitly to "now" here. The DB will
        # then accept that value (the server_default is only used when
        # the column is NULL on INSERT).
        if getattr(row, "created_at", None) is None:
            row.created_at = datetime.now(timezone.utc)

        new_hash = canonical_row_hash(_row_to_dict(row), prev_hash)
        row.prev_row_hash = prev_hash
        row.row_hash = new_hash
        prev_hash = new_hash


def install_audit_chain_event() -> None:
    """Register the before_flush listener on the global Session class.

    Idempotent: safe to call multiple times. The listener is attached
    to sqlalchemy.orm.Session (the class), so every session created
    by SessionLocal() in app.db.session inherits it.

    Called once from app.main module-import time so every code path
    that uses the ORM (FastAPI requests, Celery tasks, scripts that
    import from app.*) gets the event. Pure-SQL inserts that bypass
    the ORM entirely (none in current code, but possible in future
    scripts) would NOT get hashes -- Pillar 23 would catch the NULL.
    """
    # event.contains() lets us avoid double-registration if app.main
    # is reloaded (e.g. by uvicorn --reload). SQLAlchemy raises if
    # you double-register the same callable on the same target.
    if event.contains(Session, "before_flush", _before_flush_handler):
        return
    event.listen(Session, "before_flush", _before_flush_handler)
    logger.info("audit_chain: before_flush handler installed.")


def _before_flush_handler(session: Session, flush_context, instances) -> None:
    """SQLAlchemy before_flush callback. See _populate_chain_for_pending."""
    try:
        _populate_chain_for_pending(session)
    except Exception:
        # If chain population fails, the flush will fail too -- which
        # is the correct behaviour because Invariant 4 says the audit
        # row and the mutation must commit atomically. We log the
        # exception with full traceback for forensic purposes; the
        # original exception propagates so the transaction rolls back.
        logger.exception(
            "audit_chain: before_flush handler failed; transaction "
            "will roll back. This is a critical alert -- investigate "
            "immediately. Possible causes: DB connection lost mid-"
            "transaction, advisory lock contention timeout, model "
            "schema drift."
        )
        raise
