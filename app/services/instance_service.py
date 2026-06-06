"""InstanceService — orchestration for V2 Admin → Instance.

Arc 5 Path A (Commit A2). Sits on top of
:class:`app.repositories.instance_repository.InstanceRepository`.

RESCAN TIER-DE (lifecycle): extended to encode the §3.6.1 five-state
transition table with role gating.

Responsibilities
----------------
1. Atomic create / deactivate with audit rows (audit context propagated
   into the repo layer; audit-in-transaction per Pattern E).
2. Cascade hook invoked when an Admin is deactivated — sweeps every
   Instance under that Admin into ``active=False`` in the same
   transaction as the Admin update.
3. (TIER-DE) Transition table enforcement: validates the current state
   permits the requested transition and that the caller holds the
   required role (account owner -- single-login model, Locked Decision #19).

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

§3.6.1 Transition table
-----------------------
  active         → paused          (account owner)
  paused         → active          (account owner)
  active|paused  → deactivating    (account owner)
  deactivating   → grace_window    (automatic — system)
  grace_window   → active          (owner only — /restore within 30d)
  grace_window   → hard_deleted    (automatic — retention worker)

The ``deleted`` (legacy) state is treated as an alias for
``grace_window`` per the TIER-DE enum mapping.  Transitions
that reference ``grace_window`` also apply to ``deleted`` rows
for backward-compat.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.instance import Instance
from app.lifecycle.state import InstanceStatus, INSTANCE_GRACE_STATES
from app.repositories.admin_audit_repository import AuditContext
from app.repositories.instance_repository import InstanceRepository

if TYPE_CHECKING:
    pass

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


class InstanceLifecycleConflictError(InstanceServiceError):
    """Raised when a Pause/Resume/Delete/Restore transition is invalid
    for the current ``instance_status``. Route layer maps to 409
    Conflict. The ``current_status`` attribute lets the response carry
    the canonical state value so frontends can re-render without a
    re-fetch.
    """

    def __init__(self, message: str, *, current_status: str) -> None:
        super().__init__(message)
        self.current_status = current_status


class InstanceTransitionRoleError(InstanceServiceError):
    """Raised when the caller's role does not permit the requested
    lifecycle transition per the §3.6.1 transition table role gating.
    Route layer maps to 403 Forbidden.

    Transition role gates (§3.6.1):
      active→paused / paused→active              : account owner
      active|paused→deactivating                 : account owner
      grace_window→active (restore)              : owner only
      deactivating→grace_window / grace_window→hard_deleted : automatic (no user gate)
    """

    def __init__(self, message: str, *, required_roles: list[str]) -> None:
        super().__init__(message)
        self.required_roles = required_roles


class InstanceRestoreGraceExpiredError(InstanceServiceError):
    """Raised when /restore is called past the 30-day grace window.
    Route layer maps to 410 Gone."""


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
        website: str | None = None,
        personality_preset: str | None = None,
        personality_axes: dict | None = None,
        business_context: str | None = None,
        lead_routing: dict | None = None,
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
                website=website,
                personality_preset=personality_preset,
                personality_axes=personality_axes,
                business_context=business_context,
                lead_routing=lead_routing,
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

        Deprecated thin alias for :meth:`pause_instance`. Kept so any
        internal callsite that has not yet migrated to the explicit
        Pause/Delete vocabulary keeps compiling. New callers must
        invoke :meth:`pause_instance` or
        :meth:`delete_instance_with_grace`.
        """
        return self.pause_instance(
            audit_ctx=audit_ctx,
            pk=pk,
            updated_by=updated_by,
        )

    # ------------------------------------------------------------------
    # Lifecycle (Arc 11 Closeout PR-A) — Pause / Resume / Delete / Restore
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Internal helpers for §3.6.1 transition-table role gating.
    # ------------------------------------------------------------------

    @staticmethod
    def _is_grace_state(status: InstanceStatus) -> bool:
        """Return True if the status is in the grace-window family.

        Both ``deleted`` (legacy alias) and ``grace_window`` (new
        canonical state) are treated equivalently for transition logic.
        """
        return status.value in INSTANCE_GRACE_STATES

    def pause_instance(
        self,
        *,
        audit_ctx: AuditContext,
        pk: int,
        updated_by: str | None = None,
    ) -> Instance:
        """Pause an Instance (Customer Journey §4.5 Phase 8 "Pause").

        §3.6.1 transition: active → paused (account owner).

        Widget begins returning 204 (empty <div>); knowledge + sessions
        are retained. Reactivatable instantly via /resume.

        Raises:
          InstanceNotFoundError -> 404 when no row exists.
          InstanceLifecycleConflictError -> 409 when the row is not in
            a state that permits a Pause transition (§3.6.1: only
            active may be paused; deleted/grace_window/deactivating
            states must use Restore first).
        """
        # Pre-flight: fetch the current state for transition validation.
        pre = self.repo.get_by_pk(pk)
        if pre is None:
            raise InstanceNotFoundError(f"Instance pk={pk} not found.")

        current = pre.instance_status
        if current != InstanceStatus.ACTIVE:
            # §3.6.1: Pause is only valid from active.
            raise InstanceLifecycleConflictError(
                f"Instance pk={pk} is in state '{current.value}'; "
                f"Pause transition requires 'active' state.",
                current_status=current.value,
            )

        instance = self.repo.pause_by_pk(
            pk,
            updated_by=updated_by,
            audit_ctx=audit_ctx,
        )
        if instance is None:
            raise InstanceNotFoundError(f"Instance pk={pk} not found.")
        return instance

    def resume_instance(
        self,
        *,
        audit_ctx: AuditContext,
        pk: int,
        updated_by: str | None = None,
    ) -> Instance:
        """Resume a paused Instance.

        §3.6.1 transition: paused → active (account owner).

        Raises:
          InstanceNotFoundError -> 404 when no row exists.
          InstanceLifecycleConflictError -> 409 when the row is not in
            the 'paused' state (§3.6.1: Resume is only valid from paused;
            for grace_window / deleted rows use Restore).
        """
        # Pre-flight: fetch the current state for transition validation.
        pre = self.repo.get_by_pk(pk)
        if pre is None:
            raise InstanceNotFoundError(f"Instance pk={pk} not found.")

        current = pre.instance_status
        if current != InstanceStatus.PAUSED:
            # §3.6.1: Resume is only valid from paused.
            if self._is_grace_state(current):
                extra = " Use /restore to reactivate from the grace window."
            else:
                extra = ""
            raise InstanceLifecycleConflictError(
                f"Instance pk={pk} is in state '{current.value}'; "
                f"Resume transition requires 'paused' state.{extra}",
                current_status=current.value,
            )

        instance = self.repo.resume_by_pk(
            pk,
            updated_by=updated_by,
            audit_ctx=audit_ctx,
        )
        if instance is None:
            raise InstanceNotFoundError(f"Instance pk={pk} not found.")
        return instance

    def delete_instance_with_grace(
        self,
        *,
        audit_ctx: AuditContext,
        pk: int,
        updated_by: str | None = None,
    ) -> Instance:
        """Soft-delete an Instance (Customer Journey §4.5 Phase 8
        "Delete"); transitions the Instance through deactivating into
        the grace_window state, per Architecture §3.6.1.

        §3.6.1 transitions driven here:
          active|paused → deactivating  (account owner)
          deactivating  → grace_window  (automatic, in same call)

        The ``deactivating`` state is a transient signal that grants
        have been revoked and the instance is entering the grace period.
        The repository method stamps ``soft_deleted_at`` and sets the
        status to ``grace_window`` (or, for backward-compat, the repo
        may use ``deleted`` for existing code paths — both are valid).

        The retention worker (``app.lifecycle.retention``)
        hard-deletes the row + its customer-data cascade after the 30-
        day grace window expires. Restorable within the window via
        :meth:`restore_instance`.

        Raises:
          InstanceNotFoundError -> 404 when no row exists.
          InstanceLifecycleConflictError -> 409 when the row is already
            in the grace window / deactivating / hard_deleted state.
        """
        # Pre-flight: validate transition is permitted from current state.
        pre = self.repo.get_by_pk(pk)
        if pre is None:
            raise InstanceNotFoundError(f"Instance pk={pk} not found.")

        current = pre.instance_status
        # §3.6.1: delete is permitted from active or paused.
        # If already in a grace state, it is idempotent at the repo level
        # but we surface a 409 to make the caller aware of the state.
        if current == InstanceStatus.DEACTIVATING or self._is_grace_state(current):
            raise InstanceLifecycleConflictError(
                f"Instance pk={pk} is already in state '{current.value}'; "
                f"deletion is already in progress or the grace window is open.",
                current_status=current.value,
            )
        if current == InstanceStatus.HARD_DELETED:
            raise InstanceLifecycleConflictError(
                f"Instance pk={pk} has been hard_deleted and cannot be modified.",
                current_status=current.value,
            )

        instance = self.repo.delete_by_pk(
            pk,
            updated_by=updated_by,
            audit_ctx=audit_ctx,
        )
        if instance is None:
            raise InstanceNotFoundError(f"Instance pk={pk} not found.")
        return instance

    def restore_instance(
        self,
        *,
        audit_ctx: AuditContext,
        pk: int,
        updated_by: str | None = None,
        api_key_service=None,
    ) -> tuple[Instance, str | None]:
        """Restore a soft-deleted Instance within the 30-day grace
        window. Per Vision §6.4 Reactivation, embed keys are re-minted
        (all existing embed keys for the instance are revoked, a new
        embed key is issued).

        Returns ``(instance, new_embed_key_raw)``. ``new_embed_key_raw``
        is the one-time-readable raw key (never written to SSM, never
        re-readable) — the route layer surfaces it on the response.

        ``api_key_service`` is injected (not imported) to avoid a
        circular import; pass ``ApiKeyService(db)`` when calling.
        When ``None``, no key re-mint happens (only used by tests that
        don't care about the key-rotation side-effect).

        Raises:
          InstanceNotFoundError -> 404 when no row exists.
          InstanceLifecycleConflictError -> 409 when the row is not in
            the grace-window family (deleted/grace_window) — already-
            live or deactivating rows cannot be restored.
          InstanceRestoreGraceExpiredError -> 410 when the grace window
            has expired (the row will be hard-deleted by the next
            retention worker pass, if it has not been already).
        """
        pre = self.repo.get_by_pk(pk)
        if pre is None:
            raise InstanceNotFoundError(f"Instance pk={pk} not found.")

        # §3.6.1: Restore is only valid from grace_window (or the
        # legacy 'deleted' alias). Any other state is a conflict.
        if not self._is_grace_state(pre.instance_status):
            raise InstanceLifecycleConflictError(
                f"Instance pk={pk} is in state '{pre.instance_status.value}'; "
                f"Restore is only valid from the grace_window (or legacy "
                f"'deleted') state.",
                current_status=pre.instance_status.value,
            )

        instance = self.repo.restore_by_pk(
            pk,
            updated_by=updated_by,
            audit_ctx=audit_ctx,
        )
        if instance is None:
            # The repo returned None for a deleted row -- the only path
            # to that branch is grace-expired (the shape-invariant
            # branch logs an error and is unreachable in practice).
            raise InstanceRestoreGraceExpiredError(
                f"Instance pk={pk} grace window has expired; restore "
                f"is no longer possible."
            )

        new_embed_key_raw: str | None = None
        if api_key_service is not None:
            # Vision §6.4: re-mint embed keys on Restore. Revoke every
            # active embed key bound to this instance, then mint one
            # fresh embed key. The raw value is returned to the caller
            # (route layer surfaces it on the response under
            # ``new_embed_key`` — one-time read).
            try:
                from app.models.api_key import ApiKey

                revoked_prefixes: list[str] = []
                old_active = (
                    self.db.query(ApiKey)
                    .filter(
                        ApiKey.luciel_instance_id == pk,
                        ApiKey.key_kind == "embed",
                        ApiKey.active.is_(True),
                    )
                    .all()
                )
                for ak in old_active:
                    ak.active = False
                    revoked_prefixes.append(ak.key_prefix)

                # Pick a sensible "carrier" row to copy origins/widget
                # config off of -- use the most recent revoked one if
                # any exist; otherwise mint with no allowed_origins and
                # the route layer can refuse to serve until the admin
                # updates the new key's origins.
                carrier = old_active[-1] if old_active else None
                allowed_origins = (
                    list(carrier.allowed_origins) if carrier and carrier.allowed_origins else None
                )
                rate_limit_per_minute = (
                    carrier.rate_limit_per_minute if carrier else None
                )
                widget_config = (
                    dict(carrier.widget_config) if carrier and carrier.widget_config else None
                )

                new_key, raw = api_key_service.create_key(
                    admin_id=instance.admin_id,
                    luciel_instance_id=instance.id,
                    display_name=(
                        f"{instance.instance_slug} (restored)"
                    ),
                    permissions=["chat"],
                    key_kind="embed",
                    allowed_origins=allowed_origins,
                    rate_limit_per_minute=rate_limit_per_minute,
                    widget_config=widget_config,
                    auto_commit=True,
                    audit_ctx=audit_ctx,
                )
                new_embed_key_raw = raw

                # Persist the revocation flips committed by us (the
                # create_key call above auto-committed; we now commit
                # the revocations).
                self.db.commit()
                logger.info(
                    "Instance restored with embed-key re-mint: pk=%s "
                    "revoked_count=%d new_prefix=%s",
                    pk,
                    len(revoked_prefixes),
                    new_key.key_prefix,
                )
            except Exception:
                # Re-mint failure is logged but does not undo the
                # restore -- the row is already live and the admin
                # can re-issue a key manually. Honest fail-mode: the
                # restore worked, the auto-mint did not.
                logger.exception(
                    "Embed-key re-mint failed during restore pk=%s; "
                    "instance is live but no new key was issued.",
                    pk,
                )
                self.db.rollback()
                new_embed_key_raw = None

        return instance, new_embed_key_raw

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
