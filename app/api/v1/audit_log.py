"""
Admin audit-log read API (Step 28 Phase 2 — Commit 2 + Commit 2b review fixes).

Resolves canonical-recap §4.1 item 4: "/api/v1/admin/audit-log returns
404 currently". Mounts under the existing /api/v1/admin prefix family
behind the same admin-permission gate enforced in ApiKeyAuthMiddleware
for all /api/v1/admin/* paths.

Surface (read-only):
  GET /api/v1/admin/audit-log
       Tenant-scoped paginated listing. Platform-admin may filter by
       any tenant_id; non-platform-admin callers are forced to their
       own tenant_id.

  GET /api/v1/admin/audit-log/resource/{resource_type}/{resource_pk}
       History of a specific resource over time (per-resource history
       pane data source for Step 31 dashboards).

  GET /api/v1/admin/audit-log/actor/{actor_key_prefix}
       Forensic actor view; platform_admin only, cross-tenant by
       construction.

Security contract:
- No mutation routes. The audit log is append-only by API surface
  today; Phase 2 Commit 4 (worker DB role swap) completes the
  append-only guarantee at the DB-grant layer.
- Raw API keys are never returned. Only the 12-char actor_key_prefix.
- before_json / after_json are PASSED THROUGH A FIELD ALLOW-LIST per
  resource_type (see _SAFE_DIFF_KEYS below). Any key not in the
  allow-list for that resource_type is replaced with the literal
  string "<redacted>" so a future writer that accidentally records
  sensitive content cannot leak through this endpoint. Defense in
  depth: even an audit of every record() caller (P3 follow-up)
  cannot remove the need for this gate, because the threat model
  includes future writers we haven't shipped yet.
- Filter enums validated against ALLOWED_ACTIONS / ALLOWED_RESOURCE_TYPES
  with 400 on unknown values. No silent empty results that mask
  client bugs.
- Hard cap limit<=500 to prevent unbounded scans.
- Reuses ADMIN_RATE_LIMIT bucket — log-scraping abuse rate-limited.
- Cross-tenant filter override is logged (warning) — defense-in-depth
  attempt is itself audit-worthy.
"""
from __future__ import annotations

import logging
from typing import Annotated, Any, Iterable

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.api.deps import get_admin_audit_repository
from app.middleware.rate_limit import (
    ADMIN_RATE_LIMIT,
    get_api_key_or_ip,
    limiter,
)
from app.models.admin_audit_log import (
    ALLOWED_ACTIONS,
    ALLOWED_RESOURCE_TYPES,
    RESOURCE_AGENT,
    RESOURCE_API_KEY,
    RESOURCE_DOMAIN,
    RESOURCE_KNOWLEDGE,
    RESOURCE_LUCIEL_INSTANCE,
    RESOURCE_MEMORY,
    RESOURCE_RETENTION_POLICY,
    RESOURCE_SCOPE_ASSIGNMENT,
    RESOURCE_TENANT,
    RESOURCE_USER,
    AdminAuditLog,
)
from app.policy.scope import ScopePolicy
from app.repositories.admin_audit_repository import (
    SYSTEM_ACTOR_TENANT,
    AdminAuditRepository,
)
from app.schemas.audit_log import AdminAuditLogPage, AdminAuditLogRead


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/admin/audit-log", tags=["admin", "audit-log"])


# Hard cap to prevent unbounded scans even with the supporting indexes.
MAX_LIMIT = 500
DEFAULT_LIMIT = 100


# ---------------------------------------------------------------------
# Field allow-list per resource_type
# ---------------------------------------------------------------------
#
# Defense-in-depth filter applied to before_json / after_json before
# returning to any caller. The READ side enforces what fields are
# safe to surface, regardless of what any current OR future writer
# decides to stuff into those JSONB blobs.
#
# Adding a new resource_type? Default to a minimal allow-list. It is
# always safer to omit a field than to leak one. Operators who need
# additional fields surfaced should add them here AFTER reviewing
# whether the field is PII / secret / confidential.
#
# Keys NOT in the allow-list are replaced with the string
# "<redacted>" rather than dropped entirely, so callers can see a
# field WAS present but cannot read its value. This makes
# audit-of-the-audit possible.

# Common safe keys for soft-delete and create flows.
_COMMON_SAFE_KEYS = frozenset({"active", "id"})

