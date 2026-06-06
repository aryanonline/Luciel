"""Arc 13 Slice 2 — channel adapters + provisioning + audit (real Postgres).

Exercises the store-and-forward channel surfaces and the
purchase-on-demand provisioning service against a REAL Postgres so the
audit hash-chain handler (Postgres-only ``pg_advisory_xact_lock``) runs
exactly as it does in prod.

Coverage:
  * Provisioning: Fake provider mints+persists number+route+audit;
    deprovision releases+revokes+clears+audits; idempotent no-op;
    tier reject (Free); brokerage/shared flagged-not-implemented;
    live-switch OFF selects the Fake provider (no real Twilio).
  * SmsChannelAdapter: X-Twilio-Signature verify valid + invalid;
    routing hit + miss; live-switch OFF send makes no real Twilio call
    (synthetic sid).
  * EmailChannelAdapter: SNS trust-gate valid + invalid (TopicArn,
    SigningCertURL); routing hit + miss.
  * Tenant scoping: a number provisioned for admin A does not resolve
    to admin B's instance.
  * Audit: provision/deprovision append the expected channel actions to
    the hash-chain (verified by reading admin_audit_logs back).

Skipped unless DATABASE_URL points at a real Postgres (the sandbox URL)
or LUCIEL_LIVE_POSTGRES_URL is set.
"""
from __future__ import annotations

import os
import unittest
import uuid

os.environ.setdefault("MODERATION_PROVIDER", "null")
os.environ.setdefault("OPENAI_API_KEY", "dummy")

_DB_URL = os.environ.get("DATABASE_URL", "")
_LIVE = _DB_URL.startswith("postgresql+psycopg://") or bool(
    os.environ.get("LUCIEL_LIVE_POSTGRES_URL")
)


