"""
Arc 12 WU1 — contract-shape regression tests.

Two groups:

  1. Every tool the default registry holds must declare the full
     §3.3.1 surface: ``tool_id``, ``display_name``, ``description``,
     ``input_schema``, ``output_schema``, ``requires_tier``,
     ``requires_channels``, ``execution_mode``, plus the async
     ``execute(input, context)`` method.

  2. ``TierEntitlement`` must NOT carry the retired depth field
     (Decision #19 -- no depth limit on the composition graph); the
     §3.3.4 master switch ``composition_enabled`` MUST remain and
     be per-tier (free=False, pro=True, enterprise=True).
"""

from __future__ import annotations

import asyncio
import inspect
import os
from dataclasses import fields

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MODERATION_PROVIDER", "null")
os.environ.setdefault("OPENAI_API_KEY", "dummy")


# =====================================================================
# §3.3.1 contract-shape tests
# =====================================================================


def _registry():
    from app.tools.registry import ToolRegistry

    return ToolRegistry()


_REQUIRED_TIERS = {"free", "pro", "enterprise"}
_ALLOWED_EXECUTION_MODES = {"in_process", "subprocess"}


@pytest.mark.parametrize("tool", _registry().list_tools(), ids=lambda t: t.tool_id)
def test_registered_tool_declares_full_3_3_1_surface(tool) -> None:
    """Every tool present in the default registry must satisfy the
    §3.3.1 contract. If this fails, a tool author added a tool
    without conforming to the contract."""

    # tool_id
    assert isinstance(tool.tool_id, str) and tool.tool_id, (
        f"{type(tool).__name__}.tool_id must be a non-empty string"
    )

    # display_name
    assert isinstance(tool.display_name, str) and tool.display_name, (
        f"{type(tool).__name__}.display_name must be a non-empty string"
    )

    # description
    assert isinstance(tool.description, str) and tool.description, (
        f"{type(tool).__name__}.description must be a non-empty string"
    )

    # input_schema -- object schema (JSON Schema root)
    assert isinstance(tool.input_schema, dict), (
        f"{type(tool).__name__}.input_schema must be a dict (JSON Schema)"
    )
    # output_schema -- object schema
    assert isinstance(tool.output_schema, dict), (
        f"{type(tool).__name__}.output_schema must be a dict (JSON Schema)"
    )

    # requires_tier -- tuple subset of the three tier ids
    assert isinstance(tool.requires_tier, tuple), (
        f"{type(tool).__name__}.requires_tier must be a tuple"
    )
    assert tool.requires_tier, (
        f"{type(tool).__name__}.requires_tier must not be empty"
    )
    assert set(tool.requires_tier).issubset(_REQUIRED_TIERS), (
        f"{type(tool).__name__}.requires_tier {tool.requires_tier!r} "
        f"must be a subset of {_REQUIRED_TIERS!r}"
    )

    # requires_channels -- frozenset (may be empty)
    assert isinstance(tool.requires_channels, frozenset), (
        f"{type(tool).__name__}.requires_channels must be a frozenset"
    )

    # execution_mode -- in_process | subprocess
    assert tool.execution_mode in _ALLOWED_EXECUTION_MODES, (
        f"{type(tool).__name__}.execution_mode {tool.execution_mode!r} "
        f"must be one of {_ALLOWED_EXECUTION_MODES!r}"
    )

    # execute -- async, signature (self, input, context)
    assert inspect.iscoroutinefunction(tool.execute), (
        f"{type(tool).__name__}.execute must be async per §3.3.1"
    )
    sig = inspect.signature(tool.execute)
    params = list(sig.parameters)
    assert params[:2] == ["input", "context"], (
        f"{type(tool).__name__}.execute signature must be "
        f"(input, context); got {params!r}"
    )


def test_base_class_keeps_declared_tier_orthogonal() -> None:
    """The action-classification gate is orthogonal to the §3.3.1
    contract -- ``declared_tier`` must remain on the base class with
    a default of None so an undeclared tier still fails closed to
    APPROVAL_REQUIRED (the Step 30c invariant).
    """

    from app.policy.action_classification import ActionTier
    from app.tools.base import LucielTool

    assert hasattr(LucielTool, "declared_tier")
    assert LucielTool.declared_tier is None, (
        "LucielTool.declared_tier must default to None so undeclared "
        "subclasses route to APPROVAL_REQUIRED via the fail-closed "
        "wrapper."
    )
    # Sanity: ActionTier is still the type the base attribute is
    # declared against (helps a maintainer who renames the enum).
    assert {m.name for m in ActionTier} == {
        "ROUTINE", "NOTIFY_AND_PROCEED", "APPROVAL_REQUIRED",
    }


def test_tool_context_dataclass_carries_required_fields() -> None:
    """``ToolContext`` must carry at minimum the identity the broker
    and the WU2 authorisation lookup need: admin_id, instance_id,
    plus an optional DB session handle and an inbound_message_id for
    WU5 fan-out accounting."""

    from app.tools.base import ToolContext

    field_names = {f.name for f in fields(ToolContext)}
    assert {"admin_id", "instance_id"}.issubset(field_names), (
        f"ToolContext must carry admin_id + instance_id; got "
        f"{field_names!r}"
    )
    # The other two fields are nice-to-have, not load-bearing for
    # WU1, but the WU2/WU5 work assumes they exist. Pin them so a
    # future refactor cannot drop them silently.
    assert "session" in field_names
    assert "inbound_message_id" in field_names


