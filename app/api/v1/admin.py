
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
    ACTION_DEACTIVATE,
    ACTION_UPDATE,
    RESOURCE_AGENT,
    RESOURCE_LUCIEL_INSTANCE,
)
from fastapi import UploadFile, File, Form

# Step 25b — knowledge ingestion
from app.schemas import knowledge as kschemas
from app.knowledge.ingestion import IngestionError, IngestResult
from app.knowledge.chunker import EffectiveChunkingConfig
from app.models.admin_audit_log import (
    ACTION_KNOWLEDGE_DELETE,
    ACTION_KNOWLEDGE_INGEST,
    ACTION_KNOWLEDGE_REPLACE,
    RESOURCE_KNOWLEDGE,
)
from app.repositories.knowledge_repository import KnowledgeRepository
from app.services.luciel_instance_service import (
    InstanceNotFoundError,
    LucielInstanceService,
)
from app.repositories.admin_audit_repository import AdminAuditRepository, AuditContext
from app.repositories.agent_repository import AgentRepository
from app.schemas.agent import AgentBindUserPayload, AgentCreate, AgentRead, AgentUpdate
from app.schemas.luciel_instance import (
    LucielInstanceCreate,
    LucielInstanceRead,
    LucielInstanceUpdate,
)
from app.services.luciel_instance_service import (
    DuplicateInstanceError,
    InstanceNotFoundError,
    LucielInstanceService,
    ParentScopeInactiveError,
)

from app.policy.scope import ScopePolicy
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
from app.services.scope_prompt_preflight import (
    ScopePromptMissingError,
    ScopePromptPreflight,
)
from app.schemas.memory import MemoryRead
from app.policy.scope import ScopePolicy
from app.models.luciel_instance import LucielInstance

router = APIRouter(prefix="/admin", tags=["admin"])

