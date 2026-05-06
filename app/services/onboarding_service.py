"""
Tenant onboarding service.

Orchestrates the atomic creation of everything a new tenant needs:
  1. TenantConfig
  2. Default DomainConfig
  3. Default RetentionPolicies (PIPEDA-compliant)
  4. First API key

All writes happen in a single DB transaction — if any step fails,
everything rolls back. No partial tenants.

Step 23.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.admin_audit_log import (
    ACTION_CREATE,
    RESOURCE_API_KEY,
    RESOURCE_DOMAIN,
    RESOURCE_RETENTION_POLICY,
    RESOURCE_TENANT,
)
from app.models.domain_config import DomainConfig
from app.models.retention import RetentionPolicy
from app.models.tenant import TenantConfig
from app.repositories.admin_audit_repository import AdminAuditRepository, AuditContext
from app.services.admin_service import AdminService
from app.services.api_key_service import ApiKeyService

logger = logging.getLogger(__name__)

# PIPEDA-compliant default retention categories
DEFAULT_RETENTION_CATEGORIES = [
    "sessions",
    "messages",
    "memory_items",
    "traces",
    "knowledge_embeddings",
]


class OnboardingService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.admin = AdminService(db)
        self.api_key_service = ApiKeyService(db)

    def onboard_tenant(
        self,
        *,
        tenant_id: str,
        display_name: str,
        description: str | None = None,
        escalation_contact: str | None = None,
        system_prompt_additions: str | None = None,
        default_domain_id: str = "general",
        default_domain_display_name: str = "General Assistant",
        default_domain_description: str | None = "Default domain created during onboarding",
        api_key_display_name: str = "Default onboarding key",
        api_key_permissions: list[str] | None = None,
        api_key_rate_limit: int = 1000,
        retention_days_sessions: int = 90,
        retention_days_messages: int = 90,
        retention_days_memory_items: int = 365,
        retention_days_traces: int = 30,
        retention_days_knowledge: int = 0,
        created_by: str | None = None,
        audit_ctx: AuditContext | None = None,
    ) -> dict:
        """
        Create everything a tenant needs in one atomic transaction.

        Returns a dict with: tenant, default_domain, api_key, raw_api_key,
        retention_policies.

        Raises ValueError if tenant already exists.
        """
        if api_key_permissions is None:
            api_key_permissions = ["chat", "sessions"]

        # --- Guard: no duplicates ---
        existing = self.admin.get_tenant_config(tenant_id)
        if existing:
            raise ValueError(f"Tenant '{tenant_id}' already exists")

        try:
            # 1. Create TenantConfig
            tenant = TenantConfig(
                tenant_id=tenant_id,
                display_name=display_name,
                description=description,
                escalation_contact=escalation_contact,
                allowed_domains=[default_domain_id],
                system_prompt_additions=system_prompt_additions,
                active=True,
                created_by=created_by,
            )
            self.db.add(tenant)
            self.db.flush()  # get ID without committing
            logger.info("Onboard: created tenant config for %s", tenant_id)

            # 2. Create default DomainConfig
            domain = DomainConfig(
                tenant_id=tenant_id,
                domain_id=default_domain_id,
                display_name=default_domain_display_name,
                description=default_domain_description,
                active=True,
                created_by=created_by,
            )
            self.db.add(domain)
            self.db.flush()
            logger.info(
                "Onboard: created domain config %s/%s", tenant_id, default_domain_id
            )

            # 3. Create default retention policies
            retention_map = {
                "sessions": retention_days_sessions,
                "messages": retention_days_messages,
                "memory_items": retention_days_memory_items,
                "traces": retention_days_traces,
                "knowledge_embeddings": retention_days_knowledge,
            }
            retention_policies = []
            for category, days in retention_map.items():
                policy = RetentionPolicy(
                    tenant_id=tenant_id,
                    data_category=category,
                    retention_days=days,
                    action="anonymize",
                    purpose=f"PIPEDA-compliant default for {category}",
                    active=True,
                    created_by=created_by,
                )
                self.db.add(policy)
                retention_policies.append(policy)
            self.db.flush()
            logger.info("Onboard: created %d retention policies for %s", len(retention_policies), tenant_id)

            # 4b. Create the tenant's admin API key (management)
            admin_key, admin_raw = self.api_key_service.create_key(
                tenant_id=tenant_id,
                domain_id=None,
                agent_id=None,
                display_name=f"{display_name} — Admin Key",
                permissions=["chat", "sessions", "admin"],
                rate_limit=api_key_rate_limit,
                created_by=created_by,
                auto_commit=False,
            )
            logger.info("Onboard: created admin API key for %s", tenant_id)

            # 4c. Emit audit rows (P3-A) — four ACTION_CREATE rows
            # written in the SAME transaction as the mutations they
            # describe. Per Invariant 4 (audit-before-commit): the
            # audit rows MUST land before the commit so they cannot
            # drift out of sync with the data they describe.
            ctx = audit_ctx if audit_ctx is not None else AuditContext.system(
                label="onboard_tenant"
            )
            audit_repo = AdminAuditRepository(self.db)

            audit_repo.record(
                ctx=ctx,
                tenant_id=tenant_id,
                action=ACTION_CREATE,
                resource_type=RESOURCE_TENANT,
                resource_natural_id=tenant_id,
                after={
                    "display_name": display_name,
                    "description": description,
                    "escalation_contact": escalation_contact,
                    "allowed_domains": [default_domain_id],
                },
                note="onboard_tenant: created tenant_config",
            )
            audit_repo.record(
                ctx=ctx,
                tenant_id=tenant_id,
                action=ACTION_CREATE,
                resource_type=RESOURCE_DOMAIN,
                resource_natural_id=default_domain_id,
                domain_id=default_domain_id,
                after={
                    "display_name": default_domain_display_name,
                    "description": default_domain_description,
                },
                note="onboard_tenant: created default domain_config",
            )
            # Bulk retention-policy audit row — one row with the full
            # category breakdown in after_json. Five categories.
            audit_repo.record(
                ctx=ctx,
                tenant_id=tenant_id,
                action=ACTION_CREATE,
                resource_type=RESOURCE_RETENTION_POLICY,
                resource_natural_id=f"onboard:{tenant_id}",
                after={
                    "categories": list(retention_map.keys()),
                    "retention_days_by_category": dict(retention_map),
                    "action": "anonymize",
                },
                note=f"onboard_tenant: created {len(retention_policies)} default retention policies (PIPEDA)",
            )
            audit_repo.record(
                ctx=ctx,
                tenant_id=tenant_id,
                action=ACTION_CREATE,
                resource_type=RESOURCE_API_KEY,
                resource_pk=admin_key.id,
                resource_natural_id=admin_key.key_prefix,
                after={
                    "display_name": admin_key.display_name,
                    "permissions": ["chat", "sessions", "admin"],
                    "rate_limit": api_key_rate_limit,
                    "bound_to_luciel_instance": False,
                },
                note="onboard_tenant: created admin api_key (first key)",
            )
            logger.info("Onboard: emitted 4 audit rows for %s", tenant_id)

            # 5. Commit everything atomically
            self.db.commit()
            self.db.refresh(tenant)
            self.db.refresh(domain)
            for p in retention_policies:
                self.db.refresh(p)

            logger.info("Onboard: tenant %s fully onboarded", tenant_id)
            return {
                "tenant": tenant,
                "default_domain": domain,
                "admin_api_key": admin_key,
                "admin_raw_key": admin_raw,
                "retention_policies": retention_policies,
            }

        except Exception:
            self.db.rollback()
            logger.exception("Onboard: failed for tenant %s — rolled back", tenant_id)
            raise