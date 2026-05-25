"""
Admin service.

Arc 5 Path A (V2 collapse): Domain layer ELIMINATED per the
aggressive-cleanup amendment. V2 hierarchy is Admin → Instance → Lead;
there is no DomainConfig surface anymore. The legacy AgentConfig table
still exists (dropped at Revision C); methods that touch it remain so
cascade teardown of legacy rows works during the transition.

Handles business logic for admin (formerly tenant) and legacy agent
config management. Keeps route handlers thin by centralizing validation
and persistence.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.admin import Admin
from app.models.agent_config import AgentConfig

# V2: TenantConfig is an alias for Admin (kept for source compatibility
# during the transition; deleted with aliases shim at C2).
TenantConfig = Admin

logger = logging.getLogger(__name__)


class AdminService:

    def __init__(self, db: Session) -> None:
        self.db = db

    # --- Admin (formerly Tenant) Config ---

    def create_tenant_config(self, **kwargs) -> Admin:
        """Create an Admin row. Legacy name kept for caller compatibility.

        V2 translation table (Arc 9.2 PR #99 cleanup):
          * ``tenant_id`` -> ``id``           (PK rename Arc 5 Rev C)
          * ``display_name`` -> ``name``      (column rename Arc 5 Rev C)
          * ``description`` -> dropped        (no home on admins; V2 lives on instances)
          * ``escalation_contact`` -> dropped (legacy contact column removed Arc 5)
          * ``allowed_domains`` -> dropped    (Domain layer removed Arc 5 Path A)
          * ``system_prompt_additions`` -> dropped (now Instance-level, Arc 9 C17)
          * ``created_by`` -> dropped         (audit lives in admin_audit_logs)
          * ``updated_by`` -> dropped         (idem)

        Keeping the public ``TenantConfigCreate`` shape stable lets the
        widget-E2E harness, Stripe webhooks, and external smoke scripts
        keep their existing payloads. The translation below absorbs the
        Arc-5-Rev-C drift; full schema simplification lands in the
        Arc 9.2 Option A migration (PR #100/#101).
        """
        if "tenant_id" in kwargs and "id" not in kwargs:
            kwargs["id"] = kwargs.pop("tenant_id")
        if "display_name" in kwargs and "name" not in kwargs:
            kwargs["name"] = kwargs.pop("display_name")
        # Drop legacy kwargs the V2 admins table no longer accepts.
        for legacy in (
            "description",
            "escalation_contact",
            "allowed_domains",
            "system_prompt_additions",
            "created_by",
            "updated_by",
        ):
            kwargs.pop(legacy, None)
        config = Admin(**kwargs)
        self.db.add(config)
        self.db.commit()
        self.db.refresh(config)
        logger.info("Created admin (tenant_config): %s", config.id)
        return config

    def get_tenant_config(self, tenant_id: str) -> Admin | None:
        """Fetch an Admin row by its id (legacy ``tenant_id``)."""
        stmt = select(Admin).where(Admin.id == tenant_id)
        return self.db.scalars(stmt).first()

    def update_tenant_config(self, tenant_id: str, **kwargs) -> Admin | None:
        config = self.get_tenant_config(tenant_id)
        if not config:
            return None
        for key, value in kwargs.items():
            if value is not None and hasattr(config, key):
                setattr(config, key, value)
        self.db.commit()
        self.db.refresh(config)
        logger.info("Updated admin (tenant_config): %s", tenant_id)
        return config

    def list_tenant_configs(self) -> list[Admin]:
        stmt = select(Admin).order_by(Admin.created_at.desc())
        return list(self.db.scalars(stmt).all())

    # --- Domain Config: REMOVED (Arc 5 Path A) ---
    #
    # The V2 hierarchy is Admin → Instance → Lead. There is no Domain
    # layer. The methods previously here (create/get/update/list/
    # count_active_domains_for_tenant, enforce_domain_cap) were deleted
    # at Arc 5 B3 along with the /admin/domains/* route surface.
    #
    # If any legacy script or test still references these names, it must
    # be rewritten to operate at the Admin or Instance layer.

    # --- Agent Config (legacy table — dropped at Revision C) ---

    def create_agent_config(self, **kwargs) -> AgentConfig:
        config = AgentConfig(**kwargs)
        self.db.add(config)
        self.db.commit()
        self.db.refresh(config)
        logger.info(
            "Created agent config: %s/%s", config.tenant_id, config.agent_id
        )
        return config

    def get_agent_config(self, tenant_id: str, agent_id: str) -> AgentConfig | None:
        stmt = select(AgentConfig).where(
            AgentConfig.tenant_id == tenant_id,
            AgentConfig.agent_id == agent_id,
        )
        return self.db.scalars(stmt).first()

    def update_agent_config(
        self, tenant_id: str, agent_id: str, **kwargs
    ) -> AgentConfig | None:
        config = self.get_agent_config(tenant_id, agent_id)
        if not config:
            return None
        for key, value in kwargs.items():
            if value is not None and hasattr(config, key):
                setattr(config, key, value)
        self.db.commit()
        self.db.refresh(config)
        logger.info("Updated agent config: %s/%s", tenant_id, agent_id)
        return config

    def list_agent_configs(self, tenant_id: str | None = None) -> list[AgentConfig]:
        stmt = select(AgentConfig).order_by(AgentConfig.created_at.desc())
        if tenant_id:
            stmt = stmt.where(AgentConfig.tenant_id == tenant_id)
        return list(self.db.scalars(stmt).all())
    
    # validate_domain_active / list_agent_configs_by_domain: REMOVED at
    # Arc 5 Path A. V2 has no Domain layer, so domain-scoped validation
    # and listing are no longer meaningful. AgentConfig is itself legacy
    # and dropped at Revision C; new code uses InstanceService instead.
    def list_agent_configs_by_domain(
        self, tenant_id: str, domain_id: str,
    ) -> list:
        """V2 no-op stub. Returns []. Domain layer is gone."""
        logger.info(
            "list_agent_configs_by_domain: V2 no-op stub tenant_id=%s domain_id=%s",
            tenant_id,
            domain_id,
        )
        return []

    def deactivate_domain(
        self,
        tenant_id: str,
        domain_id: str,
        *,
        audit_ctx=None,
        luciel_instance_service=None,
        updated_by: str | None = None,
    ) -> bool:
        """Arc 5 Path A — V2 no-op stub. The Domain layer was eliminated
        per the aggressive-cleanup amendment; the /admin/domains/*
        routes that called this method were deleted at B3. Method
        survives only so any straggler caller in scripts/tests
        compiles; returns False (treated as "not found").
        """
        logger.info(
            "AdminService.deactivate_domain: Arc 5 Path A V2 no-op "
            "stub tenant_id=%s domain_id=%s (V2 has no Domain layer).",
            tenant_id,
            domain_id,
        )
        return False

    def deactivate_agent(
        self,
        tenant_id: str,
        agent_id: str,
        *,
        audit_ctx=None,                    # Step 24.5
        luciel_instance_service=None,      # Step 24.5
        updated_by: str | None = None,
    ) -> bool:
        """Soft-deactivate a legacy AgentConfig row.

        Step 24.5: if luciel_instance_service is provided, also cascade-
        deactivate every agent-scoped LucielInstance owned by this agent.
        (The new-table Agent row, if it exists, is handled by a separate
        route — POST /admin/agents/{tenant}/{agent}/deactivate in File 10.
        This legacy path only touches agent_configs and optionally the
        agent-scoped Luciels that reference the same agent_id.)

        audit_ctx / luciel_instance_service are optional for legacy callers.
        """
        from app.models.admin_audit_log import (
            ACTION_DEACTIVATE,
            RESOURCE_AGENT,
        )
        from app.repositories.admin_audit_repository import AdminAuditRepository

        agent = self.get_agent_config(tenant_id, agent_id)
        if not agent:
            return False

        was_active = bool(agent.active)

        try:
            agent.active = False
            if updated_by is not None:
                agent.updated_by = updated_by

            if audit_ctx is not None and was_active:
                AdminAuditRepository(self.db).record(
                    ctx=audit_ctx,
                    tenant_id=tenant_id,
                    action=ACTION_DEACTIVATE,
                    resource_type=RESOURCE_AGENT,
                    resource_pk=agent.id,
                    resource_natural_id=agent_id,
                    domain_id=getattr(agent, "domain_id", None),
                    agent_id=agent_id,
                    before={"active": True},
                    after={"active": False},
                    autocommit=False,
                )

            # Memory cascade: soft-deactivate agent-scoped memory_items.
            # Same audit-ctx-required contract as the leaf method.
            # autocommit=False -- this method commits the whole transaction.
            if audit_ctx is not None:
                self.bulk_soft_deactivate_memory_items_for_agent(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    audit_ctx=audit_ctx,
                    updated_by=updated_by,
                    autocommit=False,
                )

            # Step 24.5 LucielInstance cascade (optional).
            if (
                luciel_instance_service is not None
                and audit_ctx is not None
                and getattr(agent, "domain_id", None) is not None
            ):
                luciel_instance_service.cascade_on_agent_deactivate(
                    audit_ctx=audit_ctx,
                    tenant_id=tenant_id,
                    domain_id=agent.domain_id,
                    agent_id=agent_id,
                    updated_by=updated_by,
                )

            self.db.commit()
            self.db.refresh(agent)
        except Exception:
            self.db.rollback()
            raise

        return True


    def bulk_soft_deactivate_memory_items_for_tenant(
        self,
        tenant_id: str,
        *,
        audit_ctx,
        updated_by: str | None = None,
        autocommit: bool = True,
    ) -> int:
        """Soft-deactivate every active memory_items row for a tenant.

        Used by deactivate_tenant_with_cascade and (indirectly) by the
        Pattern S walker. Mirrors the platform's general soft-delete
        model (recap section 3): memory_items.active flips to False;
        rows persist with active=False until a separate retention job
        hard-purges them.

        PIPEDA Principle 5 (limit retention) is satisfied because the
        application layer filters active=False rows out of every read
        path. A future scheduled job hard-purges inactive rows after
        the configured retention window.

        Returns count of rows deactivated. Always emits one audit row
        with action=ACTION_CASCADE_DEACTIVATE -- even when count == 0 --
        so the audit trail records that this scope was visited on
        every (idempotent) re-run. The after_json carries a per-(agent,
        instance) breakdown for granular forensic queries.

        audit_ctx is REQUIRED.
        """
        from sqlalchemy import func
        from app.models.memory import MemoryItem
        from app.repositories.admin_audit_repository import AdminAuditRepository
        from app.models.admin_audit_log import (
            ACTION_CASCADE_DEACTIVATE,
            RESOURCE_MEMORY,
        )

        if audit_ctx is None:
            raise ValueError(
                "bulk_soft_deactivate_memory_items_for_tenant requires audit_ctx"
            )

        try:
            # Pre-deactivation breakdown for forensic granularity in audit.
            breakdown_rows = (
                self.db.query(
                    MemoryItem.agent_id,
                    MemoryItem.luciel_instance_id,
                    func.count().label("row_count"),
                )
                .filter(
                    MemoryItem.tenant_id == tenant_id,
                    MemoryItem.active.is_(True),
                )
                .group_by(MemoryItem.agent_id, MemoryItem.luciel_instance_id)
                .all()
            )
            breakdown = [
                {
                    "agent_id": agent_id,
                    "luciel_instance_id": luciel_instance_id,
                    "count": row_count,
                }
                for (agent_id, luciel_instance_id, row_count) in breakdown_rows
            ]

            # Bulk single-pass deactivation.
            count = (
                self.db.query(MemoryItem)
                .filter(
                    MemoryItem.tenant_id == tenant_id,
                    MemoryItem.active.is_(True),
                )
                .update(
                    {"active": False},
                    synchronize_session=False,
                )
            )

            AdminAuditRepository(self.db).record(
                ctx=audit_ctx,
                tenant_id=tenant_id,
                action=ACTION_CASCADE_DEACTIVATE,
                resource_type=RESOURCE_MEMORY,
                resource_pk=None,
                resource_natural_id=None,
                after={
                    "count": count,
                    "scope": "tenant",
                    "tenant_id": tenant_id,
                    "breakdown": breakdown,
                    "trigger": "tenant_deactivate_cascade",
                    "updated_by": updated_by,
                },
                note=(
                    f"Cascade memory_items deactivation from tenant "
                    f"{tenant_id} deactivation (PIPEDA P5)"
                ),
                autocommit=False,
            )

            if autocommit:
                self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        return count


    def bulk_soft_deactivate_memory_items_for_agent(
        self,
        tenant_id: str,
        agent_id: str,
        *,
        audit_ctx,
        updated_by: str | None = None,
        autocommit: bool = True,
    ) -> int:
        """Soft-deactivate every active memory_items row for a single agent.

        Called from deactivate_agent (cascade) when an agent is
        deactivated standalone (not as part of a tenant or domain
        cascade). Memory rows scoped to this agent under this tenant
        flip to active=False.

        Returns count deactivated. Always emits one
        ACTION_CASCADE_DEACTIVATE audit row even when count == 0.
        Breakdown by luciel_instance_id is captured in after_json
        for forensic granularity.

        audit_ctx is REQUIRED.
        """
        from sqlalchemy import func
        from app.models.memory import MemoryItem
        from app.repositories.admin_audit_repository import AdminAuditRepository
        from app.models.admin_audit_log import (
            ACTION_CASCADE_DEACTIVATE,
            RESOURCE_MEMORY,
        )

        if audit_ctx is None:
            raise ValueError(
                "bulk_soft_deactivate_memory_items_for_agent requires audit_ctx"
            )

        try:
            breakdown_rows = (
                self.db.query(
                    MemoryItem.luciel_instance_id,
                    func.count().label("row_count"),
                )
                .filter(
                    MemoryItem.tenant_id == tenant_id,
                    MemoryItem.agent_id == agent_id,
                    MemoryItem.active.is_(True),
                )
                .group_by(MemoryItem.luciel_instance_id)
                .all()
            )
            breakdown = [
                {
                    "luciel_instance_id": luciel_instance_id,
                    "count": row_count,
                }
                for (luciel_instance_id, row_count) in breakdown_rows
            ]

            count = (
                self.db.query(MemoryItem)
                .filter(
                    MemoryItem.tenant_id == tenant_id,
                    MemoryItem.agent_id == agent_id,
                    MemoryItem.active.is_(True),
                )
                .update(
                    {"active": False},
                    synchronize_session=False,
                )
            )

            AdminAuditRepository(self.db).record(
                ctx=audit_ctx,
                tenant_id=tenant_id,
                action=ACTION_CASCADE_DEACTIVATE,
                resource_type=RESOURCE_MEMORY,
                resource_pk=None,
                resource_natural_id=None,
                agent_id=agent_id,
                after={
                    "count": count,
                    "scope": "agent",
                    "tenant_id": tenant_id,
                    "agent_id": agent_id,
                    "breakdown": breakdown,
                    "trigger": "agent_deactivate_cascade",
                    "updated_by": updated_by,
                },
                note=(
                    f"Cascade memory_items deactivation from agent "
                    f"{tenant_id}/{agent_id} deactivation (PIPEDA P5)"
                ),
                autocommit=False,
            )

            if autocommit:
                self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        return count


    def bulk_soft_deactivate_memory_items_for_luciel_instance(
        self,
        tenant_id: str,
        luciel_instance_id: int,
        *,
        audit_ctx,
        updated_by: str | None = None,
        autocommit: bool = True,
    ) -> int:
        """Soft-deactivate every active memory_items row for a luciel_instance.

        Called from InstanceService cascade methods when a
        single luciel_instance is deactivated. Memory rows scoped to
        this instance under this tenant flip to active=False.

        Returns count deactivated. Always emits one
        ACTION_CASCADE_DEACTIVATE audit row even when count == 0.
        Breakdown by agent_id is captured in after_json.

        audit_ctx is REQUIRED.
        """
        from sqlalchemy import func
        from app.models.memory import MemoryItem
        from app.repositories.admin_audit_repository import AdminAuditRepository
        from app.models.admin_audit_log import (
            ACTION_CASCADE_DEACTIVATE,
            RESOURCE_MEMORY,
        )

        if audit_ctx is None:
            raise ValueError(
                "bulk_soft_deactivate_memory_items_for_luciel_instance "
                "requires audit_ctx"
            )

        try:
            breakdown_rows = (
                self.db.query(
                    MemoryItem.agent_id,
                    func.count().label("row_count"),
                )
                .filter(
                    MemoryItem.tenant_id == tenant_id,
                    MemoryItem.luciel_instance_id == luciel_instance_id,
                    MemoryItem.active.is_(True),
                )
                .group_by(MemoryItem.agent_id)
                .all()
            )
            breakdown = [
                {
                    "agent_id": agent_id,
                    "count": row_count,
                }
                for (agent_id, row_count) in breakdown_rows
            ]

            count = (
                self.db.query(MemoryItem)
                .filter(
                    MemoryItem.tenant_id == tenant_id,
                    MemoryItem.luciel_instance_id == luciel_instance_id,
                    MemoryItem.active.is_(True),
                )
                .update(
                    {"active": False},
                    synchronize_session=False,
                )
            )

            AdminAuditRepository(self.db).record(
                ctx=audit_ctx,
                tenant_id=tenant_id,
                action=ACTION_CASCADE_DEACTIVATE,
                resource_type=RESOURCE_MEMORY,
                resource_pk=None,
                resource_natural_id=None,
                luciel_instance_id=luciel_instance_id,
                after={
                    "count": count,
                    "scope": "luciel_instance",
                    "tenant_id": tenant_id,
                    "luciel_instance_id": luciel_instance_id,
                    "breakdown": breakdown,
                    "trigger": "luciel_instance_deactivate_cascade",
                    "updated_by": updated_by,
                },
                note=(
                    f"Cascade memory_items deactivation from luciel_instance "
                    f"{luciel_instance_id} (tenant {tenant_id}) deactivation "
                    f"(PIPEDA P5)"
                ),
                autocommit=False,
            )

            if autocommit:
                self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        # Step 28 C10 (P3-Q): return count, not bare return. Pre-fix
        # this method returned None despite the -> int annotation. The
        # current call sites all drop the return value, so this did not
        # cause a runtime failure, but it violates the type contract
        # and breaks any future caller that reads the return value
        # (e.g. structured cascade summary at the route level).
        return count


    def bulk_soft_deactivate_memory_items_for_domain(
        self,
        tenant_id: str,
        domain_id: str,
        *,
        audit_ctx,
        updated_by: str | None = None,
        autocommit: bool = True,
    ) -> int:
        """Arc 5 Path A V2 no-op stub.

        The Domain layer was eliminated per the aggressive-cleanup
        amendment. V2 has no domain-scoped memory cascade; this method
        is preserved only so any straggler caller in tests / verification
        compiles. Returns 0; does NOT emit an audit row (the cascade
        spine routes around this method via the V2-aware
        ``deactivate_tenant_with_cascade``).
        """
        logger.info(
            "bulk_soft_deactivate_memory_items_for_domain: Arc 5 Path A "
            "V2 no-op stub tenant_id=%s domain_id=%s.",
            tenant_id,
            domain_id,
        )
        return 0
    

    def deactivate_tenant_with_cascade(
        self,
        tenant_id: str,
        *,
        audit_ctx,
        luciel_instance_service,
        agent_repo,
        updated_by: str | None = None,
        autocommit: bool = True,
    ) -> bool:
        """Soft-deactivate a tenant and cascade leaf-first to every child.

        Arc 5 B5 -- 12-layer in-function cascade (all in a single
        transaction). The Domain layer (formerly layer 8, domain_configs)
        was REMOVED per the aggressive-cleanup amendment
        (D-arc5-aggressive-cleanup-doctrine-amendment-2026-05-23); the
        V2 Admin → Instance hierarchy has no Domain layer.

        Subscription cancellation is handled UPSTREAM in
        ``app/services/billing_webhook_service.py`` and emits its own
        audit row; this cascade does not touch ``subscriptions`` because
        the Stripe-driven webhook is the source of truth for that layer.
        Total audit-row count for a full tenant teardown is therefore
        13 = 12 in-function + 1 upstream subscription (was 14 pre-B5).

        Canonical cascade order:
          1.  conversations             (Step 30a.2 -- soft-delete + ts)
          2.  identity_claims           (Step 30a.2 -- soft-delete + ts)
          3.  memory_items              (broadest leaf below)
          4.  api_keys
          5.  luciel_instances          (legacy table — dropped at Revision C)
          6.  agents                    (legacy table — dropped at Revision C)
          7.  agent_configs             (legacy)
          8.  scope_assignments         (Step 30a.7 -- privilege revocation)
          9.  user_invites              (Step 30a.7 -- pending-invite revoke)
          10. sessions                  (Step 30a.7 -- session-cookie revoke)
          11. synthetic_orphan_users    (Step 30a.7 -- test-fixture cleanup,
                                         synthetic=True AND zero remaining
                                         active scope_assignments)
          12. tenant_config             (active=False, deactivated_at=now())

        Each in-function layer emits exactly one ``cascade_deactivate``-shape
        audit row per touched row, with the documented exception of layer 10
        (``user_invites``) which uses ``invite_revoked`` for parity with the
        user-driven revoke path. Any step failure rolls back the entire
        cascade -- no partial deactivation is possible.

        audit_ctx is REQUIRED. Tenant deactivation is the most privileged
        mutation in the platform; an audit trail is non-negotiable.

        luciel_instance_service / agent_repo are required injected
        dependencies (mirrors the deactivate_domain pattern). ApiKeyService
        has no FastAPI dep factory and is instantiated inline; it shares
        self.db so transactional atomicity is preserved.

        Returns True if the tenant was found and deactivated. Returns False
        if the tenant config row does not exist. Idempotent on re-run --
        children already inactive are skipped (each layer filters on the
        relevant active/pending predicate).

        autocommit=True by default for standalone callers (admin route).
        Future callers that wrap this in a larger transaction (Stripe
        billing webhook, GDPR deletion endpoint) can pass autocommit=False.

        Step 30a.2 -- closed
        D-cancellation-cascade-incomplete-conversations-claims-2026-05-14:
        the cascade now visits ``conversations`` and ``identity_claims``
        (both have ``tenant_id`` + ``active`` columns and were unreachable
        in the old 7-layer walk). And the tenant_config step itself stamps
        ``deactivated_at = now()`` so the retention worker can compute the
        90d purge cutoff.

        Step 30a.7 -- closes the cascade-integrity + privilege-revocation
        umbrella ``D-tenant-cascade-privilege-revocation-hardening-2026-05-20``
        and its six siblings:
          * ``D-cascade-missing-scope-assignments-layer-2026-05-20``
          * ``D-cascade-missing-user-invites-revocation-2026-05-20``
          * ``D-cascade-missing-sessions-revocation-2026-05-20``
          * ``D-cascade-missing-synthetic-users-orphan-layer-2026-05-20``
          * ``D-cascade-comment-drift-9-layer-claim-vs-13-layer-reality-2026-05-20``
          * ``D-rbac-single-gate-tenant-active-belt-and-suspenders-2026-05-20``
            (paired defence-in-depth at app/middleware/session_cookie_auth.py)

        Any future cascade-layer extension MUST update four surfaces in
        the same diff (four-surface symmetry doctrine):
          (a) this docstring enumeration,
          (b) the in-body ``# --- N. <table>`` comment,
          (c) CANONICAL_RECAP §14 cascade-layer matrix,
          (d) tests/services/test_cascade_includes_all_privilege_layers.py
              (the executable mirror of this docstring enumeration).

        NOTE on ``messages``: still no ``active`` column; handled at
        retention-time hard-purge via ``hard_delete_tenant_after_retention``
        and SQL FK CASCADE on ``messages.session_id``. ``sessions`` itself
        is now layer 11 above (status='active' -> status='revoked').
        """
        from sqlalchemy import func

        from app.models.agent_config import AgentConfig
        from app.models.conversation import Conversation
        # DomainConfig: REMOVED (Arc 5 Path A) - V2 has no Domain layer
        from app.models.identity_claim import IdentityClaim
        # Step 30a.7 -- privilege-revocation layer models.
        from app.models.scope_assignment import EndReason, ScopeAssignment
        from app.models.session import SessionModel
        from app.models.user import User
        from app.models.user_invite import InviteStatus, UserInvite
        from app.services.api_key_service import ApiKeyService
        from app.repositories.admin_audit_repository import AdminAuditRepository
        from app.models.admin_audit_log import (
            ACTION_CASCADE_DEACTIVATE,
            ACTION_DEACTIVATE,
            ACTION_INVITE_REVOKED,
            RESOURCE_AGENT,
            RESOURCE_CONVERSATION,
            RESOURCE_DOMAIN,
            RESOURCE_IDENTITY_CLAIM,
            RESOURCE_SCOPE_ASSIGNMENT,
            RESOURCE_SESSION,
            RESOURCE_TENANT,
            RESOURCE_USER,
            RESOURCE_USER_INVITE,
        )

        if audit_ctx is None:
            raise ValueError(
                "deactivate_tenant_with_cascade requires audit_ctx -- "
                "tenant deactivation must always be audited."
            )

        tenant = self.get_tenant_config(tenant_id)
        if tenant is None:
            return False

        was_active = bool(tenant.active)

        try:
            # --- 1. conversations cascade (NEW Step 30a.2) -------------
            # Soft-deactivate every active conversation under this tenant.
            # Stamp deactivated_at = now() in the same UPDATE so future
            # per-conversation retention queries have the timestamp.
            # Uses Conversation directly (no separate repo method) for
            # symmetry with the agent_configs / domain_configs inline
            # cascades below; the table is conceptually identical in
            # treatment (soft-delete + audit row + count).
            affected_conversations = (
                self.db.query(Conversation.id)
                .filter(
                    Conversation.tenant_id == tenant_id,
                    Conversation.active.is_(True),
                )
                .all()
            )
            conv_ids = [str(pk) for (pk,) in affected_conversations]
            conv_updated = (
                self.db.query(Conversation)
                .filter(
                    Conversation.tenant_id == tenant_id,
                    Conversation.active.is_(True),
                )
                .update(
                    {
                        Conversation.active: False,
                        Conversation.deactivated_at: func.now(),
                    },
                    synchronize_session=False,
                )
            )
            if conv_updated:
                AdminAuditRepository(self.db).record(
                    ctx=audit_ctx,
                    tenant_id=tenant_id,
                    action=ACTION_CASCADE_DEACTIVATE,
                    resource_type=RESOURCE_CONVERSATION,
                    resource_pk=None,
                    resource_natural_id=None,
                    after={
                        "count": int(conv_updated),
                        "affected_conversation_ids": conv_ids,
                        "table": "conversations",
                        "trigger": "tenant_deactivate",
                    },
                    note=(
                        f"Cascade conversations from tenant "
                        f"{tenant_id} deactivation (Step 30a.2)"
                    ),
                    autocommit=False,
                )

            # --- 2. identity_claims cascade (NEW Step 30a.2) -----------
            # Soft-deactivate every active identity_claim under this
            # tenant. claim_value is PII (email / phone) so this row
            # must be honored under PIPEDA Principle 5. Audit row
            # records affected count + claim row pks (NOT claim_value,
            # to avoid duplicating PII into the audit chain). The
            # underlying row itself stays in the DB until retention
            # hard-purge -- soft-delete is the PIPEDA-respecting
            # "limited use" shape, hard-delete is the "limited
            # retention" shape.
            affected_claims = (
                self.db.query(IdentityClaim.id)
                .filter(
                    IdentityClaim.tenant_id == tenant_id,
                    IdentityClaim.active.is_(True),
                )
                .all()
            )
            claim_pks = [str(pk) for (pk,) in affected_claims]
            claims_updated = (
                self.db.query(IdentityClaim)
                .filter(
                    IdentityClaim.tenant_id == tenant_id,
                    IdentityClaim.active.is_(True),
                )
                .update(
                    {
                        IdentityClaim.active: False,
                        IdentityClaim.deactivated_at: func.now(),
                    },
                    synchronize_session=False,
                )
            )
            if claims_updated:
                AdminAuditRepository(self.db).record(
                    ctx=audit_ctx,
                    tenant_id=tenant_id,
                    action=ACTION_CASCADE_DEACTIVATE,
                    resource_type=RESOURCE_IDENTITY_CLAIM,
                    resource_pk=None,
                    resource_natural_id=None,
                    after={
                        "count": int(claims_updated),
                        "affected_claim_pks": claim_pks,
                        "table": "identity_claims",
                        "trigger": "tenant_deactivate",
                    },
                    note=(
                        f"Cascade identity_claims from tenant "
                        f"{tenant_id} deactivation (Step 30a.2)"
                    ),
                    autocommit=False,
                )

            # --- 3. memory_items cascade (broadest leaf) ---------------
            # Service method emits its own RESOURCE_KNOWLEDGE audit row.
            self.bulk_soft_deactivate_memory_items_for_tenant(
                tenant_id=tenant_id,
                audit_ctx=audit_ctx,
                updated_by=updated_by,
                autocommit=False,
            )

            # --- 4. api_keys cascade -----------------------------------
            # ApiKeyService instantiated inline (no FastAPI dep factory
            # exists for it). Shares self.db -- transaction atomic.
            # Service method emits its own RESOURCE_API_KEY audit rows.
            ApiKeyService(self.db).deactivate_all_for_tenant(
                tenant_id=tenant_id,
                audit_ctx=audit_ctx,
                autocommit=False,
            )

            # --- 5. luciel_instances cascade (all scope levels) --------
            # Repo method emits its own RESOURCE_LUCIEL_INSTANCE audit rows.
            luciel_instance_service.repo.deactivate_all_for_tenant(
                tenant_id=tenant_id,
                updated_by=updated_by,
                audit_ctx=audit_ctx,
                autocommit=False,
            )

            # --- 6. agents (new-table) cascade -------------------------
            # Repo method emits its own RESOURCE_AGENT audit rows.
            agent_repo.deactivate_all_for_tenant(
                tenant_id=tenant_id,
                updated_by=updated_by,
                audit_ctx=audit_ctx,
                autocommit=False,
            )

            # --- 7. agent_configs (legacy) cascade (inline) ------------
            affected_agent_configs = (
                self.db.query(AgentConfig.id, AgentConfig.agent_id)
                .filter(
                    AgentConfig.tenant_id == tenant_id,
                    AgentConfig.active.is_(True),
                )
                .all()
            )
            ac_pks = [pk for pk, _ in affected_agent_configs]
            ac_ids = [nid for _, nid in affected_agent_configs]
            ac_updated = (
                self.db.query(AgentConfig)
                .filter(
                    AgentConfig.tenant_id == tenant_id,
                    AgentConfig.active.is_(True),
                )
                .update(
                    {
                        AgentConfig.active: False,
                        AgentConfig.updated_by: updated_by,
                    },
                    synchronize_session=False,
                )
            )
            if ac_updated:
                AdminAuditRepository(self.db).record(
                    ctx=audit_ctx,
                    tenant_id=tenant_id,
                    action=ACTION_CASCADE_DEACTIVATE,
                    resource_type=RESOURCE_AGENT,
                    resource_pk=None,
                    resource_natural_id=None,
                    after={
                        "count": int(ac_updated),
                        "affected_pks": ac_pks,
                        "affected_agent_ids": ac_ids,
                        "table": "agent_configs",
                        "trigger": "tenant_deactivate",
                    },
                    note=(
                        f"Cascade legacy agent_configs from tenant "
                        f"{tenant_id} deactivation"
                    ),
                    autocommit=False,
                )

            # --- 8. (removed at Arc 5 B5) -------------------------------
            # Domain layer was eliminated per the aggressive-cleanup
            # amendment (D-arc5-aggressive-cleanup-doctrine-amendment-
            # 2026-05-23). Previously this cascade visited domain_configs
            # at layer 8; the table itself is dropped at Revision C and
            # the V2 Admin → Instance hierarchy has no Domain layer to
            # cascade. Layer numbering: 7 → 8 (scope_assignments) skipping
            # the former domain_configs slot.

            # --- 8. scope_assignments cascade (Step 30a.7) -------------
            # Privilege-revocation layer. scope_assignments is the single
            # source of truth for tenant binding (post Step-30a.5 invitee
            # resolver fix); leaving rows active=True against a soft-
            # deleted tenant lets the RBAC resolver return a binding for
            # a dead tenant. Stamp active=False + ended_at=now() +
            # ended_reason=DEACTIVATED + ended_by_api_key_id=NULL (NULL
            # because this is a system-initiated cascade end, not an
            # API-key-initiated promotion/demotion/departure). Capture
            # affected ids/user_ids first so the synthetic-orphan-users
            # layer (12) below can read them.
            affected_scope_assignments = (
                self.db.query(ScopeAssignment.id, ScopeAssignment.user_id)
                .filter(
                    ScopeAssignment.tenant_id == tenant_id,
                    ScopeAssignment.active.is_(True),
                )
                .all()
            )
            sa_pks = [pk for pk, _ in affected_scope_assignments]
            sa_user_ids = [uid for _, uid in affected_scope_assignments]
            sa_updated = (
                self.db.query(ScopeAssignment)
                .filter(
                    ScopeAssignment.tenant_id == tenant_id,
                    ScopeAssignment.active.is_(True),
                )
                .update(
                    {
                        ScopeAssignment.active: False,
                        ScopeAssignment.ended_at: func.now(),
                        ScopeAssignment.ended_reason: EndReason.DEACTIVATED,
                        ScopeAssignment.ended_by_api_key_id: None,
                    },
                    synchronize_session=False,
                )
            )
            if sa_updated:
                AdminAuditRepository(self.db).record(
                    ctx=audit_ctx,
                    tenant_id=tenant_id,
                    action=ACTION_CASCADE_DEACTIVATE,
                    resource_type=RESOURCE_SCOPE_ASSIGNMENT,
                    resource_pk=None,
                    resource_natural_id=None,
                    after={
                        "count": int(sa_updated),
                        # Step 30a.7 caller-hygiene
                        # (D-jsonb-uuid-serializer-engine-default-2026-05-20):
                        # scope_assignments.id is uuid.UUID; coerce to str at
                        # the call site for explicit traceability, even though
                        # the engine-level json_serializer hook would catch it.
                        "affected_pks": [str(pk) for pk in sa_pks],
                        "affected_user_ids": [str(uid) for uid in sa_user_ids],
                        "table": "scope_assignments",
                        "ended_reason": EndReason.DEACTIVATED.value,
                        "trigger": "tenant_deactivate",
                    },
                    note=(
                        f"Cascade scope_assignments from tenant "
                        f"{tenant_id} deactivation (Step 30a.7)"
                    ),
                    autocommit=False,
                )

            # --- 9. user_invites cascade (Step 30a.7) ------------------
            # Pending invites against a soft-deleted tenant are by
            # definition revocable -- the JWT is still redeemable until
            # expires_at unless we flip status='revoked' here. Reuse
            # ACTION_INVITE_REVOKED (NOT cascade_deactivate) so downstream
            # audit-search tooling that already keys on the user-driven
            # revoke action sees cascade-triggered revokes uniformly.
            # ended_by_api_key_id-equivalent column is revoked_by_api_key_id
            # (per UserInvite model); NULL for system-initiated cascade.
            affected_invites = (
                self.db.query(UserInvite.id)
                .filter(
                    UserInvite.tenant_id == tenant_id,
                    UserInvite.status == InviteStatus.PENDING,
                )
                .all()
            )
            ui_pks = [pk for (pk,) in affected_invites]
            ui_updated = (
                self.db.query(UserInvite)
                .filter(
                    UserInvite.tenant_id == tenant_id,
                    UserInvite.status == InviteStatus.PENDING,
                )
                .update(
                    {
                        UserInvite.status: InviteStatus.REVOKED,
                        UserInvite.revoked_at: func.now(),
                    },
                    synchronize_session=False,
                )
            )
            if ui_updated:
                AdminAuditRepository(self.db).record(
                    ctx=audit_ctx,
                    tenant_id=tenant_id,
                    action=ACTION_INVITE_REVOKED,
                    resource_type=RESOURCE_USER_INVITE,
                    resource_pk=None,
                    resource_natural_id=None,
                    after={
                        "count": int(ui_updated),
                        # Step 30a.7 caller-hygiene
                        # (D-jsonb-uuid-serializer-engine-default-2026-05-20):
                        # user_invites.id is uuid.UUID; coerce at call site.
                        "affected_pks": [str(pk) for pk in ui_pks],
                        "table": "user_invites",
                        "revoked_via": "tenant_deactivate_cascade",
                        "trigger": "tenant_deactivate",
                    },
                    note=(
                        f"Cascade revoke pending user_invites from "
                        f"tenant {tenant_id} deactivation (Step 30a.7)"
                    ),
                    autocommit=False,
                )

            # --- 10. sessions cascade (Step 30a.7) ---------------------
            # session.status='active' is the runtime authenticator for
            # every cookie-bearing request. Flip to 'revoked' in the same
            # transaction as tenant.active=False so there is no race
            # window where a session-cookied request authenticates
            # against a soft-deleted tenant. sessions has no revoked_at
            # column; the audit-row timestamp is the source of truth for
            # "when was this session revoked". The middleware-level
            # belt-and-suspenders gate at session_cookie_auth.py
            # (Step 30a.7 sibling B1) provides defence-in-depth -- even
            # if a row flip were missed, the middleware rejects the
            # request based on tenant.active.
            affected_sessions = (
                self.db.query(SessionModel.id)
                .filter(
                    SessionModel.tenant_id == tenant_id,
                    SessionModel.status == "active",
                )
                .all()
            )
            sess_pks = [pk for (pk,) in affected_sessions]
            sess_updated = (
                self.db.query(SessionModel)
                .filter(
                    SessionModel.tenant_id == tenant_id,
                    SessionModel.status == "active",
                )
                .update(
                    {SessionModel.status: "revoked"},
                    synchronize_session=False,
                )
            )
            if sess_updated:
                AdminAuditRepository(self.db).record(
                    ctx=audit_ctx,
                    tenant_id=tenant_id,
                    action=ACTION_CASCADE_DEACTIVATE,
                    resource_type=RESOURCE_SESSION,
                    resource_pk=None,
                    resource_natural_id=None,
                    after={
                        "count": int(sess_updated),
                        "affected_pks": [str(pk) for pk in sess_pks],
                        "table": "sessions",
                        "previous_status": "active",
                        "new_status": "revoked",
                        "trigger": "tenant_deactivate",
                    },
                    note=(
                        f"Cascade revoke active sessions from tenant "
                        f"{tenant_id} deactivation (Step 30a.7)"
                    ),
                    autocommit=False,
                )

            # --- 11. synthetic_orphan_users cascade (Step 30a.7) -------
            # users is a GLOBAL table -- users have no tenant_id column
            # (binding lives in scope_assignments). A blanket
            # users.active=False on cascade would wrongly deactivate real
            # users who hold scope on multiple tenants. Narrow case where
            # deactivation IS correct: synthetic=True users whose ONLY
            # active scope_assignment was for this tenant (and was just
            # flipped in layer 9 above). Real users (synthetic=False) are
            # NEVER deactivated by the cascade -- real-user deactivation
            # is a separate operator-initiated path. Uses sa_user_ids
            # captured in layer 9 above as the candidate set.
            synthetic_deactivated_user_ids: list[str] = []
            if sa_user_ids:
                # Step 1: filter to synthetic users only.
                synthetic_candidates = (
                    self.db.query(User.id)
                    .filter(
                        User.id.in_(sa_user_ids),
                        User.synthetic.is_(True),
                        User.active.is_(True),
                    )
                    .all()
                )
                synthetic_candidate_ids = [uid for (uid,) in synthetic_candidates]
                # Step 2: per-candidate, only flip if zero remaining
                # active scope_assignments on ANY other tenant.
                for user_id in synthetic_candidate_ids:
                    remaining = (
                        self.db.query(ScopeAssignment.id)
                        .filter(
                            ScopeAssignment.user_id == user_id,
                            ScopeAssignment.active.is_(True),
                        )
                        .count()
                    )
                    if remaining == 0:
                        (
                            self.db.query(User)
                            .filter(User.id == user_id)
                            .update(
                                {User.active: False},
                                synchronize_session=False,
                            )
                        )
                        synthetic_deactivated_user_ids.append(str(user_id))
                        AdminAuditRepository(self.db).record(
                            ctx=audit_ctx,
                            tenant_id=tenant_id,
                            action=ACTION_CASCADE_DEACTIVATE,
                            resource_type=RESOURCE_USER,
                            resource_pk=None,
                            resource_natural_id=str(user_id),
                            after={
                                "user_id": str(user_id),
                                "synthetic": True,
                                "remaining_active_scopes": 0,
                                "table": "users",
                                "trigger": "tenant_deactivate",
                            },
                            note=(
                                f"Cascade deactivate synthetic orphan user "
                                f"{user_id} from tenant {tenant_id} "
                                f"deactivation (Step 30a.7)"
                            ),
                            autocommit=False,
                        )

            # --- 12. tenant_config itself ------------------------------
            # Step 30a.2: also stamp deactivated_at = now() so the
            # retention worker can compute the 90d purge cutoff. Only
            # set when was_active=True (idempotent re-runs don't
            # re-stamp -- preserves the original deactivation moment).
            tenant.active = False
            if was_active:
                tenant.deactivated_at = func.now()
            if updated_by is not None:
                tenant.updated_by = updated_by

            if was_active:
                AdminAuditRepository(self.db).record(
                    ctx=audit_ctx,
                    tenant_id=tenant_id,
                    action=ACTION_DEACTIVATE,
                    resource_type=RESOURCE_TENANT,
                    resource_pk=tenant.id,
                    resource_natural_id=tenant_id,
                    before={"active": True},
                    after={
                        "active": False,
                        # "deactivated_at" left as a server-stamped
                        # marker; the actual timestamp lives in the row
                        # itself. Including "now" here would create a
                        # second source of truth that could drift.
                    },
                    note=(
                        f"Tenant {tenant_id} deactivated with full cascade "
                        f"(PIPEDA P5 retention; Arc 5 B5 12-layer "
                        f"in-function + 1 upstream subscription; Domain "
                        f"layer removed per aggressive-cleanup amendment)"
                    ),
                    autocommit=False,
                )

            if autocommit:
                self.db.commit()
                self.db.refresh(tenant)
        except Exception:
            self.db.rollback()
            raise

        return True

    # ---------------------------------------------------------------
    # Step 30a.2 — retention hard-purge (PIPEDA Principle 5)
    # ---------------------------------------------------------------
    #
    # Companion to deactivate_tenant_with_cascade. The cascade does
    # soft-deletion (active=false + deactivated_at=now); this method
    # does HARD-deletion of every row scoped to a tenant after the
    # 90-day retention window has elapsed.
    #
    # Called by the nightly Celery beat job in
    # app/worker/tasks/retention.py::run_retention_purge.
    #
    # Order matters: we delete leaf-first to satisfy the FK RESTRICT
    # constraints that protect tenant_configs.tenant_id from cascade-
    # delete. ``conversations.tenant_id`` and
    # ``identity_claims.tenant_id`` both have ON DELETE RESTRICT to
    # tenant_configs.tenant_id, so we MUST delete them before the
    # parent tenant_configs row. Same for any other FK-RESTRICT
    # children that may exist; we delete them all explicitly rather
    # than relying on FK behavior so the row-count audit is honest.

    def hard_delete_tenant_after_retention(
        self,
        tenant_id: str,
        *,
        retention_window_days: int = 90,
    ) -> dict[str, int]:
        """Hard-delete every row scoped to ``tenant_id`` after retention.

        Re-verifies the retention predicate (active=false AND
        deactivated_at < now - N days) inside this transaction as an
        idempotency guard. If the row is not eligible (already purged,
        re-activated, or insufficient retention age), returns an empty
        dict and makes no DB changes.

        Order of deletion (leaf-first, RESTRICT-safe):
           1. messages          (via sessions FK CASCADE -- implicit)
           2. sessions          WHERE tenant_id=:tid
           3. conversations     WHERE tenant_id=:tid
           4. identity_claims   WHERE tenant_id=:tid
           5. memory_items      WHERE tenant_id=:tid
           6. api_keys          WHERE tenant_id=:tid
           7. luciel_instances  WHERE tenant_id=:tid
           8. agents            WHERE tenant_id=:tid
           9. agent_configs     WHERE tenant_id=:tid
          10. domain_configs    WHERE tenant_id=:tid
          11. tenant_configs    WHERE tenant_id=:tid
          12. AdminAuditLog row recording the purge (action=
              ACTION_TENANT_HARD_PURGED) with per-table row-count map.

        Subscriptions are intentionally NOT purged -- they carry
        billing history needed for tax/accounting retention which
        has its own clock.

        Returns a dict mapping table name -> row count deleted.
        Empty dict means the row was not eligible (idempotency guard
        fired). The caller (Celery task) is responsible for the
        outer transaction commit; this method does NOT commit -- it
        runs in the caller's transaction so the audit row + DELETEs
        are atomic.

        Raises if the tenant_configs row exists but is still active
        or has NULL deactivated_at -- those are safety-net conditions
        that should never happen if the cascade is the only writer.
        """
        from datetime import datetime, timedelta, timezone

        from sqlalchemy import text as sql_text

        from app.models.admin_audit_log import (
            ACTION_TENANT_HARD_PURGED,
            RESOURCE_TENANT,
        )
        from app.models.admin import AdminConfig as TenantConfig
        from app.repositories.admin_audit_repository import AdminAuditRepository

        # ---- Idempotency guard: re-verify retention predicate ----
        # This is intentionally done inside the same transaction as
        # the DELETEs (not as a pre-flight) so a concurrent reactivate
        # cannot race past us.
        tenant = self.get_tenant_config(tenant_id)
        if tenant is None:
            # Already hard-purged on a prior run, or never existed.
            return {}

        if tenant.active:
            raise RuntimeError(
                f"hard_delete_tenant_after_retention called on ACTIVE "
                f"tenant {tenant_id!r} -- this should never happen. "
                f"The cascade is the only writer of tenant_configs."
                f"active=false; reactivation must roll back deactivated_at."
            )

        if tenant.deactivated_at is None:
            raise RuntimeError(
                f"hard_delete_tenant_after_retention called on tenant "
                f"{tenant_id!r} with NULL deactivated_at -- this row "
                f"was deactivated before Step 30a.2 and is excluded "
                f"from automated purge by design. Manual purge only."
            )

        cutoff = datetime.now(timezone.utc) - timedelta(
            days=retention_window_days
        )
        # tenant.deactivated_at is timezone-aware (timestamptz) so the
        # comparison is well-defined; mixing tz-aware and naive would
        # raise TypeError, which is the correct behavior.
        if tenant.deactivated_at >= cutoff:
            # Eligible per the scan but raced -- another beat or a
            # bug shrank the window. Defensive skip.
            return {}

        # ---- Hard-delete chain ----
        row_counts: dict[str, int] = {}

        # Each DELETE returns an estimated row count via .rowcount;
        # for some dialects this is -1 when the driver can't tell.
        # We coerce to int and store; the audit row reflects what we
        # actually saw, even if -1.
        def _delete(sql: str) -> int:
            res = self.db.execute(sql_text(sql), {"tid": tenant_id})
            return int(res.rowcount or 0)

        # 1. messages cascade via SQL FK on sessions (implicit). We
        #    don't issue a DELETE here -- step 2's DELETE FROM sessions
        #    cascades to messages via ON DELETE CASCADE. We record the
        #    pre-count for the audit row's row-count map though.
        pre_msg_count = int(
            self.db.execute(
                sql_text(
                    "SELECT COUNT(*) FROM messages m "
                    "JOIN sessions s ON s.id = m.session_id "
                    "WHERE s.tenant_id = :tid"
                ),
                {"tid": tenant_id},
            ).scalar()
            or 0
        )
        row_counts["messages"] = pre_msg_count

        # 2. sessions (cascades to messages via FK)
        row_counts["sessions"] = _delete(
            "DELETE FROM sessions WHERE tenant_id = :tid"
        )

        # 3. conversations
        row_counts["conversations"] = _delete(
            "DELETE FROM conversations WHERE tenant_id = :tid"
        )

        # 4. identity_claims
        row_counts["identity_claims"] = _delete(
            "DELETE FROM identity_claims WHERE tenant_id = :tid"
        )

        # 5. memory_items
        row_counts["memory_items"] = _delete(
            "DELETE FROM memory_items WHERE tenant_id = :tid"
        )

        # 6. api_keys
        row_counts["api_keys"] = _delete(
            "DELETE FROM api_keys WHERE tenant_id = :tid"
        )

        # 7. luciel_instances
        row_counts["luciel_instances"] = _delete(
            "DELETE FROM luciel_instances WHERE tenant_id = :tid"
        )

        # 8. agents (new-table, Step 24.5)
        row_counts["agents"] = _delete(
            "DELETE FROM agents WHERE tenant_id = :tid"
        )

        # 9. agent_configs (legacy)
        row_counts["agent_configs"] = _delete(
            "DELETE FROM agent_configs WHERE tenant_id = :tid"
        )

        # 10. domain_configs: REMOVED at Arc 5 Path A (V2 has no Domain
        # layer). The domain_configs table is dropped at Revision C; this
        # cascade no longer touches it. Row count preserved as 0 for
        # audit-row schema stability.
        row_counts["domain_configs"] = 0

        # 11. admins (the parent row itself).
        # V2: tenant_configs table is renamed to admins at Revision C
        # via rename; same primary key (tenant_id → id). During the
        # transition before Revision C lands, the table is still named
        # tenant_configs on disk; after Revision C, it is admins. Try
        # admins first, fall back to tenant_configs.
        try:
            row_counts["admins"] = _delete(
                "DELETE FROM admins WHERE id = :tid"
            )
        except Exception:
            row_counts["admins"] = 0
        if row_counts["admins"] == 0:
            try:
                row_counts["tenant_configs"] = _delete(
                    "DELETE FROM tenant_configs WHERE tenant_id = :tid"
                )
            except Exception:
                row_counts["tenant_configs"] = 0
        else:
            row_counts["tenant_configs"] = 0

        # 12. Audit row -- write to AdminAuditLog with full row-count
        # manifest. The audit row uses the resource_natural_id field
        # to preserve tenant_id as a searchable string AFTER the
        # tenant_configs row itself is gone; the row_hash chain stays
        # walkable because AdminAuditLog rows are never FK'd to
        # tenant_configs.
        # Note: audit row is written through AuditContext.system()
        # because this is a background-task action with no HTTP caller.
        # The system() factory tags actor_permissions=('system',) and
        # actor_tenant_id=SYSTEM_ACTOR_TENANT so retention rows are
        # distinguishable from worker-task rows (which use ('worker',)).
        from app.repositories.admin_audit_repository import AuditContext

        system_ctx = AuditContext.system(label="retention_worker")
        AdminAuditRepository(self.db).record(
            ctx=system_ctx,
            tenant_id=tenant_id,
            action=ACTION_TENANT_HARD_PURGED,
            resource_type=RESOURCE_TENANT,
            resource_pk=None,
            resource_natural_id=tenant_id,
            after={
                "row_counts": row_counts,
                "retention_window_days": retention_window_days,
                "trigger": "retention_worker",
            },
            note=(
                f"Hard-purge of tenant {tenant_id} after "
                f"{retention_window_days}d retention (PIPEDA P5)"
            ),
            autocommit=False,
        )

        return row_counts

    # ---------------------------------------------------------------
    # Step 30a.1 — tier/scope guard
    # ---------------------------------------------------------------
    #
    # Called from the POST /admin/luciel-instances route (the ONE
    # self-serve creation chokepoint) BEFORE InstanceService.
    # create_instance. Service-layer enforcement is intentional:
    #
    #   * the schema layer cannot know the caller's active subscription
    #     (subscriptions are loaded by tenant_id from a DB lookup);
    #   * the policy layer (ScopePolicy) checks API-key authority, not
    #     billing entitlement, and we want those concerns separate.
    #
    # Outcomes (Arc 5 Path A — V2 collapsed):
    #   * tenant has no active subscription → 402 (we fail closed so
    #     sales-assisted tenants without a subscription row cannot use
    #     the self-serve route at all — they should call admin paths
    #     instead).
    #   * tenant already at instance_count_cap → 402
    #   * otherwise → silent.

    def _enforce_tier_scope(
        self,
        *,
        tenant_id: str,
        requested_level: str | None = None,
        **_legacy_kwargs,
    ) -> None:
        """V2 cap-enforcement guard. Asserts the Admin is entitled to
        create another LucielInstance, sourced from the active
        Subscription if present and from ``Admin.tier`` +
        ``TIER_INSTANCE_CAPS`` otherwise.

        Raises ``TierScopeViolationError`` (mapped to 402 by the route
        layer). On success returns silently.

        Arc 9 C22 -- Free-tier fix
        --------------------------
        Per the V2 entitlement doctrine (app/models/subscription.py
        ~L93-118 and CANONICAL_RECAP §6.5 founder-locks): Free admins
        have NO Subscription row at all (lazy-create on upgrade per
        Gap 1 lock) but they DO have an entitlement of
        ``TIER_INSTANCE_CAPS['free'] == 1`` instance. The previous
        version of this guard returned ``REASON_NO_ACTIVE_SUBSCRIPTION``
        for every Free user, contradicting the published tier matrix.

        The fix: when no Subscription row exists, look up the Admin
        row's ``tier`` and use the static ``TIER_INSTANCE_CAPS`` map
        as the cap source. This keeps Subscription as the canonical
        source for paid tiers (so Stripe-driven changes still flow
        through one place) while admitting Free into the same gate.
        If the Admin row itself is missing or has a tier we don't
        recognise, we fail closed -- defence in depth against
        unsynchronised provisioning.
        """
        # Local imports keep AdminService importable from contexts that
        # don't have the LucielInstance / Subscription stack loaded.
        from app.models.admin import Admin
        from app.models.subscription import (
            Subscription,
            TIER_FREE,
            TIER_INSTANCE_CAPS,
        )
        from app.repositories.instance_repository import (
            InstanceRepository,
        )
        from app.services.instance_service import TierScopeViolationError

        sub: Subscription | None = (
            self.db.query(Subscription)
            .filter(
                Subscription.tenant_id == tenant_id,
                Subscription.active.is_(True),
            )
            .order_by(Subscription.id.desc())
            .first()
        )

        cap: int | None
        if sub is not None:
            # Paid tier path: Subscription row is canonical. cap=0 means
            # "unmetered" (sales-assisted backfills); cap>0 enforces.
            raw_cap = sub.instance_count_cap
            cap = None if raw_cap is None or int(raw_cap) == 0 else int(raw_cap)
        else:
            # No Subscription -- look up the Admin row's tier and read
            # the cap from the static entitlement table. Free admins
            # legitimately land here per the V2 lazy-create doctrine;
            # paid admins SHOULD have a Subscription row by the time
            # the post-checkout provisioning leg completes.
            admin_row = (
                self.db.query(Admin)
                .filter(Admin.id == tenant_id)
                .first()
            )
            if admin_row is None:
                # Genuinely no Admin row -- fail closed. This is the
                # only path that should ever raise NO_ACTIVE_SUBSCRIPTION
                # for a self-serve request in V2: the user logged in
                # with a session cookie but the tenant they resolved
                # to has been hard-deleted or never provisioned.
                raise TierScopeViolationError(
                    f"Tenant {tenant_id!r} has no Admin row and no "
                    f"active subscription; cannot create LucielInstance.",
                    reason=TierScopeViolationError.REASON_NO_ACTIVE_SUBSCRIPTION,
                )
            tier = admin_row.tier
            if tier not in TIER_INSTANCE_CAPS:
                # Unrecognised tier -- fail closed. The only way to
                # reach this branch is a forward-compat regression
                # (someone added a tier string to admins.tier without
                # updating TIER_INSTANCE_CAPS); the right answer is
                # a tight 402 with a clear server log, not a free pass.
                logger.warning(
                    "_enforce_tier_scope: tenant=%s has unknown tier=%r; "
                    "failing closed pending entitlement-map update.",
                    tenant_id, tier,
                )
                raise TierScopeViolationError(
                    f"Tenant {tenant_id!r} tier={tier!r} has no entitlement "
                    f"mapping; cannot create LucielInstance.",
                    reason=TierScopeViolationError.REASON_NO_ACTIVE_SUBSCRIPTION,
                )
            cap = TIER_INSTANCE_CAPS[tier]  # 1 for free, None for unlimited

        # Arc 5 Path A -- V2 has no scope hierarchy below the Admin; the
        # legacy "scope_level permitted by tier" check is dropped here.
        # Only the cap-enforcement guard remains.
        if cap is not None:
            used = InstanceRepository(self.db).count_active_for_admin(tenant_id)
            if used >= cap:
                raise TierScopeViolationError(
                    f"Tenant {tenant_id!r} has reached its instance_count_cap="
                    f"{cap} (currently {used} active LucielInstances). "
                    f"Deactivate an existing Luciel or upgrade your tier.",
                    reason=TierScopeViolationError.REASON_CAP_EXCEEDED,
                )
        # else: cap=None means "unmetered" (Enterprise tier, sales-assisted
        # backfills). No enforcement at this layer.