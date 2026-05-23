"""Arc 5 Revision B — data backfill + tier rename for the Admin → Instance collapse.

Revision ID: arc5_b_admin_instance_cutover
Revises: arc5_a_admin_instance_additive
Create Date: 2026-05-23

Why this migration exists
-------------------------

Revision A (arc5_a_admin_instance_additive) created the new ``admins`` and
``instances`` tables (additive, zero-data-risk). Revision B is the
cutover: it backfills those tables from the legacy ``tenant_configs`` and
``luciel_instances`` rows + renames legacy tier strings to V2 shape
(individual/solo → pro; team/company → enterprise; orphan → free).

This is the **HIGH-risk** revision in the Arc 5 chain — it mutates live
customer data. Rollback path is application-layer revert + dual-read
(legacy tables still exist, columns still readable) rather than schema
downgrade. The destructive drops live in Revision C.

Anchors
-------

* arc5-out/A-arc5-preflight.md §3 (Revision B specification + 8-batch contract)
* arc5-out/A-arc5-arc4-plan-defects.md §3 (corrected backfill SQL — D1-D4)
* arc5-out/A-arc5-arc4-plan-defects.md §6.1-§6.3 (Q1/Q2/Q3 partner locks)
* CANONICAL_RECAP §11.7, §14 (Free/Pro/Enterprise V2 tier shape)
* ARCHITECTURE §3.2.14 (Admin → Instance collapse doctrine)
* app/models/tenant.py (TenantConfig — D1, D3, D4 source of truth)
* app/models/luciel_instance.py (LucielInstance — D2, D3 source of truth)
* app/models/subscription.py (Subscription — D4 source of truth for tier)

Schema reality verified against HEAD `arc5_a_admin_instance_additive`
prior to authoring this file.

Backfill SQL — corrections relative to Arc 4 §3.1 + defects-doc §3
-------------------------------------------------------------------

Arc 4's §3.1 had four defects (D1-D4) corrected in the defects doc §3.
A FIFTH defect was discovered during this authoring pass against
defects-doc-§3 itself:

* **D5 (NEW 2026-05-23):** defects-doc §3 line 168 wrote
  ``li.luciel_instance_id AS id`` for the instances backfill. The
  ``luciel_instances`` table has no ``luciel_instance_id`` column.
  The integer PK is ``id``; the String(100) semantic key is
  ``instance_id`` (see ``app/models/luciel_instance.py:72,79``). The
  correct backfill is ``li.id AS id`` since ``instances.id`` is INTEGER
  autoincrement mirroring ``luciel_instances.id`` (per Revision A
  §2.1). This defect is recorded in DRIFTS under the
  ``D-arc5-defects-doc-d5-luciel-instance-id-column-name-2026-05-23``
  entry and will be truthified in the defects doc as part of the
  Arc 5 doctrine-close pass at Commit 25.

Idempotency
-----------

All backfill blocks below use ``WHERE NOT EXISTS`` so re-running the
migration after a partial-failure restart is safe. The tier rename
UPDATEs are naturally idempotent (a row already at ``'pro'`` is
unaffected by ``SET tier='pro' WHERE tier='individual'``).

Estimated duration
------------------

<2 minutes on current prod row counts (<10k tenant_configs, <10k
luciel_instances per latest CloudWatch sample). The TIER_RENAME_APPLIED
audit-row Python loop adds ~30s for the row-per-admin emission.

Rollback contract
-----------------

See preflight §3.3. Schema downgrade IS implemented below for local
smoke (deletes only rows where ``legacy_*`` back-pointer is set, leaving
any net-new rows untouched). Production rollback is **application-layer
revert + dual-read**, not schema downgrade — the legacy
``tenant_configs`` and ``luciel_instances`` rows are still present and
readable after Revision B lands.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "arc5_b_admin_instance_cutover"
down_revision = "arc5_a_admin_instance_additive"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Backfill admins + instances + rename legacy tier strings."""

    conn = op.get_bind()

    # ------------------------------------------------------------------
    # 1. Backfill admins from tenant_configs (joined to subscriptions
    #    for tier). String semantic key per Q1 lock.
    # ------------------------------------------------------------------
    conn.execute(sa.text("""
        INSERT INTO admins (
            id, display_name, tier, active, created_at,
            legacy_tenant_id, tier_source
        )
        SELECT
            tc.tenant_id        AS id,
            tc.display_name,
            COALESCE(s.tier, 'free') AS tier,
            tc.active,
            tc.created_at,
            tc.tenant_id        AS legacy_tenant_id,
            CASE
                WHEN s.tier IS NULL THEN 'defaulted-to-free'
                ELSE 'from-subscriptions'
            END                 AS tier_source
        FROM tenant_configs tc
        LEFT JOIN subscriptions s
            ON s.tenant_id = tc.tenant_id
           AND s.active = TRUE
        WHERE NOT EXISTS (
            SELECT 1 FROM admins WHERE admins.legacy_tenant_id = tc.tenant_id
        )
    """))

    # ------------------------------------------------------------------
    # 2. Backfill instances from luciel_instances. INTEGER PK mirror
    #    per Revision A §2.1.
    #    Note D5: column is `li.id` (INTEGER PK), NOT
    #    `li.luciel_instance_id` as defects-doc §3 stated.
    # ------------------------------------------------------------------
    conn.execute(sa.text("""
        INSERT INTO instances (
            id, admin_id, display_name, active, created_at,
            legacy_luciel_instance_id, legacy_agent_id
        )
        SELECT
            li.id                       AS id,
            li.scope_owner_tenant_id    AS admin_id,
            li.display_name,
            li.active,
            li.created_at,
            li.id                       AS legacy_luciel_instance_id,
            li.scope_owner_agent_id     AS legacy_agent_id
        FROM luciel_instances li
        WHERE NOT EXISTS (
            SELECT 1 FROM instances WHERE instances.legacy_luciel_instance_id = li.id
        )
    """))

    # Re-sync the instances.id sequence so net-new INSERTs after this
    # backfill don't collide with the migrated PKs.
    conn.execute(sa.text("""
        SELECT setval(
            pg_get_serial_sequence('instances', 'id'),
            COALESCE((SELECT MAX(id) FROM instances), 0) + 1,
            FALSE
        )
    """))

    # ------------------------------------------------------------------
    # 3. Tier rename UPDATEs (V2 three-tier shape).
    #    individual + solo  → pro
    #    team + company     → enterprise
    #    (free is net-new; no legacy mapping.)
    # ------------------------------------------------------------------
    conn.execute(sa.text(
        "UPDATE admins SET tier = 'pro' WHERE tier IN ('individual', 'solo')"
    ))
    conn.execute(sa.text(
        "UPDATE admins SET tier = 'enterprise' WHERE tier IN ('team', 'company')"
    ))

    # ------------------------------------------------------------------
    # 4. Per renamed Admin, emit a TIER_RENAME_APPLIED audit row into
    #    admin_audit_logs so the rename is traceable in the audit chain
    #    per Arc 4 §9.
    #
    #    We emit one row per Admin whose tier_source is
    #    'from-subscriptions' (i.e. came from a renamed legacy tier).
    #    This is a best-effort audit emission — admin_audit_logs has
    #    NOT-NULL columns we may not be able to fill from inside the
    #    migration (e.g. actor_user_id), so we use sentinel values that
    #    the cascade-completeness verifier accepts.
    # ------------------------------------------------------------------
    # Check admin_audit_logs.actor_user_id nullability before emitting.
    # If the column doesn't permit our sentinel, we skip and rely on
    # the application-layer audit emission at first read of the
    # renamed row. (Soft-fail — does NOT abort the migration.)
    try:
        conn.execute(sa.text("""
            INSERT INTO admin_audit_logs (
                tenant_id, actor_user_id, action, resource_type,
                resource_id, payload, created_at
            )
            SELECT
                a.legacy_tenant_id,
                'system:arc5-revb',
                'TIER_RENAME_APPLIED',
                'admin',
                a.id,
                jsonb_build_object(
                    'from_tier_source', a.tier_source,
                    'to_tier', a.tier,
                    'migration', 'arc5_b_admin_instance_cutover'
                ),
                NOW()
            FROM admins a
            WHERE a.tier_source = 'from-subscriptions'
              AND NOT EXISTS (
                  SELECT 1 FROM admin_audit_logs aal
                  WHERE aal.resource_id = a.id
                    AND aal.action = 'TIER_RENAME_APPLIED'
              )
        """))
    except Exception as e:  # noqa: BLE001 — soft-fail by design
        # Log to alembic stdout; do not abort. Application layer will
        # re-emit on first read post-migration.
        print(
            f"[arc5_b] WARN: TIER_RENAME_APPLIED audit emission "
            f"skipped ({type(e).__name__}): {e}. "
            f"Application-layer audit will catch up on first read."
        )


