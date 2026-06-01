"""
LucielTool base — Architecture §3.3.1 contract.

Arc 12 WU1 migrated this base off the v1 `name`/`description`/
`parameter_schema` / sync `execute(**kwargs)->ToolResult` shape to the
§3.3.1 surface every shipped tool must satisfy. The new contract is:

  * ``tool_id``        : stable string identifier (replaces ``name``)
  * ``display_name``   : admin-facing label
  * ``description``    : one-sentence text shown to the LLM at tool
                         selection time
  * ``input_schema``   : JSON Schema validated BEFORE ``execute()``
  * ``output_schema``  : JSON Schema validated AFTER ``execute()``
  * ``requires_tier``  : tuple subset of ('free','pro','enterprise')
  * ``requires_channels`` : frozenset of channel ids (e.g. {'sms'});
                         the broker denies dispatch if the channel
                         adapter is not enabled. Most tools use
                         ``frozenset()``.
  * ``execution_mode`` : ``"in_process"`` | ``"subprocess"``. BYO
                         webhooks run subprocess (Decision #5);
                         everything else in-process.
  * ``execute(input, context)`` : async, returns a dict matching
                         ``output_schema``.

``declared_tier`` is deliberately retained on the base class and is
ORTHOGONAL to the §3.3.1 contract. It feeds the action-classification
gate in ``app/policy/action_classification.py`` (ROUTINE /
NOTIFY_AND_PROCEED / APPROVAL_REQUIRED). The default of ``None`` means
"unclassifiable -> fail-closed to APPROVAL_REQUIRED" per the existing
Step 30c invariant. Removing this gate is out of scope for Arc 12;
authorisation (WU2), channel gating, and tier gating are the new
checks added on top.

``ToolContext`` carries the call-scope identity the broker (and the
WU2 authorisation lookup, and the WU5 sibling dispatch) need:
``admin_id``, ``instance_id``, an optional DB-session/scope handle,
and the inbound message id used for fan-out/cycle accounting. It is
constructed by the broker on each invocation; tools should treat it
as immutable.

ToolResult vs dict reconciliation (WU1 decision)
================================================

The §3.3.1 contract says ``execute`` returns a dict. The broker
historically wraps tool outputs into a ``ToolResult`` so the
action-classification gate can stamp tier metadata and so chat_service
can consume ``result.success`` / ``result.output`` / ``result.metadata``
uniformly. WU1 keeps both:

  * Tools return a schema-validated dict per the contract literal.
  * The broker validates input pre-call and output post-call against
    the schemas, then wraps the dict into a ``ToolResult`` for tier
    stamping + downstream plumbing. ``ToolResult.metadata['output']``
    carries the full dict; ``ToolResult.output`` carries a short
    human-readable string the LLM follow-up turn references.

This keeps the contract literal at the tool boundary AND preserves the
action-classification gate intact (broker still stamps tier on every
return path). The boundary is `app/tools/broker.py`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional, TYPE_CHECKING

from app.policy.action_classification import ActionTier
from app.tools.schema import validate_schema

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session


# =====================================================================
# ToolResult — broker plumbing (NOT the §3.3.1 return type)
# =====================================================================


@dataclass
class ToolResult:
    """
    Broker-side wrapper around a tool's §3.3.1 dict return.

    The §3.3.1 contract has ``execute`` return a validated dict. The
    broker wraps that dict into a ``ToolResult`` so the
    action-classification gate can stamp tier metadata, so chat_service
    can read ``success`` / ``output`` / ``metadata`` uniformly, and so
    audit-row construction has one shape to write against. Tools do
    NOT construct ``ToolResult`` themselves under the new contract --
    they return a dict and the broker wraps it.
    """

    success: bool
    output: str
    error: str = ""
    metadata: dict = field(default_factory=dict)


# =====================================================================
# ToolContext — call-scope identity passed to every execute()
# =====================================================================


@dataclass
class SiblingCompositionState:
    """Mutable per-inbound-message composition accounting.

    Threaded through ``ToolContext.composition_state`` so the WU5
    sibling-dispatch path can:

      * detect cycles (a callee already on ``call_stack`` is a cycle);
      * enforce the per-inbound fan-out budget (``fan_out_count`` is
        the total number of sibling-call invocations across the
        whole composition tree for this inbound message — when it
        reaches ``app.tools.sibling_dispatch.SIBLING_FAN_OUT_BUDGET``
        further calls are refused as a tool-error).

    Mutable on purpose. ``ToolContext`` itself is frozen so a tool
    body cannot rewrite admin/instance identity, but the composition
    accounting MUST mutate as the tree expands and contracts. The
    convention is: the dispatch path is the SOLE writer (push on
    entry, pop on exit, increment fan-out exactly once per allowed
    dispatch). Tool bodies treat it as read-only.

    Runtime-internal. Not admin-configurable, not surfaced in any
    API, not in entitlements, not in any UI. See
    ``app/tools/sibling_dispatch.py`` for the budget default and the
    rationale.
    """

    call_stack: list[tuple[int, int]] = field(default_factory=list)
    fan_out_count: int = 0


@dataclass(frozen=True)
class ToolContext:
    """Identity + scope handle threaded through every tool invocation.

    Frozen so a tool cannot mutate the broker's view of the call. The
    fields named here are the minimum the broker, the WU2
    authorisation lookup, the WU5 sibling dispatch path, and the WU6
    BYO sandbox all need. Future fields (e.g. caller_instance_id for
    sibling composition) should be additive.

    Attributes
    ----------
    admin_id : str
        Wall-1 tenant boundary. Every tool invocation belongs to
        exactly one admin; the broker pins it from the inbound
        request. Used by the WU2 authorisation table lookup.
    instance_id : int
        Wall-3 instance boundary. The instance the tool is being
        invoked on behalf of. Used by the WU2 authorisation lookup
        and by audit-row construction.
    session : Optional[Session]
        DB session/scope handle. Optional so unit tests can construct
        a context without spinning up a DB. Tools that need DB access
        (e.g. lookup_property) read it from here.
    inbound_message_id : Optional[str]
        Identifier for the current inbound message. WU5 uses this to
        scope cycle-detection state and the per-inbound fan-out
        budget across a composition tree.
    caller_instance_id : Optional[int]
        For a SIBLING invocation, the instance that initiated this
        hop. ``None`` on the customer-facing entry point (the root
        of the composition tree). The WU5 dispatch path derives a
        new ``ToolContext`` for the callee whose ``instance_id``
        is the callee and whose ``caller_instance_id`` is the
        current ``instance_id`` — this is the Wall-3 composition
        exception (§3.7.3) that names BOTH instances.
    composition_state : Optional[SiblingCompositionState]
        Shared mutable accounting for cycle detection + per-inbound
        fan-out budget. ``None`` on the customer-facing entry point;
        WU5 dispatch lazily allocates one on first sibling call and
        propagates the SAME instance to every derived child context
        so the call stack and fan-out counter are global across the
        composition tree for this inbound message. Backward-
        compatible: WU1/WU2/WU3 contexts that never enter the
        sibling dispatch path leave this ``None`` and never touch it.
    admin_tier : Optional[str]
        The Admin's subscription tier for the dispatch-time tier
        re-check (Architecture §3.3.3 — the "Arc 14 hardening OPTION").
        When present, ``ToolAuthorizer._check_tier`` compares it against
        ``tool.requires_tier`` and refuses on mismatch; when ``None``
        the check is SKIPPED (the WU2 baseline), so call sites that do
        not know the tier are unaffected. Arc 14 U5 threads this from
        the orchestrator's ACT step, which already resolves the tier for
        the OUTCOME grounding floor.
    enabled_channels : Optional[frozenset[str]]
        The per-instance enabled-channel set for the dispatch-time
        channel re-check (§3.3.3). When present,
        ``ToolAuthorizer._check_channels`` refuses a tool whose
        ``requires_channels`` are not all enabled; when ``None`` the
        check is SKIPPED (WU2 baseline). Arc 14 U5 threads this from the
        ACT step, which already resolves the set for the channel arbiter.
    """

    admin_id: str
    instance_id: int
    session: Optional["Session"] = None
    inbound_message_id: Optional[str] = None
    caller_instance_id: Optional[int] = None
    composition_state: Optional["SiblingCompositionState"] = None
    admin_tier: Optional[str] = None
    enabled_channels: Optional[frozenset[str]] = None


# =====================================================================
# LucielTool — the §3.3.1 contract
# =====================================================================


class LucielTool(ABC):
    """Abstract base class — every Luciel tool must satisfy §3.3.1.

    Subclasses must override:
      * ``tool_id``        (property)
      * ``display_name``   (property)
      * ``description``    (property)
      * ``input_schema``   (property)
      * ``output_schema``  (property)
      * ``requires_tier``  (property; tuple subset of the three tier
                            ids)
      * ``execution_mode`` (property; "in_process" | "subprocess")
      * async ``execute(input, context)`` returning a dict matching
        ``output_schema``.

    Subclasses MAY override:
      * ``requires_channels`` -- defaults to ``frozenset()`` (no
        channel adapter dependency).
      * ``declared_tier`` -- the action-classification tier (Step
        30c). Defaults to ``None`` (fail-closed to APPROVAL_REQUIRED
        per the action-classification gate).
    """

    # Step 30c -- the action-classification gate reads this. Defaulting
    # to None means a subclass that forgets to declare a tier is routed
    # to APPROVAL_REQUIRED by the FailClosedActionClassifier wrapper,
    # which is the safe-by-default behaviour Recap §4 requires. This is
    # ORTHOGONAL to the §3.3.1 contract (tier-gating runs after channel
    # + authorisation gating).
    declared_tier: ActionTier | None = None

    # Default for the per-channel adapter requirement. Most tools do
    # not depend on a channel adapter; ``send_email`` and ``send_sms``
    # override this in WU3.
    requires_channels: frozenset[str] = frozenset()

    # ------------------------------------------------------------------
    # §3.3.1 surface — required on every subclass
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def tool_id(self) -> str:
        """Stable string identifier. Used by the broker, the registry,
        the WU2 authorisation table, and audit rows."""

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Admin-facing label."""

    @property
    @abstractmethod
    def description(self) -> str:
        """One-sentence description shown to the LLM at tool selection."""

    @property
    @abstractmethod
    def input_schema(self) -> dict[str, Any]:
        """JSON Schema validated BEFORE execute()."""

    @property
    @abstractmethod
    def output_schema(self) -> dict[str, Any]:
        """JSON Schema validated AFTER execute()."""

    @property
    @abstractmethod
    def requires_tier(self) -> tuple[str, ...]:
        """Tuple of tier ids this tool is available on. Subset of
        ('free','pro','enterprise'). The broker denies dispatch if
        the admin's tier is not in this tuple."""

    @property
    @abstractmethod
    def execution_mode(self) -> str:
        """``"in_process"`` for normal tools; ``"subprocess"`` for
        BYO webhooks (Decision #5)."""

    @abstractmethod
    async def execute(
        self,
        input: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        """Run the tool.

        Args
        ----
        input : dict
            JSON-Schema-validated payload. The broker validates
            against ``input_schema`` BEFORE calling this method, so a
            tool body can trust the shape.
        context : ToolContext
            Call-scope identity (admin_id, instance_id, optional DB
            session, inbound_message_id).

        Returns
        -------
        dict
            Payload matching ``output_schema``. The broker validates
            the return value AFTER this method returns; a tool that
            returns a malformed dict is treated as a tool failure (no
            retry; see WU6 BYO retry policy).
        """


# =====================================================================
# Re-export schema helpers
# =====================================================================

__all__ = [
    "LucielTool",
    "SiblingCompositionState",
    "ToolContext",
    "ToolResult",
    "validate_schema",
]
