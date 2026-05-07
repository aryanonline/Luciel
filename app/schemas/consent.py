"""
Pydantic schemas for consent endpoints.

Step 29.y Cluster 1: ``tenant_id`` is now Optional. Pre-29.y the body
required it, but the new contract derives tenant from
``request.state.tenant_id`` (set by the auth middleware from the API
key) for non-platform-admin callers, and only platform-admin callers
may supply it explicitly. Making the field optional at the schema
layer keeps existing platform-admin callers (verify suite, support
tools) working without changing every call site, while preventing
tenant-scoped clients from accidentally drifting away from the
key-derived tenant.
"""
from __future__ import annotations

from pydantic import BaseModel


class ConsentGrantRequest(BaseModel):
    user_id: str
    tenant_id: str | None = None
    consent_type: str = "memory_persistence"
    collection_method: str = "api"
    consent_text: str | None = None
    consent_context: str | None = None


class ConsentWithdrawRequest(BaseModel):
    user_id: str
    tenant_id: str | None = None
    consent_type: str = "memory_persistence"


class ConsentStatusResponse(BaseModel):
    user_id: str
    tenant_id: str
    consent_type: str
    granted: bool
    collection_method: str | None = None
    granted_at: str | None = None

    class Config:
        from_attributes = True


class ConsentActionResponse(BaseModel):
    status: str
    message: str