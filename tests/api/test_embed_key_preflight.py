"""
Step 30d Deliverable A -- regression tests for ScopePromptPreflight
and its wiring into both issuance surfaces.

Test strategy
=============

The repo has no pytest fixture for FastAPI TestClient or a test DB
(every existing test in tests/api/ is either AST-based or uses inline
fakes). We deliberately follow that house style here rather than
introducing a TestClient harness in the same commit that ships the
feature -- the runtime / E2E harness lands in Step 30d Deliverable C.

This file therefore has two groups of tests:

  AST tests
    Pin that the preflight is wired into the right places in the right
    order. These survive future refactors of the surrounding code as
    long as the structural contract holds: anyone who reorders the
    calls, drops the warning, or weakens the exception handling fails
    these tests immediately. Same anti-regression pattern used by
    tests/api/test_step29y_cluster6_chat_stream_sanitization.py.

  Behavioural tests
    Drive ScopePromptPreflight.check directly with an in-memory fake
    DB session (no SQLAlchemy engine, no Postgres). The check is a
    pure two-step lookup; a fake session is sufficient to pin every
    branch.

Why these tests are sufficient
==============================

  * The wired call sites are AST-pinned at file-level granularity:
    POST /admin/embed-keys and scripts/mint_embed_key.py.
  * The preflight's three failure branches (missing row, NULL prompt,
    empty/whitespace prompt) are behaviour-tested.
  * The Invariant 4 guarantee (audit row inside ApiKeyService.create_key)
    is preserved structurally: preflight runs before service.create_key
    in both surfaces, so a preflight failure cannot leave a half-written
    api_keys row -- the AST tests pin that ordering directly.

References
==========

  * app/services/scope_prompt_preflight.py -- the unit under test.
  * app/api/v1/admin.py POST /admin/embed-keys -- HTTP surface.
  * scripts/mint_embed_key.py -- CLI surface.
  * scripts/audit_widget_scope.py -- runtime audit complement.
  * ARCHITECTURE.md \u00a73.2.2 'Issuance' -- the design statement these
    tests defend.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

from app.services.scope_prompt_preflight import (
    ScopePromptMissingError,
    ScopePromptPreflight,
)


_HERE = pathlib.Path(__file__).resolve()
_PROJECT_ROOT = _HERE.parents[2]


def _read(rel: str) -> str:
    return (_PROJECT_ROOT / rel).read_text()


def _parse(rel: str) -> ast.Module:
    return ast.parse(_read(rel))


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found")


def _has_call_to(node: ast.AST, dotted_name: str) -> bool:
    """True iff a Call node anywhere under `node` matches dotted_name.

    Supports either bare names ("foo") or single-level attribute
    access ("Cls.method"). Sufficient for the patterns we pin here.
    """

    parts = dotted_name.split(".")
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if len(parts) == 1:
            if isinstance(func, ast.Name) and func.id == parts[0]:
                return True
        elif len(parts) == 2:
            if (
                isinstance(func, ast.Attribute)
                and func.attr == parts[1]
                and isinstance(func.value, ast.Name)
                and func.value.id == parts[0]
            ):
                return True
    return False


def _first_lineno_of_call(node: ast.AST, dotted_name: str) -> int | None:
    parts = dotted_name.split(".")
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if len(parts) == 1:
            if isinstance(func, ast.Name) and func.id == parts[0]:
                return child.lineno
        elif len(parts) == 2:
            if (
                isinstance(func, ast.Attribute)
                and func.attr == parts[1]
                and isinstance(func.value, ast.Name)
                and func.value.id == parts[0]
            ):
                return child.lineno
    return None


# =====================================================================
# AST tests: HTTP admin route
# =====================================================================


def test_admin_route_imports_preflight() -> None:
    """The admin module must import the preflight symbols.

    A maintainer who deletes the wiring will usually also delete the
    import -- this test fails loud the moment that happens, even if
    the function body is otherwise unchanged.
    """

    src = _read("app/api/v1/admin.py")
    assert "from app.services.scope_prompt_preflight import" in src, (
        "Step 30d-A: app/api/v1/admin.py must import "
        "ScopePromptPreflight and ScopePromptMissingError from "
        "app.services.scope_prompt_preflight."
    )
    assert "ScopePromptPreflight" in src
    assert "ScopePromptMissingError" in src


def test_admin_route_calls_preflight_before_create_key() -> None:
    """In create_embed_key, ScopePromptPreflight.check must appear at
    a lower line number than ApiKeyService(db) / service.create_key.

    This is the Invariant-4 structural guarantee: if the preflight
    raises, the api_keys INSERT and its same-transaction audit row
    never happen. A maintainer who reorders these silently breaks the
    issuance-time scoping contract.
    """

    tree = _parse("app/api/v1/admin.py")
    fn = _find_function(tree, "create_embed_key")

    preflight_line = _first_lineno_of_call(fn, "ScopePromptPreflight.check")
    create_key_line = _first_lineno_of_call(fn, "service.create_key")

    assert preflight_line is not None, (
        "Step 30d-A: create_embed_key must call "
        "ScopePromptPreflight.check(...) somewhere in its body."
    )
    assert create_key_line is not None, (
        "create_embed_key must still call service.create_key(...) -- "
        "if this assertion fails, the route shape changed and the "
        "ordering pin needs to be re-pointed at the new call site."
    )
    assert preflight_line < create_key_line, (
        f"Step 30d-A: ScopePromptPreflight.check (line "
        f"{preflight_line}) must execute BEFORE service.create_key "
        f"(line {create_key_line}). Reversing this order breaks the "
        f"Invariant-4 guarantee that a preflight failure leaves no "
        f"api_keys row behind."
    )


def test_admin_route_handles_preflight_failure_with_422() -> None:
    """The except ScopePromptMissingError clause must raise
    HTTPException with HTTP_422_UNPROCESSABLE_ENTITY.

    422 (not 400) because the schema is valid -- a business-rule
    precondition is unmet. This distinction matters for client error
    handling (a 422 means 'fix your tenant config, then retry'; a 400
    means 'your request was malformed').
    """

    tree = _parse("app/api/v1/admin.py")
    fn = _find_function(tree, "create_embed_key")

    matched_handler: ast.ExceptHandler | None = None
    for node in ast.walk(fn):
        if not isinstance(node, ast.ExceptHandler):
            continue
        # Match `except ScopePromptMissingError [as exc]:`
        if (
            isinstance(node.type, ast.Name)
            and node.type.id == "ScopePromptMissingError"
        ):
            matched_handler = node
            break

    assert matched_handler is not None, (
        "Step 30d-A: create_embed_key must catch "
        "ScopePromptMissingError raised by the preflight. Without "
        "this handler the route returns a 500 to the operator, "
        "which is wrong (the request shape is fine; the tenant "
        "config is the issue)."
    )

    # The handler body must raise HTTPException with the 422 status.
    raises_http_422 = False
    for child in ast.walk(matched_handler):
        if not (isinstance(child, ast.Raise) and isinstance(child.exc, ast.Call)):
            continue
        call = child.exc
        if not (
            (isinstance(call.func, ast.Name) and call.func.id == "HTTPException")
            or (
                isinstance(call.func, ast.Attribute)
                and call.func.attr == "HTTPException"
            )
        ):
            continue
        # status_code= keyword must reference HTTP_422_UNPROCESSABLE_ENTITY
        for kw in call.keywords:
            if kw.arg != "status_code":
                continue
            txt = ast.unparse(kw.value)
            if "HTTP_422_UNPROCESSABLE_ENTITY" in txt or txt.strip() == "422":
                raises_http_422 = True
                break
        if raises_http_422:
            break

    assert raises_http_422, (
        "Step 30d-A: the except ScopePromptMissingError handler must "
        "raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, "
        "...). 422 (not 400) is the right code: the schema is valid, "
        "a business-rule precondition is unmet."
    )


def test_admin_route_appends_tenant_wide_warning() -> None:
    """Tenant-wide mints (domain_id is None) must skip the preflight
    and surface a non-fatal warning on the response.

    Pinned at AST level so a maintainer who deletes the warning (or
    converts the tenant-wide path into a hard error) trips this test.
    """

    src = _read("app/api/v1/admin.py")
    tree = ast.parse(src)
    fn = _find_function(tree, "create_embed_key")

    # Look for `if payload.domain_id is None:` with a warnings.append(...) inside.
    found = False
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        cond_txt = ast.unparse(node.test)
        if "payload.domain_id" not in cond_txt or "None" not in cond_txt:
            continue
        body_src = "\n".join(ast.unparse(n) for n in node.body)
        if "warnings.append" in body_src:
            found = True
            break

    assert found, (
        "Step 30d-A: create_embed_key must contain an "
        "`if payload.domain_id is None:` branch that calls "
        "warnings.append(...). Tenant-wide mints skip the preflight "
        "but must surface a non-fatal warning to the operator."
    )


def test_admin_route_passes_warnings_to_response() -> None:
    """EmbedKeyCreateResponse(...) must be constructed with the
    warnings kwarg so the response actually carries the field."""

    tree = _parse("app/api/v1/admin.py")
    fn = _find_function(tree, "create_embed_key")

    saw_response_with_warnings = False
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Call)):
            continue
        if not (
            isinstance(node.func, ast.Name)
            and node.func.id == "EmbedKeyCreateResponse"
        ):
            continue
        for kw in node.keywords:
            if kw.arg == "warnings":
                saw_response_with_warnings = True
                break
        if saw_response_with_warnings:
            break

    assert saw_response_with_warnings, (
        "Step 30d-A: EmbedKeyCreateResponse(...) must be constructed "
        "with warnings=warnings so the schema field is actually "
        "populated. Without this kwarg the warning is silently "
        "dropped before reaching the operator."
    )


# =====================================================================
# AST tests: CLI surface
# =====================================================================


def test_cli_imports_preflight() -> None:
    src = _read("scripts/mint_embed_key.py")
    assert "from app.services.scope_prompt_preflight import" in src, (
        "Step 30d-A: scripts/mint_embed_key.py must import "
        "ScopePromptPreflight and ScopePromptMissingError so the "
        "CLI cannot drift from the HTTP path on issuance-time scope "
        "checks."
    )
    assert "ScopePromptPreflight" in src
    assert "ScopePromptMissingError" in src


def test_cli_calls_preflight_before_service_create_key() -> None:
    """In scripts/mint_embed_key.py main(), the preflight must
    execute before svc.create_key(...)."""

    tree = _parse("scripts/mint_embed_key.py")
    fn = _find_function(tree, "main")

    preflight_line = _first_lineno_of_call(fn, "ScopePromptPreflight.check")
    create_key_line = _first_lineno_of_call(fn, "svc.create_key")

    assert preflight_line is not None, (
        "Step 30d-A: scripts/mint_embed_key.py main() must call "
        "ScopePromptPreflight.check(...)."
    )
    assert create_key_line is not None, (
        "scripts/mint_embed_key.py main() must still call "
        "svc.create_key(...) -- if this fails the script shape "
        "changed and the pin needs re-pointing."
    )
    assert preflight_line < create_key_line, (
        f"Step 30d-A: preflight (line {preflight_line}) must run "
        f"BEFORE svc.create_key (line {create_key_line}) in "
        f"scripts/mint_embed_key.py main()."
    )


def test_cli_returns_2_on_preflight_failure() -> None:
    """The CLI's `except ScopePromptMissingError` handler must
    return 2 (conventional CLI 'precondition / usage' code, matches
    the existing schema-validation failure path)."""

    tree = _parse("scripts/mint_embed_key.py")
    fn = _find_function(tree, "main")

    saw_return_2 = False
    for node in ast.walk(fn):
        if not (
            isinstance(node, ast.ExceptHandler)
            and isinstance(node.type, ast.Name)
            and node.type.id == "ScopePromptMissingError"
        ):
            continue
        for child in ast.walk(node):
            if (
                isinstance(child, ast.Return)
                and isinstance(child.value, ast.Constant)
                and child.value.value == 2
            ):
                saw_return_2 = True
                break
        if saw_return_2:
            break

    assert saw_return_2, (
        "Step 30d-A: scripts/mint_embed_key.py main() must `return 2` "
        "from its `except ScopePromptMissingError` handler. Exit 2 "
        "matches the conventional CLI 'precondition / usage' code and "
        "the existing schema-validation failure path."
    )


# =====================================================================
# AST tests: audit script
# =====================================================================


def test_audit_script_filters_active_embed_keys() -> None:
    """scripts/audit_widget_scope.py must scope its query to active
    embed keys only.

    If a maintainer drops the active filter, the audit will surface
    already-deactivated rows (false positives that obscure real
    operator-facing issues). If they drop the key_kind filter, the
    audit will start flagging admin keys (a category that has no
    scope-prompt requirement at all).
    """

    src = _read("scripts/audit_widget_scope.py")
    assert 'ApiKey.key_kind == "embed"' in src or "key_kind == 'embed'" in src, (
        "Step 30d-A: audit_widget_scope.py must filter on "
        "ApiKey.key_kind == 'embed' so it does not flag admin keys "
        "(which legitimately have no scope-prompt requirement)."
    )
    assert "ApiKey.active.is_(True)" in src or "ApiKey.active == True" in src, (
        "Step 30d-A: audit_widget_scope.py must filter on "
        "ApiKey.active.is_(True) so it does not surface "
        "already-deactivated rows as false positives."
    )


def test_audit_script_excludes_tenant_wide_keys() -> None:
    """Tenant-wide embed keys (domain_id IS NULL) must be excluded
    from the flag set -- same exclusion the preflight applies, so the
    runtime audit and the issuance preflight cannot drift on what
    counts as 'unscoped'."""

    tree = _parse("scripts/audit_widget_scope.py")
    fn = _find_function(tree, "_collect_flagged_rows")
    body_src = ast.unparse(fn)
    # Must contain a skip-branch for domain_id is None.
    assert re.search(
        r"key\.domain_id\s+is\s+None", body_src
    ), (
        "Step 30d-A: _collect_flagged_rows must explicitly skip "
        "rows where key.domain_id is None. Tenant-wide keys are "
        "governed by TenantConfig.system_prompt and must not be "
        "flagged here."
    )
    assert "continue" in body_src, (
        "Step 30d-A: _collect_flagged_rows must `continue` past "
        "tenant-wide keys rather than flagging them."
    )


# =====================================================================
# Behavioural tests: drive ScopePromptPreflight with a fake DB
# =====================================================================


class _FakeQuery:
    """Minimal stand-in for SQLAlchemy Query that records its filters
    and returns a pre-seeded row (or None) from one_or_none().

    We only implement the surface ScopePromptPreflight.check actually
    uses: query(...).filter(...).filter(...).one_or_none(). A future
    refactor that changes the query shape will fail loudly here, which
    is the desired regression signal.
    """

    def __init__(self, row, raise_on_query: bool = False) -> None:
        self._row = row
        self._raise_on_query = raise_on_query
        self.filter_calls: list[tuple] = []

    def filter(self, *args, **kwargs) -> "_FakeQuery":
        if self._raise_on_query:
            raise AssertionError(
                "Preflight queried the DB when it should have skipped "
                "(domain_id was None and preflight should short-circuit)."
            )
        self.filter_calls.append((args, kwargs))
        return self

    def one_or_none(self):
        return self._row


class _FakeRow:
    """Stand-in for a DomainConfig row carrying just system_prompt_additions."""

    def __init__(self, system_prompt_additions):
        self.system_prompt_additions = system_prompt_additions


class _FakeSession:
    """Stand-in for sqlalchemy.orm.Session exposing only .query()."""

    def __init__(self, row, raise_on_query: bool = False) -> None:
        self._row = row
        self._raise_on_query = raise_on_query
        self.query_calls: list = []

    def query(self, model):
        self.query_calls.append(model)
        return _FakeQuery(self._row, raise_on_query=self._raise_on_query)


def test_preflight_passes_when_prompt_present() -> None:
    db = _FakeSession(row=_FakeRow("You are the support assistant for Acme."))
    # Must not raise.
    ScopePromptPreflight.check(db, tenant_id="acme", domain_id="support")
    assert db.query_calls, "preflight must perform a query"


def test_preflight_raises_missing_domain_config() -> None:
    db = _FakeSession(row=None)
    with pytest.raises(ScopePromptMissingError) as excinfo:
        ScopePromptPreflight.check(db, tenant_id="acme", domain_id="support")
    exc = excinfo.value
    assert exc.reason == "missing_domain_config", (
        f"Expected reason='missing_domain_config'; got {exc.reason!r}"
    )
    assert exc.tenant_id == "acme"
    assert exc.domain_id == "support"
    # Message must mention the offending pair so the operator can act.
    assert "acme" in str(exc) and "support" in str(exc), (
        f"Error message must surface tenant_id and domain_id; "
        f"got: {exc!s}"
    )


@pytest.mark.parametrize(
    "value",
    [None, "", " ", "   ", "\n", "\t", "  \n\t  "],
    ids=["None", "empty", "single_space", "spaces", "newline", "tab", "mixed_ws"],
)
def test_preflight_raises_empty_system_prompt(value) -> None:
    db = _FakeSession(row=_FakeRow(value))
    with pytest.raises(ScopePromptMissingError) as excinfo:
        ScopePromptPreflight.check(db, tenant_id="acme", domain_id="support")
    exc = excinfo.value
    assert exc.reason == "empty_system_prompt", (
        f"NULL / empty / whitespace-only system_prompt_additions must "
        f"raise reason='empty_system_prompt'; got {exc.reason!r} for "
        f"value={value!r}."
    )


def test_preflight_skips_when_domain_id_is_none() -> None:
    """Tenant-wide mints must short-circuit BEFORE any DB query.

    The fake session is configured to raise on any .filter() call;
    if the preflight queries anyway, the AssertionError surfaces here.
    """

    db = _FakeSession(row=None, raise_on_query=True)
    # Must not raise -- preflight should return silently without
    # touching the fake DB.
    ScopePromptPreflight.check(db, tenant_id="acme", domain_id=None)


def test_response_schema_warnings_field_defaults_to_empty_list() -> None:
    """EmbedKeyCreateResponse.warnings must default to [] so pre-Step-
    30d clients continue to work without supplying the field."""

    from datetime import datetime

    from app.schemas.api_key import (
        EmbedKeyCreateResponse,
        EmbedKeyRead,
    )

    read = EmbedKeyRead.model_construct(
        id=1,
        key_prefix="luc_sk_test",
        display_name="t",
        tenant_id="acme",
        domain_id="support",
        permissions=["chat"],
        key_kind="embed",
        allowed_origins=["https://acme.com"],
        rate_limit_per_minute=30,
        widget_config={},
        active=True,
        created_at=datetime.utcnow(),
    )
    resp = EmbedKeyCreateResponse(embed_key=read, raw_key="raw")
    assert resp.warnings == [], (
        "Step 30d-A: EmbedKeyCreateResponse.warnings must default to "
        "[] for backward compatibility with pre-Step-30d clients."
    )

    resp2 = EmbedKeyCreateResponse(
        embed_key=read, raw_key="raw", warnings=["w1", "w2"]
    )
    assert resp2.warnings == ["w1", "w2"]


# =====================================================================
# Module import smoke test
# =====================================================================


def test_preflight_module_imports_cleanly() -> None:
    import importlib

    mod = importlib.import_module("app.services.scope_prompt_preflight")
    assert hasattr(mod, "ScopePromptPreflight")
    assert hasattr(mod, "ScopePromptMissingError")
    # Exception carries the documented attributes.
    exc = mod.ScopePromptMissingError(
        reason="missing_domain_config",
        tenant_id="t",
        domain_id="d",
        message="m",
    )
    assert exc.reason == "missing_domain_config"
    assert exc.tenant_id == "t"
    assert exc.domain_id == "d"
    assert str(exc) == "m"
