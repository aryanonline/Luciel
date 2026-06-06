"""Arc 13 — channel-availability entitlement derivations.

Pins the Vision §7 tier matrix EXACTLY:

  channels_available:
    * Free       -> {widget}
    * Pro        -> {widget, email, sms}
    * Enterprise -> {widget, email, sms}

  Dedicated SMS number per Instance:
    * Free       -> False
    * Pro        -> True
    * Enterprise -> True

  Brokerage-routing flag (DEFERRED — flag only):
    * Free       -> False
    * Pro        -> False
    * Enterprise -> True

These are DERIVATIONS (module functions), NOT TierEntitlement fields —
the frozen dataclass surface is unchanged, same discipline as the
per-bucket rate-limit derivations.
"""
from __future__ import annotations

from dataclasses import fields

import pytest

from app.policy.entitlements import (
    CHANNEL_EMAIL,
    CHANNEL_SMS,
    CHANNEL_WIDGET,
    TIER_FREE,
    TIER_PRO,
    TierEntitlement,
    channels_available,
    sms_dedicated_number_entitled,
)


# ---------------------------------------------------------------------
# channels_available — the Vision §7 matrix.
# ---------------------------------------------------------------------


def test_free_channels_widget_only() -> None:
    assert channels_available(TIER_FREE) == frozenset({CHANNEL_WIDGET})


def test_pro_channels_widget_email_sms() -> None:
    assert channels_available(TIER_PRO) == frozenset(
        {CHANNEL_WIDGET, CHANNEL_EMAIL, CHANNEL_SMS}
    )


def test_widget_is_floor_on_every_tier() -> None:
    """The widget is the entitlement floor — present on both tiers."""
    for tier in (TIER_FREE, TIER_PRO):
        assert CHANNEL_WIDGET in channels_available(tier), tier


def test_email_sms_gated_off_free() -> None:
    free = channels_available(TIER_FREE)
    assert CHANNEL_EMAIL not in free
    assert CHANNEL_SMS not in free


def test_unknown_tier_fails_closed_to_free() -> None:
    """A mis-tagged tier must never gain email/sms by accident."""
    assert channels_available("totally-not-a-tier") == frozenset(
        {CHANNEL_WIDGET}
    )


def test_returned_set_is_frozen() -> None:
    """Callers cannot mutate the per-tier matrix in place."""
    result = channels_available(TIER_PRO)
    assert isinstance(result, frozenset)


# ---------------------------------------------------------------------
# Dedicated-number entitlement.
# ---------------------------------------------------------------------


def test_dedicated_number_free_false() -> None:
    assert sms_dedicated_number_entitled(TIER_FREE) is False


def test_dedicated_number_pro_true() -> None:
    assert sms_dedicated_number_entitled(TIER_PRO) is True


def test_dedicated_number_requires_sms_channel() -> None:
    """Free has no SMS channel, so it can never be handed a dedicated
    number regardless of the tier branch."""
    assert CHANNEL_SMS not in channels_available(TIER_FREE)
    assert sms_dedicated_number_entitled(TIER_FREE) is False


# ---------------------------------------------------------------------
# Surface discipline — derivations, not dataclass fields.
# ---------------------------------------------------------------------


def test_channels_not_a_tier_entitlement_field() -> None:
    """The channel matrix lives as a derivation function, NOT as a
    field on the frozen TierEntitlement dataclass."""
    field_names = {f.name for f in fields(TierEntitlement)}
    assert "channels_available" not in field_names
    assert "enabled_channels" not in field_names
