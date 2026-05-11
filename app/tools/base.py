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

from app.policy.action_classification import ActionTier


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
    - declared_tier: ActionTier the tool sits at (Step 30c) — read by
      the action-classification gate in app/policy/action_classification.py
      before the broker dispatches execute(). A tool that does NOT
      override declared_tier is treated as unknown by the
      StaticTierRegistryClassifier, which the fail-closed wrapper
      then routes to APPROVAL_REQUIRED. The default is therefore
      deliberately set to None on the base class — opting into
      execution requires an explicit tier declaration on the
      subclass, not an inherited default.
    - execute(): the actual logic
    """

    # Step 30c: every concrete tool must override this. See
    # ARCHITECTURE §3.3 step 8 for what each tier means. Leaving
    # the base class default at None means a tool author who forgets
    # to declare a tier ships a tool that fails closed to
    # APPROVAL_REQUIRED, which is the safe-by-default behaviour the
    # Recap §4 behavior contract requires.
    declared_tier: ActionTier | None = None

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