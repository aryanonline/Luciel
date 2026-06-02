"""
Admin onboarding service.

Orchestrates the atomic creation of everything a new Admin needs:
  1. Admin row (formerly TenantConfig)
  2. Default RetentionPolicies (PIPEDA-compliant)
  3. First API key

All writes happen in a single DB transaction — if any step fails,
everything rolls back. No partial Admins.

Step 23 origin; Arc 5 Path A collapsed the Domain layer; Arc 6
Commit 8 (2026-05-23) rewrote the Admin() kwargs to match the V2
schema after the kwarg drift (display_name, description,
escalation_contact, created_by, allowed_domains were all stale
post-Arc-5-B8) was discovered as a P0 during the unified-signup
design. The function now accepts a ``tier`` parameter (V2 vocabulary)
and writes ONLY the V2 Admin columns:
``id, name, tier, tier_source, active``. The remaining legacy kwargs
(description, escalation_contact) are RETAINED on the signature for
source-compat with the platform_admin route + the Stripe webhook
caller, but they are NO LONGER written to the Admin row — they thread
into audit metadata only.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.admin_audit_log import (
    ACTION_CREATE,
    RESOURCE_RETENTION_POLICY,
    RESOURCE_TENANT,
)
# Arc 5 Path A: DomainConfig REMOVED. V2 onboarding creates an Admin
# row (formerly TenantConfig) and the retention policies; there is no
# Domain layer.
from app.models.admin import (
    Admin,
    ALLOWED_TIERS_V2,
    TIER_PRO,
    TIER_SOURCE_STRIPE_WEBHOOK,
)
from app.models.retention import RetentionPolicy

# Legacy name kept for source compatibility.
TenantConfig = Admin
from app.repositories.admin_audit_repository import AdminAuditRepository, AuditContext
from app.services.admin_service import AdminService
from app.services.api_key_service import ApiKeyService

logger = logging.getLogger(__name__)

# PIPEDA-compliant default retention categories. Cleanup A renamed
# the knowledge data_category from "knowledge_embeddings" (legacy)
# to "knowledge_chunks"; the paired alembic migration
# ``arc11_cleanup_a_data_category_rename`` updates any persisted
# rows.
DEFAULT_RETENTION_CATEGORIES = [
    "sessions",
    "messages",
    "memory_items",
    "traces",
    "knowledge_chunks",
]


class OnboardingService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.admin = AdminService(db)
        self.api_key_service = ApiKeyService(db)

    def onboard_tenant(
        self,
        *,
        admin_id: str,
        display_name: str,
        tier: str = TIER_PRO,
        tier_source: str = TIER_SOURCE_STRIPE_WEBHOOK,
        description: str | None = None,
        escalation_contact: str | None = None,
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
        Create everything an Admin needs in one atomic transaction.

        Returns a dict with: tenant (Admin), admin_api_key,
        admin_raw_key, retention_policies.

        Raises ValueError if Admin already exists or ``tier`` is not in
        the V2 allowed-tier set.

        Arc 6 Commit 8 (2026-05-23): rewrote the Admin() kwargs to use
        ONLY V2 columns (id, name, tier, tier_source, active). The
        remaining legacy kwargs (description, escalation_contact) are
        RETAINED on the signature for source-compat with admin.py + the
        Stripe webhook caller but they are NO LONGER written to the
        Admin row -- they thread through to audit metadata only.
        ``tier`` defaults to PRO so the Stripe webhook caller (which
        does NOT pass tier today) keeps its current behavior; the
        unified-signup caller passes ``tier="free"`` +
        ``tier_source="free_signup"`` explicitly.
        """
        if api_key_permissions is None:
            api_key_permissions = ["chat", "sessions"]

        # --- Guard: V2 tier vocabulary ---
        if tier not in ALLOWED_TIERS_V2:
            raise ValueError(
                f"Invalid tier {tier!r}; must be one of {ALLOWED_TIERS_V2}"
            )

        # --- Guard: no duplicates ---
        existing = self.admin.get_tenant_config(admin_id)
        if existing:
            raise ValueError(f"Tenant '{admin_id}' already exists")

        try:
            # 1. Create Admin (formerly TenantConfig).
            # Arc 5 Path A: ``admin_id`` is the Admin primary key (`id`).
            # Arc 6 Commit 8: ONLY V2 columns. The verified V2 column set
            # (per app/models/admin.py) is: id, name, tier, tier_source,
            # active, stripe_customer_id, legacy_tenant_id, created_at,
            # updated_at. We write the five business-meaningful ones; the
            # timestamps are server-defaulted; stripe_customer_id and
            # legacy_tenant_id are set elsewhere (webhook / data migration).
            tenant = Admin(
                id=admin_id,
                name=display_name,
                tier=tier,
                tier_source=tier_source,
                active=True,
            )
            self.db.add(tenant)
            self.db.flush()  # get ID without committing
            logger.info(
                "Onboard: created admin id=%s tier=%s tier_source=%s",
                admin_id, tier, tier_source,
            )

            # Arc 9 C13 hotfix (demo-day-2026-05-25): bootstrap GUC.
            # Free signup is unauthenticated -- the `after_begin` engine
            # listener saw no admin_id in the ContextVar and set
            # app.admin_id to ''. The RLS with_check on retention_policies
            # (and api_keys, audit rows) requires app.admin_id ==
            # admin_id::text. We just minted the Admin row, so it is
            # now safe to push the new admin_id.
            #
            # We push at BOTH layers:
            #   1. SET LOCAL on the live transaction so the immediate
            #      child INSERTs see the GUC.
            #   2. ContextVar via set_current_admin_id() so subsequent
            #      transactions in this request (e.g. the post-commit
            #      db.refresh() calls below, which open a NEW txn, AND
            #      the last_signup_ip UPDATE / magic-link mint in
            #      signup_free after onboard_tenant returns) also see
            #      the GUC via the after_begin listener.
            # We intentionally do NOT reset the ContextVar at method
            # exit -- the ContextVar is request-scoped and will clear
            # at request end. This matches the authenticated path,
            # where the JWT-extracted admin_id stays set for the whole
            # request. Resetting here would break the follow-up code
            # in signup_free that runs under this tenant identity.
            #
            # The Admin row insert above is safe under empty GUC
            # because the admins table has RLS disabled (it is the
            # tenant directory; the fence sits AROUND it, not ON it).
            from sqlalchemy import text as _text
            from app.db.tenant_context import set_current_admin_id as _set_admin
            self.db.execute(
                _text("SELECT set_config('app.admin_id', :tid, true)"),
                {"tid": admin_id},
            )
            _set_admin(admin_id)

            # 2. Default DomainConfig: REMOVED (Arc 5 Path A). V2 has no
            # Domain layer; no domain_config row is created at onboarding.

            # 3. Create default retention policies. Cleanup A renamed
            # the knowledge data_category to "knowledge_chunks".
            retention_map = {
                "sessions": retention_days_sessions,
                "messages": retention_days_messages,
                "memory_items": retention_days_memory_items,
                "traces": retention_days_traces,
                "knowledge_chunks": retention_days_knowledge,
            }
            retention_policies = []
            for category, days in retention_map.items():
                policy = RetentionPolicy(
                    admin_id=admin_id,
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
            logger.info("Onboard: created %d retention policies for %s", len(retention_policies), admin_id)

            # 4b. Create the tenant's admin API key (management).
            #
            # Step 28 P3-B: ApiKeyService.create_key now emits its OWN
            # ACTION_CREATE / RESOURCE_API_KEY audit row in the same
            # transaction as the api_keys INSERT (Invariant 4). We thread
            # the request-bound audit_ctx through so the api_key audit
            # row carries the SAME actor_key_prefix as the other three
            # rows we emit below -- preserving Pillar 20's atomicity
            # assertion (exactly one distinct actor across the four
            # rows). We must resolve `ctx` BEFORE this call so we can
            # share it.
            ctx = audit_ctx if audit_ctx is not None else AuditContext.system(
                label="onboard_tenant"
            )
            admin_key, admin_raw = self.api_key_service.create_key(
                admin_id=admin_id,
                # Arc 12 EX1a — domain_id / agent_id removed from
                # create_key contract; V2 keys are bound to
                # (admin_id, instance_id) only.
                display_name=f"{display_name} — Admin Key",
                permissions=["chat", "sessions", "admin"],
                rate_limit=api_key_rate_limit,
                created_by=created_by,
                auto_commit=False,
                audit_ctx=ctx,
            )
            logger.info("Onboard: created admin API key for %s", admin_id)

            # 4c. Emit audit rows (P3-A) — three ACTION_CREATE rows for
            # tenant_config, domain_config, and retention_policy,
            # written in the SAME transaction as the mutations they
            # describe. The fourth required row -- ACTION_CREATE /
            # RESOURCE_API_KEY for the admin key -- is emitted by
            # ApiKeyService.create_key itself (Step 28 P3-B), so
            # OnboardingService no longer emits it directly. Pillar 20
            # still observes all four pairs because they all land in
            # the same transaction under the same audit_ctx.
            audit_repo = AdminAuditRepository(self.db)

            audit_repo.record(
                ctx=ctx,
                admin_id=admin_id,
                action=ACTION_CREATE,
                resource_type=RESOURCE_TENANT,
                resource_natural_id=admin_id,
                after={
                    # Arc 6 / Commit 8 -- audit row mirrors the V2 Admin
                    # write. Legacy descriptive fields (description,
                    # escalation_contact) are retained in the
                    # after-snapshot as call-site metadata (the caller
                    # intended them as descriptive context) rather than
                    # as column values, so platform_admin forensic
                    # queries against admin_audit_log keep the same
                    # shape during the transition.
                    "name": display_name,
                    "tier": tier,
                    "tier_source": tier_source,
                    "description": description,
                    "escalation_contact": escalation_contact,
                },
                note="onboard_tenant: created admin (V2 vocab)",
            )
            # Arc 5 Path A: RESOURCE_DOMAIN audit row REMOVED. V2 has no
            # Domain layer, so no domain_config row is created and no
            # corresponding audit row is emitted. Pillar 20's pair
            # coverage moves from (tenant, domain, retention, api_key)
            # to (tenant, retention, api_key) post-collapse.
            # Bulk retention-policy audit row — one row with the full
            # category breakdown in after_json. Five categories.
            audit_repo.record(
                ctx=ctx,
                admin_id=admin_id,
                action=ACTION_CREATE,
                resource_type=RESOURCE_RETENTION_POLICY,
                resource_natural_id=f"onboard:{admin_id}",
                after={
                    "categories": list(retention_map.keys()),
                    "retention_days_by_category": dict(retention_map),
                    "action": "anonymize",
                },
                note=f"onboard_tenant: created {len(retention_policies)} default retention policies (PIPEDA)",
            )
            # NOTE: the fourth ACTION_CREATE/RESOURCE_API_KEY audit row
            # is NOT emitted here -- ApiKeyService.create_key already
            # emitted it (P3-B). Three rows here + one from create_key
            # = four total, satisfying Pillar 20's pair-coverage
            # assertion.
            logger.info("Onboard: emitted 3 audit rows for %s (4th from create_key)", admin_id)

            # 5. Commit everything atomically
            self.db.commit()
            self.db.refresh(tenant)
            # V2: no domain row to refresh.
            for p in retention_policies:
                self.db.refresh(p)

            logger.info("Onboard: tenant %s fully onboarded", admin_id)
            return {
                "tenant": tenant,
                "admin_api_key": admin_key,
                "admin_raw_key": admin_raw,
                "retention_policies": retention_policies,
            }

        except Exception:
            self.db.rollback()
            logger.exception("Onboard: failed for tenant %s — rolled back", admin_id)
            raise