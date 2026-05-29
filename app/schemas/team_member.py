"""Team-member schemas.

Anchored to Vision v1 \u00a76.2 (Team Member lifecycle) and Architecture
v1 \u00a73.7.2 (role scope assignment governs every read and write).

A team member, as the Vision uses the term, is a User row bound to
an Admin via an active ScopeAssignment. The Dashboard Team tab needs
to list these for Pro / Enterprise admins so the founder can see who
is currently scoped to their account.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class TeamMemberRead(BaseModel):
    """One team member under the caller's Admin.

    Sourced from an active ScopeAssignment row joined to its User.

    Arc 12 EX1c — ``domain_id`` removed from the public projection.
    V2 has a single Admin→Instance boundary; the underlying column
    persists on scope_assignments until EX3 drops it.
    """
    model_config = ConfigDict(from_attributes=True)

    # ScopeAssignment fields
    scope_assignment_id: UUID
    role: str  # admin_owner / admin_manager / instance_operator / read_only_viewer
    started_at: datetime
    active: bool

    # User fields
    user_id: UUID
    email: str
    display_name: str
    user_active: bool
