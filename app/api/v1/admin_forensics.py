"""Admin forensic-read endpoints for the verify harness.

Arc 12 EX1b note (agent_id excision)
-----------------------------------

Per Architecture v1 §3.7.2 / §3.7.3 the v2 boundary is Admin ->
Instance; per-Agent forensic narrowing has no v2 equivalent. The
``agent_id`` query parameter on ``memory_items_step29c`` and the
``agent_id`` projection field on ``ApiKeyForensic`` /
``MemoryItemForensic`` / ``AdminAuditLogForensic`` are removed.
Verify-harness pillars that previously narrowed by agent_id now
narrow by ``admin_id`` (already required) +
``luciel_instance_id`` if a per-instance probe is needed.

Step 29 Commit C.1 (P11 reads) lands the first four endpoints. C.2
(P12 reads) extends `memory_items_step29c` with `actor_user_id` and
`agent_id` filters and adds `actor_user_id` to the
`MemoryItemForensic` projection -- no new endpoint needed (api_keys
lookup reuses `api_keys_step29c?id=` from C.1). C.3 (P13 reads) adds
one new endpoint `messages_step29c` for setup-message-id lookup,
extends `memory_items_step29c` with `message_id` (exact) and
`content_contains` (substring) filters, and extends
`admin_audit_logs_step29c` with `actor_key_prefix` (exact) filter.
C.4 (P14 reads) adds one new endpoint `users_step29c/{user_id}`
for the User.active assertion (A6 — User persists across
departure); the two ApiKey reads (A1/A2) reuse C.1's
`api_keys_step29c?id=` and the two MemoryItem reads (A5/A7)
reuse C.2's `memory_items_step29c?admin_id=&actor_user_id=`.
C.5 (P11 F10 ORM-write migration) adds the first and only
mutation in the C-series: a platform_admin POST at
`/instances_step29c/{instance_id}/toggle_active` that
forensically flips `instances.active` so P11's instance-
liveness Gate-4 assertion can be set up and torn down without
direct ORM writes from inside the verify harness. The route
emits an `ACTION_LUCIEL_INSTANCE_FORENSIC_TOGGLE` audit row
BEFORE mutating active so an audit-write failure aborts the
mutation; the audit-row-before-mutation invariant is pinned by
an AST test in tests/api/test_admin_forensics_step29c.py
(test 18) and the ALLOWED_ACTIONS membership invariant by
test 19. C.6 cross-pillar cleanup drops `from app.db.session
import SessionLocal` from the four pillar files.

Routes
------

    GET /api/v1/admin/forensics/api_keys_step29c
        ?id=<int>
    GET /api/v1/admin/forensics/memory_items_step29c
        ?admin_id=<str>
        &user_id=<str>                  # chat-end-user string (P11)
        &actor_user_id=<uuid>           # platform User UUID (P12, C.2)
        &message_id_not_null=<bool>
        &message_id=<int>               # exact match (P13, C.3)
        &content_contains=<str>         # substring probe (P13, C.3)
        &limit=<int=100>
    GET /api/v1/admin/forensics/admin_audit_logs_step29c
        ?admin_id=<str>
        &action=<str>
        &actor_label_like=<str>
        &actor_key_prefix=<str>         # exact 12-char handle (P13, C.3)
        &limit=<int=100>
    GET /api/v1/admin/forensics/instances_step29c/{instance_id}
    GET /api/v1/admin/forensics/messages_step29c
        ?session_id=<str>               # P13 setup-message lookup
        &limit=<int=100>
    GET /api/v1/admin/forensics/users_step29c/{user_id}    # P14, C.4
    POST /api/v1/admin/forensics/instances_step29c
         /{instance_id}/toggle_active                     # P11 F10, C.5
        body: {"active": <bool>}

All routes (4 in C.1, +1 in C.3, +1 in C.4 GET, +1 in C.5 POST = 7 today):
  - require platform_admin via ScopePolicy.is_platform_admin
  - are rate-limited via the Arc 7 C4 tier-aware limiter (pre-Arc-7: ADMIN_RATE_LIMIT)
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
       - the Arc 7 C4 tier-aware limiter (this module; pre-Arc-7 name: ADMIN_RATE_LIMIT),
       - DB role isolation (verify task's worker DSN cannot mutate
         users/scope_assignments per migration f392a842f885 from
         Phase 2 Commit 4; the API process's luciel_admin DSN that
         executes these SELECTs has read access to all four tables
         already in production).
     Audit rows on reads add nothing to that boundary.

If a future compliance requirement ever demands read-auditing, that
should be a uniform repo-wide change, not a one-off for this module.
The decision is recorded in the Architecture document in the Space
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
not permit). See the audit doctrine and
the Architecture document in the Space for the full rule.

Authored: Aryan Singh <aryans.www@gmail.com>, Step 29 Commit C.1.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import DbSession
from app.middleware.rate_limit import (
    limiter,
    get_tier_aware_key,
    get_tier_rate_limit_for_key,
)
from app.models.admin_audit_log import (
    ACTION_LUCIEL_INSTANCE_FORENSIC_TOGGLE,
    ACTION_MEMORY_EXTRACTED,
    ACTION_WORKER_MALFORMED_PAYLOAD,
    AdminAuditLog,
    RESOURCE_LUCIEL_INSTANCE,
)
from app.models.api_key import ApiKey
from app.models.instance import Instance
from app.models.memory import MemoryItem
from app.models.message import MessageModel
from app.models.user import User
from app.policy.scope import ScopePolicy
from app.repositories.admin_audit_repository import (
    AdminAuditRepository,
    AuditContext,
)
from app.services.admin_service import AdminService


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

    Arc 12 EX1b: ``domain_id`` and ``agent_id`` projection fields
    removed. Per §3.7.2 / §3.7.3 the v2 forensic narrowing is
    admin_id + luciel_instance_id.
    """

    id: int
    key_prefix: str
    admin_id: str | None
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

    Arc 12 EX1b: ``agent_id`` projection field removed (the Agent
    layer was excised; per-instance narrowing is via
    luciel_instance_id).
    """

    id: int
    user_id: str
    actor_user_id: uuid.UUID | None
    admin_id: str
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

    Arc 12 EX1b: ``domain_id`` and ``agent_id`` projection fields
    removed. The underlying columns persist on ``admin_audit_logs``
    (the canonical hash field set still includes them -- EX4 owns
    the chain reseal). The forensic projection no longer exposes
    them because v2 scoping is admin_id + luciel_instance_id.
    """

    id: int
    action: str
    resource_type: str
    admin_id: str
    luciel_instance_id: int | None
    actor_key_prefix: str | None
    actor_label: str | None
    after_json: dict[str, Any] | None
    created_at: datetime


