from fastapi import APIRouter

from app.api.v1 import admin, chat, health, sessions
from app.api.v1 import retention
from app.api.v1 import consent  # ADD THIS
from app.api.v1 import verification  # Step 26b
from app.api.v1 import users  # Step 24.5b
from app.api.v1 import audit_log  # Step 28 Phase 2 - Commit 2

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(chat.router)
api_router.include_router(sessions.router, prefix="/sessions")
api_router.include_router(admin.router)
api_router.include_router(retention.router)
api_router.include_router(consent.router)  # ADD THIS
api_router.include_router(verification.router)  # Step 26b.2
api_router.include_router(users.router)  # Step 24.5b
api_router.include_router(audit_log.router)  # Step 28 Phase 2 - Commit 2