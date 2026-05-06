from fastapi import FastAPI
from slowapi.errors import RateLimitExceeded

from app.api.router import api_router
from app.core.config import settings
from app.middleware.auth import ApiKeyAuthMiddleware
from app.middleware.rate_limit import (
    limiter,
    rate_limit_exceeded_handler,
    create_rate_limit_middleware,
)
from app.repositories.audit_chain import install_audit_chain_event

# Step 28 P3-E.2 / Pillar 23: tamper-evident hash chain on every audit
# row. The before_flush event populates row_hash / prev_row_hash on
# every AdminAuditLog instance pending in any session. Installed here
# at module-import time so every ORM session created downstream
# (FastAPI requests, scripts that import from app.*) inherits it.
# Worker processes install the event in their own bootstrap (worker
# does not import app.main).
install_audit_chain_event()

app = FastAPI(title=settings.app_name)

# Attach limiter to app state (required by SlowAPI)
app.state.limiter = limiter

# Register the clean 429 handler for normal rate limit violations
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

# Add API key authentication middleware
app.add_middleware(ApiKeyAuthMiddleware)

# Add the fallback middleware — catches Redis outages and fails open
RateLimitFallbackMiddleware = create_rate_limit_middleware()
app.add_middleware(RateLimitFallbackMiddleware)

# Register all API routes
app.include_router(api_router, prefix=settings.api_v1_prefix)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": settings.app_name}