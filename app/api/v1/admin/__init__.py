
# --- Add to imports at top of admin.py ---
from fastapi import Query, status, HTTPException
from sqlalchemy.exc import IntegrityError

# Step 24.5 — agents / luciel_instances
from app.api.deps import (
    get_admin_audit_repository,
    get_admin_service,                 # ADD THIS
    get_agent_repository,
    get_audit_context,
    get_luciel_instance_service,
)
from app.models.admin_audit_log import (
    ACTION_CREATE,
    ACTION_DEACTIVATE,
    ACTION_DOMAIN_CREATED,
    ACTION_DOMAIN_DEACTIVATED,
    ACTION_UPDATE,
    RESOURCE_AGENT,
    RESOURCE_DOMAIN,
    RESOURCE_LUCIEL_INSTANCE,
)
# Cleanup B closeout: the legacy knowledge routes are gone. The
# only remaining knowledge surface in this module is the
# diagnostic ``/instances/{instance_id}/chunking-config`` endpoint,
# which uses ``kschemas.EffectiveChunkingConfigRead`` only — no
# IngestionService, no KnowledgeRepository, no upload helpers.
from app.schemas import knowledge as kschemas
from app.services.instance_service import (
    InstanceNotFoundError,
    InstanceService,
)
from app.repositories.admin_audit_repository import AdminAuditRepository, AuditContext
# Arc 5 Path A — AgentRepository deleted at Commit A5; the routes that
# consumed it (/admin/agents/*) were already deleted at B3. The
# Annotated annotation on the bootstrap-tenant route below is replaced
# with ``object`` so the FastAPI dependency resolver still parses; the
# B1 route-body sweep rewrites that route to not depend on AgentRepository.
from app.schemas.instance import (
    InstanceCreate,
    InstanceRead,
    InstanceUpdate,
)
from app.services.instance_service import (
    DuplicateInstanceError,
    InstanceLifecycleConflictError,
    InstanceNotFoundError,
    InstanceRestoreGraceExpiredError,
    InstanceService,
    TierScopeViolationError,
)

from app.policy.scope import ScopePolicy
from app.policy.entitlements import TIER_ENTITLEMENTS, TIER_FREE
from app.policy.instance_config import validate_pillars_for_tier
from app.schemas.onboarding import (
    TenantOnboardRequest,
    TenantOnboardResponse,
    OnboardedTenantSummary,
    OnboardedApiKeySummary,
    OnboardedRetentionSummary,
)
from app.services.onboarding_service import OnboardingService
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status

from app.api.deps import DbSession, get_ingestion_service
from app.knowledge.ingestion import IngestionService
from app.middleware.rate_limit import (
    limiter,
    get_tier_aware_key,
    get_tier_rate_limit_for_key,
)
from app.schemas.admin import (
    TenantConfigCreate,
    TenantConfigRead,
    TenantConfigUpdate,
)
from app.schemas.api_key import (
    ApiKeyCreate,
    ApiKeyCreateResponse,
    ApiKeyRead,
    EmbedKeyCreate,
    EmbedKeyCreateResponse,
    EmbedKeyRead,
)
from app.services.admin_service import AdminService
from app.services.api_key_service import ApiKeyService
from app.services.memory_admin_service import MemoryAdminService
from app.schemas.memory import MemoryRead
from app.policy.scope import ScopePolicy
from app.models.instance import Instance as LucielInstance

router = APIRouter(prefix="/admin", tags=["admin"])

def _load_active_instance(
    *,
    request: Request,
    instance_id: int,
    instance_service: InstanceService,
) -> "LucielInstance":
    instance = instance_service.get_by_pk(instance_id)
    if instance is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Luciel instance {instance_id} not found",
        )
    ScopePolicy.enforce_luciel_instance_scope(request, instance)
    if not instance.active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Luciel instance {instance_id} is inactive",
        )
    return instance

# --- Add this endpoint BEFORE the individual /tenants POST route ---
# (FastAPI matches routes top-down, so /tenants/onboard must come
#  before /tenants/{admin_id} to avoid treating "onboard" as a admin_id)

@router.post(
    "/tenants/onboard",
    response_model=TenantOnboardResponse,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def onboard_tenant(
    request: Request,
    payload: TenantOnboardRequest,
    db: DbSession,
) -> TenantOnboardResponse:

    # Only platform_admin may onboard new tenants.
    if not ScopePolicy.is_platform_admin(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only platform_admin may onboard tenants",
        )

    """
    One-call tenant onboarding.

    Creates Admin (formerly TenantConfig), PIPEDA retention policies,
    and the tenant's first API key atomically. If anything fails,
    nothing is created.

    The raw API key is returned once. Store it securely.
    """
    service = OnboardingService(db)

    # P3-A: capture caller identity once at the request boundary so
    # the four audit rows OnboardingService emits are attributed to
    # the platform_admin who actually performed the onboarding, not
    # to AuditContext.system().
    audit_ctx = AuditContext.from_request(request)

    try:
        result = service.onboard_tenant(
            admin_id=payload.admin_id,
            display_name=payload.display_name,
            # Arc 6 Commit 8 -- V2 tier vocabulary threaded through.
            tier=payload.tier,
            tier_source=payload.tier_source,
            description=payload.description,
            escalation_contact=payload.escalation_contact,
            api_key_display_name=payload.api_key_display_name,
            api_key_permissions=payload.api_key_permissions,
            api_key_rate_limit=payload.api_key_rate_limit,
            retention_days_sessions=payload.retention_days_sessions,
            retention_days_messages=payload.retention_days_messages,
            retention_days_memory_items=payload.retention_days_memory_items,
            retention_days_traces=payload.retention_days_traces,
            retention_days_knowledge=payload.retention_days_knowledge,
            created_by=payload.created_by,
            audit_ctx=audit_ctx,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    tenant = result["tenant"]

    return TenantOnboardResponse(
        # Arc 6 Commit 8 -- explicit V2 -> legacy-wire mapping. The V2
        # Admin model has ``id`` (slug) and ``name``; we surface both
        # under the legacy ``admin_id`` / ``display_name`` field names
        # so existing API consumers (the platform-admin onboarding UI,
        # downstream test fixtures) keep working unchanged.
        tenant=OnboardedTenantSummary(
            id=tenant.id,
            admin_id=tenant.id,
            display_name=tenant.name,
            tier=tenant.tier,
            tier_source=tenant.tier_source,
            active=tenant.active,
            created_at=tenant.created_at,
        ),
        admin_api_key=OnboardedApiKeySummary(
            key_prefix=result["admin_api_key"].key_prefix,
            display_name=result["admin_api_key"].display_name,
            permissions=result["admin_api_key"].permissions,
            rate_limit=result["admin_api_key"].rate_limit,
            raw_key=result["admin_raw_key"],
        ),
        retention_policies=[
            OnboardedRetentionSummary(
                data_category=p.data_category,
                retention_days=p.retention_days,
                action=p.action,
            )
            for p in result["retention_policies"]
        ],
        message=(
            f"Tenant {payload.admin_id} onboarded. Use the admin key to "
            f"create your first LucielInstance and its chat key."
        ),
    )

@router.post("/tenants", response_model=TenantConfigRead, status_code=status.HTTP_201_CREATED)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def create_tenant(
    request: Request,
    payload: TenantConfigCreate,
    db: DbSession,
) -> TenantConfigRead:

    if not ScopePolicy.is_platform_admin(request):
        raise HTTPException(status_code=403, detail="Only platform_admin may create tenants")

    service = AdminService(db)
    existing = service.get_tenant_config(payload.admin_id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Tenant {payload.admin_id} already exists",
        )

    config = service.create_tenant_config(**payload.model_dump())
    return TenantConfigRead.model_validate(config)


@router.get("/tenants", response_model=list[TenantConfigRead])
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def list_tenants(
    request: Request,
    db: DbSession,
) -> list[TenantConfigRead]:
    service = AdminService(db)
    if ScopePolicy.is_platform_admin(request):
        configs = service.list_tenant_configs()
    else:
        caller_tenant = getattr(request.state, "admin_id", None)
        cfg = service.get_tenant_config(caller_tenant) if caller_tenant else None
        configs = [cfg] if cfg else []
    return [TenantConfigRead.model_validate(c) for c in configs]


@router.get("/tenants/{admin_id}", response_model=TenantConfigRead)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def get_tenant(
    request: Request,
    admin_id: str,
    db: DbSession,
) -> TenantConfigRead:
    ScopePolicy.enforce_tenant_scope(request, admin_id)
    service = AdminService(db)
    config = service.get_tenant_config(admin_id)
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant not found",
        )
    return TenantConfigRead.model_validate(config)


@router.patch("/tenants/{admin_id}", response_model=TenantConfigRead)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def update_tenant(
    request: Request,
    admin_id: str,
    payload: TenantConfigUpdate,
    db: DbSession,
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
    luciel_service: Annotated[InstanceService, Depends(get_luciel_instance_service)],
    # Arc 5 Path A — agent_repo dependency dropped (V2 has no Agent layer).
    # The deactivate_tenant_with_cascade signature still accepts the kwarg
    # for backward compat; we pass None so the cascade-spine V2 rewrite at
    # B2 can drop it cleanly.
) -> TenantConfigRead:
    ScopePolicy.enforce_tenant_scope(request, admin_id)
    service = AdminService(db)
    payload_data = payload.model_dump(exclude_unset=True)

    # Tenant deactivation routes through the cascade-aware spine.
    # All other updates use the generic update_tenant_config path.
    if payload_data.get("active") is False:
        deactivated = service.deactivate_tenant_with_cascade(
            admin_id,
            audit_ctx=audit_ctx,
            luciel_instance_service=luciel_service,
            agent_repo=None,
            updated_by=getattr(request.state, "actor_label", None),
        )
        if not deactivated:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Tenant not found",
            )
        config = service.get_tenant_config(admin_id)
    else:
        config = service.update_tenant_config(admin_id, **payload_data)
        if not config:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Tenant not found",
            )

    return TenantConfigRead.model_validate(config)


