"""Instance repository — data-access layer for V2 Admin → Instance rows.

Arc 5 Path A (Commit A1). Wraps :class:`app.models.instance.Instance`.

The V2 doctrine is Admin → Instance → Lead. There is no Domain layer
and no Agent layer. Every Instance hangs off exactly one Admin via the
RESTRICT-on-delete ``admin_id`` foreign key. The legacy three-level
``scope_level`` / ``scope_owner_tenant_id`` / ``scope_owner_domain_id``
/ ``scope_owner_agent_id`` quadruple is gone — every public method on
this class accepts ``admin_id: str`` and no other scope dimension.

Scope of responsibility:
- Pure CRUD. No HTTP exceptions, no business-logic validation beyond
  what SQLAlchemy and Postgres CHECK / FK constraints enforce.
- Uniqueness invariants live on the ``Instance`` model itself
  (``UNIQUE(admin_id, instance_slug)``).
- Authorization is the route layer's job (``Admin`` is resolved from
  the JWT before any call into this repo).
- The legacy ``validate_parent_scope_active`` helper is GONE — V2 has
  no parent-scope concept beyond the Admin FK, and Admin.active is a
  flat column the service layer reads directly.

Domain-agnostic: no imports from app/domain/, no vertical branching.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.admin_audit_log import (
    ACTION_CASCADE_DEACTIVATE,
    ACTION_CREATE,
    ACTION_DEACTIVATE,
    ACTION_UPDATE,
    RESOURCE_INSTANCE,
)
from app.models.instance import Instance
from app.repositories.admin_audit_repository import (
    AdminAuditRepository,
    AuditContext,
    diff_updated_fields,
)

logger = logging.getLogger(__name__)


class InstanceRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create(
        self,
        *,
        admin_id: str,
        instance_slug: str,
        display_name: str,
        description: str | None = None,
        active: bool = True,
        created_by: str | None = None,
        autocommit: bool = True,
        audit_ctx: AuditContext | None = None,
    ) -> Instance:
        """Insert a new Instance row.

        Uniqueness invariants (``UNIQUE(admin_id, instance_slug)``) and
        the FK to ``admins.id`` are enforced at the DB.

        ``autocommit=False`` lets the tier-provisioning service compose
        this into its onboard transaction.

        ``audit_ctx``, when provided, writes an ``admin_audit_logs`` row
        in the same transaction (Pattern E — audit-in-txn).
        """
        instance = Instance(
            admin_id=admin_id,
            instance_slug=instance_slug,
            display_name=display_name,
            description=description,
            active=active,
        )
        self.db.add(instance)
        self.db.flush()  # assigns instance.id

        if audit_ctx is not None:
            AdminAuditRepository(self.db).record(
                ctx=audit_ctx,
                tenant_id=admin_id,
                action=ACTION_CREATE,
                resource_type=RESOURCE_INSTANCE,
                resource_pk=instance.id,
                resource_natural_id=instance_slug,
                after={
                    "admin_id": admin_id,
                    "instance_slug": instance_slug,
                    "display_name": display_name,
                    "description": description,
                    "active": active,
                },
                autocommit=False,
            )

        if autocommit:
            self.db.commit()
            self.db.refresh(instance)
        logger.info(
            "Instance created admin_id=%s instance_slug=%s pk=%s",
            admin_id,
            instance_slug,
            instance.id,
        )
        return instance

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_by_pk(self, pk: int) -> Instance | None:
        return (
            self.db.query(Instance)
            .filter(Instance.id == pk)
            .first()
        )

    def get_by_admin_and_slug(
        self,
        *,
        admin_id: str,
        instance_slug: str,
    ) -> Instance | None:
        """Fetch by V2 natural key ``(admin_id, instance_slug)``."""
        return (
            self.db.query(Instance)
            .filter(
                Instance.admin_id == admin_id,
                Instance.instance_slug == instance_slug,
            )
            .first()
        )

    def list_for_admin(
        self,
        *,
        admin_id: str,
        active_only: bool = False,
    ) -> list[Instance]:
        """List every Instance owned by an Admin.

        V2 has no scope hierarchy below the Admin, so a single
        ``admin_id`` predicate is the full filter surface.
        """
        query = self.db.query(Instance).filter(Instance.admin_id == admin_id)
        if active_only:
            query = query.filter(Instance.active.is_(True))
        return query.order_by(Instance.id.asc()).all()

    def count_active_for_admin(self, admin_id: str) -> int:
        """Return the number of ACTIVE Instances under an Admin.

        Used by the V2 cap-enforcement guard
        (:func:`app.policy.entitlements.resolve_entitlement` ``axis=
        "instance_count_cap"``). Inactive rows are excluded so a
        deactivate frees a slot.
        """
        return (
            self.db.query(Instance)
            .filter(
                Instance.admin_id == admin_id,
                Instance.active.is_(True),
            )
            .count()
        )

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    # Identity columns (``id``, ``admin_id``, ``instance_slug``) are
    # deliberately not updatable. Moving an Instance across Admins
    # would break knowledge ownership, chat-key bindings, and audit
    # trails. Deactivate + recreate instead.
    _UPDATABLE_FIELDS = frozenset(
        {
            "display_name",
            "description",
            "active",
        }
    )

    def update(
        self,
        instance: Instance,
        *,
        audit_ctx: AuditContext | None = None,
        **fields,
    ) -> Instance:
        """Apply field updates to an existing Instance.

        Silently ignores any field not in :attr:`_UPDATABLE_FIELDS` so
        identity columns are unreachable via PATCH. Writes an audit
        row containing only the fields that actually changed.
        """
        before_snapshot = {
            key: getattr(instance, key) for key in self._UPDATABLE_FIELDS
        }

        applied: dict[str, object] = {}
        for key, value in fields.items():
            if key in self._UPDATABLE_FIELDS and value is not None:
                setattr(instance, key, value)
                applied[key] = value

        after_snapshot = {
            key: getattr(instance, key) for key in self._UPDATABLE_FIELDS
        }

        if audit_ctx is not None and applied:
            before_diff, after_diff = diff_updated_fields(
                before_snapshot, after_snapshot
            )
            if before_diff or after_diff:
                AdminAuditRepository(self.db).record(
                    ctx=audit_ctx,
                    tenant_id=instance.admin_id,
                    action=ACTION_UPDATE,
                    resource_type=RESOURCE_INSTANCE,
                    resource_pk=instance.id,
                    resource_natural_id=instance.instance_slug,
                    before=before_diff,
                    after=after_diff,
                    autocommit=False,
                )

        self.db.commit()
        self.db.refresh(instance)
        if applied:
            logger.info(
                "Instance updated pk=%s fields=%s",
                instance.id,
                sorted(applied.keys()),
            )
        return instance

    # ------------------------------------------------------------------
    # Deactivate (soft delete)
    # ------------------------------------------------------------------

    def deactivate_by_pk(
        self,
        pk: int,
        *,
        updated_by: str | None = None,
        audit_ctx: AuditContext | None = None,
    ) -> Instance | None:
        """Soft-deactivate a specific Instance. Returns ``None`` if not found."""
        instance = self.get_by_pk(pk)
        if instance is None:
            return None

        was_active = bool(instance.active)
        instance.active = False

        if audit_ctx is not None and was_active:
            AdminAuditRepository(self.db).record(
                ctx=audit_ctx,
                tenant_id=instance.admin_id,
                action=ACTION_DEACTIVATE,
                resource_type=RESOURCE_INSTANCE,
                resource_pk=instance.id,
                resource_natural_id=instance.instance_slug,
                before={"active": True},
                after={"active": False},
                autocommit=False,
            )

        self.db.commit()
        self.db.refresh(instance)
        logger.info(
            "Instance deactivated pk=%s admin_id=%s instance_slug=%s",
            instance.id,
            instance.admin_id,
            instance.instance_slug,
        )
        return instance

    def deactivate_all_for_admin(
        self,
        *,
        admin_id: str,
        updated_by: str | None = None,
        audit_ctx: AuditContext | None = None,
        autocommit: bool = True,
    ) -> int:
        """Cascade: deactivate every Instance owned by an Admin.

        V2 collapses the legacy per-tenant / per-domain / per-agent
        cascade helpers into a single per-Admin sweep — there is no
        scope hierarchy below the Admin.

        Returns the number of rows updated. Writes one audit row for
        the cascade event (only when ``updated > 0``, matching the
        pre-collapse ``deactivate_all_for_tenant`` pattern).

        ``autocommit=False`` allows the Admin-deactivation spine in
        :class:`app.services.admin_service.AdminService` to compose
        the whole cascade into a single transaction.
        """
        affected = (
            self.db.query(Instance.id, Instance.instance_slug)
            .filter(
                Instance.admin_id == admin_id,
                Instance.active.is_(True),
            )
            .all()
        )
        affected_pks = [pk for pk, _ in affected]
        affected_slugs = [slug for _, slug in affected]

        updated = (
            self.db.query(Instance)
            .filter(
                Instance.admin_id == admin_id,
                Instance.active.is_(True),
            )
            .update(
                {Instance.active: False},
                synchronize_session=False,
            )
        )

        if audit_ctx is not None and updated:
            AdminAuditRepository(self.db).record(
                ctx=audit_ctx,
                tenant_id=admin_id,
                action=ACTION_CASCADE_DEACTIVATE,
                resource_type=RESOURCE_INSTANCE,
                resource_pk=None,
                resource_natural_id=None,
                after={
                    "count": int(updated),
                    "affected_pks": affected_pks,
                    "affected_instance_slugs": affected_slugs,
                    "trigger": "admin_deactivate",
                },
                note=f"Cascade from admin {admin_id} deactivation",
                autocommit=False,
            )

        if autocommit:
            self.db.commit()
        logger.info(
            "Instance cascade-deactivated count=%d admin_id=%s",
            updated,
            admin_id,
        )
        return int(updated)
