"""add users scope_assignments and agent user_id fk

Revision ID: 3ad39f9e6b55
Revises: 8e2a1f5b9c4d
Create Date: 2026-04-26 17:46:10.144803

Step 24.5b -- Commit 1 (schema). Q6 resolution: Users + scope assignments
+ mandatory key rotation + immutable audit log.

Hand-written per Invariant 12. Three additive phases inside one migration:

A. pgcrypto extension + users table + LOWER(email) expression index
   - Users are tenant-agnostic durable identity.
   - LOWER(email) index aligns with UserRepository.get_by_email(func.lower(...))
     so case-insensitive lookups use the index, not a sequential scan.
   - Plain unique constraint on email column also enforced at DB level.

B. scope_assignment_end_reason enum + scope_assignments table + 3 partial
   indexes + DB-level partial check constraint
   - Enum values: PROMOTED / DEMOTED / REASSIGNED / DEPARTED / DEACTIVATED.
   - Three partial indexes filtered on (ended_at IS NULL) hit the hot path
     "currently active assignments" queries from File 1.7 repo methods.
   - Check constraint: (ended_at IS NULL) = (ended_reason IS NULL) -- defense
     in depth. The service layer enforces this; the DB enforces it again so
     a bug in the service can't write inconsistent lifecycle state.

C. agents.user_id nullable UUID FK -> users.id ON DELETE RESTRICT + index
   - Nullable in this commit. Backfilled by Commit 2's migration via
     contact_email -> users.email join with synthetic-email synthesis for
     NULLs. Flipped to NOT NULL by Commit 3's migration after backfill is
     verified clean (Invariant 12).
   - ON DELETE RESTRICT protects identity history. User deactivation is
     soft-delete (active=False) per Invariant 3, never DELETE.

Downgrade reverses C -> B -> A in correct order: drop agents.user_id first
(it depends on users.id), then scope_assignments (depends on users.id),
then the enum (depends on nothing), then users, then leave pgcrypto in
place (other migrations may need it; dropping is destructive).

Pre-flight (verified before commit):
- Local Alembic chain: 8e2a1f5b9c4d (head) -> 3ad39f9e6b55 (this rev)
- Prod Alembic chain: 8e2a1f5b9c4d (head) confirmed via ECS run-task
  `luciel-migrate:9 alembic current` 2026-04-26 13:00 EDT
- RDS rollback snapshot: luciel-db-pre-step-24-5b-20260426-1321 (creating
  at pre-flight; available before any prod migrate).

Verified replayable against fresh DB before commit.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = '3ad39f9e6b55'
down_revision = '8e2a1f5b9c4d'
branch_labels = None
depends_on = None


# ----------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------

# Postgres enum type name for ScopeAssignment.ended_reason. Matches the
# `name="scope_assignment_end_reason"` declared on the SQLAlchemy column
# in app/models/scope_assignment.py.
END_REASON_ENUM_NAME = "scope_assignment_end_reason"
END_REASON_VALUES = (
    "PROMOTED",
    "DEMOTED",
    "REASSIGNED",
    "DEPARTED",
    "DEACTIVATED",
)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Phase A: pgcrypto extension + users table
    # ------------------------------------------------------------------

    # gen_random_uuid() comes from pgcrypto. Using IF NOT EXISTS so this
    # is idempotent against any DB that already has it (prod RDS likely
    # does for some other reason; local dev typically does not).
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "email",
            sa.String(length=320),
            nullable=False,
        ),
        sa.Column(
            "display_name",
            sa.String(length=200),
            nullable=False,
        ),
        sa.Column(
            "synthetic",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("email", name="uq_users_email"),
        comment=(
            "Step 24.5b -- durable person identity. Tenant-agnostic. "
            "Soft-delete via active per Invariant 3."
        ),
    )

    # Plain index on email for prefix-match queries the LOWER(email)
    # expression index can't serve (e.g. ILIKE 'sarah.%' won't hit the
    # functional index but will hit a btree on email). Cheap to maintain.
    op.create_index(
        "ix_users_email",
        "users",
        ["email"],
        unique=False,
    )

    # LOWER(email) expression index -- this is the index that
    # UserRepository.get_by_email aligns with via
    # func.lower(User.email) == email.strip().lower(). Without this
    # index the lookup would be a sequential scan on every call.
    # Created with explicit SQL because Alembic's create_index doesn't
    # cleanly express functional indexes.
    op.execute(
        "CREATE UNIQUE INDEX ix_users_email_lower "
        "ON users (LOWER(email))"
    )

    # ------------------------------------------------------------------
    # Phase B: scope_assignment_end_reason enum + scope_assignments table
    # ------------------------------------------------------------------

    # Postgres native enum. Created explicitly here (not via SQLAlchemy
    # `create_type=True` on the column) so the enum exists before the
    # table that uses it, and so downgrade() can drop it cleanly.
    end_reason_enum = postgresql.ENUM(
        *END_REASON_VALUES,
        name=END_REASON_ENUM_NAME,
        create_type=False,  # we create it manually below
    )
    end_reason_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "scope_assignments",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "users.id",
                ondelete="RESTRICT",
                name="fk_scope_assignments_user_id_users",
            ),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            sa.String(length=100),
            sa.ForeignKey(
                "tenant_configs.tenant_id",
                ondelete="RESTRICT",
                name="fk_scope_assignments_tenant_id_tenant_configs",
            ),
            nullable=False,
        ),
        # domain_id intentionally has NO FK -- domain_configs uses
        # (tenant_id, domain_id) as natural key (composite), so a
        # single-column FK from scope_assignments would be a half-truth.
        # Service layer validates existence against domain_configs.
        # This mirrors the same convention agents.domain_id uses.
        sa.Column(
            "domain_id",
            sa.String(length=100),
            nullable=False,
        ),
        sa.Column(
            "role",
            sa.String(length=100),
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "ended_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "ended_reason",
            postgresql.ENUM(
                *END_REASON_VALUES,
                name=END_REASON_ENUM_NAME,
                create_type=False,  # already created above
            ),
            nullable=True,
        ),
        sa.Column(
            "ended_note",
            sa.String(length=500),
            nullable=True,
        ),
        sa.Column(
            "ended_by_api_key_id",
            sa.Integer(),
            sa.ForeignKey(
                "api_keys.id",
                ondelete="SET NULL",
                name="fk_scope_assignments_ended_by_api_key_id_api_keys",
            ),
            nullable=True,
        ),
        sa.Column(
            "active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # DB-level defense-in-depth on the lifecycle invariant.
        # Service layer enforces (ended_at IS NULL) <-> (ended_reason IS NULL);
        # this constraint enforces it again so a service bug can't write
        # inconsistent rows. Both NULL = active assignment; both non-NULL =
        # ended assignment. Mixing them is rejected at write time.
        sa.CheckConstraint(
            "(ended_at IS NULL) = (ended_reason IS NULL)",
            name="ck_scope_assignments_ended_at_reason_consistency",
        ),
        comment=(
            "Step 24.5b -- first-class scope assignments. Source of truth "
            "for role within (tenant, domain). Promotion/demotion/departure "
            "via end-and-recreate, never UPDATE in place. Mandatory key "
            "rotation wired at ScopeAssignmentService layer."
        ),
    )

    # Three partial indexes filtered on (ended_at IS NULL). Match the
    # postgresql_where filter declared on the SQLAlchemy model in File 1.2,
    # so Pillar 9's bidirectional integrity check sees identical schema
    # on both sides. Hot-path "currently active" queries from File 1.7
    # repository methods hit these.

    # "What active assignments does this user hold?"
    op.execute(
        "CREATE INDEX ix_scope_assignments_user_id_active "
        "ON scope_assignments (user_id, active) "
        "WHERE ended_at IS NULL"
    )

    # "Who currently holds assignments under this tenant?"
    op.execute(
        "CREATE INDEX ix_scope_assignments_tenant_id_active "
        "ON scope_assignments (tenant_id, active) "
        "WHERE ended_at IS NULL"
    )

    # "Is this user currently assigned to this (tenant, domain, role)?"
    op.execute(
        "CREATE INDEX ix_scope_assignments_user_tenant_domain_role_active "
        "ON scope_assignments (user_id, tenant_id, domain_id, role) "
        "WHERE ended_at IS NULL"
    )

    # ------------------------------------------------------------------
    # Phase C: agents.user_id nullable FK to users.id
    # ------------------------------------------------------------------

    # Nullable in this commit. Backfilled by Commit 2's migration via
    # the join path (agents.contact_email -> users.email, with
    # synthetic-email synthesis for NULLs per OnboardingService
    # backward-compat). Flipped to NOT NULL by Commit 3's migration
    # only after backfill verified zero NULL rows -- Invariant 12.
    #
    # ON DELETE RESTRICT protects identity history: a User cannot be
    # hard-deleted while Agents reference them. User deactivation is
    # always soft-delete (active=False) per Invariant 3, never DELETE.
    op.add_column(
        "agents",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "users.id",
                ondelete="RESTRICT",
                name="fk_agents_user_id_users",
            ),
            nullable=True,
        ),
    )

    # Btree index for the (user_id, tenant_id) hot-path lookup that
    # AgentRepository.get_by_user_and_tenant exercises (File 1.8).
    # Without this index that query is a sequential scan once the
    # agents table grows past a few hundred rows.
    op.create_index(
        "ix_agents_user_id",
        "agents",
        ["user_id"],
        unique=False,
    )
    
def downgrade() -> None:
    """Reverse Phases C -> B -> A in correct dependency order.

    pgcrypto extension is intentionally NOT dropped on downgrade --
    other migrations may depend on it (any future UUID columns elsewhere
    in the schema), and dropping it is destructive in a way that's
    cheap to leave behind. If a future cleanup migration determines
    pgcrypto is truly orphaned, it can drop it explicitly then.
    """

    # ------------------------------------------------------------------
    # Reverse Phase C: drop agents.user_id (depends on users.id)
    # ------------------------------------------------------------------
    op.drop_index("ix_agents_user_id", table_name="agents")
    op.drop_constraint(
        "fk_agents_user_id_users",
        "agents",
        type_="foreignkey",
    )
    op.drop_column("agents", "user_id")

    # ------------------------------------------------------------------
    # Reverse Phase B: drop scope_assignments + enum
    # ------------------------------------------------------------------

    # Indexes drop with the table on most Postgres versions, but be
    # explicit so the operation is observable in logs and rerunnable
    # against partial-failure states.
    op.execute("DROP INDEX IF EXISTS ix_scope_assignments_user_tenant_domain_role_active")
    op.execute("DROP INDEX IF EXISTS ix_scope_assignments_tenant_id_active")
    op.execute("DROP INDEX IF EXISTS ix_scope_assignments_user_id_active")

    op.drop_table("scope_assignments")

    # Drop the enum type only after the table that referenced it is gone.
    end_reason_enum = postgresql.ENUM(
        *END_REASON_VALUES,
        name=END_REASON_ENUM_NAME,
        create_type=False,
    )
    end_reason_enum.drop(op.get_bind(), checkfirst=True)

    # ------------------------------------------------------------------
    # Reverse Phase A: drop users
    # ------------------------------------------------------------------
    op.execute("DROP INDEX IF EXISTS ix_users_email_lower")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")

    # pgcrypto extension intentionally retained -- see docstring above.