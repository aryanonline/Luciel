"""
Step 30c — Live end-to-end harness against the success criteria
in docs/CANONICAL_RECAP.md §12 (row "30c").

This is NOT a unit test. It is a live exercise of the SHIPPED code
paths — real ToolBroker, real ToolRegistry, real shipped
LucielTool subclasses — against the recap's success claims for
Step 30c. The point is to demonstrate the gate works against the
production code, not against inline fakes.

Each numbered scenario maps to a recap claim. The script asserts
every claim and prints a row per claim. Exit code 0 = all claims
satisfied. Non-zero = at least one claim violated.

Run with:
    DATABASE_URL="sqlite:///:memory:" python step_30c_live_e2e.py
"""

from __future__ import annotations

import os
import sys
import traceback
from typing import Any

# Ensure module imports work
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from app.policy.action_classification import (
    ActionClassificationGate,
    ActionTier,
    FailClosedActionClassifier,
    NullActionClassifier,
    StaticTierRegistryClassifier,
)
from app.tools.base import LucielTool, ToolResult
from app.tools.broker import ToolBroker
from app.tools.implementations.escalate_tool import EscalateTool
from app.tools.implementations.save_memory_tool import SaveMemoryTool
from app.tools.implementations.session_summary_tool import SessionSummaryTool
from app.tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# Test harness scaffolding
# ---------------------------------------------------------------------------


class ScenarioResult:
    def __init__(self, name: str, passed: bool, detail: str) -> None:
        self.name = name
        self.passed = passed
        self.detail = detail


results: list[ScenarioResult] = []


def record(name: str, passed: bool, detail: str = "") -> None:
    results.append(ScenarioResult(name, passed, detail))
    flag = "PASS" if passed else "FAIL"
    print(f"  [{flag}] {name}")
    if detail:
        print(f"         {detail}")