# Step 30a.5 -- cookied self-serve sibling of POST /admin/domains.
# The existing admin-key route above is kept for operator use; this
# route is what the Company-tier CompanyTab in Dashboard.tsx calls



# Cleanup B closeout: the legacy ``POST /admin/knowledge/ingest``
# route was deleted. It was superseded by the Step-7 routes at
# ``/admin/instances/{instance_id}/knowledge/sources`` (see
# ``app/api/v1/admin_knowledge.py``), which is the only ingest
# surface from Cleanup B forward.


@router.post("/api-keys", response_model=ApiKeyCreateResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def create_api_key(
    request: Request,
    payload: ApiKeyCreate,
    db: DbSession,
    luciel_service: Annotated[InstanceService, Depends(get_luciel_instance_service)],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> ApiKeyCreateResponse:
    # --- Arc 12 EX1a scope + privilege guards -----------------------
    # V2 has a single Admin→Instance boundary (Architecture §3.7.2);
    # the legacy domain/agent levels do not exist. Scope enforcement
    # collapses to the cross-Admin guard. The "domain-scoped caller
    # may not mint tenant-wide" and "agent-scoped caller may only mint
    # for itself" carve-outs are gone — there is no domain/agent
    # caller in V2.
    ScopePolicy.enforce_tenant_scope(request, payload.admin_id)
    # Prevent privilege escalation (non-platform_admin cannot grant platform_admin).
    ScopePolicy.enforce_no_privilege_escalation(request, payload.permissions or [])

    # --- Step 24.5: LucielInstance binding validation ---------------
    # If the key is being pinned to a specific LucielInstance, the
    # caller must be allowed to access that instance (same rule as
    # read/update/delete), and the instance must belong to the same
    # tenant as the key being minted.
    if payload.luciel_instance_id is not None:
        instance = luciel_service.get_by_pk(payload.luciel_instance_id)
        if instance is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"LucielInstance pk={payload.luciel_instance_id} "
                    f"does not exist."
                ),
            )
        ScopePolicy.enforce_luciel_instance_scope(request, instance)
        if instance.admin_id != payload.admin_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    "luciel_instance_id belongs to a different tenant "
                    "than the key being minted."
                ),
            )

    # --- Mint the key + audit (Step 28 P3-B) ------------------------
    # ApiKeyService.create_key now emits the ACTION_CREATE audit row in
    # the same transaction as the api_keys INSERT (Invariant 4: audit-
    # before-commit). The endpoint just threads audit_ctx through; no
    # post-mint audit emission needed.
    service = ApiKeyService(db)
    api_key, raw_key = service.create_key(
        admin_id=payload.admin_id,
        # Arc 12 EX1a — agent_id / domain_id removed from the admin-key
        # mint contract. V2 keys are bound to (admin_id, instance_id).
        luciel_instance_id=payload.luciel_instance_id,   # Step 24.5
        display_name=payload.display_name,
        permissions=payload.permissions,
        rate_limit=payload.rate_limit,
        created_by=payload.created_by,
        audit_ctx=audit_ctx,
    )

    return ApiKeyCreateResponse(
        api_key=ApiKeyRead.model_validate(api_key),
        raw_key=raw_key,
    )


@router.get("/api-keys", response_model=list[ApiKeyRead])
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def list_api_keys(
    request: Request,
    db: DbSession,
    admin_id: str | None = Query(default=None),
) -> list[ApiKeyRead]:
    service = ApiKeyService(db)

    if not ScopePolicy.is_platform_admin(request):
        # Force tenant filter to caller's own tenant.
        admin_id = getattr(request.state, "admin_id", None)

    keys = service.list_keys(admin_id=admin_id)

    # Arc 12 EX1a — V2 has a single Admin→Instance boundary
    # (Architecture §3.7.2). The legacy domain-scoped and agent-scoped
    # caller post-filters are gone: there is no domain/agent caller in
    # V2, and the cross-Admin guard above already restricts non-
    # platform_admin callers to their own admin_id. Instance-level
    # scoping for callers bound to a specific Luciel instance will
    # land in a later EX-step once instance-keyed list filtering is
    # introduced; until then admin-key list reads are admin-scoped.

    return [ApiKeyRead.model_validate(k) for k in keys]


