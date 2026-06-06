"""Unit 13e §3.4.8 — session inactivity timeouts by channel CLASS.

§3.4.8 defines a session's inactivity timeout NOT per individual channel
but per channel CLASS, so a NEW channel inherits a class (and its timeout)
without minting a new constant — that is the doc's explicit goal. The
5-class table:

    Class               Channels                                  Timeout
    -----               --------                                  -------
    synchronous_web     web, widget                               30 min
    async_messaging     sms, whatsapp, instagram, messenger       4 h
    async_longform      email                                     24 h
    realtime_voice      voice                                     end-of-call
                                                                  + 30 min
    internal_chat       slack                                     30 min

These are PLATFORM constants — platform-tunable, NOT admin-configurable
(§3.4.8). The live session state is Redis-keyed with TTL = the channel's
class timeout; on TTL expiry the §3.4.7 summarization/finalization fires
(see app.worker.tasks.session_sweep for the deterministic in-sandbox
trigger; live Redis keyspace-expiry notifications are the deploy-phase
optimization).

§3.4.9 reopened-thread rule: a new inbound after a session has ended
starts a NEW session (a new budget unit) — it does not extend the expired
one. That rule is enforced at the ingress/session-resolution seam, not
here; this module only owns the class→timeout table + mapping.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Channel classes (§3.4.8). String identifiers so they can appear in audit
# rows / logs without an enum import.
# ---------------------------------------------------------------------------
CLASS_SYNCHRONOUS_WEB = "synchronous_web"
CLASS_ASYNC_MESSAGING = "async_messaging"
CLASS_ASYNC_LONGFORM = "async_longform"
CLASS_REALTIME_VOICE = "realtime_voice"
CLASS_INTERNAL_CHAT = "internal_chat"

# Inactivity timeout per class, in SECONDS. Platform-tunable; not
# admin-configurable (§3.4.8).
#
# realtime_voice: the session ends at end-of-call (an explicit hangup
# signal); the 30-minute value here is the FOLLOW-UP inactivity window
# after the call ends before finalization runs, so the table carries a
# concrete TTL the sweep / Redis TTL can use even for the voice class.
_THIRTY_MIN = 30 * 60
_FOUR_HOURS = 4 * 60 * 60
_TWENTY_FOUR_HOURS = 24 * 60 * 60

CLASS_TIMEOUT_SECONDS: dict[str, int] = {
    CLASS_SYNCHRONOUS_WEB: _THIRTY_MIN,
    CLASS_ASYNC_MESSAGING: _FOUR_HOURS,
    CLASS_ASYNC_LONGFORM: _TWENTY_FOUR_HOURS,
    CLASS_REALTIME_VOICE: _THIRTY_MIN,  # end-of-call + 30 min follow-up
    CLASS_INTERNAL_CHAT: _THIRTY_MIN,
}

# ---------------------------------------------------------------------------
# Channel → class mapping. The SINGLE place a channel is bound to a class.
# A new channel is added here (one line) and inherits its class's timeout —
# no new timeout constant is minted (§3.4.8 goal).
# ---------------------------------------------------------------------------
_CHANNEL_TO_CLASS: dict[str, str] = {
    # synchronous_web
    "web": CLASS_SYNCHRONOUS_WEB,
    "widget": CLASS_SYNCHRONOUS_WEB,
    "programmatic_api": CLASS_SYNCHRONOUS_WEB,
    # async_messaging
    "sms": CLASS_ASYNC_MESSAGING,
    "whatsapp": CLASS_ASYNC_MESSAGING,
    "instagram": CLASS_ASYNC_MESSAGING,
    "messenger": CLASS_ASYNC_MESSAGING,
    # async_longform
    "email": CLASS_ASYNC_LONGFORM,
    # realtime_voice
    "voice": CLASS_REALTIME_VOICE,
    # internal_chat
    "slack": CLASS_INTERNAL_CHAT,
}

# Fallback class for an unmapped channel. synchronous_web (30 min) is the
# tightest lead-facing window — failing closed to the SHORTEST timeout
# means an unknown channel never holds a live session (and its budget
# unit) open longer than the strictest class, which is the safe default.
_DEFAULT_CLASS = CLASS_SYNCHRONOUS_WEB


def channel_class(channel: str | None) -> str:
    """Map a channel id to its §3.4.8 class.

    Unknown / None channels fall back to synchronous_web (the tightest
    window). A new channel inherits a class by adding ONE line to
    ``_CHANNEL_TO_CLASS`` — no new timeout constant required.
    """
    if not channel:
        return _DEFAULT_CLASS
    return _CHANNEL_TO_CLASS.get(channel.lower(), _DEFAULT_CLASS)


def inactivity_timeout_seconds(channel: str | None) -> int:
    """Return the inactivity timeout (seconds) for a channel's class."""
    return CLASS_TIMEOUT_SECONDS[channel_class(channel)]


def session_redis_key(session_id: str) -> str:
    """The Redis key that holds a session's live liveness marker.

    The marker is SET with TTL = the channel's inactivity timeout on every
    inbound turn; its expiry is the §3.4.8 end-of-session signal. The
    deterministic sweep (app.worker.tasks.session_sweep) is the in-sandbox
    fallback for the expiry → finalization trigger; live Redis
    keyspace-notifications are the deploy-phase optimization.
    """
    return f"luciel:session:live:{session_id}"
