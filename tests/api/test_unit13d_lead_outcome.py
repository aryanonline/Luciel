"""Unit 13d (§3.9) — lead-outcome admin endpoint behavioral tests.

Calls the REAL ``set_lead_outcome`` route body against real Postgres
(mirrors tests/api/test_unit8_handoff_behavioral.py): asserts the DB
transition, the ACTION_LEAD_OUTCOME_SET audit row, the 404 cross-tenant
fence, and the 422 on a bad enum (FastAPI validates the str-Enum body,
so we assert the Pydantic model rejects the bad value).

A platform_admin caller is synthesised so these isolate the outcome
LOGIC from role resolution (role gating covered elsewhere).
"""
from __future__ import annotations

import os
import types
import unittest
import uuid

os.environ.setdefault("MODERATION_PROVIDER", "null")

_DB_URL = os.environ.get("DATABASE_URL", "")
_LIVE = _DB_URL.startswith("postgresql+psycopg://") or bool(
    os.environ.get("LUCIEL_LIVE_POSTGRES_URL")
)


def _request(*, admin_id: str, actor_user_id=None):
    req = types.SimpleNamespace()
    req.state = types.SimpleNamespace(
        admin_id=admin_id,
        permissions=["platform_admin"],
        actor_user_id=actor_user_id,
    )
    req.headers = {}
    req.client = types.SimpleNamespace(host="127.0.0.1")
    return req


@unittest.skipUnless(_LIVE, "Requires a live Postgres DATABASE_URL")
class TestUnit13dLeadOutcome(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from app.db.session import SessionLocal

        cls.SessionLocal = SessionLocal

    def setUp(self) -> None:
        self.db = self.SessionLocal()
        self._admin_ids: list[str] = []
        self._lead_ids: list[int] = []

    def tearDown(self) -> None:
        self.db.rollback()
        self.db.close()
        self._purge()

    def _purge(self) -> None:
        from app.models.admin import Admin
        from app.models.instance import Instance
        from app.models.lead import Lead

        cleanup = self.SessionLocal()
        try:
            if self._lead_ids:
                cleanup.query(Lead).filter(
                    Lead.id.in_(self._lead_ids)
                ).delete(synchronize_session=False)
            if self._admin_ids:
                cleanup.query(Instance).filter(
                    Instance.admin_id.in_(self._admin_ids)
                ).delete(synchronize_session=False)
                cleanup.query(Admin).filter(
                    Admin.id.in_(self._admin_ids)
                ).delete(synchronize_session=False)
            cleanup.commit()
        except Exception:
            cleanup.rollback()
        finally:
            cleanup.close()

    def _make_admin(self, tier: str = "pro") -> str:
        from app.models.admin import Admin

        admin_id = f"u13d-{uuid.uuid4().hex[:10]}"
        self._admin_ids.append(admin_id)
        self.db.add(Admin(id=admin_id, name="u13d", tier=tier, active=True))
        self.db.commit()
        return admin_id

    def _make_lead(self, *, admin_id: str, outcome=None):
        from app.models.lead import Lead

        row = Lead(
            admin_id=admin_id,
            session_id=str(uuid.uuid4()),
            user_id="cust-x",
            intent="wants a viewing",
            outcome=outcome,
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        self._lead_ids.append(row.id)
        return row

    def _audit_ctx(self):
        from app.repositories.admin_audit_repository import AuditContext

        return AuditContext.system("u13d-test")

    def _audit_rows(self, *, session_id: str):
        from app.models.admin_audit_log import (
            ACTION_LEAD_OUTCOME_SET,
            AdminAuditLog,
        )

        return (
            self.db.query(AdminAuditLog)
            .filter(
                AdminAuditLog.action == ACTION_LEAD_OUTCOME_SET,
                AdminAuditLog.resource_natural_id == session_id,
            )
            .all()
        )

    # -- happy path + audit ----------------------------------------------

    def test_set_outcome_happy_path_and_audit(self):
        from app.api.v1.admin_leads import (
            LeadOutcome,
            LeadOutcomeRequest,
            set_lead_outcome,
        )

        admin_id = self._make_admin()
        lead = self._make_lead(admin_id=admin_id)

        resp = set_lead_outcome(
            request=_request(admin_id=admin_id),
            lead_id=lead.id,
            body=LeadOutcomeRequest(outcome=LeadOutcome.converted),
            db=self.db,
            audit_ctx=self._audit_ctx(),
        )

        self.assertEqual(resp.outcome, "converted")
        self.db.refresh(lead)
        self.assertEqual(lead.outcome, "converted")

        rows = self._audit_rows(session_id=lead.session_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].after_json["outcome"], "converted")
        self.assertIsNone(rows[0].before_json["outcome"])
        self.assertEqual(rows[0].admin_id, admin_id)
        self.assertEqual(rows[0].resource_pk, lead.id)

    def test_set_outcome_404_cross_tenant(self):
        from fastapi import HTTPException

        from app.api.v1.admin_leads import (
            LeadOutcome,
            LeadOutcomeRequest,
            set_lead_outcome,
        )

        owner = self._make_admin()
        other = self._make_admin()
        lead = self._make_lead(admin_id=owner)

        with self.assertRaises(HTTPException) as ctx:
            set_lead_outcome(
                request=_request(admin_id=other),
                lead_id=lead.id,
                body=LeadOutcomeRequest(outcome=LeadOutcome.lost),
                db=self.db,
                audit_ctx=self._audit_ctx(),
            )
        self.assertEqual(ctx.exception.status_code, 404)
        # The foreign caller's attempt must NOT have changed the lead.
        self.db.refresh(lead)
        self.assertIsNone(lead.outcome)

    def test_bad_enum_rejected_422(self):
        """The str-Enum body rejects an out-of-vocabulary outcome.

        FastAPI returns 422 for this in HTTP; at the model layer it is a
        pydantic ValidationError, which is what produces the 422.
        """
        from pydantic import ValidationError

        from app.api.v1.admin_leads import LeadOutcomeRequest

        with self.assertRaises(ValidationError):
            LeadOutcomeRequest(outcome="bogus")

    def test_set_outcome_idempotent_overwrite(self):
        from app.api.v1.admin_leads import (
            LeadOutcome,
            LeadOutcomeRequest,
            set_lead_outcome,
        )

        admin_id = self._make_admin()
        lead = self._make_lead(admin_id=admin_id, outcome="in_progress")

        resp = set_lead_outcome(
            request=_request(admin_id=admin_id),
            lead_id=lead.id,
            body=LeadOutcomeRequest(outcome=LeadOutcome.converted),
            db=self.db,
            audit_ctx=self._audit_ctx(),
        )
        self.assertEqual(resp.outcome, "converted")
        rows = self._audit_rows(session_id=lead.session_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].before_json["outcome"], "in_progress")


if __name__ == "__main__":
    unittest.main()