# =====================================================================
# Step 30b commit (b) of step-30b-embed-key-issuance
# =====================================================================
#
# POST /admin/embed-keys -- mint an embed key for the chat widget.
#
# Why this is a sibling endpoint to /admin/api-keys, not an overload:
#   - The credential class is server-set (key_kind='embed'), not
#     client-supplied. Operators cannot accidentally mint an admin
#     key through this URL or vice versa.
#   - The request body schema (EmbedKeyCreate, extra='forbid')
#     rejects any field that belongs to the admin-key surface
#     (key_kind, permissions, agent_id, luciel_instance_id,
#     ssm_write, etc.) so the URL alone determines what credential
#     class is being minted.
#   - The admin-key path is touched ZERO. No conditional branches in
#     create_api_key, no risk of regressing the admin surface while
#     building out the embed surface.
#
# Scope policy mirrors create_api_key exactly: the caller can mint
# an embed key only at or below their own scope. A tenant-scoped
# admin key minted for tenant X cannot mint an embed key for tenant Y;
# a domain-scoped admin key cannot mint a tenant-wide embed key. We
# additionally refuse minting embed keys with NULL admin_id (those
# would be cross-tenant by definition, which the EmbedKeyCreate schema
# already rejects, but we restate the rule here so the endpoint is
# self-contained and a future schema relaxation cannot accidentally
# punch through).
#
# Per the doc-discipline rule (commit c729cd5 on main): this commit
# does not edit the canonical docs. The drift entry stays open until
# commit (d) lands the strikethrough alongside doc updates.
# =====================================================================

@router.post(
    "/embed-keys",
    response_model=EmbedKeyCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def create_embed_key(
    request: Request,
    payload: EmbedKeyCreate,
    db: DbSession,
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> EmbedKeyCreateResponse:
    """Mint an embed key for the chat widget.

    The Pydantic schema (EmbedKeyCreate) does the heavy lifting:
    origin shape validation, length caps on widget_config, HTML
    rejection, wildcard rejection, dedupe, lowercase normalization.
    By the time this function body runs, the payload is already in
    the canonical shape.

    What this function adds on top of the schema:
      1. Scope policy enforcement (caller must be at or above the
         target scope).
      2. Mutual exclusion with admin-key minting (the URL alone
         disambiguates; we do NOT inspect payload.permissions because
         the schema rejects that field outright).
      3. Server-set key_kind='embed' and permissions=['chat'] passed
         to ApiKeyService.create_key. The schema does not accept
         these fields from the client; this is the only place they
         get set.
    """
    # --- Arc 12 EX1a/EX1c scope policy ------------------------------
    # V2 has a single Admin→Instance boundary (Architecture §3.7.2).
    # Embed-key issuance collapses to the cross-Admin guard. The
    # legacy domain-scoped / agent-scoped caller carve-outs are gone,
    # and (EX1c) EmbedKeyCreate no longer carries a ``domain_id``
    # field — the widget runtime resolves scope via
    # ``luciel_instance_id`` alone.
    ScopePolicy.enforce_tenant_scope(request, payload.admin_id)

    # Step 31.2 commit B: lift the v1 luciel_instance_id carve-out.
    # When the caller passes luciel_instance_id, validate the instance
    # belongs to the same tenant (+ domain, if domain-scoped) as the
    # key being minted. Without this check a tenant-admin could mint
    # an embed key pinned to another tenant's instance, which would
    # silently cross the scope boundary at chat time.
    if payload.luciel_instance_id is not None:
        from app.repositories.instance_repository import (
            InstanceRepository,
        )
        instance_repo = InstanceRepository(db)
        instance = instance_repo.get_by_pk(payload.luciel_instance_id)
        if instance is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    f"Luciel instance pk={payload.luciel_instance_id} "
                    "not found."
                ),
            )
        if instance.admin_id != payload.admin_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Luciel instance belongs to a different tenant. "
                    "Embed keys may only pin instances within their own "
                    "tenant scope."
                ),
            )
        # Arc 12 EX1c — V2 has no Domain layer; the dead cross-domain
        # guard previously kept here (Arc 5 Path A) has been removed.
        if not instance.active:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "Luciel instance is inactive (soft-deleted). Mint "
                    "the embed key against an active instance, or "
                    "reactivate this instance first."
                ),
            )

    # Arc 12 EX1c — V2 has a single Admin→Instance boundary
    # (Architecture §3.7.2). The legacy per-domain
    # ScopePromptPreflight gate is gone: every embed-key mint is
    # tenant-wide (governed by TenantConfig.system_prompt at chat
    # time) and is surfaced with the same non-fatal warning that
    # used to fire on tenant-wide mints.
    warnings: list[str] = [
        "Tenant-wide embed key minted; scope is governed by "
        "TenantConfig.system_prompt at chat time."
    ]

    # --- Mint via ApiKeyService -------------------------------------
    # The four embed-only kwargs were added in commit (a). The audit
    # row is emitted in the same transaction as the api_keys INSERT
    # (Invariant 4: audit-before-commit), and the audit 'after'
    # payload records key_kind='embed', allowed_origins_count, the
    # rate_limit_per_minute, and the widget_config keys that were
    # set -- enough to prove the row's shape without leaking customer-
    # facing branding text into the audit log.
    service = ApiKeyService(db)
    api_key, raw_key = service.create_key(
        admin_id=payload.admin_id,
        # Arc 12 EX1a/EX1c — embed-key mint no longer threads
        # agent_id / domain_id through any layer. The api_key row's
        # binding is (admin_id, luciel_instance_id); the widget
        # runtime scopes by the same axes.
        luciel_instance_id=payload.luciel_instance_id,
        display_name=payload.display_name,
        # Server-set: the schema does not accept these from the client.
        permissions=["chat"],
        # Per-day rate_limit is an admin-key concept; embed keys are
        # gated by rate_limit_per_minute. We set rate_limit to 0
        # (=unlimited at the per-day layer) because the per-minute
        # cap is the only quota that applies and we don't want to
        # double-gate on a column that has no semantic meaning here.
        rate_limit=0,
        created_by=payload.created_by,
        audit_ctx=audit_ctx,
        key_kind="embed",
        allowed_origins=payload.allowed_origins,
        rate_limit_per_minute=payload.rate_limit_per_minute,
        widget_config=payload.widget_config.to_jsonb(),
    )

    # raw_key is always non-None here because ssm_write defaulted to
    # False (and is in fact rejected by create_key when key_kind='embed').
    assert raw_key is not None, (
        "create_key returned None raw_key for an embed key; this would "
        "mean ssm_write slipped through, which create_key explicitly "
        "rejects. Investigate immediately."
    )

    return EmbedKeyCreateResponse(
        embed_key=EmbedKeyRead.model_validate(api_key),
        raw_key=raw_key,
        warnings=warnings,
    )


@router.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def deactivate_api_key(
    request: Request,
    key_id: int,
    db: DbSession,
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> None:
    service = ApiKeyService(db)
    # Fetch first so we can enforce scope on the target.
    target = service.get_key_by_id(key_id) if hasattr(service, "get_key_by_id") else None

    # Step 28 Phase 2 HOTFIX: previously the happy path (target found) was
    # dead code -- it returned 204 without ever calling deactivate_key,
    # so Pillar 17 D5 saw zero deactivate audit rows. Now we always run
    # the deactivate path, with scope enforcement when the target is
    # known. The fallback (target unknown) preserves the legacy 404
    # behavior for buggy/legacy ApiKeyService implementations missing
    # get_key_by_id.
    if target is not None:
        # Enforce scope: a tenant-scoped caller cannot deactivate keys
        # belonging to other tenants; a domain-scoped caller cannot
        # touch keys outside their domain; same for agent.
        if not ScopePolicy.is_platform_admin(request):
            caller_tenant = getattr(request.state, "admin_id", None)
            # Arc 12 EX1a — V2 has a single Admin→Instance boundary
            # (Architecture §3.7.2); the domain-scoped and agent-scoped
            # caller carve-outs are gone.
            if caller_tenant and target.admin_id != caller_tenant:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Cannot deactivate API key outside your tenant",
                )
        success = service.deactivate_key(key_id, audit_ctx=audit_ctx)
        if not success:
            # Race: target existed at fetch but vanished before deactivate.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="API key not found",
            )
    else:
        # Fall back: just try deactivate; 404 if not found.
        success = service.deactivate_key(key_id, audit_ctx=audit_ctx)
        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="API key not found",
            )


