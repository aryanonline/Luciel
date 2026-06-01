"""Arc 14 U5 — §3.3.3 dispatch-time tier/channel re-check.

U5 threads the Admin's tier and the per-instance enabled-channel set
onto ``ToolContext`` from the orchestrator ACT step, so the broker's
gate-1 dispatch-time re-check (``DefaultDenyToolAuthorizer._check_tier``
/ ``_check_channels``) can fully enforce on the agentic-loop path —
closing the markers that previously claimed the data was "not yet
threaded onto ToolContext".

These tests exercise the two checks directly (no DB, no network): given
a ToolContext that DOES carry the fields, a mismatch denies; a context
that omits the fields skips the re-check (the backward-compatible
default for legacy / non-loop call sites).
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MODERATION_PROVIDER", "null")
os.environ.setdefault("OPENAI_API_KEY", "dummy")

from app.tools.authorization import DefaultDenyToolAuthorizer
from app.tools.base import ToolContext


class _FakeTool:
    """Minimal stand-in carrying just the attrs the two checks read."""

    def __init__(self, *, tool_id, requires_tier=(), requires_channels=frozenset()):
        self._tool_id = tool_id
        self._requires_tier = tuple(requires_tier)
        self._requires_channels = frozenset(requires_channels)

    @property
    def tool_id(self):
        return self._tool_id

    @property
    def requires_tier(self):
        return self._requires_tier

    @property
    def requires_channels(self):
        return self._requires_channels


def _ctx(*, admin_tier=None, enabled_channels=None):
    return ToolContext(
        admin_id="admin-1",
        instance_id=7,
        admin_tier=admin_tier,
        enabled_channels=enabled_channels,
    )


# ---------------------------------------------------------------------
# Tier re-check
# ---------------------------------------------------------------------


def test_tier_recheck_denies_on_mismatch_when_tier_present():
    auth = DefaultDenyToolAuthorizer()
    tool = _FakeTool(tool_id="enterprise_only", requires_tier=("enterprise",))
    decision = auth._check_tier(tool, _ctx(admin_tier="free"))
    assert decision.allowed is False
    assert decision.failure_kind == "tier_not_permitted"


def test_tier_recheck_allows_when_tier_matches():
    auth = DefaultDenyToolAuthorizer()
    tool = _FakeTool(tool_id="pro_tool", requires_tier=("pro", "enterprise"))
    assert auth._check_tier(tool, _ctx(admin_tier="pro")).allowed is True


def test_tier_recheck_skips_when_tier_absent():
    # Backward-compatible default: a context without admin_tier skips
    # the re-check (legacy / unit-test call sites are unaffected).
    auth = DefaultDenyToolAuthorizer()
    tool = _FakeTool(tool_id="enterprise_only", requires_tier=("enterprise",))
    assert auth._check_tier(tool, _ctx(admin_tier=None)).allowed is True


# ---------------------------------------------------------------------
# Channel re-check
# ---------------------------------------------------------------------


def test_channel_recheck_denies_on_missing_channel_when_set_present():
    auth = DefaultDenyToolAuthorizer()
    tool = _FakeTool(tool_id="send_sms", requires_channels={"sms"})
    decision = auth._check_channels(
        tool, _ctx(enabled_channels=frozenset({"email"}))
    )
    assert decision.allowed is False
    assert decision.failure_kind == "channel_not_enabled"


def test_channel_recheck_allows_when_channel_enabled():
    auth = DefaultDenyToolAuthorizer()
    tool = _FakeTool(tool_id="send_sms", requires_channels={"sms"})
    decision = auth._check_channels(
        tool, _ctx(enabled_channels=frozenset({"sms", "email"}))
    )
    assert decision.allowed is True


def test_channel_recheck_skips_when_set_absent():
    auth = DefaultDenyToolAuthorizer()
    tool = _FakeTool(tool_id="send_sms", requires_channels={"sms"})
    assert auth._check_channels(tool, _ctx(enabled_channels=None)).allowed is True


def test_no_required_channels_always_allows():
    auth = DefaultDenyToolAuthorizer()
    tool = _FakeTool(tool_id="lookup_property")  # no requires_channels
    assert auth._check_channels(tool, _ctx(enabled_channels=None)).allowed is True