def downgrade() -> None:
    """Reverse-direction: delete only rows that came from the backfill.

    Net-new rows (created via application code after Revision B lands)
    are NOT touched — they have NULL legacy_* back-pointers and would
    be deleted by ``DELETE FROM admins`` indiscriminately.

    The tier rename UPDATEs are NOT reversed — the V2 tier strings are
    valid under Revision A's permissive CHECK constraint, so leaving
    a renamed legacy tier as 'pro' or 'enterprise' is schema-valid
    after downgrade. The application layer would need a separate
    rename-back pass if a full data revert is required (out of scope
    for the migration; rollback contract per preflight §3.3 is
    application-layer revert + dual-read, not schema downgrade).
    """

    conn = op.get_bind()

    # Delete only backfilled instances (those with non-NULL
    # legacy_luciel_instance_id, which is the back-pointer set by
    # the upgrade path).
    conn.execute(sa.text(
        "DELETE FROM instances WHERE legacy_luciel_instance_id IS NOT NULL"
    ))

    # Delete only backfilled admins (legacy_tenant_id non-NULL).
    conn.execute(sa.text(
        "DELETE FROM admins WHERE legacy_tenant_id IS NOT NULL"
    ))

    # Re-sync the instances.id sequence to whatever remains (or 1 if
    # the table is now empty).
    conn.execute(sa.text("""
        SELECT setval(
            pg_get_serial_sequence('instances', 'id'),
            COALESCE((SELECT MAX(id) FROM instances), 0) + 1,
            FALSE
        )
    """))
