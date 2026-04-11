"""
Tool broker.

Executes tool calls requested by the LLM.
The broker validates that the tool exists, checks parameters,
runs the tool, and returns the result.

The broker does NOT decide which tool to use — the LLM does that.
The broker only executes what is requested and returns the outcome.

Later, the broker can add:
- Permission checks (can this user/tenant use this tool?)
- Rate limiting
- Timeout enforcement
- Retry logic
- Audit logging
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.tools.base import ToolResult
from app.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class ToolBroker:
    """
    Receives tool call requests, validates them, and executes them.
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

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
            ToolResult with success/failure and output.
        """
        tool = self.registry.get(tool_name)

        if tool is None:
            logger.warning("Tool not found: %s", tool_name)
            return ToolResult(
                success=False,
                output="",
                error=f"Tool '{tool_name}' not found.",
            )

        params = parameters or {}
        # Merge any extra context into params for tools that need it.
        params.update(context)

        try:
            result = tool.execute(**params)
            logger.info(
                "Tool executed: %s | success=%s",
                tool_name, result.success,
            )
            return result

        except Exception as exc:
            logger.error("Tool execution failed: %s | %s", tool_name, exc)
            return ToolResult(
                success=False,
                output="",
                error=f"Tool execution failed: {exc}",
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