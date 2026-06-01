"""Arc 14 U4 — §3.4.4 Lead Capture + §3.4.7 Summary + §3.4.6 Handoff.

Hermetic: a fake DB session captures the lead row + audit row the
finalizer would persist, so we assert the LEAD ROW field completeness,
the SUMMARY persisted alongside it, and the HANDOFF BUNDLE contents
(transcript + summary + lead) WITHOUT a live Postgres, no LLM, no
network (founder decision #2). The deterministic detector + summarizer
make every boundary assertable.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app.cognition.finalizer import CognitionFinalizer, HandoffBundle
from app.cognition.lead_capture import detect
from app.cognition.summarizer import summarize


# ---------------------------------------------------------------------
# Fake DB session — captures the lead row + audit row.
# ---------------------------------------------------------------------


class _FakeSession:
    def __init__(self):
        self.added = []
        self.committed = False
        self.rolled_back = False
        self.closed = False
        self._next_id = 501

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


class _RecordingAudit:
    """Captures the AdminAuditRepository.record(...) call."""

    instances = []

    def __init__(self, db):
        self.db = db
        self.calls = []
        _RecordingAudit.instances.append(self)

    def record(self, **kwargs):
        self.calls.append(kwargs)
        return None


def _finalizer(session):
    """Build a CognitionFinalizer wired to the fake session + audit."""
    _RecordingAudit.instances = []
    fin = CognitionFinalizer(session_factory=lambda: session)
    return fin


# =====================================================================
# §3.4.4 lead-threshold detection — fires / does NOT fire.
# =====================================================================


class TestLeadThreshold(unittest.TestCase):
    def test_contact_info_email_crosses(self):
        c = detect(message="You can reach me at jane@example.com")
        self.assertIsNotNone(c)
        self.assertIn("contact_info", c.triggers)
        self.assertEqual(c.contact_channel, "email")
        self.assertEqual(c.contact_identifier, "jane@example.com")

    def test_contact_info_phone_crosses(self):
        c = detect(message="call me on (415) 555-0123 please")
        self.assertIsNotNone(c)
        self.assertIn("contact_info", c.triggers)
        self.assertEqual(c.contact_channel, "sms")

    def test_budget_crosses_and_sets_lead_value(self):
        c = detect(message="my budget is around $750,000")
        self.assertIsNotNone(c)
        self.assertIn("budget", c.triggers)
        self.assertEqual(c.lead_value, 750_000.0)

    def test_listing_intent_crosses(self):
        c = detect(message="I'd like to view 123 Main Street this weekend")
        self.assertIsNotNone(c)
        self.assertIn("listing_intent", c.triggers)

    def test_idle_chitchat_does_not_cross(self):
        for msg in ("hi", "thanks!", "what's the weather like?", "ok cool"):
            self.assertIsNone(detect(message=msg), msg)

    def test_bare_number_is_not_a_budget(self):
        # No budget-context cue → a bare number must not register.
        c = detect(message="I have 2 kids and a dog")
        self.assertIsNone(c)

    def test_prior_messages_are_considered(self):
        # Budget mentioned earlier, contact given now: both count.
        c = detect(
            message="email me at a@b.com",
            prior_customer_messages=["my budget is $500,000"],
        )
        self.assertIsNotNone(c)
        self.assertIn("contact_info", c.triggers)
        self.assertIn("budget", c.triggers)


# =====================================================================
# §3.4.7 summarizer — shape equivalence with the folded behaviour.
# =====================================================================


class TestSummarizer(unittest.TestCase):
    def test_empty_session(self):
        self.assertEqual(summarize([]), "No messages in this session yet.")

    def test_recap_shape(self):
        s = summarize(
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
            ]
        )
        self.assertIn("Session summary (2 messages):", s)
        self.assertIn("USER: hello", s)
        self.assertIn("ASSISTANT: hi there", s)

    def test_long_message_truncated(self):
        s = summarize([{"role": "user", "content": "x" * 300}])
        self.assertIn("x" * 150 + "...", s)


# =====================================================================
# Arc 14 U5 — single-source de-dup: the loop's summary (summarizer.
# summarize, used by the finalizer) and the live chat-path summary
# (CognitionService.get_session_summary) MUST be byte-identical because
# both now delegate to ONE implementation (format_session_summary). This
# proves Finding 1's behaviour-equivalence: summary logic in one place.
# =====================================================================


class TestSummarySingleSourceEquivalence(unittest.TestCase):
    def _cases(self):
        return [
            [],
            [{"role": "user", "content": "hello"}],
            [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hey there"},
                {"role": "user", "content": "z" * 300},
            ],
            [{"role": "user", "content": ""}],
        ]

    def test_finalizer_summary_equals_chat_path_summary(self):
        from app.cognition.service import (
            CognitionService,
            format_session_summary,
        )

        svc = CognitionService()
        for messages in self._cases():
            loop_summary = summarize(messages)
            shared = format_session_summary(messages)
            # Loop summary == single source.
            self.assertEqual(loop_summary, shared)
            # Chat-path summary == single source (non-empty case routes
            # through the same function; empty case returns the same
            # literal the function returns).
            outcome = svc._handle_session_summary(messages=messages)
            self.assertEqual(outcome.output, shared)
            self.assertEqual(loop_summary, outcome.output)


# =====================================================================
# §3.4.4 + §3.4.7 — finalizer persists lead row + summary.
# =====================================================================


class TestFinalizerLeadCapture(unittest.TestCase):
    def _run(self, session, **overrides):
        fin = _finalizer(session)
        kwargs = dict(
            admin_id="admin_1",
            session_id="sess_1",
            luciel_instance_id=7,
            user_id="user_1",
            current_message="my budget is $800,000, email me at b@c.com",
            prior_customer_messages=[],
            assistant_reply="Great, I'll send matching listings.",
            inbound_channel="widget",
            escalation_fired=False,
            handoff_requested=False,
        )
        kwargs.update(overrides)
        return fin.finalize(**kwargs)

    def test_lead_row_shape_and_summary_persisted(self):
        session = _FakeSession()
        with patch(
            "app.repositories.admin_audit_repository.AdminAuditRepository",
            _RecordingAudit,
        ):
            result = self._run(session)

        self.assertTrue(result.lead_captured)
        self.assertIsNotNone(result.lead_id)
        self.assertTrue(session.committed)

        # One Lead row added with the structured fields populated.
        from app.models.lead import Lead

        leads = [r for r in session.added if isinstance(r, Lead)]
        self.assertEqual(len(leads), 1)
        row = leads[0]
        self.assertEqual(row.admin_id, "admin_1")
        self.assertEqual(row.luciel_instance_id, 7)
        self.assertEqual(row.session_id, "sess_1")
        self.assertEqual(row.contact_channel, "email")
        self.assertEqual(row.contact_identifier, "b@c.com")
        self.assertIsNotNone(row.key_facts)
        self.assertIsNotNone(row.next_step)
        # §3.4.7 — the structured summary persisted alongside the lead.
        self.assertIn("Session summary", row.summary)

    def test_lead_capture_writes_audit_row(self):
        session = _FakeSession()
        with patch(
            "app.repositories.admin_audit_repository.AdminAuditRepository",
            _RecordingAudit,
        ):
            self._run(session)

        from app.models.admin_audit_log import (
            ACTION_LEAD_CAPTURED,
            RESOURCE_LEAD,
        )

        audit = _RecordingAudit.instances[-1]
        self.assertEqual(len(audit.calls), 1)
        call = audit.calls[0]
        self.assertEqual(call["action"], ACTION_LEAD_CAPTURED)
        self.assertEqual(call["resource_type"], RESOURCE_LEAD)
        self.assertEqual(call["admin_id"], "admin_1")

    def test_idle_turn_captures_no_lead(self):
        session = _FakeSession()
        with patch(
            "app.repositories.admin_audit_repository.AdminAuditRepository",
            _RecordingAudit,
        ):
            result = self._run(
                session, current_message="hi thanks", prior_customer_messages=[]
            )
        self.assertFalse(result.lead_captured)
        self.assertIsNone(result.lead_id)
        # Summary still computed (always-on), but no row added.
        from app.models.lead import Lead

        self.assertEqual([r for r in session.added if isinstance(r, Lead)], [])
        self.assertIn("Session summary", result.summary)


# =====================================================================
# §3.4.6 live human handoff — bundle CONTENTS (transcript+summary+lead).
# =====================================================================


class TestHandoffBundle(unittest.TestCase):
    def test_bundle_carries_transcript_summary_and_lead(self):
        session = _FakeSession()
        fin = _finalizer(session)
        with patch(
            "app.repositories.admin_audit_repository.AdminAuditRepository",
            _RecordingAudit,
        ):
            result = fin.finalize(
                admin_id="admin_1",
                session_id="sess_1",
                luciel_instance_id=7,
                user_id="user_1",
                current_message="I want a human, my budget is $900,000",
                prior_customer_messages=["I'm looking at homes"],
                assistant_reply="Connecting you now.",
                inbound_channel="sms",
                escalation_fired=True,
                handoff_requested=True,
            )

        self.assertIsInstance(result.handoff, HandoffBundle)
        b = result.handoff
        # Transcript: prior turn + current message + assistant reply.
        self.assertTrue(any("looking at homes" in t["content"] for t in b.transcript))
        self.assertTrue(any("want a human" in t["content"] for t in b.transcript))
        self.assertTrue(
            any(t["role"] == "assistant" for t in b.transcript)
        )
        # Summary present.
        self.assertIn("Session summary", b.summary)
        # Captured lead present in the bundle.
        self.assertIsNotNone(b.lead)
        self.assertIn("budget", b.lead["triggers"])
        self.assertEqual(b.channel, "sms")

    def test_no_handoff_when_escalation_did_not_fire(self):
        session = _FakeSession()
        fin = _finalizer(session)
        with patch(
            "app.repositories.admin_audit_repository.AdminAuditRepository",
            _RecordingAudit,
        ):
            result = fin.finalize(
                admin_id="admin_1",
                session_id="sess_1",
                luciel_instance_id=7,
                user_id="user_1",
                current_message="email me at b@c.com",
                prior_customer_messages=[],
                assistant_reply="Will do.",
                inbound_channel="widget",
                escalation_fired=False,
                handoff_requested=True,
            )
        self.assertIsNone(result.handoff)

    def test_no_handoff_when_takeover_not_requested(self):
        session = _FakeSession()
        fin = _finalizer(session)
        with patch(
            "app.repositories.admin_audit_repository.AdminAuditRepository",
            _RecordingAudit,
        ):
            result = fin.finalize(
                admin_id="admin_1",
                session_id="sess_1",
                luciel_instance_id=7,
                user_id="user_1",
                current_message="email me at b@c.com",
                prior_customer_messages=[],
                assistant_reply="Will do.",
                inbound_channel="widget",
                escalation_fired=True,
                handoff_requested=False,
            )
        self.assertIsNone(result.handoff)


# =====================================================================
# Doctrine: finalization is a side-effect half — never crashes the turn.
# =====================================================================


class TestFinalizerNeverCrashes(unittest.TestCase):
    def test_db_open_failure_keeps_lead_facts_for_bundle(self):
        def _boom():
            raise RuntimeError("no db")

        fin = CognitionFinalizer(session_factory=_boom)
        result = fin.finalize(
            admin_id="admin_1",
            session_id="sess_1",
            luciel_instance_id=7,
            user_id="user_1",
            current_message="my budget is $800,000",
            prior_customer_messages=[],
            assistant_reply="ok",
            inbound_channel="widget",
            escalation_fired=True,
            handoff_requested=True,
        )
        # Row did not persist, but the bundle still carries the lead facts.
        self.assertFalse(result.lead_captured)
        self.assertIsNotNone(result.handoff)
        self.assertIsNotNone(result.handoff.lead)
        self.assertIn("budget", result.handoff.lead["triggers"])


if __name__ == "__main__":
    unittest.main()
