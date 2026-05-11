"""
Step 30c -- regression tests for the action-classification gate and
its wiring into the tool broker.

Test strategy
=============

Same house style as tests/api/test_content_safety_gate.py (Step 30d
Deliverable B): AST contract tests + isolated behavioural tests
driven by inline fakes. No FastAPI TestClient, no test DB.

Two groups:

  AST tests
    Pin that the classification gate is wired into ToolBroker in the
    right shape and the right place. Future refactors that silently
    drop the gate, reorder it after tool.execute, or strip the
    APPROVAL_REQUIRED short-circuit trip a specific test.

  Behavioural tests
    Drive the classifiers and the broker directly to exercise:
      * Static classifier reads declared_tier off the class
      * Static classifier raises ToolTierUndeclared on missing tier
      * FailClosed converts ToolTierUndeclared into APPROVAL_REQUIRED
      * FailClosed converts any exception into APPROVAL_REQUIRED
      * Null classifier returns ROUTINE on everything (and warns)
      * Gate factory dispatches correctly per settings
      * Factory raises ConfigurationError on unknown provider
      * Broker executes ROUTINE and NOTIFY_AND_PROCEED tools
      * Broker does NOT execute APPROVAL_REQUIRED tools and instead
        returns a pending frame
      * Every ToolResult carries tier metadata (including the
        not-found path)
      * The three shipped tools declare the expected tiers

References
==========

  * app/policy/action_classification.py -- the units under test.
  * app/tools/broker.py -- the wired call site.
  * app/tools/base.py -- the declared_tier attribute pinned here.
  * ARCHITECTURE.md section 3.3 step 8 -- the design statement.
  * ARCHITECTURE.md section 4.9 -- the rejected-alternative bullet
    on synchronous-only invocation.
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


def _find_function(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found")


def _find_class(tree: ast.AST, name: str) -> ast.ClassDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"class {name!r} not found")


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


def _first_lineno_of_self_attr_call(
    node: ast.AST, self_attr: str, method: str
) -> int | None:
    """Return the line number of the first call to self.<self_attr>.<method>(...)
    inside node. Used to pin that broker.execute_tool calls
    self._classifier.classify(...) before tool.execute(...).
    """
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        # We are looking for `self._classifier.classify(...)`.
        if not isinstance(func, ast.Attribute) or func.attr != method:
            continue
        inner = func.value
        if (
            isinstance(inner, ast.Attribute)
            and inner.attr == self_attr
            and isinstance(inner.value, ast.Name)
            and inner.value.id == "self"
        ):
            return child.lineno
    return None


# =====================================================================
# AST tests: action-classification module shape
# =====================================================================


def test_action_classification_module_defines_required_symbols() -> None:
    """app/policy/action_classification.py must publish the full
    public API the broker depends on. A maintainer who renames any
    of these silently breaks the wiring at import time."""

    import app.policy.action_classification as mod

    for name in (
        "ActionTier",
        "ActionClassification",
        "ActionClassifier",
        "StaticTierRegistryClassifier",
        "NullActionClassifier",
        "FailClosedActionClassifier",
        "ActionClassificationGate",
        "ToolTierUndeclared",
        "ConfigurationError",
    ):
        assert hasattr(mod, name), (
            f"Step 30c: app.policy.action_classification must publish "
            f"{name!r}. A rename here breaks app/tools/broker.py at "
            f"import time."
        )


def test_action_tier_enum_has_exact_three_members() -> None:
    """ActionTier must expose exactly three members corresponding to
    the three tiers in ARCHITECTURE §3.3 step 8. Adding a fourth
    tier silently here without a doc edit is exactly the kind of
    drift this test exists to catch."""

    from app.policy.action_classification import ActionTier

    members = {m.name for m in ActionTier}
    assert members == {
        "ROUTINE",
        "NOTIFY_AND_PROCEED",
        "APPROVAL_REQUIRED",
    }, (
        f"Step 30c: ActionTier members drifted from ARCHITECTURE §3.3 "
        f"step 8. Got {sorted(members)!r}. Update ARCHITECTURE and "
        f"DRIFTS together if a fourth tier is intentional."
    )


def test_failclosed_classifier_handles_tool_tier_undeclared() -> None:
    """FailClosedActionClassifier.classify must have an
    `except ToolTierUndeclared` clause that returns
    tier=APPROVAL_REQUIRED. This is the core fail-closed invariant
    for Step 30c -- if a maintainer changes the tier here to ROUTINE
    or NOTIFY_AND_PROCEED, undeclared tools silently execute, which
    is exactly what Recap §4 forbids.
    """

    src = _read("app/policy/action_classification.py")
    tree = ast.parse(src)
    cls = _find_class(tree, "FailClosedActionClassifier")
    classify_fn = next(
        (n for n in ast.walk(cls)
         if isinstance(n, ast.FunctionDef) and n.name == "classify"),
        None,
    )
    assert classify_fn is not None

    matched = None
    for node in ast.walk(classify_fn):
        if (
            isinstance(node, ast.ExceptHandler)
            and isinstance(node.type, ast.Name)
            and node.type.id == "ToolTierUndeclared"
        ):
            matched = node
            break
    assert matched is not None, (
        "Step 30c: FailClosedActionClassifier.classify must catch "
        "ToolTierUndeclared. Without this handler the wrapper does "
        "not actually fail closed and an undeclared tool will raise "
        "instead of being routed to APPROVAL_REQUIRED."
    )

    # The handler must construct ActionClassification(tier=...APPROVAL_REQUIRED).
    saw_approval_required = False
    for node in ast.walk(matched):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Name) and func.id == "ActionClassification"
        ):
            continue
        for kw in node.keywords:
            if kw.arg != "tier":
                continue
            # Accept either `ActionTier.APPROVAL_REQUIRED` or the
            # bare name; in this module body it should be the
            # attribute form.
            val = kw.value
            if (
                isinstance(val, ast.Attribute)
                and val.attr == "APPROVAL_REQUIRED"
            ):
                saw_approval_required = True
                break
    assert saw_approval_required, (
        "Step 30c: FailClosedActionClassifier.classify's "
        "except ToolTierUndeclared handler must return "
        "ActionClassification(tier=ActionTier.APPROVAL_REQUIRED, ...). "
        "Any other tier here silently opens the broker to undeclared "
        "tools."
    )


def test_failclosed_classifier_also_handles_bare_exception() -> None:
    """The fail-closed wrapper must also catch the broad Exception
    case, otherwise a classifier that raises (e.g. a future
    OffPatternActionClassifier that errors on a memory lookup)
    crashes the broker instead of routing the action to
    APPROVAL_REQUIRED."""

    src = _read("app/policy/action_classification.py")
    tree = ast.parse(src)
    cls = _find_class(tree, "FailClosedActionClassifier")
    classify_fn = next(
        (n for n in ast.walk(cls)
         if isinstance(n, ast.FunctionDef) and n.name == "classify"),
        None,
    )
    assert classify_fn is not None

    saw_bare_exception_handler = False
    for node in ast.walk(classify_fn):
        if (
            isinstance(node, ast.ExceptHandler)
            and isinstance(node.type, ast.Name)
            and node.type.id == "Exception"
        ):
            saw_bare_exception_handler = True
            break
    assert saw_bare_exception_handler, (
        "Step 30c: FailClosedActionClassifier.classify must also catch "
        "Exception so a classifier crash routes to APPROVAL_REQUIRED "
        "rather than propagating into the broker."
    )


# =====================================================================
# AST tests: broker wiring
# =====================================================================


def test_broker_imports_action_classification_symbols() -> None:
    src = _read("app/tools/broker.py")
    assert "from app.policy.action_classification import" in src, (
        "Step 30c: app/tools/broker.py must import the action "
        "classification symbols from app.policy.action_classification."
    )
    for symbol in ("ActionClassifier", "ActionClassificationGate", "ActionTier"):
        assert symbol in src, (
            f"Step 30c: broker.py must reference {symbol!r}."
        )


def test_broker_constructor_accepts_classifier() -> None:
    """ToolBroker.__init__ must accept a classifier parameter. The
    historical `ToolBroker(registry)` signature is preserved by
    making the parameter optional, but it must exist so tests and
    future callers can inject a fake without monkey-patching
    module-level state."""

    tree = _parse("app/tools/broker.py")
    cls = _find_class(tree, "ToolBroker")
    init_fn = next(
        (n for n in ast.walk(cls)
         if isinstance(n, ast.FunctionDef) and n.name == "__init__"),
        None,
    )
    assert init_fn is not None
    arg_names = [a.arg for a in init_fn.args.args]
    assert "classifier" in arg_names, (
        "Step 30c: ToolBroker.__init__ must accept a `classifier` "
        f"parameter so the gate is injectable. Got {arg_names!r}."
    )


def test_broker_classifies_before_executing() -> None:
    """In ToolBroker.execute_tool, self._classifier.classify must
    appear at a lower line number than tool.execute.

    This is the core structural guarantee of Step 30c: an
    APPROVAL_REQUIRED action cannot leak past the gate and execute.
    A maintainer who reorders these silently breaks the contract
    Recap §4 names as load-bearing.
    """

    tree = _parse("app/tools/broker.py")
    cls = _find_class(tree, "ToolBroker")
    fn = next(
        (n for n in ast.walk(cls)
         if isinstance(n, ast.FunctionDef) and n.name == "execute_tool"),
        None,
    )
    assert fn is not None

    classify_line = _first_lineno_of_self_attr_call(fn, "_classifier", "classify")
    execute_line = _first_lineno_of_call_attr(fn, "tool", "execute")

    assert classify_line is not None, (
        "Step 30c: ToolBroker.execute_tool must call "
        "self._classifier.classify(tool)."
    )
    assert execute_line is not None, (
        "ToolBroker.execute_tool must still call tool.execute(...) "
        "for the ROUTINE/NOTIFY paths. If this fails the broker "
        "shape changed and the gate may have been short-circuited."
    )
    assert classify_line < execute_line, (
        f"Step 30c: self._classifier.classify (line {classify_line}) "
        f"must execute BEFORE tool.execute (line {execute_line}). "
        f"Reversing this order would let an APPROVAL_REQUIRED action "
        f"execute before the gate has a chance to short-circuit."
    )


def test_broker_short_circuits_approval_required_without_executing() -> None:
    """execute_tool must contain an `if classification.tier ==
    ActionTier.APPROVAL_REQUIRED:` branch that returns BEFORE the
    `tool.execute(...)` call site. A maintainer who removes this
    branch (or moves the return below tool.execute) opens the gate.
    """

    tree = _parse("app/tools/broker.py")
    cls = _find_class(tree, "ToolBroker")
    fn = next(
        (n for n in ast.walk(cls)
         if isinstance(n, ast.FunctionDef) and n.name == "execute_tool"),
        None,
    )
    assert fn is not None

    # Find an `if` whose test references APPROVAL_REQUIRED and whose
    # body contains a Return.
    approval_branch_line: int | None = None
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        test_src = ast.dump(node.test)
        if "APPROVAL_REQUIRED" not in test_src:
            continue
        has_return = any(
            isinstance(child, ast.Return) for child in ast.walk(node)
        )
        if has_return:
            approval_branch_line = node.lineno
            break

    assert approval_branch_line is not None, (
        "Step 30c: ToolBroker.execute_tool must contain an `if "
        "classification.tier == ActionTier.APPROVAL_REQUIRED:` branch "
        "that returns without calling tool.execute(...). The pending "
        "frame is how the runtime layer learns the action was gated."
    )

    execute_line = _first_lineno_of_call_attr(fn, "tool", "execute")
    assert execute_line is not None
    assert approval_branch_line < execute_line, (
        f"Step 30c: the APPROVAL_REQUIRED short-circuit (line "
        f"{approval_branch_line}) must appear BEFORE tool.execute "
        f"(line {execute_line}). Otherwise the tool runs before the "
        f"gate gets a chance to refuse it."
    )


# =====================================================================
# AST tests: tools declare a tier
# =====================================================================


@pytest.mark.parametrize(
    "rel_path, class_name, expected_tier_name",
    [
        ("app/tools/implementations/save_memory_tool.py",
         "SaveMemoryTool", "ROUTINE"),
        ("app/tools/implementations/session_summary_tool.py",
         "SessionSummaryTool", "ROUTINE"),
        ("app/tools/implementations/escalate_tool.py",
         "EscalateTool", "NOTIFY_AND_PROCEED"),
    ],
)
def test_shipped_tool_declares_expected_tier(
    rel_path: str, class_name: str, expected_tier_name: str
) -> None:
    """Each tool that ships in app/tools/implementations/ must
    declare a tier explicitly on its class. The expected tier is
    pinned here so a silent retier (e.g. someone changing escalate
    from NOTIFY_AND_PROCEED to ROUTINE because it was annoying in
    a demo) trips a test rather than reaching production.
    """

    tree = _parse(rel_path)
    cls = _find_class(tree, class_name)

    # Look for `declared_tier = ActionTier.<expected>` at class body.
    found_tier_name: str | None = None
    for node in cls.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Name) and t.id == "declared_tier"
            for t in node.targets
        ):
            continue
        val = node.value
        if isinstance(val, ast.Attribute) and isinstance(val.value, ast.Name):
            if val.value.id == "ActionTier":
                found_tier_name = val.attr
                break

    assert found_tier_name is not None, (
        f"Step 30c: {class_name} in {rel_path} must declare "
        f"`declared_tier = ActionTier.<TIER>` on the class body so "
        f"the static classifier can read it."
    )
    assert found_tier_name == expected_tier_name, (
        f"Step 30c: {class_name}.declared_tier drifted from "
        f"{expected_tier_name!r} to {found_tier_name!r}. If the retier "
        f"is intentional, update this test AND CANONICAL_RECAP §12 / "
        f"ARCHITECTURE §3.3 step 8 together."
    )


def test_base_tool_declares_default_tier_none() -> None:
    """LucielTool.declared_tier must default to None on the base
    class so a subclass that forgets to declare a tier inherits a
    classifier-rejected default, not a silent ROUTINE. The
    fail-closed wrapper then maps that to APPROVAL_REQUIRED.
    """

    tree = _parse("app/tools/base.py")
    cls = _find_class(tree, "LucielTool")

    found = False
    for node in cls.body:
        if not isinstance(node, ast.AnnAssign):
            # Could also be plain Assign in some Python versions; check both.
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "declared_tier"
                for t in node.targets
            ):
                value = node.value
                if isinstance(value, ast.Constant) and value.value is None:
                    found = True
                    break
            continue
        target = node.target
        if isinstance(target, ast.Name) and target.id == "declared_tier":
            # Accept either `declared_tier: ActionTier | None = None`
            # or `declared_tier: Optional[ActionTier] = None`.
            value = node.value
            if isinstance(value, ast.Constant) and value.value is None:
                found = True
                break

    assert found, (
        "Step 30c: LucielTool.declared_tier must default to None on "
        "the base class so a subclass that forgets to declare a tier "
        "fails closed to APPROVAL_REQUIRED rather than silently "
        "inheriting ROUTINE."
    )


# =====================================================================
# Behavioural tests: classifiers
# =====================================================================


class _FakeTool:
    """Minimal stand-in for a tool. Tests set declared_tier per case."""

    def __init__(self, name: str = "fake", declared_tier=None) -> None:
        self.name = name
        self.declared_tier = declared_tier


def test_static_classifier_reads_declared_tier() -> None:
    from app.policy.action_classification import (
        ActionTier,
        StaticTierRegistryClassifier,
    )

    clf = StaticTierRegistryClassifier()
    for tier in ActionTier:
        result = clf.classify(_FakeTool(declared_tier=tier))
        assert result.tier == tier
        assert result.reason == "declared_tier"
        assert result.classifier == "static"


def test_static_classifier_raises_on_missing_tier() -> None:
    from app.policy.action_classification import (
        StaticTierRegistryClassifier,
        ToolTierUndeclared,
    )

    clf = StaticTierRegistryClassifier()
    with pytest.raises(ToolTierUndeclared):
        clf.classify(_FakeTool(declared_tier=None))


def test_static_classifier_accepts_string_tier_value() -> None:
    """Defensive ergonomics: if a maintainer writes
    `declared_tier = 'routine'` (string) instead of
    `ActionTier.ROUTINE` (enum), the classifier should still
    succeed. A typo like 'rountine' must still raise."""

    from app.policy.action_classification import (
        ActionTier,
        StaticTierRegistryClassifier,
        ToolTierUndeclared,
    )

    clf = StaticTierRegistryClassifier()
    ok = clf.classify(_FakeTool(declared_tier="routine"))
    assert ok.tier == ActionTier.ROUTINE

    with pytest.raises(ToolTierUndeclared):
        clf.classify(_FakeTool(declared_tier="rountine"))


def test_failclosed_wrapper_converts_undeclared_to_approval_required() -> None:
    from app.policy.action_classification import (
        ActionTier,
        FailClosedActionClassifier,
        StaticTierRegistryClassifier,
    )

    clf = FailClosedActionClassifier(StaticTierRegistryClassifier())
    result = clf.classify(_FakeTool(declared_tier=None))
    assert result.tier == ActionTier.APPROVAL_REQUIRED
    # reason='tier_undeclared' is deliberately distinct from the
    # broker's not-found path 'unknown_tool' so an audit log can
    # tell a stray LLM-emitted tool name (handled inside the broker)
    # apart from a registered tool whose maintainer forgot to
    # declare a tier (handled here in the fail-closed wrapper).
    assert result.reason == "tier_undeclared"
    assert result.classifier == "static+failclosed"


def test_failclosed_wrapper_converts_arbitrary_exception_to_approval_required() -> None:
    """A classifier that raises a non-ToolTierUndeclared exception
    (e.g. a future memory-backed OffPatternActionClassifier with a
    DB timeout) must NOT crash the broker. It must route the action
    to APPROVAL_REQUIRED instead."""

    from app.policy.action_classification import (
        ActionTier,
        FailClosedActionClassifier,
    )

    class _BrokenClassifier:
        name = "broken"

        def classify(self, tool):
            raise RuntimeError("simulated downstream failure")

    clf = FailClosedActionClassifier(_BrokenClassifier())
    result = clf.classify(_FakeTool(declared_tier=None))
    assert result.tier == ActionTier.APPROVAL_REQUIRED
    assert result.reason == "classifier_error"
    assert result.classifier == "broken+failclosed"


def test_failclosed_wrapper_does_not_short_circuit_real_classifications() -> None:
    """The wrapper must pass through a real classification untouched.
    A maintainer who accidentally returns APPROVAL_REQUIRED for the
    happy path turns the entire broker into a confirmation loop."""

    from app.policy.action_classification import (
        ActionTier,
        FailClosedActionClassifier,
        StaticTierRegistryClassifier,
    )

    clf = FailClosedActionClassifier(StaticTierRegistryClassifier())

    routine = clf.classify(_FakeTool(declared_tier=ActionTier.ROUTINE))
    assert routine.tier == ActionTier.ROUTINE
    assert routine.reason == "declared_tier"

    notify = clf.classify(
        _FakeTool(declared_tier=ActionTier.NOTIFY_AND_PROCEED)
    )
    assert notify.tier == ActionTier.NOTIFY_AND_PROCEED


def test_null_classifier_returns_routine_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.policy.action_classification import (
        ActionTier,
        NullActionClassifier,
    )

    clf = NullActionClassifier()
    with caplog.at_level(logging.WARNING, logger="app.policy.action_classification"):
        result = clf.classify(_FakeTool(declared_tier=None))

    assert result.tier == ActionTier.ROUTINE
    assert result.reason == "null_classifier"
    assert result.classifier == "null"
    assert any(
        "NullActionClassifier in use" in record.getMessage()
        for record in caplog.records
    ), (
        "Step 30c: NullActionClassifier must log a WARNING on every "
        "call so a misconfigured deploy is observable."
    )


# =====================================================================
# Behavioural tests: factory
# =====================================================================


class _FakeSettings:
    def __init__(self, **kwargs) -> None:
        # Defaults match the production wiring.
        self.action_classifier = kwargs.get("action_classifier", "static")
        self.action_classifier_fail_closed = kwargs.get(
            "action_classifier_fail_closed", True
        )


def test_factory_returns_failclosed_static_by_default() -> None:
    from app.policy.action_classification import (
        ActionClassificationGate,
        FailClosedActionClassifier,
        StaticTierRegistryClassifier,
    )

    clf = ActionClassificationGate.from_settings(_FakeSettings())
    assert isinstance(clf, FailClosedActionClassifier)
    assert isinstance(clf._inner, StaticTierRegistryClassifier)
    assert clf.name == "static+failclosed"


def test_factory_returns_bare_static_when_fail_closed_off() -> None:
    from app.policy.action_classification import (
        ActionClassificationGate,
        StaticTierRegistryClassifier,
    )

    clf = ActionClassificationGate.from_settings(
        _FakeSettings(action_classifier_fail_closed=False)
    )
    assert isinstance(clf, StaticTierRegistryClassifier)


def test_factory_returns_null_when_requested() -> None:
    from app.policy.action_classification import (
        ActionClassificationGate,
        NullActionClassifier,
    )

    clf = ActionClassificationGate.from_settings(
        _FakeSettings(action_classifier="null")
    )
    assert isinstance(clf, NullActionClassifier)


def test_factory_raises_on_unknown_provider() -> None:
    from app.policy.action_classification import (
        ActionClassificationGate,
        ConfigurationError,
    )

    with pytest.raises(ConfigurationError):
        ActionClassificationGate.from_settings(
            _FakeSettings(action_classifier="bogus")
        )


# =====================================================================
# Behavioural tests: broker integration
# =====================================================================


@pytest.fixture
def broker():
    """Construct a broker with the production fail-closed gate and
    the default registry. We import inside the fixture so the test
    module's import chain remains light."""

    # Step 30d added DATABASE_URL gating at module import time; the
    # broker's lazy-import of settings runs the same chain. Provide
    # a dev-shape value for the import-time validator.
    import os

    os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
    from app.tools.broker import ToolBroker
    from app.tools.registry import ToolRegistry

    return ToolBroker(ToolRegistry())


