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
from app.models.aliases import Agent
from app.models.aliases import (
    SCOPE_LEVEL_AGENT,
    SCOPE_LEVEL_DOMAIN,
    SCOPE_LEVEL_TENANT,
)
from app.models.subscription import (
    TIER_COMPANY,
    TIER_INDIVIDUAL,
    TIER_TEAM,
)
from app.models.aliases import TenantConfig
from app.repositories.admin_audit_repository import AdminAuditRepository, AuditContext
from app.repositories.scope_assignment_repository import ScopeAssignmentRepository
from app.services.admin_service import AdminService
from app.services.instance_service import InstanceService

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

# Role string on the owner-side ScopeAssignment minted at self-serve checkout.
# Sibling to the v1 default "teammate" role (Step 30a.4 invite_service.py:210)
# and the Step-30a.5-incoming "department_lead". No existing "owner" rows
# pre-date this constant -- this is the introduction point for the role string
# (greenlit by Aryan 2026-05-17 during the D-step-30a-owner-scopeassignment-
# missing-self-serve-checkout-2026-05-17 drift work).
_OWNER_ROLE = "owner"


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

_SLUG_BAD_CHARS = re.compile(r"[^a-z0-9-]+")
_SLUG_COLLAPSE_HYPHENS = re.compile(r"-{2,}")

# Email shape gate at the provisioning entry point (Arc 3 Work-Unit C,
# 2026-05-22). Mirrors the precedent in ``app/identity/resolver.py``
# (``_EMAIL_SHAPE`` + ``_EMAIL_MAX_LEN``) so this service applies the
# same liberal-but-non-degenerate email contract the resolver does --
# we deliberately do NOT do RFC-grade deliverability validation here:
#
#   1. The webhook upstream has already trusted Stripe's email column,
#      and the resolver is the canonical normalisation/shape gate per
#      ARCHITECTURE §3.2.11. Re-validating with stricter rules here
#      would create a second, divergent contract.
#
#   2. Synthetic emails minted by the Option B onboarding path and the
#      identity resolver (``identity-<uuid>@<tenant>.luciel.local``,
#      ``agent-<id>@<tenant>.luciel.local``) MUST pass -- they are
#      legitimate per ``app/models/user.py`` line 14. Stricter
#      ``email-validator`` deliverability checks (MX lookup, public-
#      suffix list, etc.) would reject these.
#
# What this gate DOES catch:
#   * empty / whitespace-only input
#   * missing or duplicated ``@``
#   * embedded control characters or whitespace inside the address
#   * length above RFC 5321 maximum (320 chars; matches User.email
#     column cap and identity resolver constants)
#
# Failure mode: ``TierProvisioningValidationError`` (subclass of
# ``ValueError``) so the existing webhook ``except ValueError`` trap
# path keeps catching it without code changes downstream.
_EMAIL_MAX_LEN = 320
_EMAIL_SHAPE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class TierProvisioningValidationError(ValueError):
    """Raised when ``premint_for_tier`` is called with structurally
    invalid input (today: only an unparseable ``primary_user.email``).

    Subclasses ``ValueError`` so the webhook's existing
    ``except ValueError`` trap path catches it without modification.
    Distinct class so future call sites can distinguish a validation
    failure (4xx-class, do not retry) from a real provisioning error
    (5xx-class, retryable by reconciler).
    """


def _validate_email_shape(email: str | None) -> str:
    """Liberal email shape gate -- accepts synthetic emails, rejects
    obvious garbage. Returns the case-folded, whitespace-stripped
    email on success; raises ``TierProvisioningValidationError`` on
    failure.

    See module-level comment above ``_EMAIL_SHAPE`` for the design
    rationale. This function deliberately does NOT do RFC-grade
    validation -- it pins the same contract the identity resolver
    pins, no stricter, no looser.
    """
    if email is None:
        raise TierProvisioningValidationError(
            "primary_user.email is required (got None)"
        )
    if not isinstance(email, str):
        raise TierProvisioningValidationError(
            f"primary_user.email must be str (got {type(email).__name__})"
        )
    candidate = email.strip().lower()
    if not candidate:
        raise TierProvisioningValidationError(
            "primary_user.email is empty / whitespace-only"
        )
    if len(candidate) > _EMAIL_MAX_LEN:
        raise TierProvisioningValidationError(
            f"primary_user.email exceeds RFC 5321 max length "
            f"({_EMAIL_MAX_LEN}); got {len(candidate)} chars"
        )
    if not _EMAIL_SHAPE.match(candidate):
        raise TierProvisioningValidationError(
            "primary_user.email is not a valid email shape "
            "(must be `local@domain.tld` with no embedded whitespace)"
        )
    return candidate


