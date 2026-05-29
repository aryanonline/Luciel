"""Sibling-Luciel composition runtime dispatch — Arc 12 WU5.

Implements the §3.3.4 "Runtime dispatch path" five-check sequence for
``call_sibling_luciel``:

  a. **Cycle detection** — track ``(caller_instance_id,
     callee_instance_id)`` pairs in the call stack PER INBOUND
     MESSAGE. A call that would revisit an instance already in the
     active call stack is rejected.
  b. **Per-inbound fan-out budget** — a max total number of
     sibling-call invocations across the ENTIRE composition tree per
     inbound message. Cost-control bound, parallel to the §3.4.1
     5-iteration loop bound. When exhausted, further sibling calls
     are refused.
  c. **Master switch** — ``call_sibling_luciel`` is enabled (live
     authorisation row) on BOTH the caller and the callee instance.
  d. **Grant lookup** — ``sibling_call_grants`` for ``(admin_id,
     caller_instance_id, callee_instance_id)`` with
     ``approval_state='live'``. No live row ⇒ tool-error.
  e. **Audit + derived context** — emit a sibling-access audit row
     (§3.7.3 Wall-3 composition exception) and construct a DERIVED
     ToolContext naming BOTH instances. Hand off to the Arc 14
     agentic-loop orchestrator (see :data:`_ARC14_SEAM`).

Decision #19 lock
-----------------
Guardrails are ONLY cycle detection and per-inbound fan-out budget.
There is NO depth limit and NO edge cap. The customer-facing
composition graph is unbounded except by those two runtime checks.
Do not add depth/edge caps anywhere — they would silently violate
the locked decision.

Runtime-internal
----------------
The cycle-detection state and the fan-out budget default are
RUNTIME-INTERNAL: they are not admin-configurable, do not appear in
entitlements (the dataclass nor ``TIER_ENTITLEMENTS``), are not
threaded through any API/route layer, and are not surfaced in any
UI. The single source of truth for the budget default lives in this
module as :data:`SIBLING_FAN_OUT_BUDGET`. Tests assert this isolation
explicitly so a future change cannot silently leak it.

Arc 14 seam
-----------
The orchestrator round-trip (the calling Luciel ASKS the callee
Luciel a question and gets an ANSWER) is the Arc 14 agentic loop
deliverable. WU5 performs the FIVE checks REALLY — the audit row
is REAL, the derived context is REAL, the guard logic is REAL —
and returns a structured ``authorized_and_dispatched`` payload at
the single seam marked :data:`_ARC14_SEAM`. When Arc 14 lands, the
seam swaps the structured payload for an
``orchestrator.run(derived_request)`` call without touching the
guardrail logic.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.tools.base import (
    LucielTool,
    SiblingCompositionState,
    ToolContext,
)

logger = logging.getLogger(__name__)


# =====================================================================
# Runtime-internal constants
# =====================================================================


#: Per-inbound fan-out budget — the max TOTAL number of sibling-call
#: invocations across the whole composition tree for one inbound
#: customer message. Cost-control bound, parallel to the §3.4.1
#: 5-iteration loop bound. Sized so the documented depth-2-or-3
#: with fan-out-2-or-3 example from §3.3.4 is unconstrained (a
#: ternary tree of depth 2 = 1+3+9 = 13 calls), with one slot of
#: headroom — but a cascade that runs away (a buggy composition
#: tree that recurses arbitrarily wide) is stopped before it
#: becomes a billing event. Runtime-internal. NOT admin-configurable.
#: NOT in entitlements. NOT in any API/UI surface.
SIBLING_FAN_OUT_BUDGET: int = 12


#: The single seam where Arc 14's orchestrator round-trip will plug
#: in. Grep target. See module docstring "Arc 14 seam" section and
#: :func:`dispatch_sibling_call` body.
_ARC14_SEAM: str = "TODO(ARC14): sibling-orchestrator round-trip"


# =====================================================================
# Dispatch error shape
# =====================================================================


#: Machine-readable reason codes. Surfaced to the calling Luciel via
#: the tool-error metadata so it can reason about the failure rather
#: than just receive a string.
REASON_CYCLE_DETECTED: str = "sibling_cycle_detected"
REASON_FAN_OUT_BUDGET_EXHAUSTED: str = "sibling_fan_out_budget_exhausted"
REASON_CALLER_MASTER_SWITCH_OFF: str = "sibling_caller_master_switch_off"
REASON_CALLEE_MASTER_SWITCH_OFF: str = "sibling_callee_master_switch_off"
REASON_NO_LIVE_GRANT: str = "sibling_no_live_grant"
REASON_NO_CALLER_CONTEXT: str = "sibling_no_caller_context"
REASON_NO_DB_SESSION: str = "sibling_no_db_session"
REASON_SELF_TARGET: str = "sibling_self_target"


def _error(
    *, reason: str, message: str, callee_instance_id: Optional[int] = None,
) -> dict[str, Any]:
    """Build a structured tool-error matching ``CallSiblingLucielTool``'s
    ``output_schema`` — the calling Luciel receives this as the dict
    payload and can branch on ``reason``."""
    return {
        "success": False,
        "output": message,
        "error_reason": reason,
        "callee_instance_id": callee_instance_id,
        "not_yet_available": False,
        "owning_arc": "ARC12_WU5",
    }


# =====================================================================
# Dispatch entry point
# =====================================================================


def dispatch_sibling_call(
    *,
    callee_instance_id: int,
    task: str,
    payload: Optional[dict[str, Any]],
    context: ToolContext,
) -> dict[str, Any]:
    """Run the five-check dispatch path for one sibling call.

    Entry signature stability
    -------------------------
    This is the function ``CallSiblingLucielTool.execute`` calls. The
    signature is keyword-only and intentionally minimal so Arc 14's
    agentic loop can compose around it (e.g. wrap with an iteration
    counter) without touching the guard logic. Add fields only at
    the END as kwargs with defaults.

    Returns
    -------
    dict
        A payload matching ``CallSiblingLucielTool.output_schema``.
        On any check failure the dict carries ``success=False`` and a
        machine-readable ``error_reason`` from this module's
        ``REASON_*`` constants. On all checks passing the dict
        carries ``success=True`` and the structured
        "authorized-and-dispatched" payload (Arc 14 will replace the
        interim seam with the actual orchestrator round-trip
        response).
    """
    caller_instance_id = context.caller_instance_id
    if caller_instance_id is None:
        # WU5 dispatch presupposes a Luciel calling another Luciel.
        # The customer-facing entry point seeds ``caller_instance_id``
        # to its own ``instance_id`` before this tool can fire.
        return _error(
            reason=REASON_NO_CALLER_CONTEXT,
            message=(
                "Sibling dispatch refused: no caller_instance_id on "
                "ToolContext. The customer-facing entry point must "
                "seed ToolContext.caller_instance_id (typically by "
                "setting it equal to the instance handling the "
                "inbound message)."
            ),
            callee_instance_id=callee_instance_id,
        )
    if context.session is None:
        # The master-switch and grant-lookup checks both need a DB
        # session. Fail-closed, mirror DefaultDenyToolAuthorizer's
        # no-session refusal.
        return _error(
            reason=REASON_NO_DB_SESSION,
            message=(
                "Sibling dispatch refused: authorisation lookup could "
                "not access a database session (ToolContext.session "
                "is None)."
            ),
            callee_instance_id=callee_instance_id,
        )
    if callee_instance_id == caller_instance_id:
        # A self-call is a degenerate cycle — reject without even
        # consulting the call stack so the error message is precise.
        return _error(
            reason=REASON_SELF_TARGET,
            message=(
                "Sibling dispatch refused: callee equals caller "
                f"(instance_id={callee_instance_id}). Use the local "
                "tool surface, not call_sibling_luciel, for same-"
                "instance work."
            ),
            callee_instance_id=callee_instance_id,
        )

    # Lazily allocate composition state on the first sibling hop. The
    # customer-facing entry point doesn't have to know about this
    # subsystem; the dispatch path materialises it on demand and
    # then propagates the SAME mutable instance to every derived
    # child context. (We mutate the frozen ToolContext via
    # object.__setattr__ — see _attach_state below.)
    state = context.composition_state
    if state is None:
        state = SiblingCompositionState()
        _attach_state(context, state)

    edge = (caller_instance_id, callee_instance_id)

    # ------------------------------------------------------------------
    # (a) Cycle detection
    # ------------------------------------------------------------------
    # A revisit is any callee that already appears as a caller OR a
    # callee in the active stack — that is the cycle definition the
    # spec asks for ("revisit an instance already in the active call
    # stack"). We check the SET of instances on the stack rather than
    # the edge so A->B->A and A->B->C->A are both caught.
    instances_on_stack: set[int] = set()
    for c, e in state.call_stack:
        instances_on_stack.add(c)
        instances_on_stack.add(e)
    if (
        callee_instance_id in instances_on_stack
        or callee_instance_id == caller_instance_id
    ):
        logger.info(
            "Sibling dispatch refused: cycle detected. caller=%s "
            "callee=%s stack=%s inbound=%s",
            caller_instance_id, callee_instance_id,
            state.call_stack, context.inbound_message_id,
        )
        return _error(
            reason=REASON_CYCLE_DETECTED,
            message=(
                f"Sibling dispatch refused: cycle detected. Callee "
                f"instance {callee_instance_id} is already on the "
                f"active call stack for this inbound message."
            ),
            callee_instance_id=callee_instance_id,
        )

    # ------------------------------------------------------------------
    # (b) Per-inbound fan-out budget
    # ------------------------------------------------------------------
    if state.fan_out_count >= SIBLING_FAN_OUT_BUDGET:
        logger.info(
            "Sibling dispatch refused: fan-out budget exhausted. "
            "count=%s budget=%s caller=%s callee=%s inbound=%s",
            state.fan_out_count, SIBLING_FAN_OUT_BUDGET,
            caller_instance_id, callee_instance_id,
            context.inbound_message_id,
        )
        return _error(
            reason=REASON_FAN_OUT_BUDGET_EXHAUSTED,
            message=(
                f"Sibling dispatch refused: per-inbound fan-out "
                f"budget of {SIBLING_FAN_OUT_BUDGET} sibling-call "
                f"invocations has been exhausted for this inbound "
                f"message."
            ),
            callee_instance_id=callee_instance_id,
        )

    # ------------------------------------------------------------------
    # (c) Master switch — call_sibling_luciel authorised on BOTH ends
    # ------------------------------------------------------------------
    # Reuses the WU2 instance_tool_authorizations table. We do NOT go
    # through the full ``DefaultDenyToolAuthorizer.authorize`` path
    # because the tier+channel structural checks there apply to the
    # CALLER side via the broker before we get here. The check we
    # need here is specifically: "is call_sibling_luciel live on
    # this instance" — done via the repository's get_live lookup
    # against the well-known tool_id.
    from app.repositories.instance_tool_authorization_repository import (
        InstanceToolAuthorizationRepository,
    )

    auth_repo = InstanceToolAuthorizationRepository(context.session)
    caller_row = auth_repo.get_live(
        admin_id=context.admin_id,
        instance_id=caller_instance_id,
        tool_id="call_sibling_luciel",
    )
    if caller_row is None or not caller_row.enabled:
        logger.info(
            "Sibling dispatch refused: caller master switch off. "
            "admin=%s caller=%s callee=%s",
            context.admin_id, caller_instance_id, callee_instance_id,
        )
        return _error(
            reason=REASON_CALLER_MASTER_SWITCH_OFF,
            message=(
                "Sibling dispatch refused: call_sibling_luciel is "
                f"not enabled on caller instance "
                f"{caller_instance_id}. The master switch must be on "
                f"both endpoints."
            ),
            callee_instance_id=callee_instance_id,
        )
    callee_row = auth_repo.get_live(
        admin_id=context.admin_id,
        instance_id=callee_instance_id,
        tool_id="call_sibling_luciel",
    )
    if callee_row is None or not callee_row.enabled:
        logger.info(
            "Sibling dispatch refused: callee master switch off. "
            "admin=%s caller=%s callee=%s",
            context.admin_id, caller_instance_id, callee_instance_id,
        )
        return _error(
            reason=REASON_CALLEE_MASTER_SWITCH_OFF,
            message=(
                "Sibling dispatch refused: call_sibling_luciel is "
                f"not enabled on callee instance "
                f"{callee_instance_id}. The master switch must be on "
                f"both endpoints."
            ),
            callee_instance_id=callee_instance_id,
        )

    # ------------------------------------------------------------------
    # (d) Grant lookup — live row in sibling_call_grants
    # ------------------------------------------------------------------
    from app.repositories.sibling_call_grant_repository import (
        SiblingCallGrantRepository,
    )

    grant_repo = SiblingCallGrantRepository(context.session)
    grant = grant_repo.get_live(
        admin_id=context.admin_id,
        caller_instance_id=caller_instance_id,
        callee_instance_id=callee_instance_id,
    )
    if grant is None:
        logger.info(
            "Sibling dispatch refused: no live grant. admin=%s "
            "caller=%s callee=%s",
            context.admin_id, caller_instance_id, callee_instance_id,
        )
        return _error(
            reason=REASON_NO_LIVE_GRANT,
            message=(
                f"Sibling dispatch refused: no live sibling_call_grants "
                f"row authorising caller instance {caller_instance_id} "
                f"to call callee instance {callee_instance_id} for "
                f"admin {context.admin_id!r}. An A->B grant does not "
                f"authorise B->A — each direction needs its own grant."
            ),
            callee_instance_id=callee_instance_id,
        )

    # ------------------------------------------------------------------
    # (e) ON ALL PASSING — push, audit, derive context, hand off
    # ------------------------------------------------------------------
    # Mutate the shared composition state BEFORE the hand-off so that
    # nested sibling calls inside the callee see the updated stack +
    # counter. The dispatch path is the sole writer.
    state.call_stack.append(edge)
    state.fan_out_count += 1

    try:
        _emit_sibling_access_audit(
            context=context,
            caller_instance_id=caller_instance_id,
            callee_instance_id=callee_instance_id,
            grant_id=grant.id,
            depth_after=len(state.call_stack),
            fan_out_after=state.fan_out_count,
            task=task,
        )

        # Derive a ToolContext naming BOTH instances. This is the
        # Wall-3 composition exception (§3.7.3): the row that
        # authorises crossing instance boundaries records both ends.
        # The same composition_state instance is shared so nested
        # sibling calls inside the callee continue to accumulate
        # against the same per-inbound stack + counter.
        derived_context = ToolContext(
            admin_id=context.admin_id,
            instance_id=callee_instance_id,
            session=context.session,
            inbound_message_id=context.inbound_message_id,
            caller_instance_id=caller_instance_id,
            composition_state=state,
        )

        # ------------------------------------------------------------------
        # _ARC14_SEAM: the orchestrator round-trip plugs in HERE.
        # ------------------------------------------------------------------
        # When Arc 14 lands, replace the structured response below
        # with:
        #
        #     from app.runtime.orchestrator import LucielOrchestrator
        #     orchestrator = LucielOrchestrator(...)
        #     response = orchestrator.run(
        #         RuntimeRequest(
        #             admin_id=context.admin_id,
        #             luciel_instance_id=callee_instance_id,
        #             caller_instance_id=caller_instance_id,
        #             inbound_message_id=context.inbound_message_id,
        #             task=task,
        #             payload=payload,
        #             composition_state=state,
        #         )
        #     )
        #     return {"success": True, "output": response.text, ...}
        #
        # All guardrails above stay exactly as they are. The seam is
        # this single block.
        # TODO(ARC14): plug in the orchestrator round-trip.
        return {
            "success": True,
            "output": (
                f"Sibling call authorised and dispatched. The Arc 14 "
                f"orchestrator round-trip is the interim seam — the "
                f"callee Luciel's reply will be returned here when "
                f"Arc 14 lands."
            ),
            "callee_instance_id": callee_instance_id,
            "caller_instance_id": caller_instance_id,
            "grant_id": grant.id,
            "depth": len(state.call_stack),
            "fan_out_count": state.fan_out_count,
            "derived_context": {
                "admin_id": derived_context.admin_id,
                "instance_id": derived_context.instance_id,
                "caller_instance_id": derived_context.caller_instance_id,
                "inbound_message_id": derived_context.inbound_message_id,
            },
            "not_yet_available": True,
            "owning_arc": "ARC14",
        }
    finally:
        # Pop the stack on exit so siblings of THIS hop are not
        # mistakenly treated as cycles. The fan-out counter is NOT
        # decremented — it's a cumulative-per-inbound bound, not a
        # depth bound (Decision #19: no depth limit).
        if state.call_stack and state.call_stack[-1] == edge:
            state.call_stack.pop()


# =====================================================================
# Internals
# =====================================================================


def _attach_state(
    context: ToolContext, state: SiblingCompositionState
) -> None:
    """Attach a composition state to a frozen ToolContext.

    ToolContext is ``frozen=True`` so a tool body cannot rewrite the
    admin/instance identity. The composition state is the one field
    the dispatch path legitimately materialises after construction.
    We use ``object.__setattr__`` — the same escape hatch ``dataclass``
    itself uses in ``__init__`` — to set the attribute exactly once
    on the root context. Subsequent reads see the new value; the
    rest of the frozen surface remains unwritable.
    """
    object.__setattr__(context, "composition_state", state)


def _emit_sibling_access_audit(
    *,
    context: ToolContext,
    caller_instance_id: int,
    callee_instance_id: int,
    grant_id: int,
    depth_after: int,
    fan_out_after: int,
    task: str,
) -> None:
    """Write the sibling-access audit row (§3.7.3 Wall-3 exception).

    The row records BOTH instances under one admin so a regulator
    scanning the chain can reconstruct the composition tree. The
    audit row is best-effort: a write failure logs + suppresses
    rather than failing the dispatch, because the LLM round-trip
    that follows is the customer-visible side effect we're really
    auditing — losing the row is a forensic regression, but
    refusing to dispatch on audit failure would convert an
    observability bug into a customer-facing outage.

    Resource conventions:
      * ``resource_type`` = ``RESOURCE_SIBLING_CALL_GRANT`` — the
        durable record that authorised this hop. Audit chain readers
        can JOIN through ``resource_pk`` to find the grant.
      * ``resource_natural_id`` = ``"{caller}->{callee}"`` — same
        shape as the four WU4 grant-lifecycle verbs so an auditor
        can filter by edge across all five verbs.
      * ``luciel_instance_id`` = ``caller_instance_id`` — the
        originating Luciel for this hop. The callee is recorded in
        ``after_json`` (the row already names the caller in the
        column).
    """
    from app.models.admin_audit_log import (
        ACTION_SIBLING_ACCESS,
        RESOURCE_SIBLING_CALL_GRANT,
    )
    from app.repositories.admin_audit_repository import (
        AdminAuditRepository,
        AuditContext,
    )

    try:
        audit_ctx = AuditContext.system(label="sibling_dispatch")
        repo = AdminAuditRepository(context.session)
        repo.record(
            ctx=audit_ctx,
            admin_id=context.admin_id,
            action=ACTION_SIBLING_ACCESS,
            resource_type=RESOURCE_SIBLING_CALL_GRANT,
            resource_pk=grant_id,
            resource_natural_id=(
                f"{caller_instance_id}->{callee_instance_id}"
            ),
            luciel_instance_id=caller_instance_id,
            before=None,
            after={
                "caller_instance_id": caller_instance_id,
                "callee_instance_id": callee_instance_id,
                "grant_id": grant_id,
                "inbound_message_id": context.inbound_message_id,
                "depth": depth_after,
                "fan_out_count": fan_out_after,
                # Task snippet only — full task text may carry PII
                # and the audit chain is the wrong place for the
                # natural-language payload. The chain links
                # customer-message -> sibling-invocations -> final-
                # response by inbound_message_id; the full task
                # lives in the conversation/trace surface.
                "task_preview": (task[:80] + "...") if len(task) > 80 else task,
            },
            note=(
                f"Sibling dispatch: caller={caller_instance_id} "
                f"-> callee={callee_instance_id} "
                f"(grant_id={grant_id}, depth={depth_after}, "
                f"fan_out={fan_out_after})"
            ),
            autocommit=False,
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.exception(
            "Sibling-access audit write failed (continuing dispatch). "
            "admin=%s caller=%s callee=%s grant=%s err=%s",
            context.admin_id, caller_instance_id, callee_instance_id,
            grant_id, exc,
        )


def _used_tool_id() -> str:
    """The well-known tool_id the master-switch check looks up.

    Pulled out as a function so a future rename can be done in one
    place. Currently a constant — included for grep discoverability
    against ``"call_sibling_luciel"``.
    """
    return "call_sibling_luciel"


__all__ = [
    "SIBLING_FAN_OUT_BUDGET",
    "REASON_CYCLE_DETECTED",
    "REASON_FAN_OUT_BUDGET_EXHAUSTED",
    "REASON_CALLER_MASTER_SWITCH_OFF",
    "REASON_CALLEE_MASTER_SWITCH_OFF",
    "REASON_NO_LIVE_GRANT",
    "REASON_NO_CALLER_CONTEXT",
    "REASON_NO_DB_SESSION",
    "REASON_SELF_TARGET",
    "dispatch_sibling_call",
]