def _load_active_instance(
    *,
    request: Request,
    instance_id: int,
    instance_service: LucielInstanceService,
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

    # P3-A: capture caller identity once at the request boundary so
    # the four audit rows OnboardingService emits are attributed to
    # the platform_admin who actually performed the onboarding, not
    # to AuditContext.system().
    audit_ctx = AuditContext.from_request(request)

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
            audit_ctx=audit_ctx,
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
            f"Tenant {payload.tenant_id} onboarded. Use the admin key to "
            f"create your first LucielInstance and its chat key."
        ),
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
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
    luciel_service: Annotated[LucielInstanceService, Depends(get_luciel_instance_service)],
    agent_repo: Annotated[AgentRepository, Depends(get_agent_repository)],
) -> TenantConfigRead:
    ScopePolicy.enforce_tenant_scope(request, tenant_id)
    service = AdminService(db)
    payload_data = payload.model_dump(exclude_unset=True)

    # Tenant deactivation routes through the cascade-aware spine.
    # All other updates use the generic update_tenant_config path.
    if payload_data.get("active") is False:
        deactivated = service.deactivate_tenant_with_cascade(
            tenant_id,
            audit_ctx=audit_ctx,
            luciel_instance_service=luciel_service,
            agent_repo=agent_repo,
            updated_by=getattr(request.state, "actor_label", None),
        )
        if not deactivated:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Tenant not found",
            )
        config = service.get_tenant_config(tenant_id)
    else:
        config = service.update_tenant_config(tenant_id, **payload_data)
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
    instance_service: Annotated[LucielInstanceService, Depends(get_luciel_instance_service)],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> DomainConfigRead:
    ScopePolicy.enforce_domain_scope(request, tenant_id, domain_id)
    service = AdminService(db)

    fields = payload.model_dump(exclude_unset=True)

    # Step 26 P7: deactivation must cascade to Agents and LucielInstances.
    # Route through deactivate_domain() instead of generic update when
    # the caller is flipping active=False. Other field updates still go
    # through the generic path (or both, if mixed).
    if fields.get("active") is False:
        ok = service.deactivate_domain(
            tenant_id,
            domain_id,
            audit_ctx=audit_ctx,
            luciel_instance_service=instance_service,
            updated_by=getattr(request.state, "actor_label", None),
        )
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Domain config not found",
            )
        # Drop active from the field set; apply any remaining updates via
        # the generic path (e.g. display_name change in same PATCH call).
        fields.pop("active", None)

    if fields:
        config = service.update_domain_config(tenant_id, domain_id, **fields)
        if not config:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Domain config not found",
            )
    else:
        config = service.get_domain_config(tenant_id, domain_id)
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
    service: Annotated[AdminService, Depends(get_admin_service)],
    luciel_service: Annotated[LucielInstanceService, Depends(get_luciel_instance_service)],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> None:
    ScopePolicy.enforce_domain_scope(request, tenant_id, domain_id)
    success = service.deactivate_domain(
        tenant_id,
        domain_id,
        audit_ctx=audit_ctx,
        luciel_instance_service=luciel_service,
        updated_by=getattr(request.state, "actor_label", None),
    )
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Domain config not found",
        )



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
    luciel_service: Annotated[LucielInstanceService, Depends(get_luciel_instance_service)],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> ApiKeyCreateResponse:
    # --- Step 24 scope + privilege guards (unchanged) ---------------
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
        if instance.scope_owner_tenant_id != payload.tenant_id:
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
        tenant_id=payload.tenant_id,
        domain_id=payload.domain_id,
        agent_id=payload.agent_id,
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
# additionally refuse minting embed keys with NULL tenant_id (those
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
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
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
    # --- Scope policy: caller mints at or below their own scope ----
    # Reuse the existing helper from create_api_key. Embed keys never
    # carry agent_id or luciel_instance_id at v1, so we pass None for
    # both -- the helper treats that as "no agent constraint", which
    # is correct: the embed key is tenant- or domain-scoped only.
    ScopePolicy.enforce_agent_scope(
        request,
        payload.tenant_id,
        payload.domain_id,
        target_agent_id=None,
    )

    # Domain-scoped callers cannot mint tenant-wide embed keys (i.e.
    # the request must specify a domain_id that matches the caller's
    # domain). Same rule as admin keys; restated here for clarity.
    caller_domain = getattr(request.state, "domain_id", None)
    if caller_domain and not ScopePolicy.is_platform_admin(request):
        if payload.domain_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Domain-scoped key may not mint tenant-wide embed keys",
            )
        if payload.domain_id != caller_domain:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Domain-scoped key may only mint embed keys within its own domain",
            )

    # Agent-scoped callers cannot mint embed keys at all (embed keys
    # are tenant- or domain-scoped at v1; per-agent embed keys would
    # require pinning to a luciel_instance, which is a Step 30c+
    # follow-up). Refusing here keeps the v1 contract narrow.
    caller_agent = getattr(request.state, "agent_id", None)
    if caller_agent and not ScopePolicy.is_platform_admin(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Agent-scoped keys cannot mint embed keys at v1. "
                "Use a tenant- or domain-scoped admin key."
            ),
        )

    # --- Scope-prompt preflight (Step 30d Deliverable A) ------------
    # For domain-scoped mints, verify the target domain_configs row
    # exists and has a non-empty system_prompt_additions BEFORE the
    # api_keys row is inserted. We block at issuance time rather than
    # at chat time so existing widget keys aren't retroactively bricked
    # and operators get the failure during a controlled admin action
    # rather than from a stranger's browser on a customer site
    # (ARCHITECTURE §3.2.2 'Issuance').
    #
    # Tenant-wide mints (domain_id is None) skip the preflight because
    # they are governed by TenantConfig.system_prompt at chat time; we
    # surface a non-fatal warning on the response instead.
    warnings: list[str] = []
    if payload.domain_id is None:
        warnings.append(
            "Tenant-wide embed key minted; scope is governed by "
            "TenantConfig.system_prompt at chat time, not by a "
            "per-domain scope prompt."
        )
    else:
        try:
            ScopePromptPreflight.check(
                db,
                tenant_id=payload.tenant_id,
                domain_id=payload.domain_id,
            )
        except ScopePromptMissingError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "scope_prompt_missing",
                    "reason": exc.reason,
                    "tenant_id": exc.tenant_id,
                    "domain_id": exc.domain_id,
                    "message": str(exc),
                },
            ) from exc

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
        tenant_id=payload.tenant_id,
        domain_id=payload.domain_id,
        agent_id=None,
        luciel_instance_id=None,
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
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
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
            caller_tenant = getattr(request.state, "tenant_id", None)
            caller_domain = getattr(request.state, "domain_id", None)
            caller_agent = getattr(request.state, "agent_id", None)
            if caller_tenant and target.tenant_id != caller_tenant:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Cannot deactivate API key outside your tenant",
                )
            if caller_domain and target.domain_id and target.domain_id != caller_domain:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Cannot deactivate API key outside your domain",
                )
            if caller_agent and target.agent_id and target.agent_id != caller_agent:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Cannot deactivate API key outside your agent",
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
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def list_memory_items(
    request: Request,
    db: DbSession,
    tenant_id: str = Query(
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
        tenant_id=tenant_id,
        active_only=active_only,
    )
    return [MemoryRead.model_validate(i) for i in items]


@router.delete("/memory-items/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
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

@router.post(
    "/luciel-instances",
    response_model=LucielInstanceRead,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def create_luciel_instance(
    request: Request,
    payload: LucielInstanceCreate,
    service: Annotated[LucielInstanceService, Depends(get_luciel_instance_service)],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> LucielInstanceRead:
    ScopePolicy.enforce_luciel_creation_scope(
        request,
        target_scope_level=payload.scope_level,
        target_tenant_id=payload.scope_owner_tenant_id,
        target_domain_id=payload.scope_owner_domain_id,
        target_agent_id=payload.scope_owner_agent_id,
    )
    try:
        instance = service.create_instance(
            audit_ctx=audit_ctx,
            instance_id=payload.instance_id,
            display_name=payload.display_name,
            scope_level=payload.scope_level,
            scope_owner_tenant_id=payload.scope_owner_tenant_id,
            scope_owner_domain_id=payload.scope_owner_domain_id,
            scope_owner_agent_id=payload.scope_owner_agent_id,
            description=payload.description,
            system_prompt_additions=payload.system_prompt_additions,
            preferred_provider=payload.preferred_provider,
            allowed_tools=payload.allowed_tools,
            created_by=payload.created_by,
        )
    except ParentScopeInactiveError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DuplicateInstanceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return LucielInstanceRead.model_validate(instance)


@router.get(
    "/luciel-instances",
    response_model=list[LucielInstanceRead],
)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def list_luciel_instances(
    request: Request,
    service: Annotated[LucielInstanceService, Depends(get_luciel_instance_service)],
    tenant_id: str | None = Query(default=None),
    domain_id: str | None = Query(default=None),
    agent_id: str | None = Query(default=None),
    include_inherited: bool = Query(default=False),
    active_only: bool = Query(default=False),
) -> list[LucielInstanceRead]:
    if not ScopePolicy.is_platform_admin(request):
        caller_tenant, caller_domain, caller_agent, _ = ScopePolicy._caller(request)
        if caller_tenant is None:
            raise HTTPException(status_code=403, detail="Admin key has no tenant scope.")
        tenant_id = caller_tenant
        if caller_domain is not None:
            if domain_id is None:
                domain_id = caller_domain
            elif domain_id != caller_domain:
                raise HTTPException(status_code=403, detail="This key is scoped to a different domain.")
        if caller_agent is not None:
            if agent_id is None:
                agent_id = caller_agent
                domain_id = caller_domain
            elif agent_id != caller_agent:
                raise HTTPException(status_code=403, detail="This key is scoped to a different agent.")
    else:
        if tenant_id is None:
            raise HTTPException(status_code=400, detail="platform_admin must specify tenant_id.")

    instances = service.list_for_scope(
        tenant_id=tenant_id,
        domain_id=domain_id,
        agent_id=agent_id,
        include_inherited=include_inherited,
        active_only=active_only,
    )
    return [LucielInstanceRead.model_validate(i) for i in instances]


@router.get(
    "/luciel-instances/{pk}",
    response_model=LucielInstanceRead,
)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def get_luciel_instance(
    request: Request,
    pk: int,
    service: Annotated[LucielInstanceService, Depends(get_luciel_instance_service)],
) -> LucielInstanceRead:
    instance = service.get_by_pk(pk)
    if instance is None:
        raise HTTPException(status_code=404, detail=f"LucielInstance pk={pk} not found.")
    ScopePolicy.enforce_luciel_instance_scope(request, instance)
    return LucielInstanceRead.model_validate(instance)


@router.patch(
    "/luciel-instances/{pk}",
    response_model=LucielInstanceRead,
)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def update_luciel_instance(
    request: Request,
    pk: int,
    payload: LucielInstanceUpdate,
    service: Annotated[LucielInstanceService, Depends(get_luciel_instance_service)],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> LucielInstanceRead:
    instance = service.get_by_pk(pk)
    if instance is None:
        raise HTTPException(status_code=404, detail=f"LucielInstance pk={pk} not found.")
    ScopePolicy.enforce_luciel_instance_scope(request, instance)

    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return LucielInstanceRead.model_validate(instance)

    updated = service.repo.update(instance, audit_ctx=audit_ctx, **updates)
    return LucielInstanceRead.model_validate(updated)


@router.delete(
    "/luciel-instances/{pk}",
    response_model=LucielInstanceRead,
)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def deactivate_luciel_instance(
    request: Request,
    pk: int,
    service: Annotated[LucielInstanceService, Depends(get_luciel_instance_service)],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
    db: DbSession,
) -> LucielInstanceRead:
    instance = service.get_by_pk(pk)
    if instance is None:
        raise HTTPException(status_code=404, detail=f"LucielInstance pk={pk} not found.")
    ScopePolicy.enforce_luciel_instance_scope(request, instance)

    # Memory cascade: soft-deactivate this instance's memory_items first.
    # Wired at route level because LucielInstanceService doesn't depend on
    # AdminService (would be a circular import).
    # autocommit=True is fine here -- service.deactivate_instance below
    # opens its own transaction for the instance row + audit row.
    #
    # Step 28 C10 (P3-Q): use scope_owner_tenant_id, not tenant_id. The
    # LucielInstance ORM model exposes the tenant column as
    # scope_owner_tenant_id (see app/models/luciel_instance.py line 99).
    # Pre-fix this line raised AttributeError before the cascade ever
    # ran, so every DELETE returned 500 in prod even though Pillar 10
    # zero-residue still passed thanks to the tenant-level cascade
    # firing later in the verify teardown PATCH.
    AdminService(db).bulk_soft_deactivate_memory_items_for_luciel_instance(
        tenant_id=instance.scope_owner_tenant_id,
        luciel_instance_id=pk,
        audit_ctx=audit_ctx,
        updated_by=getattr(request.state, "actor_label", None),
    )


    try:
        deactivated = service.deactivate_instance(
            audit_ctx=audit_ctx,
            pk=pk,
            updated_by=getattr(request.state, "actor_label", None),
        )
    except InstanceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return LucielInstanceRead.model_validate(deactivated)


# =====================================================================
# Step 24.5 — Agent (person/role) management routes
# These REPLACE Step 24's /admin/agents/* routes which wrote to
# agent_configs. The new routes write to the new `agents` table.
# =====================================================================

@router.post(
    "/agents",
    response_model=AgentRead,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def create_agent(
    request: Request,
    payload: AgentCreate,
    repo: Annotated[AgentRepository, Depends(get_agent_repository)],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> AgentRead:
    # Creating an Agent is a domain-level scope action (Agent lives
    # under a domain). Reuse Step 24's domain-scope check:
    # platform_admin bypass, tenant match, domain match if the caller
    # is domain-scoped.
    ScopePolicy.enforce_domain_scope(
        request,
        target_tenant_id=payload.tenant_id,
        target_domain_id=payload.domain_id,
    )
    try:
        agent = repo.create(
            tenant_id=payload.tenant_id,
            domain_id=payload.domain_id,
            agent_id=payload.agent_id,
            display_name=payload.display_name,
            description=payload.description,
            contact_email=payload.contact_email,
            created_by=payload.created_by,
            audit_ctx=audit_ctx,
        )
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Agent agent_id={payload.agent_id!r} already exists under "
                f"tenant={payload.tenant_id!r} / domain={payload.domain_id!r}."
            ),
        ) from exc
    return AgentRead.model_validate(agent)


@router.get(
    "/agents",
    response_model=list[AgentRead],
)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def list_agents(
    request: Request,
    repo: Annotated[AgentRepository, Depends(get_agent_repository)],
    tenant_id: str | None = Query(default=None),
    domain_id: str | None = Query(default=None),
    active_only: bool = Query(default=False),
) -> list[AgentRead]:
    if not ScopePolicy.is_platform_admin(request):
        caller_tenant, caller_domain, _, _ = ScopePolicy._caller(request)
        if caller_tenant is None:
            raise HTTPException(status_code=403, detail="Admin key has no tenant scope.")
        tenant_id = caller_tenant
        if caller_domain is not None:
            if domain_id is not None and domain_id != caller_domain:
                raise HTTPException(
                    status_code=403,
                    detail="This key is scoped to a different domain.",
                )
            domain_id = caller_domain
    else:
        if tenant_id is None:
            raise HTTPException(
                status_code=400, detail="platform_admin must specify tenant_id."
            )

    agents = repo.list_for_scope(
        tenant_id=tenant_id,
        domain_id=domain_id,
        active_only=active_only,
    )
    return [AgentRead.model_validate(a) for a in agents]


@router.get(
    "/agents/{tenant_id}/{agent_id}",
    response_model=AgentRead,
)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def get_agent(
    request: Request,
    tenant_id: str,
    agent_id: str,
    repo: Annotated[AgentRepository, Depends(get_agent_repository)],
) -> AgentRead:
    ScopePolicy.enforce_tenant_scope(request, tenant_id)
    agent = repo.get(tenant_id=tenant_id, agent_id=agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found.")
    # If the caller is domain-scoped, verify the agent's domain matches.
    ScopePolicy.enforce_domain_scope(
        request,
        target_tenant_id=agent.tenant_id,
        target_domain_id=agent.domain_id,
    )
    return AgentRead.model_validate(agent)


@router.patch(
    "/agents/{tenant_id}/{agent_id}",
    response_model=AgentRead,
)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def update_agent(
    request: Request,
    tenant_id: str,
    agent_id: str,
    payload: AgentUpdate,
    repo: Annotated[AgentRepository, Depends(get_agent_repository)],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> AgentRead:
    ScopePolicy.enforce_tenant_scope(request, tenant_id)
    agent = repo.get(tenant_id=tenant_id, agent_id=agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found.")
    ScopePolicy.enforce_domain_scope(
        request,
        target_tenant_id=agent.tenant_id,
        target_domain_id=agent.domain_id,
    )

    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        return AgentRead.model_validate(agent)

    updated = repo.update(agent, audit_ctx=audit_ctx, **updates)
    return AgentRead.model_validate(updated)


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
#      the route layer before delegating to repo.update.
#
# Step 24.5b doctrine: re-binding a user to a *different* Agent is
# deactivate-and-recreate, not in-place UPDATE — audit trail integrity.
# This route therefore refuses if the target Agent is inactive.
# ---------------------------------------------------------------------
@router.post(
    "/agents/{tenant_id}/{agent_id}/bind-user",
    response_model=AgentRead,
)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def bind_agent_to_user(
    request: Request,
    tenant_id: str,
    agent_id: str,
    payload: AgentBindUserPayload,
    repo: Annotated[AgentRepository, Depends(get_agent_repository)],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> AgentRead:
    permissions = getattr(request.state, "permissions", []) or []
    if "platform_admin" not in permissions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only platform_admin may bind an Agent to a User identity.",
        )

    agent = repo.get(tenant_id=tenant_id, agent_id=agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found.")
    if not agent.active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Agent is inactive; binding to inactive Agents is refused. "
                "Per Step 24.5b doctrine, re-binding a user to a different "
                "Agent is deactivate-and-recreate, not UPDATE in place."
            ),
        )

    # Invariant: one active Agent per (user, tenant). Refuse if the
    # target user already holds an active Agent in this tenant that
    # is not this one.
    existing = repo.get_by_user_and_tenant(
        user_id=payload.user_id,
        tenant_id=tenant_id,
        active_only=True,
    )
    if existing is not None and existing.id != agent.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"User {payload.user_id} already holds active Agent "
                f"{existing.agent_id} in tenant {tenant_id}. Per Step 24.5b "
                f"invariant, a User holds at most one active Agent per tenant."
            ),
        )

    updated = repo.update(
        agent,
        audit_ctx=audit_ctx,
        user_id=payload.user_id,
        updated_by=payload.updated_by
            or getattr(request.state, "actor_label", None),
    )
    return AgentRead.model_validate(updated)


@router.delete(
    "/agents/{tenant_id}/{agent_id}",
    response_model=AgentRead,
)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def deactivate_agent(
    request: Request,
    tenant_id: str,
    agent_id: str,
    repo: Annotated[AgentRepository, Depends(get_agent_repository)],
    service: Annotated[LucielInstanceService, Depends(get_luciel_instance_service)],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
    db: DbSession,
) -> AgentRead:
    """Soft-deactivate an Agent AND cascade-deactivate every agent-scoped
    LucielInstance owned by that agent. Both writes commit atomically
    with their audit rows."""
    ScopePolicy.enforce_tenant_scope(request, tenant_id)
    existing = repo.get(tenant_id=tenant_id, agent_id=agent_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Agent not found.")
    ScopePolicy.enforce_domain_scope(
        request,
        target_tenant_id=existing.tenant_id,
        target_domain_id=existing.domain_id,
    )

    # 1. Cascade to agent-scoped Luciels first.
    service.cascade_on_agent_deactivate(
        audit_ctx=audit_ctx,
        tenant_id=existing.tenant_id,
        domain_id=existing.domain_id,
        agent_id=existing.agent_id,
        updated_by=getattr(request.state, "actor_label", None),
    )

    # 1.5 Memory cascade: soft-deactivate agent-scoped memory_items.
    # Wired at route level because new-table Agent deactivation
    # orchestrates at this layer (cascade is not embedded in repo).
    AdminService(db).bulk_soft_deactivate_memory_items_for_agent(
        tenant_id=existing.tenant_id,
        agent_id=existing.agent_id,
        audit_ctx=audit_ctx,
        updated_by=getattr(request.state, "actor_label", None),
        autocommit=False,
    )


    # 2. Deactivate the agent row itself.
    deactivated = repo.deactivate(
        tenant_id=tenant_id,
        agent_id=agent_id,
        updated_by=getattr(request.state, "actor_label", None),
        audit_ctx=audit_ctx,
    )
    if deactivated is None:
        raise HTTPException(status_code=404, detail="Agent not found.")
    return AgentRead.model_validate(deactivated)
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
    "/luciel-instances/{instance_id}/chunking-config",
    response_model=kschemas.EffectiveChunkingConfigRead,
)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def get_effective_chunking_config(
    request: Request,
    instance_id: int,
    db: DbSession,
    instance_service: Annotated[LucielInstanceService, Depends(get_luciel_instance_service)],
    ingestion_service: Annotated[IngestionService, Depends(get_ingestion_service)],
) -> kschemas.EffectiveChunkingConfigRead:
    """Return the effective (instance -> domain -> tenant) chunking config
    for a Luciel instance. Diagnostic surface; doesn't ingest anything."""
    instance = _load_active_instance(
        request=request, instance_id=instance_id, instance_service=instance_service
    )
    cfg = ingestion_service._resolve_chunking_config(
        tenant_id=instance.scope_owner_tenant_id,
        domain_id=instance.scope_owner_domain_id,
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


@router.post(
    "/luciel-instances/{instance_id}/knowledge",
    response_model=kschemas.KnowledgeSourceRead,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit(KNOWLEDGE_UPLOAD_RATE_LIMIT, key_func=get_api_key_or_ip)
async def upload_knowledge_file(
    request: Request,
    instance_id: int,
    db: DbSession,
    instance_service: Annotated[LucielInstanceService, Depends(get_luciel_instance_service)],
    ingestion_service: Annotated[IngestionService, Depends(get_ingestion_service)],
    audit_repo: Annotated[AdminAuditRepository, Depends(get_admin_audit_repository)],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
    file: UploadFile = File(...),
    knowledge_type: str = Form(default="luciel_knowledge"),
    title: str | None = Form(default=None),
    source_id: str | None = Form(default=None),
    source_type: str | None = Form(default=None),
    replace_existing: bool = Form(default=False),
) -> kschemas.KnowledgeSourceRead:
    """Multipart file upload. Detects source_type from filename unless
    explicitly supplied via the source_type form field.
    """
    # Validate form fields via schema.
    meta = kschemas.KnowledgeUploadMeta(
        knowledge_type=knowledge_type,
        title=title,
        source_id=source_id,
        source_type=source_type,
    )

    # Step 26 P3: every knowledge source needs a stable identity for
    # versioning/replace/delete. Auto-generate when client omits it.
    if not meta.source_id:
        from uuid import uuid4
        meta = meta.model_copy(update={"source_id": f"src-{uuid4().hex[:12]}"})

    instance = _load_active_instance(
        request=request, instance_id=instance_id, instance_service=instance_service
    )
    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty",
        )
    try:
        result: IngestResult = ingestion_service.ingest_file(
            file_bytes=file_bytes,
            filename=file.filename or "upload.bin",
            tenant_id=instance.scope_owner_tenant_id,
            domain_id=instance.scope_owner_domain_id,
            luciel_instance_id=instance.id,
            knowledge_type=meta.knowledge_type,
            title=meta.title,
            source_id=meta.source_id,
            source_type=meta.source_type,
            ingested_by=getattr(request.state, "actor_label", None),
            created_by=getattr(request.state, "actor_label", None),
            replace_existing=replace_existing,
        )
    except IngestionError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    action = ACTION_KNOWLEDGE_REPLACE if replace_existing else ACTION_KNOWLEDGE_INGEST
    audit_repo.record(
        ctx=audit_ctx,
        tenant_id=instance.scope_owner_tenant_id,
        action=action,              # or the specific ACTION_KNOWLEDGE_* constant
        resource_type=RESOURCE_KNOWLEDGE,
        resource_pk=instance.id,
        domain_id=instance.scope_owner_domain_id,
        luciel_instance_id=instance.id,
        after={
            "source_id": result.source_id,
            "source_version": result.source_version,
            "source_filename": result.source_filename,
            "source_type": result.source_type,
            "chunk_count": result.chunk_count,
            "superseded_previous_version": result.superseded_previous_version,
        },
    )
    db.commit()

    return kschemas.KnowledgeSourceRead(
        luciel_instance_id=instance.id,
        source_id=result.source_id or meta.source_id,
        source_version=result.source_version,
        source_filename=result.source_filename,
        source_type=result.source_type,
        knowledge_type=result.knowledge_type,
        title=meta.title,
        chunk_count=result.chunk_count,
        ingested_by=getattr(request.state, "actor_label", None),
        created_at=None,
        superseded_at=None,
    )


@router.post(
    "/luciel-instances/{instance_id}/knowledge/text",
    response_model=kschemas.KnowledgeSourceRead,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit(KNOWLEDGE_UPLOAD_RATE_LIMIT, key_func=get_api_key_or_ip)
def ingest_knowledge_text(
    request: Request,
    instance_id: int,
    payload: kschemas.KnowledgeIngestRequest,
    db: DbSession,
    instance_service: Annotated[LucielInstanceService, Depends(get_luciel_instance_service)],
    ingestion_service: Annotated[IngestionService, Depends(get_ingestion_service)],
    audit_repo: Annotated[AdminAuditRepository, Depends(get_admin_audit_repository)],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
    replace_existing: bool = Query(default=False),
) -> kschemas.KnowledgeSourceRead:
    """JSON-body text ingest (already-extracted content)."""
    instance = _load_active_instance(
        request=request, instance_id=instance_id, instance_service=instance_service
    )

    # Step 26 P3: ensure every text-ingest also has a stable source_id.
    if not payload.source_id:
        from uuid import uuid4
        payload = payload.model_copy(update={"source_id": f"src-{uuid4().hex[:12]}"})

    try:
        result = ingestion_service.ingest_text(
            content=payload.content,
            tenant_id=instance.scope_owner_tenant_id,
            domain_id=instance.scope_owner_domain_id,
            luciel_instance_id=instance.id,
            knowledge_type=payload.knowledge_type,
            title=payload.title,
            source=payload.source,
            source_id=payload.source_id,
            source_filename=payload.source_filename,
            ingested_by=getattr(request.state, "actor_label", None),
            created_by=getattr(request.state, "actor_label", None),
            replace_existing=replace_existing,
        )
    except IngestionError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    action = ACTION_KNOWLEDGE_REPLACE if replace_existing else ACTION_KNOWLEDGE_INGEST
    audit_repo.record(
        ctx=audit_ctx,
        tenant_id=instance.scope_owner_tenant_id,
        action=action,              # or the specific ACTION_KNOWLEDGE_* constant
        resource_type=RESOURCE_KNOWLEDGE,
        resource_pk=instance.id,
        domain_id=instance.scope_owner_domain_id,
        luciel_instance_id=instance.id,
        after={
            "source_id": result.source_id,
            "source_version": result.source_version,
            "source_filename": result.source_filename,
            "source_type": result.source_type,
            "chunk_count": result.chunk_count,
            "superseded_previous_version": result.superseded_previous_version,
        },
    )
    db.commit()

    return kschemas.KnowledgeSourceRead(
        luciel_instance_id=instance.id,
        source_id=result.source_id or payload.source_id,
        source_version=result.source_version,
        source_filename=result.source_filename,
        source_type=result.source_type,
        knowledge_type=result.knowledge_type,
        title=payload.title,
        chunk_count=result.chunk_count,
        ingested_by=getattr(request.state, "actor_label", None),
        created_at=None,
        superseded_at=None,
    )


@router.get(
    "/luciel-instances/{instance_id}/knowledge",
    response_model=kschemas.KnowledgeListResponse,
)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def list_knowledge_sources(
    request: Request,
    instance_id: int,
    db: DbSession,
    instance_service: Annotated[LucielInstanceService, Depends(get_luciel_instance_service)],
    include_superseded: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> kschemas.KnowledgeListResponse:
    """List per-source summaries for one Luciel instance."""
    instance = _load_active_instance(
        request=request, instance_id=instance_id, instance_service=instance_service
    )
    repo = KnowledgeRepository(db)
    items, total = repo.list_sources_for_instance(
        luciel_instance_id=instance.id,
        include_superseded=include_superseded,
        limit=limit,
        offset=offset,
    )
    return kschemas.KnowledgeListResponse(
        items=[kschemas.KnowledgeSourceRead(**i) for i in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/luciel-instances/{instance_id}/knowledge/{source_id}",
)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def get_knowledge_source(
    request: Request,
    instance_id: int,
    source_id: str,
    db: DbSession,
    instance_service: Annotated[LucielInstanceService, Depends(get_luciel_instance_service)],
    expand: str | None = Query(default=None, description="'chunks' to include raw chunk rows"),
):
    """Get one source on a Luciel instance. By default returns a
    KnowledgeSourceRead summary; pass ?expand=chunks for the raw
    KnowledgeRead list.
    """
    instance = _load_active_instance(
        request=request, instance_id=instance_id, instance_service=instance_service
    )
    repo = KnowledgeRepository(db)
    rows = repo.get_active_source(
        luciel_instance_id=instance.id, source_id=source_id
    )
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"source_id {source_id!r} not found on instance {instance.id}",
        )

    if expand == "chunks":
        return [kschemas.KnowledgeRead.model_validate(r) for r in rows]

    # Default: single summary row, derived from the chunks.
    first = rows[0]
    return kschemas.KnowledgeSourceRead(
        luciel_instance_id=instance.id,
        source_id=source_id,
        source_version=first.source_version,
        source_filename=first.source_filename,
        source_type=first.source_type,
        knowledge_type=first.knowledge_type,
        title=first.title,
        chunk_count=len(rows),
        ingested_by=first.ingested_by,
        created_at=first.created_at,
        superseded_at=None,
    )


@router.delete(
    "/luciel-instances/{instance_id}/knowledge/{source_id}",
    response_model=kschemas.KnowledgeDeleteResponse,
)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def delete_knowledge_source(
    request: Request,
    instance_id: int,
    source_id: str,
    db: DbSession,
    instance_service: Annotated[LucielInstanceService, Depends(get_luciel_instance_service)],
    audit_repo: Annotated[AdminAuditRepository, Depends(get_admin_audit_repository)],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> kschemas.KnowledgeDeleteResponse:
    """Soft-delete all active chunks of a source on this Luciel instance
    by setting superseded_at. Idempotent: if already superseded, returns
    0 and still succeeds.
    """
    instance = _load_active_instance(
        request=request, instance_id=instance_id, instance_service=instance_service
    )
    repo = KnowledgeRepository(db)
    superseded = repo.supersede_source(
        luciel_instance_id=instance.id,
        source_id=source_id,
        autocommit=False,
    )

    audit_repo.record(
        ctx=audit_ctx,
        tenant_id=instance.scope_owner_tenant_id,
        action=ACTION_KNOWLEDGE_DELETE,              # or the specific ACTION_KNOWLEDGE_* constant
        resource_type=RESOURCE_KNOWLEDGE,
        resource_pk=instance.id,
        domain_id=instance.scope_owner_domain_id,
        luciel_instance_id=instance.id,
        after={
            "source_id": source_id,
            "superseded_rows": superseded,
        },
    )
    db.commit()

    return kschemas.KnowledgeDeleteResponse(
        luciel_instance_id=instance.id,
        source_id=source_id,
        superseded_rows=superseded,
    )


@router.put(
    "/luciel-instances/{instance_id}/knowledge/{source_id}",
    response_model=kschemas.KnowledgeSourceRead,
)
@limiter.limit(KNOWLEDGE_UPLOAD_RATE_LIMIT, key_func=get_api_key_or_ip)
def replace_knowledge_source_text(
    request: Request,
    instance_id: int,
    source_id: str,
    payload: kschemas.KnowledgeReplaceRequest,
    db: DbSession,
    instance_service: Annotated[LucielInstanceService, Depends(get_luciel_instance_service)],
    ingestion_service: Annotated[IngestionService, Depends(get_ingestion_service)],
    audit_repo: Annotated[AdminAuditRepository, Depends(get_admin_audit_repository)],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> kschemas.KnowledgeSourceRead:
    """Replace the current active version of source_id with new content.
    Always operates with replace_existing=True. Old rows are
    soft-superseded; new rows are written at previous_max + 1.
    """
    instance = _load_active_instance(
        request=request, instance_id=instance_id, instance_service=instance_service
    )
    try:
        result = ingestion_service.ingest_text(
            content=payload.content,
            tenant_id=instance.scope_owner_tenant_id,
            domain_id=instance.scope_owner_domain_id,
            luciel_instance_id=instance.id,
            knowledge_type="luciel_knowledge",
            title=payload.title,
            source=payload.source,
            source_id=source_id,
            source_filename=payload.source_filename,
            ingested_by=getattr(request.state, "actor_label", None),
            created_by=getattr(request.state, "actor_label", None),
            replace_existing=True,
        )
    except IngestionError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    audit_repo.record(
        ctx=audit_ctx,
        tenant_id=instance.scope_owner_tenant_id,
        action=ACTION_KNOWLEDGE_REPLACE,
        resource_type=RESOURCE_KNOWLEDGE,
        resource_pk=instance.id,
        domain_id=instance.scope_owner_domain_id,
        luciel_instance_id=instance.id,
        after={
            "source_id": result.source_id,
            "source_version": result.source_version,
            "source_filename": result.source_filename,
            "source_type": result.source_type,
            "chunk_count": result.chunk_count,
            "superseded_previous_version": result.superseded_previous_version,
        },
    )
    db.commit()

    return kschemas.KnowledgeSourceRead(
        luciel_instance_id=instance.id,
        source_id=source_id,
        source_version=result.source_version,
        source_filename=result.source_filename,
        source_type=result.source_type,
        knowledge_type=result.knowledge_type,
        title=payload.title,
        chunk_count=result.chunk_count,
        ingested_by=getattr(request.state, "actor_label", None),
        created_at=None,
        superseded_at=None,
    )

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
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
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


# ================================================================
# Step 28 Phase 2 Commit 12 -- ScopeAssignment / User-deactivate
# admin routes for the verify harness.
#
# Why these exist:
#   The verify ECS task runs under the least-privilege luciel_worker
#   role (per migration f392a842f885). That role intentionally has
#   ZERO access to the scope_assignments table -- not even SELECT --
#   because the worker has no production reason to read or write
#   identity-lifecycle rows. The harness historically called
#   ScopeAssignmentService and UserService directly with a worker DB
#   session; once Commit 11 fixed the bind-user UUID encoder, those
#   direct calls surfaced as InsufficientPrivilege errors in P12/P14.
#
#   These thin admin routes give the harness an HTTP path that runs
#   on the backend (which has full DB privileges) and is gated by
#   platform_admin permission, mirroring the bind-user route
#   pattern from Commit 9. No change to the production identity
#   cascade behaviour -- these routes wrap the same service methods
#   the production code paths already use.
#
# Drift register:
#   D-verify-task-pure-http-2026-05-05 -- the broader architectural
#   debt is "verify task should not hold a DB session at all". Logged
#   for Step 29; out of scope for Phase 2.
# ================================================================

import uuid as _uuid_p2c12

from app.schemas.scope_assignment import (
    EndAssignmentRequest as _EndAssignmentRequest_p2c12,
    ScopeAssignmentCreate as _ScopeAssignmentCreate_p2c12,
    ScopeAssignmentRead as _ScopeAssignmentRead_p2c12,
)
from app.services.scope_assignment_service import (
    AssignmentNotFoundError as _AssignmentNotFoundError_p2c12,
    AssignmentUserInactiveError as _AssignmentUserInactiveError_p2c12,
    AssignmentUserNotFoundError as _AssignmentUserNotFoundError_p2c12,
    ScopeAssignmentService as _ScopeAssignmentService_p2c12,
)
from app.services.user_service import (
    UserNotFoundError as _UserNotFoundError_p2c12,
    UserService as _UserService_p2c12,
)
from pydantic import BaseModel as _BaseModel_p2c12, Field as _Field_p2c12


class _ScopeAssignmentCreatePayload_p2c12(_BaseModel_p2c12):
    """Wrapper schema: ScopeAssignmentCreate carries (tenant, domain, role)
    but not user_id (which the existing schema takes from the URL path).
    For an admin-side create we want user_id in the body, so we wrap."""
    user_id: _uuid_p2c12.UUID = _Field_p2c12(
        ...,
        description="User to which the new ScopeAssignment will be bound.",
    )
    payload: _ScopeAssignmentCreate_p2c12 = _Field_p2c12(
        ...,
        description="Tenant/domain/role and optional started_at.",
    )
    audit_label: str | None = _Field_p2c12(
        default=None,
        max_length=200,
        description=(
            "Optional caller-provided audit context label. "
            "Falls back to request actor_label."
        ),
    )


class _ScopeAssignmentPromotePayload_p2c12(_BaseModel_p2c12):
    """Compound op: end old + create new in one txn (production path)."""
    old_assignment_id: _uuid_p2c12.UUID
    new_payload: _ScopeAssignmentCreate_p2c12
    end_reason: str = _Field_p2c12(
        default="PROMOTED",
        description=(
            "EndReason enum value: PROMOTED / DEMOTED / REASSIGNED / "
            "DEPARTED / DEACTIVATED."
        ),
    )
    end_note: str | None = _Field_p2c12(default=None, max_length=500)
    audit_label: str | None = _Field_p2c12(default=None, max_length=200)


class _UserDeactivatePayload_p2c12(_BaseModel_p2c12):
    reason: str = _Field_p2c12(..., min_length=10, max_length=500)
    audit_label: str | None = _Field_p2c12(default=None, max_length=200)


def _resolve_actor_p2c12(
    request: Request, audit_label: str | None
) -> AuditContext:
    """Build an AuditContext for harness-driven admin ops.

    Falls back to request.state.actor_label (set by the auth
    middleware), then to a generic system label. Never raises -- a
    missing actor label is non-fatal.
    """
    label = (
        audit_label
        or getattr(request.state, "actor_label", None)
        or "admin:scope-assignment"
    )
    return AuditContext.system(label=label)


@router.post(
    "/scope-assignments",
    response_model=_ScopeAssignmentRead_p2c12,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def create_scope_assignment_p2c12(
    request: Request,
    body: _ScopeAssignmentCreatePayload_p2c12,
    db: DbSession,
) -> _ScopeAssignmentRead_p2c12:
    """Create a ScopeAssignment for an existing User. platform_admin only.

    Phase 2 Commit 12. Thin wrapper over
    ScopeAssignmentService.create_assignment so the verify task can
    set up identity-lifecycle preconditions without holding
    INSERT privileges on scope_assignments at the DB layer.
    """
    if not ScopePolicy.is_platform_admin(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Only platform_admin may create ScopeAssignments via this "
                "administrative route."
            ),
        )

    actor = _resolve_actor_p2c12(request, body.audit_label)
    service = _ScopeAssignmentService_p2c12(db)
    try:
        sa = service.create_assignment(
            user_id=body.user_id,
            payload=body.payload,
            autocommit=True,
            audit_ctx=actor,
        )
    except _AssignmentUserNotFoundError_p2c12 as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except _AssignmentUserInactiveError_p2c12 as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _ScopeAssignmentRead_p2c12.model_validate(sa)


@router.get(
    "/scope-assignments/{assignment_id}",
    response_model=_ScopeAssignmentRead_p2c12,
)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def get_scope_assignment_p2c12(
    request: Request,
    assignment_id: _uuid_p2c12.UUID,
    db: DbSession,
) -> _ScopeAssignmentRead_p2c12:
    """Fetch a single ScopeAssignment by id. platform_admin only.

    Phase 2 Commit 12. Used by the verify harness for post-cascade
    assertions (e.g. P14 A3/A4 reads after end_assignment).
    """
    if not ScopePolicy.is_platform_admin(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only platform_admin may read ScopeAssignments here.",
        )
    sa = _ScopeAssignmentService_p2c12(db).get_assignment(assignment_id)
    if sa is None:
        raise HTTPException(
            status_code=404,
            detail=f"ScopeAssignment {assignment_id} not found.",
        )
    return _ScopeAssignmentRead_p2c12.model_validate(sa)


@router.post(
    "/scope-assignments/{assignment_id}/end",
    response_model=_ScopeAssignmentRead_p2c12,
)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def end_scope_assignment_p2c12(
    request: Request,
    assignment_id: _uuid_p2c12.UUID,
    body: _EndAssignmentRequest_p2c12,
    db: DbSession,
    audit_label: str | None = Query(default=None, max_length=200),
) -> _ScopeAssignmentRead_p2c12:
    """End a ScopeAssignment with mandatory Q6 key-rotation cascade.

    platform_admin only. Phase 2 Commit 12. Thin wrapper over
    ScopeAssignmentService.end_assignment -- exercises the same
    cascade path as production. Used by P14 to drive the DEPARTED
    semantics test.
    """
    if not ScopePolicy.is_platform_admin(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only platform_admin may end ScopeAssignments here.",
        )

    actor = _resolve_actor_p2c12(request, audit_label)
    service = _ScopeAssignmentService_p2c12(db)
    ended = service.end_assignment(
        assignment_id=assignment_id,
        reason=body.reason,
        note=body.note,
        autocommit=True,
        audit_ctx=actor,
    )
    if ended is None:
        raise HTTPException(
            status_code=404,
            detail=f"ScopeAssignment {assignment_id} not found.",
        )
    return _ScopeAssignmentRead_p2c12.model_validate(ended)


@router.post(
    "/scope-assignments/promote",
    response_model=dict,
)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def promote_scope_assignment_p2c12(
    request: Request,
    body: _ScopeAssignmentPromotePayload_p2c12,
    db: DbSession,
) -> dict:
    """Atomic role transition: end old + create new in one txn.

    platform_admin only. Phase 2 Commit 12. Thin wrapper over
    ScopeAssignmentService.promote -- preserves the single-txn
    invariant that production code relies on. Returns both rows so
    the harness can assert on both ended_at and new_role without
    a follow-up GET.
    """
    if not ScopePolicy.is_platform_admin(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only platform_admin may promote ScopeAssignments here.",
        )

    # Late import: EndReason is needed only here, and the admin module
    # already imports a lot at top level. Keep this scoped.
    from app.models.scope_assignment import EndReason as _EndReason_p2c12

    try:
        end_reason = _EndReason_p2c12(body.end_reason)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid end_reason {body.end_reason!r}. "
                f"Must be one of: "
                f"{[e.value for e in _EndReason_p2c12]}"
            ),
        ) from exc

    actor = _resolve_actor_p2c12(request, body.audit_label)
    service = _ScopeAssignmentService_p2c12(db)
    try:
        ended_old, created_new = service.promote(
            old_assignment_id=body.old_assignment_id,
            new_payload=body.new_payload,
            end_reason=end_reason,
            end_note=body.end_note,
            audit_ctx=actor,
        )
    except _AssignmentNotFoundError_p2c12 as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except _AssignmentUserInactiveError_p2c12 as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {
        "ended_old": _ScopeAssignmentRead_p2c12.model_validate(
            ended_old
        ).model_dump(mode="json"),
        "created_new": _ScopeAssignmentRead_p2c12.model_validate(
            created_new
        ).model_dump(mode="json"),
    }


@router.post(
    "/users/{user_id}/deactivate",
    status_code=status.HTTP_204_NO_CONTENT,
)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def deactivate_user_p2c12(
    request: Request,
    user_id: _uuid_p2c12.UUID,
    body: _UserDeactivatePayload_p2c12,
    db: DbSession,
) -> None:
    """Soft-deactivate a User and cascade (ends assignments + rotates keys).

    platform_admin only. Phase 2 Commit 12. Thin wrapper over
    UserService.deactivate_user. Used by P12/P13 teardown so the
    harness does not need DB write privileges on users or
    scope_assignments.
    """
    if not ScopePolicy.is_platform_admin(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only platform_admin may deactivate users here.",
        )

    actor = _resolve_actor_p2c12(request, body.audit_label)
    try:
        _UserService_p2c12(db).deactivate_user(
            user_id=user_id,
            reason=body.reason,
            audit_ctx=actor,
        )
    except _UserNotFoundError_p2c12 as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return None
