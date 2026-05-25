"""
FastAPI dependency injection wiring.
"""

# Add import at top
from app.repositories.consent_repository import ConsentRepository
from app.policy.consent import ConsentPolicy
from collections.abc import Generator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.session import SessionLocal, get_db
from app.db.tenant_context import (
    clear_current_admin_id,
    reset_current_admin_id,
    set_current_admin_id,
)
from app.db.instance_context import (
    clear_current_instance_id,
    reset_current_instance_id,
    set_current_instance_id,
)
from app.integrations.llm.router import ModelRouter
from app.knowledge.ingestion import IngestionService
from app.knowledge.retriever import KnowledgeRetriever
from app.memory.service import MemoryService
from app.repositories.config_repository import ConfigRepository
from app.repositories.knowledge_repository import KnowledgeRepository
from app.repositories.memory_repository import MemoryRepository
from app.repositories.session_repository import SessionRepository
from app.repositories.trace_repository import TraceRepository
from app.services.chat_service import ChatService
from app.services.session_service import SessionService
from app.services.trace_service import TraceService
from app.tools.broker import ToolBroker
from app.tools.registry import ToolRegistry
from fastapi import Request  # noqa: E402

from app.repositories.admin_audit_repository import (  # noqa: E402
    AdminAuditRepository,
    AuditContext,
)
# Arc 5 Path A — agent_repository.py was deleted at Commit A5; there
# is no V2 equivalent (V2 hierarchy is Admin → Instance, no Agent
# layer). The transitional ``get_agent_repository`` factory below is a
# stub that raises at call time so the FastAPI dependency graph still
# resolves at import; the routes that depend on it are deleted /
# rewritten at B1+B2.
from app.repositories.instance_repository import (  # noqa: E402
    InstanceRepository,
)
from app.services.admin_service import AdminService  # noqa: E402
from app.services.instance_service import InstanceService  # noqa: E402

DbSession = Annotated[Session, Depends(get_db)]


# Arc 9 C2 — In-app RLS connection-pool wrapper (Layer 3 of Wall 1).
# Arc 9 C4.2 — Extended to also bind Wall 3 (instance_id) ContextVar.
#
# ``get_tenant_scoped_db`` is the parallel dependency to ``get_db``
# that, on entry, reads the authenticated admin's slug off
# ``request.state.tenant_id`` AND the authenticated instance id off
# ``request.state.luciel_instance_id`` (both populated by
# ApiKeyAuthMiddleware / SessionCookieAuthMiddleware) and pushes them
# into the in-process ContextVars. The engine-level ``after_begin``
# listener in ``app.db.session`` (updated at C4.1) then issues TWO
# set_config() calls on each BEGIN:
#
#   SELECT set_config('app.admin_id',    '<slug>',    true);
#   SELECT set_config('app.instance_id', '<int|empty>', true);
#
# The Arc 9 C3 and C4.3 RLS policies then enforce ``tenant_id`` and
# ``luciel_instance_id`` respectively against the GUCs.
#
# C2 lands this as a PARALLEL dep, not a replacement. Existing routes
# keep using ``DbSession`` -> ``get_db``. C3 + C4.3 opt each route
# group in by switching its annotation to ``TenantScopedDbSession``.
# Staged migration limits blast radius and lets us roll back any
# single route group without affecting the rest.
def get_tenant_scoped_db(request: Request) -> Generator[Session, None, None]:
    """Yield a DB session bound to the requesting admin + instance scope.

    Reads two pieces of state off the FastAPI request (both populated
    by the auth middleware):

      * ``request.state.tenant_id`` -- the admin slug under Arc 5
        Revision C aliasing. Bound to ``app.db.tenant_context`` and
        emitted as ``SET LOCAL app.admin_id`` by the listener.

      * ``request.state.luciel_instance_id`` -- the bound Instance
        primary key (Integer) or None for admin-level keys.
        Bound to ``app.db.instance_context`` and emitted as
        ``SET LOCAL app.instance_id`` by the listener.

    When either is missing or None (unauth path, health checks,
    admin-level API key without an instance binding), we still set
    the matching ContextVar to None and the listener emits an empty-
    string SET. Wall 1 strict policies treat empty as deny;
    Wall 1 NULL-permissive + Wall 3 (all NULL-permissive) policies
    treat empty as 'cross-tenant read OK if the row is NULL'.

    The ``finally`` reset via the saved token is critical for BOTH
    ContextVars: ContextVar is per-async-task but the FastAPI worker
    coroutine outlives any single request. Without the reset, an
    authenticated request followed by an unauthenticated one on the
    same coroutine could leave the previous admin_id and/or
    instance_id lingering -- a leak window we don't want open.

    The two ContextVars are reset INDEPENDENTLY -- a failure to
    reset one MUST NOT prevent the other from being reset (the
    clear_*() fallback path).
    """
    tenant_id = getattr(request.state, "tenant_id", None)
    instance_id = getattr(request.state, "luciel_instance_id", None)
    admin_token = set_current_admin_id(tenant_id)
    instance_token = set_current_instance_id(instance_id)
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
        # Reset both ContextVars independently. If either reset
        # raises (token-out-of-context, etc.) we fall back to
        # clear() so the other still runs.
        try:
            reset_current_admin_id(admin_token)
        except Exception:
            clear_current_admin_id()
        try:
            reset_current_instance_id(instance_token)
        except Exception:
            clear_current_instance_id()


