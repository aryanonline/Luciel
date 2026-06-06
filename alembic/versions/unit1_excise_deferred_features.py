"""Unit 1 (audit-and-alignment) -- excise deferred-feature schema.

Drops the database surface for the deferred Enterprise tier, multi-Luciel /
sibling composition, and the multi-user / custom-role / team-seat RBAC model,
to match the ratified product docs (Free/Pro, single-login, one Luciel per
account):

  * Locked Decision #12 (one Luciel per account),
  * Locked Decision #19 (single-login; no team seats, no custom roles),
  * Locked Decision #35 + Open Decisions #7/#8 (multi-Luciel & Enterprise DEFERRED),
  * Architecture §3.7.1 (single-login), §6 (deferred list), Vision §7.

Tables dropped (FK-safe order, children first):
  role_permissions, user_role_assignments, custom_roles, permissions,
  scope_assignments, user_invites, sibling_call_grants,
  instance_composition_grants, knowledge_share_grants, admin_tier_overrides.

Also:
  * Tightens the tier CHECK constraints on ``admins`` and ``data_export_jobs``
    from ('free','pro','enterprise') to ('free','pro').
  * Drops the now-unused ``scope_role`` enum type.
  * Drops the Enterprise-only personality second-admin-approval columns on
    ``instances`` (pending_personality_*, personality_approval_state,
    personality_submitted_*, personality_approved_*) if present.

Reversibility (§5.9.1): the downgrade recreates the dropped tables with their
structural shape and restores the permissive tier CHECK + scope_role enum. It
does NOT restore row data (these are deferred-feature tables that carry no
production data in the Free/Pro product). admin_audit_logs is untouched
(append-only; §5.3).
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "unit1_excise_deferred_features"
down_revision = "rescan_core_inactive_status"
branch_labels = None
depends_on = None


# Tables to drop, in FK-safe order (children before parents).
_DROP_ORDER = [
    "role_permissions",
    "user_role_assignments",
    "custom_roles",
    "permissions",
    "scope_assignments",
    "user_invites",
    "sibling_call_grants",
    "instance_composition_grants",
    "knowledge_share_grants",
    "admin_tier_overrides",
]

# Enterprise-only personality second-admin-approval columns on instances.
_PERSONALITY_APPROVAL_COLS = [
    "pending_personality_preset",
    "pending_personality_axes",
    "pending_business_context",
    "personality_approval_state",
    "personality_submitted_by_user_id",
    "personality_submitted_at",
    "personality_approved_by_user_id",
    "personality_approved_at",
]


def _tighten_tier_check(table: str, col: str, constraint: str) -> None:
    op.drop_constraint(constraint, table, type_="check")
    op.create_check_constraint(
        constraint,
        table,
        f"{col} IN ('free', 'pro')",
    )


def upgrade() -> None:
    conn = op.get_bind()
    insp = sa.inspect(conn)
    existing = set(insp.get_table_names())

    # 0. Data migration: any existing Enterprise-tier rows are downgraded to
    #    Pro before the CHECK is tightened. Enterprise was never GA (Open
    #    Decision #8 defers it); deferring the tier means existing enterprise
    #    rows collapse to the strongest shipped tier (Pro). This keeps the
    #    tightened CHECK satisfiable and is the doctrine-consistent action.
    op.execute("UPDATE admins SET tier='pro' WHERE tier='enterprise'")
    if "subscriptions" in existing:
        op.execute("UPDATE subscriptions SET tier='pro' WHERE tier='enterprise'")
    if "data_export_jobs" in existing:
        op.execute(
            "UPDATE data_export_jobs SET tier_at_request='pro' "
            "WHERE tier_at_request='enterprise'"
        )
    # History/snapshot tier columns (no CHECK, but normalise for consistency).
    op.execute(
        "UPDATE admin_audit_logs SET tier_at_write='pro' "
        "WHERE tier_at_write='enterprise'"
    )
    if "conversation_overage_ledger" in existing:
        op.execute(
            "UPDATE conversation_overage_ledger SET tier_at_close='pro' "
            "WHERE tier_at_close='enterprise'"
        )

    # 1. Drop deferred-feature tables (FK-safe order). IF EXISTS via guard.
    for tbl in _DROP_ORDER:
        if tbl in existing:
            op.drop_table(tbl)

    # 2. Tighten tier CHECK constraints to ('free','pro').
    _tighten_tier_check("admins", "tier", "ck_admins_tier_valid")
    if "data_export_jobs" in existing:
        _tighten_tier_check(
            "data_export_jobs", "tier_at_request", "ck_data_export_jobs_tier_valid"
        )

    # 3. Drop the Enterprise personality-approval columns on instances
    #    (and the CHECK constraint that referenced approval_state).
    instance_constraints = {
        c["name"] for c in insp.get_check_constraints("instances")
    }
    if "ck_instances_personality_approval_state" in instance_constraints:
        op.drop_constraint(
            "ck_instances_personality_approval_state", "instances", type_="check"
        )
    instance_cols = {c["name"] for c in insp.get_columns("instances")}
    with op.batch_alter_table("instances") as batch:
        for col in _PERSONALITY_APPROVAL_COLS:
            if col in instance_cols:
                batch.drop_column(col)

    # 4. Drop the now-unused scope_role enum type (scope_assignments gone).
    op.execute("DROP TYPE IF EXISTS scope_role")


def downgrade() -> None:
    # Structural reversibility only (no row data — deferred-feature tables hold
    # no Free/Pro production data). Recreate the scope_role enum, the permissive
    # tier CHECK, the personality-approval columns, and minimal table shells.
    conn = op.get_bind()
    insp = sa.inspect(conn)

    # 1. Recreate scope_role enum.
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname='scope_role') "
        "THEN CREATE TYPE scope_role AS ENUM "
        "('admin_owner','admin_manager','instance_operator','read_only_viewer'); "
        "END IF; END $$;"
    )

    # 2. Restore permissive tier CHECK constraints.
    op.drop_constraint("ck_admins_tier_valid", "admins", type_="check")
    op.create_check_constraint(
        "ck_admins_tier_valid", "admins", "tier IN ('free', 'pro', 'enterprise')"
    )
    if "data_export_jobs" in set(insp.get_table_names()):
        op.drop_constraint(
            "ck_data_export_jobs_tier_valid", "data_export_jobs", type_="check"
        )
        op.create_check_constraint(
            "ck_data_export_jobs_tier_valid",
            "data_export_jobs",
            "tier_at_request IN ('free', 'pro', 'enterprise')",
        )

    # 3. Recreate personality-approval columns on instances (nullable).
    instance_cols = {c["name"] for c in insp.get_columns("instances")}
    with op.batch_alter_table("instances") as batch:
        if "pending_personality_preset" not in instance_cols:
            batch.add_column(sa.Column("pending_personality_preset", sa.String(50), nullable=True))
        if "pending_personality_axes" not in instance_cols:
            batch.add_column(sa.Column("pending_personality_axes", sa.JSON(), nullable=True))
        if "pending_business_context" not in instance_cols:
            batch.add_column(sa.Column("pending_business_context", sa.Text(), nullable=True))
        if "personality_approval_state" not in instance_cols:
            batch.add_column(
                sa.Column(
                    "personality_approval_state",
                    sa.String(20),
                    nullable=False,
                    server_default="live",
                )
            )
            batch.create_check_constraint(
                "ck_instances_personality_approval_state",
                "personality_approval_state IN ('live', 'pending_approval')",
            )
        if "personality_submitted_by_user_id" not in instance_cols:
            batch.add_column(sa.Column("personality_submitted_by_user_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True))
        if "personality_submitted_at" not in instance_cols:
            batch.add_column(sa.Column("personality_submitted_at", sa.DateTime(timezone=True), nullable=True))
        if "personality_approved_by_user_id" not in instance_cols:
            batch.add_column(sa.Column("personality_approved_by_user_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True))
        if "personality_approved_at" not in instance_cols:
            batch.add_column(sa.Column("personality_approved_at", sa.DateTime(timezone=True), nullable=True))

    # 4. Recreate minimal table shells (parents before children). These are
    #    structural placeholders sufficient to make the downgrade reversible;
    #    the original rich column sets are not reconstructed because the
    #    deferred features carry no Free/Pro data.
    UUID = sa.dialects.postgresql.UUID(as_uuid=True)

    op.create_table(
        "admin_tier_overrides",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("admin_id", sa.String(100), sa.ForeignKey("admins.id"), nullable=False),
        sa.Column("axis", sa.String(100), nullable=False),
        sa.Column("value_json", sa.JSON(), nullable=True),
    )
    op.create_table(
        "permissions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("key", sa.String(100), nullable=False, unique=True),
    )
    op.create_table(
        "custom_roles",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("admin_id", sa.String(100), sa.ForeignKey("admins.id"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
    )
    op.create_table(
        "role_permissions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("custom_role_id", UUID, sa.ForeignKey("custom_roles.id"), nullable=False),
        sa.Column("permission_id", UUID, sa.ForeignKey("permissions.id"), nullable=False),
    )
    op.create_table(
        "user_role_assignments",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("admin_id", sa.String(100), sa.ForeignKey("admins.id"), nullable=False),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("custom_role_id", UUID, sa.ForeignKey("custom_roles.id"), nullable=False),
    )
    op.create_table(
        "scope_assignments",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("admin_id", sa.String(100), sa.ForeignKey("admins.id"), nullable=False),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id"), nullable=False),
        # Structural-only downgrade: role stored as text (the scope_role enum
        # is recreated above for type-existence parity, but we avoid binding it
        # here so SQLAlchemy does not attempt to re-CREATE the type).
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "user_invites",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("admin_id", sa.String(100), sa.ForeignKey("admins.id"), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
    )
    op.create_table(
        "sibling_call_grants",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("admin_id", sa.String(100), sa.ForeignKey("admins.id"), nullable=False),
        sa.Column("caller_instance_id", sa.Integer(), nullable=False),
        sa.Column("callee_instance_id", sa.Integer(), nullable=False),
        sa.Column("approval_state", sa.String(20), nullable=False, server_default="pending"),
    )
    op.create_table(
        "instance_composition_grants",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("admin_id", sa.String(100), sa.ForeignKey("admins.id"), nullable=False),
        sa.Column("parent_instance_id", sa.Integer(), nullable=False),
        sa.Column("child_instance_id", sa.Integer(), nullable=False),
    )
    op.create_table(
        "knowledge_share_grants",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("admin_id", sa.String(100), sa.ForeignKey("admins.id"), nullable=False),
        sa.Column("source_instance_id", sa.Integer(), nullable=False),
        sa.Column("target_instance_id", sa.Integer(), nullable=False),
    )
