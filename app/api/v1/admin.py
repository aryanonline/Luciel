
# --- Add to imports at top of admin.py ---
from app.schemas.onboarding import (
    TenantOnboardRequest,
    TenantOnboardResponse,
    OnboardedTenantSummary,
    OnboardedDomainSummary,
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
    get_api_key_or_ip,
    ADMIN_RATE_LIMIT,
    KNOWLEDGE_UPLOAD_RATE_LIMIT,
)
from app.schemas.admin import (
    AgentConfigCreate,
    AgentConfigRead,
    AgentConfigUpdate,
    DomainConfigCreate,
    DomainConfigRead,
    DomainConfigUpdate,
    KnowledgeIngestRequest,
    KnowledgeIngestResponse,
    TenantConfigCreate,
    TenantConfigRead,
    TenantConfigUpdate,
)
from app.schemas.api_key import ApiKeyCreate, ApiKeyCreateResponse, ApiKeyRead
from app.services.admin_service import AdminService
from app.services.api_key_service import ApiKeyService
from app.policy.scope import ScopePolicy

router = APIRouter(prefix="/admin", tags=["admin"])

# --- Add this endpoint BEFORE the individual /tenants POST route ---
# (FastAPI matches routes top-down, so /tenants/onboard must come
#  before /tenants/{tenant_id} to avoid treating "onboard" as a tenant_id)

