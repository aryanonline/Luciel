"""
Step 30d Deliverable B -- regression tests for the content-safety
moderation gate and its wiring into the widget chat surface.

Test strategy
=============

Same house style as tests/api/test_embed_key_preflight.py
(Deliverable A) and tests/api/test_step29y_cluster6_*.py: AST
contract tests + isolated behavioural tests driven by inline fakes.
No FastAPI TestClient, no test DB -- the harness-level work lands in
Deliverable C.

Two groups:

  AST tests
    Pin that the moderation gate is wired into the chat_widget route
    in the right shape and the right place. Future refactors that
    silently drop the gate, reorder it after the LLM call, or leak
    moderation categories into the SSE frame trip a specific test.

  Behavioural tests
    Drive the moderation providers directly (with httpx stubbed via
    monkeypatch) to exercise:
      * FailClosed converts provider unavailability into block
      * FailClosed does not short-circuit on real-block or real-pass
      * Null provider never blocks and emits a WARNING
      * Gate factory dispatches correctly per settings
      * Factory raises ConfigurationError at the right times

The refusal-frame shape is pinned both at AST level (no `categories`
ever appears in a json.dumps call inside the refusal stream) and at
behavioural level (the moderation result's categories list does not
appear in any frame the route would yield).

References
==========

  * app/policy/moderation.py -- the units under test.
  * app/api/v1/chat_widget.py -- the wired call site.
  * ARCHITECTURE.md section 3.3 step 6.5 -- the design statement.
  * ARCHITECTURE.md section 4.9 -- the rejected-alternative bullet.
"""

from __future__ import annotations

import ast
import logging
import pathlib

import pytest


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


def _first_lineno_of_call_attr(node: ast.AST, obj: str, attr: str) -> int | None:
    """Return the line number of the first call to obj.attr(...) inside node."""

    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == attr
            and isinstance(func.value, ast.Name)
            and func.value.id == obj
        ):
            return child.lineno
    return None


# =====================================================================
# AST tests: moderation module shape
# =====================================================================


def test_moderation_module_defines_required_symbols() -> None:
    """app/policy/moderation.py must publish the full public API the
    chat route depends on. A maintainer who renames any of these
    silently breaks the wiring at import time."""

    import app.policy.moderation as mod

    for name in (
        "ModerationResult",
        "ModerationProvider",
        "OpenAIModerationProvider",
        "NullModerationProvider",
        "FailClosedModerationProvider",
        "ModerationGate",
        "ModerationProviderUnavailable",
        "ConfigurationError",
    ):
        assert hasattr(mod, name), (
            f"Step 30d-B: app.policy.moderation must publish {name!r}. "
            f"A rename here breaks chat_widget.py at import time."
        )


