"""InstanceToolAuthorization repository — Arc 12 WU2.

Pure CRUD against the ``instance_tool_authorizations`` table. The
broker's default-deny lookup, the future admin grant-authoring API,
and tests all read through this repo.

Scope of responsibility:
* Authorise / list / revoke rows scoped by ``(admin_id, instance_id)``.
* No policy decisions — service layer enforces who may author / revoke
  what (Wall-2 — see ``InstanceToolAuthorizationService``).
* No HTTP exceptions — callers raise them.

Soft-delete semantics:
* Authorise = INSERT a row with ``revoked_at IS NULL``.
* Revoke    = UPDATE ``revoked_at = NOW()`` on the live row.
* List      = filter ``revoked_at IS NULL`` unless ``include_revoked``.

The partial unique index on ``(admin_id, instance_id, tool_id)
WHERE revoked_at IS NULL`` is the integrity backstop — at most one
live row per tuple at any time.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import and_, select, update
from sqlalchemy.orm import Session

from app.models.instance_tool_authorization import (
    InstanceToolAuthorization,
)

logger = logging.getLogger(__name__)


class InstanceToolAuthorizationRepository:
    """Data-access layer for per-instance tool authorisations.

    All read methods filter on ``(admin_id, instance_id)`` — Wall-1 +
    Wall-3. RLS at the DB layer enforces Wall-1 a second time; this
    explicit filter at the application layer keeps service-layer
    callers honest even in test environments that lack the RLS GUC.
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Hot-path: broker's default-deny lookup
    # ------------------------------------------------------------------

    def get_live(
        self,
        *,
        admin_id: str,
        instance_id: int,
        tool_id: str,
    ) -> Optional[InstanceToolAuthorization]:
        """Return the live (non-revoked) authorisation row for the
        tuple, or ``None`` if no live row exists.

        The broker's default-deny gate maps ``None`` → refuse.
        """
        stmt = (
            select(InstanceToolAuthorization)
            .where(
                and_(
                    InstanceToolAuthorization.admin_id == admin_id,
                    InstanceToolAuthorization.instance_id == instance_id,
                    InstanceToolAuthorization.tool_id == tool_id,
                    InstanceToolAuthorization.revoked_at.is_(None),
                )
            )
            .limit(1)
        )
        return self.db.execute(stmt).scalar_one_or_none()

    # ------------------------------------------------------------------
    # Write: authorise
    # ------------------------------------------------------------------

    def authorize(
        self,
        *,
        admin_id: str,
        instance_id: int,
        tool_id: str,
        authorized_by_user_id: uuid.UUID,
        enabled: bool = True,
        autocommit: bool = True,
    ) -> InstanceToolAuthorization:
        """Insert a new live authorisation row.

        Caller is expected to have already verified there is no
        live row for the tuple (the partial unique index will raise
        IntegrityError otherwise — caller may catch and translate).
        """
        row = InstanceToolAuthorization(
            admin_id=admin_id,
            instance_id=instance_id,
            tool_id=tool_id,
            enabled=enabled,
            authorized_by_user_id=authorized_by_user_id,
        )
        self.db.add(row)
        if autocommit:
            self.db.commit()
            self.db.refresh(row)
        else:
            self.db.flush()
        return row

    # ------------------------------------------------------------------
    # Write: revoke
    # ------------------------------------------------------------------

    def revoke(
        self,
        *,
        admin_id: str,
        instance_id: int,
        tool_id: str,
        autocommit: bool = True,
    ) -> bool:
        """Soft-revoke the live row for the tuple.

        Returns True if a row was revoked, False if no live row
        existed (idempotent).
        """
        now = datetime.now(timezone.utc)
        stmt = (
            update(InstanceToolAuthorization)
            .where(
                and_(
                    InstanceToolAuthorization.admin_id == admin_id,
                    InstanceToolAuthorization.instance_id == instance_id,
                    InstanceToolAuthorization.tool_id == tool_id,
                    InstanceToolAuthorization.revoked_at.is_(None),
                )
            )
            .values(revoked_at=now, updated_at=now)
        )
        result = self.db.execute(stmt)
        if autocommit:
            self.db.commit()
        return result.rowcount > 0

    # ------------------------------------------------------------------
    # Read: listings
    # ------------------------------------------------------------------

    def list_for_instance(
        self,
        *,
        admin_id: str,
        instance_id: int,
        include_revoked: bool = False,
    ) -> list[InstanceToolAuthorization]:
        """List all authorisations for an instance.

        Filters live rows by default. Pass ``include_revoked=True``
        to include the full historical chain (revoked + live).
        """
        conditions = [
            InstanceToolAuthorization.admin_id == admin_id,
            InstanceToolAuthorization.instance_id == instance_id,
        ]
        if not include_revoked:
            conditions.append(
                InstanceToolAuthorization.revoked_at.is_(None)
            )
        stmt = (
            select(InstanceToolAuthorization)
            .where(and_(*conditions))
            .order_by(InstanceToolAuthorization.created_at.desc())
        )
        return list(self.db.execute(stmt).scalars())

    def list_authorized_tool_ids(
        self,
        *,
        admin_id: str,
        instance_id: int,
    ) -> set[str]:
        """Convenience: the set of currently-live tool_ids on an
        instance. Returned as a set so callers can do membership
        checks cheaply (broker uses ``get_live`` for the single-tool
        hot path; chat_service / UI may want the full set)."""
        rows = self.list_for_instance(
            admin_id=admin_id, instance_id=instance_id
        )
        return {r.tool_id for r in rows if r.enabled}
