"""Arc 15 WU3 — escalation-CONTACT config schemas (Vision §3.4).

These schemas describe the **contact + routing** surface only. They are
deliberately permissive at the structural layer (an open ``dict`` for
the config body) because the authoritative, security-critical validation
— the hard "no escalation-trigger configuration" guard and the
tier-conditional contact/channel/chain rules — lives in
``app.policy.escalation_config`` where the resolved tier is known. The
Pydantic layer only enforces the request envelope.

Critically: there is NO field anywhere here that lets an admin configure
WHEN to escalate. The four escalation signals are fixed runtime
cognition.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class EscalationContact(BaseModel):
    """A single notify destination — channel + value."""

    channel: Literal["email", "sms", "slack", "custom"]
    value: str = Field(..., min_length=1, max_length=320)


class EscalationConfigUpdate(BaseModel):
    """PUT body for the escalation-contact config.

    The body is a free-form object validated by
    ``app.policy.escalation_config.validate_escalation_config_for_tier``
    once the tier is resolved server-side. We do NOT model trigger
    fields here precisely because none are permitted — modelling them
    would imply they are accepted. Unknown / forbidden keys are rejected
    by the policy guard, not silently dropped.
    """

    model_config = ConfigDict(extra="forbid")

    config: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Escalation CONTACT + ROUTING config: primary_email (Free), "
            "primary_contact / secondary_contact / routing_rules (Pro). "
            "NEVER escalation triggers."
        ),
    )


class EscalationConfigResponse(BaseModel):
    """GET/PUT response: the stored escalation-contact config + context."""

    instance_id: int
    admin_id: str
    admin_tier: str
    # The notify channels this tier may route escalation contacts through.
    available_notify_channels: list[str]
    # The four fixed runtime signals — returned for the admin UI to render
    # per-signal routing, but they are READ-ONLY (not configurable).
    escalation_signals: list[str]
    escalation_config: dict[str, Any] | None = None
    updated_at: datetime | None = None
