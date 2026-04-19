"""
Agent model — the person/role row.

Step 24.5 splits the old AgentConfig into two concepts:

1. Agent (this file) — the human or role the system is serving:
   "Sarah the listings agent", "Mike the mortgage broker",
   "py-debugger-user", "sales-director". Minimal fields. No persona.
   One admin API key per Agent is scoped to agent_id.

2. LucielInstance (app/models/luciel_instance.py) — a concrete child
   Luciel owned at tenant / domain / agent scope. Persona + preferred
   provider + system prompt additions live there.

Why split:
- Today an "agent" conflates the person with the Luciel serving them,
  which breaks as soon as one person wants multiple Luciels
  (writer / tester / debugger) or a domain wants a shared Luciel
  for the whole team.
- Knowledge in Step 25 attaches to luciel_instance_id, not agent_id.
- Scope policy (Step 24) still uses agent_id on the API key, but the
  create-at-or-below rule now targets LucielInstance rows.

This model is intentionally thin. All persona / prompt / provider
fields belong on LucielInstance.
"""
from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Agent(Base, TimestampMixin):
    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Scope — an Agent always belongs to exactly one tenant and one domain.
    # (A person works in one department at a time. Cross-domain movement
    # is handled by deactivate + recreate, not mutation, to keep audit
    # trails clean — see Step 24.5 strategic question #6 on promotion /
    # demotion.)
    tenant_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("tenant_configs.tenant_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    domain_id: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
        comment="Must match a domain_configs.domain_id under the same tenant_id. "
                "Validated at the service layer, not via composite FK, because "
                "domain_configs uses (tenant_id, domain_id) as its natural key.",
    )

    # Stable slug the rest of the system references.
    # e.g. "sarah-listings", "mike-mortgages", "py-debugger-user"
    agent_id: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
        comment="URL-safe slug unique within (tenant_id, domain_id).",
    )

    display_name: Mapped[str] = mapped_column(String(200), nullable=False)

    # Optional human-readable blurb. NOT a persona — personas live on
    # LucielInstance. This is "Sarah is a senior listings agent covering
    # Markham / Scarborough." Used for dashboards and audit, not prompts.
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Contact of the real human (if the agent represents a person).
    # Nullable because an Agent can also be a role slot that isn't tied
    # to a single human yet.
    contact_email: Mapped[str | None] = mapped_column(String(200), nullable=True)

    active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )

    # Audit
    created_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(100), nullable=True)

    __table_args__ = (
        # Enforce agent_id uniqueness within (tenant, domain). Two
        # different tenants can both have a "sarah-listings" — that's
        # fine and expected.
        UniqueConstraint(
            "tenant_id",
            "domain_id",
            "agent_id",
            name="uq_agents_tenant_domain_agent",
        ),
        {"comment": "Step 24.5 — person / role rows. Persona lives on luciel_instances."},
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return (
            f"<Agent id={self.id} tenant={self.tenant_id} "
            f"domain={self.domain_id} agent_id={self.agent_id} "
            f"active={self.active}>"
        )