"""
Verification endpoints for operational health checks and teardown integrity.

These endpoints power the app.verification suite (Step 26-redo, Pillar 10)
when it runs against a remote environment. By exposing the teardown-integrity
check as an admin API, the suite no longer needs direct DATABASE_URL access
to the target environment — it uses the same HTTPS + platform_admin auth path
as every other pillar.

Security:
- platform_admin ONLY. This endpoint can inspect ANY tenant's row counts
  across every app table, which is a cross-tenant info shape leak if given
  to tenant-scoped admins.
- Read-only: no schema changes, no row mutations, no side effects.
- Idempotent: repeated calls return the same observations.

PIPEDA posture:
- No PII returned (only tenant_id and aggregate counts).
- No raw user data, no message contents, no memory items.
- Audit: intentionally NOT logged to admin_audit_log — this endpoint is
  diagnostic, not a mutation, and logging every poll would noise the audit
  trail used for real admin actions (Invariant 4 applies to mutations).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import inspect, text

from app.api.deps import DbSession


router = APIRouter(prefix="/admin/verification", tags=["verification"])


# (table_name, active_col_or_None, expectation, tenant_col_candidates)
#
# Mirrors app/verification/tests/pillar_10_teardown_integrity.py exactly so
# the API and the original subprocess probe return byte-identical observations.
#
# expectation semantics:
#   0                              -> must be zero live rows for this tenant
#   "tenant_inactive_exactly_one"  -> exactly 1 row, soft-deactivated
#   "*"                            -> observe only, do not enforce
#
# tenant_col_candidates: first column that exists on the table is used to
# scope the count. If none match, table is flagged "no_tenant_scope" and
# skipped for enforcement (still observed).
_PROBES: list[tuple[str, str | None, Any, list[str]]] = [
    ("tenant_configs",       "active", "tenant_inactive_exactly_one", ["tenant_id"]),
    ("domain_configs",       "active", 0,                              ["tenant_id"]),
    ("agents",               "active", 0,                              ["tenant_id"]),
    ("luciel_instances",     "active", 0,                              ["scope_owner_tenant_id", "tenant_id"]),
    ("api_keys",             "active", 0,                              ["tenant_id"]),
    ("sessions",             None,     "*",                            ["tenant_id"]),
    ("messages",             None,     "*",                            ["tenant_id"]),
    ("traces",               None,     "*",                            ["tenant_id"]),
    ("memory_items",         None,     "*",                            ["tenant_id"]),
    ("user_consents",        None,     "*",                            ["tenant_id"]),
    ("knowledge_embeddings", None,     "*",                            ["tenant_id"]),
    ("retention_policies",   None,     "*",                            ["tenant_id"]),
    ("deletion_logs",        None,     "*",                            ["tenant_id"]),
    ("admin_audit_logs",     None,     "*",                            ["tenant_id"]),
    ("agent_configs",        None,     "*",                            ["tenant_id"]),
]


@router.get("/teardown-integrity")
def teardown_integrity(
    request: Request,
    db: DbSession,
    tenant_id: str = Query(
        ...,
        min_length=2,
        max_length=100,
        description="Tenant to audit post-teardown. Typically a throwaway "
                    "step26-verify-<uuid> tenant from the verification suite.",
    ),
) -> dict[str, Any]:
    """Audit post-teardown state for a single tenant across all app tables.

    Returns an observations + violations shape identical to the subprocess
    probe in pillar_10_teardown_integrity.py so the suite can drop in
    without changing its assertion logic.

    Platform-admin only. Read-only. No audit log row.
    """
    # Permission guard — platform_admin bypass is required.
    # Any admin-scoped key (tenant/domain/agent) is NOT enough: this endpoint
    # can inspect ANY tenant's shape.
    permissions = getattr(request.state, "permissions", []) or []
    if "platform_admin" not in permissions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only platform_admin may call verification/teardown-integrity",
        )

    insp = inspect(db.get_bind())
    existing_tables = set(insp.get_table_names())

    def _has_column(table: str, col: str) -> bool:
        try:
            return col in {c["name"] for c in insp.get_columns(table)}
        except Exception:
            return False

    observations: dict[str, Any] = {}
    violations: list[str] = []

    for table, active_col, expectation, tenant_col_candidates in _PROBES:
        if table not in existing_tables:
            observations[table] = "missing_table"
            continue

        tenant_col = next(
            (c for c in tenant_col_candidates if _has_column(table, c)),
            None,
        )
        if tenant_col is None:
            observations[table] = "no_tenant_scope"
            continue

        total = db.execute(
            text(f"SELECT COUNT(*) FROM {table} WHERE {tenant_col} = :tid"),
            {"tid": tenant_id},
        ).scalar_one()

        if active_col and _has_column(table, active_col):
            live = db.execute(
                text(
                    f"SELECT COUNT(*) FROM {table} "
                    f"WHERE {tenant_col} = :tid AND {active_col} = TRUE"
                ),
                {"tid": tenant_id},
            ).scalar_one()
        else:
            live = None

        observations[table] = {
            "total": total,
            "live": live,
            "scoped_by": tenant_col,
        }

        if expectation == 0:
            if live is None:
                violations.append(f"{table}: expected live=0 but no active column")
            elif live != 0:
                violations.append(
                    f"{table}: expected live=0, got live={live} "
                    f"(total={total}, scoped_by={tenant_col})"
                )
        elif expectation == "tenant_inactive_exactly_one":
            if total != 1:
                violations.append(f"{table}: expected exactly 1 row, got {total}")
            elif live not in (0, None):
                violations.append(f"{table}: expected inactive, got live={live}")
        # "*" expectations: observe, do not enforce

    return {
        "target_tenant": tenant_id,
        "violations": violations,
        "observations": observations,
        "passed": len(violations) == 0,
    }