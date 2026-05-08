"""step28_p3e2_audit_log_hash_chain

Step 28 Phase 3, Commit 6 -- P3-E.2 (Pillar 23).

Adds a per-row hash chain to admin_audit_logs so any tampering with
historical rows is detectable even by a DB superuser:

  row_hash       CHAR(64)  -- sha256 of canonical content + prev_row_hash
  prev_row_hash  CHAR(64)  -- the row_hash of the row with the next-lower id;
                              genesis row uses '0' * 64.

Both columns are NULLABLE in this migration. Rationale: during the
deploy window, the OLD container image (which knows nothing about the
chain) and the NEW image briefly run side-by-side. Old image inserts
would otherwise violate a NOT NULL constraint and crash audit emission
mid-mutation, which would in turn roll back the mutation itself
(Invariant 4: audit-before-commit). The columns are populated by a
SQLAlchemy session event registered in app.repositories.audit_chain,
and the verification pillar (Pillar 23) walks the chain and FAILs if
any row has NULL hashes after deploy completion. We can flip to NOT
NULL in a future cosmetic migration once we have months of clean prod
data and the chain has zero gaps.

Backfill strategy:
- Walk admin_audit_logs in id ASC order.
- For each row, compute the canonical content hash (see
  app.repositories.audit_chain.canonical_row_hash for the exact
  recipe; this migration inlines an equivalent SQL/Python form to
  avoid a hard dependency on app code at migration time).
- Genesis row's prev_row_hash = '0' * 64.
- UPDATE the row in place with both columns.

UNIQUE INDEX on row_hash:
- Adds a database-level guard against the (astronomically unlikely
  but not impossible) case of two rows hashing to the same value,
  which would indicate either a SHA-256 collision or, far more
  plausibly, a bug in the canonicalisation step.

No GRANT changes:
- luciel_worker already has SELECT, INSERT on admin_audit_logs from
  migration f392a842f885. Column-level grants in Postgres inherit
  from the table grant unless explicitly restricted, so the worker
  can write the new columns without further DDL. UPDATE is still
  forbidden, which means the chain remains append-only -- a
  compromised worker cannot rewrite a historical row's hash.

Drift register: closes P3-E.2 (Pillar 23 hash chain) Phase 3 OPEN item.
Cross-ref: Pillar 23 in app/verification/tests/pillar_23_audit_log_hash_chain.py.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "8ddf0be96f44"
down_revision = "f392a842f885"
branch_labels = None
depends_on = None


GENESIS_PREV_HASH = "0" * 64


# Field names included in the canonical content for hashing. Order is
# irrelevant because we use sort_keys=True; what matters is that the
# SET of fields here is identical between this migration's backfill
# code and app.repositories.audit_chain.canonical_row_hash. Drift
# between the two would invalidate the chain on the first new insert
# after deploy.
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

    Inlined here (vs. importing from app.repositories.audit_chain) so
    that a migration run in a fresh checkout never has a hidden coupling
    to live application code -- migrations should be self-contained.
    The companion function in app.repositories.audit_chain MUST stay in
    lockstep with this; Pillar 23 catches any drift on the next verify.
    """
    serialisable = {}
    for k in _CHAIN_FIELDS:
        v = row_dict.get(k)
        if isinstance(v, datetime):
            # Normalise to UTC ISO 8601 with explicit offset; the
            # database stores TIMESTAMP WITH TIME ZONE so the value
            # we read back is already tz-aware. Defensive coerce just
            # in case someone backfills naive timestamps.
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
    # 1. Add columns NULLABLE. Both columns intentionally lack a
    #    server_default because backfill sets them explicitly and new
    #    rows get them via the session event. A server_default would
    #    silently mask bugs where the event failed to fire.
    op.add_column(
        "admin_audit_logs",
        sa.Column("row_hash", sa.CHAR(64), nullable=True),
    )
    op.add_column(
        "admin_audit_logs",
        sa.Column("prev_row_hash", sa.CHAR(64), nullable=True),
    )

    # 2. Backfill in id ASC order.
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, tenant_id, domain_id, agent_id, "
            "luciel_instance_id, actor_key_prefix, actor_permissions, "
            "actor_label, action, resource_type, resource_pk, "
            "resource_natural_id, before_json, after_json, note, "
            "created_at "
            "FROM admin_audit_logs "
            "ORDER BY id ASC"
        )
    ).mappings().all()

    prev_hash = GENESIS_PREV_HASH
    for r in rows:
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

    # 3. UNIQUE INDEX on row_hash. Created AFTER backfill so that
    #    duplicate detection runs against the populated values. NULLs
    #    are not considered equal by Postgres unique indexes (default
    #    behaviour pre-PG15; from PG15 we'd need NULLS NOT DISTINCT to
    #    treat them as equal, which we DON'T want -- multiple NULLs
    #    are fine during deploy windows).
    op.create_index(
        "ux_admin_audit_logs_row_hash",
        "admin_audit_logs",
        ["row_hash"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ux_admin_audit_logs_row_hash",
        table_name="admin_audit_logs",
    )
    op.drop_column("admin_audit_logs", "prev_row_hash")
    op.drop_column("admin_audit_logs", "row_hash")