_SAFE_DIFF_KEYS: dict[str, frozenset[str]] = {
    RESOURCE_TENANT: _COMMON_SAFE_KEYS | {
        "tenant_id",
        "display_name",
        "description",
        "allowed_domains",
        # NOTE: escalation_contact, system_prompt_additions are
        # intentionally NOT here — escalation_contact is a contact
        # email (PII), system_prompt_additions can be anything.
    },
    RESOURCE_DOMAIN: _COMMON_SAFE_KEYS | {
        "tenant_id",
        "domain_id",
        "display_name",
    },
    RESOURCE_AGENT: _COMMON_SAFE_KEYS | {
        "tenant_id",
        "domain_id",
        "agent_id",
        "user_id",  # platform User UUID (not email)
    },
    RESOURCE_LUCIEL_INSTANCE: _COMMON_SAFE_KEYS | {
        "tenant_id",
        "domain_id",
        "agent_id",
        "instance_id",
        "luciel_instance_id",
        # NOTE: persona / system_prompt content NOT included.
    },
    RESOURCE_API_KEY: _COMMON_SAFE_KEYS | {
        "tenant_id",
        "domain_id",
        "agent_id",
        "luciel_instance_id",
        "key_prefix",       # safe, 12-char prefix only
        "permissions",
        "created_by",       # operator label
        # NOTE: raw_key, key_hash MUST NEVER appear here. If they do,
        # the redaction filter masks them.
    },
    RESOURCE_KNOWLEDGE: _COMMON_SAFE_KEYS | {
        "tenant_id",
        "domain_id",
        "source_id",
        "version",
        "chunk_count",
        # NOTE: chunk content / embedding vectors NOT included.
    },
    RESOURCE_RETENTION_POLICY: _COMMON_SAFE_KEYS | {
        "tenant_id",
        "category",
        "cutoff_days",
        "cutoff_date",
    },
    RESOURCE_MEMORY: _COMMON_SAFE_KEYS | {
        "tenant_id",
        "agent_id",
        "luciel_instance_id",
        "category",
        "session_id",
        "message_id",
        "trace_id",
        "actor_key_prefix",
        # NOTE: memory_items.content (the inferred user fact)
        # explicitly NOT here — that's the most sensitive field
        # in the whole system per recap §2.3.
    },
    RESOURCE_USER: _COMMON_SAFE_KEYS | {
        "synthetic",
        "display_name",
        # NOTE: email is PII. We surface a synthetic flag so an
        # auditor can see WHICH KIND of user record was created
        # without seeing the raw address. Tenant-admin and platform
        # admin who legitimately need the email can fetch it via
        # the dedicated /api/v1/admin/users endpoints with their
        # own scope checks.
    },
    RESOURCE_SCOPE_ASSIGNMENT: _COMMON_SAFE_KEYS | {
        "user_id",
        "tenant_id",
        "domain_id",
        "agent_id",
        "role",
    },
}


_REDACTED = "<redacted>"


def _filter_diff(
    blob: dict[str, Any] | None,
    resource_type: str,
) -> dict[str, Any] | None:
    """Return a shallow copy of blob with disallowed keys replaced by
    the literal '<redacted>' string. Returns None unchanged.

    This is intentionally NOT a deep walk: nested values are passed
    through opaquely. If a writer ever places sensitive content
    inside a nested structure under a safe key, that nested content
    will leak. The mitigation is the writer-side audit (P3 follow-up)
    — but the top-level allow-list catches the common case where a
    writer dumps a whole row dict (e.g. user_repository.py line 121
    putting `email` at the top level).
    """
    if blob is None:
        return None
    safe_keys = _SAFE_DIFF_KEYS.get(resource_type, frozenset())
    return {
        key: (value if key in safe_keys else _REDACTED)
        for key, value in blob.items()
    }


def _to_read(row: AdminAuditLog) -> AdminAuditLogRead:
    """Build the read DTO with diff-filtering applied."""
    return AdminAuditLogRead(
        id=row.id,
        created_at=row.created_at,
        actor_key_prefix=row.actor_key_prefix,
        actor_permissions=row.actor_permissions,
        actor_label=row.actor_label,
        tenant_id=row.tenant_id,
        domain_id=row.domain_id,
        agent_id=row.agent_id,
        luciel_instance_id=row.luciel_instance_id,
        action=row.action,
        resource_type=row.resource_type,
        resource_pk=row.resource_pk,
        resource_natural_id=row.resource_natural_id,
        before_json=_filter_diff(row.before_json, row.resource_type),
        after_json=_filter_diff(row.after_json, row.resource_type),
        note=row.note,
    )


def _validate_actions(actions: Iterable[str] | None) -> tuple[str, ...] | None:
    if not actions:
        return None
    bad = [a for a in actions if a not in ALLOWED_ACTIONS]
    if bad:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown action filter values: {bad}",
        )
    return tuple(actions)


def _validate_resource_types(resource_types: Iterable[str] | None) -> tuple[str, ...] | None:
    if not resource_types:
        return None
    bad = [r for r in resource_types if r not in ALLOWED_RESOURCE_TYPES]
    if bad:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown resource_type filter values: {bad}",
        )
    return tuple(resource_types)


# ---------------------------------------------------------------------
# GET /admin/audit-log
# ---------------------------------------------------------------------

