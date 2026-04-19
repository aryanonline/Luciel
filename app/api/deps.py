"""
FastAPI dependency injection wiring.
"""

# Add import at top
from app.repositories.consent_repository import ConsentRepository
from app.policy.consent import ConsentPolicy
from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
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
from app.repositories.agent_repository import AgentRepository  # noqa: E402
from app.repositories.luciel_instance_repository import (  # noqa: E402
    LucielInstanceRepository,
)
from app.services.admin_service import AdminService  # noqa: E402
from app.services.luciel_instance_service import LucielInstanceService  # noqa: E402

DbSession = Annotated[Session, Depends(get_db)]

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


def get_ingestion_service(
    repository: Annotated[KnowledgeRepository, Depends(get_knowledge_repository)],
) -> IngestionService:
    return IngestionService(repository=repository)

# Add these two functions
def get_consent_repository(db: DbSession) -> ConsentRepository:
    return ConsentRepository(db)

def get_consent_policy(
    repo: Annotated[ConsentRepository, Depends(get_consent_repository)],
) -> ConsentPolicy:
    return ConsentPolicy(consent_repository=repo)

def get_luciel_instance_repository(db: DbSession) -> LucielInstanceRepository:
    return LucielInstanceRepository(db)

def get_chat_service(
    session_service: Annotated[SessionService, Depends(get_session_service)],
    memory_service: Annotated[MemoryService, Depends(get_memory_service)],
    trace_service: Annotated[TraceService, Depends(get_trace_service)],
    knowledge_retriever: Annotated[KnowledgeRetriever, Depends(get_knowledge_retriever)],
    config_repository: Annotated[ConfigRepository, Depends(get_config_repository)],
    luciel_instance_repository: Annotated[                                          # Step 24.5 File 15
        LucielInstanceRepository, Depends(get_luciel_instance_repository)           # Step 24.5 File 15
    ],                                                                              # Step 24.5 File 15
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
        luciel_instance_repository=luciel_instance_repository,   # Step 24.5 File 15
        consent_policy=consent_policy,
    )

def get_agent_repository(db: DbSession) -> AgentRepository:
    return AgentRepository(db)


def get_admin_audit_repository(db: DbSession) -> AdminAuditRepository:
    return AdminAuditRepository(db)


def get_admin_service(db: DbSession) -> AdminService:
    """Step 24 constructed AdminService inline inside each handler.
    Step 24.5 needs it as a proper FastAPI dependency because
    LucielInstanceService takes it as a constructor arg (File 7)."""
    return AdminService(db)


def get_luciel_instance_service(
    db: DbSession,
    admin_service: Annotated[AdminService, Depends(get_admin_service)],
) -> LucielInstanceService:
    return LucielInstanceService(db, admin_service=admin_service)


def get_audit_context(request: Request) -> AuditContext:
    """Capture WHO is performing an admin action, once per request."""
    return AuditContext.from_request(request)