# =====================================================================
# Step 28 - Commit 8b-prereq-data-cascade-fix
# Memory item admin endpoints (platform_admin only).
# Powers the Pattern S walker's memory_items leaf step.
# =====================================================================

@router.get("/memory-items", response_model=list[MemoryRead])
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def list_memory_items(
    request: Request,
    db: DbSession,
    admin_id: str = Query(
        ...,
        min_length=2,
        max_length=100,
        description="Tenant whose memory_items to list. Required.",
    ),
    active_only: bool = Query(
        default=False,
        description="If true, return only rows with active=True.",
    ),
) -> list[MemoryRead]:
    permissions = getattr(request.state, "permissions", []) or []
    if "platform_admin" not in permissions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only platform_admin may list memory_items",
        )
    service = MemoryAdminService(db)
    items = service.list_memories_for_tenant(
        admin_id=admin_id,
        active_only=active_only,
    )
    return [MemoryRead.model_validate(i) for i in items]


@router.delete("/memory-items/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def deactivate_memory_item(
    request: Request,
    memory_id: int,
    db: DbSession,
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> None:
    permissions = getattr(request.state, "permissions", []) or []
    if "platform_admin" not in permissions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only platform_admin may deactivate memory_items",
        )
    service = MemoryAdminService(db)
    success = service.deactivate_memory(memory_id, audit_ctx=audit_ctx)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Memory item not found",
        )
# =====================================================================
# Step 24.5 — LucielInstance management routes
# Route order: static paths before any path-parameter route.
# =====================================================================

def _resolve_admin_tier_for_pillars(db, *, admin_id: str) -> str:
    """Resolve an Admin's subscription tier for pillar validation.

    Fail-closed to Free when the row is missing or the tier value is not
    a recognised tier — mirrors ``admin_channels._resolve_admin_tier``.
    """
    from sqlalchemy import select

    from app.models.admin import Admin

    row = db.execute(
        select(Admin.tier).where(Admin.id == admin_id)
    ).scalar_one_or_none()
    return row if row in TIER_ENTITLEMENTS else TIER_FREE


@router.post(
    "/instances",
    response_model=InstanceRead,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def create_luciel_instance(
    request: Request,
    payload: InstanceCreate,
    service: Annotated[InstanceService, Depends(get_luciel_instance_service)],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> InstanceRead:
    # Arc 5 Path A — V2-collapsed body. Path renamed to
    # /admin/instances at B3; the legacy teammate_email overload was
    # removed at Step 30a.5 (invitations flow through POST /admin/invites).
    ScopePolicy.enforce_admin_scope(request, payload.admin_id)
    # V2 cap-enforcement guard — platform_admin keys bypass tier enforcement.
    if not ScopePolicy.is_platform_admin(request):
        try:
            service.admin._enforce_tier_scope(
                admin_id=payload.admin_id,
            )
        except TierScopeViolationError as exc:
            raise HTTPException(
                status_code=402,
                detail={
                    "message": str(exc),
                    "reason": exc.reason,
                },
            ) from exc
    # Arc 15 WU1 — tier-conditional validation for the config pillars.
    # Structural validity is already enforced by the Pydantic schema;
    # here we apply the per-tier rules (custom preset Pro/Ent-only,
    # business_context length cap, lead_routing Pro/Ent-only) that need
    # the server-resolved tier. platform_admin keys bypass cap/tier
    # enforcement above but the pillar caps are content limits, not
    # billing limits, so we still apply them against the resolved tier.
    admin_tier = _resolve_admin_tier_for_pillars(
        service.db, admin_id=payload.admin_id
    )
    pillar_problems = validate_pillars_for_tier(
        tier=admin_tier,
        personality_preset=payload.personality_preset,
        business_context=payload.business_context,
        lead_routing=payload.lead_routing,
    )
    if pillar_problems:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "instance_config_invalid_for_tier",
                "tier": admin_tier,
                "problems": pillar_problems,
            },
        )

    try:
        instance = service.create_instance(
            audit_ctx=audit_ctx,
            admin_id=payload.admin_id,
            instance_slug=payload.instance_slug,
            display_name=payload.display_name,
            description=payload.description,
            active=payload.active,
            created_by=payload.created_by,
            website=payload.website,
            personality_preset=payload.personality_preset,
            personality_axes=payload.personality_axes,
            business_context=payload.business_context,
            lead_routing=payload.lead_routing,
        )
    except DuplicateInstanceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return InstanceRead.model_validate(instance)


# =====================================================================
# Step 30a.5 -- /admin/luciel-instances teammate_email overload removed
# =====================================================================
#
# The Step 30a.1 _invite_teammate helper that previously sat here was
# deleted in Step 30a.5 along with the schema field and the deprecated
# email-send block above. The first-class invite path is POST
# /admin/invites (Step 30a.4) -- see Architecture v1 §3.2 (Instance
# subsystem / Team Member invite path) for the canonical contract.
# invite_service.create_invite() now carries the User + Agent +
# ScopeAssignment provisioning logic that _invite_teammate used to
# duplicate (see app/services/invite_service.py line 308+).


@router.get(
    "/instances",
    response_model=list[InstanceRead],
)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def list_luciel_instances(
    request: Request,
    service: Annotated[InstanceService, Depends(get_luciel_instance_service)],
    admin_id: str | None = Query(default=None),
    active_only: bool = Query(default=False),
) -> list[InstanceRead]:
    # Arc 9 C22 -- Arc 5 Path A collapsed the tenant/domain/agent
    # hierarchy into a single admin scope. "admin_id" here is the
    # caller's admin_id and is the only scope axis the service
    # understands. Platform admins must pass it explicitly; tenant
    # admins inherit it from the cookie/session scope.
    if not ScopePolicy.is_platform_admin(request):
        caller_tenant, _caller_domain, _caller_agent, _ = ScopePolicy._caller(request)
        if caller_tenant is None:
            raise HTTPException(status_code=403, detail="Admin key has no tenant scope.")
        admin_id = caller_tenant
    else:
        if admin_id is None:
            raise HTTPException(status_code=400, detail="platform_admin must specify admin_id.")

    instances = service.list_for_admin(
        admin_id=admin_id,
        active_only=active_only,
    )
    return [InstanceRead.model_validate(i) for i in instances]


@router.get(
    "/instances/{pk}",
    response_model=InstanceRead,
)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def get_luciel_instance(
    request: Request,
    pk: int,
    service: Annotated[InstanceService, Depends(get_luciel_instance_service)],
) -> InstanceRead:
    instance = service.get_by_pk(pk)
    if instance is None:
        raise HTTPException(status_code=404, detail=f"LucielInstance pk={pk} not found.")
    ScopePolicy.enforce_luciel_instance_scope(request, instance)
    return InstanceRead.model_validate(instance)


