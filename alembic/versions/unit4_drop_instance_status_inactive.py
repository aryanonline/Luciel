"""Unit 4 (lifecycle alignment) — drop the non-spec 'inactive'
instance_status enum value.

The ratified Luciel state machine (Architecture §3.6.1) has exactly
five states: active, paused, deactivating, grace_window, hard_deleted.
The PG enum carried a sixth value, ``inactive``, added for a
multi-instance "over the Free instance cap" downgrade path. That path
is unreachable in the single-Luciel model (Locked Decision #12:
instance_count_cap = 1 on both tiers, so a downgrade never leaves
instances over the new cap) and it contradicted §3.6.7 (a downgrade
keeps the single Luciel ``active`` / widget-only, never inactivates
it). The application enum member and its only writer were removed in
the same unit; this migration removes the value from the PG type.

(The legacy ``deleted`` alias is intentionally RETAINED — existing
deploy rows may carry it as a grace_window equivalent, and the code
reads it via INSTANCE_GRACE_STATES.)

Postgres cannot DROP a value from an enum in place, so we recreate the
type without ``inactive`` and swap the column over. Safe because no
row uses ``inactive`` (verified) and only ``instances.instance_status``
depends on the type.

Revision ID: unit4_drop_instance_status_inactive
Revises: unit3_add_permissive_rls_base_policy
"""
from __future__ import annotations

from alembic import op


revision = "unit4_drop_instance_status_inactive"
down_revision = "unit3_add_permissive_rls_base_policy"
branch_labels = None
depends_on = None


# Spec 5 states + retained legacy 'deleted' alias (no 'inactive').
_VALUES_WITHOUT_INACTIVE = (
    "active", "paused", "deleted", "deactivating",
    "grace_window", "hard_deleted",
)
_VALUES_WITH_INACTIVE = (
    "active", "paused", "inactive", "deleted", "deactivating",
    "grace_window", "hard_deleted",
)


# Partial index whose predicate references instance_status::instance_status
# literals -- it must be dropped before the type swap (its predicate pins
# the old type) and recreated against the new type afterward.
_SWEEP_INDEX_DDL = (
    "CREATE INDEX ix_instances_soft_deleted_sweep "
    "ON public.instances USING btree (soft_deleted_at) "
    "WHERE ((instance_status = ANY (ARRAY['deleted'::instance_status, "
    "'grace_window'::instance_status])) AND (soft_deleted_at IS NOT NULL))"
)


def _recreate_enum(new_values: tuple[str, ...]) -> None:
    vals = ", ".join(f"'{v}'" for v in new_values)
    # 1. Drop the partial index that pins the enum type in its predicate.
    op.execute("DROP INDEX IF EXISTS ix_instances_soft_deleted_sweep")
    # 2. Rename old type, create the new one.
    op.execute("ALTER TYPE instance_status RENAME TO instance_status_old")
    op.execute(f"CREATE TYPE instance_status AS ENUM ({vals})")
    # 3. Swap the column over (drop default first, restore after).
    op.execute(
        "ALTER TABLE instances ALTER COLUMN instance_status DROP DEFAULT"
    )
    op.execute(
        "ALTER TABLE instances ALTER COLUMN instance_status TYPE "
        "instance_status USING instance_status::text::instance_status"
    )
    op.execute(
        "ALTER TABLE instances ALTER COLUMN instance_status "
        "SET DEFAULT 'active'::instance_status"
    )
    # 4. Drop the old type and recreate the partial index against the new.
    op.execute("DROP TYPE instance_status_old")
    op.execute(_SWEEP_INDEX_DDL)


def upgrade() -> None:
    # Guard: refuse if any row still uses 'inactive' (should be none).
    conn = op.get_bind()
    n = conn.exec_driver_sql(
        "SELECT count(*) FROM instances WHERE instance_status = 'inactive'"
    ).scalar()
    if n:
        raise RuntimeError(
            f"{n} instances row(s) still use instance_status='inactive'; "
            "migrate them to 'grace_window' or 'active' before dropping "
            "the enum value."
        )
    _recreate_enum(_VALUES_WITHOUT_INACTIVE)


def downgrade() -> None:
    _recreate_enum(_VALUES_WITH_INACTIVE)
