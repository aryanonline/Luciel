"""
Base tool interface.

Every tool Luciel can use must implement this contract.
That keeps tool execution uniform and makes adding new tools
as simple as creating a new file and registering it.

To create a new tool:
1. Create a new file in app/tools/implementations/
2. Subclass LucielTool
3. Define name, description, and parameter_schema
4. Implement execute()
5. Register it in app/tools/registry.py
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolResult:
    """
    Standardized result from any tool execution.

    success:  Whether the tool completed without error.
    output:   The data or message the tool produced.
    error:    Error message if the tool failed.
    metadata: Optional extra info for logging/tracing.
    """
    success: bool
    output: str
    error: str = ""
    metadata: dict = field(default_factory=dict)


class LucielTool(ABC):
    """
    Abstract base class for all Luciel tools.

    Every tool must define:
    - name: unique identifier used in tool selection
    - description: what the tool does (shown to the LLM for tool selection)
    - parameter_schema: dict describing expected input parameters
    - execute(): the actual logic
    """

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        ...

    @property
    @abstractmethod
    def parameter_schema(self) -> dict[str, Any]:
        ...

    @abstractmethod
    def execute(self, **kwargs: Any) -> ToolResult:
        ...