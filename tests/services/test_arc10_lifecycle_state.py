"""Arc 10 re-open Gap 1 -- ClosureService.get_lifecycle_state contract.

Protects the server-sourced lifecycle-state surface that replaces the
original arc's localStorage-only client read. The contract is:

  C1  get_lifecycle_state exists as a public method on ClosureService
      and takes a single admin_id argument.

  C2  The method returns a LifecycleState dataclass with eight fields
      matching LifecycleStateResponse 1:1 (admin_id, closed, in_grace,
      hard_deleted, cancel_mode, closure_initiated_at,
      grace_window_expires_at, hard_deleted_at).

  C3  in_grace is computed as (closed AND not hard_deleted AND
      now() < grace_window_expires_at). The two-failure-mode case
      where closed and hard_deleted are both true (post-tombstone)
      MUST yield in_grace=false.

  C4  grace_window_expires_at = closure_initiated_at + GRACE_WINDOW_DAYS.
      Sourced from the single GRACE_WINDOW_DAYS module constant; the
      method must not invent a separate window.

  C5  AccountNotFoundError exists and is raised when the admin_id
      does not resolve. The route (admin.py) maps this to HTTP 404.

  C6  The route GET /admin/account/lifecycle-state exists with
      response_model=LifecycleStateResponse and uses _require_admin_id
      for auth -- same auth posture as every other /admin/account/*
      route.

Test strategy: AST + text assertions on the shipped source. No DB.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
CLOSURE_SERVICE_PATH = REPO_ROOT / "app" / "lifecycle" / "closure.py"
ADMIN_ROUTE_PATH = REPO_ROOT / "app" / "api" / "v1" / "admin" / "__init__.py"
SCHEMA_PATH = REPO_ROOT / "app" / "schemas" / "lifecycle.py"


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------
# C1: get_lifecycle_state exists on ClosureService.
# ---------------------------------------------------------------------

def test_closure_service_has_get_lifecycle_state():
    tree = _parse(CLOSURE_SERVICE_PATH)
    closure_cls = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == "ClosureService"
    )
    method_names = {
        n.name for n in closure_cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "get_lifecycle_state" in method_names, (
        "Arc 10 re-open Gap 1: ClosureService must expose "
        "get_lifecycle_state(admin_id) as a public read-only method."
    )


def test_get_lifecycle_state_signature_admin_id_only():
    tree = _parse(CLOSURE_SERVICE_PATH)
    method = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "get_lifecycle_state"
    )
    args = [a.arg for a in method.args.args]
    assert args == ["self", "admin_id"], (
        f"get_lifecycle_state must take exactly (self, admin_id), got {args}. "
        "Extra arguments would couple lifecycle reads to billing or other "
        "concerns and break the read-only contract."
    )


# ---------------------------------------------------------------------
# C2: LifecycleState dataclass shape.
# ---------------------------------------------------------------------

def test_lifecycle_state_dataclass_eight_fields():
    tree = _parse(CLOSURE_SERVICE_PATH)
    cls = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == "LifecycleState"
    )
    annotated = [
        n.target.id for n in cls.body
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
    ]
    expected = [
        "admin_id",
        "closed",
        "in_grace",
        "hard_deleted",
        "cancel_mode",
        "closure_initiated_at",
        "grace_window_expires_at",
        "hard_deleted_at",
    ]
    assert annotated == expected, (
        f"LifecycleState must declare exactly {expected} in order; got {annotated}. "
        "Field order matches LifecycleStateResponse so the route can pass "
        "fields through positionally without ambiguity."
    )


def test_lifecycle_state_response_schema_matches_dataclass():
    tree = _parse(SCHEMA_PATH)
    cls = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == "LifecycleStateResponse"
    )
    annotated = [
        n.target.id for n in cls.body
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
    ]
    expected = [
        "admin_id",
        "closed",
        "in_grace",
        "hard_deleted",
        "cancel_mode",
        "closure_initiated_at",
        "grace_window_expires_at",
        "hard_deleted_at",
    ]
    assert annotated == expected, (
        "LifecycleStateResponse fields must match LifecycleState dataclass 1:1. "
        f"Got {annotated}, expected {expected}."
    )


# ---------------------------------------------------------------------
# C3: in_grace computation excludes hard_deleted admins.
# ---------------------------------------------------------------------

def test_in_grace_excludes_hard_deleted():
    src = CLOSURE_SERVICE_PATH.read_text(encoding="utf-8")
    # The method must include 'not hard_deleted' in its in_grace logic.
    # Looking for the exact pattern from get_lifecycle_state.
    method_match = re.search(
        r"def get_lifecycle_state\(.*?\)(.*?)(?=\n    def |\n\nclass |\Z)",
        src, re.DOTALL,
    )
    assert method_match, "could not locate get_lifecycle_state body"
    body = method_match.group(1)
    assert "not hard_deleted" in body, (
        "in_grace computation must exclude hard_deleted admins. "
        "Vision 6.5: a tombstoned admin (hard_deleted_at IS NOT NULL) is "
        "past the grace window by definition and the banner must not "
        "render a misleading 'reactivate' CTA for them."
    )


def test_in_grace_compares_against_grace_window_expires_at():
    src = CLOSURE_SERVICE_PATH.read_text(encoding="utf-8")
    method_match = re.search(
        r"def get_lifecycle_state\(.*?\)(.*?)(?=\n    def |\n\nclass |\Z)",
        src, re.DOTALL,
    )
    body = method_match.group(1)
    # The window comparison should reference grace_window_expires_at,
    # not a re-computation from closure_initiated_at + delta inline.
    # This keeps the single-source-of-truth posture.
    assert "now < grace_window_expires_at" in body, (
        "in_grace must compare now() < grace_window_expires_at. The window "
        "value is computed once via _add_days(..., GRACE_WINDOW_DAYS) and "
        "reused; do not inline a second timedelta."
    )


# ---------------------------------------------------------------------
# C4: grace_window_expires_at sources GRACE_WINDOW_DAYS, not a hardcode.
# ---------------------------------------------------------------------

def test_grace_window_sources_module_constant():
    src = CLOSURE_SERVICE_PATH.read_text(encoding="utf-8")
    method_match = re.search(
        r"def get_lifecycle_state\(.*?\)(.*?)(?=\n    def |\n\nclass |\Z)",
        src, re.DOTALL,
    )
    body = method_match.group(1)
    assert "GRACE_WINDOW_DAYS" in body, (
        "grace_window_expires_at must be computed via _add_days(..., "
        "GRACE_WINDOW_DAYS). Hardcoded 30s would drift from the single "
        "source of truth at the top of closure_service.py."
    )
    # And the literal 30 must NOT appear adjacent to the computation
    # (defense against someone helpfully inlining a magic number).
    # We allow '30' to appear in docstrings/comments above.
    # The assignment may be split across lines:
    #   grace_window_expires_at = _add_days(
    #       closure_initiated_at, GRACE_WINDOW_DAYS
    #   )
    # So we collapse the body into a single logical string and check
    # the multi-token expression as one unit.
    flat = re.sub(r"\s+", " ", body)
    assert re.search(
        r"grace_window_expires_at\s*=\s*_add_days\([^)]*GRACE_WINDOW_DAYS[^)]*\)",
        flat,
    ), (
        "expected 'grace_window_expires_at = _add_days(..., GRACE_WINDOW_DAYS)' "
        "in the method body. Hardcoded values like _add_days(now, 30) would "
        "drift from the single source of truth."
    )


# ---------------------------------------------------------------------
# C5: AccountNotFoundError exists and is part of the closure-error tree.
# ---------------------------------------------------------------------

def test_account_not_found_error_exists():
    tree = _parse(CLOSURE_SERVICE_PATH)
    classes = {
        n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)
    }
    assert "AccountNotFoundError" in classes, (
        "Gap 1: get_lifecycle_state must raise AccountNotFoundError when "
        "the admin_id does not resolve. The route maps this to HTTP 404."
    )


def test_account_not_found_inherits_account_closure_error():
    tree = _parse(CLOSURE_SERVICE_PATH)
    cls = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == "AccountNotFoundError"
    )
    base_names = [b.id for b in cls.bases if isinstance(b, ast.Name)]
    assert "AccountClosureError" in base_names, (
        "AccountNotFoundError must extend AccountClosureError so callers "
        "can catch the whole closure-error family with one except clause."
    )


# ---------------------------------------------------------------------
# C6: Route surface (GET /admin/account/lifecycle-state).
# ---------------------------------------------------------------------

def test_lifecycle_state_route_exists():
    src = ADMIN_ROUTE_PATH.read_text(encoding="utf-8")
    assert '"/account/lifecycle-state"' in src, (
        "Arc 10 Gap 1: GET /admin/account/lifecycle-state must exist on "
        "the admin router."
    )


def test_lifecycle_state_route_uses_correct_response_model():
    src = ADMIN_ROUTE_PATH.read_text(encoding="utf-8")
    # Find the decorator block for the route
    pat = re.search(
        r'@router\.get\(\s*"/account/lifecycle-state"[^)]*response_model=([A-Za-z_]+)',
        src, re.DOTALL,
    )
    assert pat, "could not parse lifecycle-state route decorator"
    assert pat.group(1) == "LifecycleStateResponse", (
        f"response_model must be LifecycleStateResponse, got {pat.group(1)}."
    )


def test_lifecycle_state_route_requires_admin_auth():
    src = ADMIN_ROUTE_PATH.read_text(encoding="utf-8")
    # Find the function body
    fn = re.search(
        r"def get_lifecycle_state\([^)]*\)[^:]*:(.*?)(?=\n@router|\nclass |\Z)",
        src, re.DOTALL,
    )
    assert fn, "could not locate get_lifecycle_state route function"
    body = fn.group(1)
    assert "_require_admin_id(request)" in body, (
        "Route must call _require_admin_id(request) -- consistent with every "
        "other /admin/account/* route. Reading lifecycle state without an "
        "admin scope is meaningless and would leak the existence of an "
        "admin_id to an unauthenticated probe."
    )
