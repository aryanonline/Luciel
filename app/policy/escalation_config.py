"""Tier-conditional validation for the Arc 15 WU3 escalation-CONTACT config.

This is the single place the escalation-contact tier rules + the
hard "no escalation-trigger configuration" guard live, so the
escalation API (and any future caller) agree.

Vision §3.4 / Journey §4.4 — the escalation-contact surface configures
**WHO is notified and HOW**, never **WHEN** to escalate:

  * The four escalation SIGNALS are fixed runtime cognition
    (explicit_human_request, cannot_confidently_answer,
    strong_negative_sentiment, high_value_lead). They are NOT
    admin-configurable. Any payload that tries to set trigger
    conditions, thresholds, enable/disable a signal, or add a new
    signal is REJECTED here.
  * Free       — a single ``primary_email``.
  * Pro        — ``primary_contact`` + optional ``secondary_contact``
                 (each ``{channel: email|sms, value}``) + ``routing_rules``
                 (who is paged per fixed signal, via which notify channel).
  * Enterprise — + ordered ``chains`` (ordered contacts + SLA minutes).

Admin-notification channels per tier (``escalation_notify_channels``):
Free=email; Pro=email+sms; Ent=+slack+custom.

Each function returns a list of structured problem dicts (empty = OK).
The route translates a non-empty list into 422 (malformed/forbidden
shape) or 403 (tier-gated capability), per the reason code.
"""

from __future__ import annotations

from typing import Any

from app.policy.entitlements import (
    escalation_chains_enabled,
    escalation_notify_channels,
    escalation_secondary_contact_enabled,
)

# The four fixed runtime escalation signals (Vision §3.4.5). Admins may
# only address WHO is paged per signal — never whether/when a signal
# fires. This frozenset is the closed vocabulary routing_rules keys are
# validated against.
ESCALATION_SIGNALS: frozenset[str] = frozenset(
    {
        "explicit_human_request",
        "cannot_confidently_answer",
        "strong_negative_sentiment",
        "high_value_lead",
    }
)

# The full notify-channel vocabulary a contact may use. Which subset a
# given tier may actually use is enforced via
# ``escalation_notify_channels(tier)`` — this set only guards against a
# value outside the closed vocabulary entirely.
_CONTACT_CHANNELS: frozenset[str] = frozenset(
    {"email", "sms", "slack", "custom"}
)

# Keys that, if present anywhere in the payload, indicate an attempt to
# configure WHEN to escalate rather than WHO/HOW. Rejected outright.
# (The signal NAMES are allowed as routing_rules KEYS — they address a
# fixed signal — but never as toggles/thresholds.)
_FORBIDDEN_TRIGGER_KEYS: frozenset[str] = frozenset(
    {
        "triggers",
        "trigger",
        "signals",
        "signal_config",
        "thresholds",
        "threshold",
        "conditions",
        "rules",  # ambiguous with WHEN-rules; routing uses routing_rules
        "enabled_signals",
        "disabled_signals",
        "sensitivity",
        "confidence_threshold",
        "auto_escalate",
        "escalate_when",
        "when",
    }
)

# Top-level keys the escalation-CONTACT config may legitimately carry.
_ALLOWED_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "primary_email",
        "primary_contact",
        "secondary_contact",
        "routing_rules",
        "chains",
    }
)


def _contact_problems(contact: Any, *, field: str, tier: str) -> list[dict[str, Any]]:
    """Structural check for one ``{channel, value}`` contact dict."""
    problems: list[dict[str, Any]] = []
    if not isinstance(contact, dict):
        return [
            {
                "field": field,
                "reason": "malformed_contact",
                "message": f"{field} must be an object with 'channel' and 'value'.",
            }
        ]
    channel = contact.get("channel")
    value = contact.get("value")
    if channel not in _CONTACT_CHANNELS:
        problems.append(
            {
                "field": f"{field}.channel",
                "reason": "invalid_contact_channel",
                "message": (
                    f"{field}.channel must be one of {sorted(_CONTACT_CHANNELS)}."
                ),
            }
        )
    elif channel not in escalation_notify_channels(tier):
        problems.append(
            {
                "field": f"{field}.channel",
                "reason": "notify_channel_not_available_on_tier",
                "message": (
                    f"The {channel!r} notify channel is not available on tier "
                    f"{tier!r}. Available: {sorted(escalation_notify_channels(tier))}."
                ),
                "tier": tier,
                "upgrade_required": True,
            }
        )
    if not isinstance(value, str) or not value.strip():
        problems.append(
            {
                "field": f"{field}.value",
                "reason": "missing_contact_value",
                "message": f"{field}.value must be a non-empty string.",
            }
        )
    return problems


