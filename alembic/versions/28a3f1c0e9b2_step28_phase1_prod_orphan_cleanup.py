"""step 28 phase 1 prod orphan memory_items cleanup

Revision ID: 28a3f1c0e9b2
Revises: 4e989b9392c0
Create Date: 2026-05-02

Step 28 -- Phase 1, Commit 11 (prerequisite to NOT NULL flip in 4e4c2c0fb572).

PURPOSE
-------
Hard-delete prod-only orphan memory_items rows that have actor_user_id IS NULL
so the subsequent NOT NULL flip (4e4c2c0fb572) can apply cleanly.

CONTEXT
-------
Migration 4e4c2c0fb572 (NOT NULL flip) explicitly states in its docstring:
    "Prod orphan count must be verified == 0 before this migration runs in
     prod (Phase 1 6-phase rollout runbook will gate on this)."

The cleanup precondition was originally planned as an operator-side
one-shot task (Pattern N override). It is expressed here as a migration
instead so the cleanup + flip run atomically in a single Alembic upgrade,
with the migration file itself serving as the durable audit record of
what was deleted and why.

Drift D-cleanup-via-migration-not-precondition-task-2026-05-02 logs this
deviation from 4e4c2c0fb572's stated design intent.

PROD STATE AT TIME OF AUTHORING (2026-05-02, Pattern O recon Q1)
----------------------------------------------------------------
- prod alembic head: 4e989b9392c0 (matches recap claim)
- memory_items rows with actor_user_id IS NULL: 1
- The single orphan belongs to tenant 'step27-syncverify-7064', a
  Step 27c-final sync verification residue tenant whose parent chain
  was deactivated but whose memory row predates the actor_user_id
  attribution path that landed in 4e989b9392c0.

PRECEDENT
---------
Local dev cleanup of equivalent orphans was logged as
admin_audit_logs.id = 2086 with:
    action='sweep' (pre-allow-list, now would use 'delete_hard')
    resource_type='memory_items'
    actor_label='step28-d11-orphan-sweep'

This migration mirrors that pattern using the canonical ACTION_DELETE_HARD
allow-list value and RESOURCE_MEMORY resource type (per
app/models/admin_audit_log.py). The audit row's after_json captures the
deleted rows' identifying tuple (id, tenant_id, agent_id,
luciel_instance_id) for forensic reconstruction.

DOWNGRADE
---------
The downgrade is a no-op: hard-deleted rows have no recoverable identity
and were not snapshot beyond their scope tuple. The downgrade exists only
to permit emergency rollback of the NOT NULL flip in 4e4c2c0fb572 (which
itself preserves nullability on downgrade); restoring the orphan rows is
not in scope.
"""
from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '28a3f1c0e9b2'
down_revision = '4e989b9392c0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # Snapshot the orphans for audit before deleting.
    rows = conn.execute(
        sa.text(
            "SELECT id, tenant_id, agent_id, luciel_instance_id, user_id "
            "FROM memory_items WHERE actor_user_id IS NULL "
            "ORDER BY id"
        )
    ).mappings().all()
    rows_list = [dict(r) for r in rows]

    # Idempotency: if zero orphans, no work and no audit row.
    # Re-running this migration after manual cleanup would otherwise
    # emit a misleading audit row claiming a sweep happened.
    if not rows_list:
        return

    # Hard-delete via single statement.
    conn.execute(
        sa.text(
            "DELETE FROM memory_items WHERE actor_user_id IS NULL"
        )
    )

    # Audit row -- mirrors the dev precedent (admin_audit_logs.id=2086)
    # using canonical ACTION_DELETE_HARD + RESOURCE_MEMORY constants.
    after_json = json.dumps(
        {
            "count": len(rows_list),
            "rows": rows_list,
            "reason": (
                "D-prod-orphan-memory-items-step27-syncverify-7064-2026-05-02"
            ),
            "sweep_label": "step28-phase1-prod-orphan-sweep",
            "trigger": "alembic_migration_28a3f1c0e9b2",
        },
        default=str,
    )
    note = (
        "Hard-delete prod orphan memory_items rows pre-NOT-NULL flip (D11). "
        "See migration 4e4c2c0fb572 docstring for design rationale and the "
        "matching dev precedent at admin_audit_logs.id=2086."
    )

    # Use the system actor convention (NULL actor_key_prefix) since this
    # is a migration-driven sweep, not an operator API call. The
    # actor_label captures attribution for DD readability.
    conn.execute(
        sa.text(
            "INSERT INTO admin_audit_logs ("
            "actor_key_prefix, actor_permissions, actor_label, "
            "tenant_id, domain_id, agent_id, luciel_instance_id, "
            "action, resource_type, resource_pk, resource_natural_id, "
            "before_json, after_json, note, "
            "created_at, updated_at"
            ") VALUES ("
            "NULL, NULL, :actor_label, "
            ":tenant_id, NULL, NULL, NULL, "
            ":action, :resource_type, NULL, NULL, "
            "NULL, CAST(:after_json AS jsonb), :note, "
            "NOW(), NOW()"
            ")"
        ),
        {
            "actor_label": "step28-phase1-prod-orphan-sweep",
            # Use the orphan's tenant for scoping; if multiple tenants
            # had orphans (none observed but defensive), use the first.
            "tenant_id": rows_list[0]["tenant_id"],
            "action": "delete_hard",
            "resource_type": "memory",
            "after_json": after_json,
            "note": note,
        },
    )


def downgrade() -> None:
    """No-op. See module docstring."""
    pass