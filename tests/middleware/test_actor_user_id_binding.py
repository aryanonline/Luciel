"""
Regression test for the actor_user_id binding contract on the API-key
auth path.

CONTRACT GUARDED (post Arc 5 Path A)
====================================

API keys authenticate one of three kinds of caller per the
Architecture v1 model:

  * Embed-key (data-plane chat-widget traffic; Architecture v1 \u00a71.2).
  * Channel webhook signed for an Admin (data-plane channel ingress).
  * Platform-admin keys (cross-tenant operator surface).

NONE of these carries a platform User identity. The platform User
identity that backs ``actor_user_id`` is established by the Control
Plane cookied-auth path (web dashboard sign-in, Architecture v1
\u00a71.1) -- not by an API key.

Therefore, the invariant on the API-key middleware path is:

  ``request.state.actor_user_id is None`` after API-key auth completes.

This file pins that invariant at the source level (no app deps
required to verify) and at the behavioural level (runs the actual
middleware when deps are available).

HISTORY
=======

A previous iteration of this test file guarded a different invariant:
that ``actor_user_id`` was set from ``agent.user_id`` when an
``agent`` row was resolved during API-key dispatch. That code path
was removed at Arc 5 Path A when the Agent layer was deleted (Vision
v1 \u00a72/\u00a73 -- V2 has Admin -> Instance, no intermediate Agent layer).
Re-asserting the deleted path with mocks of a deleted class is not a
regression guard; it is dead scaffolding. This file now asserts the
current contract directly.
"""
from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_AUTH_PATH = _PROJECT_ROOT / "app" / "middleware" / "auth.py"


# ---------------------------------------------------------------------
# Source-level invariants. Runnable without ANY app deps installed.
# ---------------------------------------------------------------------

def test_source_does_not_import_deleted_agent_repository() -> None:
    """auth.py must not import the deleted AgentRepository class.

    AgentRepository was removed at Arc 5 Path A. A surviving import
    here would either (a) fail at module load (ModuleNotFoundError,
    every request rejected) or (b) succeed by accident if a future
    contributor re-introduces a stub, silently re-attaching the
    deleted resolution path the test file's previous iteration was
    meant to guard.
    """
    src = _AUTH_PATH.read_text()
    assert "from app.repositories.agent_repository" not in src, (
        "auth.py imports the deleted AgentRepository module; "
        "remove the import per Arc 5 Path A doctrine."
    )
    assert "import AgentRepository" not in src, (
        "auth.py references the deleted AgentRepository class; "
        "remove per Arc 5 Path A doctrine."
    )


def test_source_dispatch_assigns_actor_user_id_to_none_on_api_key_path() -> None:
    """The API-key dispatch path must explicitly set actor_user_id = None.

    Per Architecture v1 \u00a71.2, the data-plane authenticates as an
    embed key or channel webhook signed for an Admin -- neither
    carries a platform User identity. The middleware must therefore
    set ``actor_user_id = None`` on the API-key path so downstream
    consumers (audit chain, traces, metrics) read a definite value
    rather than tripping on AttributeError.
    """
    src = _AUTH_PATH.read_text()
    tree = ast.parse(src)
    dispatch = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "dispatch":
            dispatch = node
            break
    assert dispatch is not None, "dispatch() not found in app/middleware/auth.py"

    # Look for `actor_user_id = None` literal assignment somewhere
    # inside dispatch -- that is the load-bearing line for the
    # current contract.
    found = False
    for node in ast.walk(dispatch):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            tgt = node.targets[0]
            if (isinstance(tgt, ast.Name)
                    and tgt.id == "actor_user_id"
                    and isinstance(node.value, ast.Constant)
                    and node.value.value is None):
                found = True
                break
    assert found, (
        "dispatch() must contain `actor_user_id = None` per the post-"
        "Arc-5-Path-A contract -- API keys do not carry a platform "
        "User identity. See module docstring."
    )


def test_source_state_actor_user_id_is_bound() -> None:
    """dispatch() must bind request.state.actor_user_id before handing\n    off to call_next. Downstream consumers (audit chain, traces)\n    rely on the attribute existing, even if the value is None.\n    """
    src = _AUTH_PATH.read_text()
    assert "request.state.actor_user_id = actor_user_id" in src, (
        "dispatch() must bind request.state.actor_user_id before "
        "calling call_next. Without this, downstream consumers raise "
        "AttributeError on missing state."
    )


# ---------------------------------------------------------------------
# Behavioural test. Runs only when app deps are importable.
# ---------------------------------------------------------------------

_BEHAVIORAL_AVAILABLE = True
_BEHAVIORAL_IMPORT_ERROR: Exception | None = None
try:
    from app.middleware.auth import ApiKeyAuthMiddleware  # noqa: E402
except Exception as _exc:  # noqa: BLE001
    _BEHAVIORAL_AVAILABLE = False
    _BEHAVIORAL_IMPORT_ERROR = _exc
    ApiKeyAuthMiddleware = None  # type: ignore


def _make_request(path: str = "/api/v1/sessions",
                  bearer: str = "luc_sk_fake-raw-key-value",
                  method: str = "POST") -> object:
    return SimpleNamespace(
        method=method,
        url=SimpleNamespace(path=path),
        headers={"Authorization": f"Bearer {bearer}"},
        state=SimpleNamespace(),
    )


async def _invoke(middleware, request):
    captured = {}

    async def call_next(req):
        captured["state"] = req.state
        return SimpleNamespace(status_code=200)

    response = await middleware.dispatch(request, call_next)
    return response, captured.get("state")


def test_api_key_path_leaves_actor_user_id_none() -> None:
    """End-to-end: a successful API-key dispatch must set\n    request.state.actor_user_id = None.\n    """
    if not _BEHAVIORAL_AVAILABLE:
        import pytest  # noqa: PLC0415
        pytest.skip(
            f"behavioral test skipped: app deps unavailable "
            f"({type(_BEHAVIORAL_IMPORT_ERROR).__name__}: "
            f"{_BEHAVIORAL_IMPORT_ERROR})"
        )

    fake_apikey = SimpleNamespace(
        id="ak_test_123",
        admin_id="acme-corp",
        domain_id=None,
        agent_id=None,
        luciel_instance_id=None,
        permissions=["chat:write"],
        key_prefix="luc_sk_fa",
        created_by="acme-corp api-key",
    )

    with patch("app.middleware.auth.SessionLocal") as session_local, \
         patch("app.middleware.auth.ApiKeyService") as api_key_service_cls:
        session_local.return_value = MagicMock()
        api_key_service_cls.return_value.validate_key.return_value = fake_apikey

        mw = ApiKeyAuthMiddleware(app=None)
        request = _make_request()
        _, state = asyncio.run(_invoke(mw, request))

    assert state is not None, (
        "middleware did not invoke call_next -- request was rejected pre-dispatch"
    )
    assert state.actor_user_id is None, (
        f"API-key dispatch must leave actor_user_id=None per the "
        f"post-Arc-5-Path-A contract; got {state.actor_user_id!r}"
    )
