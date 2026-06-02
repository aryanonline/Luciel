"""Arc 15 WU1 — tier-conditional config-pillar entitlement derivations.

Pins the ARC15 spec tier matrix EXACTLY:

  business_context char cap:
    * Free       -> 280
    * Pro        -> 280
    * Enterprise -> 2000

  custom personality preset (direct axis authoring):
    * Free       -> False
    * Pro        -> True
    * Enterprise -> True

  lead_routing:
    * Free       -> False
    * Pro        -> True
    * Enterprise -> True

  escalation notify channels:
    * Free       -> {email}
    * Pro        -> {email, sms}
    * Enterprise -> {email, sms, slack, custom}

  escalation secondary contact:
    * Free       -> False
    * Pro/Ent    -> True

  escalation chains (ordered contacts + SLA):
    * Free/Pro   -> False
    * Enterprise -> True

These are DERIVATIONS (module functions), NOT TierEntitlement fields —
the frozen dataclass surface is unchanged.
"""
from __future__ import annotations

from dataclasses import fields

from app.policy.entitlements import (
    ESCALATION_NOTIFY_CUSTOM,
    ESCALATION_NOTIFY_EMAIL,
    ESCALATION_NOTIFY_SLACK,
    ESCALATION_NOTIFY_SMS,
    TIER_ENTERPRISE,
    TIER_FREE,
    TIER_PRO,
    TierEntitlement,
    business_context_max_chars,
    custom_personality_enabled,
    escalation_chains_enabled,
    escalation_notify_channels,
    escalation_secondary_contact_enabled,
    lead_routing_enabled,
)


# ---------------------------------------------------------------------
# business_context char cap
# ---------------------------------------------------------------------


def test_business_context_cap_free() -> None:
    assert business_context_max_chars(TIER_FREE) == 280


def test_business_context_cap_pro() -> None:
    assert business_context_max_chars(TIER_PRO) == 280


def test_business_context_cap_enterprise() -> None:
    assert business_context_max_chars(TIER_ENTERPRISE) == 2000


def test_business_context_cap_unknown_tier_fails_closed_to_free() -> None:
    assert business_context_max_chars("not-a-tier") == 280


# ---------------------------------------------------------------------
# custom personality preset
# ---------------------------------------------------------------------


def test_custom_preset_free_false() -> None:
    assert custom_personality_enabled(TIER_FREE) is False


def test_custom_preset_pro_true() -> None:
    assert custom_personality_enabled(TIER_PRO) is True


def test_custom_preset_enterprise_true() -> None:
    assert custom_personality_enabled(TIER_ENTERPRISE) is True


def test_custom_preset_unknown_fails_closed() -> None:
    assert custom_personality_enabled("not-a-tier") is False


# ---------------------------------------------------------------------
# lead_routing
# ---------------------------------------------------------------------


def test_lead_routing_free_false() -> None:
    assert lead_routing_enabled(TIER_FREE) is False


def test_lead_routing_pro_true() -> None:
    assert lead_routing_enabled(TIER_PRO) is True


def test_lead_routing_enterprise_true() -> None:
    assert lead_routing_enabled(TIER_ENTERPRISE) is True


def test_lead_routing_unknown_fails_closed() -> None:
    assert lead_routing_enabled("not-a-tier") is False


# ---------------------------------------------------------------------
# escalation notify channels
# ---------------------------------------------------------------------


def test_escalation_channels_free_email_only() -> None:
    assert escalation_notify_channels(TIER_FREE) == frozenset(
        {ESCALATION_NOTIFY_EMAIL}
    )


def test_escalation_channels_pro_email_sms() -> None:
    assert escalation_notify_channels(TIER_PRO) == frozenset(
        {ESCALATION_NOTIFY_EMAIL, ESCALATION_NOTIFY_SMS}
    )


def test_escalation_channels_enterprise_all() -> None:
    assert escalation_notify_channels(TIER_ENTERPRISE) == frozenset(
        {
            ESCALATION_NOTIFY_EMAIL,
            ESCALATION_NOTIFY_SMS,
            ESCALATION_NOTIFY_SLACK,
            ESCALATION_NOTIFY_CUSTOM,
        }
    )


def test_escalation_channels_unknown_fails_closed_to_email() -> None:
    assert escalation_notify_channels("not-a-tier") == frozenset(
        {ESCALATION_NOTIFY_EMAIL}
    )


def test_email_is_escalation_floor_on_every_tier() -> None:
    for tier in (TIER_FREE, TIER_PRO, TIER_ENTERPRISE):
        assert ESCALATION_NOTIFY_EMAIL in escalation_notify_channels(tier), tier


# ---------------------------------------------------------------------
# escalation secondary contact + chains
# ---------------------------------------------------------------------


def test_secondary_contact_gating() -> None:
    assert escalation_secondary_contact_enabled(TIER_FREE) is False
    assert escalation_secondary_contact_enabled(TIER_PRO) is True
    assert escalation_secondary_contact_enabled(TIER_ENTERPRISE) is True


def test_escalation_chains_enterprise_only() -> None:
    assert escalation_chains_enabled(TIER_FREE) is False
    assert escalation_chains_enabled(TIER_PRO) is False
    assert escalation_chains_enabled(TIER_ENTERPRISE) is True


# ---------------------------------------------------------------------
# Surface discipline — derivations, not dataclass fields.
# ---------------------------------------------------------------------


def test_pillars_not_tier_entitlement_fields() -> None:
    field_names = {f.name for f in fields(TierEntitlement)}
    for name in (
        "business_context_max_chars",
        "custom_personality_enabled",
        "lead_routing_enabled",
        "escalation_notify_channels",
    ):
        assert name not in field_names, name