@router.patch(
    "/instances/{pk}",
    response_model=InstanceRead,
)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def update_luciel_instance(
    request: Request,
    pk: int,
    payload: InstanceUpdate,
    service: Annotated[InstanceService, Depends(get_luciel_instance_service)],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> InstanceRead:
    instance = service.get_by_pk(pk)
    if instance is None:
        raise HTTPException(status_code=404, detail=f"LucielInstance pk={pk} not found.")
    ScopePolicy.enforce_luciel_instance_scope(request, instance)

    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return InstanceRead.model_validate(instance)

    # Arc 15 WU1 — validate the config pillars against the MERGED row
    # (stored value where the PATCH did not supply one) so a partial
    # update is checked correctly. Two checks:
    #   (a) custom-axes cross-field rule against the effective preset
    #       (the schema can only check this when the preset is in the
    #       body; here we resolve it against the stored preset too).
    #   (b) tier-conditional rules (custom preset, business_context
    #       length, lead_routing presence).
    eff_preset = updates.get(
        "personality_preset", instance.personality_preset
    )
    eff_axes = (
        updates["personality_axes"]
        if "personality_axes" in updates
        else instance.personality_axes
    )
    from app.persona.presets import PRESET_CUSTOM, validate_custom_axes

    axes_problems: list[dict] = []
    if eff_preset == PRESET_CUSTOM:
        if not eff_axes:
            axes_problems.append(
                {
                    "field": "personality_axes",
                    "reason": "axes_required_for_custom",
                    "message": (
                        "personality_axes is required when "
                        "personality_preset is 'custom'."
                    ),
                }
            )
        else:
            for problem in validate_custom_axes(eff_axes):
                axes_problems.append(
                    {
                        "field": "personality_axes",
                        "reason": "invalid_axes",
                        "message": problem,
                    }
                )
    elif eff_axes:
        axes_problems.append(
            {
                "field": "personality_axes",
                "reason": "axes_not_allowed_for_named_preset",
                "message": (
                    "personality_axes may only be set when "
                    "personality_preset is 'custom'."
                ),
            }
        )

    admin_tier = _resolve_admin_tier_for_pillars(
        service.db, admin_id=instance.admin_id
    )
    eff_business_context = (
        updates["business_context"]
        if "business_context" in updates
        else instance.business_context
    )
    eff_lead_routing = (
        updates["lead_routing"]
        if "lead_routing" in updates
        else instance.lead_routing
    )
    pillar_problems = axes_problems + validate_pillars_for_tier(
        tier=admin_tier,
        personality_preset=eff_preset,
        business_context=eff_business_context,
        lead_routing=eff_lead_routing,
    )
    if pillar_problems:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "instance_config_invalid_for_tier",
                "tier": admin_tier,
                "problems": pillar_problems,
            },
        )

    updated = service.repo.update(instance, audit_ctx=audit_ctx, **updates)
    return InstanceRead.model_validate(updated)


# ---------------------------------------------------------------------
# Arc 11 Closeout PR-A — instance lifecycle routes.
#
# Customer Journey §4.5 Phase 8 mandates three distinct affordances on
# the "Manage account" surface: Pause / Delete / Close. The Close
# branch is the Arc 10 closure flow (already shipped). The Pause and
# Delete branches are the four routes below, with Resume / Restore as
# their respective reversals. Architecture §3.6.1 locks the 30-day
# grace window to ``soft_deleted_at``; the retention worker reads it.
# Vision §6.4 Reactivation re-mints embed keys on Restore.
# ---------------------------------------------------------------------


def _lifecycle_cascade_memory_items(
    *,
    request: Request,
    db,
    admin_id: str,
    pk: int,
    audit_ctx: AuditContext,
) -> None:
    """Shared memory_items soft-deactivate cascade.

    Wired at route level because InstanceService does not depend on
    AdminService (would be a circular import). Same posture as the
    pre-Arc-11-Closeout DELETE route; called from both /pause and
    DELETE to keep memory cleanly cascaded.
    """
    AdminService(db).bulk_soft_deactivate_memory_items_for_luciel_instance(
        admin_id=admin_id,
        luciel_instance_id=pk,
        audit_ctx=audit_ctx,
        updated_by=getattr(request.state, "actor_label", None),
    )


@router.post(
    "/instances/{pk}/pause",
    response_model=InstanceRead,
)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def pause_luciel_instance(
    request: Request,
    pk: int,
    service: Annotated[InstanceService, Depends(get_luciel_instance_service)],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
    db: DbSession,
) -> InstanceRead:
    """Pause an instance (Customer Journey §4.5 Phase 8 "Pause my Luciel").

    Widget begins returning 204 (empty <div>); knowledge + sessions are
    retained; reactivatable instantly via /resume. Memory items are
    soft-deactivated as a cascade so the widget surface goes fully
    quiet (no half-state where the widget is paused but memory writes
    keep landing).
    """
    instance = service.get_by_pk(pk)
    if instance is None:
        raise HTTPException(status_code=404, detail=f"Instance pk={pk} not found.")
    ScopePolicy.enforce_luciel_instance_scope(request, instance)

    _lifecycle_cascade_memory_items(
        request=request,
        db=db,
        admin_id=instance.admin_id,
        pk=pk,
        audit_ctx=audit_ctx,
    )

    try:
        paused = service.pause_instance(
            audit_ctx=audit_ctx,
            pk=pk,
            updated_by=getattr(request.state, "actor_label", None),
        )
    except InstanceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InstanceLifecycleConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "instance_lifecycle_conflict",
                "message": str(exc),
                "current_status": exc.current_status,
            },
        ) from exc
    return InstanceRead.model_validate(paused)


@router.post(
    "/instances/{pk}/resume",
    response_model=InstanceRead,
)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def resume_luciel_instance(
    request: Request,
    pk: int,
    service: Annotated[InstanceService, Depends(get_luciel_instance_service)],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> InstanceRead:
    """Resume a paused instance.

    Widget begins serving again. No key rotation (Pause was operational,
    not destructive). 409 if the instance is in the 'deleted' state —
    the right verb for that case is /restore.
    """
    instance = service.get_by_pk(pk)
    if instance is None:
        raise HTTPException(status_code=404, detail=f"Instance pk={pk} not found.")
    ScopePolicy.enforce_luciel_instance_scope(request, instance)

    try:
        resumed = service.resume_instance(
            audit_ctx=audit_ctx,
            pk=pk,
            updated_by=getattr(request.state, "actor_label", None),
        )
    except InstanceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InstanceLifecycleConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "instance_lifecycle_conflict",
                "message": str(exc),
                "current_status": exc.current_status,
            },
        ) from exc
    return InstanceRead.model_validate(resumed)


@router.delete(
    "/instances/{pk}",
    response_model=InstanceRead,
)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def delete_luciel_instance(
    request: Request,
    pk: int,
    service: Annotated[InstanceService, Depends(get_luciel_instance_service)],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
    db: DbSession,
) -> InstanceRead:
    """Soft-delete an instance (Customer Journey §4.5 Phase 8 "Delete
    this instance"). Stamps ``soft_deleted_at`` and opens the 30-day
    grace window per Architecture §3.6.1. The retention worker
    (``app.lifecycle.retention``) hard-deletes the row +
    its knowledge / conversations / leads / traces / api_keys cascade
    once the window elapses. Restorable via POST /instances/{pk}/restore
    within the window — keys are re-minted on restore per Vision §6.4.
    """
    instance = service.get_by_pk(pk)
    if instance is None:
        raise HTTPException(status_code=404, detail=f"Instance pk={pk} not found.")
    ScopePolicy.enforce_luciel_instance_scope(request, instance)

    _lifecycle_cascade_memory_items(
        request=request,
        db=db,
        admin_id=instance.admin_id,
        pk=pk,
        audit_ctx=audit_ctx,
    )

    try:
        deleted = service.delete_instance_with_grace(
            audit_ctx=audit_ctx,
            pk=pk,
            updated_by=getattr(request.state, "actor_label", None),
        )
    except InstanceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return InstanceRead.model_validate(deleted)