def header(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def must_be(actual: Any, expected: Any, label: str) -> bool:
    ok = actual == expected
    if not ok:
        print(f"         {label}: expected={expected!r} actual={actual!r}")
    return ok


# ---------------------------------------------------------------------------
# Build the production broker exactly as production builds it.
# Then build a fail-open broker for comparison.
# ---------------------------------------------------------------------------

header("SETUP — building production-shaped ToolBroker")

# 1. Production broker — no classifier injected, so it builds the
#    fail-closed static gate from settings.
prod_broker = ToolBroker(ToolRegistry())
print(f"  Production broker classifier: {prod_broker._classifier.name}")

# 2. Inspect classifier wiring
wrapped = isinstance(prod_broker._classifier, FailClosedActionClassifier)
inner = getattr(prod_broker._classifier, "_inner", None)
inner_static = isinstance(inner, StaticTierRegistryClassifier)
print(
    f"  Wrapped fail-closed: {wrapped}, inner static: {inner_static}"
)

# 3. Confirm settings-driven knobs are present
from app.core.config import settings  # noqa: E402

print(
    f"  Settings.action_classifier={settings.action_classifier!r} "
    f"Settings.action_classifier_fail_closed={settings.action_classifier_fail_closed!r}"
)


# ---------------------------------------------------------------------------
# CLAIM 1: ROUTINE tool (save_memory) executes decisively and the
# returned ToolResult.metadata carries tier=routine.
# ---------------------------------------------------------------------------

header("CLAIM 1 — ROUTINE tool executes; tier stamped")

result = prod_broker.execute_tool(
    "save_memory",
    {"category": "preference", "content": "Aryan prefers concise summaries."},
    session_id="live-e2e-1",
    user_id="aryan",
)

print(f"  ToolResult.success = {result.success}")
print(f"  ToolResult.output  = {result.output!r}")
print(f"  ToolResult.error   = {result.error!r}")
print(f"  ToolResult.metadata = {result.metadata}")

# Canonical tier_reason is 'declared_tier' (verified against
# StaticTierRegistryClassifier in app/policy/action_classification.py).
claim_1a = result.metadata.get("tier") == "routine"
claim_1b = result.metadata.get("tier_reason") == "declared_tier"
claim_1c = "classifier" in result.metadata
claim_1d = result.metadata.get("pending") is None  # no pending flag on a routine call
claim_1e = result.success is True  # the underlying tool also succeeded

record("ROUTINE → tier='routine' on metadata", claim_1a)
record("ROUTINE → tier_reason='declared_tier'", claim_1b)
record("ROUTINE → classifier name stamped", claim_1c)
record("ROUTINE → no pending flag", claim_1d)
record("ROUTINE → underlying tool ran successfully", claim_1e)


# ---------------------------------------------------------------------------
# CLAIM 2: NOTIFY_AND_PROCEED tool (escalate_to_human) executes and is
# tiered notify_and_proceed.
# ---------------------------------------------------------------------------

header("CLAIM 2 — NOTIFY_AND_PROCEED tool executes; tier stamped")

result = prod_broker.execute_tool(
    "escalate_to_human",
    {
        "reason": "Customer asked for human agent.",
        "summary": "Live e2e harness scenario for Step 30c.",
        "urgency": "low",
    },
    session_id="live-e2e-2",
    user_id="aryan",
)

print(f"  ToolResult.success = {result.success}")
print(f"  ToolResult.metadata.tier = {result.metadata.get('tier')}")
print(f"  ToolResult.metadata.tier_reason = {result.metadata.get('tier_reason')}")
print(f"  ToolResult.output (first 80 chars) = {result.output[:80]!r}")

claim_2a = result.metadata.get("tier") == "notify_and_proceed"
claim_2b = result.metadata.get("pending") is None  # still executes

record("NOTIFY_AND_PROCEED → tier='notify_and_proceed'", claim_2a)
record("NOTIFY_AND_PROCEED → executes (no pending flag)", claim_2b)


# ---------------------------------------------------------------------------
# CLAIM 3: APPROVAL_REQUIRED short-circuit — undeclared-tier tool must
# NOT execute. We build a deliberately mis-declared tool and register
# it into a fresh broker.
# ---------------------------------------------------------------------------

header("CLAIM 3 — Undeclared-tier tool is refused without executing")


class UndeclaredTierTool(LucielTool):
    """A deliberately undeclared tool. Inherits declared_tier=None
    from the base class. Used here to prove the broker fails closed."""

    EXECUTE_CALLED = False

    @property
    def name(self) -> str:
        return "undeclared_tier_probe"

    @property
    def description(self) -> str:
        return "Probe tool — should never execute under fail-closed gate."

    @property
    def parameter_schema(self) -> dict[str, Any]:
        return {}

    def execute(self, **kwargs: Any) -> ToolResult:
        UndeclaredTierTool.EXECUTE_CALLED = True
        return ToolResult(success=True, output="should-not-be-reached")


registry = ToolRegistry()
registry.register(UndeclaredTierTool())
broker = ToolBroker(registry)

result = broker.execute_tool("undeclared_tier_probe", {"x": 1, "y": "z"})

print(f"  ToolResult.success = {result.success}")
print(f"  ToolResult.error = {result.error!r}")
print(f"  ToolResult.metadata = {result.metadata}")
print(f"  UndeclaredTierTool.EXECUTE_CALLED = {UndeclaredTierTool.EXECUTE_CALLED}")

claim_3a = result.success is False
claim_3b = result.metadata.get("tier") == "approval_required"
claim_3c = result.metadata.get("pending") is True
claim_3d = result.metadata.get("tool_name") == "undeclared_tier_probe"
claim_3e = result.metadata.get("proposed_parameters") == {"x": 1, "y": "z"}
claim_3f = UndeclaredTierTool.EXECUTE_CALLED is False
claim_3g = "approval" in (result.error or "").lower()
# Audit-clarity refinement: tier_reason MUST distinguish a
# registered-but-undeclared tool ('tier_undeclared') from a
# stray unknown tool name ('unknown_tool'). Both still route to
# APPROVAL_REQUIRED; the distinction is for the auditor.
claim_3h = result.metadata.get("tier_reason") == "tier_undeclared"

record("Undeclared → success=False", claim_3a)
record("Undeclared → tier='approval_required'", claim_3b)
record("Undeclared → pending=True", claim_3c)
record("Undeclared → tool_name echoed in metadata", claim_3d)
record("Undeclared → proposed_parameters echoed verbatim", claim_3e)
record(
    "Undeclared → tool.execute() WAS NOT CALLED (this is the bug we just closed)",
    claim_3f,
)
record("Undeclared → error message mentions approval", claim_3g)
record("Undeclared → tier_reason='tier_undeclared' (distinct from 'unknown_tool')", claim_3h)


# ---------------------------------------------------------------------------
# CLAIM 4: Unknown tool → APPROVAL_REQUIRED + tier_reason='unknown_tool'.
# ---------------------------------------------------------------------------

header("CLAIM 4 — Unknown tool routes to APPROVAL_REQUIRED + unknown_tool reason")

result = prod_broker.execute_tool("there_is_no_such_tool", {"foo": "bar"})

print(f"  ToolResult.success = {result.success}")
print(f"  ToolResult.metadata = {result.metadata}")

claim_4a = result.success is False
claim_4b = result.metadata.get("tier") == "approval_required"
claim_4c = result.metadata.get("tier_reason") == "unknown_tool"

record("Unknown tool → success=False", claim_4a)
record("Unknown tool → tier='approval_required'", claim_4b)
record("Unknown tool → tier_reason='unknown_tool'", claim_4c)


# ---------------------------------------------------------------------------
# CLAIM 5: Fail-closed wrapper converts arbitrary classifier exceptions
# into APPROVAL_REQUIRED — proves the gate cannot crash open.
# ---------------------------------------------------------------------------

header("CLAIM 5 — Fail-closed wrapper converts arbitrary exceptions to APPROVAL_REQUIRED")


class CrashingClassifier:
    name = "crashing"

    def classify(self, tool: LucielTool) -> Any:
        raise RuntimeError("Simulated classifier explosion.")


fail_closed = FailClosedActionClassifier(CrashingClassifier())
broker = ToolBroker(ToolRegistry(), classifier=fail_closed)

# Even save_memory (ROUTINE in production) should be gated to
# APPROVAL_REQUIRED if the classifier explodes.
SaveMemoryTool_execute_was_called = {"value": False}
original_execute = SaveMemoryTool.execute

def spy_execute(self, **kwargs):
    SaveMemoryTool_execute_was_called["value"] = True
    return original_execute(self, **kwargs)

SaveMemoryTool.execute = spy_execute  # type: ignore
try:
    result = broker.execute_tool(
        "save_memory",
        {"content": "should not execute", "memory_type": "preference"},
        session_id="live-e2e-5",
        user_id="aryan",
    )
finally:
    SaveMemoryTool.execute = original_execute  # type: ignore

print(f"  ToolResult.success = {result.success}")
print(f"  ToolResult.metadata = {result.metadata}")
print(
    f"  SaveMemoryTool.execute called? {SaveMemoryTool_execute_was_called['value']}"
)

claim_5a = result.metadata.get("tier") == "approval_required"
claim_5b = SaveMemoryTool_execute_was_called["value"] is False
claim_5c = result.metadata.get("pending") is True

record("Crashing classifier → tier='approval_required'", claim_5a)
record("Crashing classifier → tool.execute() NOT called", claim_5b)
record("Crashing classifier → pending=True", claim_5c)


# ---------------------------------------------------------------------------
# CLAIM 6: Settings knobs work — turning fail-closed OFF and selecting
# the null classifier lets undeclared tools through (this is the dev
# escape hatch documented in the recap).
# ---------------------------------------------------------------------------

header("CLAIM 6 — Settings knobs: NullActionClassifier lets undeclared tools through")

# Build the null gate directly (mimics what from_settings does when
# action_classifier='null')
null_classifier = NullActionClassifier()
registry = ToolRegistry()
registry.register(UndeclaredTierTool())
broker = ToolBroker(registry, classifier=null_classifier)

UndeclaredTierTool.EXECUTE_CALLED = False
result = broker.execute_tool("undeclared_tier_probe", {})

print(f"  ToolResult.success = {result.success}")
print(f"  ToolResult.metadata.tier = {result.metadata.get('tier')}")
print(f"  UndeclaredTierTool.EXECUTE_CALLED = {UndeclaredTierTool.EXECUTE_CALLED}")

claim_6a = UndeclaredTierTool.EXECUTE_CALLED is True
claim_6b = result.metadata.get("tier") == "routine"

record("Null classifier → undeclared tool DOES execute (dev escape)", claim_6a)
record("Null classifier → tier='routine'", claim_6b)


# ---------------------------------------------------------------------------
# CLAIM 7: from_settings factory dispatch — verify production default
# returns a FailClosedActionClassifier(StaticTierRegistryClassifier).
# ---------------------------------------------------------------------------

header("CLAIM 7 — ActionClassificationGate.from_settings respects knobs")


class FakeSettings:
    def __init__(self, provider: str, fail_closed: bool) -> None:
        self.action_classifier = provider
        self.action_classifier_fail_closed = fail_closed


gate_default = ActionClassificationGate.from_settings(
    FakeSettings("static", True)
)
gate_bare = ActionClassificationGate.from_settings(
    FakeSettings("static", False)
)
gate_null = ActionClassificationGate.from_settings(
    FakeSettings("null", True)
)

claim_7a = isinstance(gate_default, FailClosedActionClassifier)
claim_7b = isinstance(getattr(gate_default, "_inner", None), StaticTierRegistryClassifier)
claim_7c = isinstance(gate_bare, StaticTierRegistryClassifier)
claim_7d = isinstance(gate_null, NullActionClassifier)

# Unknown provider must raise
try:
    ActionClassificationGate.from_settings(FakeSettings("totally-bogus", True))
    claim_7e = False
except Exception:
    claim_7e = True

record("Default (static, fail_closed=True) → FailClosed wrapping Static", claim_7a and claim_7b)
record("static + fail_closed=False → bare Static", claim_7c)
record("null provider → NullActionClassifier", claim_7d)
record("Unknown provider → ConfigurationError raised", claim_7e)


# ---------------------------------------------------------------------------
# CLAIM 8: All three shipped tools declare their expected tier on the
# class body (so they survive a future tier-audit grep).
# ---------------------------------------------------------------------------

header("CLAIM 8 — Shipped tools declare expected tiers")

claim_8a = SaveMemoryTool.declared_tier == ActionTier.ROUTINE
claim_8b = SessionSummaryTool.declared_tier == ActionTier.ROUTINE
claim_8c = EscalateTool.declared_tier == ActionTier.NOTIFY_AND_PROCEED
claim_8d = LucielTool.declared_tier is None

record("SaveMemoryTool.declared_tier == ROUTINE", claim_8a)
record("SessionSummaryTool.declared_tier == ROUTINE", claim_8b)
record("EscalateTool.declared_tier == NOTIFY_AND_PROCEED", claim_8c)
record("LucielTool (base) .declared_tier is None (fail-closed default)", claim_8d)


# ---------------------------------------------------------------------------
# CLAIM 9: parse_and_execute (the LLM-facing path) also passes through
# the gate. Construct a TOOL_CALL string and route through the path the
# LLM would actually take.
# ---------------------------------------------------------------------------

header("CLAIM 9 — parse_and_execute (LLM-facing path) honours the gate")

# 9a — ROUTINE path through parse_and_execute
result = prod_broker.parse_and_execute(
    'TOOL_CALL: {"tool": "save_memory", "parameters": '
    '{"content": "via parse_and_execute", "memory_type": "preference"}}',
    session_id="live-e2e-9",
    user_id="aryan",
)
print(f"  parse_and_execute(save_memory) tier = {result.metadata.get('tier') if result else None}")
claim_9a = result is not None and result.metadata.get("tier") == "routine"

# 9b — APPROVAL_REQUIRED path through parse_and_execute via an
# undeclared tool
registry = ToolRegistry()
registry.register(UndeclaredTierTool())
broker9 = ToolBroker(registry)
UndeclaredTierTool.EXECUTE_CALLED = False
result = broker9.parse_and_execute(
    'TOOL_CALL: {"tool": "undeclared_tier_probe", "parameters": {"k": "v"}}',
)
print(f"  parse_and_execute(undeclared) tier = {result.metadata.get('tier') if result else None}")
print(f"  parse_and_execute(undeclared) pending = {result.metadata.get('pending') if result else None}")
print(f"  execute called? {UndeclaredTierTool.EXECUTE_CALLED}")
claim_9b = (
    result is not None
    and result.metadata.get("tier") == "approval_required"
    and result.metadata.get("pending") is True
    and UndeclaredTierTool.EXECUTE_CALLED is False
)

record("parse_and_execute routes ROUTINE through gate", claim_9a)
record("parse_and_execute short-circuits APPROVAL_REQUIRED without executing", claim_9b)


# ---------------------------------------------------------------------------
# SUMMARY
# ---------------------------------------------------------------------------

header("SUMMARY")

total = len(results)
passed = sum(1 for r in results if r.passed)
failed = total - passed

print(f"  Total claims:     {total}")
print(f"  Passed:           {passed}")
print(f"  Failed:           {failed}")
print()

if failed:
    print("  FAILED CLAIMS:")
    for r in results:
        if not r.passed:
            print(f"    - {r.name}")
    print()
    print("  Step 30c is NOT fully complete. Do NOT merge.")
    sys.exit(1)
else:
    print("  All Step 30c recap success criteria are satisfied against")
    print("  the live shipped code. Safe to merge PR #22.")
    sys.exit(0)
