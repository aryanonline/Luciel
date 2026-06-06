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

from app.connections.instance_connection import InstanceConnection

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
        non_secret_config: dict | None = None,
        secret_ref: str | None = None,
        last_health_check_at: datetime | None = None,
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
            non_secret_config=non_secret_config,
            secret_ref=secret_ref,
            last_health_check_at=last_health_check_at,
        )
        self.db.add(row)
        if autocommit:
            self.db.commit()
            self.db.refresh(row)
        else:
            self.db.flush()
        return row

    # ------------------------------------------------------------------
    # Write: apply a health-check / refresh result.
    # ------------------------------------------------------------------

    def apply_health_check(
        self,
        *,
        row: InstanceConnection,
        status: str,
        last_health_check_at: datetime | None,
        secret_ref: str | None = None,
        status_detail: str | None = None,
        autocommit: bool = True,
    ) -> InstanceConnection:
        """Persist the honest outcome of a refresh/health check onto an
        already-loaded row.

        ``status`` and ``last_health_check_at`` come from the health
        service; ``secret_ref`` is updated ONLY when a silent token
        refresh rotated the stored secret (a NEW ref) — it is never
        cleared here. ``status_detail`` is written on the expired path
        (CJ §7 Reconnect chip) and cleared when status is not expired.
        The caller (route or worker) writes the audit row
        in the same transaction.
        """
        row.status = status
        if last_health_check_at is not None:
            row.last_health_check_at = last_health_check_at
        if secret_ref is not None:
            row.secret_ref = secret_ref
        # Populate status_detail on the expired path (§3.8.5).
        # Clear it when the connection transitions to a healthy state.
        if status_detail is not None:
            row.status_detail = status_detail
        elif status in ("connected", "unconfigured"):
            row.status_detail = None
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
    # Write: lifecycle cascade (Arc 10 — revoke ALL on deactivation /
    # account closure). Returns the rows it revoked so the caller can
    # audit each and enqueue secret cleanup for non-null secret_refs.
    # ------------------------------------------------------------------

    def revoke_all_for_instance(
        self,
        *,
        admin_id: str,
        instance_id: int,
        autocommit: bool = True,
    ) -> list[InstanceConnection]:
        """Soft-revoke every live connection row for an instance.

        Returns the rows AS THEY WERE before revocation (their
        ``secret_ref`` is needed by the caller to enqueue secret
        cleanup). Idempotent: an already-revoked row is skipped.
        """
        rows = self.list_for_instance(
            admin_id=admin_id, instance_id=instance_id
        )
        return self._revoke_rows(rows, autocommit=autocommit)

    def revoke_all_for_admin(
        self,
        *,
        admin_id: str,
        autocommit: bool = True,
    ) -> list[InstanceConnection]:
        """Soft-revoke every live connection row across ALL of the
        admin's instances (account closure). Returns the revoked rows."""
        stmt = (
            select(InstanceConnection)
            .where(
                and_(
                    InstanceConnection.admin_id == admin_id,
                    InstanceConnection.revoked_at.is_(None),
                )
            )
            .order_by(InstanceConnection.created_at.desc())
        )
        rows = list(self.db.execute(stmt).scalars())
        return self._revoke_rows(rows, autocommit=autocommit)

    def _revoke_rows(
        self,
        rows: list[InstanceConnection],
        *,
        autocommit: bool,
    ) -> list[InstanceConnection]:
        now = datetime.now(timezone.utc)
        for row in rows:
            row.revoked_at = now
            row.updated_at = now
            # Dual representation: revoked_at IS NOT NULL ⇒ status='revoked'.
            # Both signals must agree (rescand_connections_schema §3.8.4).
            row.status = "revoked"
            self.db.add(row)
        if autocommit:
            self.db.commit()
        else:
            self.db.flush()
        return rows

    # ------------------------------------------------------------------
    # Write: dormant transitions (Pro→Free downgrade / re-upgrade).
    # §3.6.7: on downgrade, retain secrets and set status='dormant'.
    # On re-upgrade, restore the prior status from status_detail.
    # ------------------------------------------------------------------

    def set_dormant_for_admin(
        self,
        *,
        admin_id: str,
        autocommit: bool = True,
    ) -> list[InstanceConnection]:
        """Set every live (non-revoked, non-dormant) connection for the
        admin to status='dormant', preserving secret_ref (secrets
        are retained per §3.6.7).  Stores the prior status in
        status_detail so restore_from_dormant can recover it.

        Returns the list of connections that were set dormant.
        """
        stmt = (
            select(InstanceConnection)
            .where(
                and_(
                    InstanceConnection.admin_id == admin_id,
                    InstanceConnection.revoked_at.is_(None),
                    InstanceConnection.status != "dormant",
                    InstanceConnection.status != "revoked",
                )
            )
            .order_by(InstanceConnection.created_at.desc())
        )
        rows = list(self.db.execute(stmt).scalars())
        now = datetime.now(timezone.utc)
        for row in rows:
            # Store the prior status in status_detail for restore.
            prior = row.status
            row.status = "dormant"
            row.status_detail = (
                f"prior_status={prior}; "
                "Connection dormant: Pro→Free downgrade. "
                "Secrets retained. Re-upgrade to restore."
            )
            row.updated_at = now
            self.db.add(row)
        if autocommit:
            self.db.commit()
        else:
            self.db.flush()
        return rows

    def restore_from_dormant_for_admin(
        self,
        *,
        admin_id: str,
        autocommit: bool = True,
    ) -> list[InstanceConnection]:
        """Restore all dormant connections for an admin to their prior
        status (stored in status_detail) on re-upgrade.

        If status_detail carries a ``prior_status=<value>`` prefix, that
        value is restored; otherwise falls back to ``'connected'``.
        Clears status_detail after restore.
        """
        stmt = (
            select(InstanceConnection)
            .where(
                and_(
                    InstanceConnection.admin_id == admin_id,
                    InstanceConnection.status == "dormant",
                )
            )
            .order_by(InstanceConnection.created_at.desc())
        )
        rows = list(self.db.execute(stmt).scalars())
        now = datetime.now(timezone.utc)
        for row in rows:
            prior_status = _extract_prior_status(row.status_detail)
            row.status = prior_status
            row.status_detail = None
            row.updated_at = now
            self.db.add(row)
        if autocommit:
            self.db.commit()
        else:
            self.db.flush()
        return rows

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


# ---------------------------------------------------------------------------
# Module-level helper — extract prior_status from status_detail.
# ---------------------------------------------------------------------------

def _extract_prior_status(status_detail: str | None) -> str:
    """Parse the ``prior_status=<value>`` prefix written by
    ``set_dormant_for_admin``.  Falls back to ``'connected'`` if the
    detail is absent or malformed.

    Pattern: "prior_status=<value>; ..."
    """
    if not status_detail:
        return "connected"
    if status_detail.startswith("prior_status="):
        # Take the value up to the first ';' or end of string.
        fragment = status_detail[len("prior_status="):]
        value = fragment.split(";", 1)[0].strip()
        if value in ("unconfigured", "connected", "error", "expired"):
            return value
    return "connected"
