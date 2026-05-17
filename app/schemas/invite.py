"""UserInvite schemas -- request/response models for the invite arc.

Step 30a.4. Matches the UserInvite SQLAlchemy model
(app/models/user_invite.py) and the four-event InviteService surface
(app/services/invite_service.py).

Domain-agnostic: no vertical enums, no hardcoded role names beyond
the v1 default "teammate".
"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.user_invite import InviteStatus


# ---------------------------------------------------------------------
# Shared field constraints
# ---------------------------------------------------------------------

_SLUG_PATTERN = r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$"


# ---------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------


class UserInviteCreate(BaseModel):
    """Payload for POST /api/v1/admin/invites.

    tenant_id and domain_id are derived server-side from the cookied
    User's active ScopeAssignment when omitted; passing them explicitly
    is the platform_admin path (Step 30a.5's Company-admin-invites-lead
    leg). Most Team-tier callers will only pass invited_email.
    """

    invited_email: EmailStr = Field(
        ...,
        description="Email address of the teammate to invite.",
    )
    tenant_id: str | None = Field(
        default=None,
        min_length=2,
        max_length=100,
        pattern=_SLUG_PATTERN,
        description=(
            "Tenant that owns the invite. Defaults to the cookied user's "
            "active tenant when omitted."
        ),
    )
    domain_id: str | None = Field(
        default=None,
        min_length=2,
        max_length=100,
        pattern=_SLUG_PATTERN,
        description=(
            "Domain (department/vertical) the invitee will be provisioned "
            "into. Defaults to the cookied user's active domain when omitted."
        ),
    )
    role: str = Field(
        default="teammate",
        min_length=2,
        max_length=100,
        description=(
            "Role label within the (tenant, domain) scope. v1 defaults to "
            "'teammate'; Step 30a.5 introduces 'department_lead'."
        ),
    )


# ---------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------


class UserInviteRead(BaseModel):
    """Response shape for GET / POST /admin/invites.

    The raw JWT is deliberately NOT exposed -- the invitee receives it
    via the welcome-set-password email, never via the admin's response
    body. Admins see only the lifecycle state.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: str
    domain_id: str
    invited_email: str
    role: str
    status: InviteStatus
    inviter_user_id: uuid.UUID
    expires_at: datetime
    accepted_at: datetime | None = None
    accepted_user_id: uuid.UUID | None = None
    revoked_at: datetime | None = None
    created_at: datetime
    updated_at: datetime | None = None


# ---------------------------------------------------------------------
# Resend / revoke responses
# ---------------------------------------------------------------------


class UserInviteResendResponse(BaseModel):
    """Response shape for POST /admin/invites/{id}/resend."""

    model_config = ConfigDict(from_attributes=True)

    invite: UserInviteRead


class UserInviteRevokeResponse(BaseModel):
    """Response shape for DELETE /admin/invites/{id}."""

    revoked: bool = True
    invite_id: uuid.UUID
