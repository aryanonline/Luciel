"""
FastAPI dependency injection wiring.
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.integrations.llm.router import ModelRouter
from app.memory.service import MemoryService
from app.repositories.memory_repository import MemoryRepository
from app.repositories.session_repository import SessionRepository
from app.repositories.trace_repository import TraceRepository
from app.services.chat_service import ChatService
from app.services.session_service import SessionService
from app.services.trace_service import TraceService
from app.tools.broker import ToolBroker
from app.tools.registry import ToolRegistry

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


def get_chat_service(
    session_service: Annotated[SessionService, Depends(get_session_service)],
    memory_service: Annotated[MemoryService, Depends(get_memory_service)],
    trace_service: Annotated[TraceService, Depends(get_trace_service)],
) -> ChatService:
    return ChatService(
        session_service=session_service,
        memory_service=memory_service,
        model_router=_model_router,
        tool_registry=_tool_registry,
        tool_broker=_tool_broker,
        trace_service=trace_service,
    )