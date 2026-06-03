"""GoogleCalendarOAuthProvider — the Arc 17 reference OAuth provider.

Implements the full real Google OAuth 2.0 web-server flow:
  * authorization_url  → accounts.google.com consent URL with
                         access_type=offline + prompt=consent so Google
                         returns a refresh token.
  * exchange_code      → POST oauth2.googleapis.com/token (auth code).
  * refresh            → POST oauth2.googleapis.com/token
                         (grant_type=refresh_token).

DEPLOY-GATED: the network calls require ``google_oauth_client_id`` +
``google_oauth_client_secret`` to be populated (prod SSM). When either
is empty ``is_configured()`` returns False and the token methods raise
``OAuthNotConfiguredError`` BEFORE any network call — so this session
(no Google creds) never reaches the wire and the connector stays an
honest ``unconfigured``. The code path itself is identical in prod; only
the credential presence differs.
"""
from __future__ import annotations

from urllib.parse import urlencode

import httpx

from app.integrations.oauth.base import (
    OAuthError,
    OAuthNotConfiguredError,
    OAuthProvider,
    OAuthTokens,
)

_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
_HTTP_TIMEOUT = 10.0


class GoogleCalendarOAuthProvider(OAuthProvider):
    """Google Calendar OAuth 2.0 web-server-flow provider."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri

    @property
    def connection_type(self) -> str:
        return "calendar"

    def is_configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    def _require_configured(self) -> None:
        if not self.is_configured():
            raise OAuthNotConfiguredError(
                "Google OAuth client credentials are absent; calendar "
                "connector stays unconfigured (arc17_pending)."
            )

    def authorization_url(self, *, state: str) -> str:
        self._require_configured()
        params = {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "response_type": "code",
            "scope": _CALENDAR_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        return f"{_AUTH_ENDPOINT}?{urlencode(params)}"

    def exchange_code(self, *, code: str) -> OAuthTokens:
        self._require_configured()
        payload = {
            "code": code,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "redirect_uri": self._redirect_uri,
            "grant_type": "authorization_code",
        }
        return self._token_request(payload)

    def refresh(self, *, refresh_token: str) -> OAuthTokens:
        self._require_configured()
        payload = {
            "refresh_token": refresh_token,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "grant_type": "refresh_token",
        }
        return self._token_request(payload)

    def _token_request(self, payload: dict) -> OAuthTokens:
        # DEPLOY-GATED: live POST to Google's token endpoint. Reached
        # only when is_configured() is True (real client creds present).
        try:
            resp = httpx.post(
                _TOKEN_ENDPOINT, data=payload, timeout=_HTTP_TIMEOUT
            )
        except httpx.HTTPError as exc:  # pragma: no cover - DEPLOY-GATED
            raise OAuthError(f"Google token request failed: {exc}") from exc
        if resp.status_code != 200:  # pragma: no cover - DEPLOY-GATED
            raise OAuthError(
                f"Google token endpoint returned {resp.status_code}: "
                f"{resp.text[:200]}"
            )
        body = resp.json()
        access_token = body.get("access_token")
        if not access_token:  # pragma: no cover - DEPLOY-GATED
            raise OAuthError("Google token response had no access_token")
        return OAuthTokens(
            access_token=access_token,
            refresh_token=body.get("refresh_token"),
            expires_in=int(body.get("expires_in", 0)),
            scope=body.get("scope", ""),
            token_type=body.get("token_type", "Bearer"),
        )