@router.post(
    "/instances/{pk}/restore",
    response_model=InstanceRead,
)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def restore_luciel_instance(
    request: Request,
    pk: int,
    service: Annotated[InstanceService, Depends(get_luciel_instance_service)],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
    db: DbSession,
) -> InstanceRead:
    """Restore a soft-deleted instance within the 30-day grace window.

    Per Vision §6.4 Reactivation: knowledge + conversations are
    reactivated, embed keys are re-minted (new keys, old keys stay
    revoked), capacity slot is consumed again. The new embed key is
    surfaced on the response under ``new_embed_key`` -- one-time read,
    never persisted to SSM (the admin must paste it into their site).
    Returns 410 Gone if the grace window has expired.
    """
    instance = service.get_by_pk(pk)
    if instance is None:
        raise HTTPException(status_code=404, detail=f"Instance pk={pk} not found.")
    ScopePolicy.enforce_luciel_instance_scope(request, instance)

    api_key_service = ApiKeyService(db)
    try:
        restored, new_embed_key = service.restore_instance(
            audit_ctx=audit_ctx,
            pk=pk,
            updated_by=getattr(request.state, "actor_label", None),
            api_key_service=api_key_service,
        )
    except InstanceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InstanceLifecycleConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "instance_lifecycle_conflict",
                "message": str(exc),
                "current_status": exc.current_status,
            },
        ) from exc
    except InstanceRestoreGraceExpiredError as exc:
        raise HTTPException(
            status_code=410,
            detail={
                "code": "instance_restore_grace_expired",
                "message": str(exc),
            },
        ) from exc

    payload = InstanceRead.model_validate(restored)
    if new_embed_key is not None:
        payload = payload.model_copy(update={"new_embed_key": new_embed_key})
    return payload




# ---------------------------------------------------------------------
# Bind-user (Step 28 Phase 2 - Commit 9)
#
# Platform-admin only. Binds an Agent row to a User identity row.
# Replaces the raw-SQL UPDATE that the verification harness Pillars
# 12/13/14 used to perform via a local SessionLocal() — a path that
# correctly fails when the harness runs from a least-privilege Pattern
# N task with the worker DSN (luciel_worker has no UPDATE on agents).
#
# Why a dedicated route (not piggybacking on PATCH /agents):
#   1. Auth: this route requires platform_admin. The general PATCH
#      route is tenant-admin scoped — binding identity is more
#      sensitive and should not be reachable via tenant-admin keys.
#   2. Audit: ACTION_UPDATE on RESOURCE_AGENT with the user_id diff
#      shows up cleanly in audit-log queries filtered by resource_type
#      = agent, action = update, before/after.user_id present.
#   3. Invariant: enforces "one active Agent per (user, tenant)" at
# ================================================================
# Step 25b — Knowledge ingestion / listing / replace / delete
#
# All routes guarded by ScopePolicy.enforce_luciel_instance_scope via
# _load_active_instance(). Audit row written in the same transaction
# as the mutation.
#
# Route ordering: /chunking-config (static) before /knowledge
# (prefix), /knowledge (static) before /knowledge/{source_id}
# (wildcard) so FastAPI matches correctly.
# ================================================================


@router.get(
    "/instances/{instance_id}/chunking-config",
    response_model=kschemas.EffectiveChunkingConfigRead,
)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def get_effective_chunking_config(
    request: Request,
    instance_id: int,
    db: DbSession,
    instance_service: Annotated[InstanceService, Depends(get_luciel_instance_service)],
    ingestion_service: Annotated[IngestionService, Depends(get_ingestion_service)],
) -> kschemas.EffectiveChunkingConfigRead:
    """Return the effective (instance -> domain -> tenant) chunking config
    for a Luciel instance. Diagnostic surface; doesn't ingest anything."""
    instance = _load_active_instance(
        request=request, instance_id=instance_id, instance_service=instance_service
    )
    cfg = ingestion_service._resolve_chunking_config(
        admin_id=instance.admin_id,
        luciel_instance_id=instance.id,
    )
    return kschemas.EffectiveChunkingConfigRead(
        chunk_size=cfg.chunk_size,
        chunk_overlap=cfg.chunk_overlap,
        chunk_strategy=cfg.chunk_strategy,
        size_source=cfg.size_source,
        overlap_source=cfg.overlap_source,
        strategy_source=cfg.strategy_source,
    )


# Cleanup B closeout: the seven legacy ``/instances/{instance_id}/
# knowledge*`` routes (upload_knowledge_file, ingest_knowledge_text,
# list_knowledge_sources, get_knowledge_source,
# delete_knowledge_source, replace_knowledge_source_text) were
# deleted. The Step-7 router at ``app/api/v1/admin_knowledge.py``
# is the single ingest + list + get + patch + delete surface from
# Cleanup B forward (paths under
# ``/admin/instances/{instance_id}/knowledge/sources/...``).


# ============================================================================
# Step 27b: Async worker queue-depth diagnostic
# ============================================================================
# Read-only operational endpoint. Platform-admin only. NOT audit-logged
# (diagnostic poll, not a mutation - Invariant 4 applies to mutations).
# Mirrors the Step 26b.2 pattern (verification/teardown-integrity):
# server-side AWS call so caller never needs SQS credentials.
#
# Returns ApproximateNumberOfMessages for both queues:
#   - luciel-memory-tasks (main)
#   - luciel-memory-dlq (dead-letter)
#
# Used by Pillar 11 and ops dashboards.

import logging as _logging_27b
from app.core.config import settings as _settings_27b

_logger_27b = _logging_27b.getLogger(__name__)


@router.get("/worker/queue-depth")
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def get_worker_queue_depth(
    request: Request,
    main_queue_name: str = Query(
        "luciel-memory-tasks",
        min_length=1,
        max_length=80,
        description="SQS main queue name",
    ),
    dlq_name: str = Query(
        "luciel-memory-dlq",
        min_length=1,
        max_length=80,
        description="SQS dead-letter queue name",
    ),
) -> dict:
    """Return ApproximateNumberOfMessages for the worker SQS queues.

    Platform-admin only. Read-only. No audit log row written (diagnostic).
    """
    if not ScopePolicy.is_platform_admin(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only platform_admin may read worker queue depth",
        )

    # Lazy boto3 import - keeps cold-start light for non-worker deploys.
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="boto3 not installed",
        )

    try:
        client = boto3.client("sqs", region_name=_settings_27b.aws_region)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"sqs client init failed: {type(exc).__name__}",
        ) from exc

    def _depth(queue_name: str) -> int | None:
        """Return ApproximateNumberOfMessages or None if queue is unreachable."""
        try:
            url = client.get_queue_url(QueueName=queue_name)["QueueUrl"]
            attrs = client.get_queue_attributes(
                QueueUrl=url,
                AttributeNames=["ApproximateNumberOfMessages"],
            )["Attributes"]
            return int(attrs.get("ApproximateNumberOfMessages", 0))
        except (BotoCoreError, ClientError) as exc:
            # Never echo raw AWS error strings - could leak account ids,
            # queue ARNs, etc. Only the exception class name.
            _logger_27b.warning(
                "queue-depth fetch failed queue=%s type=%s",
                queue_name, type(exc).__name__,
            )
            return None
        except Exception as exc:
            _logger_27b.warning(
                "queue-depth unexpected error queue=%s type=%s",
                queue_name, type(exc).__name__,
            )
            return None

    main_depth = _depth(main_queue_name)
    dlq_depth = _depth(dlq_name)

    if main_depth is None and dlq_depth is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="worker queues unreachable",
        )

    return {
        "region": _settings_27b.aws_region,
        "main_queue": {
            "name": main_queue_name,
            "approximate_messages": main_depth,
        },
        "dlq": {
            "name": dlq_name,
            "approximate_messages": dlq_depth,
        },
    }
