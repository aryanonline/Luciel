"""Admin forensic-read endpoints for the verify harness.

Step 29 Commit C.1 (P11 reads) lands the first four endpoints. C.2
(P12 reads) extends `memory_items_step29c` with `actor_user_id` and
`agent_id` filters and adds `actor_user_id` to the
`MemoryItemForensic` projection -- no new endpoint needed (api_keys
lookup reuses `api_keys_step29c?id=` from C.1). C.3-C.4 extend
further for P13/P14 reads. C.5 adds one POST for the P11 F10
`luciel_instances.active` toggle. C.6 cross-pillar cleanup drops
`from app.db.session import SessionLocal` from the four pillar files.

Routes
------

    GET /api/v1/admin/forensics/api_keys_step29c
        ?id=<int>
    GET /api/v1/admin/forensics/memory_items_step29c
        ?tenant_id=<str>
        &user_id=<str>                  # chat-end-user string (P11)
        &actor_user_id=<uuid>           # platform User UUID (P12, C.2)
        &agent_id=<str>                 # agent slug (P12, C.2)
        &message_id_not_null=<bool>
        &limit=<int=100>
    GET /api/v1/admin/forensics/admin_audit_logs_step29c
        ?tenant_id=<str>
        &action=<str>
        &actor_label_like=<str>
        &limit=<int=100>
    GET /api/v1/admin/forensics/luciel_instances_step29c/{instance_id}

All four routes:
  - require platform_admin via ScopePolicy.is_platform_admin
  - are rate-limited via ADMIN_RATE_LIMIT
  - are SELECT-only against tables the API process's luciel_admin
    DSN already reads in production
  - return strict-projection Pydantic models (key_hash NEVER returned;
    memory_items.content NEVER returned; admin_audit_log.after_json IS
    returned because the harness's F5/F6/F8 hygiene assertions require
    it -- production code already guarantees no PII in after_json;
    memory_items.actor_user_id IS returned because P12 A1/A3/A4/A5
    assert identity-stability across role changes by reading it.
    actor_user_id is a platform User UUID, not user-supplied content,
    and is the canonical attribution handle Step 24.5b makes
    NOT NULL on every memory row.)

No-read-audit decision (Step 29 Commit C.1, 2026-05-06)
-------------------------------------------------------

These endpoints do NOT write admin_audit_log rows on call. Rationale:

  1. Symmetry with existing precedent. `get_scope_assignment_p2c12`
     at admin.py:1919 (Phase 2 Commit 12) is platform_admin-gated,
     rate-limited, and does NOT audit-on-call. Adding read-auditing
     for forensics routes only would create an inconsistent surface
     that future readers of the codebase would reasonably question.

  2. Audit-row-on-read for verify-harness traffic would generate
     27 reads * verify-after-every-commit doctrine + future CI runs
     = 5-figure audit row growth per week from the harness alone.
     That is noise that masks real audit signal during incident
     response. ALLOWED_ACTIONS in `app/models/admin_audit_log.py`
     deliberately contains only mutation-shaped verbs.

  3. The actual security boundary is enforced by:
       - platform_admin permission gate (this module),
       - ADMIN_RATE_LIMIT (this module),
       - DB role isolation (verify task's worker DSN cannot mutate
         users/scope_assignments per migration f392a842f885 from
         Phase 2 Commit 4; the API process's luciel_admin DSN that
         executes these SELECTs has read access to all four tables
         already in production).
     Audit rows on reads add nothing to that boundary.

If a future compliance requirement ever demands read-auditing, that
should be a uniform repo-wide change, not a one-off for this module.
The decision is recorded in `docs/CANONICAL_RECAP.md` Section 4.4
(Step 29 commit order) so future readers can find the rationale.

Producer-side exemption cross-reference
---------------------------------------

These read-only forensic GETs are the IN-SCOPE half of
`D-verify-task-pure-http-2026-05-05`. The OUT-OF-SCOPE half is the
producer-side exemption codified by Commit B.3 (4120f8d): a
verification pillar may act as a direct Celery producer or service-
layer caller WHEN AND ONLY WHEN the assertion under test is a
property of the producer-side path itself (latency, idempotency,
worker response to a payload shape that the HTTP API contract does
not permit). See `docs/STEP_29_AUDIT.md` Section 6 and
`docs/CANONICAL_RECAP.md` Section 15 for the full rule.

Authored: Aryan Singh <aryans.www@gmail.com>, Step 29 Commit C.1.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import DbSession
from app.middleware.rate_limit import (
    ADMIN_RATE_LIMIT,
    get_api_key_or_ip,
    limiter,
)
from app.models.admin_audit_log import AdminAuditLog
from app.models.api_key import ApiKey
from app.models.luciel_instance import LucielInstance
from app.models.memory import MemoryItem
from app.policy.scope import ScopePolicy


router = APIRouter(prefix="/admin/forensics", tags=["admin", "forensics"])


# ---------------------------------------------------------------------
# Response schemas (strict projections; defined inline so the
# forensic-read surface is fully visible in this single module).
# ---------------------------------------------------------------------


class ApiKeyForensic(BaseModel):
    """Strict-projection of `api_keys` for forensic read.

    `key_hash` is NEVER included. The 12-char `key_prefix` is the
    same public correlation handle already stored on every audit
    row, so admins can correlate without any secret exposure.
    """

    id: int
    key_prefix: str
    tenant_id: str | None
    domain_id: str | None
    agent_id: str | None
    luciel_instance_id: int | None
    active: bool
    created_at: datetime


class MemoryItemForensic(BaseModel):
    """Strict-projection of `memory_items` for forensic read.

    `content` is NEVER included. The harness only reads ids and
    metadata for idempotency probes, cross-tenant leak checks, and
    Step 24.5b actor-attribution assertions; it never asserts on
    memory text content.

    `actor_user_id` IS included (Step 29 Commit C.2): P12 A1/A3/A4/A5
    require it to verify that platform User identity persists across
    Agent role changes. It is the platform User UUID (FK to users.id),
    NOT user-supplied content, and is the canonical attribution
    handle Step 24.5b made NOT NULL on every memory row.
    """

    id: int
    user_id: str
    actor_user_id: uuid.UUID | None
    tenant_id: str
    agent_id: str | None
    category: str
    message_id: int | None
    luciel_instance_id: int | None
    active: bool
    created_at: datetime


class MemoryItemsForensic(BaseModel):
    items: list[MemoryItemForensic]


class AdminAuditLogForensic(BaseModel):
    """Strict-projection of `admin_audit_logs` for forensic read.

    `after_json` IS included; F5/F6/F8 hygiene assertions in P11
    require it, and production code already guarantees no PII in
    `after_json` (the F5 assertion verifies this every run).
    `before_json`, `note`, `row_hash`, `prev_row_hash` are omitted
    to keep the projection minimal; harness does not read them.
    """

    id: int
    action: str
    resource_type: str
    tenant_id: str
    domain_id: str | None
    agent_id: str | None
    luciel_instance_id: int | None
    actor_key_prefix: str | None
    actor_label: str | None
    after_json: dict[str, Any] | None
    created_at: datetime


class AdminAuditLogsForensic(BaseModel):
    rows: list[AdminAuditLogForensic]


class LucielInstanceForensic(BaseModel):
    """Strict-projection of `luciel_instances` for forensic read.

    P11 F10 only needs `id` + `active`; the rest are included for
    cross-pillar reuse (P12/P13 may want `scope_level` etc. in
    later sub-commits) without forcing another round-trip.
    """

    id: int
    instance_id: str
    tenant_id: str = Field(alias="scope_owner_tenant_id")
    scope_level: str
    scope_owner_domain_id: str | None
    scope_owner_agent_id: str | None
    active: bool
    created_at: datetime

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------
# Permission helper -- one private function so the four routes share
# an identical 403 message and we do not drift between them.
# ---------------------------------------------------------------------


def _require_platform_admin_step29c(request: Request) -> None:
    if not ScopePolicy.is_platform_admin(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Only platform_admin may read this forensic endpoint."
            ),
        )


# ---------------------------------------------------------------------
# Limit clamps -- a pillar that asks for more than 1000 rows is
# almost certainly buggy. Reject up-front rather than let an
# accidentally-unbounded SELECT grind the DB.
# ---------------------------------------------------------------------


_LIMIT_DEFAULT = 100
_LIMIT_MAX = 1000


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------


@router.get(
    "/api_keys_step29c",
    response_model=ApiKeyForensic,
)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def get_api_key_forensic_step29c(
    request: Request,
    db: DbSession,
    id: int = Query(..., description="ApiKey primary key (int)."),
) -> ApiKeyForensic:
    """Forensic read of one api_keys row by id. platform_admin only.

    Step 29 Commit C.1. Backs P11 F1 line 197's lookup of the agent
    chat key's key_prefix. Returns the strict ApiKeyForensic
    projection (no key_hash). 404 if the row does not exist.
    """
    _require_platform_admin_step29c(request)

    row = db.scalars(select(ApiKey).where(ApiKey.id == id).limit(1)).first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"api_keys row id={id} not found.",
        )
    return ApiKeyForensic(
        id=row.id,
        key_prefix=row.key_prefix,
        tenant_id=row.tenant_id,
        domain_id=row.domain_id,
        agent_id=row.agent_id,
        luciel_instance_id=row.luciel_instance_id,
        active=row.active,
        created_at=row.created_at,
    )


@router.get(
    "/memory_items_step29c",
    response_model=MemoryItemsForensic,
)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def list_memory_items_forensic_step29c(
    request: Request,
    db: DbSession,
    tenant_id: str = Query(..., max_length=100),
    user_id: str | None = Query(default=None, max_length=100),
    actor_user_id: uuid.UUID | None = Query(default=None),
    agent_id: str | None = Query(default=None, max_length=100),
    message_id_not_null: bool = Query(default=False),
    limit: int = Query(default=_LIMIT_DEFAULT, ge=1, le=_LIMIT_MAX),
) -> MemoryItemsForensic:
    """Forensic read of memory_items rows. platform_admin only.

    Step 29 Commit C.1 backs P11 F2 line 238 (idempotency probe target
    lookup). Step 29 Commit C.2 extends the filter set with
    `actor_user_id` (platform User UUID) and `agent_id` (agent slug)
    so P12 A1/A3/A4/A5 identity-stability assertions can read
    actor-attributed memory rows over HTTP. P13/P14 cross-tenant leak
    checks reuse this endpoint in later sub-commits.

    Filter combination semantics (all AND-joined; omit a param to
    leave its dimension unconstrained):
      - tenant_id is required (per-tenant isolation, no
        cross-tenant scans even for platform_admin)
      - user_id is the chat-end-user string
        (memory_items.user_id, e.g. "pillar11-user")
      - actor_user_id is the platform User UUID
        (memory_items.actor_user_id FK -> users.id)
      - agent_id is the agent slug
        (memory_items.agent_id, e.g. "p12-a1-abc123")
      - message_id_not_null narrows to rows where message_id is set
        (Step 27b idempotency probe shape)

    Returns the strict MemoryItemForensic projection (no content;
    actor_user_id IS returned). Hard limit 1000.
    """
    _require_platform_admin_step29c(request)

    stmt = select(MemoryItem).where(MemoryItem.tenant_id == tenant_id)
    if user_id is not None:
        stmt = stmt.where(MemoryItem.user_id == user_id)
    if actor_user_id is not None:
        stmt = stmt.where(MemoryItem.actor_user_id == actor_user_id)
    if agent_id is not None:
        stmt = stmt.where(MemoryItem.agent_id == agent_id)
    if message_id_not_null:
        stmt = stmt.where(MemoryItem.message_id.is_not(None))
    stmt = stmt.order_by(MemoryItem.id.desc()).limit(limit)

    rows = list(db.scalars(stmt))
    return MemoryItemsForensic(
        items=[
            MemoryItemForensic(
                id=r.id,
                user_id=r.user_id,
                actor_user_id=r.actor_user_id,
                tenant_id=r.tenant_id,
                agent_id=r.agent_id,
                category=r.category,
                message_id=r.message_id,
                luciel_instance_id=r.luciel_instance_id,
                active=r.active,
                created_at=r.created_at,
            )
            for r in rows
        ]
    )


@router.get(
    "/admin_audit_logs_step29c",
    response_model=AdminAuditLogsForensic,
)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def list_admin_audit_logs_forensic_step29c(
    request: Request,
    db: DbSession,
    tenant_id: str = Query(..., max_length=100),
    action: str | None = Query(default=None, max_length=100),
    actor_label_like: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=_LIMIT_DEFAULT, ge=1, le=_LIMIT_MAX),
) -> AdminAuditLogsForensic:
    """Forensic read of admin_audit_logs rows. platform_admin only.

    Step 29 Commit C.1. Backs P11 F3 line 274
    (worker_cross_tenant_reject poll), F4 line 312
    (worker_malformed_payload poll), F9 line 399 (forbidden-action
    count -- here returned as a list the harness can len()), and
    F10 line 476 (worker_instance_deactivated poll). P13 A1/A2
    (worker_identity_spoof_reject poll) reuses this endpoint in
    Commit C.3.

    `actor_label_like` is a substring filter using SQL LIKE (the
    harness's F9 query uses `actor_label LIKE 'worker:%'` which we
    encode by passing `actor_label_like='worker:'` and matching it
    as a prefix). Action filter is exact-match.

    after_json is included in the projection; production code
    already guarantees no PII there (P11 F5 verifies this every
    run). Hard limit 1000.
    """
    _require_platform_admin_step29c(request)

    stmt = select(AdminAuditLog).where(AdminAuditLog.tenant_id == tenant_id)
    if action is not None:
        stmt = stmt.where(AdminAuditLog.action == action)
    if actor_label_like is not None:
        # Treat caller's input as a literal-prefix match. We append
        # '%' here rather than asking the caller to do it, so the
        # harness side stays simple.
        stmt = stmt.where(AdminAuditLog.actor_label.like(actor_label_like + "%"))
    stmt = stmt.order_by(AdminAuditLog.id.desc()).limit(limit)

    rows = list(db.scalars(stmt))
    return AdminAuditLogsForensic(
        rows=[
            AdminAuditLogForensic(
                id=r.id,
                action=r.action,
                resource_type=r.resource_type,
                tenant_id=r.tenant_id,
                domain_id=r.domain_id,
                agent_id=r.agent_id,
                luciel_instance_id=r.luciel_instance_id,
                actor_key_prefix=r.actor_key_prefix,
                actor_label=r.actor_label,
                after_json=r.after_json,
                created_at=r.created_at,
            )
            for r in rows
        ]
    )


@router.get(
    "/luciel_instances_step29c/{instance_id}",
    response_model=LucielInstanceForensic,
)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def get_luciel_instance_forensic_step29c(
    request: Request,
    db: DbSession,
    instance_id: int,
) -> LucielInstanceForensic:
    """Forensic read of one luciel_instances row by integer id.

    platform_admin only. Step 29 Commit C.1. Backs P11 F10
    lines 419 / 453's `db.get(LucielInstance, state.instance_agent)`.
    Returns the strict LucielInstanceForensic projection. The
    `active` boolean toggle is OUT of scope here -- it lands as an
    admin POST in Commit C.5 (P11 F10 ORM-write migration).
    404 if the row does not exist.
    """
    _require_platform_admin_step29c(request)

    row = db.get(LucielInstance, instance_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"luciel_instances row id={instance_id} not found.",
        )
    return LucielInstanceForensic(
        id=row.id,
        instance_id=row.instance_id,
        scope_owner_tenant_id=row.scope_owner_tenant_id,
        scope_level=row.scope_level,
        scope_owner_domain_id=row.scope_owner_domain_id,
        scope_owner_agent_id=row.scope_owner_agent_id,
        active=row.active,
        created_at=row.created_at,
    )
