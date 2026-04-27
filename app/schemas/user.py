"""
User schemas (Step 24.5b).

The User layer is the durable person identity that spans tenants/agents.
Per Q6 resolution: "Data lives with scope, not person. Users + scope
assignments + mandatory key rotation + immutable audit log."

Schema vocabulary:
- UserCreate          : input for POST /api/v1/users (platform-admin only).
                        Real users land with synthetic=False. Backward-compat
                        Option B onboarding (Step 23) auto-creates with
                        synthetic=True via OnboardingService -- not via this
                        schema directly.
- UserRead            : response shape. ORM-to-schema via from_attributes.
                        Used by GET /api/v1/users/{id} and POST response.
- UserUpdate          : input for PATCH /api/v1/users/{id}. All fields
                        optional. Cannot toggle `synthetic` post-create
                        (rejected at service layer). Cannot toggle `active`
                        here -- deactivation goes through UserDeactivate
                        for audit-row clarity (Invariant 4).
- UserDeactivate      : input for the explicit deactivation endpoint.
                        Reason is required and feeds the audit row -- every
                        deactivation has recorded business justification.

Email handling:
- Email is normalized to lowercase on input. Matches the LOWER(email)
  expression index landed in File 1.9 -- case-insensitive uniqueness
  enforced both at validation time AND at DB level.
- EmailStr from pydantic[email] is the standard validator. Project already
  depends on email-validator transitively via pydantic[email] in
  pyproject.toml (verified during Step 27c-final dependency audit).

Synthetic flag:
- `synthetic=True` flags users auto-created via Step 23 Option B onboarding
  backward-compat path. Real users created via POST /api/v1/users land with
  synthetic=False. PIPEDA access/erasure paths filter on this so we don't
  surface auto-generated stubs as if they were real persons.
- Service layer rejects non-platform-admin attempts to set synthetic=True
  on create. Schema permits the field; authorization gates writes.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class UserCreate(BaseModel):
    """Input for POST /api/v1/users. Platform-admin only at the route level."""

    email: EmailStr = Field(
        ...,
        description=(
            "Durable email identity. Lowercased on input to match the "
            "LOWER(email) expression index. RFC 5321 max length 320."
        ),
    )
    display_name: str = Field(
        ...,
        min_length=2,
        max_length=200,
        description="Human-readable name. Stripped + whitespace-collapsed on input.",
    )
    synthetic: bool = Field(
        default=False,
        description=(
            "True for auto-generated stubs (Step 23 Option B onboarding "
            "backward-compat). Real users land with synthetic=False. "
            "Service layer rejects non-platform-admin attempts to set True."
        ),
    )

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        """
        Lowercase + strip the email.

        Motivation: case-insensitive uniqueness is enforced at the DB level
        via LOWER(email) expression index (File 1.9). Normalizing on input
        means equality comparisons in service code don't have to remember
        to LOWER() -- the canonical form lives in the column.
        """
        if not isinstance(v, str):
            raise ValueError("email must be a string")
        return v.strip().lower()

    @field_validator("display_name")
    @classmethod
    def _normalize_display_name(cls, v: str) -> str:
        """
        Strip + collapse internal whitespace.

        Motivation: ' Sarah   Listings ' and 'Sarah Listings' should be
        treated as the same display name. Avoids look-alike-display-name
        impersonation in tenant-internal tooling.
        """
        if not isinstance(v, str):
            raise ValueError("display_name must be a string")
        cleaned = " ".join(v.split())
        if len(cleaned) < 2:
            raise ValueError("display_name must be at least 2 non-whitespace chars")
        return cleaned


class UserRead(BaseModel):
    """Response shape for GET /api/v1/users/{id} and POST /api/v1/users."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    display_name: str
    synthetic: bool
    active: bool
    created_at: datetime
    updated_at: datetime


class UserUpdate(BaseModel):
    """
    Input for PATCH /api/v1/users/{id}.

    All fields optional. The service layer:
    - Rejects toggling `synthetic` (not exposed here -- create-time only).
    - Rejects toggling `active` (route through UserDeactivate for audit clarity).
    - Re-validates email uniqueness if changed (LOWER(email) collision check).
    """

    email: EmailStr | None = Field(
        default=None,
        description=(
            "If provided, re-validated for uniqueness against LOWER(email) "
            "across all users (active and inactive). Lowercased on input."
        ),
    )
    display_name: str | None = Field(
        default=None,
        min_length=2,
        max_length=200,
    )

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not isinstance(v, str):
            raise ValueError("email must be a string")
        return v.strip().lower()

    @field_validator("display_name")
    @classmethod
    def _normalize_display_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not isinstance(v, str):
            raise ValueError("display_name must be a string")
        cleaned = " ".join(v.split())
        if len(cleaned) < 2:
            raise ValueError("display_name must be at least 2 non-whitespace chars")
        return cleaned


class UserDeactivate(BaseModel):
    """
    Input for the explicit deactivation endpoint.

    Deactivation is its own action separate from PATCH because:
    1. It cascades to ScopeAssignments (ended_at + ended_reason=DEACTIVATED)
       and to all bound ApiKeys (mandatory key rotation per Q6 resolution).
       The audit row needs a single distinct action label, not a generic
       "user updated" trail.
    2. Reason is required -- forces the operator to record business
       justification at action time, before context is lost.
    """

    reason: str = Field(
        ...,
        min_length=10,
        max_length=500,
        description=(
            "Business justification. Recorded immutably in admin_audit_logs "
            "in the same txn as the deactivation (Invariant 4). Examples: "
            "'departed REMAX Crossroads 2026-05-01', 'role consolidation per "
            "broker-of-record request', 'PIPEDA right-of-erasure request "
            "ticket #1234'."
        ),
    )

    @field_validator("reason")
    @classmethod
    def _normalize_reason(cls, v: str) -> str:
        """Strip + reject all-whitespace reasons."""
        if not isinstance(v, str):
            raise ValueError("reason must be a string")
        cleaned = v.strip()
        if len(cleaned) < 10:
            raise ValueError("reason must be at least 10 non-whitespace chars")
        return cleaned