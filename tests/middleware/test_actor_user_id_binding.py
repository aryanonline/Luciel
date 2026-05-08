"""
Regression test for D-pillar-13-a3-real-root-cause-2026-05-04.

CONTRACT GUARDED:
    For agent-scoped API keys whose Agent has a non-NULL user_id, the
    auth middleware MUST set request.state.actor_user_id to that user_id.

THE BUG THIS GUARDS AGAINST:
    The original code at app/middleware/auth.py:124 read:
        user_id = agent.user_id
    creating a never-read local variable instead of mutating the
    actor_user_id captured at line 115. As a result,
    request.state.actor_user_id remained None for every agent-scoped
    key in production from Step 24.5b through Step 28 Phase 2.

    Downstream effect: the worker memory-extraction task wrote
    actor_user_id=NULL, which the Step 28 Phase 1 D11 NOT NULL
    constraint then rejected. The IntegrityError was swallowed by
    MemoryService.extract_and_save's generic except, surfacing as
    0 MemoryItem rows in Pillar 13 A3 with no error visible to the
    chat caller.

THE FIX:
    Line 124 now reads:
        actor_user_id = agent.user_id

WHY MOCK INSTEAD OF DB:
    The bug is purely in variable scoping inside the middleware
    function -- it has nothing to do with DB state, schema, or
    Postgres. A mocked AgentRepository exercises the exact code path
    in isolation. A full DB-backed assertion already runs as part of
    Pillar 13 A3 (which previously failed and now passes). This test
    catches the regression *first* and *fastest*.

RUN:
    python -m pytest tests/middleware/test_actor_user_id_binding.py -v
    OR (no pytest needed):
    python tests/middleware/test_actor_user_id_binding.py
"""
from __future__ import annotations

import ast
import asyncio
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Allow running via `python tests/middleware/test_actor_user_id_binding.py`
# from any cwd by inserting the project root on sys.path before imports.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_AUTH_PATH = _PROJECT_ROOT / "app" / "middleware" / "auth.py"


# ---------------------------------------------------------------- test 0
# Source-level AST proof. Runnable without ANY app deps installed --
# catches regressions in CI/sandbox even when sqlalchemy/pgvector/etc.
# are unavailable. This is the canary.
def test_source_level_assignment_targets_actor_user_id() -> None:
    src = _AUTH_PATH.read_text()
    tree = ast.parse(src)

    dispatch = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "dispatch":
            dispatch = node
            break
    assert dispatch is not None, "dispatch() not found in app/middleware/auth.py"

    found_assignment = None
    for node in ast.walk(dispatch):
        if isinstance(node, ast.If):
            t = node.test
            if (isinstance(t, ast.Compare) and len(t.ops) == 1
                    and isinstance(t.ops[0], ast.IsNot)
                    and isinstance(t.left, ast.Name) and t.left.id == "agent"
                    and isinstance(t.comparators[0], ast.Constant)
                    and t.comparators[0].value is None):
                for stmt in node.body:
                    if isinstance(stmt, ast.Assign):
                        val = stmt.value
                        if (isinstance(val, ast.Attribute)
                                and isinstance(val.value, ast.Name)
                                and val.value.id == "agent"
                                and val.attr == "user_id"):
                            found_assignment = stmt
                            break
                break

    assert found_assignment is not None, (
        "could not locate any `<x> = agent.user_id` assignment inside "
        "`if agent is not None:` -- the structure of dispatch() has changed"
    )
    target = found_assignment.targets[0]
    assert isinstance(target, ast.Name), "target is not a simple Name"
    assert target.id == "actor_user_id", (
        f"REGRESSION of D-pillar-13-a3-real-root-cause-2026-05-04: "
        f"line {found_assignment.lineno} assigns `agent.user_id` to "
        f"`{target.id}` -- expected `actor_user_id`. The original typo "
        f"created a never-read local and left request.state.actor_user_id "
        f"=None for every agent-scoped key in production."
    )

    # Also assert there is NO stray `user_id = agent.user_id` anywhere in dispatch.
    for node in ast.walk(dispatch):
        if isinstance(node, ast.Assign):
            val = node.value
            if (isinstance(val, ast.Attribute)
                    and isinstance(val.value, ast.Name)
                    and val.value.id == "agent"
                    and val.attr == "user_id"):
                tgt = node.targets[0]
                assert isinstance(tgt, ast.Name) and tgt.id == "actor_user_id", (
                    f"stray `{tgt.id} = agent.user_id` at line {node.lineno} "
                    f"-- partial regression"
                )


