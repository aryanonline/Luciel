"""Arc 18 — BudgetAlertService channel selection (§3.4.1b, Vision §7).

Injects fake email/sms senders + a fake session factory so the dispatch
runs without SES/Twilio/Postgres. Pins the tier-shaped channel doctrine:

  * Free exhausted → email only (admin upgrade nudge); customer never sees it.
  * Pro 80% → email only; Pro 100% → email + SMS.
  * Enterprise 80% → email + CSM copy (when configured).
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.policy.entitlements import TIER_FREE, TIER_PRO
from app.services.budget_alert_service import BudgetAlertService


class _FakeSession:
    def __init__(self, *, email="admin@example.com", label="Acme Bot"):
        self._email = email
        self._label = label

    def execute(self, *a, **k):
        # First call resolves the billing email (scalars().first()), the
        # second resolves the instance label (scalar_one_or_none()).
        email = self._email
        label = self._label
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(first=lambda: email),
            scalar_one_or_none=lambda: label,
        )

    def close(self):
        pass


def _svc(emails, smses, *, email="admin@example.com"):
    return BudgetAlertService(
        email_sender=lambda **kw: emails.append(kw),
        sms_sender=lambda **kw: smses.append(kw),
        session_factory=lambda: _FakeSession(email=email),
    )


def _no_audit():
    # The audit leg opens its own SessionLocal; patch record to a no-op so
    # the test never touches a real engine.
    return patch(
        "app.repositories.admin_audit_repository.AdminAuditRepository.record",
        return_value=None,
    )


class TestChannelSelection(unittest.TestCase):

    def test_free_exhausted_emails_only(self):
        emails, smses = [], []
        with _no_audit():
            _svc(emails, smses).send_budget_alert(
                admin_id="a1", instance_id=7, tier=TIER_FREE,
                threshold=100, current=201, cap=200, exhausted=True,
            )
        self.assertEqual(len(emails), 1)
        self.assertTrue(emails[0]["exhausted"])
        self.assertEqual(smses, [])

    def test_pro_80_email_only(self):
        emails, smses = [], []
        with _no_audit():
            _svc(emails, smses).send_budget_alert(
                admin_id="a1", instance_id=7, tier=TIER_PRO,
                threshold=80, current=800, cap=1000,
            )
        self.assertEqual(len(emails), 1)
        self.assertEqual(smses, [])

    def test_pro_100_email_and_sms(self):
        emails, smses = [], []
        with _no_audit():
            _svc(emails, smses).send_budget_alert(
                admin_id="a1", instance_id=7, tier=TIER_PRO,
                threshold=100, current=1001, cap=1000,
            )
        self.assertEqual(len(emails), 1)
        self.assertEqual(len(smses), 1)

    def test_no_billing_email_degrades_to_audit_only(self):
        emails, smses = [], []
        svc = BudgetAlertService(
            email_sender=lambda **kw: emails.append(kw),
            sms_sender=lambda **kw: smses.append(kw),
            session_factory=lambda: _FakeSession(email=None),
        )
        with _no_audit():
            svc.send_budget_alert(
                admin_id="a1", instance_id=7, tier=TIER_FREE,
                threshold=100, current=201, cap=200, exhausted=True,
            )
        # No email could be sent (no billing contact) — but no crash.
        self.assertEqual(emails, [])

    def test_dispatch_never_raises_on_sender_failure(self):
        def _boom(**kw):
            raise RuntimeError("SES down")

        svc = BudgetAlertService(
            email_sender=_boom,
            sms_sender=_boom,
            session_factory=lambda: _FakeSession(),
        )
        with _no_audit():
            # Must not propagate — best-effort posture.
            svc.send_budget_alert(
                admin_id="a1", instance_id=7, tier=TIER_PRO,
                threshold=100, current=1001, cap=1000,
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
