"""
API key schemas.

Permission vocabulary (Step 24):
- chat           : may call /api/v1/chat and /chat/stream
- sessions       : may manage sessions under its scope
- admin          : may manage domains/agents/knowledge/keys WITHIN its scope
                   (tenant-, domain-, or agent-scoped based on key fields)
- platform_admin : may act across all tenants (VantageMind operators only)

Scope is determined by the key's tenant_id / domain_id / agent_id columns,
not by the permissions list. Permissions gate WHICH actions;
scope gates WHICH rows those actions may touch.

Step 27a: ALLOWED_PERMISSIONS enum validator added to catch typos like
`platformadmin` or `platform-admin` at mint time, before they reach the
DB and silently bypass `"platform_admin" in permissions` checks.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


# Step 27a: single source of truth for valid permission strings.
# Any addition must update this set AND the permission-matrix docs in Section 2.3
# of the canonical recap.
ALLOWED_PERMISSIONS: frozenset[str] = frozenset({
    "chat",
    "sessions",
    "admin",
    "platform_admin",
})


class ApiKeyCreate(BaseModel):
    tenant_id: str | None = Field(
        default=None,
        min_length=2,
        max_length=100,
        description=(
            "NULL for platform-admin keys (cross-tenant bypass via "
            "platform_admin permission per Invariant 5)."
        ),
    )
    domain_id: str | None = None
    agent_id: str | None = None
    luciel_instance_id: int | None = Field(  # Step 24.5
        default=None,
        description=(
            "Pin this key to a specific LucielInstance. When set, the key "
            "can only chat with that one Luciel. Admin keys leave this null."
        ),
    )
    display_name: str
    permissions: list[str] = Field(
        default_factory=lambda: ["chat", "sessions"]
    )
    rate_limit: int = Field(default=1000, ge=0)
    created_by: str | None = None

    @field_validator("permissions")
    @classmethod
    def _validate_permissions(cls, v: list[str]) -> list[str]:
        """
        Step 27a: reject unknown permission strings at mint time.

        Motivation: pre-27a, `ApiKeyCreate(permissions=["platformadmin"])`
        (missing underscore) would land in the DB and silently fail every
        `"platform_admin" in permissions` check downstream, producing a key
        that looks privileged but has zero effective permissions. This
        validator raises ValueError at mint time so the typo surfaces loudly.
        """
        if not isinstance(v, list):
            raise ValueError("permissions must be a list of strings")
        if not v:
            raise ValueError("permissions must not be empty")
        unknown = [p for p in v if p not in ALLOWED_PERMISSIONS]
        if unknown:
            raise ValueError(
                f"unknown permission(s): {unknown!r}. "
                f"allowed: {sorted(ALLOWED_PERMISSIONS)}"
            )
        # normalize: dedupe while preserving order
        seen: set[str] = set()
        out: list[str] = []
        for p in v:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return out


class ApiKeyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    key_prefix: str
    tenant_id: str | None
    domain_id: str | None
    agent_id: str | None
    display_name: str
    permissions: list[str]
    rate_limit: int
    active: bool
    created_by: str | None
    created_at: datetime
    luciel_instance_id: int | None = None


class ApiKeyCreateResponse(BaseModel):
    """Returned only at creation time. The raw_key is shown once and never again."""

    api_key: ApiKeyRead
    raw_key: str