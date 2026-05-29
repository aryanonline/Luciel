"""InstanceToolAuthorization service — Arc 12 WU2.

Thin orchestration layer on top of
``InstanceToolAuthorizationRepository``. Centralises:

  * idempotency on authorise (no live row → INSERT; live row exists →
    return the existing row, do not raise)
  * Wall-1 / Wall-3 scope enforcement at the application layer (the
    repo already filters on ``(admin_id, instance_id)``; the service
    is the documented place to add e.g. ``ScopePolicy`` reuse when
    the grant-authoring API lands in a future WU).

WU4 (sibling-grant authoring) is the unit that will wire
``ScopePolicy.enforce_role_on_instance`` for the cross-instance
case. For per-instance tool authorisation (WU2), the rule is "Admin
owns the Instance" — that's Wall-1 + Wall-3, both of which the repo
already enforces by filter and by RLS. The service interface here
is intentionally minimal so the WU4 grant-authoring API has a clear
seam to plug into.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from sqlalchemy.orm import Session

from app.models.instance_tool_authorization import (
    InstanceToolAuthorization,
)
from app.repositories.instance_tool_authorization_repository import (
    InstanceToolAuthorizationRepository,
)

logger = logging.getLogger(__name__)


class InstanceToolAuthorizationService:
    """Authorise / list / revoke tools on an Instance."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = InstanceToolAuthorizationRepository(db)

    # ------------------------------------------------------------------
    # Authorise
    # ------------------------------------------------------------------

    def authorize(
        self,
        *,
        admin_id: str,
        instance_id: int,
        tool_id: str,
        authorized_by_user_id: uuid.UUID,
        enabled: bool = True,
    ) -> InstanceToolAuthorization:
        """Idempotent authorise.

        If a live row already exists, return it. Otherwise insert a
        new row. The partial unique index is the backstop against
        race conditions; in the rare case two parallel calls both
        see "no live row" and both INSERT, the second commit raises
        IntegrityError — callers catch and retry the lookup.
        """
        existing = self.repo.get_live(
            admin_id=admin_id,
            instance_id=instance_id,
            tool_id=tool_id,
        )
        if existing is not None:
            return existing
        return self.repo.authorize(
            admin_id=admin_id,
            instance_id=instance_id,
            tool_id=tool_id,
            authorized_by_user_id=authorized_by_user_id,
            enabled=enabled,
        )

    # ------------------------------------------------------------------
    # Revoke
    # ------------------------------------------------------------------

    def revoke(
        self,
        *,
        admin_id: str,
        instance_id: int,
        tool_id: str,
    ) -> bool:
        """Soft-revoke the live row. Idempotent — returns False if
        no live row existed."""
        return self.repo.revoke(
            admin_id=admin_id,
            instance_id=instance_id,
            tool_id=tool_id,
        )

    # ------------------------------------------------------------------
    # List
    # ------------------------------------------------------------------

    def list_for_instance(
        self,
        *,
        admin_id: str,
        instance_id: int,
        include_revoked: bool = False,
    ) -> list[InstanceToolAuthorization]:
        return self.repo.list_for_instance(
            admin_id=admin_id,
            instance_id=instance_id,
            include_revoked=include_revoked,
        )

    def is_authorized(
        self,
        *,
        admin_id: str,
        instance_id: int,
        tool_id: str,
    ) -> bool:
        """The broker's default-deny check. Returns True iff a live,
        enabled row exists for the tuple."""
        row = self.repo.get_live(
            admin_id=admin_id,
            instance_id=instance_id,
            tool_id=tool_id,
        )
        return row is not None and bool(row.enabled)