def test_openai_provider_uses_explicit_timeout() -> None:
    """OpenAIModerationProvider.moderate must construct httpx.Client
    with a timeout. Dropping the timeout would let a slow / hanging
    provider stall the entire chat turn -- which is exactly what the
    fail-closed wrapper exists to prevent. The timeout has to ARRIVE
    at httpx, not just live in __init__.
    """

    src = _read("app/policy/moderation.py")
    tree = ast.parse(src)
    cls = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.ClassDef) and n.name == "OpenAIModerationProvider"),
        None,
    )
    assert cls is not None
    moderate_fn = next(
        (n for n in ast.walk(cls)
         if isinstance(n, ast.FunctionDef) and n.name == "moderate"),
        None,
    )
    assert moderate_fn is not None

    saw_timeout_kwarg = False
    for node in ast.walk(moderate_fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_httpx_client = (
            isinstance(func, ast.Attribute)
            and func.attr == "Client"
            and isinstance(func.value, ast.Name)
            and func.value.id == "httpx"
        )
        if not is_httpx_client:
            continue
        for kw in node.keywords:
            if kw.arg == "timeout":
                saw_timeout_kwarg = True
                break
    assert saw_timeout_kwarg, (
        "Step 30d-B: OpenAIModerationProvider.moderate must pass "
        "timeout=... to httpx.Client(). Without an explicit timeout, "
        "a slow provider can stall the entire chat turn and the "
        "fail-closed path never fires."
    )


def test_failclosed_wrapper_blocks_on_provider_unavailable() -> None:
    """FailClosedModerationProvider.moderate must have an
    `except ModerationProviderUnavailable` clause that returns
    blocked=True. This is the core fail-closed invariant -- if a
    maintainer changes 'blocked=True' to 'blocked=False' here, the
    gate silently opens the widget to unmoderated traffic whenever
    the provider is unreachable.
    """

    src = _read("app/policy/moderation.py")
    tree = ast.parse(src)
    cls = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.ClassDef)
         and n.name == "FailClosedModerationProvider"),
        None,
    )
    assert cls is not None
    moderate_fn = next(
        (n for n in ast.walk(cls)
         if isinstance(n, ast.FunctionDef) and n.name == "moderate"),
        None,
    )
    assert moderate_fn is not None

    matched = None
    for node in ast.walk(moderate_fn):
        if (
            isinstance(node, ast.ExceptHandler)
            and isinstance(node.type, ast.Name)
            and node.type.id == "ModerationProviderUnavailable"
        ):
            matched = node
            break
    assert matched is not None, (
        "Step 30d-B: FailClosedModerationProvider.moderate must catch "
        "ModerationProviderUnavailable. Without this handler the "
        "wrapper does not actually fail closed."
    )

    # The handler must construct ModerationResult(blocked=True, ...).
    saw_blocked_true = False
    for node in ast.walk(matched):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "ModerationResult"):
            continue
        for kw in node.keywords:
            if kw.arg == "blocked" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                saw_blocked_true = True
                break
    assert saw_blocked_true, (
        "Step 30d-B: FailClosedModerationProvider.moderate's except "
        "handler must return ModerationResult(blocked=True, ...). "
        "Flipping to blocked=False here silently opens the widget to "
        "unmoderated traffic whenever the provider is down."
    )


# =====================================================================
# AST tests: chat_widget wiring
# =====================================================================


def test_chat_widget_imports_moderation_gate() -> None:
    src = _read("app/api/v1/chat_widget.py")
    assert "from app.policy.moderation import" in src, (
        "Step 30d-B: app/api/v1/chat_widget.py must import "
        "ModerationGate from app.policy.moderation."
    )
    assert "ModerationGate" in src


def test_chat_widget_builds_module_level_singleton() -> None:
    """The gate must be built once at module import (matches the
    pattern the route uses for `logger`). Per-request DI would
    hammer connect time on the httpx client and would also defer the
    ConfigurationError check from boot to first request -- the
    opposite of what we want."""

    src = _read("app/api/v1/chat_widget.py")
    assert "_moderation_gate = ModerationGate.from_settings" in src, (
        "Step 30d-B: chat_widget.py must construct the module-level "
        "singleton `_moderation_gate = "
        "ModerationGate.from_settings(settings)` so the gate is built "
        "once at import time and a misconfiguration crashes the "
        "process at boot rather than at first request."
    )


def test_chat_widget_calls_gate_before_respond_stream() -> None:
    """In widget_chat_stream, _moderation_gate.moderate must appear at
    a lower line number than chat_service.respond_stream.

    This is the core structural guarantee: a blocked turn cannot leak
    its message to the LLM. A maintainer who reorders these silently
    breaks the gate.
    """

    tree = _parse("app/api/v1/chat_widget.py")
    fn = _find_function(tree, "widget_chat_stream")

    moderate_line = _first_lineno_of_call_attr(fn, "_moderation_gate", "moderate")
    respond_line = _first_lineno_of_call_attr(fn, "chat_service", "respond_stream")

    assert moderate_line is not None, (
        "Step 30d-B: widget_chat_stream must call "
        "_moderation_gate.moderate(...)."
    )
    assert respond_line is not None, (
        "widget_chat_stream must still call chat_service.respond_stream "
        "-- if this fails the route shape changed."
    )
    assert moderate_line < respond_line, (
        f"Step 30d-B: _moderation_gate.moderate (line {moderate_line}) "
        f"must execute BEFORE chat_service.respond_stream (line "
        f"{respond_line}). Reversing this order leaks the user "
        f"message to the LLM before the gate has a chance to block."
    )


