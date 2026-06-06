"""Unit 13e §3.4.8 — channel-class inactivity timeout mapping.

Pure (no DB): asserts the 5-class table, the channel→class mapping, and
that a new/unknown channel inherits a class without a new constant.
"""
from __future__ import annotations

from app.runtime import session_timeouts as st


def test_five_class_timeout_table():
    assert st.CLASS_TIMEOUT_SECONDS[st.CLASS_SYNCHRONOUS_WEB] == 30 * 60
    assert st.CLASS_TIMEOUT_SECONDS[st.CLASS_ASYNC_MESSAGING] == 4 * 60 * 60
    assert st.CLASS_TIMEOUT_SECONDS[st.CLASS_ASYNC_LONGFORM] == 24 * 60 * 60
    # realtime_voice = end-of-call + 30 min follow-up window
    assert st.CLASS_TIMEOUT_SECONDS[st.CLASS_REALTIME_VOICE] == 30 * 60
    assert st.CLASS_TIMEOUT_SECONDS[st.CLASS_INTERNAL_CHAT] == 30 * 60


def test_channel_to_class_mapping():
    assert st.channel_class("web") == st.CLASS_SYNCHRONOUS_WEB
    assert st.channel_class("widget") == st.CLASS_SYNCHRONOUS_WEB
    assert st.channel_class("sms") == st.CLASS_ASYNC_MESSAGING
    assert st.channel_class("whatsapp") == st.CLASS_ASYNC_MESSAGING
    assert st.channel_class("instagram") == st.CLASS_ASYNC_MESSAGING
    assert st.channel_class("messenger") == st.CLASS_ASYNC_MESSAGING
    assert st.channel_class("email") == st.CLASS_ASYNC_LONGFORM
    assert st.channel_class("voice") == st.CLASS_REALTIME_VOICE
    assert st.channel_class("slack") == st.CLASS_INTERNAL_CHAT


def test_timeout_seconds_by_channel():
    assert st.inactivity_timeout_seconds("widget") == 30 * 60
    assert st.inactivity_timeout_seconds("sms") == 4 * 60 * 60
    assert st.inactivity_timeout_seconds("email") == 24 * 60 * 60


def test_case_insensitive_and_unknown_channel_inherits_class():
    # Case-insensitive lookup.
    assert st.channel_class("WhatsApp") == st.CLASS_ASYNC_MESSAGING
    # An unknown / None channel falls back to the tightest class
    # (synchronous_web, 30 min) — no new constant required.
    assert st.channel_class("teams") == st.CLASS_SYNCHRONOUS_WEB
    assert st.channel_class(None) == st.CLASS_SYNCHRONOUS_WEB
    assert st.inactivity_timeout_seconds("teams") == 30 * 60


def test_redis_key_shape():
    assert st.session_redis_key("abc") == "luciel:session:live:abc"