# =====================================================================
# Arc 10 -- Lifecycle endpoints (closure, reactivation, data export).
# =====================================================================
#
# These routes are the customer-facing surface of the Arc 10 lifecycle
# subsystem. Each one composes a single service call and translates
# typed service errors to HTTP responses.
#
# All routes:
#   * require an admin-scoped session (admin_id resolved from
#     request.state.admin_id by the auth middleware -- not platform_admin)
#   * write via the tenant-scoped session that emits app.admin_id GUC
#     so RLS policies on the new tables (data_export_jobs) fire
#     correctly
#   * emit one audit row per state transition via the services
# =====================================================================

from app.schemas.lifecycle import (
    AccountCloseRequest,
    AccountCloseResponse,
    DataExportJobResponse,
    DataExportReadyResponse,
    LifecycleStateResponse,
    ReactivationCompleteRequest,
    ReactivationCompleteResponse,
    ReactivationStageRequest,
    ReactivationStageResponse,
)
from app.lifecycle.closure import (
    AccountAlreadyClosedError,
    AccountAlreadyTombstoneError,
    AccountNotFoundError,
    ClosureService,
    InvalidCancelModeError,
    InvalidConfirmationError,
)
from app.services.reactivation_service import (
    AccountAlreadyTombstoneError as ReactAccountTombstoneError,
    AccountNotInGraceError,
    ReactivationError,
    ReactivationService,
    ReactivationWindowExpiredError,
    StripeReactivationCheckoutFailedError,
    StripeSubscriptionMismatchError,
)
from app.services.data_export_service import (
    DataExportService,
    ExportAlreadyInFlightError,
    ExportFreeGateError,
    ExportNotFoundError,
    ExportNotReadyError,
)


def _require_admin_id(request: Request) -> str:
    # Helper -- centralizes the "you must be authenticated as an
    # admin" check. The closure / reactivation / export endpoints
    # all require an admin scope; an embed key or unauthenticated
    # request must 401 before reaching the service layer.
    admin_id = getattr(request.state, "admin_id", None)
    if not admin_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin authentication required.",
        )
    return admin_id


@router.post(
    "/account/close",
    response_model=AccountCloseResponse,
    status_code=status.HTTP_200_OK,
)
def close_account(
    request: Request,
    body: AccountCloseRequest,
    db: DbSession,
) -> AccountCloseResponse:
    # Vision 6.3: admin-initiated closure with optional pre-closure
    # data export. Starts the 30-day grace clock. The hard-delete
    # cascade runs 30 days later via the retention worker.
    admin_id = _require_admin_id(request)
    audit_ctx = AuditContext.from_request(request)

    # Compose services. Each is request-scoped.
    from app.services.admin_service import AdminService
    from app.services.billing_service import BillingService
    from app.integrations.stripe import get_stripe_client
    admin_svc = AdminService(db)
    billing_svc = BillingService(db, get_stripe_client())
    audit_repo = AdminAuditRepository(db)
    # Data export service constructed lazily only when needed -- the
    # S3 client / bucket settings are not required for closures that
    # do not request an export.
    if body.request_export:
        from app.core.config import settings
        import boto3
        s3 = boto3.client("s3", region_name=settings.aws_region)
        data_export_svc = DataExportService(
            db=db,
            s3_client=s3,
            s3_bucket=getattr(settings, "data_export_bucket", "luciel-data-exports"),
            audit_repository=audit_repo,
        )
    else:
        data_export_svc = None

    # luciel_instance_service wiring matches the cascade route at
    # line ~320 of this file. AgentRepository was deleted at Arc 5
    # Path A (Commit A5); the cascade no longer touches the dropped
    # `agents` / `agent_configs` tables (see
    # app/services/admin_service.py::deactivate_tenant_with_cascade
    # and D-arc10-close-path-imports-deleted-agent-repository-
    # 2026-05-27). Passing agent_repo=None here aligns with the
    # /tenants/{admin_id} PATCH route's contract.
    from app.services.instance_service import InstanceService
    instance_svc = InstanceService(db)
    agent_repo = None

    closure_svc = ClosureService(
        db,
        admin_service=admin_svc,
        billing_service=billing_svc,
        data_export_service=data_export_svc,
        audit_repository=audit_repo,
    )

    try:
        outcome = closure_svc.initiate_closure(
            admin_id=admin_id,
            cancel_mode=body.cancel_mode,
            confirm_account_name=body.confirm_account_name,
            request_export=body.request_export,
            audit_ctx=audit_ctx,
            luciel_instance_service=instance_svc,
            agent_repo=agent_repo,
        )
    except InvalidConfirmationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Account-name confirmation did not match.",
        ) from exc
    except InvalidCancelModeError as exc:
        # Pydantic should have caught this at the route boundary;
        # belt-and-suspenders.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except AccountAlreadyClosedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except AccountAlreadyTombstoneError as exc:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=str(exc)) from exc

    db.commit()
    return AccountCloseResponse(
        admin_id=outcome.admin_id,
        closure_initiated_at=outcome.closure_initiated_at,
        grace_window_expires_at=outcome.grace_window_expires_at,
        cancel_mode=outcome.cancel_mode,
        stripe_cancellation_applied=outcome.stripe_cancellation_applied,
        data_export_job_id=outcome.data_export_job_id,
    )


@router.post(
    "/account/reactivate/stage",
    response_model=ReactivationStageResponse,
    status_code=status.HTTP_200_OK,
)
def reactivate_stage(
    request: Request,
    body: ReactivationStageRequest,
    db: DbSession,
) -> ReactivationStageResponse:
    # Vision 6.4 phase 1: admin within 30-day grace requests a new
    # Stripe checkout to re-subscribe. No DB mutation in this phase.
    admin_id = _require_admin_id(request)
    from app.services.billing_service import BillingService
    from app.integrations.stripe import get_stripe_client
    billing_svc = BillingService(db, get_stripe_client())
    audit_repo = AdminAuditRepository(db)

    react_svc = ReactivationService(
        db,
        billing_service=billing_svc,
        audit_repository=audit_repo,
    )
    try:
        staged = react_svc.stage_reactivation(
            admin_id=admin_id,
            target_tier=body.target_tier,
            success_url=body.success_url,
            cancel_url=body.cancel_url,
        )
    except AccountNotInGraceError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ReactivationWindowExpiredError as exc:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=str(exc)) from exc
    except ReactAccountTombstoneError as exc:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=str(exc)) from exc
    except StripeReactivationCheckoutFailedError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    return ReactivationStageResponse(
        admin_id=staged.admin_id,
        closure_initiated_at=staged.closure_initiated_at,
        grace_window_expires_at=staged.grace_window_expires_at,
        stripe_checkout_url=staged.stripe_checkout_url,
        stripe_checkout_session_id=staged.stripe_checkout_session_id,
    )