class AdminAuditLogsForensic(BaseModel):
    rows: list[AdminAuditLogForensic]


class MessageForensic(BaseModel):
    """Strict-projection of `messages` for forensic read.

    `content` is NEVER included. Chat content is the most sensitive
    field after `memory_items.content`; the harness only needs `id`
    (the message_id used to construct/probe spoof payloads in P13)
    plus `session_id` and `role` for context. `trace_id` is included
    because it lets future cross-pillar tests correlate a message
    with the Celery task it kicked off without dragging audit-log
    rows into the loop.
    """

    id: int
    session_id: str
    role: str
    trace_id: str | None
    created_at: datetime


class MessagesForensic(BaseModel):
    items: list[MessageForensic]


class UserForensic(BaseModel):
    """Strict-projection of `users` for forensic read.

    Step 29 Commit C.4. Backs P14 A6 (`User.active` after departure
    -- the foundational Q6 claim that a User leaving one tenant
    keeps their platform identity). Projection EXCLUDES `email` and
    `display_name` -- both are PII and the forensic surface has no
    business returning them. `synthetic` is included as useful
    metadata (it is a boolean flag distinguishing
    Option-B-onboarding-auto-created users from real users; not
    PII). `id` is returned as a string because the column is a
    Postgres UUID and the API serialization is consistent across
    the rest of the forensics surface.
    """

    id: str
    active: bool
    synthetic: bool


class InstanceForensic(BaseModel):
    """Strict-projection of ``instances`` for forensic read (V2 shape).

    Arc 5 Path A — collapsed to the V2 (Admin → Instance) hierarchy.
    The legacy scope_level / scope_owner_domain_id / scope_owner_agent_id
    fields no longer exist; the V2 Instance carries ``admin_id`` only.
    """

    id: int
    instance_id: str = Field(alias="instance_slug")
    admin_id: str = Field(alias="admin_id")
    active: bool
    created_at: datetime

    model_config = {"populate_by_name": True}


class InstanceToggleRequest(BaseModel):
    """Request body for the C.5 forensic toggle POST.

    A single field `active`. The route emits an audit row carrying
    both the previous and the requested value, then mutates the row
    only if the previous value differs (no-op writes are still
    audited but skip the SQL UPDATE so audit history accurately
    reflects observable state changes).
    """

    active: bool


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
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
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
        admin_id=row.admin_id,
        luciel_instance_id=row.luciel_instance_id,
        active=row.active,
        created_at=row.created_at,
    )