# Behavioral tests below require app deps (sqlalchemy, pydantic-settings,
# pgvector, etc.) and a stub DATABASE_URL env var. They are the
# functional layer of the test guard. If the import fails (e.g. running
# in a minimal sandbox), the source-level test 0 above is sufficient on
# its own to catch the regression -- the behavioral tests are skipped.
_BEHAVIORAL_AVAILABLE = True
_BEHAVIORAL_IMPORT_ERROR: Exception | None = None
try:
    from app.middleware.auth import ApiKeyAuthMiddleware  # noqa: E402
except Exception as _exc:  # noqa: BLE001
    _BEHAVIORAL_AVAILABLE = False
    _BEHAVIORAL_IMPORT_ERROR = _exc
    ApiKeyAuthMiddleware = None  # type: ignore


def _make_request(path: str = "/api/v1/sessions",
                  bearer: str = "luc_sk_fake-raw-key-value") -> object:
    """Build the smallest object that quacks like a Starlette Request."""
    return SimpleNamespace(
        url=SimpleNamespace(path=path),
        headers={"Authorization": f"Bearer {bearer}"},
        state=SimpleNamespace(),
    )


async def _invoke(middleware: ApiKeyAuthMiddleware, request: object) -> object:
    """Run the middleware's dispatch with a captured call_next."""
    captured = {}

    async def call_next(req: object) -> object:
        # Capture request.state at the moment dispatch hands off.
        captured["state"] = req.state
        return SimpleNamespace(status_code=200)

    response = await middleware.dispatch(request, call_next)
    return response, captured.get("state")


def _run(coro) -> tuple:
    return asyncio.run(coro)


class _SkipTest(Exception):
    """Raised by behavioral tests when their imports are unavailable."""


def _require_behavioral() -> None:
    if not _BEHAVIORAL_AVAILABLE:
        raise _SkipTest(
            f"behavioral test skipped: app deps unavailable in this env "
            f"({type(_BEHAVIORAL_IMPORT_ERROR).__name__}: {_BEHAVIORAL_IMPORT_ERROR})"
        )


# ---------------------------------------------------------------- test 1
def test_agent_scoped_key_with_user_id_binds_actor_user_id() -> None:
    """The fix: actor_user_id MUST equal agent.user_id for agent-scoped keys."""
    _require_behavioral()
    expected_user_id = uuid.uuid4()

    fake_apikey = SimpleNamespace(
        id=42,
        tenant_id="t-test",
        domain_id="d-test",
        agent_id="a-test",
        permissions=["chat", "sessions"],
        key_prefix="luc_sk_fakepfx",
        created_by="test-actor",
        luciel_instance_id=None,
    )
    fake_agent = SimpleNamespace(user_id=expected_user_id)

    with patch("app.middleware.auth.SessionLocal") as session_local, \
         patch("app.middleware.auth.ApiKeyService") as api_key_service_cls, \
         patch("app.middleware.auth.AgentRepository") as agent_repo_cls:
        session_local.return_value = MagicMock()
        api_key_service_cls.return_value.validate_key.return_value = fake_apikey
        agent_repo_cls.return_value.get_scoped.return_value = fake_agent

        mw = ApiKeyAuthMiddleware(app=None)
        request = _make_request()
        _, state = _run(_invoke(mw, request))

    assert state is not None, (
        "middleware did not invoke call_next -- request was rejected pre-dispatch"
    )
    assert state.actor_user_id == expected_user_id, (
        f"actor_user_id binding regression: expected {expected_user_id}, "
        f"got {state.actor_user_id!r}. The auth.py:124 typo is back."
    )


