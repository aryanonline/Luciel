"""Cluster 1 contract tests for Step 29.y per-route scope hardening.

These are structural / AST tests. They do NOT require a live backend,
Postgres, Redis, or any infrastructure. They lock the invariants that
findings_phase1g.md G-3, G-4, G-5, G-6, G-7 introduced into the four
route files plus admin_audit_log.py:

  * G-3: consent routes have rate-limit + scope + audit on grant/withdraw
  * G-4: session routes have rate-limit + cross-tenant audit
  * G-5: retention routes have rate-limit + scope on every route
         + audit-before-mutate for every write path
  * G-6: verification teardown-integrity has rate-limit
  * G-7: consent.py has no UTF-8 BOM and no smart characters

A failure here means a future maintainer has accidentally undone one of
the Cluster 1 hardening steps. Each assertion message names the finding
ID so the bisect is one-step.

These tests intentionally do NOT exercise FastAPI routing or call
endpoints. The invariants checked are syntactic ("is this decorator
present", "is this audit call before this mutation"), not runtime --
which is how they survive a missing-backend CI environment.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def _read_bytes(rel: str) -> bytes:
    return (REPO_ROOT / rel).read_bytes()


def _parse(rel: str) -> ast.Module:
    return ast.parse(_read(rel), filename=rel)


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found in module")


def _decorator_call_names(fn: ast.FunctionDef) -> list[str]:
    """Return a list like ['router.post', 'limiter.limit'] for each decorator.

    Handles ``@x.y(...)`` (Call wrapping Attribute) and ``@x.y`` (bare
    Attribute) and ``@x()`` / ``@x`` (Name).
    """
    out: list[str] = []
    for d in fn.decorator_list:
        if isinstance(d, ast.Call):
            d = d.func
        if isinstance(d, ast.Attribute):
            base = d.value.id if isinstance(d.value, ast.Name) else "?"
            out.append(f"{base}.{d.attr}")
        elif isinstance(d, ast.Name):
            out.append(d.id)
    return out


def _has_rate_limit(fn: ast.FunctionDef) -> bool:
    return "limiter.limit" in _decorator_call_names(fn)


def _audit_record_lineno(fn: ast.FunctionDef) -> int | None:
    """Return the first lineno where ``audit_repo.record(...)`` is called.

    Used to assert the audit-before-mutate invariant: the audit call
    must appear at a lower lineno than any mutating call (repo.create,
    repo.update, repo.delete, service.manual_purge, etc.).
    """
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if (
                node.func.attr == "record"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "audit_repo"
            ):
                return node.lineno
    return None


def _first_call_lineno(fn: ast.FunctionDef, predicate) -> int | None:
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and predicate(node):
            return node.lineno
    return None


# =====================================================================
# G-7 — consent.py BOM + smart-character cleanup
# =====================================================================


def test_g7_consent_no_utf8_bom():
    raw = _read_bytes("app/api/v1/consent.py")
    assert not raw.startswith(b"\xef\xbb\xbf"), (
        "G-7: consent.py must not start with a UTF-8 BOM. "
        "The file was previously saved by a Windows editor; the "
        "Step 29.y Cluster 1 rewrite removed the BOM."
    )


def test_g7_consent_no_smart_characters():
    raw = _read_bytes("app/api/v1/consent.py")
    # mojibake of em-dash (â€”) is the specific shape findings_phase1g
    # G-7 called out. We also reject curly quotes and the actual em-dash
    # because the rest of the codebase is ASCII-only by convention.
    forbidden = [b"\xe2\x80\x94", b"\xe2\x80\x99", b"\xe2\x80\x9c", b"\xe2\x80\x9d"]
    for token in forbidden:
        assert token not in raw, (
            f"G-7: consent.py must be ASCII-only but contains {token!r}. "
            "The Step 29.y Cluster 1 rewrite normalized smart characters."
        )


# =====================================================================
# G-3 — consent.py routes
# =====================================================================


def test_g3_consent_grant_has_rate_limit():
    fn = _find_function(_parse("app/api/v1/consent.py"), "grant_consent")
    assert _has_rate_limit(fn), "G-3: POST /consent/grant missing @limiter.limit"


def test_g3_consent_withdraw_has_rate_limit():
    fn = _find_function(_parse("app/api/v1/consent.py"), "withdraw_consent")
    assert _has_rate_limit(fn), "G-3: POST /consent/withdraw missing @limiter.limit"


def test_g3_consent_status_has_rate_limit():
    fn = _find_function(_parse("app/api/v1/consent.py"), "consent_status")
    assert _has_rate_limit(fn), "G-3: GET /consent/status missing @limiter.limit"


def test_g3_consent_grant_audits_before_mutating():
    """audit_repo.record() must appear before repo.grant_consent()."""
    fn = _find_function(_parse("app/api/v1/consent.py"), "grant_consent")
    audit_ln = _audit_record_lineno(fn)
    grant_ln = _first_call_lineno(
        fn,
        lambda n: (
            isinstance(n.func, ast.Attribute)
            and n.func.attr == "grant_consent"
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "repo"
        ),
    )
    assert audit_ln is not None, "G-3: grant_consent has no audit_repo.record() call"
    assert grant_ln is not None, "G-3: grant_consent has no repo.grant_consent() call"
    assert audit_ln < grant_ln, (
        f"G-3: audit_repo.record() at line {audit_ln} must appear "
        f"before repo.grant_consent() at line {grant_ln}. "
        "Audit-before-mutate is the compliance invariant locked at "
        "admin_forensics.py line 779-800."
    )


def test_g3_consent_withdraw_audits_before_mutating():
    fn = _find_function(_parse("app/api/v1/consent.py"), "withdraw_consent")
    audit_ln = _audit_record_lineno(fn)
    withdraw_ln = _first_call_lineno(
        fn,
        lambda n: (
            isinstance(n.func, ast.Attribute)
            and n.func.attr == "withdraw_consent"
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "repo"
        ),
    )
    assert audit_ln is not None
    assert withdraw_ln is not None
    assert audit_ln < withdraw_ln, (
        f"G-3: audit_repo.record() at line {audit_ln} must appear "
        f"before repo.withdraw_consent() at line {withdraw_ln}."
    )


def test_g3_consent_status_does_not_audit():
    """Read-only routes must not write audit rows (volume noise)."""
    fn = _find_function(_parse("app/api/v1/consent.py"), "consent_status")
    assert _audit_record_lineno(fn) is None, (
        "G-3: GET /consent/status must NOT call audit_repo.record(). "
        "Read-only routes are intentionally non-audited (matches the "
        "convention used by GET /admin/audit-log and GET /admin/verification)."
    )


def test_g3_consent_uses_scope_policy():
    """consent.py must reference ScopePolicy (the cross-tenant guard)."""
    src = _read("app/api/v1/consent.py")
    assert "ScopePolicy" in src, (
        "G-3: consent.py must use ScopePolicy to gate cross-tenant access."
    )


# =====================================================================
# G-4 — sessions.py routes
# =====================================================================


@pytest.mark.parametrize(
    "fn_name",
    ["create_session", "list_sessions", "get_session", "list_messages"],
)
def test_g4_sessions_routes_have_rate_limit(fn_name):
    fn = _find_function(_parse("app/api/v1/sessions.py"), fn_name)
    assert _has_rate_limit(fn), (
        f"G-4: sessions.{fn_name} missing @limiter.limit decorator. "
        "All four session routes must be rate-limited."
    )


def test_g4_create_session_audits_cross_tenant():
    """create_session must include an audit_repo.record() call gated on
    the cross-tenant flag (the privileged platform_admin path)."""
    fn = _find_function(_parse("app/api/v1/sessions.py"), "create_session")
    assert _audit_record_lineno(fn) is not None, (
        "G-4: create_session must call audit_repo.record() to capture "
        "the privileged platform_admin cross-tenant creation case."
    )


def test_g4_create_session_uses_scope_policy():
    src = _read("app/api/v1/sessions.py")
    assert "ScopePolicy" in src, (
        "G-4: sessions.py must reference ScopePolicy to detect platform_admin."
    )


def test_g4_action_session_create_cross_tenant_imported():
    src = _read("app/api/v1/sessions.py")
    assert "ACTION_SESSION_CREATE_CROSS_TENANT" in src, (
        "G-4: the audit row must use ACTION_SESSION_CREATE_CROSS_TENANT, "
        "not a generic ACTION_CREATE."
    )


# =====================================================================
# G-5 — retention.py routes
# =====================================================================


_RETENTION_ROUTES = [
    "create_policy",
    "list_policies",
    "get_policy",
    "update_policy",
    "delete_policy",
    "list_logs",
    "enforce_policies",
    "manual_purge",
]


@pytest.mark.parametrize("fn_name", _RETENTION_ROUTES)
def test_g5_retention_routes_have_rate_limit(fn_name):
    fn = _find_function(_parse("app/api/v1/retention.py"), fn_name)
    assert _has_rate_limit(fn), (
        f"G-5: retention.{fn_name} missing @limiter.limit decorator."
    )


_RETENTION_MUTATING_ROUTES = [
    ("create_policy", "create_policy"),     # repo.create_policy
    ("update_policy", "update_policy"),     # repo.update_policy
    ("delete_policy", "delete_policy"),     # repo.delete_policy
    ("manual_purge", "manual_purge"),       # service.manual_purge
    ("enforce_policies", "enforce_for_tenant"),  # service.enforce_for_tenant
]


@pytest.mark.parametrize("fn_name,mutator_attr", _RETENTION_MUTATING_ROUTES)
def test_g5_retention_mutating_routes_audit_before_mutate(fn_name, mutator_attr):
    fn = _find_function(_parse("app/api/v1/retention.py"), fn_name)
    audit_ln = _audit_record_lineno(fn)
    mutator_ln = _first_call_lineno(
        fn,
        lambda n: (
            isinstance(n.func, ast.Attribute) and n.func.attr == mutator_attr
        ),
    )
    assert audit_ln is not None, (
        f"G-5: retention.{fn_name} missing audit_repo.record() call"
    )
    assert mutator_ln is not None, (
        f"G-5: retention.{fn_name} missing call to {mutator_attr}()"
    )
    assert audit_ln < mutator_ln, (
        f"G-5: in retention.{fn_name}, audit_repo.record() at line "
        f"{audit_ln} must appear before {mutator_attr}() at line "
        f"{mutator_ln}. This is the audit-before-mutate compliance "
        "invariant (admin_forensics.py:779-800)."
    )


@pytest.mark.parametrize(
    "fn_name", ["list_policies", "get_policy", "list_logs"]
)
def test_g5_retention_read_routes_do_not_audit(fn_name):
    fn = _find_function(_parse("app/api/v1/retention.py"), fn_name)
    assert _audit_record_lineno(fn) is None, (
        f"G-5: read-only retention.{fn_name} must not write audit rows."
    )


def test_g5_retention_uses_scope_policy():
    src = _read("app/api/v1/retention.py")
    assert "ScopePolicy" in src, (
        "G-5: retention.py must reference ScopePolicy to gate "
        "cross-tenant access on every route."
    )


# =====================================================================
# G-6 — verification teardown-integrity rate limit
# =====================================================================


def test_g6_teardown_integrity_has_rate_limit():
    fn = _find_function(
        _parse("app/api/v1/verification.py"), "teardown_integrity"
    )
    assert _has_rate_limit(fn), (
        "G-6: GET /admin/verification/teardown-integrity must carry "
        "@limiter.limit(ADMIN_RATE_LIMIT). Without it, a misconfigured "
        "platform_admin key (or a runaway verify harness) can DoS the "
        "database by polling N COUNT(*) queries unboundedly."
    )


# =====================================================================
# Audit-constants registry (admin_audit_log.py)
# =====================================================================


_NEW_ACTIONS = [
    "ACTION_CONSENT_GRANT",
    "ACTION_CONSENT_WITHDRAW",
    "ACTION_SESSION_CREATE_CROSS_TENANT",
    "ACTION_RETENTION_ENFORCE",
    "ACTION_RETENTION_MANUAL_PURGE",
]

_NEW_RESOURCES = ["RESOURCE_CONSENT", "RESOURCE_SESSION"]


@pytest.mark.parametrize("name", _NEW_ACTIONS)
def test_cluster1_action_in_allowed_actions(name):
    """Half-applied refactors that add the constant but forget to extend
    the ALLOWED_ACTIONS tuple silently break every audit_repo.record()
    call site at runtime (ValueError). Lock both shapes here."""
    from app.models import admin_audit_log as m

    val = getattr(m, name)
    assert val in m.ALLOWED_ACTIONS, (
        f"Cluster 1: {name}={val!r} is missing from ALLOWED_ACTIONS. "
        f"AdminAuditRepository.record() will reject every call until "
        f"the tuple is extended."
    )


@pytest.mark.parametrize("name", _NEW_RESOURCES)
def test_cluster1_resource_in_allowed_resource_types(name):
    from app.models import admin_audit_log as m

    val = getattr(m, name)
    assert val in m.ALLOWED_RESOURCE_TYPES, (
        f"Cluster 1: {name}={val!r} is missing from "
        f"ALLOWED_RESOURCE_TYPES. AdminAuditRepository.record() will "
        f"reject every call until the tuple is extended."
    )


# =====================================================================
# Module-level smoke: every patched module imports cleanly.
# =====================================================================


@pytest.mark.parametrize(
    "module",
    [
        "app.api.v1.consent",
        "app.api.v1.sessions",
        "app.api.v1.retention",
        "app.api.v1.verification",
    ],
)
def test_cluster1_modules_import(module):
    """If any Cluster 1 file has a NameError, ImportError, or
    syntax issue, this catches it without needing the rest of the
    suite. Failures here mean the patched module is unloadable
    end-to-end, not just structurally suspect."""
    import importlib

    importlib.import_module(module)