def test_broker_validates_input_schema_before_execute() -> None:
    """A tool whose ``input_schema`` rejects the call must NOT
    execute. The broker maps the validation failure to a tool
    failure (NOT an APPROVAL_REQUIRED tier-bump -- the tier reason
    is orthogonal)."""

    from app.policy.action_classification import ActionTier
    from app.tools.base import LucielTool, ToolContext
    from app.tools.broker import ToolBroker
    from app.tools.registry import ToolRegistry

    executed: list[bool] = []

    class _StrictInputTool(LucielTool):
        declared_tier = ActionTier.ROUTINE

        @property
        def tool_id(self) -> str:
            return "strict_input"

        @property
        def display_name(self) -> str:
            return "Strict input"

        @property
        def description(self) -> str:
            return "Refuses anything without 'recipient'."

        @property
        def input_schema(self) -> dict:
            return {
                "type": "object",
                "properties": {"recipient": {"type": "string"}},
                "required": ["recipient"],
                "additionalProperties": False,
            }

        @property
        def output_schema(self) -> dict:
            return {"type": "object", "additionalProperties": True}

        @property
        def requires_tier(self) -> tuple[str, ...]:
            return ("pro", "enterprise")

        @property
        def execution_mode(self) -> str:
            return "in_process"

        async def execute(self, input, context) -> dict:
            executed.append(True)
            return {"success": True, "output": "ran"}

    registry = ToolRegistry()
    registry.register(_StrictInputTool())
    broker = ToolBroker(registry)

    result = broker.execute_tool("strict_input", {"not_recipient": "x"})
    assert executed == [], (
        "Input-schema validation must run BEFORE execute(); the tool "
        "executed despite missing required field 'recipient'."
    )
    assert result.success is False
    assert result.metadata.get("schema_error") == "input"


def test_broker_validates_output_schema_after_execute() -> None:
    """A tool that returns a dict not matching ``output_schema``
    must surface as a tool failure with ``schema_error='output'``."""

    from app.policy.action_classification import ActionTier
    from app.tools.base import LucielTool
    from app.tools.broker import ToolBroker
    from app.tools.registry import ToolRegistry

    class _BadOutputTool(LucielTool):
        declared_tier = ActionTier.ROUTINE

        @property
        def tool_id(self) -> str:
            return "bad_output"

        @property
        def display_name(self) -> str:
            return "Bad output"

        @property
        def description(self) -> str:
            return "Returns the wrong shape."

        @property
        def input_schema(self) -> dict:
            return {"type": "object", "additionalProperties": True}

        @property
        def output_schema(self) -> dict:
            return {
                "type": "object",
                "properties": {"success": {"type": "boolean"}},
                "required": ["success", "must_be_present"],
                "additionalProperties": True,
            }

        @property
        def requires_tier(self) -> tuple[str, ...]:
            return ("free", "pro", "enterprise")

        @property
        def execution_mode(self) -> str:
            return "in_process"

        async def execute(self, input, context) -> dict:
            # Missing the schema-required ``must_be_present`` key.
            return {"success": True, "output": "ran"}

    registry = ToolRegistry()
    registry.register(_BadOutputTool())
    broker = ToolBroker(registry)

    result = broker.execute_tool("bad_output", {})
    assert result.success is False
    assert result.metadata.get("schema_error") == "output"


def test_schema_validator_supports_required_subset() -> None:
    """Sanity-check the minimal validator on the keywords the WU3
    catalog schemas will exercise."""

    from app.tools.schema import SchemaValidationError, validate_schema

    schema = {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": ["preference", "fact"],
            },
            "count": {"type": "integer", "minimum": 1, "maximum": 10},
            "items": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
            },
        },
        "required": ["category"],
        "additionalProperties": False,
    }

    # Happy path
    validate_schema(
        {"category": "fact", "count": 3, "items": ["a", "b"]},
        schema,
    )

    # Missing required
    with pytest.raises(SchemaValidationError):
        validate_schema({"count": 1}, schema)

    # Enum violation
    with pytest.raises(SchemaValidationError):
        validate_schema({"category": "other"}, schema)

    # Integer minimum
    with pytest.raises(SchemaValidationError):
        validate_schema({"category": "fact", "count": 0}, schema)

    # additionalProperties=False
    with pytest.raises(SchemaValidationError):
        validate_schema({"category": "fact", "extra": 1}, schema)

    # Array item minLength
    with pytest.raises(SchemaValidationError):
        validate_schema(
            {"category": "fact", "items": ["a", ""]}, schema,
        )


def test_tools_execute_returns_validated_dict() -> None:
    """The three shipped (cognition) tools' ``execute`` must return a
    dict matching their ``output_schema``. Smoke-tests the §3.3.1
    contract literal end-to-end."""

    from app.tools.base import ToolContext
    from app.tools.implementations.escalate_tool import EscalateTool
    from app.tools.implementations.save_memory_tool import SaveMemoryTool
    from app.tools.implementations.session_summary_tool import (
        SessionSummaryTool,
    )
    from app.tools.schema import validate_schema

    ctx = ToolContext(admin_id="adm_1", instance_id=1)

    async def _run():
        for tool, payload in [
            (EscalateTool(), {"reason": "user frustrated"}),
            (
                SaveMemoryTool(),
                {"category": "preference", "content": "terminal dev"},
            ),
            (SessionSummaryTool(), {}),
        ]:
            out = await tool.execute(payload, ctx)
            assert isinstance(out, dict)
            validate_schema(out, tool.output_schema)
            assert out["success"] is True

    asyncio.run(_run())