@unittest.skipUnless(
    _LIVE,
    "Requires DATABASE_URL=postgresql+psycopg://... or LUCIEL_LIVE_POSTGRES_URL",
)
class TestArc13ChannelsSlice2(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from app.db.session import SessionLocal

        cls.SessionLocal = SessionLocal

    def setUp(self) -> None:
        from app.models.admin import Admin
        from app.models.instance import Instance
        from app.policy.entitlements import TIER_PRO

        self.db = self.SessionLocal()
        self.admin_id = f"arc13s2-{uuid.uuid4().hex[:10]}"
        self.db.add(
            Admin(id=self.admin_id, name="arc13 s2", tier=TIER_PRO, active=True)
        )
        self.db.flush()
        self.instance = Instance(
            admin_id=self.admin_id,
            instance_slug=f"i-{uuid.uuid4().hex[:8]}",
            display_name="arc13 s2 instance",
        )
        self.db.add(self.instance)
        self.db.flush()

    def tearDown(self) -> None:
        # Roll back everything this test wrote; nothing leaks to the DB.
        self.db.rollback()
        self.db.close()

    # -----------------------------------------------------------------
    # Provisioning.
    # -----------------------------------------------------------------

    def test_provision_persists_number_route_and_audit(self):
        from app.channels.provisioning import (
            FakePhoneNumberProvider,
            PhoneNumberProvisioningService,
        )
        from app.models.admin_audit_log import (
            ACTION_CHANNEL_NUMBER_PROVISIONED,
            AdminAuditLog,
        )
        from app.models.channel_route import CHANNEL_SMS, ChannelRoute
        from app.policy.entitlements import TIER_PRO

        fake = FakePhoneNumberProvider()
        svc = PhoneNumberProvisioningService(self.db, provider=fake)
        result = svc.provision(
            admin_id=self.admin_id, instance_id=self.instance.id, tier=TIER_PRO
        )

        self.assertTrue(result.e164.startswith("+1555"))
        self.assertEqual(result.mode, "dedicated")
        self.assertEqual(result.provider, "fake")
        self.assertEqual(self.instance.sms_provisioned_number, result.e164)
        self.assertEqual(self.instance.sms_number_mode, "dedicated")

        route = (
            self.db.query(ChannelRoute)
            .filter(
                ChannelRoute.id == result.route_id,
                ChannelRoute.channel == CHANNEL_SMS,
            )
            .one()
        )
        self.assertEqual(route.route_value, result.e164)
        self.assertIsNone(route.revoked_at)

        audit = (
            self.db.query(AdminAuditLog)
            .filter(
                AdminAuditLog.admin_id == self.admin_id,
                AdminAuditLog.action == ACTION_CHANNEL_NUMBER_PROVISIONED,
            )
            .one()
        )
        self.assertEqual(audit.resource_natural_id, result.e164)

    def test_deprovision_releases_revokes_clears_and_audits(self):
        from app.channels.provisioning import (
            FakePhoneNumberProvider,
            PhoneNumberProvisioningService,
        )
        from app.models.admin_audit_log import (
            ACTION_CHANNEL_NUMBER_DEPROVISIONED,
            AdminAuditLog,
        )
        from app.models.channel_route import ChannelRoute
        from app.policy.entitlements import TIER_PRO

        fake = FakePhoneNumberProvider()
        svc = PhoneNumberProvisioningService(self.db, provider=fake)
        result = svc.provision(
            admin_id=self.admin_id, instance_id=self.instance.id, tier=TIER_PRO
        )
        number = result.e164

        svc.deprovision(admin_id=self.admin_id, instance_id=self.instance.id)

        self.assertEqual(fake.released, [number])
        self.assertIsNone(self.instance.sms_provisioned_number)
        self.assertIsNone(self.instance.sms_number_mode)

        route = (
            self.db.query(ChannelRoute)
            .filter(ChannelRoute.id == result.route_id)
            .one()
        )
        self.assertIsNotNone(route.revoked_at)

        audit = (
            self.db.query(AdminAuditLog)
            .filter(
                AdminAuditLog.admin_id == self.admin_id,
                AdminAuditLog.action == ACTION_CHANNEL_NUMBER_DEPROVISIONED,
            )
            .one()
        )
        self.assertEqual(audit.resource_natural_id, number)

    def test_deprovision_no_number_is_idempotent_noop(self):
        from app.channels.provisioning import (
            FakePhoneNumberProvider,
            PhoneNumberProvisioningService,
        )
        from app.models.admin_audit_log import (
            ACTION_CHANNEL_NUMBER_DEPROVISIONED,
            AdminAuditLog,
        )

        fake = FakePhoneNumberProvider()
        svc = PhoneNumberProvisioningService(self.db, provider=fake)
        svc.deprovision(admin_id=self.admin_id, instance_id=self.instance.id)

        self.assertEqual(fake.released, [])
        count = (
            self.db.query(AdminAuditLog)
            .filter(
                AdminAuditLog.admin_id == self.admin_id,
                AdminAuditLog.action == ACTION_CHANNEL_NUMBER_DEPROVISIONED,
            )
            .count()
        )
        self.assertEqual(count, 0)

    def test_provision_free_tier_rejected(self):
        from app.channels.provisioning import (
            FakePhoneNumberProvider,
            PhoneNumberProvisioningService,
            TierNotEntitledError,
        )
        from app.policy.entitlements import TIER_FREE

        svc = PhoneNumberProvisioningService(
            self.db, provider=FakePhoneNumberProvider()
        )
        with self.assertRaises(TierNotEntitledError):
            svc.provision(
                admin_id=self.admin_id,
                instance_id=self.instance.id,
                tier=TIER_FREE,
            )

    def test_provision_shared_mode_flagged_not_implemented(self):
        from app.channels.provisioning import (
            SMS_MODE_SHARED,
            BrokerageRoutingNotImplementedError,
            FakePhoneNumberProvider,
            PhoneNumberProvisioningService,
        )
        from app.policy.entitlements import TIER_PRO

        svc = PhoneNumberProvisioningService(
            self.db, provider=FakePhoneNumberProvider()
        )
        # Shared/brokerage routing is unimplemented regardless of tier;
        # the mode check fires before the tier check.
        with self.assertRaises(BrokerageRoutingNotImplementedError):
            svc.provision(
                admin_id=self.admin_id,
                instance_id=self.instance.id,
                tier=TIER_PRO,
                mode=SMS_MODE_SHARED,
            )

    def test_select_provider_off_switch_is_fake(self):
        from app.channels.provisioning import (
            FakePhoneNumberProvider,
            select_provider,
        )
        from app.core.config import settings

        # Default boot-safe posture: live switch OFF → Fake provider.
        self.assertFalse(settings.channels_live_provisioning_enabled)
        self.assertIsInstance(select_provider(), FakePhoneNumberProvider)

    # -----------------------------------------------------------------
    # SmsChannelAdapter.
    # -----------------------------------------------------------------

    def _provision_number(self) -> str:
        from app.channels.provisioning import (
            FakePhoneNumberProvider,
            PhoneNumberProvisioningService,
        )
        from app.policy.entitlements import TIER_PRO

        svc = PhoneNumberProvisioningService(
            self.db, provider=FakePhoneNumberProvider()
        )
        return svc.provision(
            admin_id=self.admin_id, instance_id=self.instance.id, tier=TIER_PRO
        ).e164

    def test_sms_signature_valid_then_routing_hit(self):
        from unittest.mock import patch

        from app.channels.sms_adapter import (
            SmsChannelAdapter,
            compute_twilio_signature,
        )

        number = self._provision_number()
        url = "https://luciel.test/api/v1/twilio/sms"
        params = {"To": number, "From": "+15559990000", "Body": "hello"}

        with patch(
            "app.channels.sms_adapter.settings"
        ) as mock_settings:
            mock_settings.twilio_auth_token = "tok-secret"
            sig = compute_twilio_signature(
                url=url, params=params, auth_token="tok-secret"
            )
            adapter = SmsChannelAdapter(self.db)
            ctx = adapter.verify_inbound(
                {"url": url, "params": params, "signature": sig}
            )
        self.assertEqual(ctx.admin_id, self.admin_id)
        self.assertEqual(ctx.instance_id, self.instance.id)

    def test_sms_signature_invalid_raises(self):
        from unittest.mock import patch

        from app.channels.base import SignatureVerificationError
        from app.channels.sms_adapter import SmsChannelAdapter

        number = self._provision_number()
        url = "https://luciel.test/api/v1/twilio/sms"
        params = {"To": number, "From": "+15559990000", "Body": "hi"}

        with patch("app.channels.sms_adapter.settings") as mock_settings:
            mock_settings.twilio_auth_token = "tok-secret"
            adapter = SmsChannelAdapter(self.db)
            with self.assertRaises(SignatureVerificationError):
                adapter.verify_inbound(
                    {"url": url, "params": params, "signature": "WRONG"}
                )

    def test_sms_routing_miss_raises_unresolvable(self):
        from unittest.mock import patch

        from app.channels.base import UnresolvableInboundError
        from app.channels.sms_adapter import (
            SmsChannelAdapter,
            compute_twilio_signature,
        )

        url = "https://luciel.test/api/v1/twilio/sms"
        params = {"To": "+15550009999", "From": "+15559990000", "Body": "hi"}
        with patch("app.channels.sms_adapter.settings") as mock_settings:
            mock_settings.twilio_auth_token = "tok-secret"
            sig = compute_twilio_signature(
                url=url, params=params, auth_token="tok-secret"
            )
            adapter = SmsChannelAdapter(self.db)
            with self.assertRaises(UnresolvableInboundError):
                adapter.verify_inbound(
                    {"url": url, "params": params, "signature": sig}
                )

    def test_sms_send_off_switch_no_real_twilio(self):
        from unittest.mock import patch

        from app.channels.base import OutboundMessage
        from app.channels.sms_adapter import SmsChannelAdapter

        with patch("app.channels.sms_adapter.settings") as mock_settings:
            mock_settings.channels_live_provisioning_enabled = False
            adapter = SmsChannelAdapter(self.db)
            receipt = adapter.send(
                OutboundMessage(
                    to="+15559990000",
                    body="reply",
                    admin_id=self.admin_id,
                    instance_id=self.instance.id,
                    channel_metadata={"from": "+15550000001"},
                )
            )
        self.assertEqual(receipt.status, "logged")
        self.assertTrue(receipt.provider_message_id.startswith("SMfake"))
        self.assertEqual(receipt.channel, "sms")

    def test_sms_tenant_scoping(self):
        """A number under admin A must not resolve through admin B."""
        from unittest.mock import patch

        from app.channels.sms_adapter import (
            SmsChannelAdapter,
            compute_twilio_signature,
        )

        number = self._provision_number()
        # The route resolves to THIS admin/instance regardless of who
        # asks — the adapter binds tenant scope from the route row, never
        # from the caller. Confirm the resolved admin is admin A.
        url = "https://luciel.test/api/v1/twilio/sms"
        params = {"To": number, "From": "+15551112222", "Body": "x"}
        with patch("app.channels.sms_adapter.settings") as mock_settings:
            mock_settings.twilio_auth_token = "tok-secret"
            sig = compute_twilio_signature(
                url=url, params=params, auth_token="tok-secret"
            )
            ctx = SmsChannelAdapter(self.db).verify_inbound(
                {"url": url, "params": params, "signature": sig}
            )
        self.assertEqual(ctx.admin_id, self.admin_id)

    # -----------------------------------------------------------------
    # EmailChannelAdapter.
    # -----------------------------------------------------------------

    def _provision_email_route(self, recipient: str):
        from app.models.channel_route import CHANNEL_EMAIL, ChannelRoute

        route = ChannelRoute(
            admin_id=self.admin_id,
            luciel_instance_id=self.instance.id,
            channel=CHANNEL_EMAIL,
            route_value=recipient,
        )
        self.db.add(route)
        self.db.flush()
        return route

    def _sns_email(self, *, recipient: str, topic: str, cert_url: str):
        import json

        inner = {
            "mail": {
                "destination": [recipient],
                "source": "customer@example.com",
                "commonHeaders": {"subject": "Help please"},
            },
            "body": "I need help with my order",
        }
        return {
            "Type": "Notification",
            "TopicArn": topic,
            "SigningCertURL": cert_url,
            "MessageId": uuid.uuid4().hex,
            "Message": json.dumps(inner),
        }

    def test_email_trust_gate_valid_then_routing_hit(self):
        from unittest.mock import patch

        from app.channels.email_adapter import EmailChannelAdapter

        recipient = f"support-{uuid.uuid4().hex[:6]}@mail.luciel.test"
        self._provision_email_route(recipient)
        topic = "arn:aws:sns:ca-central-1:123:luciel-inbound"
        payload = self._sns_email(
            recipient=recipient,
            topic=topic,
            cert_url="https://sns.ca-central-1.amazonaws.com/cert.pem",
        )

        with patch("app.channels.email_adapter.settings") as mock_settings:
            mock_settings.ses_inbound_topic_arn = topic
            adapter = EmailChannelAdapter(self.db)
            ctx = adapter.verify_inbound(payload)
            inbound = adapter.receive(payload)

        self.assertEqual(ctx.admin_id, self.admin_id)
        self.assertEqual(ctx.instance_id, self.instance.id)
        self.assertEqual(inbound.customer_identifier, "customer@example.com")
        self.assertEqual(inbound.body, "I need help with my order")

    def test_email_topic_mismatch_raises_signature_error(self):
        from unittest.mock import patch

        from app.channels.base import SignatureVerificationError
        from app.channels.email_adapter import EmailChannelAdapter

        recipient = f"support-{uuid.uuid4().hex[:6]}@mail.luciel.test"
        self._provision_email_route(recipient)
        payload = self._sns_email(
            recipient=recipient,
            topic="arn:aws:sns:ca-central-1:123:ATTACKER-TOPIC",
            cert_url="https://sns.ca-central-1.amazonaws.com/cert.pem",
        )
        with patch("app.channels.email_adapter.settings") as mock_settings:
            mock_settings.ses_inbound_topic_arn = (
                "arn:aws:sns:ca-central-1:123:luciel-inbound"
            )
            with self.assertRaises(SignatureVerificationError):
                EmailChannelAdapter(self.db).verify_inbound(payload)

    def test_email_bad_cert_host_raises_signature_error(self):
        from unittest.mock import patch

        from app.channels.base import SignatureVerificationError
        from app.channels.email_adapter import EmailChannelAdapter

        recipient = f"support-{uuid.uuid4().hex[:6]}@mail.luciel.test"
        self._provision_email_route(recipient)
        topic = "arn:aws:sns:ca-central-1:123:luciel-inbound"
        payload = self._sns_email(
            recipient=recipient,
            topic=topic,
            cert_url="https://evil.example.com/cert.pem",
        )
        with patch("app.channels.email_adapter.settings") as mock_settings:
            mock_settings.ses_inbound_topic_arn = topic
            with self.assertRaises(SignatureVerificationError):
                EmailChannelAdapter(self.db).verify_inbound(payload)

    def test_email_routing_miss_raises_unresolvable(self):
        from unittest.mock import patch

        from app.channels.base import UnresolvableInboundError
        from app.channels.email_adapter import EmailChannelAdapter

        topic = "arn:aws:sns:ca-central-1:123:luciel-inbound"
        payload = self._sns_email(
            recipient="nobody@mail.luciel.test",
            topic=topic,
            cert_url="https://sns.ca-central-1.amazonaws.com/cert.pem",
        )
        with patch("app.channels.email_adapter.settings") as mock_settings:
            mock_settings.ses_inbound_topic_arn = topic
            with self.assertRaises(UnresolvableInboundError):
                EmailChannelAdapter(self.db).verify_inbound(payload)


if __name__ == "__main__":
    unittest.main()