TenantScopedDbSession = Annotated[Session, Depends(get_tenant_scoped_db)]

_model_router = ModelRouter()
_tool_registry = ToolRegistry()
_tool_broker = ToolBroker(registry=_tool_registry)


def get_session_repository(db: DbSession) -> SessionRepository:
    return SessionRepository(db)


def get_memory_repository(db: DbSession) -> MemoryRepository:
    return MemoryRepository(db)


def get_trace_repository(db: DbSession) -> TraceRepository:
    return TraceRepository(db)


def get_knowledge_repository(db: DbSession) -> KnowledgeRepository:
    return KnowledgeRepository(db)


def get_config_repository(db: DbSession) -> ConfigRepository:
    return ConfigRepository(db)


def get_session_service(
    repository: Annotated[SessionRepository, Depends(get_session_repository)],
) -> SessionService:
    return SessionService(repository)


def get_memory_service(
    repository: Annotated[MemoryRepository, Depends(get_memory_repository)],
) -> MemoryService:
    return MemoryService(repository=repository, model_router=_model_router)


def get_trace_service(
    repository: Annotated[TraceRepository, Depends(get_trace_repository)],
) -> TraceService:
    return TraceService(repository=repository)


def get_knowledge_retriever(
    repository: Annotated[KnowledgeRepository, Depends(get_knowledge_repository)],
) -> KnowledgeRetriever:
    return KnowledgeRetriever(repository=repository)


def get_ingestion_service(db: DbSession) -> IngestionService:
    return IngestionService(db=db)

# Add these two functions
def get_consent_repository(db: DbSession) -> ConsentRepository:
    return ConsentRepository(db)

def get_consent_policy(
    repo: Annotated[ConsentRepository, Depends(get_consent_repository)],
) -> ConsentPolicy:
    return ConsentPolicy(consent_repository=repo)

def get_instance_repository(db: DbSession) -> InstanceRepository:
    return InstanceRepository(db)


# Arc 5 Path A — transitional alias for the old factory name. Removed
# at B2 when chat_service.py + admin_service.py + ad-hoc importers
# finish renaming to ``get_instance_repository``.
get_luciel_instance_repository = get_instance_repository

def get_chat_service(
    session_service: Annotated[SessionService, Depends(get_session_service)],
    memory_service: Annotated[MemoryService, Depends(get_memory_service)],
    trace_service: Annotated[TraceService, Depends(get_trace_service)],
    knowledge_retriever: Annotated[KnowledgeRetriever, Depends(get_knowledge_retriever)],
    config_repository: Annotated[ConfigRepository, Depends(get_config_repository)],
    instance_repository: Annotated[
        InstanceRepository, Depends(get_instance_repository)
    ],
    consent_policy: Annotated[ConsentPolicy, Depends(get_consent_policy)],
) -> ChatService:
    return ChatService(
        session_service=session_service,
        memory_service=memory_service,
        model_router=_model_router,
        tool_registry=_tool_registry,
        tool_broker=_tool_broker,
        trace_service=trace_service,
        knowledge_retriever=knowledge_retriever,
        config_repository=config_repository,
        instance_repository=instance_repository,
        consent_policy=consent_policy,
    )

def get_agent_repository(db: DbSession):
    """Arc 5 Path A stub — the V1 AgentRepository is deleted; this
    factory raises if any surviving route still depends on it. The
    routes that consumed the V1 Agent layer (``/admin/agents/*``) were
    deleted at B3 (Commit 12); any remaining caller is a sweep target
    for B1's route-body rewrite.
    """
    raise RuntimeError(
        "AgentRepository was deleted at Arc 5 Commit A5 (Path A). "
        "V2 has no Agent layer; rewrite the caller against Admin "
        "and Instance directly."
    )


def get_admin_audit_repository(db: DbSession) -> AdminAuditRepository:
    return AdminAuditRepository(db)


def get_admin_service(db: DbSession) -> AdminService:
    """Step 24 constructed AdminService inline inside each handler.
    Step 24.5 needs it as a proper FastAPI dependency because
    InstanceService takes it as a constructor arg (File 7)."""
    return AdminService(db)


def get_luciel_instance_service(
    db: DbSession,
    admin_service: Annotated[AdminService, Depends(get_admin_service)],
) -> InstanceService:
    return InstanceService(db, admin_service=admin_service)


def get_audit_context(request: Request) -> AuditContext:
    """Capture WHO is performing an admin action, once per request."""
    return AuditContext.from_request(request)