"""OAuth integration package — Arc 17 Connections layer.

The deferred connectors (calendar / crm / email_sender / sms_sender)
authenticate via OAuth. Google Calendar is the reference provider per
the Arc 17 brief.

Honesty posture (architecture §3.8.2): the FULL real OAuth code path
(auth-URL builder, code→token exchange, silent refresh) is built here,
but it only completes when client credentials are present. When they
are absent — this session, by design, since no Google client creds are
available — the provider reports ``is_configured() is False`` and every
caller round-trips an honest ``unconfigured`` + ``arc17_pending``
marker. The provider NEVER fabricates a ``connected`` result.

Three pieces:
  * ``OAuthProvider``    — the ABC (is_configured / authorization_url /
                           exchange_code / refresh).
  * ``OAuthTokens``      — the token bundle returned by exchange/refresh.
  * ``OAuthError`` / ``OAuthNotConfiguredError`` — failure types.
  * ``GoogleCalendarOAuthProvider`` — the reference provider.
  * ``get_oauth_provider(connection_type, settings)`` — factory.
"""
from __future__ import annotations

from app.integrations.oauth.base import (
    OAuthError,
    OAuthNotConfiguredError,
    OAuthProvider,
    OAuthTokens,
)
from app.integrations.oauth.google_calendar import (
    GoogleCalendarOAuthProvider,
)
from app.integrations.oauth.factory import get_oauth_provider
from app.integrations.oauth.state import (
    OAuthState,
    OAuthStateError,
    sign_state,
    verify_state,
)

__all__ = [
    "OAuthError",
    "OAuthNotConfiguredError",
    "OAuthProvider",
    "OAuthTokens",
    "GoogleCalendarOAuthProvider",
    "get_oauth_provider",
    "OAuthState",
    "OAuthStateError",
    "sign_state",
    "verify_state",
]
