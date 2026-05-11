"""
Action classification — three-tier tool-invocation gate.

Step 30c, Deliverable A.

Purpose
=======

A pluggable runtime gate that runs on every tool invocation routed
through `ToolBroker.execute_tool`, before the tool's `execute()`
method is called. The gate classifies the invocation into exactly one
of three tiers and the tier determines what happens next:

  * ROUTINE              — execute immediately. No notification, no
                           approval. This is what a senior advisor
                           does with reading-shaped or
                           low-blast-radius bookkeeping work. An
                           audit row is written by the broker either
                           way; the tier is recorded on it.

  * NOTIFY_AND_PROCEED   — execute and surface visibly. The action
                           runs without blocking, and the broker
                           returns a result that lets the runtime
                           layer surface what was done to the
                           customer. Reserved for actions that are
                           external-facing but reversible and
                           expected within the customer's existing
                           pattern (e.g. a routine follow-up email,
                           a CRM lead row, escalation to a human).

  * APPROVAL_REQUIRED    — do NOT execute. The broker returns a
                           pending frame describing the proposed
                           action and the reason a tier-bump was
                           triggered. The action runs only after a
                           subsequent confirmation turn. Reserved
                           for actions that are genuinely
                           consequential per Recap §4: irreversible,
                           high-blast-radius, off-pattern, or where
                           Luciel itself is uncertain.

Why a separate gate (not in `LucielTool.execute` itself)
========================================================

A tier check embedded inside each individual tool would (a) duplicate
the same approval logic across every tool we ever ship, (b) silently
break the contract any time a maintainer added a new tool without
remembering to re-implement the check, and (c) leave us with no
single audit anchor that says "this action was classified at tier X
before it executed". The out-of-band classifier runs at one
chokepoint (the broker), fails closed on unknown tools, and emits a
structured tier signal that survives any future tool surface
addition (Step 34 workflow actions, Step 34a channel adapters).

This mirrors the rationale for `app/policy/moderation.py` and is the
same pattern: a single gate in `app/policy/`, a `from_settings`
factory, fail-closed defaults, and a pluggable provider so future
work (an off-pattern detector, per-user trust calibration) can swap
implementations without rewriting the broker integration.

Provider model
==============

We define a Protocol (ActionClassifier) and ship two
implementations:

  * StaticTierRegistryClassifier -- production default. Reads each
    tool's declared tier from `LucielTool.declared_tier` (declared
    on the tool class itself) and returns the corresponding
    ActionClassification. Tools without a declared_tier are not
    classifiable and the classifier raises ToolTierUndeclared.
    Callers (typically the FailClosedActionClassifier) translate
    this into APPROVAL_REQUIRED so an undeclared tool cannot
    silently execute.

  * NullActionClassifier -- never gates; treats every invocation as
    ROUTINE. Used in unit tests that are not testing the gate
    itself, and in dev environments where the operator has opted
    out of action classification entirely. Logs a WARNING on every
    call so it cannot silently ship to production.

The production wiring is FailClosedActionClassifier wrapping
StaticTierRegistryClassifier. If the inner classifier raises
ToolTierUndeclared (a registered tool that forgot to set
`declared_tier`) the wrapper returns
ActionClassification(tier=APPROVAL_REQUIRED, reason='tier_undeclared');
if it raises any other exception the wrapper returns
reason='classifier_error'. Either way the tool is NOT executed.
Note: the broker's separate not-found path (a tool name the
registry does not know about) uses reason='unknown_tool' and is
routed directly inside the broker without invoking the classifier.
The two reason codes are deliberately distinct so an audit log can
tell a stray tool name (LLM hallucination) apart from a
maintainer forgetting to declare a tier on a real tool. The
behavior contract in Recap §4 forbids silent consequential action;
an unclassifiable tool is treated as consequential by default.

What this module deliberately does NOT do
=========================================

  * No off-pattern detection. Off-pattern (an unusual category, an
    unusual amount, an unusual recipient, an unusual time) is named
    in Recap §4 as one of the things that bumps an invocation to
    APPROVAL_REQUIRED. Detecting it needs the four-kinds memory
    architecture to be queryable, and DRIFTS.md `D-context-assembler-thin`
    records that runtime memory composition is still a stub. v1
    therefore classifies on declared tier only. A future
    OffPatternActionClassifier can be added as a third provider
    and composed with the static registry without touching the
    broker.

  * No confirmation-message UX. The broker returns a structured
    pending-frame describing the action; rendering the frame as a
    confirmation message to the customer (and accepting their reply
    as approval) is the Runtime layer's responsibility and lands
    with richer widget UX in Step 31. v1 makes the gate enforceable
    via the structured frame; the conversational shape comes later.

  * No per-tenant tier overrides. Every tenant runs the same tier
    map at v1. A future Step can layer per-tenant overrides (e.g. a
    higher-trust tenant might downgrade certain notify-and-proceed
    tools to routine).

  * No model-side instruction to the LLM about the tiered contract.
    The gate is enforced server-side at the broker; the LLM does
    not need to know which tier a tool sits at to call it. This is
    deliberate: a contract that the model has to remember is a
    contract that degrades silently when the model is swapped.
    Same rationale as the out-of-band moderation gate in §4.9.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)


# =====================================================================
# Tier enum
# =====================================================================


class ActionTier(str, enum.Enum):
    """The three action tiers from ARCHITECTURE §3.3 step 8.

    String-valued so the tier name is stable across the wire (audit
    rows, pending-frame JSON, future widget UX). Adding a fourth
    tier later is a deliberate doc-and-code change, not a constant
    rename.
    """

    ROUTINE = "routine"
    NOTIFY_AND_PROCEED = "notify_and_proceed"
    APPROVAL_REQUIRED = "approval_required"


# =====================================================================
# Exceptions
# =====================================================================


class ToolTierUndeclared(Exception):
    """Raised by a classifier when asked to tier a tool that does
    not declare one. Callers (typically the FailClosedActionClassifier)
    translate this into APPROVAL_REQUIRED so an undeclared tool
    cannot silently execute. Catching this and routing it to ROUTINE
    would defeat the purpose of the gate.
    """


class ConfigurationError(Exception):
    """Raised at boot when the action classifier is configured to a
    value the system cannot actually run. We fail loud at module
    import time rather than at first request so production deploys
    cannot ship with a silently-broken gate. Mirrors the
    `ConfigurationError` doctrine in app/policy/moderation.py.
    """


# =====================================================================
# Result type
# =====================================================================


@dataclass
class ActionClassification:
    """Outcome of a single classification call.

    Attributes
    ----------
    tier : ActionTier
        Which tier the invocation falls into. The broker's behaviour
        is a direct function of this value.
    reason : str
        Short machine-readable reason code. For ROUTINE and
        NOTIFY_AND_PROCEED this is usually 'declared_tier'. For
        APPROVAL_REQUIRED it carries the specific cause:
        'declared_tier' (a tool whose declared_tier is itself
        APPROVAL_REQUIRED — not yet used by any shipped tool but
        reserved), 'tier_undeclared' (the registered tool did not
        set declared_tier and the fail-closed wrapper caught the
        ToolTierUndeclared exception), 'unknown_tool' (the broker
        did not find the tool in the registry at all — set inside
        the broker, never produced by a classifier), and
        'classifier_error' (the classifier raised an arbitrary
        exception and the fail-closed wrapper translated it).
        Operator-only — never returned to the customer.
    classifier : str
        Which classifier produced this result. Composite labels
        like 'static+failclosed' are used by the wrapper.
    """

    tier: ActionTier
    reason: str = "declared_tier"
    classifier: str = ""


# =====================================================================
# Classifier protocol
# =====================================================================


class ActionClassifier(Protocol):
    """Minimal interface an action classifier must satisfy.

    Implementations must raise ToolTierUndeclared (not a bare
    Exception) when asked about a tool whose tier is not known, so
    the FailClosedActionClassifier wrapper can distinguish 'real
    tier unknown' from 'classifier crashed'.
    """

    name: str

    def classify(self, tool) -> ActionClassification:  # pragma: no cover
        ...


# =====================================================================
# Static tier registry classifier (production default)
# =====================================================================


class StaticTierRegistryClassifier:
    """Reads tier directly from `LucielTool.declared_tier`.

    Every tool that lives in `app/tools/implementations/` must
    declare a tier on the class itself. The classifier does no
    inference — it only reads what the tool author committed to.
    This is deliberate: tier is a security property and we want it
    in the same file as the tool's behaviour, where a reviewer
    cannot miss it during code review.

    A tool that ships without a declared_tier raises
    ToolTierUndeclared from this classifier; the fail-closed wrapper
    translates that into APPROVAL_REQUIRED so the missing decoration
    surfaces as a refusal rather than a silent escalation of
    privilege.
    """

    name = "static"

    def classify(self, tool) -> ActionClassification:
        tier = getattr(tool, "declared_tier", None)
        if tier is None:
            raise ToolTierUndeclared(
                f"Tool {getattr(tool, 'name', repr(tool))!r} does not "
                f"declare an action tier. Every tool must set "
                f"`declared_tier` on the class. Refusing to classify."
            )
        if not isinstance(tier, ActionTier):
            # A maintainer typed a plain string ('routine') instead
            # of the enum value. We accept the string if it matches
            # an enum value (defensive ergonomics), and raise
            # otherwise so a typo cannot route silently to the
            # wrong tier.
            try:
                tier = ActionTier(tier)
            except ValueError as exc:
                raise ToolTierUndeclared(
                    f"Tool {getattr(tool, 'name', repr(tool))!r} has "
                    f"declared_tier={tier!r} which is not a valid "
                    f"ActionTier value."
                ) from exc
        return ActionClassification(
            tier=tier,
            reason="declared_tier",
            classifier=self.name,
        )


# =====================================================================
# Null classifier (dev / non-gate tests only)
# =====================================================================


class NullActionClassifier:
    """Treats every invocation as ROUTINE. Logs a WARNING on every
    call.

    Exists so dev environments and unit tests that are not exercising
    the gate can run without declaring tiers on test-doubles. The
    WARNING line is loud by design: any production environment that
    wires this in by accident will surface it in the application log
    stream immediately. Parallel discipline to
    NullModerationProvider in app/policy/moderation.py.
    """

    name = "null"

    def classify(self, tool) -> ActionClassification:
        logger.warning(
            "NullActionClassifier in use -- action classification gate "
            "is DISABLED. Every tool will execute as ROUTINE. This must "
            "not run in production."
        )
        return ActionClassification(
            tier=ActionTier.ROUTINE,
            reason="null_classifier",
            classifier=self.name,
        )


# =====================================================================
# Fail-closed wrapper (production wiring)
# =====================================================================


class FailClosedActionClassifier:
    """Wraps an inner classifier; converts ToolTierUndeclared and
    any other unexpected exception into an APPROVAL_REQUIRED
    classification.

    This is the production wrapper. Rationale: a tool that cannot be
    classified is by definition a tool whose blast radius the
    platform does not understand. Recap §4 forbids silent
    consequential action; the safe default is to refuse to execute
    and surface a pending frame so an operator (or, with Step 31,
    the customer) can decide. The wrapper logs the failure mode at
    WARNING so misconfiguration is observable.
    """

    def __init__(self, inner: ActionClassifier) -> None:
        self._inner = inner
        self.name = f"{inner.name}+failclosed"

    def classify(self, tool) -> ActionClassification:
        try:
            return self._inner.classify(tool)
        except ToolTierUndeclared as exc:
            logger.warning(
                "Action classifier could not tier tool -- failing closed "
                "to APPROVAL_REQUIRED. inner=%s error=%s",
                self._inner.name,
                exc,
            )
            # reason='tier_undeclared' (distinct from the broker's
            # 'unknown_tool' not-found path) so an audit log can
            # tell a stray LLM-emitted tool name apart from a
            # registered tool whose maintainer forgot to declare a
            # tier. Both still route to APPROVAL_REQUIRED.
            return ActionClassification(
                tier=ActionTier.APPROVAL_REQUIRED,
                reason="tier_undeclared",
                classifier=self.name,
            )
        except Exception as exc:
            # A bare-Exception catch is deliberate here. The whole
            # point of the gate is that classifier failure must not
            # translate into silent execution. We log loudly and
            # return APPROVAL_REQUIRED so the broker refuses the
            # action and the operator sees a structured signal.
            logger.warning(
                "Action classifier raised unexpected exception -- "
                "failing closed to APPROVAL_REQUIRED. inner=%s "
                "exc_type=%s error=%s",
                self._inner.name,
                type(exc).__name__,
                exc,
            )
            return ActionClassification(
                tier=ActionTier.APPROVAL_REQUIRED,
                reason="classifier_error",
                classifier=self.name,
            )


# =====================================================================
# Gate factory
# =====================================================================


class ActionClassificationGate:
    """Thin factory that reads settings and returns the right
    classifier.

    The broker imports a single module-level instance built from
    this factory so it never instantiates classifiers directly.
    Wiring lives in one place, the factory; the broker just calls
    .classify(tool). Mirrors the ModerationGate factory pattern in
    app/policy/moderation.py.
    """

    @staticmethod
    def from_settings(settings) -> ActionClassifier:
        """Build the production gate from a Settings instance.

        Recognised values for settings.action_classifier:
          * 'static' -- FailClosedActionClassifier(
                          StaticTierRegistryClassifier())
          * 'null'   -- NullActionClassifier (dev only)

        Raises ConfigurationError at call time for any unknown
        provider value. We fail loud at module import (which is
        when from_settings is called) rather than at first request,
        so a misconfigured production deploy crash-loops on rollout
        rather than silently running with a disabled gate.
        """

        provider_name = getattr(settings, "action_classifier", "static")
        fail_closed = getattr(settings, "action_classifier_fail_closed", True)

        if provider_name == "null":
            return NullActionClassifier()

        if provider_name == "static":
            inner = StaticTierRegistryClassifier()
            if fail_closed:
                return FailClosedActionClassifier(inner)
            # Non-fail-closed mode is a development knob; we log a
            # WARNING so it is visible if anyone ships it. Parallel
            # discipline to ModerationGate's fail_closed=False path.
            logger.warning(
                "action_classifier_fail_closed=False -- tools that do "
                "not declare a tier will raise ToolTierUndeclared "
                "instead of being routed to APPROVAL_REQUIRED. Do NOT "
                "run this configuration in production."
            )
            return inner

        raise ConfigurationError(
            f"Unknown action_classifier={provider_name!r}. "
            f"Expected one of: 'static', 'null'."
        )