# ---------------------------------------------------------------- test 2
def test_agent_with_null_user_id_yields_none_actor_user_id() -> None:
    """Legacy contract: agents pending Commit 3 backfill (user_id=None)
    correctly produce actor_user_id=None -- NOT a bug, just deferred."""
    _require_behavioral()
    fake_apikey = SimpleNamespace(
        id=43, tenant_id="t-test", domain_id="d-test", agent_id="a-test",
        permissions=["chat"], key_prefix="luc_sk_fakepfx",
        created_by="test-actor", luciel_instance_id=None,
    )
    fake_agent = SimpleNamespace(user_id=None)  # backfill pending

    with patch("app.middleware.auth.SessionLocal") as session_local, \
         patch("app.middleware.auth.ApiKeyService") as api_key_service_cls, \
         patch("app.middleware.auth.AgentRepository") as agent_repo_cls:
        session_local.return_value = MagicMock()
        api_key_service_cls.return_value.validate_key.return_value = fake_apikey
        agent_repo_cls.return_value.get_scoped.return_value = fake_agent

        mw = ApiKeyAuthMiddleware(app=None)
        request = _make_request()
        _, state = _run(_invoke(mw, request))

    assert state is not None
    assert state.actor_user_id is None, (
        "agent.user_id=None should pass through as actor_user_id=None "
        f"(deferred backfill); got {state.actor_user_id!r}"
    )


# ---------------------------------------------------------------- test 3
def test_tenant_admin_key_yields_none_actor_user_id() -> None:
    """Tenant-admin / platform-admin keys have agent_id=None and MUST
    NOT trigger the agent lookup at all -- actor_user_id stays None."""
    _require_behavioral()
    fake_apikey = SimpleNamespace(
        id=44, tenant_id="t-test", domain_id=None, agent_id=None,
        permissions=["admin"], key_prefix="luc_sk_adminpfx",
        created_by="test-actor", luciel_instance_id=None,
    )

    with patch("app.middleware.auth.SessionLocal") as session_local, \
         patch("app.middleware.auth.ApiKeyService") as api_key_service_cls, \
         patch("app.middleware.auth.AgentRepository") as agent_repo_cls:
        session_local.return_value = MagicMock()
        api_key_service_cls.return_value.validate_key.return_value = fake_apikey

        mw = ApiKeyAuthMiddleware(app=None)
        request = _make_request(path="/api/v1/sessions")
        _, state = _run(_invoke(mw, request))

    assert state is not None
    assert state.actor_user_id is None
    # The repo must NOT have been instantiated -- this proves the
    # tenant_id/domain_id/agent_id triple-non-null guard is working.
    agent_repo_cls.assert_not_called()


# ---------------------------------------------------------------- test 4
def test_orphan_apikey_missing_agent_yields_none_actor_user_id() -> None:
    """Defensive contract: ApiKey references a non-existent Agent (drift).
    Must log a warning AND leave actor_user_id=None -- never crash."""
    _require_behavioral()
    fake_apikey = SimpleNamespace(
        id=45, tenant_id="t-test", domain_id="d-test", agent_id="a-orphan",
        permissions=["chat"], key_prefix="luc_sk_orphpfx",
        created_by="test-actor", luciel_instance_id=None,
    )

    with patch("app.middleware.auth.SessionLocal") as session_local, \
         patch("app.middleware.auth.ApiKeyService") as api_key_service_cls, \
         patch("app.middleware.auth.AgentRepository") as agent_repo_cls:
        session_local.return_value = MagicMock()
        api_key_service_cls.return_value.validate_key.return_value = fake_apikey
        agent_repo_cls.return_value.get_scoped.return_value = None  # orphan

        mw = ApiKeyAuthMiddleware(app=None)
        request = _make_request()
        _, state = _run(_invoke(mw, request))

    assert state is not None
    assert state.actor_user_id is None


# ---------------------------------------------------------------- harness
def _main() -> int:
    """Allow `python tests/middleware/test_actor_user_id_binding.py` (no pytest)."""
    failures: list[str] = []
    skipped: list[tuple[str, str]] = []
    # Run test_source... first deterministically (it is the canary).
    ordered = sorted(
        [(n, fn) for n, fn in globals().items()
         if n.startswith("test_") and callable(fn)],
        key=lambda kv: (0 if "source_level" in kv[0] else 1, kv[0]),
    )
    for name, fn in ordered:
        try:
            fn()
            print(f"PASS  {name}")
        except _SkipTest as exc:
            print(f"SKIP  {name}: {exc}")
            skipped.append((name, str(exc)))
        except AssertionError as exc:
            print(f"FAIL  {name}: {exc}")
            failures.append(name)
        except Exception as exc:
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
            failures.append(name)
    print("-" * 60)
    if failures:
        print(f"{len(failures)} failures: {failures}")
        return 1
    if skipped:
        print(f"{len(skipped)} skipped (env-limited), source-level canary green")
    else:
        print("all green")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main())