@router.get(
    "/memory_items_step29c",
    response_model=MemoryItemsForensic,
)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def list_memory_items_forensic_step29c(
    request: Request,
    db: DbSession,
    admin_id: str = Query(..., max_length=100),
    user_id: str | None = Query(default=None, max_length=100),
    actor_user_id: uuid.UUID | None = Query(default=None),
    message_id_not_null: bool = Query(default=False),
    message_id: int | None = Query(default=None, ge=1),
    content_contains: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=_LIMIT_DEFAULT, ge=1, le=_LIMIT_MAX),
) -> MemoryItemsForensic:
    """Forensic read of memory_items rows. platform_admin only.

    Step 29 Commit C.1 backs P11 F2 line 238 (idempotency probe target
    lookup). Step 29 Commit C.2 extends the filter set with
    `actor_user_id` (platform User UUID) so P12 A1/A3/A4/A5
    identity-stability assertions can read actor-attributed memory
    rows over HTTP. P13/P14 cross-tenant leak checks reuse this
    endpoint in later sub-commits. Arc 12 EX1b: the legacy
    ``agent_id`` filter was removed (the Agent layer was excised
    per §3.7.2; per-instance narrowing -- if needed -- is via
    luciel_instance_id added by the api+schemas lane).

    Step 29 Commit C.3 adds `message_id` (exact match) and
    `content_contains` (substring probe) for P13 A1/A3/A5/degraded
    reads. The projection is unchanged -- `content_contains` lets
    the caller test for substring presence without ever returning
    the substring or the surrounding content text. The probe is
    locked behind platform_admin + the Arc 7 C4 tier-aware limiter and is the
    minimum primitive needed for P13 A1's spoof-absence assertion.

    Filter combination semantics (all AND-joined; omit a param to
    leave its dimension unconstrained):
      - admin_id is required (per-tenant isolation, no
        cross-tenant scans even for platform_admin)
      - user_id is the chat-end-user string
        (memory_items.user_id, e.g. "pillar11-user")
      - actor_user_id is the platform User UUID
        (memory_items.actor_user_id FK -> users.id)
      - message_id_not_null narrows to rows where message_id is set
        (Step 27b idempotency probe shape)
      - message_id is exact match on the FK to messages.id (P13 C.3)
      - content_contains is a substring probe over memory text
        (P13 A1 spoof-absence; projection still excludes content,
        so callers can only learn whether matching rows exist, not
        what the content is)

    Returns the strict MemoryItemForensic projection (no content;
    actor_user_id IS returned). Hard limit 1000.
    """
    _require_platform_admin_step29c(request)

    stmt = select(MemoryItem).where(MemoryItem.admin_id == admin_id)
    if user_id is not None:
        stmt = stmt.where(MemoryItem.user_id == user_id)
    if actor_user_id is not None:
        stmt = stmt.where(MemoryItem.actor_user_id == actor_user_id)
    if message_id_not_null:
        stmt = stmt.where(MemoryItem.message_id.is_not(None))
    if message_id is not None:
        stmt = stmt.where(MemoryItem.message_id == message_id)
    if content_contains is not None:
        # Substring probe. Projection still excludes content, so the
        # caller learns only "row id N matches", not what content[N]
        # holds. Used by P13 A1 to assert ABSENCE of a sentinel,
        # which is the safest possible shape for this filter.
        stmt = stmt.where(MemoryItem.content.contains(content_contains))
    stmt = stmt.order_by(MemoryItem.id.desc()).limit(limit)

    rows = list(db.scalars(stmt))
    return MemoryItemsForensic(
        items=[
            MemoryItemForensic(
                id=r.id,
                user_id=r.user_id,
                actor_user_id=r.actor_user_id,
                admin_id=r.admin_id,
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
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def list_admin_audit_logs_forensic_step29c(
    request: Request,
    db: DbSession,
    admin_id: str = Query(..., max_length=100),
    action: str | None = Query(default=None, max_length=100),
    actor_label_like: str | None = Query(default=None, max_length=100),
    actor_key_prefix: str | None = Query(default=None, max_length=32),
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
    as a prefix). `action` and `actor_key_prefix` are exact-match.
    `actor_key_prefix` (Step 29 Commit C.3) is the 12-char public
    correlation handle stamped on every audit row at write time;
    P13 A2 uses it to confirm Gate 6's IDENTITY_SPOOF audit row
    was emitted by the legitimate K1 key, not by some bystander
    key in the same tenant.

    after_json is included in the projection; production code
    already guarantees no PII there (P11 F5 verifies this every
    run). Hard limit 1000.
    """
    _require_platform_admin_step29c(request)

    stmt = select(AdminAuditLog).where(AdminAuditLog.admin_id == admin_id)
    if action is not None:
        stmt = stmt.where(AdminAuditLog.action == action)
    if actor_label_like is not None:
        # Treat caller's input as a literal-prefix match. We append
        # '%' here rather than asking the caller to do it, so the
        # harness side stays simple.
        stmt = stmt.where(AdminAuditLog.actor_label.like(actor_label_like + "%"))
    if actor_key_prefix is not None:
        stmt = stmt.where(AdminAuditLog.actor_key_prefix == actor_key_prefix)
    stmt = stmt.order_by(AdminAuditLog.id.desc()).limit(limit)

    rows = list(db.scalars(stmt))
    return AdminAuditLogsForensic(
        rows=[
            AdminAuditLogForensic(
                id=r.id,
                action=r.action,
                resource_type=r.resource_type,
                admin_id=r.admin_id,
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
    "/instances_step29c/{instance_id}",
    response_model=InstanceForensic,
)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def get_luciel_instance_forensic_step29c(
    request: Request,
    db: DbSession,
    instance_id: int,
) -> InstanceForensic:
    """Forensic read of one luciel_instances row by integer id.

    platform_admin only. Step 29 Commit C.1. Backs P11 F10
    lines 419 / 453's `db.get(Instance, state.instance_agent)`.
    Returns the strict InstanceForensic projection. The
    `active` boolean toggle is OUT of scope here -- it lands as an
    admin POST in Commit C.5 (P11 F10 ORM-write migration).
    404 if the row does not exist.
    """
    _require_platform_admin_step29c(request)

    row = db.get(Instance, instance_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"instances row id={instance_id} not found.",
        )
    return InstanceForensic(
        id=row.id,
        instance_slug=row.instance_slug,
        admin_id=row.admin_id,
        active=row.active,
        created_at=row.created_at,
    )


@router.get(
    "/users_step29c/{user_id}",
    response_model=UserForensic,
)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def get_user_forensic_step29c(
    request: Request,
    db: DbSession,
    user_id: str,
) -> UserForensic:
    """Forensic read of one users row by UUID. platform_admin only.

    Step 29 Commit C.4. Backs P14 A6's `db.get(User, user_id)`
    assertion at pillar_14_departure_semantics.py L490 -- the
    foundational Q6 claim that a User leaving one tenant does
    NOT lose their platform identity (`active` stays True).
    Returns the strict UserForensic projection (no email, no
    display_name -- both are PII and have no place on a
    forensic surface). 404 if the row does not exist.

    The `user_id` path param is the UUID string form of the
    primary key; it is parsed via uuid.UUID() to reject
    malformed inputs early (a 400 is friendlier than letting
    Postgres raise on the SELECT).
    """
    _require_platform_admin_step29c(request)

    try:
        parsed_id = uuid.UUID(user_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"user_id={user_id!r} is not a valid UUID.",
        )

    row = db.get(User, parsed_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"users row id={user_id} not found.",
        )
    return UserForensic(
        id=str(row.id),
        active=row.active,
        synthetic=row.synthetic,
    )


@router.get(
    "/messages_step29c",
    response_model=MessagesForensic,
)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def list_messages_forensic_step29c(
    request: Request,
    db: DbSession,
    session_id: str = Query(..., max_length=100),
    limit: int = Query(default=_LIMIT_DEFAULT, ge=1, le=_LIMIT_MAX),
) -> MessagesForensic:
    """Forensic read of messages rows for a session. platform_admin only.

    Step 29 Commit C.3. Backs P13 setup-message-id lookup at
    pillar_13_cross_tenant_identity.py L346-L361 -- the spoof
    payload referenced in P13 A1/A2 needs a real message_id from
    the legitimate setup turn (Gate 1 rejects malformed message_id
    integers, so without a real one the spoof never reaches Gate 6).

    Returns the strict MessageForensic projection (no content; chat
    content is the most sensitive field after memory content). Rows
    are ordered DESC by id, so callers requesting `limit=1` get the
    most recent message in the session, which is the shape P13's
    setup lookup uses. Hard limit 1000.
    """
    _require_platform_admin_step29c(request)

    stmt = (
        select(MessageModel)
        .where(MessageModel.session_id == session_id)
        .order_by(MessageModel.id.desc())
        .limit(limit)
    )
    rows = list(db.scalars(stmt))
    return MessagesForensic(
        items=[
            MessageForensic(
                id=r.id,
                session_id=r.session_id,
                role=r.role,
                trace_id=r.trace_id,
                created_at=r.created_at,
            )
            for r in rows
        ]
    )


@router.post(
    "/instances_step29c/{instance_id}/toggle_active",
    response_model=InstanceForensic,
)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def toggle_luciel_instance_active_step29c(
    request: Request,
    db: DbSession,
    instance_id: int,
    payload: InstanceToggleRequest,
) -> InstanceForensic:
    """Forensic toggle of one luciel_instances row's `active` flag.

    Step 29 Commit C.5 -- the first and only mutation in the C-series.
    Backs P11 F10 at pillar_11_async_memory.py L535-541 (deactivate to
    set up the instance-liveness Gate-4 assertion) and L614-615
    (restore previous state in the finally-block teardown). Both
    callsites previously used direct ORM writes
    (`inst.active = ...; db.commit()`) inside the verify harness;
    routing them through this POST migrates the last ORM-write
    surface in the four-pillar verify suite to platform-admin HTTP.

    platform_admin only. Rate-limited by the Arc 7 C4 tier-aware limiter identically
    to the C.1-C.4 forensic GETs.

    Audit-row-before-mutation invariant
    -----------------------------------

    The route writes the admin_audit_log row BEFORE mutating
    `instances.active`. If the audit insert fails (constraint
    violation, permission denied, etc.), the function raises and the
    SQL UPDATE never executes. Both writes commit atomically in a
    single `db.commit()` so a commit-time failure rolls both back.
    AdminAuditRepository.record(autocommit=False) gives us this
    behavior: the row is `add()`'d and `flush()`'d (gets an id) but
    only persists when the caller's commit succeeds.

    The ordering is pinned by an AST test in
    tests/api/test_admin_forensics_step29c.py (test 18) -- the audit
    .record() call must appear at a lower line number than the
    `inst.active = ...` assignment. A future maintainer who refactors
    this route into a "mutate then audit" shape (which is wrong because
    a mutation that fails to audit silently breaks the compliance
    contract) breaks the test.

    No-op writes
    ------------

    When the requested `active` already matches the row's current
    state, we still write the audit row (so the harness's POST is
    fully traceable in admin_audit_log) but skip the SQL UPDATE so
    `updated_at` does not advance. This keeps audit history aligned
    with observable state changes: a row ages only when something
    actually changed.

    Action constant
    ---------------

    ACTION_LUCIEL_INSTANCE_FORENSIC_TOGGLE is deliberately distinct
    from ACTION_DEACTIVATE / ACTION_REACTIVATE. The latter pair are
    operational verbs used across many resource_types (tenants,
    api_keys, memory items, scope assignments) and disambiguated only
    by resource_type. The forensic toggle is NOT operational -- it
    is a verify-harness fixture lever, and an auditor scanning
    admin_audit_log for real LucielInstance deactivations should not
    see harness traffic mixed in. The constant's membership in
    ALLOWED_ACTIONS is pinned by test 19.

    404 if the luciel_instances row does not exist.
    """
    _require_platform_admin_step29c(request)

    inst = db.get(Instance, instance_id)
    if inst is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"instances row id={instance_id} not found.",
        )

    previous_active = bool(inst.active)
    requested_active = bool(payload.active)

    audit_ctx = AuditContext.from_request(request)
    audit_repo = AdminAuditRepository(db)
    # Audit FIRST -- if record() raises (e.g. unknown action/resource_type
    # validation, DB constraint), the mutation below never executes.
    audit_repo.record(
        ctx=audit_ctx,
        admin_id=inst.admin_id,
        action=ACTION_LUCIEL_INSTANCE_FORENSIC_TOGGLE,
        resource_type=RESOURCE_LUCIEL_INSTANCE,
        resource_pk=inst.id,
        resource_natural_id=inst.instance_slug,
        # Arc 5 Path A — domain_id / agent_id no longer exist in V2.
        domain_id=None,
        agent_id=None,
        luciel_instance_id=inst.id,
        before={"active": previous_active},
        after={"active": requested_active},
        note="step29-c5-forensic-toggle",
        autocommit=False,
    )

    # Step 29.y Cluster 2 (G-2 fix): cascade memory deactivation BEFORE
    # flipping active. Pre-29.y, the forensic toggle deactivated the
    # luciel_instance row but left memory_items rows with active=True
    # underneath. When an agent slot was later reassigned, the new
    # occupant inherited the prior occupant's memory rows because:
    #   - D-1 in memory_repository does not filter on luciel_instance_id
    #     when reading user memories
    #   - F-1 confirms the FK is ON DELETE SET NULL, not RESTRICT
    #   - The forensic-toggle path (this route) did not cascade
    # That is the exact PIPEDA P5 hole findings_phase1g.md G-2 calls
    # out, with a working production reach path through any
    # platform_admin key.
    #
    # The cascade only fires on a real True->False transition, so
    # P11 F10's deactivate-then-restore harness flow still works:
    # the deactivate leg cascades memory rows (active=False), the
    # restore leg (False->True) is a no-op for memory cascade. We
    # do NOT auto-reactivate memory rows on True<-False because
    # forensic restore is a fixture-lever, not an operational
    # un-deactivate -- operational reactivation would go through a
    # dedicated route with explicit memory-reactivation policy.
    #
    # autocommit=False keeps the audit row + cascade UPDATE +
    # active-flag flip atomic in a single db.commit() at the end of
    # this function. If any leg fails the whole batch rolls back.
    if requested_active is False and previous_active is True:
        AdminService(db).bulk_soft_deactivate_memory_items_for_luciel_instance(
            admin_id=inst.admin_id,
            luciel_instance_id=inst.id,
            audit_ctx=audit_ctx,
            updated_by=getattr(request.state, "actor_label", None),
            autocommit=False,
        )

    # Mutate only if the requested value differs from current. No-op
    # writes still audit (above) but do not bump updated_at.
    if requested_active != previous_active:
        inst.active = requested_active

    db.commit()
    db.refresh(inst)

    return InstanceForensic(
        id=inst.id,
        instance_slug=inst.instance_slug,
        admin_id=inst.admin_id,
        active=inst.active,
        created_at=inst.created_at,
    )


# =====================================================================
# Step 29.y -- Worker pipeline liveness probe (Pillar 25 backing route)
#
# Why this route exists
# ---------------------
#
# Step 29.x diag15 found that production has had ZERO `memory_extracted`
# audit rows ever, ZERO `worker_*` audit rows in the last 7 days, and
# ZERO non-verify tenant messages in 7 days. That is consistent with
# "no real customer traffic yet" but it ALSO means we have never
# observed the Celery worker pipeline emit a single audit row in
# production. Before REMAX Tier-3 onboarding (Step 30b) we MUST be
# able to assert, on every verify run, that the worker pipeline is
# alive and emitting audit rows -- otherwise the first real customer
# message would be the first time we discover the pipeline is dead.
#
# The blocker for asserting this from the verify ECS task itself is
# that the verify task does NOT have broker network access in prod
# (Pillar 11 falls into MODE=degraded for the same reason; its broker
# probe checks REDIS_URL which prod does not set -- prod uses SQS).
# The backend container DOES have broker network access (it is the
# Celery producer for every chat turn). So the verify task asks the
# backend, over HTTP, to do a producer-side enqueue + audit-row poll,
# and asserts on the result. That keeps verify pure-HTTP while still
# exercising the real broker plane.
#
# Two modes
# ---------
#
#   - mode=malformed (DEFAULT). Enqueues a payload with `message_id`
#     of the wrong type. The worker's Gate 1 rejects it and writes
#     ACTION_WORKER_MALFORMED_PAYLOAD. This proves: broker connection
#     up, worker process up, worker accepting tasks, worker writing
#     audit rows, DB write path live. NO LLM call, NO real memory row,
#     fast (typically < 2s end-to-end). This is what Pillar 25 calls
#     on every verify run.
#
#   - mode=full. Enqueues a real well-formed payload that exercises
#     extract_memory_from_turn end-to-end through the LLM. Polls for
#     ACTION_MEMORY_EXTRACTED. Slower (10-20s) and consumes LLM
#     credits. NEVER fires from CI -- only callable manually with
#     ?mode=full and a real `message_id` query param. Used pre-REMAX
#     onboarding and after any worker-pipeline change to prove the
#     happy path.
#
# What this route does NOT do
# ---------------------------
#
# The probe does NOT write its OWN admin_audit_log row. The contract
# we assert is "the WORKER emits an audit row in response to our
# enqueue." Adding a probe-emitted audit row would (a) require a new
# ACTION_* constant, a new ALLOWED_ACTIONS migration, and a new
# RESOURCE_* type -- all of which are net-new compliance surface
# for a route whose only job is observation; and (b) muddy the
# audit-log-as-evidence story: a forensic auditor scanning for
# worker-emitted rows should not have to filter out probe-emitted
# ones. The route is platform_admin gated and rate-limited, so the
# observability requirement is met by the regular access log.
#
# Cleanup
# -------
#
# The malformed-payload mode is fire-and-forget on the broker side
# -- there is no scheduled task to clean up because Gate 1 rejects
# before any DB row is created (no MemoryItem, no Message). The
# audit row IS persisted (that is what we polled for); it is left
# in place as evidence the probe ran. The verify-task tenant teardown
# (Pillar 10) reaps tenant-scoped audit rows, which is where probe-
# emitted rows from the verify run end up.
#
# In full-mode (manual only), the resulting MemoryItem is owned by
# whatever tenant the caller specifies; it is the caller's
# responsibility to clean it up (manual probe = manual cleanup).
# =====================================================================


class WorkerPipelineProbeRequest(BaseModel):
    """Body for the worker-pipeline-probe route.

    All fields except `admin_id` and `actor_key_prefix` are optional
    in the malformed-mode default path; the malformed payload is
    constructed by the route. In full-mode the caller MUST supply a
    real `message_id` (int) and `user_id` (str) so the worker can
    actually extract memory from a turn.
    """

    admin_id: str = Field(..., min_length=1, max_length=128)
    actor_key_prefix: str = Field(..., min_length=12, max_length=12)
    # full-mode only:
    user_id: str | None = Field(default=None, max_length=128)
    message_id: int | None = Field(default=None)
    session_id: str | None = Field(default=None, max_length=128)


class WorkerPipelineProbeResponse(BaseModel):
    """Probe outcome.

    `audit_id` is the id of the worker-emitted audit row that the
    probe polled for. `elapsed_ms` is wall-clock time from enqueue
    to audit-row visibility. `polled_for_action` echoes which action
    constant the probe was looking for so a 504 caller can see what
    we expected.
    """

    mode: Literal["malformed", "full"]
    audit_id: int
    polled_for_action: str
    elapsed_ms: int


class WorkerPipelineProbeTimeout(BaseModel):
    mode: Literal["malformed", "full"]
    polled_for_action: str
    elapsed_ms: int
    detail: str


_PROBE_DEADLINE_SECONDS = 30.0
_PROBE_POLL_INTERVAL = 0.5


@router.post(
    "/worker_pipeline_probe_step29y",
    response_model=WorkerPipelineProbeResponse,
    responses={504: {"model": WorkerPipelineProbeTimeout}},
)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def worker_pipeline_probe_step29y(
    request: Request,
    db: DbSession,
    payload: WorkerPipelineProbeRequest,
    mode: Literal["malformed", "full"] = Query(default="malformed"),
) -> WorkerPipelineProbeResponse:
    """Backend-side worker pipeline liveness probe (Step 29.y / Pillar 25).

    Enqueues a Celery task and polls admin_audit_logs for the worker-
    emitted row that proves the pipeline is alive. Returns 200 with
    timing on success, 504 with structured detail on timeout.

    platform_admin gated. See the module-level Step 29.y comment block
    above for the full design rationale (modes, no-self-audit, cleanup).
    """
    _require_platform_admin_step29c(request)

    # Producer-side import: the backend container is the Celery producer
    # for every chat turn, so this import is the same one that runs in
    # production every time a user sends a message.
    from app.worker.tasks.memory_extraction import extract_memory_from_turn

    if mode == "full":
        if payload.message_id is None or payload.user_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "mode=full requires message_id (int) and user_id (str) "
                    "in the request body so the worker can extract memory "
                    "from a real turn."
                ),
            )
        polled_action = ACTION_MEMORY_EXTRACTED
        kwargs = {
            "session_id": payload.session_id or f"worker-probe-{uuid.uuid4().hex[:12]}",
            "user_id": payload.user_id,
            "admin_id": payload.admin_id,
            "message_id": int(payload.message_id),
            "actor_key_prefix": payload.actor_key_prefix,
        }
    else:
        # Default: malformed payload. message_id deliberately wrong type so
        # the worker's Gate 1 rejects with WORKER_MALFORMED_PAYLOAD.
        polled_action = ACTION_WORKER_MALFORMED_PAYLOAD
        kwargs = {
            "session_id": f"worker-probe-{uuid.uuid4().hex[:12]}",
            "user_id": "worker-probe-user",
            "admin_id": payload.admin_id,
            "message_id": "not-an-int",  # type violation -> Gate 1 rejection
            "actor_key_prefix": payload.actor_key_prefix,
        }

    # Snapshot the current max audit id so we only count rows that
    # land AFTER our enqueue. Without this, a recently-enqueued
    # malformed payload from another caller could falsely satisfy
    # the poll. The id column is monotonic (bigserial), so MAX(id)
    # is a valid high-water mark.
    high_water = db.execute(
        select(AdminAuditLog.id)
        .where(AdminAuditLog.admin_id == payload.admin_id)
        .where(AdminAuditLog.action == polled_action)
        .order_by(AdminAuditLog.id.desc())
        .limit(1)
    ).scalar_one_or_none() or 0

    started = time.monotonic()
    extract_memory_from_turn.apply_async(kwargs=kwargs)

    deadline = started + _PROBE_DEADLINE_SECONDS
    found: AdminAuditLog | None = None
    while time.monotonic() < deadline:
        # Each poll is a fresh SELECT -- we deliberately do NOT hold a
        # transaction open across sleeps because a long-held read
        # transaction would block worker INSERTs on the same table
        # under MVCC at the highest isolation levels.
        db.expire_all()
        row = db.execute(
            select(AdminAuditLog)
            .where(AdminAuditLog.admin_id == payload.admin_id)
            .where(AdminAuditLog.action == polled_action)
            .where(AdminAuditLog.id > high_water)
            .order_by(AdminAuditLog.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if row is not None:
            found = row
            break
        time.sleep(_PROBE_POLL_INTERVAL)

    elapsed_ms = int((time.monotonic() - started) * 1000)

    if found is None:
        # Structured 504 -- caller (Pillar 25) can format a meaningful
        # failure message without re-implementing the polling contract.
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={
                "mode": mode,
                "polled_for_action": polled_action,
                "elapsed_ms": elapsed_ms,
                "detail": (
                    f"worker_pipeline_probe_step29y did not observe a new "
                    f"{polled_action!r} audit row for admin_id="
                    f"{payload.admin_id!r} within "
                    f"{int(_PROBE_DEADLINE_SECONDS)}s of enqueue. The "
                    f"worker may be down, the broker may be unreachable, "
                    f"or the worker DSN may have lost INSERT privilege "
                    f"on admin_audit_logs."
                ),
            },
        )

    return WorkerPipelineProbeResponse(
        mode=mode,
        audit_id=found.id,
        polled_for_action=polled_action,
        elapsed_ms=elapsed_ms,
    )


# =====================================================================
# Arc 11 Step 6 -- knowledge ingest pipeline smoke probe.
#
# Same posture as ``worker_pipeline_probe_step29y`` above but for the
# new knowledge ingest plane. The full happy path (S3 download +
# parse + chunk + embed + persist) would call the LLM and burn
# credits on every probe; the chat-path smoke probe handles the
# Celery + broker piece. What this route covers is the STRUCTURAL
# chain: the embed_source task is registered, the matching queue is
# wired into the broker transport config, the S3 bucket name resolves
# from env / SSM, and ``KnowledgeSourceRepository`` is importable +
# constructible against a tenant-scoped session.
#
# If any of those is broken on a fresh deploy, this probe fails
# loudly BEFORE the first real customer ingest hits an opaque
# IngestionConfigError in CloudWatch.
#
# A future mode=full could enqueue a tiny synthetic ingest end-to-end
# (the way worker_pipeline_probe_step29y has a mode=full); deferred
# until we have a way to mock-out the embedder or run against a
# dummy S3 object.
# =====================================================================


class KnowledgePipelineProbeResponse(BaseModel):
    """Probe outcome for the Arc 11 Step 6 knowledge-pipeline smoke."""

    celery_task_registered: bool
    knowledge_queue_wired: bool
    knowledge_bucket_resolved: bool
    source_repository_constructible: bool
    retriever_importable: bool
    detail: dict[str, Any]


@router.get(
    "/knowledge_pipeline_probe_arc11",
    response_model=KnowledgePipelineProbeResponse,
)
@limiter.limit(get_tier_rate_limit_for_key, key_func=get_tier_aware_key)
def knowledge_pipeline_probe_arc11(
    request: Request,
    db: DbSession,
) -> KnowledgePipelineProbeResponse:
    """Smoke probe for the Arc 11 knowledge ingest pipeline.

    Five structural checks, each independent so the response shows
    exactly which piece is wedged:

      1. ``celery_task_registered`` — ``app.worker.tasks.embed_source.
         embed_source`` is registered on the producer-side Celery app.
         Failure here means the worker container is running but the
         backend container has not imported the task module (the
         include[] list in celery_app.py drifted).
      2. ``knowledge_queue_wired`` — the matching SQS queue is in
         ``broker_transport_options['predefined_queues']``. Without
         this, the next ``.apply_async(queue='luciel-knowledge-tasks')``
         falls through to a ListQueues call and a permission error.
      3. ``knowledge_bucket_resolved`` — ``KNOWLEDGE_S3_BUCKET`` env
         is populated. The worker fails the first ingest with an
         IngestionConfigError without this; better to detect at
         deploy time.
      4. ``source_repository_constructible`` — the new repository
         imports cleanly against the request-scoped session.
      5. ``retriever_importable`` — the chunk repository's vector
         path imports cleanly (catches pgvector ImportError that
         would otherwise only fire on the first real retrieve).

    Platform_admin gated. Read-only. No audit row.
    """
    _require_platform_admin_step29c(request)

    detail: dict[str, Any] = {}

    # ----- Check 1: task registered -----
    try:
        from app.worker.celery_app import celery_app

        celery_app.loader.import_default_modules()
        task_name = "app.worker.tasks.embed_source.embed_source"
        task_registered = task_name in celery_app.tasks
        detail["task_name"] = task_name
    except Exception as exc:  # noqa: BLE001
        task_registered = False
        detail["task_registered_error"] = type(exc).__name__

    # ----- Check 2: queue wired -----
    try:
        from app.worker.celery_app import celery_app

        bto = celery_app.conf.get("broker_transport_options") or {}
        predef = bto.get("predefined_queues") or {}
        queue_wired = "luciel-knowledge-tasks" in predef
        detail["predefined_queues_present"] = sorted(predef.keys())
    except Exception as exc:  # noqa: BLE001
        queue_wired = False
        detail["queue_wired_error"] = type(exc).__name__

    # ----- Check 3: bucket name resolves -----
    try:
        from app.worker.tasks.embed_source import _resolve_bucket_name

        bucket = _resolve_bucket_name()
        bucket_resolved = bool(bucket)
        # Bucket name itself is not sensitive — it's in the IAM
        # policy, the CFN template, and the task-def env. Surfacing
        # it in the probe response lets the operator confirm which
        # bucket the worker will actually hit.
        detail["bucket_resolved_value"] = bucket or None
    except Exception as exc:  # noqa: BLE001
        bucket_resolved = False
        detail["bucket_resolved_error"] = type(exc).__name__

    # ----- Check 4: source repository constructible -----
    try:
        from app.repositories.knowledge_source_repository import (
            KnowledgeSourceRepository,
        )

        # Construct against the request session — proves the import
        # path is clean and the constructor signature hasn't drifted.
        KnowledgeSourceRepository(db)
        source_repo_constructible = True
    except Exception as exc:  # noqa: BLE001
        source_repo_constructible = False
        detail["source_repo_error"] = type(exc).__name__

    # ----- Check 5: retriever importable -----
    try:
        from app.knowledge.retriever import (  # noqa: F401
            KnowledgeRetriever,
            RetrievedChunk,
            collect_source_pks,
        )

        retriever_importable = True
    except Exception as exc:  # noqa: BLE001
        retriever_importable = False
        detail["retriever_error"] = type(exc).__name__

    return KnowledgePipelineProbeResponse(
        celery_task_registered=task_registered,
        knowledge_queue_wired=queue_wired,
        knowledge_bucket_resolved=bucket_resolved,
        source_repository_constructible=source_repo_constructible,
        retriever_importable=retriever_importable,
        detail=detail,
    )