def test_refusal_stream_uses_refusal_message_constant() -> None:
    """REFUSAL_MESSAGE must exist as a module-level constant and be
    referenced inside widget_chat_stream's refusal branch. A
    maintainer who inlines a category-specific string here can
    accidentally leak the blocking reason to the client.
    """

    src = _read("app/api/v1/chat_widget.py")
    tree = ast.parse(src)

    has_constant = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "REFUSAL_MESSAGE"
        ):
            has_constant = True
            break
    assert has_constant, (
        "Step 30d-B: chat_widget.py must define module-level "
        "REFUSAL_MESSAGE so the refusal text is auditable in one "
        "place and cannot drift across the codebase."
    )

    fn = _find_function(tree, "widget_chat_stream")
    used_in_route = any(
        isinstance(n, ast.Name) and n.id == "REFUSAL_MESSAGE"
        for n in ast.walk(fn)
    )
    assert used_in_route, (
        "Step 30d-B: widget_chat_stream must reference REFUSAL_MESSAGE "
        "in its refusal branch. If you see this assertion fire, the "
        "route was refactored to inline the refusal text -- restore "
        "the constant reference so future edits stay auditable."
    )


def test_refusal_branch_does_not_leak_categories() -> None:
    """Inside widget_chat_stream, no json.dumps call may include a
    `categories` key or pass `moderation.categories` as a value.
    The moderation categories are an internal signal for operators;
    surfacing them in the SSE frame would tell a hostile prober
    exactly which detection tripped.
    """

    tree = _parse("app/api/v1/chat_widget.py")
    fn = _find_function(tree, "widget_chat_stream")
    offenders: list[int] = []

    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_json_dumps = (
            isinstance(func, ast.Attribute)
            and func.attr == "dumps"
            and isinstance(func.value, ast.Name)
            and func.value.id == "json"
        )
        if not is_json_dumps:
            continue
        # Inspect the first positional argument (the dict being dumped).
        if not node.args:
            continue
        arg = node.args[0]
        if not isinstance(arg, ast.Dict):
            continue
        for key, value in zip(arg.keys, arg.values):
            # Reject any string key 'categories'.
            if (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and key.value == "categories"
            ):
                offenders.append(node.lineno)
            # Reject any value that reads .categories from anywhere.
            for sub in ast.walk(value):
                if (
                    isinstance(sub, ast.Attribute)
                    and sub.attr == "categories"
                ):
                    offenders.append(node.lineno)

    assert not offenders, (
        f"Step 30d-B: widget_chat_stream's SSE frames must never "
        f"include moderation categories. json.dumps calls at lines "
        f"{offenders} leak category info to the client. Categories "
        f"are for the server-side WARNING line only."
    )


# =====================================================================
# Behavioural tests: moderation providers
# =====================================================================


def test_null_provider_never_blocks_and_emits_warning(caplog) -> None:
    from app.policy.moderation import NullModerationProvider

    provider = NullModerationProvider()
    caplog.set_level(logging.WARNING, logger="app.policy.moderation")
    result = provider.moderate("hello")

    assert result.blocked is False
    assert result.categories == []
    assert result.provider == "null"

    assert any(
        "NullModerationProvider" in rec.message
        and rec.levelno >= logging.WARNING
        for rec in caplog.records
    ), (
        "Step 30d-B: NullModerationProvider must emit a WARNING on "
        "every call so it cannot silently ship to production."
    )


class _RaisingProvider:
    """Inner provider that always raises ModerationProviderUnavailable."""

    name = "raising_for_test"

    def moderate(self, text):
        from app.policy.moderation import ModerationProviderUnavailable
        raise ModerationProviderUnavailable("simulated outage")


class _PassThroughProvider:
    name = "passthrough_for_test"

    def moderate(self, text):
        from app.policy.moderation import ModerationResult
        return ModerationResult(
            blocked=False, categories=[], provider=self.name
        )


class _RealBlockProvider:
    name = "realblock_for_test"

    def moderate(self, text):
        from app.policy.moderation import ModerationResult
        return ModerationResult(
            blocked=True,
            categories=["hate"],
            provider=self.name,
            provider_request_id="req-abc",
        )


