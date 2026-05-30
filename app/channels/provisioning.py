"""Arc 13 — phone-number provisioning (PhoneNumberProvider + service).

Two layers:

  * :class:`PhoneNumberProvider` — a swappable Protocol for *acquiring*
    and *releasing* an SMS-capable phone number. Two implementations:
        - :class:`TwilioPhoneNumberProvider` — real Twilio REST calls,
          GATED by ``settings.channels_live_provisioning_enabled``. It
          refuses to act when the live switch is off, so a mis-wired
          caller can never bill Twilio.
        - :class:`FakePhoneNumberProvider` — deterministic in-memory
          provider for tests / dev. Mints synthetic E.164 numbers and
          records release calls. Never touches the network.

  * :class:`PhoneNumberProvisioningService` — the orchestration layer
    the channel admin API calls. ``provision`` buys+configures a number
    (provider chosen per the live switch), persists
    ``instances.sms_provisioned_number`` + ``sms_number_mode``, inserts
    a live :class:`ChannelRoute` (channel='sms'), and writes an audit
    row. ``deprovision`` releases the number, soft-revokes the route,
    clears the instance fields, and audits. Free tier is refused at this
    layer too (defence in depth behind the API boundary's tier reject).

Acquisition model (founder-locked): PURCHASE-ON-DEMAND. There is no
pre-bought pool; ``provision`` purchases a number at toggle time and
``deprovision`` releases it. Shared/brokerage routing is DEDICATED-ONLY
in Arc 13 — the ``mode='shared'`` branch is an explicit
flagged-not-implemented raise, never a silent fallthrough.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.admin_audit_log import (
    ACTION_CHANNEL_NUMBER_DEPROVISIONED,
    ACTION_CHANNEL_NUMBER_PROVISIONED,
    RESOURCE_CHANNEL_ROUTE,
)
from app.models.channel_route import CHANNEL_SMS, ChannelRoute
from app.models.instance import Instance
from app.policy.entitlements import sms_dedicated_number_entitled
from app.repositories.admin_audit_repository import AdminAuditRepository, AuditContext

logger = logging.getLogger(__name__)

# SMS number modes.
SMS_MODE_DEDICATED = "dedicated"
SMS_MODE_SHARED = "shared"


# =====================================================================
# Errors.
# =====================================================================


class ProvisioningError(Exception):
    """Base class for provisioning failures."""


class LiveProvisioningDisabledError(ProvisioningError):
    """Raised when a real-Twilio call is attempted but the platform
    live-switch (``settings.channels_live_provisioning_enabled``) is
    off, or a required Twilio credential is unset. Fail-loud — never a
    silent live call against a half-configured account.
    """


class TierNotEntitledError(ProvisioningError):
    """Raised when provisioning is attempted for a tier with no SMS /
    dedicated-number entitlement (Free). Defence in depth behind the
    channel API's tier reject.
    """


class BrokerageRoutingNotImplementedError(ProvisioningError):
    """Raised on the ``mode='shared'`` (brokerage) branch.

    Arc 13 ships DEDICATED-ONLY. Shared/brokerage routing — pooling a
    number across Instances behind a routing broker — is a flagged
    Enterprise affordance (see
    ``entitlements.sms_brokerage_routing_flag``) but is NOT implemented
    in this slice. This explicit raise keeps the not-implemented branch
    visible rather than letting a 'shared' request silently fall through
    to dedicated behaviour.
    """


# =====================================================================
# PhoneNumberProvider — the swappable acquisition interface.
# =====================================================================


@dataclass(frozen=True)
class ProvisionedNumber:
    """The result of a provider acquiring a number."""

    e164: str
    provider: str
    provider_sid: str | None = None


@runtime_checkable
class PhoneNumberProvider(Protocol):
    """Swappable acquire/release surface for an SMS-capable number.

    ``provision`` returns a :class:`ProvisionedNumber`; ``release``
    relinquishes a previously provisioned number. Implementations decide
    whether they touch the network (Twilio) or are in-memory (Fake).
    """

    name: str

    def provision(self, *, webhook_url: str) -> ProvisionedNumber:
        """Acquire an SMS number and wire its inbound webhook."""
        ...

    def release(self, *, e164: str, provider_sid: str | None = None) -> None:
        """Relinquish a previously provisioned number."""
        ...


class TwilioPhoneNumberProvider:
    """Real Twilio purchase-on-demand provider.

    GATED: every method first asserts the platform live switch is on AND
    the required Twilio credentials are present. When the switch is off
    it raises :class:`LiveProvisioningDisabledError` rather than making
    a call — so this provider is only ever selected by the service when
    ``settings.channels_live_provisioning_enabled`` is True, and even if
    constructed directly it cannot fire a live call with the switch off.
    """

    name = "twilio"

    def _assert_live(self) -> None:
        if not settings.channels_live_provisioning_enabled:
            raise LiveProvisioningDisabledError(
                "channels_live_provisioning_enabled is False; refusing a "
                "real Twilio call."
            )
        missing = [
            n
            for n, v in (
                ("twilio_account_sid", settings.twilio_account_sid),
                ("twilio_auth_token", settings.twilio_auth_token),
                ("twilio_webhook_base_url", settings.twilio_webhook_base_url),
            )
            if not v
        ]
        if missing:
            raise LiveProvisioningDisabledError(
                f"Twilio live provisioning requires {missing} to be set."
            )

    def provision(self, *, webhook_url: str) -> ProvisionedNumber:
        self._assert_live()
        # Real Twilio REST: search available numbers then purchase,
        # setting the SMS webhook to ``webhook_url``. Imported lazily so
        # the dev / test path never needs the twilio package installed.
        from twilio.rest import Client  # pragma: no cover - live only

        client = Client(  # pragma: no cover - live only
            settings.twilio_account_sid, settings.twilio_auth_token
        )
        available = client.available_phone_numbers("US").local.list(  # pragma: no cover
            sms_enabled=True, limit=1
        )
        if not available:  # pragma: no cover - live only
            raise ProvisioningError("No SMS-capable numbers available from Twilio.")
        purchased = client.incoming_phone_numbers.create(  # pragma: no cover
            phone_number=available[0].phone_number,
            sms_url=webhook_url,
            sms_method="POST",
        )
        return ProvisionedNumber(  # pragma: no cover - live only
            e164=purchased.phone_number,
            provider=self.name,
            provider_sid=purchased.sid,
        )

    def release(self, *, e164: str, provider_sid: str | None = None) -> None:
        self._assert_live()
        from twilio.rest import Client  # pragma: no cover - live only

        client = Client(  # pragma: no cover - live only
            settings.twilio_account_sid, settings.twilio_auth_token
        )
        if provider_sid:  # pragma: no cover - live only
            client.incoming_phone_numbers(provider_sid).delete()


class FakePhoneNumberProvider:
    """Deterministic in-memory provider for tests / dev.

    Mints synthetic E.164 numbers from a monotonic counter (seeded so
    repeated provisions never collide) and records released numbers so a
    test can assert release happened. Never touches the network — the
    default provider whenever the platform live switch is off.
    """

    name = "fake"

    def __init__(self, *, seed: int = 0) -> None:
        self._counter = seed
        self.released: list[str] = []
        self.provisioned: list[str] = []

    def provision(self, *, webhook_url: str) -> ProvisionedNumber:
        self._counter += 1
        # +1555 0100 + zero-padded counter → a stable fake E.164.
        e164 = f"+1555{self._counter:07d}"
        self.provisioned.append(e164)
        logger.info(
            "FakePhoneNumberProvider: provisioned %s (webhook=%s)",
            e164,
            webhook_url,
        )
        return ProvisionedNumber(
            e164=e164, provider=self.name, provider_sid=f"FAKE{self._counter:07d}"
        )

    def release(self, *, e164: str, provider_sid: str | None = None) -> None:
        self.released.append(e164)
        logger.info("FakePhoneNumberProvider: released %s", e164)


def select_provider() -> PhoneNumberProvider:
    """Return the provider dictated by the platform live switch.

    Live switch ON → :class:`TwilioPhoneNumberProvider` (real calls).
    Live switch OFF (the boot-safe default) → :class:`FakePhoneNumberProvider`
    — guaranteeing no real Twilio call is ever made in dev / CI / tests.
    """
    if settings.channels_live_provisioning_enabled:
        return TwilioPhoneNumberProvider()
    return FakePhoneNumberProvider()


# =====================================================================
# PhoneNumberProvisioningService — orchestration.
# =====================================================================


@dataclass(frozen=True)
class ProvisionResult:
    """Outcome of a provision call: the number + the route row id."""

    e164: str
    mode: str
    provider: str
    route_id: int


class PhoneNumberProvisioningService:
    """Buy/wire/persist + release/clear an SMS number for an Instance.

    Constructed with a DB Session and (optionally) a provider override.
    When no provider is passed, :func:`select_provider` chooses one per
    the platform live switch — the default path callers should use so
    the live switch governs behaviour.
    """

    def __init__(
        self,
        db: Session,
        *,
        provider: PhoneNumberProvider | None = None,
    ) -> None:
        self.db = db
        self.provider = provider or select_provider()
        self.audit = AdminAuditRepository(db)

    def _webhook_url(self) -> str:
        base = (settings.twilio_webhook_base_url or "").rstrip("/")
        return f"{base}/api/v1/twilio/sms"

    def provision(
        self,
        *,
        admin_id: str,
        instance_id: int,
        tier: str,
        mode: str = SMS_MODE_DEDICATED,
        audit_ctx: AuditContext | None = None,
    ) -> ProvisionResult:
        """Provision an SMS number for an Instance.

        Steps (all in the caller's transaction; the audit row commits
        atomically with the mutations):
          1. Refuse Free / non-entitled tiers (TierNotEntitledError).
          2. Refuse the brokerage/shared mode (flagged not-implemented).
          3. Acquire a number via the live-switch-selected provider.
          4. Persist instances.sms_provisioned_number + sms_number_mode.
          5. Insert a live ChannelRoute (channel='sms', route_value=E.164).
          6. Audit ACTION_CHANNEL_NUMBER_PROVISIONED.
        """
        if mode == SMS_MODE_SHARED:
            raise BrokerageRoutingNotImplementedError(
                "Shared/brokerage SMS routing is DEDICATED-ONLY in Arc 13; "
                "the brokerage branch is a flagged not-implemented affordance."
            )
        if mode != SMS_MODE_DEDICATED:
            raise ProvisioningError(f"Unknown SMS number mode {mode!r}.")

        if not sms_dedicated_number_entitled(tier):
            raise TierNotEntitledError(
                f"Tier {tier!r} is not entitled to a dedicated SMS number."
            )

        instance = (
            self.db.query(Instance)
            .filter(Instance.id == instance_id, Instance.admin_id == admin_id)
            .first()
        )
        if instance is None:
            raise ProvisioningError(
                f"Instance id={instance_id} not found under admin {admin_id!r}."
            )

        acquired = self.provider.provision(webhook_url=self._webhook_url())

        instance.sms_provisioned_number = acquired.e164
        instance.sms_number_mode = mode

        route = ChannelRoute(
            admin_id=admin_id,
            luciel_instance_id=instance_id,
            channel=CHANNEL_SMS,
            route_value=acquired.e164,
        )
        self.db.add(route)
        self.db.flush()

        self.audit.record(
            ctx=audit_ctx or AuditContext.system("channel_provisioning"),
            admin_id=admin_id,
            action=ACTION_CHANNEL_NUMBER_PROVISIONED,
            resource_type=RESOURCE_CHANNEL_ROUTE,
            resource_pk=route.id,
            resource_natural_id=acquired.e164,
            luciel_instance_id=instance_id,
            after={
                "number": acquired.e164,
                "mode": mode,
                "provider": acquired.provider,
            },
            note="SMS number provisioned (purchase-on-demand).",
        )

        logger.info(
            "PhoneNumberProvisioningService: provisioned %s for instance=%s "
            "admin=%s mode=%s provider=%s",
            acquired.e164,
            instance_id,
            admin_id,
            mode,
            acquired.provider,
        )
        return ProvisionResult(
            e164=acquired.e164,
            mode=mode,
            provider=acquired.provider,
            route_id=route.id,
        )

    def deprovision(
        self,
        *,
        admin_id: str,
        instance_id: int,
        audit_ctx: AuditContext | None = None,
    ) -> None:
        """Release the Instance's SMS number and tear down its route.

        Idempotent: a no-op (with no provider call and no audit row) when
        the Instance has no provisioned number. Otherwise releases via
        the provider, soft-revokes the live ChannelRoute, clears the
        instance fields, and audits ACTION_CHANNEL_NUMBER_DEPROVISIONED.
        """
        instance = (
            self.db.query(Instance)
            .filter(Instance.id == instance_id, Instance.admin_id == admin_id)
            .first()
        )
        if instance is None:
            raise ProvisioningError(
                f"Instance id={instance_id} not found under admin {admin_id!r}."
            )

        number = instance.sms_provisioned_number
        if not number:
            logger.info(
                "PhoneNumberProvisioningService: deprovision no-op for "
                "instance=%s (no provisioned number).",
                instance_id,
            )
            return

        route = (
            self.db.query(ChannelRoute)
            .filter(
                ChannelRoute.admin_id == admin_id,
                ChannelRoute.luciel_instance_id == instance_id,
                ChannelRoute.channel == CHANNEL_SMS,
                ChannelRoute.route_value == number,
                ChannelRoute.revoked_at.is_(None),
            )
            .first()
        )

        self.provider.release(e164=number)

        route_id: int | None = None
        if route is not None:
            route.revoked_at = datetime.now(timezone.utc)
            route_id = route.id

        instance.sms_provisioned_number = None
        instance.sms_number_mode = None
        self.db.flush()

        self.audit.record(
            ctx=audit_ctx or AuditContext.system("channel_provisioning"),
            admin_id=admin_id,
            action=ACTION_CHANNEL_NUMBER_DEPROVISIONED,
            resource_type=RESOURCE_CHANNEL_ROUTE,
            resource_pk=route_id,
            resource_natural_id=number,
            luciel_instance_id=instance_id,
            before={"number": number},
            note="SMS number released + route revoked.",
        )

        logger.info(
            "PhoneNumberProvisioningService: deprovisioned %s for instance=%s "
            "admin=%s",
            number,
            instance_id,
            admin_id,
        )
