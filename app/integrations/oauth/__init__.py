"""OAuth integration package — Connections layer.

The OAuth-backed connectors (calendar / crm) authenticate via OAuth.
Google Calendar is the reference provider.

Honesty posture (architecture §3.8.2): the FULL real OAuth code path
(auth-URL builder, code→token exchange, silent refresh) is BUILT here.
It completes whenever the OAuth client credentials are present. When they
are absent in a given environment — DEPLOY-GATED, not unbuilt — the
provider reports ``is_configured() is False`` and every caller
round-trips an honest ``unconfigured`` + ``arc17_pending`` marker. The
``arc17_pending`` field name is retained for API stability and means
"deploy-gated pending" (the connect path exists; it needs live client
credentials), NOT "feature not built". The provider NEVER fabricates a
``connected`` result.

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
from app.integrations.oauth.hubspot import HubSpotOAuthProvider
from app.integrations.oauth.salesforce import SalesforceOAuthProvider
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
    "HubSpotOAuthProvider",
    "SalesforceOAuthProvider",
    "get_oauth_provider",
    "OAuthState",
    "OAuthStateError",
    "sign_state",
    "verify_state",
]