def test_broker_executes_routine_tool_and_stamps_tier(broker) -> None:
    """save_memory is declared ROUTINE -- the broker must run it and
    return a successful ToolResult whose metadata['tier'] is
    'routine'."""

    result = broker.execute_tool(
        "save_memory",
        {"category": "preference", "content": "prefers terminal-based dev"},
    )
    assert result.success is True
    assert result.metadata["tier"] == "routine"
    assert result.metadata["tier_reason"] == "declared_tier"
    # Original tool metadata must survive the tier stamp.
    assert result.metadata.get("category") == "preference"


def test_broker_executes_notify_and_proceed_tool_and_stamps_tier(broker) -> None:
    """escalate_to_human is declared NOTIFY_AND_PROCEED -- the broker
    must run it and surface the tier so the runtime layer can render
    a notification."""

    result = broker.execute_tool(
        "escalate_to_human", {"reason": "frustrated user"}
    )
    assert result.success is True
    assert result.metadata["tier"] == "notify_and_proceed"
    assert result.metadata.get("escalated") is True


def test_broker_refuses_unknown_tool_with_approval_required(broker) -> None:
    """A tool that is not in the registry at all must fail closed
    too, not just fail. The tier is APPROVAL_REQUIRED with reason
    'unknown_tool' (set inside the broker; never produced by a
    classifier). This is deliberately distinct from the
    'tier_undeclared' reason that the fail-closed wrapper produces
    when a registered tool is missing `declared_tier` — the two
    failure modes look different to an auditor on purpose so a
    stray LLM-emitted tool name (this test) can be told apart from
    a maintainer forgetting to declare a tier on a real tool
    (test_broker_refuses_undeclared_tier_tool_without_executing).
    Both still route to APPROVAL_REQUIRED."""

    result = broker.execute_tool("definitely_not_a_real_tool", {})
    assert result.success is False
    assert result.metadata["tier"] == "approval_required"
    assert result.metadata["tier_reason"] == "unknown_tool"


