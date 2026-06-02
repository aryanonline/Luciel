"""InstanceConnection repository — Arc 15 WU4 (Arc 17 connection slice).

Pure CRUD against the ``instance_connections`` table. The WU5 dispatch
gate's hot-path lookup, the connections admin API, and tests all read
through this repo.

Scope of responsibility:
* Configure / list / disconnect rows scoped by ``(admin_id, instance_id)``.
* No policy decisions — the route enforces who may configure what
  (Wall-2) and which connectors connect LIVE vs land ``unconfigured``.
* No HTTP exceptions — callers raise them.

Soft-delete semantics:
* Configure   = INSERT a row with ``revoked_at IS NULL``.
* Disconnect  = UPDATE ``revoked_at = NOW()`` on the live row.
* List        = filter ``revoked_at IS NULL`` unless ``include_revoked``.

The partial unique index on
``(admin_id, instance_id, connection_type, provider) WHERE revoked_at
IS NULL`` is the integrity backstop — at most one live row per tuple.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import and_, select, update
from sqlalchemy.orm import Session

from app.models.instance_connection import InstanceConnection

logger = logging.getLogger(__name__)


class InstanceConnectionRepository:
    """Data-access layer for per-instance external-system connections.

    All read methods filter on ``(admin_id, instance_id)`` — Wall-1 +
    Wall-3. RLS at the DB layer enforces Wall-1 a second time; the
    explicit application filter keeps callers honest in test
    environments that lack the RLS GUC.
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Hot-path: gate's connection lookup (by connection_type).
    # ------------------------------------------------------------------

    def get_live_by_type(
        self,
        *,
        admin_id: str,
        instance_id: int,
        connection_type: str,
    ) -> Optional[InstanceConnection]:
        """Return the live (non-revoked) connection row for the
        ``(admin_id, instance_id, connection_type)`` tuple, or ``None``.

        WU5's gate maps a missing/non-``connected`` row → refuse.
        """
        stmt = (
            select(InstanceConnection)
            .where(
                and_(
                    InstanceConnection.admin_id == admin_id,
                    InstanceConnection.instance_id == instance_id,
                    InstanceConnection.connection_type == connection_type,
                    InstanceConnection.revoked_at.is_(None),
                )
            )
            .order_by(InstanceConnection.created_at.desc())
            .limit(1)
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def get_live_for_admin(
        self, *, admin_id: str, connection_id: int
    ) -> Optional[InstanceConnection]:
        """Return a single live row by PK, fenced to the admin (Wall-1)."""
        stmt = (
            select(InstanceConnection)
            .where(
                and_(
                    InstanceConnection.id == connection_id,
                    InstanceConnection.admin_id == admin_id,
                    InstanceConnection.revoked_at.is_(None),
                )
            )
            .limit(1)
        )
        return self.db.execute(stmt).scalar_one_or_none()

    # ------------------------------------------------------------------
    # Write: configure (create a connection row).
    # ------------------------------------------------------------------

    def configure(
        self,
        *,
        admin_id: str,
        instance_id: int,
        connection_type: str,
        provider: str,
        status: str,
        config_json: dict | None = None,
        credential_ref: str | None = None,
        last_verified_at: datetime | None = None,
        autocommit: bool = True,
    ) -> InstanceConnection:
        """Insert a new live connection row.

        ``status`` is decided by the caller (the route): connectors with
        a real backing land ``connected``; deferred connectors land
        ``unconfigured``. The repository never fabricates a status.
        """
        row = InstanceConnection(
            admin_id=admin_id,
            instance_id=instance_id,
            connection_type=connection_type,
            provider=provider,
            status=status,
            config_json=config_json,
            credential_ref=credential_ref,
            last_verified_at=last_verified_at,
        )
        self.db.add(row)
        if autocommit:
            self.db.commit()
            self.db.refresh(row)
        else:
            self.db.flush()
        return row

    # ------------------------------------------------------------------
    # Write: disconnect (soft-delete).
    # ------------------------------------------------------------------

    def disconnect(
        self,
        *,
        admin_id: str,
        connection_id: int,
        autocommit: bool = True,
    ) -> bool:
        """Soft-revoke a single connection row by PK, fenced to the
        admin. Returns True if a live row was revoked, False otherwise
        (idempotent)."""
        now = datetime.now(timezone.utc)
        stmt = (
            update(InstanceConnection)
            .where(
                and_(
                    InstanceConnection.id == connection_id,
                    InstanceConnection.admin_id == admin_id,
                    InstanceConnection.revoked_at.is_(None),
                )
            )
            .values(revoked_at=now, updated_at=now)
        )
        result = self.db.execute(stmt)
        if autocommit:
            self.db.commit()
        return result.rowcount > 0

    # ------------------------------------------------------------------
    # Read: listings.
    # ------------------------------------------------------------------

    def list_for_instance(
        self,
        *,
        admin_id: str,
        instance_id: int,
        include_revoked: bool = False,
    ) -> list[InstanceConnection]:
        """List connections for an instance (live by default)."""
        conditions = [
            InstanceConnection.admin_id == admin_id,
            InstanceConnection.instance_id == instance_id,
        ]
        if not include_revoked:
            conditions.append(InstanceConnection.revoked_at.is_(None))
        stmt = (
            select(InstanceConnection)
            .where(and_(*conditions))
            .order_by(InstanceConnection.created_at.desc())
        )
        return list(self.db.execute(stmt).scalars())

    def live_status_by_type(
        self, *, admin_id: str, instance_id: int
    ) -> dict[str, str]:
        """Map of ``connection_type -> status`` for live rows on the
        instance. Used by the ToolView serializer to surface
        per-tool connection_status without an N+1 query."""
        rows = self.list_for_instance(
            admin_id=admin_id, instance_id=instance_id
        )
        # Most recent live row per type wins (list is created_at DESC).
        out: dict[str, str] = {}
        for r in rows:
            out.setdefault(r.connection_type, r.status)
        return out