@router.post(
    "/tenants/onboard",
    response_model=TenantOnboardResponse,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
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

    Creates TenantConfig, default DomainConfig, PIPEDA retention policies,
    and the tenant's first API key atomically. If anything fails,
    nothing is created.

    The raw API key is returned once. Store it securely.
    """
    service = OnboardingService(db)

    try:
        result = service.onboard_tenant(
            tenant_id=payload.tenant_id,
            display_name=payload.display_name,
            description=payload.description,
            escalation_contact=payload.escalation_contact,
            system_prompt_additions=payload.system_prompt_additions,
            default_domain_id=payload.default_domain_id,
            default_domain_display_name=payload.default_domain_display_name,
            default_domain_description=payload.default_domain_description,
            api_key_display_name=payload.api_key_display_name,
            api_key_permissions=payload.api_key_permissions,
            api_key_rate_limit=payload.api_key_rate_limit,
            retention_days_sessions=payload.retention_days_sessions,
            retention_days_messages=payload.retention_days_messages,
            retention_days_memory_items=payload.retention_days_memory_items,
            retention_days_traces=payload.retention_days_traces,
            retention_days_knowledge=payload.retention_days_knowledge,
            created_by=payload.created_by,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    tenant = result["tenant"]
    domain = result["default_domain"]
    # api_key = result["api_key"]
    # raw_key = result["raw_api_key"]
    # policies = result["retention_policies"]

    return TenantOnboardResponse(
        tenant=OnboardedTenantSummary.model_validate(tenant),
        default_domain=OnboardedDomainSummary.model_validate(domain),
        chat_api_key=OnboardedApiKeySummary(
            key_prefix=result["chat_api_key"].key_prefix,
            display_name=result["chat_api_key"].display_name,
            permissions=result["chat_api_key"].permissions,
            rate_limit=result["chat_api_key"].rate_limit,
            raw_key=result["chat_raw_key"],
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
        message=f"Tenant '{payload.tenant_id}' onboarded successfully",
    )

@router.post("/tenants", response_model=TenantConfigRead, status_code=status.HTTP_201_CREATED)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def create_tenant(
    request: Request,
    payload: TenantConfigCreate,
    db: DbSession,
) -> TenantConfigRead:

    if not ScopePolicy.is_platform_admin(request):
        raise HTTPException(status_code=403, detail="Only platform_admin may create tenants")

    service = AdminService(db)
    existing = service.get_tenant_config(payload.tenant_id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Tenant {payload.tenant_id} already exists",
        )

    config = service.create_tenant_config(**payload.model_dump())
    return TenantConfigRead.model_validate(config)


@router.get("/tenants", response_model=list[TenantConfigRead])
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def list_tenants(
    request: Request,
    db: DbSession,
) -> list[TenantConfigRead]:
    service = AdminService(db)
    if ScopePolicy.is_platform_admin(request):
        configs = service.list_tenant_configs()
    else:
        caller_tenant = getattr(request.state, "tenant_id", None)
        cfg = service.get_tenant_config(caller_tenant) if caller_tenant else None
        configs = [cfg] if cfg else []
    return [TenantConfigRead.model_validate(c) for c in configs]


@router.get("/tenants/{tenant_id}", response_model=TenantConfigRead)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def get_tenant(
    request: Request,
    tenant_id: str,
    db: DbSession,
) -> TenantConfigRead:
    ScopePolicy.enforce_tenant_scope(request, tenant_id)
    service = AdminService(db)
    config = service.get_tenant_config(tenant_id)
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant not found",
        )
    return TenantConfigRead.model_validate(config)


@router.patch("/tenants/{tenant_id}", response_model=TenantConfigRead)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def update_tenant(
    request: Request,
    tenant_id: str,
    payload: TenantConfigUpdate,
    db: DbSession,
) -> TenantConfigRead:
    ScopePolicy.enforce_tenant_scope(request, tenant_id)
    service = AdminService(db)
    config = service.update_tenant_config(
        tenant_id,
        **payload.model_dump(exclude_unset=True),
    )
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant not found",
        )
    return TenantConfigRead.model_validate(config)


@router.post("/domains", response_model=DomainConfigRead, status_code=status.HTTP_201_CREATED)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def create_domain(
    request: Request,
    payload: DomainConfigCreate,
    db: DbSession,
) -> DomainConfigRead:
    ScopePolicy.enforce_tenant_scope(request, payload.tenant_id)
    # domain-scoped keys cannot create a different domain
    caller_domain = getattr(request.state, "domain_id", None)
    if caller_domain and caller_domain != payload.domain_id and not ScopePolicy.is_platform_admin(request):
        raise HTTPException(status_code=403, detail="This key is scoped to a different domain")
    service = AdminService(db)
    existing = service.get_domain_config(payload.tenant_id, payload.domain_id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Domain {payload.domain_id} for tenant {payload.tenant_id} already exists",
        )

    config = service.create_domain_config(**payload.model_dump())
    return DomainConfigRead.model_validate(config)


@router.get("/domains", response_model=list[DomainConfigRead])
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def list_domains(
    request: Request,
    db: DbSession,
    tenant_id: str | None = Query(default=None),
) -> list[DomainConfigRead]:
    service = AdminService(db)
    if not ScopePolicy.is_platform_admin(request):
        caller_tenant = getattr(request.state, "tenant_id", None)
        # Force scope: non-platform callers can only see their own tenant.
        tenant_id = caller_tenant
    configs = service.list_domain_configs(tenant_id=tenant_id)
    # If caller is domain-scoped, filter further.
    caller_domain = getattr(request.state, "domain_id", None)
    if caller_domain and not ScopePolicy.is_platform_admin(request):
        configs = [c for c in configs if c.domain_id == caller_domain]
    return [DomainConfigRead.model_validate(c) for c in configs]


@router.get("/domains/{tenant_id}/{domain_id}", response_model=DomainConfigRead)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def get_domain(
    request: Request,
    tenant_id: str,
    domain_id: str,
    db: DbSession,
) -> DomainConfigRead:
    ScopePolicy.enforce_domain_scope(request, tenant_id, domain_id)
    service = AdminService(db)
    config = service.get_domain_config(tenant_id, domain_id)
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Domain config not found",
        )
    return DomainConfigRead.model_validate(config)


@router.patch("/domains/{tenant_id}/{domain_id}", response_model=DomainConfigRead)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def update_domain(
    request: Request,
    tenant_id: str,
    domain_id: str,
    payload: DomainConfigUpdate,
    db: DbSession,
) -> DomainConfigRead:
    ScopePolicy.enforce_domain_scope(request, tenant_id, domain_id)
    service = AdminService(db)
    config = service.update_domain_config(
        tenant_id, domain_id, **payload.model_dump(exclude_unset=True),
    )
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Domain config not found",
        )
    return DomainConfigRead.model_validate(config)

@router.delete("/domains/{tenant_id}/{domain_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def deactivate_domain(
    request: Request,
    tenant_id: str,
    domain_id: str,
    db: DbSession,
) -> None:
    ScopePolicy.enforce_domain_scope(request, tenant_id, domain_id)
    service = AdminService(db)
    success = service.deactivate_domain(tenant_id, domain_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Domain config not found",
        )

@router.post("/agents", response_model=AgentConfigRead, status_code=status.HTTP_201_CREATED)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def create_agent(
    request: Request,
    payload: AgentConfigCreate,
    db: DbSession,
) -> AgentConfigRead:
    # Scope: caller must match tenant (and domain if scoped).
    target_domain_id = getattr(payload, "domain_id", None)
    ScopePolicy.enforce_domain_scope(request, payload.tenant_id, target_domain_id)

    service = AdminService(db)

    # Hierarchy validation: parent domain must exist and be active.
    if target_domain_id:
        if not service.validate_domain_active(payload.tenant_id, target_domain_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Domain '{target_domain_id}' does not exist or is inactive "
                    f"for tenant '{payload.tenant_id}'"
                ),
            )

    existing = service.get_agent_config(payload.tenant_id, payload.agent_id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Agent {payload.agent_id} for tenant {payload.tenant_id} already exists",
        )
    config = service.create_agent_config(**payload.model_dump())
    return AgentConfigRead.model_validate(config)


@router.get("/agents", response_model=list[AgentConfigRead])
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def list_agents(
    request: Request,
    db: DbSession,
    tenant_id: str | None = Query(default=None),
) -> list[AgentConfigRead]:
    service = AdminService(db)
    if not ScopePolicy.is_platform_admin(request):
        tenant_id = getattr(request.state, "tenant_id", None)
    configs = service.list_agent_configs(tenant_id=tenant_id)

    caller_domain = getattr(request.state, "domain_id", None)
    caller_agent = getattr(request.state, "agent_id", None)
    if caller_agent and not ScopePolicy.is_platform_admin(request):
        configs = [c for c in configs if c.agent_id == caller_agent]
    elif caller_domain and not ScopePolicy.is_platform_admin(request):
        configs = [c for c in configs if getattr(c, "domain_id", None) == caller_domain]

    return [AgentConfigRead.model_validate(c) for c in configs]

@router.get("/agents/{tenant_id}/by-domain/{domain_id}", response_model=list[AgentConfigRead])
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def list_agents_by_domain(
    request: Request,
    tenant_id: str,
    domain_id: str,
    db: DbSession,
) -> list[AgentConfigRead]:
    ScopePolicy.enforce_domain_scope(request, tenant_id, domain_id)
    service = AdminService(db)
    configs = service.list_agent_configs_by_domain(tenant_id, domain_id)
    return [AgentConfigRead.model_validate(c) for c in configs]


@router.get("/agents/{tenant_id}/{agent_id}", response_model=AgentConfigRead)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def get_agent(
    request: Request,
    tenant_id: str,
    agent_id: str,
    db: DbSession,
) -> AgentConfigRead:
    ScopePolicy.enforce_tenant_scope(request, tenant_id)
    service = AdminService(db)
    config = service.get_agent_config(tenant_id, agent_id)
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent config not found",
        )
    # If caller is agent-scoped, it can only read itself.
    caller_agent = getattr(request.state, "agent_id", None)
    if caller_agent and caller_agent != agent_id and not ScopePolicy.is_platform_admin(request):
        raise HTTPException(status_code=403, detail="This key is scoped to a different agent")
    # If caller is domain-scoped, the agent must belong to that domain.
    caller_domain = getattr(request.state, "domain_id", None)
    if (caller_domain and getattr(config, "domain_id", None) != caller_domain
            and not ScopePolicy.is_platform_admin(request)):
        raise HTTPException(status_code=403, detail="Agent belongs to a different domain")
    return AgentConfigRead.model_validate(config)


@router.patch("/agents/{tenant_id}/{agent_id}", response_model=AgentConfigRead)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def update_agent(
    request: Request,
    tenant_id: str,
    agent_id: str,
    payload: AgentConfigUpdate,
    db: DbSession,
) -> AgentConfigRead:
    ScopePolicy.enforce_tenant_scope(request, tenant_id)
    service = AdminService(db)
    existing = service.get_agent_config(tenant_id, agent_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Agent config not found")

    caller_domain = getattr(request.state, "domain_id", None)
    caller_agent = getattr(request.state, "agent_id", None)
    if (caller_domain and getattr(existing, "domain_id", None) != caller_domain
            and not ScopePolicy.is_platform_admin(request)):
        raise HTTPException(status_code=403, detail="Agent belongs to a different domain")
    if caller_agent and caller_agent != agent_id and not ScopePolicy.is_platform_admin(request):
        raise HTTPException(status_code=403, detail="This key is scoped to a different agent")

    config = service.update_agent_config(
        tenant_id, agent_id, **payload.model_dump(exclude_unset=True),
    )
    return AgentConfigRead.model_validate(config)

@router.delete("/agents/{tenant_id}/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def deactivate_agent(
    request: Request,
    tenant_id: str,
    agent_id: str,
    db: DbSession,
) -> None:
    ScopePolicy.enforce_tenant_scope(request, tenant_id)
    service = AdminService(db)
    existing = service.get_agent_config(tenant_id, agent_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Agent config not found")

    caller_domain = getattr(request.state, "domain_id", None)
    if (caller_domain and getattr(existing, "domain_id", None) != caller_domain
            and not ScopePolicy.is_platform_admin(request)):
        raise HTTPException(status_code=403, detail="Agent belongs to a different domain")

    service.deactivate_agent(tenant_id, agent_id)



@router.post("/knowledge/ingest", response_model=KnowledgeIngestResponse)
@limiter.limit(KNOWLEDGE_UPLOAD_RATE_LIMIT, key_func=get_api_key_or_ip)
def ingest_knowledge(
    request: Request,
    payload: KnowledgeIngestRequest,
    ingestion: Annotated[IngestionService, Depends(get_ingestion_service)],
) -> KnowledgeIngestResponse:
    # Scope enforcement: caller must match tenant, domain (if scoped),
    # and agent (if scoped) of the target knowledge record.
    ScopePolicy.enforce_agent_scope(
        request,
        payload.tenant_id,
        payload.domain_id,
        payload.agent_id,
    )

    # Additional rule: a domain-scoped caller (no agent_id) cannot ingest
    # agent-level knowledge for an agent outside their domain.
    # enforce_agent_scope already covers (caller_domain vs payload.domain_id),
    # so this is belt-and-suspenders in case payload.domain_id is None but
    # payload.agent_id is set.
    caller_domain = getattr(request.state, "domain_id", None)
    caller_agent = getattr(request.state, "agent_id", None)
    if (caller_agent and payload.agent_id != caller_agent
            and not ScopePolicy.is_platform_admin(request)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Agent-scoped key may only ingest its own knowledge",
        )
    if (caller_domain and payload.domain_id and payload.domain_id != caller_domain
            and not ScopePolicy.is_platform_admin(request)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Domain-scoped key may only ingest knowledge within its domain",
        )

    try:
        chunks_stored = ingestion.ingest(
            content=payload.content,
            knowledge_type=payload.knowledge_type,
            tenant_id=payload.tenant_id,
            domain_id=payload.domain_id,
            agent_id=payload.agent_id,
            title=payload.title,
            source=payload.source,
            created_by=payload.created_by,
            max_chunk_size=payload.max_chunk_size,
            replace_existing=payload.replace_existing,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return KnowledgeIngestResponse(
        chunks_stored=chunks_stored,
        knowledge_type=payload.knowledge_type,
        tenant_id=payload.tenant_id,
        domain_id=payload.domain_id,
        agent_id=payload.agent_id,
        source=payload.source,
    )


@router.post("/api-keys", response_model=ApiKeyCreateResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def create_api_key(
    request: Request,
    payload: ApiKeyCreate,
    db: DbSession,
) -> ApiKeyCreateResponse:
    # Scope: a caller can only mint keys at or below their own scope.
    ScopePolicy.enforce_agent_scope(
        request,
        payload.tenant_id,
        payload.domain_id,
        payload.agent_id,
    )
    # Prevent privilege escalation (non-platform_admin cannot grant platform_admin).
    ScopePolicy.enforce_no_privilege_escalation(request, payload.permissions or [])

    # Extra rule: a domain-scoped caller cannot mint a tenant-wide key
    # (i.e. target domain_id must be set and match caller_domain).
    caller_domain = getattr(request.state, "domain_id", None)
    caller_agent = getattr(request.state, "agent_id", None)
    if caller_domain and not ScopePolicy.is_platform_admin(request):
        if payload.domain_id is None:
            raise HTTPException(
                status_code=403,
                detail="Domain-scoped key may not mint tenant-wide keys",
            )
    if caller_agent and not ScopePolicy.is_platform_admin(request):
        if payload.agent_id is None or payload.agent_id != caller_agent:
            raise HTTPException(
                status_code=403,
                detail="Agent-scoped key may only mint keys for itself",
            )

    service = ApiKeyService(db)
    api_key, raw_key = service.create_key(
        tenant_id=payload.tenant_id,
        domain_id=payload.domain_id,
        agent_id=payload.agent_id,
        display_name=payload.display_name,
        permissions=payload.permissions,
        rate_limit=payload.rate_limit,
        created_by=payload.created_by,
    )
    return ApiKeyCreateResponse(
        api_key=ApiKeyRead.model_validate(api_key),
        raw_key=raw_key,
    )


@router.get("/api-keys", response_model=list[ApiKeyRead])
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def list_api_keys(
    request: Request,
    db: DbSession,
    tenant_id: str | None = Query(default=None),
) -> list[ApiKeyRead]:
    service = ApiKeyService(db)

    if not ScopePolicy.is_platform_admin(request):
        # Force tenant filter to caller's own tenant.
        tenant_id = getattr(request.state, "tenant_id", None)

    keys = service.list_keys(tenant_id=tenant_id)

    caller_domain = getattr(request.state, "domain_id", None)
    caller_agent = getattr(request.state, "agent_id", None)
    if caller_agent and not ScopePolicy.is_platform_admin(request):
        keys = [k for k in keys if k.agent_id == caller_agent]
    elif caller_domain and not ScopePolicy.is_platform_admin(request):
        keys = [k for k in keys if k.domain_id == caller_domain]

    return [ApiKeyRead.model_validate(k) for k in keys]


@router.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def deactivate_api_key(
    request: Request,
    key_id: int,
    db: DbSession,
) -> None:
    service = ApiKeyService(db)
    # Fetch first so we can enforce scope on the target.
    target = service.get_key_by_id(key_id) if hasattr(service, "get_key_by_id") else None
    if target is None:
        # Fall back: just try deactivate; 404 if not found.
        success = service.deactivate_key(key_id)
        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="API key not found",
            )
        return

    ScopePolicy.enforce_agent_scope(
        request, target.tenant_id, target.domain_id, target.agent_id,
    )
    success = service.deactivate_key(key_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="API key not found",
        )