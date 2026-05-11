"""
Tool broker.

Executes tool calls requested by the LLM.
The broker validates that the tool exists, checks parameters,
runs the tool, and returns the result.

The broker does NOT decide which tool to use — the LLM does that.
The broker only executes what is requested and returns the outcome.

Step 30c addition — action-classification gate
----------------------------------------------

Before invoking `tool.execute(...)`, the broker calls a pluggable
ActionClassifier (see app/policy/action_classification.py) to tier
the invocation as ROUTINE, NOTIFY_AND_PROCEED, or APPROVAL_REQUIRED.

  * ROUTINE              -> execute immediately, return ToolResult
                            with tier metadata.
  * NOTIFY_AND_PROCEED   -> execute immediately, return ToolResult
                            with tier metadata so the runtime layer
                            can surface what was done to the
                            customer.
  * APPROVAL_REQUIRED    -> do NOT execute. Return a structured
                            ToolResult(success=False, ...) whose
                            metadata['tier']='approval_required'
                            and metadata['pending']=True so the
                            runtime layer can render a confirmation
                            prompt. The action runs only after a
                            subsequent confirmation turn (the
                            confirmation-loop UX is the Runtime
                            layer's responsibility and lands with
                            Step 31).

The classifier is injected via the constructor; if the caller does
not pass one (the historical signature is preserved), the broker
constructs the production gate via
ActionClassificationGate.from_settings(settings) on first need. The
classifier wiring is therefore present-by-default and a maintainer
cannot remove it just by instantiating ToolBroker(registry) without
also wiring an opt-out path.

Later, the broker can add:
- Permission checks (can this user/tenant use this tool?)
- Rate limiting
- Timeout enforcement
- Retry logic
- Audit logging (Step 30c records tier on every ToolResult.metadata
  but the structured audit-row write is the broader audit work
  tracked in DRIFTS.md `D-widget-chat-no-application-level-audit-log`).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.policy.action_classification import (
    ActionClassifier,
    ActionClassificationGate,
    ActionTier,
)
from app.tools.base import ToolResult
from app.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class ToolBroker:
    """
    Receives tool call requests, validates them, classifies them, and
    executes them (or returns a pending-approval frame).

    The historical public signature `ToolBroker(registry)` is
    preserved; callers that do not inject an ActionClassifier get the
    production fail-closed gate built from `app.core.config.settings`
    on first construction. This is Pattern E (deactivate by
    replacement, never delete) at the API surface: existing callers
    keep working unchanged and inherit the new behaviour.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        classifier: ActionClassifier | None = None,
    ) -> None:
        self.registry = registry
        if classifier is None:
            # Lazy-import settings so this module stays importable
            # in unit tests that monkey-patch the settings before
            # the classifier is constructed.
            from app.core.config import settings

            classifier = ActionClassificationGate.from_settings(settings)
        self._classifier: ActionClassifier = classifier

    def execute_tool(
        self,
        tool_name: str,
        parameters: dict[str, Any] | None = None,
        **context: Any,
    ) -> ToolResult:
        """
        Execute a tool by name with given parameters.

        Args:
            tool_name:  The name of the tool to execute.
            parameters: The parameters to pass to the tool.
            **context:  Extra context (like session messages) passed
                        to tools that need it.

        Returns:
            ToolResult with success/failure and output. metadata
            carries the classifier's tier on every call (including
            the not-found path, which is recorded as
            APPROVAL_REQUIRED so a stray tool name cannot route
            silently into execution by virtue of being unknown
            twice over).
        """
        tool = self.registry.get(tool_name)

        if tool is None:
            logger.warning("Tool not found: %s", tool_name)
            return ToolResult(
                success=False,
                output="",
                error=f"Tool '{tool_name}' not found.",
                metadata={
                    "tier": ActionTier.APPROVAL_REQUIRED.value,
                    "tier_reason": "unknown_tool",
                    "classifier": getattr(self._classifier, "name", ""),
                },
            )

        # Step 30c: classify BEFORE executing. The classifier is
        # fail-closed by default — an unclassifiable tool will be
        # tiered APPROVAL_REQUIRED and we will return a pending
        # frame without calling tool.execute(). The order here is
        # load-bearing and is pinned by an AST contract test in
        # tests/api/test_action_classification.py.
        classification = self._classifier.classify(tool)
        tier_metadata = {
            "tier": classification.tier.value,
            "tier_reason": classification.reason,
            "classifier": classification.classifier,
        }

        if classification.tier == ActionTier.APPROVAL_REQUIRED:
            logger.info(
                "Tool gated APPROVAL_REQUIRED -- not executing. "
                "tool=%s reason=%s classifier=%s",
                tool_name,
                classification.reason,
                classification.classifier,
            )
            # We expose `pending=True` and `proposed_parameters` so
            # the runtime layer can render a confirmation prompt
            # without re-classifying. The tool name is repeated in
            # metadata so a downstream audit row can be built from
            # the ToolResult alone.
            return ToolResult(
                success=False,
                output="",
                error=(
                    "This action requires explicit approval before it "
                    "can run."
                ),
                metadata={
                    **tier_metadata,
                    "pending": True,
                    "tool_name": tool_name,
                    "proposed_parameters": dict(parameters or {}),
                },
            )

        params = parameters or {}
        # Merge any extra context into params for tools that need it.
        params.update(context)

        try:
            result = tool.execute(**params)
            # Stamp tier on the result. We do NOT overwrite any
            # metadata the tool already set — the tool's metadata
            # wins on key collision because the tool knows more
            # about what it just did than the broker does. The
            # broker only adds tier/classifier/tier_reason keys.
            merged_metadata = {**tier_metadata, **result.metadata}
            stamped = ToolResult(
                success=result.success,
                output=result.output,
                error=result.error,
                metadata=merged_metadata,
            )
            logger.info(
                "Tool executed: %s | success=%s | tier=%s",
                tool_name, result.success, classification.tier.value,
            )
            return stamped

        except Exception as exc:
            logger.error("Tool execution failed: %s | %s", tool_name, exc)
            return ToolResult(
                success=False,
                output="",
                error=f"Tool execution failed: {exc}",
                metadata=tier_metadata,
            )

    def parse_and_execute(
        self,
        llm_tool_call: str,
        **context: Any,
    ) -> ToolResult | None:
        """
        Parse a tool call from LLM output and execute it.

        The LLM is instructed to output tool calls in this format:
        TOOL_CALL: {"tool": "tool_name", "parameters": {...}}

        Returns None if the text does not contain a tool call.
        """
        if "TOOL_CALL:" not in llm_tool_call:
            return None

        try:
            # Extract the JSON after TOOL_CALL:
            json_str = llm_tool_call.split("TOOL_CALL:", 1)[1].strip()
            call_data = json.loads(json_str)

            tool_name = call_data.get("tool", "")
            parameters = call_data.get("parameters", {})

            return self.execute_tool(tool_name, parameters, **context)

        except (json.JSONDecodeError, Exception) as exc:
            logger.warning("Failed to parse tool call: %s", exc)
            return None
