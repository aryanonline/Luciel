"""Arc 14 U3 — §3.4.2 Channel Arbiter.

Picks the outbound channel for the RESPOND step. Pure decision: it reads
the resolved facts of a turn (which channel the inbound arrived on, how
long the reply is, whether an escalation fired, whether the customer
explicitly asked to switch channel) plus the set of channels the admin
ENABLED for this instance, and returns a ``ChannelChoice``. It touches no
DB and sends nothing — the orchestrator resolves the enabled-channel set
and calls ``arbiter.pick(...)``, then RESPOND acts on the result.

Decision tree, evaluated IN THIS ORDER (§3.4.2):

  1. CUSTOMER-INITIATED SWITCH — the customer explicitly requested a
     channel. Always wins, beating every other rule. (Subject only to
     the enablement constraint: a requested-but-disabled channel falls
     back to the inbound channel.)
  2. LONG SMS REPLY — response > 500 chars AND inbound was SMS → switch
     to email IF email is enabled for the instance, and set a
     permission-prompt marker the RESPOND step acts on (ask the customer
     before moving them off SMS). If email is not enabled, fall through.
  3. URGENT ESCALATION — an escalation fired this turn → pick the
     highest-priority ENABLED channel in order voice > SMS > email.
     Voice is ARC 14b (deferred) and treated as never-available in v1,
     so it falls through to SMS then email.
  4. DEFAULT — same channel as inbound.

CONSTRAINT (§3.4.2): the arbiter may ONLY select a channel the admin
enabled for this instance. Instance channel-enablement is the per-Instance
``instances.enabled_channels`` set (ARC 13; the widget is the structural
floor). If a preferred channel is disabled, fall back to the inbound
channel. When channel info is sparse (empty enabled set, unknown inbound),
default safely to the inbound channel.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.policy.entitlements import (
    CHANNEL_EMAIL,
    CHANNEL_SMS,
    CHANNEL_WIDGET,
)

logger = logging.getLogger(__name__)


# §3.4.2 long-reply threshold: an SMS reply over this many characters is
# a candidate to move to email (SMS fragments badly past this length).
SMS_LENGTH_SWITCH_THRESHOLD: int = 500

# ARC 14b — deferred (Architecture §3.1.2). Voice is the highest-priority
# escalation channel in the doctrine but is OUT OF SCOPE in v1, so the
# arbiter treats it as never-available and never selects it. Listed here
# (not silently omitted) so the priority order is explicit and the
# ARC 14b seam is visible.
CHANNEL_VOICE = "voice"

# Escalation channel priority, highest first (§3.4.2). Voice leads but is
# deferred (never enabled in v1) so the loop falls through to SMS, then
# email.
_ESCALATION_PRIORITY: tuple[str, ...] = (
    CHANNEL_VOICE,  # ARC 14b — deferred (Architecture §3.1.2)
    CHANNEL_SMS,
    CHANNEL_EMAIL,
)


@dataclass(frozen=True)
class ChannelChoice:
    """The arbiter's verdict for one turn.

    ``channel`` is the chosen outbound channel id (always a member of the
    enabled set, or the inbound channel as the safe fallback).
    ``prompt_channel_switch`` is the permission-prompt marker: True only
    when rule 2 moved a long SMS reply to email — the RESPOND step should
    ask the customer's permission before delivering on the new channel.
    ``reason`` is a short human-readable tag for the trace/audit.
    ``switched_from`` records the inbound channel when the choice differs
    from it (None when the channel is unchanged).
    """

    channel: str
    prompt_channel_switch: bool = False
    reason: str = "default_inbound"
    switched_from: str | None = None


@dataclass
class ArbiterInput:
    """The resolved facts the arbiter decides on.

    ``inbound_channel`` is the channel the turn arrived on (the safe
    fallback for every rule). ``enabled_channels`` is the per-Instance
    enabled set (ARC 13 ``instances.enabled_channels`` + the widget
    floor); the arbiter may ONLY pick a member of it. ``response_length``
    is the character length of the reply about to be sent.
    ``escalation_fired`` is True when an escalation fired this turn.
    ``customer_requested_channel`` is a channel id the customer explicitly
    asked to switch to (None when they did not).
    """

    inbound_channel: str
    enabled_channels: set[str] = field(default_factory=set)
    response_length: int = 0
    escalation_fired: bool = False
    customer_requested_channel: str | None = None


class ChannelArbiter:
    """Evaluates the §3.4.2 decision tree. Pure decision, no side-effects."""

    def pick(self, data: ArbiterInput) -> ChannelChoice:
        """Run the four-rule decision tree in order and return a choice.

        The inbound channel is the safe fallback for every rule: a
        preferred channel that is not enabled falls back to inbound, and
        a sparse/empty enabled set degrades to inbound.
        """
        inbound = data.inbound_channel
        enabled = self._effective_enabled(data)

        # 1. CUSTOMER-INITIATED SWITCH — always wins, subject only to the
        #    enablement constraint.
        requested = data.customer_requested_channel
        if requested:
            if requested in enabled:
                return ChannelChoice(
                    channel=requested,
                    reason="customer_requested",
                    switched_from=inbound if requested != inbound else None,
                )
            # Requested but disabled → fall back to inbound (constraint).
            logger.info(
                "customer requested disabled channel %r — falling back to "
                "inbound %r",
                requested,
                inbound,
            )
            return ChannelChoice(
                channel=inbound,
                reason="customer_requested_disabled_fallback_inbound",
            )

        # 2. LONG SMS REPLY → email, IF email enabled; prompt for
        #    permission. Otherwise fall through.
        if (
            inbound == CHANNEL_SMS
            and data.response_length > SMS_LENGTH_SWITCH_THRESHOLD
            and CHANNEL_EMAIL in enabled
        ):
            return ChannelChoice(
                channel=CHANNEL_EMAIL,
                prompt_channel_switch=True,
                reason="long_sms_reply_switch_email",
                switched_from=inbound,
            )

        # 3. URGENT ESCALATION → highest-priority ENABLED channel
        #    (voice > SMS > email). Voice is ARC 14b deferred → never
        #    enabled → falls through to SMS then email.
        if data.escalation_fired:
            for candidate in _ESCALATION_PRIORITY:
                if candidate in enabled:
                    return ChannelChoice(
                        channel=candidate,
                        reason="escalation_priority",
                        switched_from=(
                            inbound if candidate != inbound else None
                        ),
                    )
            # No escalation-priority channel enabled → fall back to inbound.
            return ChannelChoice(
                channel=inbound,
                reason="escalation_no_priority_channel_fallback_inbound",
            )

        # 4. DEFAULT — same channel as inbound.
        return ChannelChoice(channel=inbound, reason="default_inbound")

    @staticmethod
    def _effective_enabled(data: ArbiterInput) -> set[str]:
        """The set of channels the arbiter may pick from.

        Always includes the inbound channel (a turn that arrived on a
        channel can always reply on it — it is the safe fallback) and the
        widget structural floor. Sparse input (empty enabled set) degrades
        to {inbound, widget}, so the arbiter never selects a channel the
        instance cannot serve.
        """
        enabled = set(data.enabled_channels or ())
        enabled.add(CHANNEL_WIDGET)
        enabled.add(data.inbound_channel)
        return enabled
