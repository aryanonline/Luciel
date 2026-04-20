"""
LucielInstance model — a concrete child Luciel owned at one scope.

Step 24.5 split — persona lives here, not on Agent.

Scope model
-----------
Every LucielInstance belongs to exactly one scope level:

    scope_level = "tenant"  -> shared across all domains/agents in the tenant
    scope_level = "domain"  -> shared across all agents in one domain
    scope_level = "agent"   -> belongs to a single agent (person/role)

The (scope_level, scope_owner_tenant_id, scope_owner_domain_id,
scope_owner_agent_id) tuple tells the system who can create it, who
can manage it, and who can chat with it:

    tenant instance:  scope_owner_domain_id IS NULL,  scope_owner_agent_id IS NULL
    domain instance:  scope_owner_domain_id = "<x>",  scope_owner_agent_id IS NULL
    agent  instance:  scope_owner_domain_id = "<x>",  scope_owner_agent_id = "<y>"

Knowledge (Step 25), chat keys, and sessions attach to luciel_instance_id.

Create-at-or-below rule (enforced in ScopePolicy, File 9):

    caller key scope                  may create instance at
    --------------------------------- ---------------------------
    platform_admin                    any scope, any tenant
    tenant-scoped admin               tenant / domain / agent in own tenant
    domain-scoped admin               domain / agent in own domain
    agent-scoped admin                agent level, bound to own agent_id

Domain-agnostic by design
-------------------------
No vertical enums, no hardcoded role names, no tenant-ID branches.
All vertical flavour lives in data — preferred_provider,
system_prompt_additions, allowed_tools (JSON list of tool slugs).
Same model serves REMAX Crossroads, a Markham law firm, and an
engineering team with zero code changes.
"""
from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


# Scope-level values. Kept as string literals (not a Python Enum / PG
# ENUM type) so future scopes — e.g. "council" for Luciel Councils —
# can be added without a schema migration. The CHECK constraint below
# is the single source of truth for allowed values today.
SCOPE_LEVEL_TENANT = "tenant"
SCOPE_LEVEL_DOMAIN = "domain"
SCOPE_LEVEL_AGENT = "agent"
ALLOWED_SCOPE_LEVELS = (SCOPE_LEVEL_TENANT, SCOPE_LEVEL_DOMAIN, SCOPE_LEVEL_AGENT)


