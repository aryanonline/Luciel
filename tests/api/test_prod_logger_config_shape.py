"""Backend-free contract test for prod application-logger configuration.

Closes DRIFTS token `D-prod-app-logger-info-suppressed-2026-05-12` and
pins the cross-cutting invariant that the deployed backend image emits
application-level INFO logs to CloudWatch.

Why this test exists
--------------------
Python's default root-logger level is WARNING. Without an explicit
`logging.basicConfig(level=logging.INFO)` (or equivalent
`dictConfig` / `setLevel` call) at process startup, every
`logger = logging.getLogger(__name__)` inside the `app.*` namespace
resolves to the root level and silently discards every `logger.info(...)`
emission. The Step 31 sub-branch 1 widget-chat audit log lines
(`widget_chat_turn_received` / `widget_chat_session_resolved` /
`widget_chat_turn_completed`) are emitted via `logger.info` and the
ARCHITECTURE §3.2.7 *Application log stream* claim depends on them
being observable in `/ecs/luciel-backend`. The 2026-05-12 prod deploy
of `step-24-5c-31-d843812` (rev `:39` -> `:40`) verified end-to-end via
Pillar 4c that the route emits the correct request/response shape, but
CloudWatch showed ZERO `widget_chat_*` lines for two real synthetic
widget turns -- the requests landed (uvicorn access log shows both
`POST /api/v1/chat/widget HTTP/1.1 200 OK` entries) but the application
INFO emissions were dropped before reaching stdout.

The worker process is unaffected because Celery's
`--loglevel=info` flag configures the worker root logger at bootstrap
(verified by the 15s heartbeat INFO lines visible in
`/ecs/luciel-worker` during the same prod window). This contract only
pins the backend uvicorn entrypoint side.

What this test pins
-------------------
By AST inspection of `app/main.py` (no FastAPI import, no Postgres,
no runtime side effects):

    * `app/main.py` calls `logging.basicConfig` exactly once at module
      load, AT MODULE TOP-LEVEL (not inside a function/class).
    * The call sets `level=logging.INFO` (or a lower numeric level).
    * The call passes `force=True` so it survives any earlier handler
      installation by uvicorn's CLI bootstrap.
    * The `logging.basicConfig` call PRECEDES the first `from app.`
      import, so application loggers bind to a configured root rather
      than the default WARNING root.

A regression that re-introduces the dropped-INFO failure mode must
either remove the `basicConfig` call, raise the level above INFO, or
move it after an `app.*` import -- any of those changes fail at least
one assertion here in the existing backend-free AST CI lane.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

MAIN_PATH = Path(__file__).resolve().parents[2] / "app" / "main.py"


@pytest.fixture(scope="module")
def main_module() -> ast.Module:
    """Parse app/main.py once for the whole module."""
    source = MAIN_PATH.read_text(encoding="utf-8")
    return ast.parse(source, filename=str(MAIN_PATH))


def _basicconfig_calls(tree: ast.Module) -> list[ast.Call]:
    """Return every `logging.basicConfig(...)` call at module top-level."""
    found: list[ast.Call] = []
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            call = node.value
            func = call.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "basicConfig"
                and isinstance(func.value, ast.Name)
                and func.value.id == "logging"
            ):
                found.append(call)
    return found


def _first_app_import_lineno(tree: ast.Module) -> int | None:
    """Lineno of the first `from app.<x> import ...` statement, or None."""
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "app" or node.module.startswith("app."):
                return node.lineno
    return None


def _logging_import_lineno(tree: ast.Module) -> int | None:
    """Lineno of `import logging` at module top-level."""
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "logging":
                    return node.lineno
    return None


def test_logging_import_present(main_module: ast.Module) -> None:
    """`app/main.py` imports the stdlib `logging` module at top level."""
    assert _logging_import_lineno(main_module) is not None, (
        "app/main.py must `import logging` at module top-level so the "
        "`basicConfig` call below can configure the root logger before "
        "any `app.*` import binds a child logger."
    )


def test_basicconfig_called_exactly_once_at_top_level(
    main_module: ast.Module,
) -> None:
    """Exactly one top-level `logging.basicConfig(...)` call."""
    calls = _basicconfig_calls(main_module)
    assert len(calls) == 1, (
        f"app/main.py must call logging.basicConfig() exactly once at "
        f"module top-level; found {len(calls)} call(s). Multiple calls "
        f"are an anti-pattern (only the first wins unless force=True is "
        f"passed); zero calls re-introduce "
        f"D-prod-app-logger-info-suppressed-2026-05-12."
    )


def test_basicconfig_level_is_info_or_lower(main_module: ast.Module) -> None:
    """`level=` keyword on the basicConfig call resolves to INFO (20) or lower.

    Accepts either `level=logging.INFO` (attribute access) or
    `level=20` (literal integer). Rejects WARNING / ERROR / CRITICAL
    by numeric or attribute form.
    """
    calls = _basicconfig_calls(main_module)
    assert calls, "no logging.basicConfig() call found -- see other test"

    call = calls[0]
    level_kw = next((kw for kw in call.keywords if kw.arg == "level"), None)
    assert level_kw is not None, (
        "logging.basicConfig() must pass a `level=` keyword argument. "
        "Omitting it leaves the root logger at the default WARNING and "
        "re-introduces D-prod-app-logger-info-suppressed-2026-05-12."
    )

    value = level_kw.value
    info_numeric = 20  # logging.INFO

    # Form 1: level=logging.INFO / logging.DEBUG
    if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
        assert value.value.id == "logging", (
            f"level= must reference the stdlib `logging` module, got "
            f"`{value.value.id}.{value.attr}`."
        )
        # INFO=20, DEBUG=10, NOTSET=0 are all acceptable (<= INFO)
        acceptable = {"INFO", "DEBUG", "NOTSET"}
        assert value.attr in acceptable, (
            f"level=logging.{value.attr} is above INFO and would still "
            f"drop the Step 31 widget-chat audit lines. Use logging.INFO "
            f"(or lower) per D-prod-app-logger-info-suppressed-2026-05-12."
        )
        return

    # Form 2: level=20 (or any int literal <= 20)
    if isinstance(value, ast.Constant) and isinstance(value.value, int):
        assert value.value <= info_numeric, (
            f"level={value.value} is above logging.INFO ({info_numeric}); "
            f"would still drop application-level INFO emissions."
        )
        return

    pytest.fail(
        f"level= argument must be either `logging.INFO`-style attribute or "
        f"an int literal <= 20; got {ast.dump(value)}."
    )


def test_basicconfig_uses_force_true(main_module: ast.Module) -> None:
    """`force=True` is required to survive uvicorn's pre-load logger setup.

    Without `force=True`, an earlier `logging.basicConfig` call by
    uvicorn's CLI bootstrap (or any imported library) would make our
    call a silent no-op -- exactly the failure mode we're closing.
    """
    calls = _basicconfig_calls(main_module)
    assert calls, "no logging.basicConfig() call found -- see other test"

    call = calls[0]
    force_kw = next((kw for kw in call.keywords if kw.arg == "force"), None)
    assert force_kw is not None, (
        "logging.basicConfig() must pass `force=True` so the call survives "
        "any earlier root-logger handler installation by uvicorn CLI or an "
        "imported library. Without it, our call is a silent no-op."
    )

    value = force_kw.value
    assert isinstance(value, ast.Constant) and value.value is True, (
        f"force= must be the literal True; got {ast.dump(value)}."
    )


def test_basicconfig_precedes_first_app_import(main_module: ast.Module) -> None:
    """The `basicConfig` call must precede the first `from app.X import ...`.

    If an `app.*` import lands first, the module-level
    `logger = logging.getLogger(__name__)` in that import path is
    evaluated against the unconfigured root (WARNING), and even after
    our later basicConfig the child logger may still have a cached
    effective-level lookup that misses INFO. Configuring root BEFORE
    any app import is the only invariant that makes the audit log
    behavior deterministic.
    """
    calls = _basicconfig_calls(main_module)
    assert calls, "no logging.basicConfig() call found -- see other test"

    bc_lineno = calls[0].lineno
    first_app_lineno = _first_app_import_lineno(main_module)
    assert first_app_lineno is not None, (
        "app/main.py is expected to import from `app.*` at least once; "
        "if that ever changes, revisit this test's premise."
    )
    assert bc_lineno < first_app_lineno, (
        f"logging.basicConfig() at line {bc_lineno} must precede the "
        f"first `from app.* import ...` at line {first_app_lineno}. "
        f"Configuring root after an app-module import re-introduces "
        f"D-prod-app-logger-info-suppressed-2026-05-12 for that "
        f"module's logger."
    )


def test_logging_import_precedes_basicconfig(main_module: ast.Module) -> None:
    """`import logging` must come before its first reference."""
    log_lineno = _logging_import_lineno(main_module)
    calls = _basicconfig_calls(main_module)
    assert log_lineno is not None and calls, "see other tests"
    assert log_lineno < calls[0].lineno, (
        f"`import logging` at line {log_lineno} must precede the "
        f"logging.basicConfig() call at line {calls[0].lineno}."
    )
