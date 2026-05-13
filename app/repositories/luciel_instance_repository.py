"""
LucielInstance repository — data-access layer for child-Luciel rows.

Step 24.5. Wraps app.models.luciel_instance.LucielInstance.

Scope of responsibility:
- Pure CRUD. No ScopePolicy calls, no HTTP exceptions, no validation
  beyond what SQLAlchemy / Postgres CHECK constraints enforce.
- Scope-shape invariants (scope_level <-> owner columns) are already
  enforced by LucielInstanceCreate (File 4) and the DB CheckConstraint
  ck_luciel_instances_scope_owner_shape (File 2), so this layer can
  trust its inputs.
- Create-at-or-below authorization is ScopePolicy's job (File 9).
- Parent-scope-active validation (is the owning domain active? is the
  owning agent active?) lives in LucielInstanceService (File 7).

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
    RESOURCE_LUCIEL_INSTANCE,
)
from app.models.luciel_instance import (
    SCOPE_LEVEL_AGENT,
    SCOPE_LEVEL_DOMAIN,
    SCOPE_LEVEL_TENANT,
    LucielInstance,
)
from app.repositories.admin_audit_repository import (
    AdminAuditRepository,
    AuditContext,
    diff_updated_fields,
)

logger = logging.getLogger(__name__)


class LucielInstanceRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ---------------------------------------------------------------
    # Create
    # ---------------------------------------------------------------

    def create(
        self,
        *,
        instance_id: str,
        display_name: str,
        scope_level: str,
        scope_owner_tenant_id: str,
        scope_owner_domain_id: str | None = None,
        scope_owner_agent_id: str | None = None,
        description: str | None = None,
        system_prompt_additions: str | None = None,
        preferred_provider: str | None = None,
        allowed_tools: list[str] | None = None,
        created_by: str | None = None,
        autocommit: bool = True,
        audit_ctx: AuditContext | None = None,
    ) -> LucielInstance:
        """Insert a new LucielInstance row.

        Uniqueness + scope-shape invariants are enforced at the DB.
        autocommit=False lets OnboardingService compose into its
        atomic onboard transaction.

        audit_ctx, when provided, writes an admin_audit_logs row in
        the same transaction.
        """
        instance = LucielInstance(
            instance_id=instance_id,
            display_name=display_name,
            description=description,
            scope_level=scope_level,
            scope_owner_tenant_id=scope_owner_tenant_id,
            scope_owner_domain_id=scope_owner_domain_id,
            scope_owner_agent_id=scope_owner_agent_id,
            system_prompt_additions=system_prompt_additions,
            preferred_provider=preferred_provider,
            allowed_tools=allowed_tools,
            active=True,
            knowledge_chunk_count=0,
            created_by=created_by,
        )
        self.db.add(instance)
        self.db.flush()  # assigns instance.id

        if audit_ctx is not None:
            AdminAuditRepository(self.db).record(
                ctx=audit_ctx,
                tenant_id=scope_owner_tenant_id,
                action=ACTION_CREATE,
                resource_type=RESOURCE_LUCIEL_INSTANCE,
                resource_pk=instance.id,
                resource_natural_id=instance_id,
                domain_id=scope_owner_domain_id,
                agent_id=scope_owner_agent_id,
                luciel_instance_id=instance.id,
                after={
                    "scope_level": scope_level,
                    "display_name": display_name,
                    "description": description,
                    "preferred_provider": preferred_provider,
                    "allowed_tools": allowed_tools,
                    # NOTE: system_prompt_additions intentionally
                    # omitted from create snapshot — it can be large
                    # and we capture it in later updates via diff.
                    "active": True,
                },
                autocommit=False,
            )

        if autocommit:
            self.db.commit()
            self.db.refresh(instance)
        logger.info(
            "LucielInstance created scope=%s tenant=%s domain=%s agent=%s "
            "instance_id=%s",
            scope_level,
            scope_owner_tenant_id,
            scope_owner_domain_id,
            scope_owner_agent_id,
            instance_id,
        )
        return instance

    # ---------------------------------------------------------------
    # Read
    # ---------------------------------------------------------------

    def get_by_pk(self, pk: int) -> LucielInstance | None:
        return (
            self.db.query(LucielInstance)
            .filter(LucielInstance.id == pk)
            .first()
        )

    def get_scoped(
        self,
        *,
        scope_owner_tenant_id: str,
        scope_owner_domain_id: str | None,
        scope_owner_agent_id: str | None,
        instance_id: str,
    ) -> LucielInstance | None:
        """Fetch by full natural key.

        NULL-aware filtering: when scope_owner_domain_id / agent_id is
        None, we must use IS NULL rather than ==, because SQL = NULL
        never matches.
        """
        query = self.db.query(LucielInstance).filter(
            LucielInstance.scope_owner_tenant_id == scope_owner_tenant_id,
            LucielInstance.instance_id == instance_id,
        )
        if scope_owner_domain_id is None:
            query = query.filter(LucielInstance.scope_owner_domain_id.is_(None))
        else:
            query = query.filter(
                LucielInstance.scope_owner_domain_id == scope_owner_domain_id
            )
        if scope_owner_agent_id is None:
            query = query.filter(LucielInstance.scope_owner_agent_id.is_(None))
        else:
            query = query.filter(
                LucielInstance.scope_owner_agent_id == scope_owner_agent_id
            )
        return query.first()

    # ---------------------------------------------------------------
    # Count helpers — used by the Step 30a.1 cap-enforcement guard.
    # The repo never raises on count; the service layer compares the
    # returned int against Subscription.instance_count_cap and decides.
    # ---------------------------------------------------------------

    def count_active_for_tenant(self, tenant_id: str) -> int:
        """Return the number of ACTIVE LucielInstances under a tenant.

        Counts across ALL scope levels (tenant + domain + agent) -- the
        Step 30a.1 cap is a tenant-wide budget on instance_count_cap,
        not a per-scope cap. Inactive instances are excluded so a
        deactivate frees a slot.
        """
        return (
            self.db.query(LucielInstance)
            .filter(
                LucielInstance.scope_owner_tenant_id == tenant_id,
                LucielInstance.active.is_(True),
            )
            .count()
        )

    def list_for_scope(
        self,
        *,
        tenant_id: str,
        domain_id: str | None = None,
        agent_id: str | None = None,
        include_inherited: bool = False,
        active_only: bool = False,
    ) -> list[LucielInstance]:
        """List instances belonging to a scope.

        Two modes:

        include_inherited=False (default — management view):
            Returns only instances OWNED at the specified scope.
            - (tenant)                       -> tenant-level only
            - (tenant, domain)               -> domain-level under that domain
            - (tenant, domain, agent)        -> agent-level under that agent
            This is what the admin list endpoint uses: "show me what I
            own at this scope."

        include_inherited=True (chat-time / discovery view):
            Returns the specified scope PLUS everything above it in
            the ownership chain. An agent call with include_inherited
            will surface tenant-level + that-domain-level + agent-level
            instances — useful for "what Luciels can this user reach?"
            queries (e.g. building a Council view).

        Caller is responsible for ScopePolicy authorization.
        """
        query = self.db.query(LucielInstance).filter(
            LucielInstance.scope_owner_tenant_id == tenant_id
        )

        if not include_inherited:
            # Strict "owned at this scope" semantics.
            if agent_id is not None:
                # agent-level: both domain and agent owner must match
                assert domain_id is not None, (
                    "agent_id requires domain_id for unambiguous scope"
                )
                query = query.filter(
                    LucielInstance.scope_level == SCOPE_LEVEL_AGENT,
                    LucielInstance.scope_owner_domain_id == domain_id,
                    LucielInstance.scope_owner_agent_id == agent_id,
                )
            elif domain_id is not None:
                # domain-level only
                query = query.filter(
                    LucielInstance.scope_level == SCOPE_LEVEL_DOMAIN,
                    LucielInstance.scope_owner_domain_id == domain_id,
                    LucielInstance.scope_owner_agent_id.is_(None),
                )
            else:
                # tenant-level only
                query = query.filter(
                    LucielInstance.scope_level == SCOPE_LEVEL_TENANT,
                    LucielInstance.scope_owner_domain_id.is_(None),
                    LucielInstance.scope_owner_agent_id.is_(None),
                )
        else:
            # Inherited semantics: tenant + (optionally) that domain +
            # (optionally) that agent.
            from sqlalchemy import and_, or_

            branches = [
                # Always include tenant-level
                and_(
                    LucielInstance.scope_level == SCOPE_LEVEL_TENANT,
                    LucielInstance.scope_owner_domain_id.is_(None),
                    LucielInstance.scope_owner_agent_id.is_(None),
                ),
            ]
            if domain_id is not None:
                branches.append(
                    and_(
                        LucielInstance.scope_level == SCOPE_LEVEL_DOMAIN,
                        LucielInstance.scope_owner_domain_id == domain_id,
                        LucielInstance.scope_owner_agent_id.is_(None),
                    )
                )
            if agent_id is not None:
                assert domain_id is not None, (
                    "agent_id requires domain_id for inherited scope"
                )
                branches.append(
                    and_(
                        LucielInstance.scope_level == SCOPE_LEVEL_AGENT,
                        LucielInstance.scope_owner_domain_id == domain_id,
                        LucielInstance.scope_owner_agent_id == agent_id,
                    )
                )
            query = query.filter(or_(*branches))

        if active_only:
            query = query.filter(LucielInstance.active.is_(True))

        return query.order_by(LucielInstance.id.asc()).all()

    # ---------------------------------------------------------------
    # Update
    # ---------------------------------------------------------------

    # Identity / scope columns are deliberately not updatable. Moving
    # an instance across scopes would break knowledge ownership, chat
    # key bindings, and audit trails. Deactivate + recreate instead.
    _UPDATABLE_FIELDS = frozenset(
        {
            "display_name",
            "description",
            "system_prompt_additions",
            "preferred_provider",
            "allowed_tools",
            "active",
            "updated_by",
        }
    )

    def update(
        self,
        instance: LucielInstance,
        *,
        audit_ctx: AuditContext | None = None,
        **fields,
    ) -> LucielInstance:
        """Apply field updates to an existing LucielInstance.

        Silently ignores any field not in _UPDATABLE_FIELDS so scope
        columns and instance_id are unreachable via PATCH. Writes an
        audit row containing only the fields that actually changed.
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
                    tenant_id=instance.scope_owner_tenant_id,
                    action=ACTION_UPDATE,
                    resource_type=RESOURCE_LUCIEL_INSTANCE,
                    resource_pk=instance.id,
                    resource_natural_id=instance.instance_id,
                    domain_id=instance.scope_owner_domain_id,
                    agent_id=instance.scope_owner_agent_id,
                    luciel_instance_id=instance.id,
                    before=before_diff,
                    after=after_diff,
                    autocommit=False,
                )

        self.db.commit()
        self.db.refresh(instance)
        if applied:
            logger.info(
                "LucielInstance updated pk=%s fields=%s",
                instance.id,
                sorted(applied.keys()),
            )
        return instance

    # ---------------------------------------------------------------
    # Deactivate (soft delete)
    # ---------------------------------------------------------------
    # ---------------------------------------------------------------
    # Deactivate (soft delete)
    # ---------------------------------------------------------------

    def deactivate_by_pk(
        self,
        pk: int,
        *,
        updated_by: str | None = None,
        audit_ctx: AuditContext | None = None,
    ) -> LucielInstance | None:
        """Soft-deactivate a specific instance. Returns None if not found."""
        instance = self.get_by_pk(pk)
        if instance is None:
            return None

        was_active = bool(instance.active)
        instance.active = False
        if updated_by is not None:
            instance.updated_by = updated_by

        if audit_ctx is not None and was_active:
            AdminAuditRepository(self.db).record(
                ctx=audit_ctx,
                tenant_id=instance.scope_owner_tenant_id,
                action=ACTION_DEACTIVATE,
                resource_type=RESOURCE_LUCIEL_INSTANCE,
                resource_pk=instance.id,
                resource_natural_id=instance.instance_id,
                domain_id=instance.scope_owner_domain_id,
                agent_id=instance.scope_owner_agent_id,
                luciel_instance_id=instance.id,
                before={"active": True},
                after={"active": False},
                autocommit=False,
            )

        self.db.commit()
        self.db.refresh(instance)
        logger.info(
            "LucielInstance deactivated pk=%s scope=%s tenant=%s "
            "domain=%s agent=%s instance_id=%s",
            instance.id,
            instance.scope_level,
            instance.scope_owner_tenant_id,
            instance.scope_owner_domain_id,
            instance.scope_owner_agent_id,
            instance.instance_id,
        )
        return instance

    def deactivate_all_for_agent(
        self,
        *,
        tenant_id: str,
        domain_id: str,
        agent_id: str,
        updated_by: str | None = None,
        audit_ctx: AuditContext | None = None,
    ) -> int:
        """Cascade: deactivate every agent-scoped instance owned by a
        single agent. Returns the number of rows updated.

        Writes ONE audit row for the cascade (with count + affected
        instance PKs in `after`), not one per row. Per-row audit would
        balloon the audit trail for large cascades; the cascade event
        itself is the meaningful unit.
        """
        # Snapshot affected PKs before the bulk update so the audit row
        # can identify exactly which instances were cascaded.
        affected = (
            self.db.query(LucielInstance.id, LucielInstance.instance_id)
            .filter(
                LucielInstance.scope_owner_tenant_id == tenant_id,
                LucielInstance.scope_owner_domain_id == domain_id,
                LucielInstance.scope_owner_agent_id == agent_id,
                LucielInstance.scope_level == SCOPE_LEVEL_AGENT,
                LucielInstance.active.is_(True),
            )
            .all()
        )
        affected_pks = [pk for pk, _ in affected]
        affected_ids = [nid for _, nid in affected]

        updated = (
            self.db.query(LucielInstance)
            .filter(
                LucielInstance.scope_owner_tenant_id == tenant_id,
                LucielInstance.scope_owner_domain_id == domain_id,
                LucielInstance.scope_owner_agent_id == agent_id,
                LucielInstance.scope_level == SCOPE_LEVEL_AGENT,
                LucielInstance.active.is_(True),
            )
            .update(
                {
                    LucielInstance.active: False,
                    LucielInstance.updated_by: updated_by,
                },
                synchronize_session=False,
            )
        )

        if audit_ctx is not None and updated:
            AdminAuditRepository(self.db).record(
                ctx=audit_ctx,
                tenant_id=tenant_id,
                action=ACTION_CASCADE_DEACTIVATE,
                resource_type=RESOURCE_LUCIEL_INSTANCE,
                resource_pk=None,
                resource_natural_id=None,
                domain_id=domain_id,
                agent_id=agent_id,
                luciel_instance_id=None,
                after={
                    "count": int(updated),
                    "affected_pks": affected_pks,
                    "affected_instance_ids": affected_ids,
                    "trigger": "agent_deactivate",
                },
                note=f"Cascade from agent {agent_id} deactivation",
                autocommit=False,
            )

        self.db.commit()
        logger.info(
            "LucielInstance cascade-deactivated count=%d tenant=%s "
            "domain=%s agent=%s",
            updated,
            tenant_id,
            domain_id,
            agent_id,
        )
        return int(updated)

    def deactivate_all_for_domain(
        self,
        *,
        tenant_id: str,
        domain_id: str,
        updated_by: str | None = None,
        audit_ctx: AuditContext | None = None,
    ) -> int:
        """Cascade: deactivate every domain-scoped AND agent-scoped
        instance under a single domain. Returns the number of rows
        updated. Writes one audit row for the cascade event.
        """
        affected = (
            self.db.query(LucielInstance.id, LucielInstance.instance_id)
            .filter(
                LucielInstance.scope_owner_tenant_id == tenant_id,
                LucielInstance.scope_owner_domain_id == domain_id,
                LucielInstance.scope_level.in_(
                    (SCOPE_LEVEL_DOMAIN, SCOPE_LEVEL_AGENT)
                ),
                LucielInstance.active.is_(True),
            )
            .all()
        )
        affected_pks = [pk for pk, _ in affected]
        affected_ids = [nid for _, nid in affected]

        updated = (
            self.db.query(LucielInstance)
            .filter(
                LucielInstance.scope_owner_tenant_id == tenant_id,
                LucielInstance.scope_owner_domain_id == domain_id,
                LucielInstance.scope_level.in_(
                    (SCOPE_LEVEL_DOMAIN, SCOPE_LEVEL_AGENT)
                ),
                LucielInstance.active.is_(True),
            )
            .update(
                {
                    LucielInstance.active: False,
                    LucielInstance.updated_by: updated_by,
                },
                synchronize_session=False,
            )
        )

        if audit_ctx is not None and updated:
            AdminAuditRepository(self.db).record(
                ctx=audit_ctx,
                tenant_id=tenant_id,
                action=ACTION_CASCADE_DEACTIVATE,
                resource_type=RESOURCE_LUCIEL_INSTANCE,
                resource_pk=None,
                resource_natural_id=None,
                domain_id=domain_id,
                agent_id=None,
                luciel_instance_id=None,
                after={
                    "count": int(updated),
                    "affected_pks": affected_pks,
                    "affected_instance_ids": affected_ids,
                    "trigger": "domain_deactivate",
                },
                note=f"Cascade from domain {domain_id} deactivation",
                autocommit=False,
            )

        self.db.commit()
        logger.info(
            "LucielInstance cascade-deactivated count=%d tenant=%s domain=%s",
            updated,
            tenant_id,
            domain_id,
        )
        return int(updated)


    def deactivate_all_for_tenant(
        self,
        *,
        tenant_id: str,
        updated_by: str | None = None,
        audit_ctx: AuditContext | None = None,
        autocommit: bool = True,
    ) -> int:
        """Cascade: deactivate every instance owned by a tenant,
        across all scope levels (tenant, domain, agent).

        Used by AdminService.deactivate_tenant_with_cascade. Returns
        the number of rows updated. Writes one audit row for the
        cascade event (only when updated > 0, matching the per-agent
        and per-domain pattern in this repo).

        No scope_level filter -- a tenant deactivation should clear
        every instance under that tenant regardless of which scope
        level the instance binds at.

        autocommit=True by default for standalone callers. The tenant-
        cascade spine passes autocommit=False so the whole cascade
        commits in a single transaction. The two existing _for_agent /
        _for_domain methods commit unconditionally; making them
        autocommit-aware is deferred to a follow-up refactor (drift
        D-luciel-instance-repo-cascade-not-autocommit-aware-2026-05-02).
        """
        affected = (
            self.db.query(LucielInstance.id, LucielInstance.instance_id)
            .filter(
                LucielInstance.scope_owner_tenant_id == tenant_id,
                LucielInstance.active.is_(True),
            )
            .all()
        )
        affected_pks = [pk for pk, _ in affected]
        affected_ids = [nid for _, nid in affected]

        updated = (
            self.db.query(LucielInstance)
            .filter(
                LucielInstance.scope_owner_tenant_id == tenant_id,
                LucielInstance.active.is_(True),
            )
            .update(
                {
                    LucielInstance.active: False,
                    LucielInstance.updated_by: updated_by,
                },
                synchronize_session=False,
            )
        )

        if audit_ctx is not None and updated:
            AdminAuditRepository(self.db).record(
                ctx=audit_ctx,
                tenant_id=tenant_id,
                action=ACTION_CASCADE_DEACTIVATE,
                resource_type=RESOURCE_LUCIEL_INSTANCE,
                resource_pk=None,
                resource_natural_id=None,
                domain_id=None,
                agent_id=None,
                luciel_instance_id=None,
                after={
                    "count": int(updated),
                    "affected_pks": affected_pks,
                    "affected_instance_ids": affected_ids,
                    "trigger": "tenant_deactivate",
                },
                note=f"Cascade from tenant {tenant_id} deactivation",
                autocommit=False,
            )

        if autocommit:
            self.db.commit()
        logger.info(
            "LucielInstance cascade-deactivated count=%d tenant=%s "
            "(all scope levels)",
            updated,
            tenant_id,
        )
        return int(updated)

    # ---------------------------------------------------------------
    # Step 25 hook — knowledge chunk counter maintenance
    # ---------------------------------------------------------------

    def adjust_knowledge_chunk_count(
        self,
        instance: LucielInstance,
        delta: int,
    ) -> LucielInstance:
        """Increment / decrement the advisory knowledge_chunk_count.

        Step 25's ingestion / replace / delete flows will call this.
        Clamped at zero — a negative delta that would underflow is
        treated as "reset to zero" rather than letting the counter go
        negative, since the column is advisory for dashboards and a
        negative value would look like a bug.
        """
        new_count = max(0, int(instance.knowledge_chunk_count or 0) + int(delta))
        instance.knowledge_chunk_count = new_count
        self.db.commit()
        self.db.refresh(instance)
        return instance