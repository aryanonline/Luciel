
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
from app.schemas.agent import AgentBindUserPayload, AgentCreate, AgentRead, AgentUpdate
from app.schemas.instance import (
    LucielInstanceCreate,
    LucielInstanceRead,
    LucielInstanceUpdate,
)
from app.services.instance_service import (
    DuplicateInstanceError,
    InstanceNotFoundError,
    InstanceService,
    TierScopeViolationError,
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
    get_tier_aware_key,
    get_tier_rate_limit_for_key,
)
from app.schemas.admin import (
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
    # Arc 5 Path A: default_domain is always None post-Domain-collapse;
    # leave the field null on the response. Pre-Arc-5 API clients that
    # read it tolerate the null (the schema marks the field Optional).
    domain = result["default_domain"]
    # api_key = result["api_key"]
    # raw_key = result["raw_api_key"]
    # policies = result["retention_policies"]

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
        default_domain=(
            OnboardedDomainSummary.model_validate(domain)
            if domain is not None
            else None
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



@router.post("/knowledge/ingest", response_model=KnowledgeIngestResponse)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def ingest_knowledge(
    request: Request,
    payload: KnowledgeIngestRequest,
    ingestion: Annotated[IngestionService, Depends(get_ingestion_service)],
) -> KnowledgeIngestResponse:
    # Scope enforcement: caller must match tenant, domain (if scoped),
    # and agent (if scoped) of the target knowledge record.
    ScopePolicy.enforce_agent_scope(
        request,
        payload.admin_id,
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
            admin_id=payload.admin_id,
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
        admin_id=payload.admin_id,
        domain_id=payload.domain_id,
        agent_id=payload.agent_id,
        source=payload.source,
    )


@router.post("/api-keys", response_model=ApiKeyCreateResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def create_api_key(
    request: Request,
    payload: ApiKeyCreate,
    db: DbSession,
    luciel_service: Annotated[InstanceService, Depends(get_luciel_instance_service)],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> ApiKeyCreateResponse:
    # --- Step 24 scope + privilege guards (unchanged) ---------------
    # Scope: a caller can only mint keys at or below their own scope.
    ScopePolicy.enforce_agent_scope(
        request,
        payload.admin_id,
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
    # --- Scope policy: caller mints at or below their own scope ----
    # Reuse the existing helper from create_api_key. Embed keys never
    # carry agent_id or luciel_instance_id at v1, so we pass None for
    # both -- the helper treats that as "no agent constraint", which
    # is correct: the embed key is tenant- or domain-scoped only.
    ScopePolicy.enforce_agent_scope(
        request,
        payload.admin_id,
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

    # Agent-scoped callers cannot mint embed keys (embed keys are
    # tenant-, domain-, or instance-scoped -- never agent-scoped). The
    # agent-scope carve-out remains as a guardrail: an agent-bound key
    # has no need to mint widget credentials.
    caller_agent = getattr(request.state, "agent_id", None)
    if caller_agent and not ScopePolicy.is_platform_admin(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Agent-scoped keys cannot mint embed keys. Use a "
                "tenant- or domain-scoped admin key (or the customer's "
                "cookied dashboard session)."
            ),
        )

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
        # Arc 5 Path A — V2 collapsed: the legacy domain-scoped embed-key
        # cross-domain guard is gone (V2 has no Domain layer). The
        # branch below is intentionally dead — the if-condition can
        # never fire because the second predicate is ``None is not None``.
        # Kept as a placeholder so the test harness diff stays small;
        # the whole block is deleted in a follow-on cleanup.
        if (
            payload.domain_id is not None
            and None is not None  # noqa: F632 — Path A intentional dead branch
            and None != payload.domain_id  # noqa: F632
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Luciel instance belongs to a different domain."
                ),
            )
        if not instance.active:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "Luciel instance is inactive (soft-deleted). Mint "
                    "the embed key against an active instance, or "
                    "reactivate this instance first."
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
                admin_id=payload.admin_id,
                domain_id=payload.domain_id,
            )
        except ScopePromptMissingError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "scope_prompt_missing",
                    "reason": exc.reason,
                    "admin_id": exc.admin_id,
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
        admin_id=payload.admin_id,
        domain_id=payload.domain_id,
        agent_id=None,
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
            caller_domain = getattr(request.state, "domain_id", None)
            caller_agent = getattr(request.state, "agent_id", None)
            if caller_tenant and target.admin_id != caller_tenant:
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

@router.post(
    "/instances",
    response_model=LucielInstanceRead,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def create_luciel_instance(
    request: Request,
    payload: LucielInstanceCreate,
    service: Annotated[InstanceService, Depends(get_luciel_instance_service)],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> LucielInstanceRead:
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
    try:
        instance = service.create_instance(
            audit_ctx=audit_ctx,
            admin_id=payload.admin_id,
            instance_slug=payload.instance_slug,
            display_name=payload.display_name,
            description=payload.description,
            active=payload.active,
            created_by=payload.created_by,
            system_prompt_additions=payload.system_prompt_additions,
        )
    except DuplicateInstanceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return LucielInstanceRead.model_validate(instance)


# =====================================================================
# Step 30a.5 -- /admin/luciel-instances teammate_email overload removed
# =====================================================================
#
# The Step 30a.1 _invite_teammate helper that previously sat here was
# deleted in Step 30a.5 along with the schema field and the deprecated
# email-send block above. The first-class invite path is POST
# /admin/invites (Step 30a.4) -- see docs/designs/step-30a-5-company-
# self-serve.md §8 for the removal rationale and migration notes.
# invite_service.create_invite() now carries the User + Agent +
# ScopeAssignment provisioning logic that _invite_teammate used to
# duplicate (see app/services/invite_service.py line 308+).


# =====================================================================
# Step 30a.4 -- /admin/invites: first-class invite lifecycle
# =====================================================================
#
# These routes are COOKIE-gated (not API-key-gated like the rest of
# admin.py) because the call site is the /app/team UI, which is a
# cookied React surface. The actor is the cookied User; the inviting
# tenant + domain default to the cookied User's active ScopeAssignment.
#
# All four routes delegate to app.services.invite_service.* -- the
# audit row and DB transaction discipline live there, not here.

import uuid as _step_30a_4_uuid  # local alias to avoid colliding with line-2575 import

from app.schemas.invite import (
    UserInviteCreate,
    UserInviteRead,
    UserInviteResendResponse,
    UserInviteRevokeResponse,
)
from app.schemas.team_member import TeamMemberRead
from app.services import invite_service
from app.services.invite_service import (
    DuplicatePendingInviteError,
    InviteError,
    InviteExpiredError,
    InviteNotFoundError,
    InviteNotPendingError,
    InvitePendingCapExceededError,
    InviteRoleNotAllowedForTierError,
)


def _resolve_invite_actor(
    *,
    request: Request,
    db,
) -> tuple["User", str, str]:
    """Resolve (cookied_user, admin_id, default_domain_id) for invite routes.

    Reads the session cookie off the Request directly (same pattern as
    billing routes). Returns the cookied User, their active admin_id
    (from the session JWT), and the domain_id of their currently-active
    ScopeAssignment within that tenant.

    Raises HTTPException:
      * 401 -- no valid session cookie, or User row inactive.
      * 403 -- cookied user has no active ScopeAssignment under any
               tenant (cannot invite without a home scope).
    """
    from app.core.config import settings
    from app.models.user import User
    from app.repositories.scope_assignment_repository import (
        ScopeAssignmentRepository,
    )
    from app.services.magic_link_service import (
        MagicLinkError,
        validate_session_token,
    )

    cookie = request.cookies.get(settings.session_cookie_name)
    if not cookie:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    try:
        payload = validate_session_token(cookie)
    except MagicLinkError as exc:
        raise HTTPException(
            status_code=401, detail=str(exc) or "Invalid session."
        ) from exc

    user_id = payload.get("sub")
    session_tenant_id = payload.get("admin_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Malformed session.")

    user = db.get(User, user_id)
    if user is None or not user.active:
        raise HTTPException(
            status_code=401, detail="User not found or inactive."
        )

    # Find the cookied user's active ScopeAssignment to source
    # (admin_id, domain_id) when the caller omits them on the payload.
    sar = ScopeAssignmentRepository(db)
    active_assignments = sar.list_for_user(user.id, active_only=True)
    if not active_assignments:
        raise HTTPException(
            status_code=403,
            detail="Cookied user has no active scope assignment.",
        )

    # Prefer the assignment matching the session JWT's admin_id; fall
    # back to the first active assignment (single-tenant common case).
    chosen = next(
        (a for a in active_assignments if a.admin_id == session_tenant_id),
        active_assignments[0],
    )
    return user, chosen.admin_id, chosen.domain_id


def _map_invite_error(exc: InviteError) -> HTTPException:
    """Translate InviteService errors into HTTP responses."""
    if isinstance(exc, InviteNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, InviteNotPendingError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, InviteExpiredError):
        return HTTPException(status_code=410, detail=str(exc))
    if isinstance(exc, DuplicatePendingInviteError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, InvitePendingCapExceededError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, InviteRoleNotAllowedForTierError):
        # Step 30a.6 -- Team-tier callers cannot mint department_lead
        # invites (Team is flat, no Domain layer). 422 because it is a
        # request-shape error, not an auth event.
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@router.post(
    "/invites",
    response_model=UserInviteRead,
    status_code=status.HTTP_201_CREATED,
)
def create_invite_route(  # noqa: D401
    payload: UserInviteCreate,
    request: Request,
    db: DbSession,
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> UserInviteRead:
    """Mint a UserInvite + welcome-set-password email (Step 30a.4).

    Cookied route. The cookied User is the inviter; tenant + domain
    default to the cookied user's active scope when omitted.
    """
    inviter, default_tenant_id, default_domain_id = _resolve_invite_actor(
        request=request, db=db
    )
    admin_id = payload.admin_id or default_tenant_id
    domain_id = payload.domain_id or default_domain_id

    # Cross-tenant safety: a cookied user can only invite into their own
    # tenant unless they hold platform_admin (which a cookied session
    # does not carry today). Tenant mismatch is 403 rather than 422 so
    # the audit chain reads as an authz event.
    if admin_id != default_tenant_id:
        raise HTTPException(
            status_code=403,
            detail="Cookied user cannot invite into a tenant they do not belong to.",
        )

    try:
        invite, _token = invite_service.create_invite(
            db=db,
            admin_id=admin_id,
            domain_id=domain_id,
            inviter_user_id=inviter.id,
            inviter_email=inviter.email,
            invited_email=str(payload.invited_email),
            role=payload.role,
            audit_ctx=audit_ctx,
        )
    except InviteError as exc:
        raise _map_invite_error(exc) from exc
    except IntegrityError as exc:
        # Race: another request landed the partial-unique-index INSERT
        # between our pre-flight and the actual INSERT. Surface as 409.
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="A pending invite for this email already exists under this tenant.",
        ) from exc

    return UserInviteRead.model_validate(invite)


@router.get(
    "/invites",
    response_model=list[UserInviteRead],
)
def list_invites_route(
    request: Request,
    db: DbSession,
    status_filter: str | None = Query(
        default=None,
        alias="status",
        description=(
            "Filter by status. Pass 'pending', 'accepted', 'expired', "
            "'revoked', or omit for all statuses."
        ),
    ),
) -> list[UserInviteRead]:
    """List invites under the cookied user's tenant (Step 30a.4).

    Default order: descending created_at (newest first).
    """
    from app.models.user_invite import InviteStatus
    from app.repositories.user_invites import UserInviteRepository

    _user, admin_id, _domain_id = _resolve_invite_actor(
        request=request, db=db
    )

    statuses: tuple[InviteStatus, ...] | None = None
    if status_filter is not None:
        try:
            statuses = (InviteStatus(status_filter.strip().lower()),)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown invite status: {status_filter!r}",
            ) from exc

    repo = UserInviteRepository(db)
    invites = repo.list_for_tenant(admin_id=admin_id, statuses=statuses)
    return [UserInviteRead.model_validate(i) for i in invites]


@router.get(
    "/team-members",
    response_model=list[TeamMemberRead],
)
def list_team_members_route(
    request: Request,
    db: DbSession,
    active_only: bool = Query(default=True),
) -> list[TeamMemberRead]:
    """List active team members under the cookied user's Admin.

    Anchored to Vision v1 §6.2 (Team Member lifecycle) and
    Architecture v1 §3.7.2 (role scope assignment is the single
    source of truth for team-member binding).

    The Dashboard Team tab consumes this to show the founder who
    currently has scope on their Admin. Auth follows the same
    cookied-actor pattern as ``GET /admin/invites``: the cookied
    User's active ScopeAssignment is resolved to ``(admin_id,
    domain_id)`` and the listing is scoped to that admin_id.

    Replaces the legacy ``GET /admin/agents`` frontend endpoint,
    which referenced the deleted ``agents`` table and would have
    crashed in production on any real call.
    """
    from app.models.user import User
    from app.repositories.scope_assignment_repository import (
        ScopeAssignmentRepository,
    )

    _user, admin_id, _domain_id = _resolve_invite_actor(
        request=request, db=db
    )

    sar = ScopeAssignmentRepository(db)
    assignments = sar.list_for_tenant(
        admin_id=admin_id, active_only=active_only,
    )
    # Bulk-fetch the User rows for these assignments.
    user_ids = list({a.user_id for a in assignments})
    users_by_id = {
        u.id: u
        for u in db.query(User).filter(User.id.in_(user_ids)).all()
    } if user_ids else {}

    out: list[TeamMemberRead] = []
    for a in assignments:
        user = users_by_id.get(a.user_id)
        if user is None:
            # Defensive: the FK guarantees presence, but if the
            # synthetic-orphan cleanup removed the row, skip rather
            # than 500.
            logger.warning(
                "list_team_members: assignment %s references missing "
                "user %s; skipping.", a.id, a.user_id,
            )
            continue
        out.append(TeamMemberRead(
            scope_assignment_id=a.id,
            role=a.role,
            domain_id=a.domain_id,
            started_at=a.started_at,
            active=a.active,
            user_id=user.id,
            email=user.email,
            display_name=user.display_name,
            user_active=user.active,
        ))
    return out


@router.post(
    "/invites/{invite_id}/resend",
    response_model=UserInviteResendResponse,
    status_code=status.HTTP_200_OK,
)
def resend_invite_route(
    invite_id: _step_30a_4_uuid.UUID,
    request: Request,
    db: DbSession,
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> UserInviteResendResponse:
    """Rotate token_jti and re-mint the welcome email (Step 30a.4)."""
    inviter, admin_id, _domain_id = _resolve_invite_actor(
        request=request, db=db
    )

    try:
        invite, _new_token = invite_service.resend_invite(
            db=db,
            invite_id=invite_id,
            inviter_user_id=inviter.id,
            audit_ctx=audit_ctx,
        )
    except InviteError as exc:
        raise _map_invite_error(exc) from exc

    # Cross-tenant safety: resending an invite under a tenant the
    # cookied user does not belong to is a 403. Done AFTER the lookup
    # so a 404 wins over a 403 for non-existent invite ids (no info
    # leakage about which ids exist).
    if invite.admin_id != admin_id:
        raise HTTPException(
            status_code=403,
            detail="Cookied user cannot resend invites under a foreign tenant.",
        )

    return UserInviteResendResponse(invite=UserInviteRead.model_validate(invite))


@router.delete(
    "/invites/{invite_id}",
    response_model=UserInviteRevokeResponse,
    status_code=status.HTTP_200_OK,
)
def revoke_invite_route(
    invite_id: _step_30a_4_uuid.UUID,
    request: Request,
    db: DbSession,
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
) -> UserInviteRevokeResponse:
    """Flip a still-pending invite to REVOKED (Step 30a.4)."""
    from app.repositories.user_invites import UserInviteRepository

    _inviter, admin_id, _domain_id = _resolve_invite_actor(
        request=request, db=db
    )

    # Cross-tenant safety: same as resend.
    pre = UserInviteRepository(db).get_by_pk(invite_id)
    if pre is None:
        raise HTTPException(status_code=404, detail="Invite not found.")
    if pre.admin_id != admin_id:
        raise HTTPException(
            status_code=403,
            detail="Cookied user cannot revoke invites under a foreign tenant.",
        )

    try:
        invite_service.revoke_invite(
            db=db,
            invite_id=invite_id,
            audit_ctx=audit_ctx,
        )
    except InviteError as exc:
        raise _map_invite_error(exc) from exc

    return UserInviteRevokeResponse(invite_id=invite_id)


@router.get(
    "/instances",
    response_model=list[LucielInstanceRead],
)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def list_luciel_instances(
    request: Request,
    service: Annotated[InstanceService, Depends(get_luciel_instance_service)],
    admin_id: str | None = Query(default=None),
    active_only: bool = Query(default=False),
) -> list[LucielInstanceRead]:
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
    return [LucielInstanceRead.model_validate(i) for i in instances]


@router.get(
    "/instances/{pk}",
    response_model=LucielInstanceRead,
)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def get_luciel_instance(
    request: Request,
    pk: int,
    service: Annotated[InstanceService, Depends(get_luciel_instance_service)],
) -> LucielInstanceRead:
    instance = service.get_by_pk(pk)
    if instance is None:
        raise HTTPException(status_code=404, detail=f"LucielInstance pk={pk} not found.")
    ScopePolicy.enforce_luciel_instance_scope(request, instance)
    return LucielInstanceRead.model_validate(instance)


@router.patch(
    "/instances/{pk}",
    response_model=LucielInstanceRead,
)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def update_luciel_instance(
    request: Request,
    pk: int,
    payload: LucielInstanceUpdate,
    service: Annotated[InstanceService, Depends(get_luciel_instance_service)],
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
    "/instances/{pk}",
    response_model=LucielInstanceRead,
)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def deactivate_luciel_instance(
    request: Request,
    pk: int,
    service: Annotated[InstanceService, Depends(get_luciel_instance_service)],
    audit_ctx: Annotated[AuditContext, Depends(get_audit_context)],
    db: DbSession,
) -> LucielInstanceRead:
    instance = service.get_by_pk(pk)
    if instance is None:
        raise HTTPException(status_code=404, detail=f"LucielInstance pk={pk} not found.")
    ScopePolicy.enforce_luciel_instance_scope(request, instance)

    # Memory cascade: soft-deactivate this instance's memory_items first.
    # Wired at route level because InstanceService doesn't depend on
    # AdminService (would be a circular import).
    # autocommit=True is fine here -- service.deactivate_instance below
    # opens its own transaction for the instance row + audit row.
    #
    # Arc 5 Path A — V2 collapsed: read instance.admin_id directly. The
    # LucielInstance ORM model exposes the tenant column as
    # admin_id (V2 Instance shape post-Arc-5 Path A).
    # Pre-fix this line raised AttributeError before the cascade ever
    # ran, so every DELETE returned 500 in prod even though Pillar 10
    # zero-residue still passed thanks to the tenant-level cascade
    # firing later in the verify teardown PATCH.
    AdminService(db).bulk_soft_deactivate_memory_items_for_luciel_instance(
        admin_id=instance.admin_id,
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
        domain_id=None,
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
    "/instances/{instance_id}/knowledge",
    response_model=kschemas.KnowledgeSourceRead,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
async def upload_knowledge_file(
    request: Request,
    instance_id: int,
    db: DbSession,
    instance_service: Annotated[InstanceService, Depends(get_luciel_instance_service)],
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
            admin_id=instance.admin_id,
            domain_id=None,
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
        admin_id=instance.admin_id,
        action=action,              # or the specific ACTION_KNOWLEDGE_* constant
        resource_type=RESOURCE_KNOWLEDGE,
        resource_pk=instance.id,
        domain_id=None,
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
    "/instances/{instance_id}/knowledge/text",
    response_model=kschemas.KnowledgeSourceRead,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def ingest_knowledge_text(
    request: Request,
    instance_id: int,
    payload: kschemas.KnowledgeIngestRequest,
    db: DbSession,
    instance_service: Annotated[InstanceService, Depends(get_luciel_instance_service)],
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
            admin_id=instance.admin_id,
            domain_id=None,
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
        admin_id=instance.admin_id,
        action=action,              # or the specific ACTION_KNOWLEDGE_* constant
        resource_type=RESOURCE_KNOWLEDGE,
        resource_pk=instance.id,
        domain_id=None,
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
    "/instances/{instance_id}/knowledge",
    response_model=kschemas.KnowledgeListResponse,
)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def list_knowledge_sources(
    request: Request,
    instance_id: int,
    db: DbSession,
    instance_service: Annotated[InstanceService, Depends(get_luciel_instance_service)],
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
    "/instances/{instance_id}/knowledge/{source_id}",
)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def get_knowledge_source(
    request: Request,
    instance_id: int,
    source_id: str,
    db: DbSession,
    instance_service: Annotated[InstanceService, Depends(get_luciel_instance_service)],
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
    "/instances/{instance_id}/knowledge/{source_id}",
    response_model=kschemas.KnowledgeDeleteResponse,
)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def delete_knowledge_source(
    request: Request,
    instance_id: int,
    source_id: str,
    db: DbSession,
    instance_service: Annotated[InstanceService, Depends(get_luciel_instance_service)],
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
        admin_id=instance.admin_id,
        action=ACTION_KNOWLEDGE_DELETE,              # or the specific ACTION_KNOWLEDGE_* constant
        resource_type=RESOURCE_KNOWLEDGE,
        resource_pk=instance.id,
        domain_id=None,
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
    "/instances/{instance_id}/knowledge/{source_id}",
    response_model=kschemas.KnowledgeSourceRead,
)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def replace_knowledge_source_text(
    request: Request,
    instance_id: int,
    source_id: str,
    payload: kschemas.KnowledgeReplaceRequest,
    db: DbSession,
    instance_service: Annotated[InstanceService, Depends(get_luciel_instance_service)],
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
            admin_id=instance.admin_id,
            domain_id=None,
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
        admin_id=instance.admin_id,
        action=ACTION_KNOWLEDGE_REPLACE,
        resource_type=RESOURCE_KNOWLEDGE,
        resource_pk=instance.id,
        domain_id=None,
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
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
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
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
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
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
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
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
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
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
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
from app.services.closure_service import (
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
        )
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

    from app.services.closure_service import ClosureService
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
