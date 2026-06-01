"""Arc 14 U2 — EscalationService flow tests (event store + routing + audit).

Hermetic: a fake DB session captures the rows the service would persist
and a fake audit repo captures the audit call, so we assert the EVENT
ROW field completeness, the TIER-SHAPED routing decision, and the AUDIT
row WITHOUT a live Postgres. The live notify leg stays gated behind the
``channels_live_provisioning_enabled`` switch (False in tests → dry-run).
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app.models.admin_audit_log import (
    ACTION_ESCALATION_FIRED,
    RESOURCE_ESCALATION_EVENT,
)
from app.models.escalation_event import (
    GATE_INTAKE,
    SIGNAL_EXPLICIT_HUMAN_REQUEST,
)
from app.policy.escalation import EscalationDecision, EscalationService
from app.policy.escalation_routing import (
    NOTIFY_EMAIL,
    NOTIFY_SLACK,
    NOTIFY_SMS,
)
from app.policy.entitlements import TIER_ENTERPRISE, TIER_FREE, TIER_PRO


# ---------------------------------------------------------------------
# Fake DB session + audit capture.
# ---------------------------------------------------------------------


class _FakeRow:
    """Stands in for the EscalationEvent ORM row; flush() assigns an id."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.id = None


class _FakeSession:
    def __init__(self, *, tier):
        self.added = []
        self.committed = False
        self.rolled_back = False
        self.closed = False
        self._tier = tier
        self._next_id = 101

    # resolve_contact runs `db.execute(select(Admin.tier)...).scalar_one_or_none()`
    def execute(self, *_a, **_k):
        tier = self._tier

        class _Res:
            def scalar_one_or_none(self_inner):
                return tier

        return _Res()

    def add(self, row):
        self.added.append(row)

    def flush(self):
        for row in self.added:
            if getattr(row, "id", None) is None:
                row.id = self._next_id
                self._next_id += 1

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def _decision():
    return EscalationDecision(
        signal=SIGNAL_EXPLICIT_HUMAN_REQUEST,
        gate=GATE_INTAKE,
        admin_id="admin-1",
        session_id="sess-1",
        luciel_instance_id=7,
        user_id="user-1",
        signal_confidence=0.91,
        reasoning_excerpt="intent=request_human confidence=0.91 >= 0.85",
        signal_inputs={"intent_class": "request_human", "confidence": 0.91},
    )


class _ServiceHarness:
    """Builds an EscalationService wired to a fake session + captures the
    audit call by patching AdminAuditRepository.record."""

    def __init__(self, *, tier):
        self.session = _FakeSession(tier=tier)
        self.audit_calls = []
        self.event_rows = []
        self.svc = EscalationService(session_factory=lambda: self.session)

    def run(self, decision):
        # Patch the ORM row class so add() captures a plain object, and
        # the audit repo so we capture the audit call without a real DB.
        def _fake_event(**kwargs):
            row = _FakeRow(**kwargs)
            self.event_rows.append(row)
            return row

        def _fake_record(_self, **kwargs):
            self.audit_calls.append(kwargs)
            return None

        with patch(
            "app.models.escalation_event.EscalationEvent", side_effect=_fake_event
        ), patch(
            "app.repositories.admin_audit_repository.AdminAuditRepository.record",
            new=_fake_record,
        ):
            return self.svc.record_escalation(decision)


# =====================================================================
# Event store — the row carries every §3.4.5-required field.
# =====================================================================


class TestEventStore(unittest.TestCase):

    def test_event_row_has_all_required_fields(self):
        h = _ServiceHarness(tier=TIER_FREE)
        routing = h.run(_decision())

        self.assertEqual(len(h.event_rows), 1)
        row = h.event_rows[0]
        self.assertEqual(row.signal, SIGNAL_EXPLICIT_HUMAN_REQUEST)
        self.assertEqual(row.gate, GATE_INTAKE)
        self.assertEqual(row.admin_id, "admin-1")
        self.assertEqual(row.luciel_instance_id, 7)
        self.assertEqual(row.session_id, "sess-1")
        self.assertEqual(row.user_id, "user-1")
        self.assertEqual(row.signal_confidence, 0.91)
        # The model-reasoning excerpt is persisted (spec requirement).
        self.assertIn("request_human", row.reasoning_excerpt)
        # The raw inputs are persisted.
        self.assertEqual(row.signal_inputs["intent_class"], "request_human")
        # The event id flushed before commit is surfaced on the routing.
        self.assertEqual(routing.event_id, row.id)
        self.assertTrue(h.session.committed)
        self.assertTrue(h.session.closed)


