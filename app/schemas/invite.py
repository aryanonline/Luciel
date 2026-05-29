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

from app.models.scope_assignment import ScopeRole
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

    Arc 12 EX1c — ``domain_id`` removed from the request body. Arc 12
    EX3 (this run) drops ``user_invites.domain_id`` outright; the v2
    invite is scoped by ``admin_id`` (plus instance where relevant)
    via the cookied user's active ScopeAssignment.

    admin_id is derived server-side from the cookied User's active
    ScopeAssignment when omitted; passing it explicitly is the
    platform_admin path (Step 30a.5's Company-admin-invites-lead leg).
    Most Team-tier callers will only pass invited_email.
    """

    invited_email: EmailStr = Field(
        ...,
        description="Email address of the teammate to invite.",
    )
    admin_id: str | None = Field(
        default=None,
        min_length=2,
        max_length=100,
        pattern=_SLUG_PATTERN,
        description=(
            "Tenant that owns the invite. Defaults to the cookied user's "
            "active tenant when omitted."
        ),
    )
    role: ScopeRole = Field(
        default=ScopeRole.INSTANCE_OPERATOR,
        description=(
            "Role within the (Admin, Instance) scope. Arc 11 Cleanup C "
            "locked this field to the canonical four-role taxonomy "
            "(admin_owner, admin_manager, instance_operator, "
            "read_only_viewer) from Architecture §3.2.2. Pro-tier invites "
            "default to ``instance_operator`` per Customer Journey §10.3."
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

    # Arc 12 EX1c — ``domain_id`` removed from the public projection.
    # Underlying column persists on user_invites (NOT NULL until EX3).
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    admin_id: str
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
