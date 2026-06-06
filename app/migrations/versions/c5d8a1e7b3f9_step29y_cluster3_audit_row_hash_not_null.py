"""Step 29.y Cluster 3 (D-8): admin_audit_logs.row_hash NOT NULL.

Revision ID: c5d8a1e7b3f9
Revises: a1f29c7e4b08
Create Date: 2026-05-07

Why this migration exists
-------------------------

findings_phase1d.md D-8 documents a deploy-window NULL-row-hash
chain hole. The 8ddf0be96f44 migration added row_hash + prev_row_hash
as NULLABLE columns and backfilled every row present at that time;
new rows get populated by the SQLAlchemy `before_flush` event hook
in `app.repositories.audit_chain`. Two failure modes leave NULL
rows behind:

  1. A worker image rolled out without the event hook installed
     (e.g. an emergency revert that misses the registration call)
     can INSERT into admin_audit_logs with row_hash=NULL.
  2. Any direct DB write that bypasses the SQLAlchemy session
     (psql, raw psycopg, a different ORM) skips the hook entirely.

Pre-Cluster-3, Pillar 23 tolerated a contiguous trailing run of
NULLs as a deploy-window remnant and only soft-warned. Cluster 8
already gave Pillar 23 tri-state (DEGRADED/FAIL); D-8's fix is the
DDL backstop: make the DB itself reject row_hash IS NULL so the
only way to land an unhashed row is a privileged manual override.

When the schema flips to NOT NULL, P23's STRICT mode kicks in
(see app/verification/tests/pillar_23_audit_log_hash_chain.py
lines 142-211 -- the pillar probes column nullability and switches
to zero-tolerance once the schema is hardened).

What this migration does
------------------------

  upgrade():
    1. Backfill any remaining NULL row_hash / prev_row_hash rows
       in id ASC order using the canonical hash function inlined
       below (kept in lockstep with the original 8ddf0be96f44
       backfill and with app.repositories.audit_chain).
    2. ALTER COLUMN row_hash SET NOT NULL.
    3. ALTER COLUMN prev_row_hash SET NOT NULL.

  downgrade():
    Drop NOT NULL constraints. Backfilled rows stay populated --
    we never NULL them back out, because Pillar 23's chain
    verifier walks every row and a NULL would invalidate the
    forensic record. Downgrade is provided for migration symmetry
    only; in practice we don't roll backwards on chain DDL.

Why the canonical hash is inlined
---------------------------------

A migration must be self-contained. Importing from
app.repositories.audit_chain would couple this migration to a
specific app revision, so a future fresh checkout where the
audit_chain field set has drifted would replay this migration
with the wrong fields and produce a chain mismatch the next time
Pillar 23 ran. Inlining freezes the algorithm at the time of this
migration. The companion `_CHAIN_FIELDS` test added in Cluster 3
(D-9 fix) guarantees app-side and DB-side stay aligned going
forward.

Idempotency
-----------

The backfill runs only on rows where row_hash IS NULL. Re-running
the upgrade on a database where every row already has a hash is a
no-op SELECT followed by two ALTER COLUMN SET NOT NULLs (which are
idempotent on already-NOT-NULL columns).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import sqlalchemy as sa

from alembic import op


# Revision identifiers, used by Alembic.
revision = "c5d8a1e7b3f9"
down_revision = "a1f29c7e4b08"
branch_labels = None
depends_on = None


GENESIS_PREV_HASH = "0" * 64


# Frozen snapshot of the canonical chain field set at the time of
# this migration. Drift between this set and the live
# app.repositories.audit_chain._CHAIN_FIELDS is caught by the
# tests/integrity/test_audit_chain_fields_in_sync.py D-9 test
# added in this same cluster.
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


def _canonical_hash(row_dict: dict, prev_hash: str) -> str:
    """Compute sha256 of canonical content + prev_hash.

    Inlined (not imported from app.repositories.audit_chain) so the
    migration is self-contained against the application code state
    at migration time. Stays in lockstep with the 8ddf0be96f44
    backfill (same field tuple, same JSON canonicalisation).
    """
    serialisable: dict = {}
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


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Backfill any remaining NULL rows. We walk in id ASC and
    # chain off the previous row's row_hash. If the previous row
    # is itself NULL, we use whatever we just computed for it
    # (since we walk in order). If row 1 is NULL we chain off
    # GENESIS_PREV_HASH.
    rows = bind.execute(
        sa.text(
            "SELECT id, tenant_id, domain_id, agent_id, "
            "luciel_instance_id, actor_key_prefix, actor_permissions, "
            "actor_label, action, resource_type, resource_pk, "
            "resource_natural_id, before_json, after_json, note, "
            "created_at, row_hash, prev_row_hash "
            "FROM admin_audit_logs "
            "ORDER BY id ASC"
        )
    ).mappings().all()

    prev_hash = GENESIS_PREV_HASH
    for r in rows:
        if r["row_hash"] is not None and r["prev_row_hash"] is not None:
            # Already chained. Move forward without recomputing so
            # we don't disturb live forensic state.
            prev_hash = r["row_hash"]
            continue
        row_dict = dict(r)
        new_hash = _canonical_hash(row_dict, prev_hash)
        bind.execute(
            sa.text(
                "UPDATE admin_audit_logs "
                "SET row_hash = :rh, prev_row_hash = :ph "
                "WHERE id = :id"
            ),
            {"rh": new_hash, "ph": prev_hash, "id": r["id"]},
        )
        prev_hash = new_hash

    # 2. Flip both columns to NOT NULL. row_hash already carries
    # the UNIQUE INDEX from 8ddf0be96f44 so the only thing we add
    # here is the nullability constraint.
    op.alter_column(
        "admin_audit_logs",
        "row_hash",
        existing_type=sa.CHAR(64),
        nullable=False,
        existing_comment=(
            "sha256 hex of canonical_content + prev_row_hash; "
            "NULLABLE for deploy-window tolerance."
        ),
        comment=(
            "sha256 hex of canonical_content + prev_row_hash; "
            "NOT NULL post Step 29.y Cluster 3 (D-8). DB-side "
            "guarantee that no row lands without a chain entry."
        ),
    )
    op.alter_column(
        "admin_audit_logs",
        "prev_row_hash",
        existing_type=sa.CHAR(64),
        nullable=False,
        existing_comment=(
            "row_hash of the prior row in id ASC order; "
            "genesis = '0'*64."
        ),
        comment=(
            "row_hash of the prior row in id ASC order; "
            "genesis = '0'*64. NOT NULL post Step 29.y Cluster 3 "
            "(D-8)."
        ),
    )


def downgrade() -> None:
    # Symmetric DDL only. We deliberately do NOT NULL out the
    # backfilled values -- the chain stays valid. Operators who
    # truly want to wipe the chain would do so out-of-band.
    op.alter_column(
        "admin_audit_logs",
        "prev_row_hash",
        existing_type=sa.CHAR(64),
        nullable=True,
    )
    op.alter_column(
        "admin_audit_logs",
        "row_hash",
        existing_type=sa.CHAR(64),
        nullable=True,
    )