class LucielInstance(Base, TimestampMixin):
    __tablename__ = "luciel_instances"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # --- Identity ----------------------------------------------------

    # URL-safe slug. Unique within its scope owner — see __table_args__.
    # Examples: "sarah-listings-writer", "sales-briefing-bot",
    # "remax-policy-bot", "py-debugger".
    instance_id: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
        comment="URL-safe slug, unique within its scope owner.",
    )

    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    # --- Scope -------------------------------------------------------

    scope_level: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        index=True,
        comment="'tenant' | 'domain' | 'agent' — see ALLOWED_SCOPE_LEVELS.",
    )

    # Always set.
    scope_owner_tenant_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("tenant_configs.tenant_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # Set when scope_level in ('domain', 'agent'). NULL at tenant scope.
    # Not a composite FK to domain_configs — we validate at the service
    # layer (matches Agent model & Step 24 pattern).
    scope_owner_domain_id: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        index=True,
    )

    # Set only when scope_level == 'agent'. References Agent.agent_id,
    # scoped by (tenant_id, domain_id) — validated at service layer.
    scope_owner_agent_id: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        index=True,
    )

    # --- Persona / behaviour (was on AgentConfig pre-Step-24.5) -----

    # Free-form additions merged into the child Luciel system prompt
    # at chat time. Luciel Core persona is fixed and prepended; this
    # is the per-instance flavour. Plain text, no vertical awareness
    # in the model itself.
    system_prompt_additions: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ---- Step 25b (Option A): optional chunking overrides at instance level ----
    # NULL = inherit from DomainConfig (which itself NULL = inherit from TenantConfig).
    # Same "scope-owned, layered overrides" pattern as persona/provider/allowed_tools.
    chunk_size: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    """Override of DomainConfig/TenantConfig chunk_size for this Luciel instance."""

    chunk_overlap: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    """Override of DomainConfig/TenantConfig chunk_overlap for this Luciel instance."""

    chunk_strategy: Mapped[str | None] = mapped_column(
        String(length=20), nullable=True
    )
    """Override of DomainConfig/TenantConfig chunk_strategy for this Luciel instance."""

    # Optional LLM provider preference: "openai" / "anthropic" /
    # future providers. NULL => use ModelRouter default for the tenant.
    # String, not enum, so new providers don't need migrations.
    preferred_provider: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Optional list of tool slugs this instance is allowed to call.
    # NULL => inherit from DomainConfig.allowed_tools. Empty list =>
    # explicitly no tools. Populated per-row, not hardcoded.
    allowed_tools: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    # --- Lifecycle / audit ------------------------------------------

    active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )

    # Step 25 will attach knowledge via luciel_instance_id. For now
    # this counter is advisory and updated by the ingestion service
    # when Step 25 lands — keeps admin dashboards cheap.
    knowledge_chunk_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )

    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(100), nullable=True)

    __table_args__ = (
        # --- Value validation ---------------------------------------
        CheckConstraint(
            f"scope_level IN "
            f"('{SCOPE_LEVEL_TENANT}', '{SCOPE_LEVEL_DOMAIN}', '{SCOPE_LEVEL_AGENT}')",
            name="ck_luciel_instances_scope_level",
        ),
        # Scope-level ↔ owner-column consistency.
        # tenant  -> domain NULL AND agent NULL
        # domain  -> domain NOT NULL AND agent NULL
        # agent   -> domain NOT NULL AND agent NOT NULL
        CheckConstraint(
            "("
            f" (scope_level = '{SCOPE_LEVEL_TENANT}' "
            "   AND scope_owner_domain_id IS NULL "
            "   AND scope_owner_agent_id IS NULL)"
            " OR "
            f"(scope_level = '{SCOPE_LEVEL_DOMAIN}' "
            "   AND scope_owner_domain_id IS NOT NULL "
            "   AND scope_owner_agent_id IS NULL)"
            " OR "
            f"(scope_level = '{SCOPE_LEVEL_AGENT}' "
            "   AND scope_owner_domain_id IS NOT NULL "
            "   AND scope_owner_agent_id IS NOT NULL)"
            ")",
            name="ck_luciel_instances_scope_owner_shape",
        ),
        # --- Uniqueness --------------------------------------------
        # instance_id is unique within its scope owner. Two different
        # agents can each have a "writer" — that's fine and expected.
        # NULL-safe columns (domain/agent owner ids) work here because
        # Postgres treats NULLs as distinct in UNIQUE constraints,
        # which is exactly what we want (tenant-scope rows compare only
        # on tenant + instance_id).
        UniqueConstraint(
            "scope_owner_tenant_id",
            "scope_owner_domain_id",
            "scope_owner_agent_id",
            "instance_id",
            name="uq_luciel_instances_scope_instance",
        ),
        # --- Query indexes -----------------------------------------
        # Fast "list all instances for this tenant/domain/agent" lookups.
        Index(
            "ix_luciel_instances_scope_lookup",
            "scope_owner_tenant_id",
            "scope_owner_domain_id",
            "scope_owner_agent_id",
            "active",
        ),
        {"comment": "Step 24.5 — scope-owned child Luciels. Persona lives here."},
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return (
            f"<LucielInstance id={self.id} "
            f"scope={self.scope_level} "
            f"tenant={self.scope_owner_tenant_id} "
            f"domain={self.scope_owner_domain_id} "
            f"agent={self.scope_owner_agent_id} "
            f"instance_id={self.instance_id} active={self.active}>"
        )