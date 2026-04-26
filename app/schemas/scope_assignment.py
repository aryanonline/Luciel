"""
ScopeAssignment schemas — request/response models for the durable role-binding
between User and (tenant, domain).

Step 24.5b Q6 resolution: "Data lives with scope, not person. Users + scope
assignments + mandatory key rotation + immutable audit log."

A ScopeAssignment captures "User U held role R within (tenant T, domain D)
from time A to time B (or still active if B is null)." Promotions, demotions,
reassignments, and departures are end-and-recreate operations — never UPDATE
in place — so the full role history is walkable backwards for PIPEDA / audit.

Matches the SQLAlchemy model in app/models/scope_assignment.py.

Domain-agnostic: role is a free-form slug-style string. A future step may
promote it to a controlled vocabulary once real-world role taxonomy stabilizes
across verticals (real estate, mortgage, property management, ...).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.scope_assignment import EndReason


# ---------------------------------------------------------------------
# Shared field constraints
# ---------------------------------------------------------------------

# Slug pattern: lowercase alphanumerics + hyphens (and underscores for
# role labels like "team_lead", "broker_of_record"). Mirrors the project
# convention from app/schemas/agent.py with the underscore concession
# for human-readable role labels.
_ROLE_PATTERN = r"^[a-z0-9]([a-z0-9_-]*[a-z0-9])?$"

# tenant_id / domain_id reuse the existing slug pattern from elsewhere
# in the codebase. NOT normalized in the schema -- service layer asserts
# existence against the live tables (so a typo produces a 404, not a
# silent slug rewrite that masks the error).
_SLUG_PATTERN = r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$"


# ---------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------

class ScopeAssignmentCreate(BaseModel):
    """Payload for POST /api/v1/users/{user_id}/scope-assignments.

    Caller's API key must have admin scope at-or-above (tenant_id, domain_id)
    per Invariant 5 -- enforced at the service layer, not here.

    user_id is taken from the URL path, not the body. started_at defaults
    to server now() if omitted (matches the column server_default).
    """

    tenant_id: str = Field(
        ...,
        min_length=2,
        max_length=100,
        pattern=_SLUG_PATTERN,
        description="Tenant scope of this assignment.",
    )
    domain_id: str = Field(
        ...,
        min_length=2,
        max_length=100,
        pattern=_SLUG_PATTERN,
        description="Domain scope. Must exist and be active under the given "
                    "tenant_id (validated at the service layer, no composite FK).",
    )
    role: str = Field(
        ...,
        min_length=2,
        max_length=100,
        pattern=_ROLE_PATTERN,
        description="Role label within the (tenant, domain) scope. "
                    "e.g. 'listings_agent', 'team_lead', 'broker_of_record'. "
                    "Free-form for now -- may become an enum once role "
                    "taxonomy stabilizes across verticals.",
    )
    started_at: datetime | None = Field(
        default=None,
        description="When the assignment started. Defaults to server now() "
                    "if omitted. Override only for historical backfills.",
    )
    created_by: str | None = Field(default=None, max_length=100)


# ---------------------------------------------------------------------
# End (the assignment-lifecycle action)
# ---------------------------------------------------------------------

class EndAssignmentRequest(BaseModel):
    """Payload for POST /api/v1/scope-assignments/{id}/end.

    This is the single, audit-clean entry point for promotion / demotion /
    reassignment / departure / administrative deactivation. Service layer:

    1. Asserts the assignment is currently active (ended_at IS NULL).
    2. Sets ended_at = now(), ended_reason = reason, ended_note = note,
       ended_by_api_key_id = caller's key id.
    3. Triggers MANDATORY KEY ROTATION for any ApiKey bound to the affected
       Agent and any LucielInstance under that Agent (Q6 resolution).
    4. Emits SCOPE_ASSIGNMENT_ENDED audit row + per-key
       KEY_ROTATED_ON_ROLE_CHANGE audit rows.

    All four steps land in the same txn (Invariant 4).
    """

    reason: EndReason = Field(
        ...,
        description=(
            "Why the assignment ended. Typed enum -- malformed reasons fail "
            "fast at the API boundary, not at DB write time. "
            "PROMOTED / DEMOTED / REASSIGNED / DEPARTED / DEACTIVATED."
        ),
    )
    note: str | None = Field(
        default=None,
        max_length=500,
        description=(
            "Free-form business context. Non-PII safe -- PII goes in the "
            "audit log row's actor snapshot, not here. e.g. 'promoted to "
            "team lead', 'departed REMAX Crossroads 2026-05-01'."
        ),
    )

    @field_validator("note")
    @classmethod
    def _normalize_note(cls, v: str | None) -> str | None:
        """Strip + collapse whitespace; treat all-whitespace as None."""
        if v is None:
            return None
        if not isinstance(v, str):
            raise ValueError("note must be a string")
        cleaned = " ".join(v.split())
        return cleaned if cleaned else None


# ---------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------

class ScopeAssignmentRead(BaseModel):
    """Full response shape for single-assignment reads.

    Includes all audit metadata. ended_by_api_key_id exposure is gated
    at the service / route layer -- platform-admins see it, tenant-admins
    only see assignments under their own tenant and may have the actor
    field stripped if cross-tenant actor identity matters.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    tenant_id: str
    domain_id: str
    role: str
    started_at: datetime
    ended_at: datetime | None = None
    ended_reason: EndReason | None = None
    ended_note: str | None = None
    ended_by_api_key_id: int | None = None
    active: bool
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------
# Summary (used in list endpoints to keep responses cheap)
# ---------------------------------------------------------------------

class ScopeAssignmentSummary(BaseModel):
    """Compact assignment shape for list responses.

    Used by GET /api/v1/users/{user_id}/scope-assignments?active=true and
    similar listing endpoints. Drops audit-actor metadata to keep
    cross-tenant list payloads slim and to avoid unnecessary actor
    disclosure in mass reads.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    tenant_id: str
    domain_id: str
    role: str
    started_at: datetime
    ended_at: datetime | None = None
    ended_reason: EndReason | None = None
    active: bool