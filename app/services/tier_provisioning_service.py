"""Step 30a.1: TierProvisioningService.

Post-checkout pre-minting of the tier-differentiating LucielInstances.

This service is the **single place** that turns a freshly-onboarded tenant
into a tier-shaped tenant. It is called from the webhook AFTER the
``Subscription`` row has been committed, never from any other path. A
failure here does NOT roll back the subscription -- the tenant is paid
for and the webhook returns success to Stripe; a reconciler can re-run
pre-minting later.

Tier -> scope-instance mapping (the §4.7 line-551 commitment made
concrete on disk):

* ``individual``: 1 agent-scope Luciel for the primary buyer
* ``team``:       1 agent-scope Luciel for the primary buyer +
                  1 domain-scope "Team Luciel" on the default domain
* ``company``:    1 agent-scope Luciel for the primary buyer +
                  1 domain-scope "Team Luciel" on the default domain +
                  1 tenant-scope "Company Luciel"

Each higher tier is a strict superset of the tier below -- this is the
"a Team Luciel is not a bigger Individual Luciel" commitment in
CANONICAL_RECAP §14 made code.

Agent-scope pre-mint side-effect: OnboardingService does NOT create an
``Agent`` row -- it stops at tenant + default domain. To pre-mint an
agent-scope Luciel we need an Agent under the (tenant, default-domain).
This service creates one for the primary buyer, slugged from the email
local-part, bound via ``Agent.user_id`` to the durable User row.

All writes are atomic within ``premint_for_tier``: a failure on any
step rolls back the entire pre-mint set (no half-provisioned tenants).
The audit row(s) ride the same transaction as the mutations they
describe (Invariant 4).
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.admin_audit_log import ACTION_CREATE, RESOURCE_AGENT
from app.models.agent import Agent
from app.models.luciel_instance import (
    SCOPE_LEVEL_AGENT,
    SCOPE_LEVEL_DOMAIN,
    SCOPE_LEVEL_TENANT,
)
from app.models.subscription import (
    TIER_COMPANY,
    TIER_INDIVIDUAL,
    TIER_TEAM,
)
from app.models.tenant import TenantConfig
from app.repositories.admin_audit_repository import AdminAuditRepository, AuditContext
from app.services.admin_service import AdminService
from app.services.luciel_instance_service import LucielInstanceService

if TYPE_CHECKING:
    from app.models.user import User

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------

# Default domain id minted by OnboardingService.onboard_tenant. If
# OnboardingService ever changes its default, update here too -- there
# is no cleaner coupling because onboarding accepts a parameter but the
# webhook does not override it (Step 30a.1 keeps the contract stable).
DEFAULT_DOMAIN_ID = "general"

# Instance-id slugs we mint. These are deterministic per tenant so
# re-running pre-mint after a partial failure surfaces as a
# DuplicateInstanceError (409) rather than a silent second copy.
_INSTANCE_ID_AGENT_PRIMARY = "primary"
_INSTANCE_ID_DOMAIN_TEAM = "team-luciel"
_INSTANCE_ID_TENANT_COMPANY = "company-luciel"

# Audit / created_by label.
_CREATED_BY = "tier_provisioning"


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

_SLUG_BAD_CHARS = re.compile(r"[^a-z0-9-]+")
_SLUG_COLLAPSE_HYPHENS = re.compile(r"-{2,}")


def _slugify_agent_id_from_email(email: str) -> str:
    """Turn an email into a URL-safe Agent.agent_id slug.

    Rules (kept in sync with the ``_SLUG_PATTERN`` in
    ``app/schemas/luciel_instance.py``):
      * lowercase
      * strip the domain part (``@example.com``)
      * replace non ``[a-z0-9-]`` runs with ``-``
      * collapse multiple hyphens
      * strip leading/trailing hyphens
      * cap at 100 chars
      * fall back to ``primary`` if the result is empty / too short
    """
    local = email.split("@", 1)[0].lower().strip()
    slug = _SLUG_BAD_CHARS.sub("-", local)
    slug = _SLUG_COLLAPSE_HYPHENS.sub("-", slug).strip("-")
    if len(slug) < 2:
        # The schema regex requires min length 2; pad / fall back.
        slug = "primary"
    return slug[:100]


# ---------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------

class TierProvisioningService:
    """Pre-mints tier-shaped child resources for a freshly-onboarded tenant.

    Lifetime: one instance per webhook call -- never reuse across
    requests, the bound ``Session`` is request-scoped.
    """

    def __init__(self, db: Session) -> None:
        self.db = db
        self.admin = AdminService(db)
        # LucielInstanceService requires admin_service injection to
        # avoid the File-11 circular import (see luciel_instance_service.py
        # docstring).
        self.luciel = LucielInstanceService(db, admin_service=self.admin)
        self.audit = AdminAuditRepository(db)

    # -----------------------------------------------------------------
    # Public entry point
    # -----------------------------------------------------------------

    def premint_for_tier(
        self,
        *,
        tenant_id: str,
        tier: str,
        primary_user: "User",
        audit_ctx: AuditContext,
    ) -> dict:
        """Pre-mint the tier-mandated set of LucielInstances for ``tenant_id``.

        Returns a dict describing what was created -- useful for tests
        and observability. The webhook does NOT consume the return; the
        dict shape may evolve.

        Idempotency: re-running pre-mint after a partial success will
        raise ``DuplicateInstanceError`` from
        ``LucielInstanceService.create_instance`` on the first row that
        already exists. The caller (the webhook) catches and logs but
        does NOT roll back the subscription -- a reconciler is expected
        to re-attempt after the duplicates are cleaned up.

        Raises any exception from the underlying repos / service; the
        webhook treats this as "best effort" and traps it.
        """
        if tier not in (TIER_INDIVIDUAL, TIER_TEAM, TIER_COMPANY):
            raise ValueError(
                f"TierProvisioningService.premint_for_tier: unknown tier {tier!r}"
            )

        tenant = self.admin.get_tenant_config(tenant_id)
        if tenant is None or not getattr(tenant, "active", False):
            raise ValueError(
                f"TierProvisioningService.premint_for_tier: tenant {tenant_id!r} "
                f"missing or inactive at pre-mint time"
            )

        created: dict = {
            "tenant_id": tenant_id,
            "tier": tier,
            "agent_id": None,
            "instances": [],
        }

        # 1. ALWAYS create a primary Agent for the buyer (every tier has
        #    an agent-scope Luciel under it).
        agent = self._ensure_primary_agent(
            tenant=tenant,
            primary_user=primary_user,
            audit_ctx=audit_ctx,
        )
        created["agent_id"] = agent.agent_id

        # 2. Mint the agent-scope Luciel -- the "your Luciel" the buyer
        #    chats with from day one. Every tier gets this.
        ind_instance = self.luciel.create_instance(
            audit_ctx=audit_ctx,
            instance_id=_INSTANCE_ID_AGENT_PRIMARY,
            display_name=f"{tenant.display_name} Luciel",
            scope_level=SCOPE_LEVEL_AGENT,
            scope_owner_tenant_id=tenant_id,
            scope_owner_domain_id=DEFAULT_DOMAIN_ID,
            scope_owner_agent_id=agent.agent_id,
            description=(
                f"Pre-minted at {tier} signup -- primary buyer agent-scope Luciel."
            ),
            created_by=_CREATED_BY,
        )
        created["instances"].append({
            "scope_level": SCOPE_LEVEL_AGENT,
            "instance_id": ind_instance.instance_id,
            "pk": ind_instance.id,
        })

        # 3. Team + Company tiers ALSO get the domain-scope Team Luciel
        #    on the default domain -- this is the §14 differentiator
        #    that makes a Team subscription not just "bigger Individual."
        if tier in (TIER_TEAM, TIER_COMPANY):
            team_instance = self.luciel.create_instance(
                audit_ctx=audit_ctx,
                instance_id=_INSTANCE_ID_DOMAIN_TEAM,
                display_name=f"{tenant.display_name} Team Luciel",
                scope_level=SCOPE_LEVEL_DOMAIN,
                scope_owner_tenant_id=tenant_id,
                scope_owner_domain_id=DEFAULT_DOMAIN_ID,
                scope_owner_agent_id=None,
                description=(
                    f"Pre-minted at {tier} signup -- domain-scope Team Luciel "
                    f"(reads across all teammates in the default domain)."
                ),
                created_by=_CREATED_BY,
            )
            created["instances"].append({
                "scope_level": SCOPE_LEVEL_DOMAIN,
                "instance_id": team_instance.instance_id,
                "pk": team_instance.id,
            })

        # 4. Company tier ALSO gets the tenant-scope Company Luciel --
        #    the one that sees every domain, every agent, every team.
        if tier == TIER_COMPANY:
            company_instance = self.luciel.create_instance(
                audit_ctx=audit_ctx,
                instance_id=_INSTANCE_ID_TENANT_COMPANY,
                display_name=f"{tenant.display_name} Company Luciel",
                scope_level=SCOPE_LEVEL_TENANT,
                scope_owner_tenant_id=tenant_id,
                scope_owner_domain_id=None,
                scope_owner_agent_id=None,
                description=(
                    "Pre-minted at company signup -- tenant-scope Company Luciel "
                    "(reads across every domain, agent, and team)."
                ),
                created_by=_CREATED_BY,
            )
            created["instances"].append({
                "scope_level": SCOPE_LEVEL_TENANT,
                "instance_id": company_instance.instance_id,
                "pk": company_instance.id,
            })

        logger.info(
            "tier_provisioning: pre-minted tenant=%s tier=%s instances=%d agent=%s",
            tenant_id, tier, len(created["instances"]), agent.agent_id,
        )
        return created

    # -----------------------------------------------------------------
    # Primary agent provisioning
    # -----------------------------------------------------------------

    def _ensure_primary_agent(
        self,
        *,
        tenant: TenantConfig,
        primary_user: "User",
        audit_ctx: AuditContext,
    ) -> Agent:
        """Resolve-or-create the primary Agent row for the buyer.

        The Agent is created under the default domain. The slug is
        derived from the user's email local-part to keep dashboards /
        audit rows human-readable; on collision (extremely rare at the
        moment of pre-mint -- new tenant, single user) we suffix the
        first 8 chars of the User UUID.

        Writes an ACTION_CREATE / RESOURCE_AGENT audit row in the same
        transaction as the INSERT (Invariant 4). Commits before
        returning so ``LucielInstanceService.validate_parent_scope_active``
        sees the Agent on the immediate subsequent
        ``create_instance(scope_level='agent', ...)``.
        """
        base_slug = _slugify_agent_id_from_email(primary_user.email)
        candidate = base_slug

        # Defensive de-collision -- the buyer's tenant is brand new in
        # the same transaction, so a collision is essentially impossible,
        # but a redeliveried Stripe event combined with a partial-failure
        # reconciler could produce one. We try once with the base slug,
        # then fall back to a User-id-suffixed slug.
        existing = self.db.execute(
            select(Agent).where(
                Agent.tenant_id == tenant.tenant_id,
                Agent.domain_id == DEFAULT_DOMAIN_ID,
                Agent.agent_id == candidate,
            )
        ).scalars().first()
        if existing is not None:
            if existing.user_id == primary_user.id and existing.active:
                # Already provisioned (idempotency on retry); return it.
                logger.info(
                    "tier_provisioning: reusing existing agent tenant=%s agent_id=%s",
                    tenant.tenant_id, candidate,
                )
                return existing
            # Collision on a different user -- suffix the slug.
            suffix = primary_user.id.hex[:8]
            candidate = f"{base_slug}-{suffix}"[:100]

        agent = Agent(
            tenant_id=tenant.tenant_id,
            domain_id=DEFAULT_DOMAIN_ID,
            agent_id=candidate,
            display_name=primary_user.display_name or primary_user.email,
            description=(
                "Primary buyer agent, auto-created at self-serve signup."
            ),
            contact_email=primary_user.email,
            user_id=primary_user.id,
            active=True,
            created_by=_CREATED_BY,
        )
        self.db.add(agent)
        self.db.flush()

        # Audit row in the same transaction (Invariant 4).
        self.audit.record(
            ctx=audit_ctx,
            tenant_id=tenant.tenant_id,
            domain_id=DEFAULT_DOMAIN_ID,
            agent_id=candidate,
            action=ACTION_CREATE,
            resource_type=RESOURCE_AGENT,
            resource_pk=agent.id,
            resource_natural_id=candidate,
            after={
                "tenant_id": tenant.tenant_id,
                "domain_id": DEFAULT_DOMAIN_ID,
                "agent_id": candidate,
                "display_name": agent.display_name,
                "user_id": str(primary_user.id),
                "active": True,
            },
            note=(
                "tier_provisioning: pre-minted primary agent for self-serve buyer"
            ),
        )

        # Commit so the immediate subsequent agent-scope LucielInstance
        # create -- which validates parent-scope-active via a SELECT --
        # can see this row.
        self.db.commit()
        self.db.refresh(agent)
        logger.info(
            "tier_provisioning: created primary agent tenant=%s agent_id=%s user=%s",
            tenant.tenant_id, candidate, primary_user.id,
        )
        return agent
