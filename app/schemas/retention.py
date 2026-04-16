"""
Pydantic schemas for retention policy endpoints.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, field_validator


VALID_CATEGORIES = {
    "sessions",
    "messages",
    "memory_items",
    "traces",
    "knowledge_embeddings",
}

VALID_ACTIONS = {"delete", "anonymize"}


class RetentionPolicyCreate(BaseModel):
    tenant_id: str | None = None
    data_category: str
    retention_days: int
    action: str = "anonymize"
    purpose: str | None = None
    created_by: str | None = None

    @field_validator("data_category")
    @classmethod
    def validate_category(cls, v: str) -> str:
        if v not in VALID_CATEGORIES:
            raise ValueError(
                f"Invalid data_category: {v}. Valid: {VALID_CATEGORIES}"
            )
        return v

    @field_validator("action")
    @classmethod
    def validate_action(cls, v: str) -> str:
        if v not in VALID_ACTIONS:
            raise ValueError(
                f"Invalid action: {v}. Valid: {VALID_ACTIONS}"
            )
        return v

    @field_validator("retention_days")
    @classmethod
    def validate_days(cls, v: int) -> int:
        if v < 0:
            raise ValueError("retention_days must be >= 0")
        return v


class RetentionPolicyUpdate(BaseModel):
    retention_days: int | None = None
    action: str | None = None
    purpose: str | None = None
    active: bool | None = None
    updated_by: str | None = None

    @field_validator("action")
    @classmethod
    def validate_action(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_ACTIONS:
            raise ValueError(
                f"Invalid action: {v}. Valid: {VALID_ACTIONS}"
            )
        return v

    @field_validator("retention_days")
    @classmethod
    def validate_days(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError("retention_days must be >= 0")
        return v


class RetentionPolicyRead(BaseModel):
    id: int
    tenant_id: str | None
    data_category: str
    retention_days: int
    action: str
    purpose: str | None
    active: bool
    created_by: str | None
    updated_by: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class DeletionLogRead(BaseModel):
    id: int
    tenant_id: str | None
    data_category: str
    action_taken: str
    rows_affected: int
    cutoff_date: str
    triggered_by: str
    reason: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ManualPurgeRequest(BaseModel):
    data_category: str
    tenant_id: str | None = None
    reason: str

    @field_validator("data_category")
    @classmethod
    def validate_category(cls, v: str) -> str:
        if v not in VALID_CATEGORIES:
            raise ValueError(
                f"Invalid data_category: {v}. Valid: {VALID_CATEGORIES}"
            )
        return v

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("reason is required for manual purges")
        return v


class EnforceResult(BaseModel):
    policy_id: int | None = None
    data_category: str
    action: str
    rows_affected: int = 0
    cutoff_date: str | None = None
    tenant_id: str | None = None
    error: str | None = None