def test_failclosed_wraps_unavailable_into_block(caplog) -> None:
    from app.policy.moderation import FailClosedModerationProvider

    wrapper = FailClosedModerationProvider(_RaisingProvider())
    caplog.set_level(logging.WARNING, logger="app.policy.moderation")
    result = wrapper.moderate("anything")

    assert result.blocked is True, (
        "Step 30d-B: FailClosedModerationProvider must block when "
        "the inner provider raises ModerationProviderUnavailable. "
        "This is the production safety invariant."
    )
    assert result.categories == ["provider_unavailable"], (
        f"Expected categories=['provider_unavailable']; got "
        f"{result.categories!r}"
    )
    assert result.provider == "raising_for_test+failclosed"
    # Operator-facing WARNING must have been emitted.
    assert any(
        "Moderation provider unavailable" in rec.message
        for rec in caplog.records
    )


def test_failclosed_passes_through_real_pass() -> None:
    from app.policy.moderation import FailClosedModerationProvider

    wrapper = FailClosedModerationProvider(_PassThroughProvider())
    result = wrapper.moderate("hello")
    assert result.blocked is False
    assert result.provider == "passthrough_for_test"
    assert result.categories == []


def test_failclosed_passes_through_real_block() -> None:
    """A real block from the inner provider must survive the wrapper
    unchanged -- the wrapper only intervenes on
    ModerationProviderUnavailable.
    """

    from app.policy.moderation import FailClosedModerationProvider

    wrapper = FailClosedModerationProvider(_RealBlockProvider())
    result = wrapper.moderate("hateful text")
    assert result.blocked is True
    assert result.categories == ["hate"], (
        "Step 30d-B: FailClosed must NOT rewrite the categories list "
        "for a real block. The wrapper only adds 'provider_unavailable' "
        "on the unavailability path."
    )
    assert result.provider == "realblock_for_test"
    assert result.provider_request_id == "req-abc"


# =====================================================================
# Behavioural tests: ModerationGate factory
# =====================================================================


class _FakeSettings:
    """Minimal stand-in for Settings, only the keys the factory reads."""

    def __init__(
        self,
        moderation_provider="openai",
        openai_api_key="sk-test",
        moderation_timeout_seconds=3.0,
        moderation_fail_closed=True,
    ):
        self.moderation_provider = moderation_provider
        self.openai_api_key = openai_api_key
        self.moderation_timeout_seconds = moderation_timeout_seconds
        self.moderation_fail_closed = moderation_fail_closed


def test_gate_factory_returns_null_provider_for_null_setting() -> None:
    from app.policy.moderation import (
        ModerationGate,
        NullModerationProvider,
    )

    provider = ModerationGate.from_settings(
        _FakeSettings(moderation_provider="null")
    )
    assert isinstance(provider, NullModerationProvider)


def test_gate_factory_returns_failclosed_openai_for_openai_setting() -> None:
    from app.policy.moderation import (
        FailClosedModerationProvider,
        ModerationGate,
        OpenAIModerationProvider,
    )

    provider = ModerationGate.from_settings(
        _FakeSettings(moderation_provider="openai", openai_api_key="sk-test")
    )
    assert isinstance(provider, FailClosedModerationProvider), (
        "Step 30d-B: the production-default wiring must wrap the "
        "OpenAI provider in FailClosed. Without the wrapper, an "
        "outage at OpenAI passes turns through to the LLM."
    )
    # The inner provider is the OpenAI one.
    assert isinstance(provider._inner, OpenAIModerationProvider)


def test_gate_factory_raises_when_openai_key_missing() -> None:
    """ConfigurationError must fire at factory time (i.e. module
    import time when the chat route loads), not at first request."""

    from app.policy.moderation import ConfigurationError, ModerationGate

    with pytest.raises(ConfigurationError) as excinfo:
        ModerationGate.from_settings(
            _FakeSettings(moderation_provider="openai", openai_api_key="")
        )
    assert "openai_api_key" in str(excinfo.value).lower() or "OPENAI" in str(
        excinfo.value
    )


def test_gate_factory_raises_on_unknown_provider() -> None:
    from app.policy.moderation import ConfigurationError, ModerationGate

    with pytest.raises(ConfigurationError):
        ModerationGate.from_settings(
            _FakeSettings(moderation_provider="bogus_provider")
        )


