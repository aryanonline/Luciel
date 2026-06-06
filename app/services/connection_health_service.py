"""Connection health-check + refresh orchestration — §3.8.5 auth_class-driven.

Shared by the refresh endpoint (Task 3) and the token-refresh worker
(Task 5c). Given an ``InstanceConnection`` row it computes the new
honest ``status`` + ``last_health_check_at`` — WITHOUT touching the DB.
The caller persists the result and writes the audit row in its own
transaction, so this service holds no session and makes no policy
decision about WHO may trigger a refresh.

§3.8.5 — the dispatch keys on the connection's ``auth_class`` (the
credential SHAPE), NOT the (vertical-leaning) ``connection_type``. The
four classes each have a distinct cadence + action:

* ``oauth_token`` (calendar / crm) — 15-min check + PROACTIVE refresh.
  The FULL real refresh runs through the OAuth provider abstraction but
  only COMPLETES when the provider ``is_configured()`` AND a stored
  refresh token exists. Absent client creds → honest ``unconfigured`` +
  arc17_pending; a present-but-rejected refresh token → ``expired`` +
  ``notify_admin`` (the admin must reconnect). NEVER fakes ``connected``.

* ``long_lived_token`` — 60-min liveness. No live provider backs this
  class today, so it degrades to the same config-presence liveness as
  ``api_key`` (detect-and-flag, no auto-refresh).

* ``api_key`` (record_source / outbound_webhook) — 60-min liveness via
  a config-presence probe (store_ref / url present). No auto-refresh; a
  12-month credential-hygiene nudge is surfaced in ``detail`` only.

* ``provisioned_resource`` (email_sender / sms_sender) — 4-h liveness via
  config-presence of the platform sender identity in non_secret_config.
  No per-tenant credential, so no refresh.

429 / rate-limit backoff: a provider that signals rate-limiting sets
``is_rate_limited`` on the result; the worker records it and applies an
exponential backoff (``next_backoff``) before the next attempt rather
than hammering the provider in-window. The wire-level 429 detection is a
documented deploy-phase seam (today's providers do not surface 429
distinctly); the backoff MATH is testable here.

The honesty invariant (architecture §3.8.2) lives here: a
``connected`` result is only ever returned when there is a real
backing — a live OAuth token exchange or a present config reference.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional

from app.connections.instance_connection import auth_class_for
from app.integrations.oauth import (
    OAuthError,
    OAuthNotConfiguredError,
    get_oauth_provider,
)
from app.integrations.secrets import SecretStoreError, get_secret_store

if TYPE_CHECKING:  # pragma: no cover
    from app.core.config import Settings
    from app.integrations.secrets import SecretStore
    from app.connections.instance_connection import InstanceConnection

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# §3.8.5 cadence constants — the per-auth_class health-check interval.
# The worker uses cadence_for(auth_class) to decide a row is "due"; the
# values are module constants so a test pins each class to its interval.
# ---------------------------------------------------------------------
OAUTH_CHECK_INTERVAL = timedelta(minutes=15)
LONG_LIVED_CHECK_INTERVAL = timedelta(minutes=60)
API_KEY_CHECK_INTERVAL = timedelta(minutes=60)
PROVISIONED_RESOURCE_CHECK_INTERVAL = timedelta(hours=4)

# Proactive OAuth refresh lead: refresh ~30 min before access-token
# expiry rather than waiting for it to lapse (§3.8.5 "proactive refresh").
OAUTH_PROACTIVE_REFRESH_LEAD = timedelta(minutes=30)

# api_key credential-hygiene nudge horizon (§3.8.5): surface a "consider
# rotating" note (detail only — never a status change) once a static key
# has gone this long without rotation.
API_KEY_HYGIENE_NUDGE_AFTER = timedelta(days=365)

# 429 / rate-limit backoff: exponential, 2× per attempt, capped at 1h.
RATE_LIMIT_BACKOFF_FACTOR = 2
RATE_LIMIT_BACKOFF_BASE = timedelta(minutes=1)
RATE_LIMIT_BACKOFF_MAX = timedelta(hours=1)

_CADENCE_BY_CLASS: dict[str, timedelta] = {
    "oauth_token": OAUTH_CHECK_INTERVAL,
    "long_lived_token": LONG_LIVED_CHECK_INTERVAL,
    "api_key": API_KEY_CHECK_INTERVAL,
    "provisioned_resource": PROVISIONED_RESOURCE_CHECK_INTERVAL,
}


def cadence_for(auth_class: str) -> timedelta:
    """Return the §3.8.5 health-check interval for an auth_class.

    Unknown classes fall back to the most conservative (shortest) cadence
    so a mis-classified row is checked MORE often, never less.
    """
    return _CADENCE_BY_CLASS.get(auth_class, OAUTH_CHECK_INTERVAL)


def next_backoff(prev: Optional[timedelta]) -> timedelta:
    """Exponential 429 backoff: double the previous interval (starting at
    ``RATE_LIMIT_BACKOFF_BASE``), capped at ``RATE_LIMIT_BACKOFF_MAX``."""
    if prev is None or prev <= timedelta(0):
        return RATE_LIMIT_BACKOFF_BASE
    doubled = prev * RATE_LIMIT_BACKOFF_FACTOR
    return min(doubled, RATE_LIMIT_BACKOFF_MAX)


def _auth_class_of(connection: "InstanceConnection") -> str:
    """Resolve the connection's auth_class, falling back to the
    type→class mapping for rows whose ORM attribute is absent (SQLite
    stubs / pre-migration rows)."""
    klass = getattr(connection, "auth_class", None)
    if klass:
        return klass
    return auth_class_for(connection.connection_type)


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
    human-readable line for the audit note + API body. ``notify_admin``
    is True on a refresh-fail→expired transition (§3.8.5: tell the admin
    to reconnect). ``is_rate_limited`` is True when the provider signaled
    429 — the worker applies ``next_backoff`` rather than retrying
    in-window.
    """

    status: str
    checked_at: Optional[datetime]
    new_secret_ref: Optional[str] = None
    arc17_pending: bool = False
    detail: str = ""
    notify_admin: bool = False
    is_rate_limited: bool = False


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
        """Return the honest new status for ``connection``, dispatched on
        the §3.8.5 ``auth_class`` (the credential SHAPE).

        Pure: reads the row + the OAuth/secret abstractions; never writes
        the DB. The caller persists ``status`` / ``last_health_check_at``
        / ``new_secret_ref`` and audits.
        """
        klass = _auth_class_of(connection)
        if klass == "oauth_token":
            return self._refresh_oauth(connection)
        if klass == "provisioned_resource":
            return self._check_provisioned_resource(connection)
        if klass in ("api_key", "long_lived_token"):
            # long_lived_token has no live provider today, so it degrades
            # to the same config-presence liveness as api_key (§3.8.5
            # detect-and-flag, no auto-refresh).
            return self._check_liveness(connection)
        # Unreachable for a valid row; fail honest, never fake connected.
        return HealthCheckResult(
            status="error",
            checked_at=self._now(),
            detail=f"Unknown auth_class {klass!r}.",
        )

    # ------------------------------------------------------------------
    # api_key / long_lived_token — config-presence liveness probe.
    # ------------------------------------------------------------------

    def _check_liveness(
        self, connection: "InstanceConnection"
    ) -> HealthCheckResult:
        required_key = (
            "store_ref"
            if connection.connection_type == "record_source"
            else "url"
        )
        cfg = connection.non_secret_config or {}
        if not cfg.get(required_key):
            return HealthCheckResult(
                status="error",
                checked_at=self._now(),
                detail=(
                    f"{connection.connection_type} health check failed: "
                    f"missing {required_key} in config."
                ),
            )
        # NOTE: a real network probe of the CSV store / webhook URL is a
        # documented seam. In this slice a present, validated config
        # reference is the backing, so the probe is config-presence. A
        # 12-month credential-hygiene nudge is surfaced in detail ONLY
        # (never a status change) for static keys past the horizon.
        detail = f"{connection.connection_type} reachable ({required_key} present)."
        if self._past_hygiene_horizon(connection):
            detail += (
                " Hygiene: this static credential is over 12 months old; "
                "consider rotating it."
            )
        return HealthCheckResult(
            status="connected",
            checked_at=self._now(),
            detail=detail,
        )

    def _past_hygiene_horizon(
        self, connection: "InstanceConnection"
    ) -> bool:
        """True when a static credential has gone longer than
        ``API_KEY_HYGIENE_NUDGE_AFTER`` without a successful check/rotation."""
        anchor = (
            getattr(connection, "last_health_check_at", None)
            or getattr(connection, "created_at", None)
        )
        if anchor is None:
            return False
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        return self._now() - anchor > API_KEY_HYGIENE_NUDGE_AFTER

    # ------------------------------------------------------------------
    # provisioned_resource — platform sender-identity presence probe.
    # ------------------------------------------------------------------

    def _check_provisioned_resource(
        self, connection: "InstanceConnection"
    ) -> HealthCheckResult:
        """4-h liveness for email_sender / sms_sender: the platform sender
        identity must be present in non_secret_config (recorded at connect
        time). No per-tenant credential, so no refresh — present →
        connected; absent → error (the connect verification regressed)."""
        cfg = connection.non_secret_config or {}
        present = (
            cfg.get("from_address")
            if connection.connection_type == "email_sender"
            else cfg.get("account_sid")
        )
        if present:
            return HealthCheckResult(
                status="connected",
                checked_at=self._now(),
                detail=(
                    f"{connection.connection_type} provisioned "
                    "(sender identity present)."
                ),
            )
        return HealthCheckResult(
            status="error",
            checked_at=self._now(),
            detail=(
                f"{connection.connection_type} health check failed: "
                "platform sender identity absent from config."
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
                notify_admin=True,
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