def check_no_trigger_config(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Reject any attempt to configure WHEN to escalate.

    Hard guard, tier-independent: the four escalation signals are fixed
    runtime cognition. A payload may address WHO is paged per signal
    (``routing_rules`` keyed by signal name) but may NOT carry trigger
    toggles, thresholds, conditions, or signal enable/disable. Any
    forbidden key or any unknown top-level key is a rejection.
    """
    problems: list[dict[str, Any]] = []
    if not isinstance(config, dict):
        return [
            {
                "field": "escalation_config",
                "reason": "malformed",
                "message": "escalation_config must be an object.",
            }
        ]
    for key in config:
        if key in _FORBIDDEN_TRIGGER_KEYS:
            problems.append(
                {
                    "field": key,
                    "reason": "escalation_triggers_not_configurable",
                    "message": (
                        "Escalation triggers are not configurable. The four "
                        "escalation signals (explicit_human_request, "
                        "cannot_confidently_answer, strong_negative_sentiment, "
                        "high_value_lead) are fixed runtime cognition. This API "
                        "configures WHO is notified and HOW, never WHEN to "
                        "escalate."
                    ),
                }
            )
        elif key not in _ALLOWED_TOP_LEVEL_KEYS:
            problems.append(
                {
                    "field": key,
                    "reason": "unknown_field",
                    "message": (
                        f"{key!r} is not a recognized escalation-contact field. "
                        f"Allowed: {sorted(_ALLOWED_TOP_LEVEL_KEYS)}."
                    ),
                }
            )
    return problems


def check_routing_rules(
    *, routing_rules: Any, tier: str
) -> list[dict[str, Any]]:
    """Validate routing_rules: keys are fixed signals, values name a
    notify channel available on the tier.

    routing_rules addresses WHO/HOW per fixed signal — it does NOT alter
    WHEN a signal fires. An unknown key (not one of the four signals) is
    rejected: it would be an attempt to invent a new trigger.
    """
    if routing_rules is None:
        return []
    problems: list[dict[str, Any]] = []
    if not isinstance(routing_rules, dict):
        return [
            {
                "field": "routing_rules",
                "reason": "malformed",
                "message": "routing_rules must be an object keyed by signal name.",
            }
        ]
    allowed_channels = escalation_notify_channels(tier)
    for signal, target in routing_rules.items():
        if signal not in ESCALATION_SIGNALS:
            problems.append(
                {
                    "field": f"routing_rules.{signal}",
                    "reason": "unknown_escalation_signal",
                    "message": (
                        f"{signal!r} is not one of the four fixed escalation "
                        f"signals {sorted(ESCALATION_SIGNALS)}. New signals "
                        "cannot be defined."
                    ),
                }
            )
            continue
        channel = target.get("channel") if isinstance(target, dict) else None
        if channel is None or channel not in allowed_channels:
            problems.append(
                {
                    "field": f"routing_rules.{signal}.channel",
                    "reason": "notify_channel_not_available_on_tier",
                    "message": (
                        f"routing_rules.{signal}.channel must be a notify "
                        f"channel available on tier {tier!r}: "
                        f"{sorted(allowed_channels)}."
                    ),
                    "tier": tier,
                }
            )
    return problems


def validate_escalation_config_for_tier(
    *, tier: str, config: dict[str, Any]
) -> list[dict[str, Any]]:
    """Run every escalation-contact check, returning all problems.

    Empty list means the payload is a valid contact+routing config
    permitted on the given tier. This NEVER accepts trigger config.
    """
    problems: list[dict[str, Any]] = []

    # Hard guard first: no WHEN-to-escalate configuration, ever.
    problems += check_no_trigger_config(config)
    if problems:
        # If the payload carries forbidden/unknown keys, stop — the rest
        # of the validation assumes a clean contact-only shape.
        return problems

    primary_email = config.get("primary_email")
    primary_contact = config.get("primary_contact")
    secondary_contact = config.get("secondary_contact")
    routing_rules = config.get("routing_rules")
    chains = config.get("chains")

    # Every tier needs at least one primary destination.
    if primary_email is None and primary_contact is None:
        problems.append(
            {
                "field": "primary_email",
                "reason": "primary_contact_required",
                "message": (
                    "An escalation config must name a primary destination: "
                    "either primary_email (Free) or primary_contact (Pro/Ent)."
                ),
            }
        )

    if primary_email is not None and (
        not isinstance(primary_email, str) or "@" not in primary_email
    ):
        problems.append(
            {
                "field": "primary_email",
                "reason": "invalid_email",
                "message": "primary_email must be a valid email address.",
            }
        )

    if primary_contact is not None:
        problems += _contact_problems(
            primary_contact, field="primary_contact", tier=tier
        )

    # Secondary contact + routing rules are Pro/Enterprise only.
    secondary_used = secondary_contact is not None or routing_rules is not None
    if secondary_used and not escalation_secondary_contact_enabled(tier):
        problems.append(
            {
                "field": "secondary_contact",
                "reason": "secondary_contact_not_available_on_tier",
                "message": (
                    "A secondary contact and per-signal routing_rules are "
                    "available on Pro and Enterprise only."
                ),
                "tier": tier,
                "upgrade_required": True,
            }
        )
    else:
        if secondary_contact is not None:
            problems += _contact_problems(
                secondary_contact, field="secondary_contact", tier=tier
            )
        problems += check_routing_rules(routing_rules=routing_rules, tier=tier)

    # Escalation chains are Enterprise only.
    if chains is not None:
        if not escalation_chains_enabled(tier):
            problems.append(
                {
                    "field": "chains",
                    "reason": "chains_not_available_on_tier",
                    "message": (
                        "Ordered escalation chains with SLA minutes are "
                        "available on Enterprise only."
                    ),
                    "tier": tier,
                    "upgrade_required": True,
                }
            )
        else:
            problems += _check_chains(chains=chains, tier=tier)

    return problems


def _check_chains(*, chains: Any, tier: str) -> list[dict[str, Any]]:
    """Validate the Enterprise ordered-chain shape: a list of steps, each
    an ordered contact + positive SLA minutes."""
    problems: list[dict[str, Any]] = []
    if not isinstance(chains, list):
        return [
            {
                "field": "chains",
                "reason": "malformed",
                "message": "chains must be an ordered list of escalation steps.",
            }
        ]
    for idx, step in enumerate(chains):
        if not isinstance(step, dict):
            problems.append(
                {
                    "field": f"chains[{idx}]",
                    "reason": "malformed_chain_step",
                    "message": f"chains[{idx}] must be an object.",
                }
            )
            continue
        problems += _contact_problems(
            step.get("contact"), field=f"chains[{idx}].contact", tier=tier
        )
        sla = step.get("sla_minutes")
        if not isinstance(sla, int) or isinstance(sla, bool) or sla <= 0:
            problems.append(
                {
                    "field": f"chains[{idx}].sla_minutes",
                    "reason": "invalid_sla_minutes",
                    "message": (
                        f"chains[{idx}].sla_minutes must be a positive integer."
                    ),
                }
            )
    return problems


__all__ = [
    "ESCALATION_SIGNALS",
    "check_no_trigger_config",
    "check_routing_rules",
    "validate_escalation_config_for_tier",
]