def _slugify_agent_id_from_email(email: str) -> str:
    """Turn an email into a URL-safe Agent.agent_id slug.

    Rules (kept in sync with the ``_SLUG_PATTERN`` in
    ``app/schemas/instance.py``):
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
        # InstanceService requires admin_service injection to
        # avoid the File-11 circular import (see luciel_instance_service.py
        # docstring).
        self.luciel = InstanceService(db, admin_service=self.admin)
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
        ``InstanceService.create_instance`` on the first row that
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

        # Arc 3 Work-Unit C (2026-05-22): shape-gate the email at the
        # entry point so a structurally-invalid email surfaces as a
        # clean ``TierProvisioningValidationError`` (4xx-class, do not
        # retry) instead of failing later with an opaque downstream
        # error (slug collision, DB constraint, etc.). The webhook's
        # existing ``except ValueError`` trap catches it unchanged --
        # ``TierProvisioningValidationError`` subclasses ``ValueError``.
        # Synthetic ``*.luciel.local`` emails pass; we mirror the
        # liberal contract pinned by ``app/identity/resolver.py``.
        _validate_email_shape(
            getattr(primary_user, "email", None) if primary_user is not None else None
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

        # 1.5. Mint the owner-side ScopeAssignment binding the buyer to
        #      (tenant, default-domain, role="owner"). Without this row
        #      the buyer has no tenant binding -- every cookied admin
        #      route fails 403 "Cookied user has no active scope
        #      assignment" because the auth dependency requires an
        #      active ScopeAssignment to resolve the inviter's tenant.
        #      Caught manually 2026-05-17 when the first real owner hit
        #      "Send invite" on /app/team. Drift:
        #      D-step-30a-owner-scopeassignment-missing-self-serve-
        #      checkout-2026-05-17.
        self._ensure_owner_scope_assignment(
            tenant=tenant,
            primary_user=primary_user,
            audit_ctx=audit_ctx,
        )

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

        # 3. Company tier ALSO gets the domain-scope Team Luciel on the
        #    default domain -- the cross-teammate cohesion Luciel that
        #    reads across everyone in a Domain.
        #
        #    Step 30a.6 (tier-hierarchy semantic realignment, 2026-05-20):
        #    Team tier no longer mints a Domain-scope Luciel at signup.
        #    Team is now flat -- one tenant.admin lead + N agent.admin
        #    teammates directly under the tenant, no Domain layer. The
        #    "Team Luciel that sees across teammates" promise on the Team
        #    tier card on Pricing.tsx is satisfied by tenant-scope memory
        #    sharing under the single default domain, not by a separate
        #    Domain-scope instance. Multi-Domain remains a Company-only
        #    value driver. See CANONICAL_RECAP §12 Step 30a.6 row, §14
        #    Entitlement matrix row 3 (Domains cap: 0/0/50), and DRIFTS
        #    `D-tier-semantics-realignment-2026-05-20`.
        if tier == TIER_COMPANY:
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
        returning so ``InstanceService.validate_parent_scope_active``
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

    # -----------------------------------------------------------------
    # Owner ScopeAssignment provisioning
    # -----------------------------------------------------------------

    def _ensure_owner_scope_assignment(
        self,
        *,
        tenant: TenantConfig,
        primary_user: "User",
        audit_ctx: AuditContext,
    ) -> None:
        """Resolve-or-create the owner-role ScopeAssignment for the buyer.

        Idempotent on retry: a Stripe webhook redeliver after a partial
        success must not create a second active assignment. We look up
        any currently-active assignment for (user, tenant) first -- the
        ScopeAssignmentService doctrine says there should be at most one
        per (user, tenant) in steady state (commented at
        scope_assignment_repository.get_active_for_user_in_tenant:226).
        If we find one, we log and return without touching it.

        Writes an ACTION_CREATE / RESOURCE_SCOPE_ASSIGNMENT audit row in
        the same transaction as the INSERT (Invariant 4), via the repo's
        audit_ctx-passthrough path.

        Commits so the immediate subsequent agent-scope LucielInstance
        create sees the same transactional state as the primary Agent
        commit just above -- mirrors _ensure_primary_agent's commit
        discipline. Re-fetch is not needed; we don't return the row.
        """
        sar = ScopeAssignmentRepository(self.db)

        existing = sar.get_active_for_user_in_tenant(
            user_id=primary_user.id,
            tenant_id=tenant.tenant_id,
        )
        if existing is not None:
            logger.info(
                "tier_provisioning: reusing existing owner scope assignment "
                "tenant=%s user=%s assignment_id=%s role=%s",
                tenant.tenant_id,
                primary_user.id,
                existing.id,
                existing.role,
            )
            return

        sar.create(
            user_id=primary_user.id,
            tenant_id=tenant.tenant_id,
            domain_id=DEFAULT_DOMAIN_ID,
            role=_OWNER_ROLE,
            autocommit=False,  # we commit below, after the audit row lands
            audit_ctx=audit_ctx,
        )

        # Same-txn commit discipline as _ensure_primary_agent above.
        self.db.commit()
        logger.info(
            "tier_provisioning: created owner scope assignment tenant=%s "
            "user=%s domain=%s role=%s",
            tenant.tenant_id,
            primary_user.id,
            DEFAULT_DOMAIN_ID,
            _OWNER_ROLE,
        )