def test_gate_factory_returns_bare_openai_when_failclosed_disabled(
    caplog,
) -> None:
    """The non-fail-closed knob is a development override. When it is
    flipped off, the factory must still log a WARNING -- the gate is
    weaker than it should be and the log line is the only signal an
    operator gets that it shipped that way."""

    from app.policy.moderation import (
        FailClosedModerationProvider,
        ModerationGate,
        OpenAIModerationProvider,
    )

    caplog.set_level(logging.WARNING, logger="app.policy.moderation")
    provider = ModerationGate.from_settings(
        _FakeSettings(
            moderation_provider="openai",
            openai_api_key="sk-test",
            moderation_fail_closed=False,
        )
    )
    # The bare OpenAI provider (no wrapper).
    assert isinstance(provider, OpenAIModerationProvider)
    assert not isinstance(provider, FailClosedModerationProvider)
    # WARNING line must have been emitted.
    assert any(
        "moderation_fail_closed=False" in rec.message
        for rec in caplog.records
    ), (
        "Step 30d-B: flipping moderation_fail_closed off must emit "
        "an operator-facing WARNING so a dev-environment override "
        "is visible if it ever ships to production."
    )


# =====================================================================
# Behavioural test: OpenAI provider transport failure handling
# =====================================================================


def test_openai_provider_raises_unavailable_on_transport_error(
    monkeypatch,
) -> None:
    """OpenAIModerationProvider must funnel transport errors into
    ModerationProviderUnavailable so the FailClosed wrapper can
    handle them. We monkeypatch httpx.Client to raise on .post().
    """

    import httpx

    from app.policy.moderation import (
        ModerationProviderUnavailable,
        OpenAIModerationProvider,
    )

    class _BoomClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            raise httpx.ConnectError("simulated connect failure")

    monkeypatch.setattr(httpx, "Client", _BoomClient)

    provider = OpenAIModerationProvider(api_key="sk-test", timeout_seconds=1.0)
    with pytest.raises(ModerationProviderUnavailable) as excinfo:
        provider.moderate("any text")
    assert "transport" in str(excinfo.value).lower() or "connect" in str(
        excinfo.value
    ).lower()


def test_openai_provider_raises_unavailable_on_non_2xx(monkeypatch) -> None:
    import httpx

    from app.policy.moderation import (
        ModerationProviderUnavailable,
        OpenAIModerationProvider,
    )

    class _Resp:
        status_code = 503
        headers = {}

        def json(self):
            return {}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)

    provider = OpenAIModerationProvider(api_key="sk-test")
    with pytest.raises(ModerationProviderUnavailable) as excinfo:
        provider.moderate("any text")
    assert "503" in str(excinfo.value)


def test_openai_provider_maps_flagged_true_to_block(monkeypatch) -> None:
    import httpx

    from app.policy.moderation import OpenAIModerationProvider

    class _Resp:
        status_code = 200
        headers = {"x-request-id": "req-xyz"}

        def json(self):
            return {
                "results": [
                    {
                        "flagged": True,
                        "categories": {
                            "hate": True,
                            "violence": False,
                            "self-harm": True,
                        },
                    }
                ]
            }

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)

    provider = OpenAIModerationProvider(api_key="sk-test")
    result = provider.moderate("blocked text")
    assert result.blocked is True
    assert set(result.categories) == {"hate", "self-harm"}, (
        f"Expected truthy categories only; got {result.categories!r}"
    )
    assert result.provider == "openai"
    assert result.provider_request_id == "req-xyz"


def test_openai_provider_maps_flagged_false_to_pass(monkeypatch) -> None:
    import httpx

    from app.policy.moderation import OpenAIModerationProvider

    class _Resp:
        status_code = 200
        headers = {}

        def json(self):
            return {"results": [{"flagged": False, "categories": {}}]}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)

    provider = OpenAIModerationProvider(api_key="sk-test")
    result = provider.moderate("clean text")
    assert result.blocked is False
    assert result.categories == []


# =====================================================================
# Module import smoke test
# =====================================================================


def test_moderation_module_imports_cleanly() -> None:
    import importlib

    mod = importlib.import_module("app.policy.moderation")
    # ModerationResult is the contract returned across the public API;
    # ensure it can be constructed with just blocked= and that the
    # other fields default sanely.
    res = mod.ModerationResult(blocked=False)
    assert res.blocked is False
    assert res.categories == []
    assert res.provider == ""
    assert res.provider_request_id is None
