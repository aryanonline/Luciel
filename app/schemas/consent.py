"""
Pydantic schemas for consent endpoints.
"""
from __future__ import annotations

from pydantic import BaseModel


class ConsentGrantRequest(BaseModel):
    user_id: str
    tenant_id: str
    consent_type: str = "memory_persistence"
    collection_method: str = "api"
    consent_text: str | None = None
    consent_context: str | None = None


class ConsentWithdrawRequest(BaseModel):
    user_id: str
    tenant_id: str
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