# =====================================================================
# Tier routing — channel set widens with tier.
# =====================================================================


class TestTierRouting(unittest.TestCase):

    def test_free_is_email_only(self):
        routing = _ServiceHarness(tier=TIER_FREE).run(_decision())
        self.assertEqual(routing.tier, TIER_FREE)
        self.assertEqual(routing.channels, (NOTIFY_EMAIL,))

    def test_pro_is_email_and_sms(self):
        routing = _ServiceHarness(tier=TIER_PRO).run(_decision())
        self.assertEqual(routing.channels, (NOTIFY_EMAIL, NOTIFY_SMS))

    def test_enterprise_is_email_sms_slack_custom(self):
        routing = _ServiceHarness(tier=TIER_ENTERPRISE).run(_decision())
        self.assertIn(NOTIFY_SLACK, routing.channels)
        self.assertEqual(routing.channels[0], NOTIFY_EMAIL)
        self.assertEqual(len(routing.channels), 4)

    def test_notify_is_dry_run_when_live_switch_off(self):
        # Default config has the live-switch False → no real send.
        routing = _ServiceHarness(tier=TIER_PRO).run(_decision())
        self.assertFalse(routing.notified_live)


# =====================================================================
# Audit — one escalation_fired row in the same flow (§5.1).
# =====================================================================


class TestAudit(unittest.TestCase):

    def test_audit_row_written_with_signal_and_channels(self):
        h = _ServiceHarness(tier=TIER_ENTERPRISE)
        h.run(_decision())

        self.assertEqual(len(h.audit_calls), 1)
        call = h.audit_calls[0]
        self.assertEqual(call["action"], ACTION_ESCALATION_FIRED)
        self.assertEqual(call["resource_type"], RESOURCE_ESCALATION_EVENT)
        self.assertEqual(call["admin_id"], "admin-1")
        self.assertEqual(call["luciel_instance_id"], 7)
        self.assertEqual(call["after"]["signal"], SIGNAL_EXPLICIT_HUMAN_REQUEST)
        self.assertEqual(call["after"]["tier"], TIER_ENTERPRISE)
        self.assertIn(NOTIFY_SLACK, call["after"]["notify_channels"])


# =====================================================================
# Best-effort — a persistence failure never raises; routing still returns.
# =====================================================================


class TestBestEffort(unittest.TestCase):

    def test_no_db_session_still_returns_routing(self):
        # session_factory raises → no DB. record_escalation must still
        # return a coherent (Free) routing decision without raising.
        def _boom():
            raise RuntimeError("no db")

        svc = EscalationService(session_factory=_boom)
        routing = svc.record_escalation(_decision())
        self.assertEqual(routing.tier, TIER_FREE)
        self.assertEqual(routing.channels, (NOTIFY_EMAIL,))
        self.assertIsNone(routing.event_id)

    def test_persistence_failure_rolls_back_and_returns_routing(self):
        h = _ServiceHarness(tier=TIER_PRO)

        # Make the audit write blow up AFTER the event flush so the
        # try/except persistence block rolls back.
        def _boom_record(_self, **kwargs):
            raise RuntimeError("audit boom")

        def _fake_event(**kwargs):
            row = _FakeRow(**kwargs)
            h.event_rows.append(row)
            return row

        with patch(
            "app.models.escalation_event.EscalationEvent", side_effect=_fake_event
        ), patch(
            "app.repositories.admin_audit_repository.AdminAuditRepository.record",
            new=_boom_record,
        ):
            routing = h.svc.record_escalation(_decision())

        # No raise; routing still carries the tier-shaped channel set.
        self.assertEqual(routing.channels, (NOTIFY_EMAIL, NOTIFY_SMS))
        self.assertTrue(h.session.rolled_back)
        self.assertIsNone(routing.event_id)


# =====================================================================
# Live-switch ON — notify leg attempted (still no real transport bound).
# =====================================================================


class TestLiveSwitch(unittest.TestCase):

    def test_notify_attempted_when_live_switch_on(self):
        h = _ServiceHarness(tier=TIER_FREE)
        with patch(
            "app.core.config.settings.channels_live_provisioning_enabled", True
        ):
            routing = h.run(_decision())
        self.assertTrue(routing.notified_live)


# =====================================================================
# Legacy entry point preserved (cognition path still works).
# =====================================================================


class TestLegacyEntryPoint(unittest.TestCase):

    def test_handle_escalation_still_log_only(self):
        # No DB, no raise — the pre-Arc-14 call site is unchanged.
        EscalationService().handle_escalation(
            session_id="s", user_id="u", admin_id="a", reason="r"
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
