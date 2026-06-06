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
import warnings
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models.admin_audit_log import (
    ACTION_CASCADE_DEACTIVATE,
    ACTION_CONNECTION_REVOKED,
    ACTION_CREATE,
    ACTION_DEACTIVATE,
    ACTION_INSTANCE_DELETED,
    ACTION_INSTANCE_PAUSED,
    ACTION_INSTANCE_RESTORED,
    ACTION_INSTANCE_RESUMED,
    ACTION_UPDATE,
    RESOURCE_INSTANCE,
    RESOURCE_INSTANCE_CONNECTION,
)
from app.models.instance import Instance
from app.models.instance_status import InstanceStatus, INSTANCE_GRACE_STATES
from app.repositories.admin_audit_repository import (
    AdminAuditRepository,
    AuditContext,
    diff_updated_fields,
)

logger = logging.getLogger(__name__)


# Arc 11 Closeout PR-A — restore-grace window per Architecture §3.6.1
# ("soft-delete window measured from soft_deleted_at (locked)") and
# Vision §6.4 Reactivation ("Admin clicks Reactivate within 30 days").
# Single source of truth; the retention worker imports this constant
# from here too so both ends of the lifecycle clock agree.
INSTANCE_RESTORE_GRACE_DAYS = 30


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
        website: str | None = None,
        personality_preset: str | None = None,
        personality_axes: dict | None = None,
        business_context: str | None = None,
        lead_routing: dict | None = None,
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
        instance_kwargs: dict[str, object] = dict(
            admin_id=admin_id,
            instance_slug=instance_slug,
            display_name=display_name,
            description=description,
            active=active,
            website=website,
            personality_axes=personality_axes,
            business_context=business_context,
            lead_routing=lead_routing,
        )
        # Let the DB server_default (warm_concierge) stand when the
        # caller did not specify a preset, rather than forcing NULL.
        if personality_preset is not None:
            instance_kwargs["personality_preset"] = personality_preset
        instance = Instance(**instance_kwargs)
        self.db.add(instance)
        self.db.flush()  # assigns instance.id

        if audit_ctx is not None:
            # Arc 9.1 Phase A made admin_audit_logs.luciel_instance_id
            # NOT NULL. The instance we just flushed is exactly the
            # row this audit entry is about, so wire instance.id
            # through as the luciel_instance_id field. Without this
            # the audit row INSERT fails with NotNullViolation and
            # the whole transaction rolls back, surfacing as a
            # confusing 409 DuplicateInstanceError at the HTTP layer.
            AdminAuditRepository(self.db).record(
                ctx=audit_ctx,
                admin_id=admin_id,
                action=ACTION_CREATE,
                resource_type=RESOURCE_INSTANCE,
                resource_pk=instance.id,
                resource_natural_id=instance_slug,
                luciel_instance_id=instance.id,
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
            # Arc 15 WU1 — instance configuration pillars. Tier-conditional
            # validation (custom preset, business_context length,
            # lead_routing presence) happens at the API layer BEFORE the
            # update call; this repo only persists the validated values.
            "website",
            "personality_preset",
            "personality_axes",
            "business_context",
            "lead_routing",
            # Arc 15 WU3 — escalation contact + routing config.
            "escalation_config",
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
                    admin_id=instance.admin_id,
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

    def pause_by_pk(
        self,
        pk: int,
        *,
        updated_by: str | None = None,
        audit_ctx: AuditContext | None = None,
    ) -> Instance | None:
        """Pause a specific Instance (Customer Journey §4.5 Phase 8).

        Sets ``instance_status='paused'`` and ``active=False`` (the
        legacy mirror). Writes an ``ACTION_INSTANCE_PAUSED`` audit row
        in the same transaction. Returns ``None`` if not found; returns
        the row unchanged with no audit emission if already ``deleted``
        (operational pause is not a valid transition out of the
        destructive-intent state — the route layer maps this to 409).
        """
        instance = self.get_by_pk(pk)
        if instance is None:
            return None
        if instance.instance_status.value in INSTANCE_GRACE_STATES:
            # No-op signal — route layer maps this to 409. Caller can
            # distinguish "already paused" (idempotent) from a grace-window
            # instance (conflict) by reading instance.instance_status off
            # the returned row. (Accepts grace_window + legacy 'deleted'.)
            return instance

        before_status = instance.instance_status
        was_active = bool(instance.active)
        instance.instance_status = InstanceStatus.PAUSED
        instance.active = False

        if audit_ctx is not None and (
            before_status != InstanceStatus.PAUSED or was_active
        ):
            AdminAuditRepository(self.db).record(
                ctx=audit_ctx,
                admin_id=instance.admin_id,
                action=ACTION_INSTANCE_PAUSED,
                resource_type=RESOURCE_INSTANCE,
                resource_pk=instance.id,
                resource_natural_id=instance.instance_slug,
                luciel_instance_id=instance.id,
                before={
                    "instance_status": before_status.value,
                    "active": was_active,
                },
                after={
                    "instance_status": InstanceStatus.PAUSED.value,
                    "active": False,
                },
                autocommit=False,
            )

        self.db.commit()
        self.db.refresh(instance)
        logger.info(
            "Instance paused pk=%s admin_id=%s instance_slug=%s",
            instance.id,
            instance.admin_id,
            instance.instance_slug,
        )
        return instance

    def resume_by_pk(
        self,
        pk: int,
        *,
        updated_by: str | None = None,
        audit_ctx: AuditContext | None = None,
    ) -> Instance | None:
        """Resume a paused Instance.

        Sets ``instance_status='active'`` and ``active=True``. Writes
        an ``ACTION_INSTANCE_RESUMED`` audit row. Returns ``None`` if
        not found; returns the row unchanged with no audit emission
        if already ``deleted`` (route layer maps to 409). Resuming an
        already-active row is idempotent and emits no audit (matches
        the no-diff convention in :meth:`update`).
        """
        instance = self.get_by_pk(pk)
        if instance is None:
            return None
        if instance.instance_status.value in INSTANCE_GRACE_STATES:
            return instance

        before_status = instance.instance_status
        was_active = bool(instance.active)
        instance.instance_status = InstanceStatus.ACTIVE
        instance.active = True

        if audit_ctx is not None and (
            before_status != InstanceStatus.ACTIVE or not was_active
        ):
            AdminAuditRepository(self.db).record(
                ctx=audit_ctx,
                admin_id=instance.admin_id,
                action=ACTION_INSTANCE_RESUMED,
                resource_type=RESOURCE_INSTANCE,
                resource_pk=instance.id,
                resource_natural_id=instance.instance_slug,
                luciel_instance_id=instance.id,
                before={
                    "instance_status": before_status.value,
                    "active": was_active,
                },
                after={
                    "instance_status": InstanceStatus.ACTIVE.value,
                    "active": True,
                },
                autocommit=False,
            )

        self.db.commit()
        self.db.refresh(instance)
        logger.info(
            "Instance resumed pk=%s admin_id=%s instance_slug=%s",
            instance.id,
            instance.admin_id,
            instance.instance_slug,
        )
        return instance

    def delete_by_pk(
        self,
        pk: int,
        *,
        updated_by: str | None = None,
        audit_ctx: AuditContext | None = None,
    ) -> Instance | None:
        """Soft-delete an Instance — opens the 30-day grace window.

        Sets ``instance_status='deleted'``, ``active=False``, and
        stamps ``soft_deleted_at = now()``. Writes an
        ``ACTION_INSTANCE_DELETED`` audit row.

        Arc 12 WU4 — sibling-grant cascade (Architecture §3.6.1 step
        3): in the same transaction as the soft-delete, revoke every
        non-revoked ``sibling_call_grants`` row where this Instance
        appears as caller OR callee. Each revocation emits its own
        ``ACTION_SIBLING_GRANT_REVOKED`` audit row carrying the
        cascade source. The cascade runs only when ``audit_ctx`` is
        provided (test paths without an audit context skip it, same
        as the instance-level audit row).

        Idempotent on an already-deleted row: returns the row without
        re-stamping ``soft_deleted_at`` (the original delete moment is
        the only honest grace-window start) and without emitting a
        second audit row. Returns ``None`` if not found.
        """
        instance = self.get_by_pk(pk)
        if instance is None:
            return None
        if instance.instance_status.value in INSTANCE_GRACE_STATES:
            # Idempotent — already in the grace window (grace_window, or a
            # legacy 'deleted' row). Preserve the original soft_deleted_at
            # clock.
            return instance

        before_status = instance.instance_status
        was_active = bool(instance.active)
        now = datetime.now(timezone.utc)

        # 5-state machine (Architecture §3.6.1): deactivation lands the
        # instance in ``grace_window`` (the 30-day soft-delete state),
        # NOT the legacy 3-state ``deleted`` alias. ``soft_deleted_at`` is
        # the grace clock the retention worker reads.
        instance.instance_status = InstanceStatus.GRACE_WINDOW
        instance.active = False
        instance.soft_deleted_at = now

        if audit_ctx is not None:
            AdminAuditRepository(self.db).record(
                ctx=audit_ctx,
                admin_id=instance.admin_id,
                action=ACTION_INSTANCE_DELETED,
                resource_type=RESOURCE_INSTANCE,
                resource_pk=instance.id,
                resource_natural_id=instance.instance_slug,
                luciel_instance_id=instance.id,
                before={
                    "instance_status": before_status.value,
                    "active": was_active,
                    "soft_deleted_at": None,
                },
                after={
                    "instance_status": InstanceStatus.GRACE_WINDOW.value,
                    "active": False,
                    "soft_deleted_at": now.isoformat(),
                    "grace_window_days": INSTANCE_RESTORE_GRACE_DAYS,
                },
                autocommit=False,
            )

            # Sibling-call-grant cascade removed in Unit 1: the
            # call_sibling_luciel tool and sibling_call_grants table are
            # deferred-feature surfaces (multi-Luciel, Open Decision #7)
            # excised in this unit. The single-Luciel model has no
            # sibling grants to revoke on instance delete.

            # Arc 17 Task 4 — connection cascade. In the same transaction
            # as the soft-delete, revoke every live instance_connections
            # row for this instance, audit each, and enqueue secret
            # cleanup for any non-null credential_ref. Mirrors the
            # sibling-grant cascade above (lazy import to avoid a cycle).
            self._cascade_revoke_connections(
                admin_id=instance.admin_id,
                instance_id=instance.id,
                audit_ctx=audit_ctx,
            )

        self.db.commit()
        self.db.refresh(instance)
        logger.info(
            "Instance soft-deleted pk=%s admin_id=%s instance_slug=%s "
            "soft_deleted_at=%s",
            instance.id,
            instance.admin_id,
            instance.instance_slug,
            now.isoformat(),
        )
        return instance

    # ------------------------------------------------------------------
    # Arc 17 Task 4 — connection cascade (instance delete + account
    # closure). Revoke live instance_connections rows, audit each, and
    # enqueue secret cleanup for any non-null credential_ref. Shared by
    # delete_by_pk (one instance) and the closure path (admin-wide).
    # ------------------------------------------------------------------

    def cascade_revoke_connections_for_admin(
        self,
        *,
        admin_id: str,
        audit_ctx: AuditContext,
    ) -> int:
        """Public entry for the account-closure destructive cascade.

        Revokes EVERY live connection across the admin's instances +
        enqueues secret cleanup, all in the caller's transaction. Called
        by :class:`app.services.closure_service.ClosureService` — the
        destructive-intent path. Operational admin-deactivation (Pause)
        does NOT call this: paused instances retain their connection
        rows (data-retained semantics).
        """
        return self._cascade_revoke_connections(
            admin_id=admin_id,
            audit_ctx=audit_ctx,
            instance_id=None,
        )

    def _cascade_revoke_connections(
        self,
        *,
        admin_id: str,
        audit_ctx: AuditContext,
        instance_id: int | None = None,
    ) -> int:
        """Soft-revoke connections + enqueue secret cleanup, in-txn.

        Scope: a single instance when ``instance_id`` is given, else
        every connection across the admin's instances (account closure).
        Returns the number of rows revoked. Imports lazily to avoid a
        circular import (the connection repo / outbox model import back
        into this module's model graph).
        """
        from app.repositories.instance_connection_repository import (
            InstanceConnectionRepository,
        )
        from app.repositories.secret_cleanup_outbox_repository import (
            SecretCleanupOutboxRepository,
        )

        conn_repo = InstanceConnectionRepository(self.db)
        if instance_id is not None:
            revoked = conn_repo.revoke_all_for_instance(
                admin_id=admin_id,
                instance_id=instance_id,
                autocommit=False,
            )
        else:
            revoked = conn_repo.revoke_all_for_admin(
                admin_id=admin_id,
                autocommit=False,
            )
        if not revoked:
            return 0

        audit_repo = AdminAuditRepository(self.db)
        outbox = SecretCleanupOutboxRepository(self.db)
        for conn in revoked:
            audit_repo.record(
                ctx=audit_ctx,
                admin_id=admin_id,
                action=ACTION_CONNECTION_REVOKED,
                resource_type=RESOURCE_INSTANCE_CONNECTION,
                resource_pk=conn.id,
                resource_natural_id=f"{conn.instance_id}:{conn.connection_type}",
                luciel_instance_id=conn.instance_id,
                before={
                    "connection_type": conn.connection_type,
                    "status": conn.status,
                },
                after={"revoked": True},
                note=(
                    "Connection revoked by lifecycle cascade "
                    f"({'instance_delete' if instance_id is not None else 'account_closure'})."
                ),
                autocommit=False,
            )
            # Enqueue secret cleanup ONLY when a real secret pointer
            # exists. credential_ref is NULL in this slice (no live
            # credential-bearing connectors), so this is a no-op today —
            # but the real enqueue path is exercised when full Arc 17
            # populates credential_ref. We enqueue the POINTER only,
            # never a secret value (Locked Decision #18).
            if conn.credential_ref:
                outbox.enqueue(
                    admin_id=admin_id,
                    credential_ref=conn.credential_ref,
                    instance_id=conn.instance_id,
                    connection_id=conn.id,
                    autocommit=False,
                )

        logger.info(
            "Connection cascade revoked %d connection(s) admin=%s "
            "scope=%s",
            len(revoked),
            admin_id,
            f"instance:{instance_id}" if instance_id is not None else "admin",
        )
        return len(revoked)

    def restore_by_pk(
        self,
        pk: int,
        *,
        updated_by: str | None = None,
        audit_ctx: AuditContext | None = None,
    ) -> Instance | None:
        """Restore a soft-deleted Instance within the grace window.

        Sets ``instance_status='active'``, ``active=True``, clears
        ``soft_deleted_at``. Writes an ``ACTION_INSTANCE_RESTORED``
        audit row. Returns ``None`` if not found OR if the grace
        window has expired (route layer maps the latter to 410 Gone).

        Per Vision §6.4 Reactivation, the embed-key re-mint is NOT
        performed here — that is the service layer's job, because it
        spans two repositories (instance + api_keys) and the service
        is the right place to coordinate them in a single transaction.
        """
        instance = self.get_by_pk(pk)
        if instance is None:
            return None
        if instance.instance_status.value not in INSTANCE_GRACE_STATES:
            # Not in the grace window -- restore is a no-op transition.
            # The route layer treats this as 409 (not 410: the grace
            # window is not the question; the row is already live).
            # (Accepts grace_window + legacy 'deleted'.)
            return instance
        if instance.soft_deleted_at is None:
            # Shape invariant violated: grace-window rows must carry a
            # soft_deleted_at. Refuse to restore rather than guess.
            logger.error(
                "Instance restore refused: pk=%s is in a grace state but "
                "soft_deleted_at is NULL (shape invariant violation)",
                instance.id,
            )
            return None

        deleted_at = instance.soft_deleted_at
        if deleted_at.tzinfo is None:
            deleted_at = deleted_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - deleted_at > timedelta(
            days=INSTANCE_RESTORE_GRACE_DAYS
        ):
            # Grace expired -- route layer maps to 410 Gone.
            return None

        before_status = instance.instance_status
        before_soft_deleted_at = instance.soft_deleted_at

        instance.instance_status = InstanceStatus.ACTIVE
        instance.active = True
        instance.soft_deleted_at = None

        if audit_ctx is not None:
            AdminAuditRepository(self.db).record(
                ctx=audit_ctx,
                admin_id=instance.admin_id,
                action=ACTION_INSTANCE_RESTORED,
                resource_type=RESOURCE_INSTANCE,
                resource_pk=instance.id,
                resource_natural_id=instance.instance_slug,
                luciel_instance_id=instance.id,
                before={
                    "instance_status": before_status.value,
                    "active": False,
                    "soft_deleted_at": before_soft_deleted_at.isoformat(),
                },
                after={
                    "instance_status": InstanceStatus.ACTIVE.value,
                    "active": True,
                    "soft_deleted_at": None,
                },
                autocommit=False,
            )

        self.db.commit()
        self.db.refresh(instance)
        logger.info(
            "Instance restored pk=%s admin_id=%s instance_slug=%s",
            instance.id,
            instance.admin_id,
            instance.instance_slug,
        )
        return instance

    def deactivate_by_pk(
        self,
        pk: int,
        *,
        updated_by: str | None = None,
        audit_ctx: AuditContext | None = None,
    ) -> Instance | None:
        """Deprecated alias for :meth:`pause_by_pk`.

        Arc 11 Closeout PR-A renamed operational deactivation to Pause
        per Customer Journey §4.5 Phase 8 (three distinct affordances:
        Pause / Delete / Close). Existing internal callsites continue
        to work; new code must call ``pause_by_pk`` directly. Slated
        for removal in Arc 12 alongside the legacy ``active`` boolean.
        """
        warnings.warn(
            "InstanceRepository.deactivate_by_pk is deprecated; "
            "use pause_by_pk (or delete_by_pk for destructive intent).",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.pause_by_pk(
            pk,
            updated_by=updated_by,
            audit_ctx=audit_ctx,
        )

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
                {
                    Instance.active: False,
                    # Arc 11 Closeout PR-A — cascade-deactivate maps to
                    # the Pause state (not Delete): the admin-level
                    # cascade is operational (data retained), not
                    # destructive. Closure (Arc 10) drives the
                    # destructive-intent path for accounts.
                    Instance.instance_status: InstanceStatus.PAUSED,
                },
                synchronize_session=False,
            )
        )

        if audit_ctx is not None and updated:
            AdminAuditRepository(self.db).record(
                ctx=audit_ctx,
                admin_id=admin_id,
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
