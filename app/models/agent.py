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

Step 24.5b additions (this commit):
- user_id (nullable UUID FK -> users.id) — the durable person identity
  this Agent row represents within (tenant, domain). One User can hold
  many Agent rows across tenants. Nullable in this commit; flipped to
  NOT NULL in Commit 3 once backfill is verified clean (Invariant 12).
- ScopeAssignment is the new source of truth for "currently-held role".
  Agent retains its own scope columns for backward compatibility and
  for hot-path read performance; ScopeAssignment is consulted whenever
  promotion / demotion / departure semantics are needed (Q6 resolution).

This model is intentionally thin. All persona / prompt / provider
fields belong on LucielInstance.
"""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.scope_assignment import ScopeAssignment
    from app.models.user import User


class Agent(Base, TimestampMixin):
    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Scope — an Agent always belongs to exactly one tenant and one domain.
    # (A person works in one department at a time. Cross-domain movement
    # is handled by deactivate + recreate, not mutation, to keep audit
    # trails clean — see Step 24.5 strategic question #6 on promotion /
    # demotion. Step 24.5b formalizes that doctrine via ScopeAssignment.)
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
    #
    # Step 24.5b note: contact_email is retained on Agent for backward
    # compatibility with existing onboarding flows. The durable identity
    # email lives on User.email. When Agent.user_id is populated,
    # contact_email is expected to match User.email (or be NULL); the
    # service layer enforces this on create. Real PIPEDA access/erasure
    # walks User, not Agent.contact_email.
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

    # ---- Step 24.5b identity layer ----
    # Nullable in this commit. Backfilled in Commit 2 via the join path
    # (agents.contact_email -> users.email, with synthetic-email synthesis
    # for NULLs per the OnboardingService backward-compat path). Flipped
    # to NOT NULL in Commit 3 only after backfill is verified clean per
    # Invariant 12.
    #
    # ON DELETE RESTRICT protects identity history — a User cannot be hard-
    # deleted while Agents reference them. User deactivation goes through
    # UserService.deactivate_user (Commit 2), which soft-deletes via
    # active=False and cascades to ScopeAssignment.ended_at, never DELETEs
    # the User row. Invariant 3 honored end-to-end.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )

    # ---- relationships ----
    # User<->Agent: bidirectional. User.agents (File 1.1) back-populates
    # to Agent.user. Lazy="select" — eager loading is repository-explicit.
    user: Mapped["User | None"] = relationship(
        "User",
        back_populates="agents",
        foreign_keys=[user_id],
        lazy="select",
    )

    # Agent<->ScopeAssignment: navigational read-only lens. Agent does not
    # own assignment lifecycle — ScopeAssignmentService (Commit 2) is the
    # only writer. Multi-column primaryjoin reflects "all assignments this
    # Agent's identity-and-scope have ever held"; for current-role lookups
    # the service layer filters on ended_at IS NULL.
    #
    # viewonly=True is deliberate: we never want an ORM session flush to
    # accidentally INSERT or DELETE assignment rows via this relationship.
    scope_assignments: Mapped[list["ScopeAssignment"]] = relationship(
        "ScopeAssignment",
        primaryjoin=(
            "and_(ScopeAssignment.user_id == Agent.user_id, "
            "ScopeAssignment.tenant_id == Agent.tenant_id, "
            "ScopeAssignment.domain_id == Agent.domain_id)"
        ),
        foreign_keys=(
            "[ScopeAssignment.user_id, ScopeAssignment.tenant_id, "
            "ScopeAssignment.domain_id]"
        ),
        viewonly=True,
        lazy="select",
    )

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
        {"comment": "Step 24.5b — person / role rows, now bound to durable User identity. Persona lives on luciel_instances."},
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid only
        return (
            f"<Agent id={self.id} tenant={self.tenant_id} "
            f"domain={self.domain_id} agent_id={self.agent_id} "
            f"user_id={self.user_id} active={self.active}>"
        )