@router.post(
    "/account/reactivate/complete",
    response_model=ReactivationCompleteResponse,
    status_code=status.HTTP_200_OK,
)
def reactivate_complete(
    request: Request,
    body: ReactivationCompleteRequest,
    db: DbSession,
) -> ReactivationCompleteResponse:
    # Vision 6.4 phase 2: Stripe has confirmed the new subscription;
    # run the inverse cascade and clear closure stamps.
    admin_id = _require_admin_id(request)
    audit_ctx = AuditContext.from_request(request)
    from app.services.billing_service import BillingService
    from app.integrations.stripe import get_stripe_client
    billing_svc = BillingService(db, get_stripe_client())
    audit_repo = AdminAuditRepository(db)

    react_svc = ReactivationService(
        db,
        billing_service=billing_svc,
        audit_repository=audit_repo,
    )
    try:
        completed = react_svc.complete_reactivation(
            admin_id=admin_id,
            stripe_checkout_session_id=body.stripe_checkout_session_id,
            audit_ctx=audit_ctx,
        )
    except AccountNotInGraceError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ReactivationWindowExpiredError as exc:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=str(exc)) from exc
    except ReactAccountTombstoneError as exc:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=str(exc)) from exc
    except StripeSubscriptionMismatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except ReactivationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc

    db.commit()
    return ReactivationCompleteResponse(
        admin_id=completed.admin_id,
        reactivated_at=completed.reactivated_at,
        new_subscription_id=completed.new_subscription_id,
        instances_restored=completed.instances_restored,
        api_keys_revoked_count=completed.api_keys_revoked_count,
        team_members_restored=completed.team_members_restored,
    )


@router.post(
    "/account/export",
    response_model=DataExportJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def request_data_export(
    request: Request,
    db: DbSession,
) -> DataExportJobResponse:
    # Standalone export request. Used both by an active admin
    # exercising GDPR data-portability and by an admin within the
    # closure grace window who did not check the export box at
    # closure time.
    admin_id = _require_admin_id(request)
    audit_ctx = AuditContext.from_request(request)

    # Determine triggered_by by reading the admin's closure state.
    from app.models.admin import Admin
    admin = db.get(Admin, admin_id)
    if admin is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin not found.")
    if admin.hard_deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Account already hard-deleted.")
    triggered_by = (
        "grace_window_request"
        if admin.closure_initiated_at is not None
        else "admin_request"
    )

    from app.core.config import settings
    import boto3
    s3 = boto3.client("s3", region_name=settings.aws_region)
    audit_repo = AdminAuditRepository(db)
    data_export_svc = DataExportService(
        db=db,
        s3_client=s3,
        s3_bucket=getattr(settings, "data_export_bucket", "luciel-data-exports"),
        audit_repository=audit_repo,
    )

    try:
        job = data_export_svc.enqueue(
            admin_id=admin_id,
            triggered_by=triggered_by,
            tier_at_request=admin.tier,
            audit_ctx=audit_ctx,
            # RESCAN TIER-DE §5.10: pass closure state so the service can
            # enforce the Free=closure-only gate.
            closure_initiated_at=admin.closure_initiated_at,
        )
    except ExportFreeGateError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    except ExportAlreadyInFlightError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    db.commit()

    # The Celery task is dispatched by the route layer rather than by
    # the service so the service stays test-friendly without a Celery
    # dependency. We import the task lazily to avoid a circular import
    # at module load.
    from app.worker.tasks.data_export import generate_export_bundle
    generate_export_bundle.delay(job.id)

    return DataExportJobResponse(
        id=job.id,
        admin_id=job.admin_id,
        status=job.status,
        requested_at=job.requested_at,
        tier_at_request=job.tier_at_request,
        triggered_by=job.triggered_by,
        ready_at=job.ready_at,
        signed_url_expires_at=job.signed_url_expires_at,
    )


@router.get(
    "/account/export/{job_id}",
    response_model=None,  # response varies (job-status vs signed-url)
    status_code=status.HTTP_200_OK,
)
def get_data_export(
    request: Request,
    job_id: str,
    db: DbSession,
):
    # Poll status. If ready -> returns DataExportReadyResponse with
    # the signed URL. Otherwise returns DataExportJobResponse with
    # the current status.
    admin_id = _require_admin_id(request)
    from app.core.config import settings
    import boto3
    s3 = boto3.client("s3", region_name=settings.aws_region)
    audit_repo = AdminAuditRepository(db)
    data_export_svc = DataExportService(
        db=db,
        s3_client=s3,
        s3_bucket=getattr(settings, "data_export_bucket", "luciel-data-exports"),
        audit_repository=audit_repo,
    )

    # Read the job row.
    from sqlalchemy import text as sql_text
    row = db.execute(
        sql_text(
            """
            SELECT id, admin_id, status, requested_at, tier_at_request,
                   triggered_by, ready_at, signed_url_expires_at,
                   bytes_size
              FROM data_export_jobs
             WHERE id = :id
               AND admin_id = :aid
            """
        ),
        {"id": job_id, "aid": admin_id},
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Export job not found.")
    (job_pk, job_admin, job_status, job_req_at, job_tier,
     job_trigger, job_ready_at, job_expires_at, job_bytes) = row

    if job_status != "ready":
        return DataExportJobResponse(
            id=str(job_pk),
            admin_id=job_admin,
            status=job_status,
            requested_at=job_req_at,
            tier_at_request=job_tier,
            triggered_by=job_trigger,
            ready_at=job_ready_at,
            signed_url_expires_at=job_expires_at,
        )

    try:
        signed_url, expires_at = data_export_svc.get_signed_url(
            job_id=str(job_pk),
            admin_id=admin_id,
        )
    except ExportNotReadyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ExportNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return DataExportReadyResponse(
        id=str(job_pk),
        admin_id=job_admin,
        status="ready",
        signed_url=signed_url,
        signed_url_expires_at=expires_at,
        bytes_size=int(job_bytes or 0),
    )


@router.get(
    "/account/lifecycle-state",
    response_model=LifecycleStateResponse,
    status_code=status.HTTP_200_OK,
)
def get_lifecycle_state(
    request: Request,
    db: DbSession,
) -> LifecycleStateResponse:
    """Return authoritative lifecycle state for the cookied admin.

    Arc 10 re-open Gap 1: the original frontend sourced the closure-grace
    banner state from localStorage, which fails the obvious case where an
    admin closes on one device and signs in on another. This route is the
    server-sourced replacement. Cheap (single SELECT on admins) so the
    /account and /dashboard pages can call it on every mount.

    Returns 200 with closed=false / in_grace=false / hard_deleted=false
    for admins that have never been closed -- callers branch on the
    booleans rather than on the HTTP status.
    """
    admin_id = _require_admin_id(request)

    from app.lifecycle.closure import ClosureService
    audit_repo = AdminAuditRepository(db)  # not used for the read but
    # ClosureService's __init__ requires it (write-path concern). Cheap
    # to construct; no side effects.
    closure_svc = ClosureService(db=db, audit_repository=audit_repo)

    try:
        state = closure_svc.get_lifecycle_state(admin_id)
    except AccountNotFoundError as exc:
        # Cookie carried a stale admin_id whose row was removed. This is
        # not a "you don't exist" case in the normal flow -- but a 404
        # here lets the frontend force a sign-out cleanly.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    return LifecycleStateResponse(
        admin_id=state.admin_id,
        closed=state.closed,
        in_grace=state.in_grace,
        hard_deleted=state.hard_deleted,
        cancel_mode=state.cancel_mode,
        closure_initiated_at=state.closure_initiated_at,
        grace_window_expires_at=state.grace_window_expires_at,
        hard_deleted_at=state.hard_deleted_at,
    )
