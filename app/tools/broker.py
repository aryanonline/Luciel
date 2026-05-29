"""
Tool broker.

Executes tool calls requested by the LLM. The broker validates that
the tool exists, validates the input against the tool's JSON Schema,
classifies the invocation (Step 30c action-classification gate),
runs the tool (async), validates the output against the tool's
output schema, and wraps the dict return into a ``ToolResult`` for
downstream plumbing.

§3.3.1 contract literal
-----------------------
The §3.3.1 contract says ``execute`` returns a dict. WU1 retains
``ToolResult`` as broker plumbing only -- tools return a dict, and
the broker wraps it. This keeps the contract literal at the tool
boundary AND preserves the action-classification gate intact (the
gate stamps tier on every return path) AND preserves chat_service's
``result.success`` / ``result.output`` / ``result.metadata`` shape.
The dict payload lives at ``ToolResult.metadata['output']``;
``ToolResult.output`` carries a short human-readable string for the
LLM follow-up turn.

Step 30c action-classification gate (preserved)
-----------------------------------------------
Before invoking ``tool.execute(...)`` the broker calls a pluggable
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
                            prompt.

The classifier is injected via the constructor; if the caller does
not pass one, the broker constructs the production gate via
``ActionClassificationGate.from_settings(settings)`` on first need.

Async + sync entry points
-------------------------
The §3.3.1 contract makes ``execute`` async. The chat_service entry
points are still sync (``parse_and_execute``, ``execute_tool``), so
the broker drives the coroutine with ``asyncio.run`` /
``asyncio.get_event_loop().run_until_complete`` depending on whether
an event loop is already running. WU5 sibling dispatch and WU6 BYO
sandbox are async-native; they will call ``execute_tool_async``
directly.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.policy.action_classification import (
    ActionClassifier,
    ActionClassificationGate,
    ActionTier,
)
from app.tools.authorization import (
    AuthorizationDecision,
    DefaultDenyToolAuthorizer,
    ToolAuthorizer,
)
from app.tools.base import LucielTool, ToolContext, ToolResult
from app.tools.registry import ToolRegistry
from app.tools.schema import SchemaValidationError, validate_schema

logger = logging.getLogger(__name__)


def _default_context() -> ToolContext:
    """Synthetic ToolContext for callers that have not yet been
    threaded through WU2's per-instance authorisation work.

    Arc 12 WU2 will require every call site to supply an explicit
    ``ToolContext`` (admin_id + instance_id) so the authorisation
    table lookup is meaningful. WU1 keeps the legacy ``execute_tool``
    sync entrypoint working by synthesising a placeholder context;
    WU2 will remove this fallback.
    """
    return ToolContext(admin_id="", instance_id=0)


class ToolBroker:
    """Receives tool call requests, validates them, classifies them,
    executes them (or returns a pending-approval frame), and wraps
    the §3.3.1 dict return into a ``ToolResult``.

    The historical public signature ``ToolBroker(registry)`` is
    preserved; callers that do not inject an ActionClassifier get the
    production fail-closed gate built from ``app.core.config.settings``
    on first construction.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        classifier: ActionClassifier | None = None,
        *,
        authorizer: ToolAuthorizer | None = None,
    ) -> None:
        """Construct the broker.

        ``authorizer`` is the Arc 12 WU2 default-deny gate (see
        ``app.tools.authorization``). If None, the broker constructs
        a ``DefaultDenyToolAuthorizer`` that reads
        ``instance_tool_authorizations`` via ``context.session``. The
        signature is kept stable for Arc 14 — the agentic loop will
        either inject an enriched authoriser (cycle detection +
        fan-out budget) or wrap the default one.
        """
        self.registry = registry
        if classifier is None:
            # Lazy-import settings so this module stays importable
            # in unit tests that monkey-patch the settings before
            # the classifier is constructed.
            from app.core.config import settings

            classifier = ActionClassificationGate.from_settings(settings)
        self._classifier: ActionClassifier = classifier
        # Arc 12 WU2 — default-deny authorisation gate. Constructed
        # eagerly so the broker fails closed even if a caller forgets
        # to inject one.
        self._authorizer: ToolAuthorizer = (
            authorizer or DefaultDenyToolAuthorizer()
        )

    # ------------------------------------------------------------------
    # Public sync entrypoint (chat_service + legacy callers)
    # ------------------------------------------------------------------

    def execute_tool(
        self,
        tool_name: str,
        parameters: dict[str, Any] | None = None,
        *,
        context: ToolContext | None = None,
        **extra: Any,
    ) -> ToolResult:
        """Execute a tool by name with given parameters (sync wrapper).

        Args:
            tool_name:   The ``tool_id`` of the tool to execute.
            parameters:  The input payload validated against the
                         tool's ``input_schema``.
            context:     Optional explicit ``ToolContext``. If None,
                         a placeholder is synthesised (WU2 will make
                         this mandatory).
            **extra:     Legacy keyword pass-through. Merged into
                         parameters for backwards compatibility with
                         pre-WU1 callers that passed ad-hoc kwargs
                         (e.g. ``messages=[...]`` from chat_service's
                         summary-tool path). New code should populate
                         ``parameters`` directly.

        Returns:
            ``ToolResult`` -- the dict payload from the tool lives at
            ``metadata['output']``. ``output`` is a short human
            string for the LLM follow-up turn.
        """
        ctx = context or _default_context()
        merged_input = dict(parameters or {})
        if extra:
            merged_input.update(extra)
        return self._run_sync(tool_name, merged_input, ctx)

    async def execute_tool_async(
        self,
        tool_name: str,
        parameters: dict[str, Any] | None = None,
        *,
        context: ToolContext | None = None,
    ) -> ToolResult:
        """Async entrypoint used by WU5/WU6 paths.

        Same semantics as ``execute_tool``; the only difference is the
        coroutine is awaited by the caller rather than driven by the
        broker.
        """
        ctx = context or _default_context()
        return await self._run(tool_name, dict(parameters or {}), ctx)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _run_sync(
        self,
        tool_name: str,
        input_payload: dict[str, Any],
        ctx: ToolContext,
    ) -> ToolResult:
        """Drive the async ``_run`` from sync code.

        Tries to detect a running event loop (e.g. inside a FastAPI
        request handler that calls a sync adapter on the broker);
        falls back to ``asyncio.run`` when no loop is present.
        """
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None:
            return asyncio.run_coroutine_threadsafe(
                self._run(tool_name, input_payload, ctx), running
            ).result()
        return asyncio.run(self._run(tool_name, input_payload, ctx))

    async def _run(
        self,
        tool_name: str,
        input_payload: dict[str, Any],
        ctx: ToolContext,
    ) -> ToolResult:
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

        # Arc 12 WU2 — default-deny authorisation gate. Runs BEFORE
        # the action-classification gate and BEFORE tool.execute().
        # An absent / revoked / disabled authorisation row refuses
        # the call with a structured tool-error; the classifier is
        # never consulted, and tool.execute() is never invoked. This
        # is the load-bearing security gate Arc 14's agentic loop
        # will compose its cycle / fan-out checks on top of —
        # ``ToolAuthorizer.authorize(tool, context)`` is the stable
        # interface.
        auth_decision = self._authorizer.authorize(tool, ctx)
        if not auth_decision.allowed:
            logger.info(
                "Tool dispatch refused by authoriser. tool=%s "
                "admin=%s instance=%s reason=%s",
                tool_name, ctx.admin_id, ctx.instance_id,
                auth_decision.reason,
            )
            return ToolResult(
                success=False,
                output="",
                error=auth_decision.message,
                metadata={
                    "tier": ActionTier.APPROVAL_REQUIRED.value,
                    "tier_reason": auth_decision.reason,
                    "classifier": getattr(self._classifier, "name", ""),
                    "authorization": "denied",
                    "authorization_reason": auth_decision.reason,
                    "authorization_failure_kind": (
                        auth_decision.failure_kind
                    ),
                    "tool_name": tool_name,
                },
            )

        # Step 30c: classify BEFORE executing. The classifier is
        # fail-closed by default -- an unclassifiable tool will be
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
                    "proposed_parameters": dict(input_payload),
                },
            )

        # §3.3.1 input validation -- happens BEFORE execute(). A bad
        # input is a tool failure, not a tier-bump.
        try:
            validate_schema(input_payload, tool.input_schema)
        except SchemaValidationError as exc:
            logger.warning(
                "Tool input schema validation failed: %s | %s",
                tool_name, exc,
            )
            return ToolResult(
                success=False,
                output="",
                error=f"Tool input invalid: {exc}",
                metadata={
                    **tier_metadata,
                    "schema_error": "input",
                    "schema_path": exc.path,
                },
            )

        try:
            output_payload = await tool.execute(input_payload, ctx)
        except Exception as exc:
            logger.error("Tool execution failed: %s | %s", tool_name, exc)
            return ToolResult(
                success=False,
                output="",
                error=f"Tool execution failed: {exc}",
                metadata=tier_metadata,
            )

        if not isinstance(output_payload, dict):
            logger.error(
                "Tool %s returned non-dict %r (contract violation)",
                tool_name, type(output_payload).__name__,
            )
            return ToolResult(
                success=False,
                output="",
                error=(
                    f"Tool '{tool_name}' returned "
                    f"{type(output_payload).__name__}, expected dict"
                ),
                metadata=tier_metadata,
            )

        try:
            validate_schema(output_payload, tool.output_schema)
        except SchemaValidationError as exc:
            logger.warning(
                "Tool output schema validation failed: %s | %s",
                tool_name, exc,
            )
            return ToolResult(
                success=False,
                output="",
                error=f"Tool output invalid: {exc}",
                metadata={
                    **tier_metadata,
                    "schema_error": "output",
                    "schema_path": exc.path,
                    "output": output_payload,
                },
            )

        # Map the validated dict into a ToolResult for downstream
        # consumers. ``success`` defaults to True unless the tool
        # explicitly declared otherwise; ``output`` is a short string
        # the LLM follow-up turn references; the full dict lives at
        # ``metadata['output']`` so audit-row construction has the
        # complete payload available.
        success = bool(output_payload.get("success", True))
        output_str = str(
            output_payload.get("output")
            or output_payload.get("message")
            or ""
        )
        merged_metadata = {**tier_metadata, **output_payload}
        result = ToolResult(
            success=success,
            output=output_str,
            error=str(output_payload.get("error", "")),
            metadata=merged_metadata,
        )
        logger.info(
            "Tool executed: %s | success=%s | tier=%s",
            tool_name, success, classification.tier.value,
        )
        return result

    # ------------------------------------------------------------------
    # LLM-output dispatch
    # ------------------------------------------------------------------

    def parse_and_execute(
        self,
        llm_tool_call: str,
        *,
        context: ToolContext | None = None,
        **extra: Any,
    ) -> ToolResult | None:
        """Parse a tool call from LLM output and execute it.

        The LLM is instructed to output tool calls in the format::

            TOOL_CALL: {"tool": "tool_name", "parameters": {...}}

        Returns None if the text does not contain a tool call.
        """
        if "TOOL_CALL:" not in llm_tool_call:
            return None

        try:
            json_str = llm_tool_call.split("TOOL_CALL:", 1)[1].strip()
            call_data = json.loads(json_str)

            tool_name = call_data.get("tool", "")
            parameters = call_data.get("parameters", {})

            return self.execute_tool(
                tool_name, parameters, context=context, **extra,
            )

        except (json.JSONDecodeError, Exception) as exc:
            logger.warning("Failed to parse tool call: %s", exc)
            return None
