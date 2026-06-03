"""OAuthProvider ABC + token types.

A provider abstracts a single OAuth identity provider (Google Calendar
is the reference). The contract is built around the three real moments
of an OAuth lifecycle: build the consent URL, exchange the returned
auth code for tokens, and silently refresh an access token from a
stored refresh token.

``is_configured()`` is the honesty gate. When client credentials are
absent it returns False and callers MUST round-trip an honest
``unconfigured`` rather than attempt (and fake) a connection.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


class OAuthError(RuntimeError):
    """A live OAuth call failed (network, provider rejection, expired
    refresh token). Callers translate this into an honest ``error`` /
    ``expired`` connection status."""


class OAuthNotConfiguredError(OAuthError):
    """Raised when a token operation is attempted on a provider whose
    client credentials are absent. Callers should check
    ``is_configured()`` first and round-trip ``unconfigured`` — this
    error is the fail-loud backstop if they don't."""


@dataclass(frozen=True)
class OAuthTokens:
    """Tokens returned by an exchange or refresh.

    ``refresh_token`` may be ``None`` on a refresh response (Google only
    re-issues it on first consent); callers keep the prior refresh token
    in that case. ``expires_in`` is seconds-to-expiry as the provider
    reports it.
    """

    access_token: str
    refresh_token: Optional[str]
    expires_in: int
    scope: str = ""
    token_type: str = "Bearer"


class OAuthProvider(ABC):
    """Abstract OAuth identity provider."""

    @property
    @abstractmethod
    def connection_type(self) -> str:
        """The ``instance_connections.connection_type`` this provider
        backs (e.g. ``"calendar"``)."""

    @abstractmethod
    def is_configured(self) -> bool:
        """True only when client credentials are present. False →
        callers round-trip honest ``unconfigured`` + arc17_pending."""

    @abstractmethod
    def authorization_url(self, *, state: str) -> str:
        """Build the consent-screen URL the admin is redirected to.
        Raises ``OAuthNotConfiguredError`` if not configured."""

    @abstractmethod
    def exchange_code(self, *, code: str) -> OAuthTokens:
        """Exchange an auth ``code`` for tokens. Raises
        ``OAuthNotConfiguredError`` if not configured, ``OAuthError`` on
        provider failure."""

    @abstractmethod
    def refresh(self, *, refresh_token: str) -> OAuthTokens:
        """Silently refresh using a stored ``refresh_token``. Raises
        ``OAuthNotConfiguredError`` if not configured, ``OAuthError`` on
        provider failure (e.g. a revoked/expired refresh token)."""
