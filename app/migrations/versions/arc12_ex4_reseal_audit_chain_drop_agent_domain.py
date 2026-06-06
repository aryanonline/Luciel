"""Arc 12 EX4 — RESEAL audit hash chain + drop admin_audit_logs.agent_id/domain_id.

Revision ID: arc12_ex4_reseal_audit_chain_drop_agent_domain
Revises: arc12_ex3_drop_scope_assignment_domain
Create Date: 2026-05-29

Context
-------
Per ``arc12_specs/02_EXCISION_PLAN.md`` § "EX4 FOUNDER DECISION
(RESEAL — LOCKED)" (2026-05-28), the legacy v1 ``domain_id`` and
``agent_id`` columns on ``admin_audit_logs`` must be physically
excised — and because both columns are in the canonical-content
field set for the row hash chain (``app.repositories.audit_chain
._CHAIN_FIELDS`` pre-EX4), a naive ``DROP COLUMN`` would leave every
historical row's stored ``row_hash`` impossible to recompute, breaking
Pillar 23's chain verifier on the next run.

The founder-locked choice is **Approach A — RESEAL** (NOT versioned-
hash-function): drop both columns from ``_CHAIN_FIELDS`` (done in the
companion code change in ``app/repositories/audit_chain.py``),
RECOMPUTE ``row_hash`` and ``prev_row_hash`` for every historical row
in id-ASC order under the new canonical field set starting from
GENESIS, THEN drop the two columns. This is a deliberate, one-time
integrity operation — the chain re-verifies end-to-end after the
migration but the original v1 hash values are gone forever.

What this migration does (in one transaction)
---------------------------------------------
1. Take the chain-wide advisory lock
   (``pg_advisory_xact_lock(hashtext('admin_audit_logs_chain'))``)
   so no concurrent audit writer can interleave during the reseal.
   This matches the discipline in ``audit_chain._populate_chain_for_pending``.
2. Walk ``admin_audit_logs`` in ``id ASC`` order. For each row, recompute
   ``row_hash = sha256(canonical_content + prev_row_hash)`` under the
   NEW _CHAIN_FIELDS set (which excludes ``agent_id`` / ``domain_id``).
   ``prev_row_hash`` chains off the previous (resealed) row's row_hash;
   the first row starts from ``GENESIS_PREV_HASH = '0'*64``. Both
   columns are UPDATEd in place.
3. Drop ``ix_admin_audit_logs_agent_id`` and ``ix_admin_audit_logs_domain_id``
   (BEFORE the column drops so PostgreSQL doesn't have to chase the
   cascade), then drop the ``agent_id`` and ``domain_id`` columns.
4. INSERT one final audit row with ``action='audit_chain_resealed'``
   (``ACTION_AUDIT_CHAIN_RESEALED``) under the NEW chain. The row
   chains off the freshly-resealed tail; its actor is
   ``actor_label='migration:arc12_ex4_reseal_audit_chain_drop_agent_domain'``.
   This makes the rewrite itself a traceable, chained audit event.

Why this is safe under append-only / immutability invariants
------------------------------------------------------------
* ``admin_audit_logs`` has RESTRICTIVE RLS policies (migration
  ``arc9_c6_2_admin_audit_immutability``) that block UPDATE/DELETE
  for every role except ``luciel_ops``. Postgres does NOT apply RLS
  to the table owner unless ``FORCE ROW LEVEL SECURITY`` is set —
  this migration runs as the schema owner, so the UPDATE/DELETE
  blocks do not apply at migration time. The
  ``arc10_lifecycle_subsystem`` migration relies on the same
  property (it backfills ``tier_at_write`` with a bulk UPDATE).
* The reseal is a SINGLE controlled operation, fully reversible
  ONLY in schema (the historical hash values are not preserved —
  this is the founder-accepted tradeoff). See ``downgrade()``.
* The advisory lock is held xact-scoped, so a concurrent INSERT
  via ``app.repositories.audit_chain._before_flush_handler`` blocks
  until this migration's transaction commits or rolls back, then
  reads the new (resealed) tail and chains correctly.

Recompute correctness
---------------------
The canonical hash function is INLINED here (a self-contained copy
of ``app.repositories.audit_chain.canonical_row_hash``) so the
migration does not depend on importing live application code at
migration time. The inlined ``_CHAIN_FIELDS`` set is kept in
lockstep with ``app.repositories.audit_chain._CHAIN_FIELDS`` post-
EX4; the ``tests/integrity/test_audit_chain_fields_in_sync.py``
invariant pins that the runtime field set matches the new model's
content columns. Pillar 23 recomputes hashes for ALL rows on every
verify run and will catch any drift between this migration and the
runtime.

Downgrade reversibility caveat
------------------------------
Downgrade RE-ADDs ``domain_id`` and ``agent_id`` as nullable
``String(100)`` with their original index names, matching the pre-
EX4 schema shape. It does NOT and CANNOT restore the original v1
``row_hash``/``prev_row_hash`` values — those were computed under
the v1 field set against column values the migration has just
dropped. Downgrade therefore leaves the chain in the resealed-but-
columns-re-added state; Pillar 23 will still verify the chain end-
to-end (under the v2 field set), but historical forensic queries
that filtered by ``agent_id``/``domain_id`` will see NULL on every
row. **This is a one-way integrity operation — the upgrade can be
"undone" only at the schema layer.** Per the EX4 founder decision,
this tradeoff is accepted.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "arc12_ex4_reseal_audit_chain_drop_agent_domain"
down_revision = "arc12_ex3_drop_scope_assignment_domain"
branch_labels = None
depends_on = None


# --------------------------------------------------------------------
# Self-contained reseal hashing (must match the post-EX4 runtime field
# set in app.repositories.audit_chain._CHAIN_FIELDS).
# --------------------------------------------------------------------
GENESIS_PREV_HASH = "0" * 64

# Advisory-lock label — MUST match audit_chain._CHAIN_LOCK_LABEL.
_CHAIN_LOCK_LABEL = "admin_audit_logs_chain"

# Post-EX4 canonical field set. KEEP IN LOCKSTEP with
# app.repositories.audit_chain._CHAIN_FIELDS. The
# test_audit_chain_fields_in_sync.py invariant pins this.
_CHAIN_FIELDS_V2 = (
    "admin_id",
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


def _canonical_hash_v2(row_dict: dict, prev_hash: str) -> str:
    """sha256 hex of canonical content (post-EX4 field set) + prev_hash.

    Mirrors ``app.repositories.audit_chain.canonical_row_hash`` exactly
    so the migration-time reseal produces values byte-identical to what
    the runtime verifier (Pillar 23) recomputes.
    """
    serialisable = {}
    for k in _CHAIN_FIELDS_V2:
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


# Columns we SELECT during the walk -- the v2 field set plus ``id`` so
# we know which row to UPDATE. ``before_json`` / ``after_json`` come
# back as Python dicts via the JSONB type binding, which is the same
# shape the runtime hash function operates on.
_SELECT_COLS_SQL = (
    "id, admin_id, luciel_instance_id, actor_key_prefix, "
    "actor_permissions, actor_label, action, resource_type, "
    "resource_pk, resource_natural_id, before_json, after_json, "
    "note, created_at"
)


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Advisory lock: blocks any concurrent runtime audit insert
    # path (which takes the same lock in _before_flush_handler) for
    # the duration of this transaction.
    bind.execute(
        sa.text("SELECT pg_advisory_xact_lock(hashtext(:lbl))"),
        {"lbl": _CHAIN_LOCK_LABEL},
    )

    # 2. RESEAL: walk in id ASC, recompute row_hash + prev_row_hash
    # under the v2 field set, UPDATE in place.
    rows = bind.execute(
        sa.text(
            f"SELECT {_SELECT_COLS_SQL} FROM admin_audit_logs "
            "ORDER BY id ASC"
        )
    ).mappings().all()

    prev_hash = GENESIS_PREV_HASH
    resealed = 0
    for r in rows:
        row_dict = dict(r)
        new_hash = _canonical_hash_v2(row_dict, prev_hash)
        bind.execute(
            sa.text(
                "UPDATE admin_audit_logs "
                "SET row_hash = :rh, prev_row_hash = :ph "
                "WHERE id = :id"
            ),
            {"rh": new_hash, "ph": prev_hash, "id": r["id"]},
        )
        prev_hash = new_hash
        resealed += 1

    # 3. Drop the indexes BEFORE the columns so PG doesn't have to
    # cascade-chase. The index names are stamped by the original
    # create migration ``c957a155c325_step_24_5_agent_luciel_split``.
    op.drop_index(
        "ix_admin_audit_logs_agent_id",
        table_name="admin_audit_logs",
    )
    op.drop_index(
        "ix_admin_audit_logs_domain_id",
        table_name="admin_audit_logs",
    )

    # 4. Drop the columns themselves.
    op.drop_column("admin_audit_logs", "agent_id")
    op.drop_column("admin_audit_logs", "domain_id")

    # 5. Emit a dedicated audit-of-the-reseal row under the NEW chain.
    # This row chains off ``prev_hash`` (the freshly-resealed tail), so
    # the rewrite itself is a traceable, chained audit event. We INSERT
    # directly (no ORM) because the chain event handler isn't installed
    # at migration time, and we already hold the advisory lock so no
    # race with runtime writers is possible.
    #
    # FK-safety (Arc 12 verify fix): the reseal row's actor is the
    # ``platform`` system-actor sentinel (``SYSTEM_ACTOR_TENANT`` in
    # app/repositories/admin_audit_repository.py). ``admin_audit_logs.admin_id``
    # is NOT NULL with a RESTRICT FK to ``admins.id``. No prior migration
    # seeds the ``platform`` admin (the arc5_b cutover only backfills
    # admins from pre-existing tenant_configs), so on a fresh database the
    # INSERT below would violate the FK. We idempotently seed the
    # ``platform`` sentinel admin first (no-op when it already exists in
    # prod). This makes the documented system-actor convention valid at
    # the schema level and lets ``alembic upgrade head`` succeed on a
    # fresh database (the CI verify contract).
    bind.execute(
        sa.text(
            "INSERT INTO admins (id, name, tier, tier_source, active) "
            "VALUES ('platform', 'Platform System Actor', 'enterprise', "
            "'manual', true) "
            "ON CONFLICT (id) DO NOTHING"
        )
    )

    now = datetime.now(timezone.utc)
    reseal_row = {
        "admin_id": "platform",  # SYSTEM_ACTOR_TENANT
        "luciel_instance_id": None,
        "actor_key_prefix": None,
        "actor_permissions": json.dumps(["system"]),
        "actor_label": (
            "migration:arc12_ex4_reseal_audit_chain_drop_agent_domain"
        ),
        "action": "audit_chain_resealed",  # ACTION_AUDIT_CHAIN_RESEALED
        "resource_type": "admin",  # RESOURCE_ADMIN — the chain belongs to the platform admin scope
        "resource_pk": None,
        "resource_natural_id": "admin_audit_logs",
        "before_json": None,
        "after_json": {
            "rationale": (
                "Arc 12 EX4 founder-directed reseal (LOCKED 2026-05-28): "
                "remove v1 ``domain_id`` and ``agent_id`` from the audit "
                "hash chain and physically drop the columns. The "
                "historical chain is rewritten under the new canonical "
                "field set; the rewrite itself is recorded here."
            ),
            "rows_resealed": resealed,
            "old_field_set_dropped": ["domain_id", "agent_id"],
            "new_field_set": list(_CHAIN_FIELDS_V2),
            "genesis_prev_hash": GENESIS_PREV_HASH,
            "migration": revision,
        },
        "note": "arc12-ex4-audit-chain-reseal",
        "created_at": now,
    }
    reseal_hash = _canonical_hash_v2(reseal_row, prev_hash)
    bind.execute(
        sa.text(
            "INSERT INTO admin_audit_logs ("
            "admin_id, luciel_instance_id, actor_key_prefix, "
            "actor_permissions, actor_label, action, resource_type, "
            "resource_pk, resource_natural_id, before_json, after_json, "
            "note, created_at, row_hash, prev_row_hash"
            ") VALUES ("
            ":admin_id, :luciel_instance_id, :actor_key_prefix, "
            ":actor_permissions, :actor_label, :action, :resource_type, "
            ":resource_pk, :resource_natural_id, "
            "CAST(:before_json AS JSONB), CAST(:after_json AS JSONB), "
            ":note, :created_at, :row_hash, :prev_row_hash"
            ")"
        ),
        {
            "admin_id": reseal_row["admin_id"],
            "luciel_instance_id": reseal_row["luciel_instance_id"],
            "actor_key_prefix": reseal_row["actor_key_prefix"],
            "actor_permissions": reseal_row["actor_permissions"],
            "actor_label": reseal_row["actor_label"],
            "action": reseal_row["action"],
            "resource_type": reseal_row["resource_type"],
            "resource_pk": reseal_row["resource_pk"],
            "resource_natural_id": reseal_row["resource_natural_id"],
            "before_json": (
                json.dumps(reseal_row["before_json"])
                if reseal_row["before_json"] is not None
                else None
            ),
            "after_json": json.dumps(reseal_row["after_json"]),
            "note": reseal_row["note"],
            "created_at": reseal_row["created_at"],
            "row_hash": reseal_hash,
            "prev_row_hash": prev_hash,
        },
    )


def downgrade() -> None:
    """Re-add the columns + indexes; DO NOT (and CANNOT) restore v1 hashes.

    Per the EX4 founder decision the reseal is a one-way integrity
    operation. Downgrade restores the SCHEMA shape (nullable columns
    + their original indexes) so a rollback can boot, but every
    historical row's ``agent_id``/``domain_id`` is NULL after
    downgrade (the original values were dropped). The chain itself
    remains valid under the post-EX4 (v2) field set; Pillar 23 will
    still verify end-to-end. Operators rolling back accept this
    forensic-data loss explicitly.
    """
    op.add_column(
        "admin_audit_logs",
        sa.Column("domain_id", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "admin_audit_logs",
        sa.Column("agent_id", sa.String(length=100), nullable=True),
    )
    op.create_index(
        "ix_admin_audit_logs_domain_id",
        "admin_audit_logs",
        ["domain_id"],
        unique=False,
    )
    op.create_index(
        "ix_admin_audit_logs_agent_id",
        "admin_audit_logs",
        ["agent_id"],
        unique=False,
    )
