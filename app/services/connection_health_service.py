"""Connection health-check + refresh orchestration — Arc 17.

Shared by the refresh endpoint (Task 3) and the token-refresh worker
(Task 5c). Given an ``InstanceConnection`` row it computes the new
honest ``status`` + ``last_health_check_at`` — WITHOUT touching the DB.
The caller persists the result and writes the audit row in its own
transaction, so this service holds no session and makes no policy
decision about WHO may trigger a refresh.

Two connector shapes:

* LIVE connectors (``record_source`` / ``outbound_webhook``) — a
  reachability/probe check. In this slice the backing is a config
  reference (CSV store_ref / webhook URL) already validated at
  configure time, so the probe is a config-presence check: present →
  ``connected``; absent → ``error``. The real network probe is a
  documented seam.

* DEFERRED OAuth connectors (``calendar`` / ``crm`` / ``email_sender``
  / ``sms_sender``) — token validity. The FULL real refresh path runs
  through the OAuth provider abstraction, but it only COMPLETES when
  the provider ``is_configured()`` AND a stored refresh token exists.
  Absent client creds (this session) → honest ``unconfigured`` +
  arc17_pending; a present-but-rejected refresh token →
  ``expired``. The path NEVER fabricates ``connected``.

The honesty invariant (architecture §3.8.2) lives here: a
``connected`` result is only ever returned when there is a real
backing — a live OAuth token exchange or a present LIVE-connector
config reference.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from app.integrations.oauth import (
    OAuthError,
    OAuthNotConfiguredError,
    get_oauth_provider,
)
from app.integrations.secrets import SecretStoreError, get_secret_store
from app.schemas.connection import (
    DEFERRED_CONNECTION_TYPES,
    LIVE_CONNECTION_TYPES,
)

if TYPE_CHECKING:  # pragma: no cover
    from app.core.config import Settings
    from app.integrations.secrets import SecretStore
    from app.models.instance_connection import InstanceConnection

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HealthCheckResult:
    """The honest outcome of a refresh/health check.

    ``status`` is one of the four DB statuses (never ``action_needed`` —
    that is a frontend label). ``checked_at`` stamps
    ``last_health_check_at`` ONLY on a real check (None when the result
    is the no-op deferred/unconfigured round-trip). ``new_secret_ref``
    carries a rotated secret ref when a silent refresh yielded a new
    refresh token; the caller persists it. ``arc17_pending`` is True when
    the connector is deferred and its OAuth provider is unconfigured —
    the caller surfaces the honest pending marker. ``detail`` is a short
    human-readable line for the audit note + API body.
    """

    status: str
    checked_at: Optional[datetime]
    new_secret_ref: Optional[str] = None
    arc17_pending: bool = False
    detail: str = ""


class ConnectionHealthService:
    """Computes the honest post-refresh status for a connection row."""

    def __init__(
        self,
        settings: "Settings",
        *,
        secret_store: "Optional[SecretStore]" = None,
    ) -> None:
        self._settings = settings
        # Allow injection (tests pass a LocalFakeSecretStore directly);
        # otherwise the factory selects fake/AWS off the settings flag.
        self._secret_store = secret_store or get_secret_store(settings)

    # ------------------------------------------------------------------
    # Public entry — used by both the route and the worker.
    # ------------------------------------------------------------------

    def check_health(self, connection: "InstanceConnection") -> HealthCheckResult:
        """Return the honest new status for ``connection``.

        Pure: reads the row + the OAuth/secret abstractions; never writes
        the DB. The caller persists ``status`` / ``last_health_check_at``
        / ``new_secret_ref`` and audits.
        """
        conn_type = connection.connection_type
        if conn_type in LIVE_CONNECTION_TYPES:
            return self._check_live(connection)
        if conn_type in DEFERRED_CONNECTION_TYPES:
            return self._refresh_oauth(connection)
        # Unreachable for a valid row; fail honest, never fake connected.
        return HealthCheckResult(
            status="error",
            checked_at=self._now(),
            detail=f"Unknown connection_type {conn_type!r}.",
        )

    # ------------------------------------------------------------------
    # LIVE connector — reachability/config-presence probe.
    # ------------------------------------------------------------------

    def _check_live(
        self, connection: "InstanceConnection"
    ) -> HealthCheckResult:
        required_key = (
            "store_ref"
            if connection.connection_type == "record_source"
            else "url"
        )
        cfg = connection.non_secret_config or {}
        if cfg.get(required_key):
            # NOTE: a real network probe of the CSV store / webhook URL is
            # a documented seam. In this slice a present, validated config
            # reference is the backing, so the probe is config-presence.
            return HealthCheckResult(
                status="connected",
                checked_at=self._now(),
                detail=(
                    f"{connection.connection_type} reachable "
                    f"({required_key} present)."
                ),
            )
        return HealthCheckResult(
            status="error",
            checked_at=self._now(),
            detail=(
                f"{connection.connection_type} health check failed: "
                f"missing {required_key} in config."
            ),
        )

    # ------------------------------------------------------------------
    # DEFERRED OAuth connector — token-refresh path.
    # ------------------------------------------------------------------

    def _refresh_oauth(
        self, connection: "InstanceConnection"
    ) -> HealthCheckResult:
        provider = get_oauth_provider(
            connection.connection_type, self._settings
        )

        # No provider wired, or client creds absent → honest unconfigured.
        # DEPLOY-GATED: the live exchange below runs only once client creds
        # are populated; until then every OAuth connector round-trips
        # unconfigured + arc17_pending and NEVER fakes connected.
        if provider is None or not provider.is_configured():
            return HealthCheckResult(
                status="unconfigured",
                checked_at=None,
                arc17_pending=True,
                detail=(
                    f"{connection.connection_type} OAuth client not "
                    "configured; connection stays unconfigured "
                    "(arc17_pending). Live refresh is deploy-gated."
                ),
            )

        # Provider configured but no stored refresh token → cannot refresh.
        if not connection.secret_ref:
            return HealthCheckResult(
                status="unconfigured",
                checked_at=None,
                arc17_pending=True,
                detail=(
                    f"{connection.connection_type} has no stored refresh "
                    "token yet; complete the OAuth consent flow first."
                ),
            )

        # DEPLOY-GATED: read the stored refresh token + call the provider's
        # live token endpoint. Exercised in tests via a fake provider +
        # LocalFakeSecretStore; never hits Google without real creds.
        try:
            refresh_token = self._secret_store.get(connection.secret_ref)
        except SecretStoreError as exc:
            logger.warning(
                "connection %s refresh: secret read failed: %s",
                connection.id,
                exc,
            )
            return HealthCheckResult(
                status="error",
                checked_at=self._now(),
                detail="Stored credential could not be read.",
            )

        try:
            tokens = provider.refresh(refresh_token=refresh_token)
        except OAuthNotConfiguredError:
            return HealthCheckResult(
                status="unconfigured",
                checked_at=None,
                arc17_pending=True,
                detail="OAuth provider became unconfigured.",
            )
        except OAuthError as exc:
            logger.info(
                "connection %s refresh rejected by provider: %s",
                connection.id,
                exc,
            )
            return HealthCheckResult(
                status="expired",
                checked_at=self._now(),
                detail="Refresh token rejected; reconnect required.",
            )

        # Success: a real token came back → connected. Rotate the stored
        # refresh token if the provider issued a new one.
        new_ref = None
        if tokens.refresh_token:
            try:
                new_ref = self._secret_store.rotate(
                    connection.secret_ref, tokens.refresh_token
                )
            except SecretStoreError as exc:  # pragma: no cover - defensive
                logger.warning(
                    "connection %s token rotate failed: %s",
                    connection.id,
                    exc,
                )
        return HealthCheckResult(
            status="connected",
            checked_at=self._now(),
            new_secret_ref=new_ref,
            detail="Token refreshed; connection healthy.",
        )

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)
