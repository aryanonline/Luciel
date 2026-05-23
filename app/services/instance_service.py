"""InstanceService — orchestration for V2 Admin → Instance.

Arc 5 Path A (Commit A2). Sits on top of
:class:`app.repositories.instance_repository.InstanceRepository`.

Responsibilities
----------------
1. Atomic create / deactivate with audit rows (audit context propagated
   into the repo layer; audit-in-transaction per Pattern E).
2. Cascade hook invoked when an Admin is deactivated — sweeps every
   Instance under that Admin into ``active=False`` in the same
   transaction as the Admin update.

V2 doctrine notes
-----------------
- There is no Domain layer and no Agent layer; therefore no
  ``validate_parent_scope_active`` helper, no Agent-level cascade, no
  Domain-level cascade. The only parent of an Instance is its Admin,
  and the FK enforces that.
- ``Admin.active`` is the single source of truth for "is the owning
  scope live". The route layer reads this before any call into this
  service, so the service trusts its inputs.
- Authorization (which Admin may create which Instance) lives at the
  route layer; this service trusts that authorization has already run.

Domain-agnostic: no imports from app/domain/, no vertical branching,
no hardcoded role names.
"""

from __future__ import annotations

import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.instance import Instance
from app.repositories.admin_audit_repository import AuditContext
from app.repositories.instance_repository import InstanceRepository

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Error types — service-level exceptions the route layer translates
# into HTTP responses.
# ---------------------------------------------------------------------

class InstanceServiceError(Exception):
    """Base class for service-level errors."""


class DuplicateInstanceError(InstanceServiceError):
    """Raised when the V2 ``(admin_id, instance_slug)`` unique
    constraint rejects a create. Route layer maps to 409.
    """


class InstanceNotFoundError(InstanceServiceError):
    """Raised when a target Instance can't be resolved. Route layer
    maps to 404."""


class TierScopeViolationError(InstanceServiceError):
    """Raised when an Admin has hit its ``instance_count_cap`` per the
    V2 entitlement map (or has no active subscription on a tier that
    requires one). Route layer maps to 402 Payment Required so the
    caller can distinguish "upgrade your tier" from a 403 ("this key
    is not allowed") or 400 ("payload is malformed").

    The ``reason`` attribute disambiguates the sub-conditions; the
    legacy REASON_SCOPE_NOT_PERMITTED constant survives only as a
    compatibility shim for any route that still passes it.
    """

    # Sub-conditions:
    REASON_CAP_EXCEEDED = "cap_exceeded"
    REASON_NO_ACTIVE_SUBSCRIPTION = "no_active_subscription"
    # Transitional — preserved for one release so existing route-layer
    # callsites still compile. V2 has no scope hierarchy and therefore
    # no "scope not permitted" sub-condition; new code must use
    # REASON_CAP_EXCEEDED. Removed at Arc 6.
    REASON_SCOPE_NOT_PERMITTED = "scope_not_permitted"
    REASON_DOMAIN_CAP_EXCEEDED = "domain_cap_exceeded"

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


# ---------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------

class InstanceService:
    def __init__(
        self,
        db: Session,
        *,
        admin_service=None,
    ) -> None:
        """Construct the V2 service.

        ``admin_service`` is injected (not imported) to avoid a
        circular import; some legacy callsites still wire it via
        keyword for the cascade-on-admin-deactivate hook. The service
        does not call into it at create time — Admin.active is the only
        owning-scope predicate, read directly by the route layer.
        """
        self.db = db
        self.repo = InstanceRepository(db)
        self.admin = admin_service

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create_instance(
        self,
        *,
        audit_ctx: AuditContext,
        admin_id: str,
        instance_slug: str,
        display_name: str,
        description: str | None = None,
        active: bool = True,
        created_by: str | None = None,
    ) -> Instance:
        """Create a new Instance atomically.

        Workflow:
          1. Delegate to repo with ``autocommit=False``.
          2. Commit on success; rollback on any exception.
          3. Audit row written by the repo in the same transaction
             (Pattern E — audit-in-txn).

        Raises:
          DuplicateInstanceError -> 409 when ``(admin_id, instance_slug)``
          collides with an existing row.
        """
        try:
            instance = self.repo.create(
                admin_id=admin_id,
                instance_slug=instance_slug,
                display_name=display_name,
                description=description,
                active=active,
                created_by=created_by,
                autocommit=False,
                audit_ctx=audit_ctx,
            )
            self.db.commit()
            self.db.refresh(instance)
        except IntegrityError as exc:
            self.db.rollback()
            logger.info(
                "Instance create rejected (integrity): admin_id=%s "
                "instance_slug=%s",
                admin_id,
                instance_slug,
            )
            raise DuplicateInstanceError(
                f"An Instance with instance_slug={instance_slug!r} "
                f"already exists under admin_id={admin_id!r}."
            ) from exc
        except Exception:
            self.db.rollback()
            logger.exception(
                "Instance create failed admin_id=%s instance_slug=%s",
                admin_id,
                instance_slug,
            )
            raise

        return instance

    # ------------------------------------------------------------------
    # Deactivate (single instance)
    # ------------------------------------------------------------------

    def deactivate_instance(
        self,
        *,
        audit_ctx: AuditContext,
        pk: int,
        updated_by: str | None = None,
    ) -> Instance:
        """Soft-deactivate one Instance by PK.

        Authorization (the caller's Admin owns this Instance) is the
        route layer's responsibility. This method only enforces
        existence.

        Raises InstanceNotFoundError -> 404 when no row exists.
        """
        instance = self.repo.deactivate_by_pk(
            pk,
            updated_by=updated_by,
            audit_ctx=audit_ctx,
        )
        if instance is None:
            raise InstanceNotFoundError(
                f"Instance pk={pk} not found."
            )
        return instance

    # ------------------------------------------------------------------
    # Cascade hook — invoked when an Admin is deactivated
    # ------------------------------------------------------------------

    def cascade_on_admin_deactivate(
        self,
        *,
        audit_ctx: AuditContext,
        admin_id: str,
        updated_by: str | None = None,
    ) -> int:
        """Deactivate every Instance owned by the Admin.

        Called by :meth:`AdminService.deactivate_tenant_with_cascade`
        in the same transaction as the Admin update. Returns the
        number of Instances deactivated.

        Writes ONE audit row for the cascade event (implemented at the
        repo layer; one row, not one per Instance).
        """
        count = self.repo.deactivate_all_for_admin(
            admin_id=admin_id,
            updated_by=updated_by,
            audit_ctx=audit_ctx,
            autocommit=False,
        )
        logger.info(
            "Cascade on admin deactivate: count=%d admin_id=%s",
            count,
            admin_id,
        )
        return count

    # ------------------------------------------------------------------
    # Convenience reads (no authorization — route layer enforces it)
    # ------------------------------------------------------------------

    def get_by_pk(self, pk: int) -> Instance | None:
        return self.repo.get_by_pk(pk)

    def list_for_admin(
        self,
        *,
        admin_id: str,
        active_only: bool = False,
    ) -> list[Instance]:
        return self.repo.list_for_admin(
            admin_id=admin_id,
            active_only=active_only,
        )
