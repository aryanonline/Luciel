"""step_24_5_agent_luciel_split

Creates the three new tables introduced in Step 24.5:

  1. agents                — person/role rows (File 1)
  2. luciel_instances      — scope-owned child Luciels (File 2)
  3. admin_audit_logs      — durable admin mutation audit trail (File 6.5a)

Non-goals of this migration:
  - Does NOT touch agent_configs. That table stays read-only for one
    release cycle. A follow-up migration after Step 26b production
    redeploy will deprecate it.
  - Does NOT add luciel_instance_id FK columns to api_keys /
    knowledge_embeddings / sessions. Those come in a later migration
    once the new tables are stable under production traffic. Until
    then, Step 24.5's routes resolve instance <-> key/knowledge/session
    bindings at the service layer.

Hand-written (not autogenerate) to avoid Alembic dropping the pgvector
`embedding` column on knowledge_embeddings — same trap we hit in
Step 22. See the SAWarning "Did not recognize type vector of column
embedding" in the Step 22 autogen output for context.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = 'c957a155c325'
down_revision = '107ed978475b'
branch_labels = None
depends_on = None


# Revision identifiers auto-filled by Alembic — DO NOT EDIT these two
# lines if alembic revision set them correctly. If down_revision is
# not "107ed978475b", edit it by hand to point at Step 22's migration.
# revision = "..."
# down_revision = "107ed978475b"
# branch_labels = None
# depends_on = None


# Allowed values for the LucielInstance.scope_level CHECK constraint.
# Kept as a module constant so the downgrade can reference it too.
_ALLOWED_SCOPE_LEVELS = ("tenant", "domain", "agent")


def upgrade() -> None:
    # -----------------------------------------------------------------
    # 1. agents — the person/role table (File 1)
    # -----------------------------------------------------------------
    op.create_table(
        "agents",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),

        sa.Column(
            "tenant_id",
            sa.String(length=100),
            sa.ForeignKey("tenant_configs.tenant_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("domain_id", sa.String(length=100), nullable=False),
        sa.Column("agent_id", sa.String(length=100), nullable=False),

        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("contact_email", sa.String(length=200), nullable=True),

        sa.Column(
            "active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),

        sa.Column("created_by", sa.String(length=100), nullable=True),
        sa.Column("updated_by", sa.String(length=100), nullable=True),

        # TimestampMixin columns
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),

        sa.UniqueConstraint(
            "tenant_id",
            "domain_id",
            "agent_id",
            name="uq_agents_tenant_domain_agent",
        ),
        comment="Step 24.5 — person / role rows. Persona lives on luciel_instances.",
    )
    op.create_index("ix_agents_tenant_id", "agents", ["tenant_id"])
    op.create_index("ix_agents_domain_id", "agents", ["domain_id"])
    op.create_index("ix_agents_agent_id", "agents", ["agent_id"])

    # -----------------------------------------------------------------
    # 2. luciel_instances — scope-owned child Luciels (File 2)
    # -----------------------------------------------------------------
    op.create_table(
        "luciel_instances",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),

        sa.Column("instance_id", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=True),

        sa.Column("scope_level", sa.String(length=20), nullable=False),

        sa.Column(
            "scope_owner_tenant_id",
            sa.String(length=100),
            sa.ForeignKey("tenant_configs.tenant_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("scope_owner_domain_id", sa.String(length=100), nullable=True),
        sa.Column("scope_owner_agent_id", sa.String(length=100), nullable=True),

        sa.Column("system_prompt_additions", sa.Text(), nullable=True),
        sa.Column("preferred_provider", sa.String(length=50), nullable=True),
        sa.Column("allowed_tools", postgresql.JSONB(), nullable=True),

        sa.Column(
            "active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "knowledge_chunk_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),

        sa.Column("created_by", sa.String(length=100), nullable=True),
        sa.Column("updated_by", sa.String(length=100), nullable=True),

        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),

        # --- CHECK constraints ---------------------------------------
        # Enum-like validation for scope_level without a PG ENUM type
        # (matches File 2's model-layer constraint).
        sa.CheckConstraint(
            "scope_level IN ("
            + ", ".join(f"'{lvl}'" for lvl in _ALLOWED_SCOPE_LEVELS)
            + ")",
            name="ck_luciel_instances_scope_level",
        ),
        # Scope-level <-> owner-column consistency.
        sa.CheckConstraint(
            "("
            "  (scope_level = 'tenant' "
            "     AND scope_owner_domain_id IS NULL "
            "     AND scope_owner_agent_id IS NULL)"
            "  OR "
            "  (scope_level = 'domain' "
            "     AND scope_owner_domain_id IS NOT NULL "
            "     AND scope_owner_agent_id IS NULL)"
            "  OR "
            "  (scope_level = 'agent' "
            "     AND scope_owner_domain_id IS NOT NULL "
            "     AND scope_owner_agent_id IS NOT NULL)"
            ")",
            name="ck_luciel_instances_scope_owner_shape",
        ),

        # --- Uniqueness ---------------------------------------------
        sa.UniqueConstraint(
            "scope_owner_tenant_id",
            "scope_owner_domain_id",
            "scope_owner_agent_id",
            "instance_id",
            name="uq_luciel_instances_scope_instance",
        ),
        comment="Step 24.5 — scope-owned child Luciels. Persona lives here.",
    )
    op.create_index(
        "ix_luciel_instances_instance_id", "luciel_instances", ["instance_id"]
    )
    op.create_index(
        "ix_luciel_instances_scope_level", "luciel_instances", ["scope_level"]
    )
    op.create_index(
        "ix_luciel_instances_scope_owner_tenant_id",
        "luciel_instances",
        ["scope_owner_tenant_id"],
    )
    op.create_index(
        "ix_luciel_instances_scope_owner_domain_id",
        "luciel_instances",
        ["scope_owner_domain_id"],
    )
    op.create_index(
        "ix_luciel_instances_scope_owner_agent_id",
        "luciel_instances",
        ["scope_owner_agent_id"],
    )
    op.create_index(
        "ix_luciel_instances_scope_lookup",
        "luciel_instances",
        [
            "scope_owner_tenant_id",
            "scope_owner_domain_id",
            "scope_owner_agent_id",
            "active",
        ],
    )

    # -----------------------------------------------------------------
    # 3. admin_audit_logs — durable admin mutation audit trail
    #    (File 6.5a / 6.5d)
    # -----------------------------------------------------------------
    op.create_table(
        "admin_audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),

        # WHO
        sa.Column("actor_key_prefix", sa.String(length=20), nullable=True),
        sa.Column("actor_permissions", sa.String(length=500), nullable=True),
        sa.Column("actor_label", sa.String(length=100), nullable=True),

        # WHERE
        sa.Column("tenant_id", sa.String(length=100), nullable=False),
        sa.Column("domain_id", sa.String(length=100), nullable=True),
        sa.Column("agent_id", sa.String(length=100), nullable=True),
        sa.Column("luciel_instance_id", sa.Integer(), nullable=True),

        # WHAT
        sa.Column("action", sa.String(length=30), nullable=False),
        sa.Column("resource_type", sa.String(length=50), nullable=False),
        sa.Column("resource_pk", sa.Integer(), nullable=True),
        sa.Column("resource_natural_id", sa.String(length=200), nullable=True),

        # DIFF
        sa.Column("before_json", postgresql.JSONB(), nullable=True),
        sa.Column("after_json", postgresql.JSONB(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),

        # TimestampMixin
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),

        comment="Step 24.5 — durable admin mutation audit trail.",
    )

    # Single-column indexes (index=True on the model columns).
    op.create_index(
        "ix_admin_audit_logs_actor_key_prefix",
        "admin_audit_logs",
        ["actor_key_prefix"],
    )
    op.create_index("ix_admin_audit_logs_tenant_id", "admin_audit_logs", ["tenant_id"])
    op.create_index("ix_admin_audit_logs_domain_id", "admin_audit_logs", ["domain_id"])
    op.create_index("ix_admin_audit_logs_agent_id", "admin_audit_logs", ["agent_id"])
    op.create_index(
        "ix_admin_audit_logs_luciel_instance_id",
        "admin_audit_logs",
        ["luciel_instance_id"],
    )
    op.create_index("ix_admin_audit_logs_action", "admin_audit_logs", ["action"])
    op.create_index(
        "ix_admin_audit_logs_resource_type",
        "admin_audit_logs",
        ["resource_type"],
    )
    op.create_index(
        "ix_admin_audit_logs_resource_natural_id",
        "admin_audit_logs",
        ["resource_natural_id"],
    )

    # Composite indexes (the three query shapes the audit trail serves).
    op.create_index(
        "ix_admin_audit_logs_tenant_time",
        "admin_audit_logs",
        ["tenant_id", "created_at"],
    )
    op.create_index(
        "ix_admin_audit_logs_actor_time",
        "admin_audit_logs",
        ["actor_key_prefix", "created_at"],
    )
    op.create_index(
        "ix_admin_audit_logs_resource",
        "admin_audit_logs",
        ["resource_type", "resource_pk", "created_at"],
    )


def downgrade() -> None:
    # Reverse order: drop indexes / tables in the opposite order of
    # creation. CHECK constraints and UNIQUE constraints are dropped
    # implicitly when the parent table is dropped.

    # --- admin_audit_logs -------------------------------------------
    op.drop_index("ix_admin_audit_logs_resource", table_name="admin_audit_logs")
    op.drop_index("ix_admin_audit_logs_actor_time", table_name="admin_audit_logs")
    op.drop_index("ix_admin_audit_logs_tenant_time", table_name="admin_audit_logs")
    op.drop_index(
        "ix_admin_audit_logs_resource_natural_id", table_name="admin_audit_logs"
    )
    op.drop_index("ix_admin_audit_logs_resource_type", table_name="admin_audit_logs")
    op.drop_index("ix_admin_audit_logs_action", table_name="admin_audit_logs")
    op.drop_index(
        "ix_admin_audit_logs_luciel_instance_id", table_name="admin_audit_logs"
    )
    op.drop_index("ix_admin_audit_logs_agent_id", table_name="admin_audit_logs")
    op.drop_index("ix_admin_audit_logs_domain_id", table_name="admin_audit_logs")
    op.drop_index("ix_admin_audit_logs_tenant_id", table_name="admin_audit_logs")
    op.drop_index(
        "ix_admin_audit_logs_actor_key_prefix", table_name="admin_audit_logs"
    )
    op.drop_table("admin_audit_logs")

    # --- luciel_instances -------------------------------------------
    op.drop_index(
        "ix_luciel_instances_scope_lookup", table_name="luciel_instances"
    )
    op.drop_index(
        "ix_luciel_instances_scope_owner_agent_id",
        table_name="luciel_instances",
    )
    op.drop_index(
        "ix_luciel_instances_scope_owner_domain_id",
        table_name="luciel_instances",
    )
    op.drop_index(
        "ix_luciel_instances_scope_owner_tenant_id",
        table_name="luciel_instances",
    )
    op.drop_index(
        "ix_luciel_instances_scope_level", table_name="luciel_instances"
    )
    op.drop_index(
        "ix_luciel_instances_instance_id", table_name="luciel_instances"
    )
    op.drop_table("luciel_instances")

    # --- agents -----------------------------------------------------
    op.drop_index("ix_agents_agent_id", table_name="agents")
    op.drop_index("ix_agents_domain_id", table_name="agents")
    op.drop_index("ix_agents_tenant_id", table_name="agents")
    op.drop_table("agents")