def test_broker_refuses_undeclared_tier_tool_without_executing() -> None:
    """A tool whose class is in the registry but does NOT declare a
    tier must NOT execute. The broker must return a pending frame
    with proposed_parameters so the runtime layer can render a
    confirmation. This is the load-bearing invariant Step 30c
    enforces -- silent fall-through here would re-open the drift
    `D-confirmation-gate-not-enforced-2026-05-09`.
    """

    import os

    os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
    from app.tools.base import LucielTool, ToolResult
    from app.tools.broker import ToolBroker
    from app.tools.registry import ToolRegistry

    executed: list[dict] = []

    class _UndeclaredTool(LucielTool):
        # Deliberately no declared_tier override.
        @property
        def name(self) -> str:
            return "undeclared_test_tool"

        @property
        def description(self) -> str:
            return "test tool with no declared tier"

        @property
        def parameter_schema(self) -> dict:
            return {}

        def execute(self, **kwargs) -> ToolResult:
            executed.append(dict(kwargs))
            return ToolResult(
                success=True, output="SHOULD NOT BE REACHED"
            )

    registry = ToolRegistry()
    registry.register(_UndeclaredTool())
    broker = ToolBroker(registry)

    result = broker.execute_tool(
        "undeclared_test_tool", {"arbitrary": "payload"}
    )

    # The tool MUST NOT have executed.
    assert executed == [], (
        "Step 30c: an undeclared-tier tool executed despite the "
        "fail-closed gate. This re-opens the confirmation-gate drift."
    )
    assert result.success is False
    assert result.metadata["tier"] == "approval_required"
    # tier_reason MUST be 'tier_undeclared' (set by the fail-closed
    # wrapper) and MUST NOT be 'unknown_tool' (the broker's
    # not-found path) -- this is the audit-clarity distinction
    # introduced after the live-e2e review of Step 30c.
    assert result.metadata["tier_reason"] == "tier_undeclared"
    assert result.metadata["pending"] is True
    assert result.metadata["tool_name"] == "undeclared_test_tool"
    assert result.metadata["proposed_parameters"] == {"arbitrary": "payload"}


def test_broker_with_null_classifier_executes_undeclared_tool() -> None:
    """Sanity-check the other side of the gate: with the null
    classifier wired in (dev override), an undeclared tool DOES run.
    This proves the gate is actually doing the work in the
    fail-closed default, not some unrelated guard."""

    import os

    os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
    from app.policy.action_classification import NullActionClassifier
    from app.tools.base import LucielTool, ToolResult
    from app.tools.broker import ToolBroker
    from app.tools.registry import ToolRegistry

    executed: list[bool] = []

    class _UndeclaredTool(LucielTool):
        @property
        def name(self) -> str:
            return "null_clf_test_tool"

        @property
        def description(self) -> str:
            return "x"

        @property
        def parameter_schema(self) -> dict:
            return {}

        def execute(self, **kwargs) -> ToolResult:
            executed.append(True)
            return ToolResult(success=True, output="ran")

    registry = ToolRegistry()
    registry.register(_UndeclaredTool())
    broker = ToolBroker(registry, classifier=NullActionClassifier())

    result = broker.execute_tool("null_clf_test_tool", {})
    assert executed == [True]
    assert result.success is True
    assert result.metadata["tier"] == "routine"
    assert result.metadata["tier_reason"] == "null_classifier"