@router.get("", response_model=AdminAuditLogPage)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def list_audit_log(
    request: Request,
    repo: Annotated[AdminAuditRepository, Depends(get_admin_audit_repository)],
    tenant_id: str | None = Query(
        default=None,
        min_length=2,
        max_length=100,
    ),
    action: list[str] | None = Query(default=None),
    resource_type: list[str] | None = Query(default=None),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> AdminAuditLogPage:
    """List audit rows for a tenant, most-recent-first.

    Authorization:
      - Platform-admin: may query any tenant. If `tenant_id` is
        omitted, defaults to the platform sentinel so platform-level
        system actions are surfaced.
      - Tenant-admin (or any non-platform-admin admin): tenant_id is
        forced to the caller's own tenant_id. Any value passed in the
        query string is ignored; the override attempt is logged at
        WARNING level for forensic review.
    """
    # Defense-in-depth: tenant scoping at the API layer in addition
    # to the middleware's admin-perm check.
    if not ScopePolicy.is_platform_admin(request):
        caller_tenant = getattr(request.state, "tenant_id", None)
        if caller_tenant is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Caller has no tenant_id on the API key; cannot "
                    "infer which tenant's audit log to return."
                ),
            )
        # Log scope-override attempts so a tenant_admin who tries to
        # read another tenant's audit log leaves a forensic trail.
        # The actual response still comes back (filtered to their
        # own tenant), but the attempt is captured in CloudWatch.
        if tenant_id is not None and tenant_id != caller_tenant:
            logger.warning(
                "audit-log: tenant scope override — caller_tenant=%s "
                "requested_tenant_id=%s key_prefix=%s",
                caller_tenant,
                tenant_id,
                getattr(request.state, "key_prefix", "<unknown>"),
            )
        tenant_id = caller_tenant
    else:
        # Platform-admin: default to platform-sentinel tenant when
        # no filter supplied, to surface system actions.
        if tenant_id is None:
            tenant_id = SYSTEM_ACTOR_TENANT

    actions_tuple = _validate_actions(action)
    resource_types_tuple = _validate_resource_types(resource_type)

    rows: list[AdminAuditLog] = repo.list_for_tenant(
        tenant_id=tenant_id,
        limit=limit,
        offset=offset,
        actions=actions_tuple,
        resource_types=resource_types_tuple,
    )

    items = [_to_read(r) for r in rows]
    return AdminAuditLogPage(
        items=items,
        limit=limit,
        offset=offset,
        returned=len(items),
    )


# ---------------------------------------------------------------------
# GET /admin/audit-log/resource/{resource_type}/{resource_pk}
# ---------------------------------------------------------------------

@router.get(
    "/resource/{resource_type}/{resource_pk}",
    response_model=list[AdminAuditLogRead],
)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def list_audit_log_for_resource(
    request: Request,
    resource_type: str,
    resource_pk: int,
    repo: Annotated[AdminAuditRepository, Depends(get_admin_audit_repository)],
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
) -> list[AdminAuditLogRead]:
    """All audit events for one specific resource over time.

    Authorization:
      - Platform-admin: full access.
      - Non-platform-admin: results are post-filtered to the caller's
        tenant_id. Cross-tenant rows are dropped silently from the
        response (they shouldn't appear, but defense in depth).
    """
    if resource_type not in ALLOWED_RESOURCE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown resource_type {resource_type!r}",
        )

    rows: list[AdminAuditLog] = repo.list_for_resource(
        resource_type=resource_type,
        resource_pk=resource_pk,
        limit=limit,
    )

    if not ScopePolicy.is_platform_admin(request):
        caller_tenant = getattr(request.state, "tenant_id", None)
        if caller_tenant is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Caller has no tenant_id on the API key; cannot "
                    "filter resource audit history."
                ),
            )
        # Drop cross-tenant rows defensively. resource_pk is
        # (resource_type, pk)-unique within a tenant by construction;
        # this filter shouldn't change the count, but if it ever does
        # it indicates a model bug worth catching here, not at the
        # caller's screen.
        leaked = [r for r in rows if r.tenant_id != caller_tenant]
        if leaked:
            logger.error(
                "audit-log: resource-scope leak guard tripped — "
                "caller_tenant=%s resource=%s/%s leaked_count=%d",
                caller_tenant, resource_type, resource_pk, len(leaked),
            )
        rows = [r for r in rows if r.tenant_id == caller_tenant]

    return [_to_read(r) for r in rows]


# ---------------------------------------------------------------------
# GET /admin/audit-log/actor/{actor_key_prefix}
# ---------------------------------------------------------------------

@router.get(
    "/actor/{actor_key_prefix}",
    response_model=list[AdminAuditLogRead],
)
@limiter.limit(ADMIN_RATE_LIMIT, key_func=get_api_key_or_ip)
def list_audit_log_for_actor(
    request: Request,
    actor_key_prefix: str,
    repo: Annotated[AdminAuditRepository, Depends(get_admin_audit_repository)],
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
) -> list[AdminAuditLogRead]:
    """Forensic view: every row written by an actor key prefix.

    Cross-tenant by definition. Platform-admin only.
    """
    if not ScopePolicy.is_platform_admin(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Only platform_admin may query audit rows by actor "
                "key prefix (cross-tenant by construction)."
            ),
        )

    if not actor_key_prefix or len(actor_key_prefix) > 20:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="actor_key_prefix must be 1..20 characters",
        )

    rows: list[AdminAuditLog] = repo.list_for_actor(
        actor_key_prefix=actor_key_prefix,
        limit=limit,
    )
    return [_to_read(r) for r in rows]
