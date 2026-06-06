"""Unit 13e (§3.4.8/§3.4.9) — session-key lookup isolation (NON-NEGOTIABLE).

The §3.4.8 session key is (instance_id, participant_id, channel) where
participant_id = resolved_lead_id. Two tenants can independently resolve
their OWN leads to the SAME participant id string (the id space is
per-tenant: it is an Admin's lead). A session-key lookup MUST therefore
never cross tenants — tenant A asking for (instance, lead, channel) must
never return tenant B's session even when the participant ids collide.

Also pins the §3.4.9 HARD RULE: a NULL resolved_lead_id (anonymous widget)
never matches another session's NULL as "same participant".

This lives in the isolation suite (tests/db) and MUST pass. It adds a new
cross-tenant exclusion guarantee for the §3.4.8 session-key surface and
weakens no existing isolation test.
"""
from __future__ import annotations

import os
import unittest
import uuid

os.environ.setdefault("MODERATION_PROVIDER", "null")

_DB_URL = os.environ.get("DATABASE_URL", "")
_LIVE = _DB_URL.startswith("postgresql+psycopg://") or bool(
    os.environ.get("LUCIEL_LIVE_POSTGRES_URL")
)


@unittest.skipUnless(_LIVE, "Requires a live Postgres DATABASE_URL")
class TestUnit13eSessionKeyIsolation(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from app.db.session import SessionLocal

        cls.SessionLocal = SessionLocal

    def setUp(self) -> None:
        self.db = self.SessionLocal()
        self._admin_ids: list[str] = []

    def tearDown(self) -> None:
        self.db.rollback()
        self.db.close()
        self._purge()

    def _purge(self) -> None:
        from app.models.admin import Admin
        from app.models.instance import Instance
        from app.models.session import SessionModel

        cleanup = self.SessionLocal()
        try:
            if self._admin_ids:
                cleanup.query(SessionModel).filter(
                    SessionModel.admin_id.in_(self._admin_ids)
                ).delete(synchronize_session=False)
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

    def _make_admin(self) -> str:
        from app.models.admin import Admin

        admin_id = f"u13e-{uuid.uuid4().hex[:10]}"
        self._admin_ids.append(admin_id)
        self.db.add(Admin(id=admin_id, name="u13e", tier="pro", active=True))
        self.db.commit()
        return admin_id

    def _make_instance(self, *, admin_id: str) -> int:
        from app.models.instance import Instance

        inst = Instance(
            admin_id=admin_id,
            instance_slug=f"slug-{uuid.uuid4().hex[:8]}",
            display_name="u13e inst",
        )
        self.db.add(inst)
        self.db.commit()
        self.db.refresh(inst)
        return inst.id

    def _seed_session(
        self, *, admin_id: str, instance_id: int,
        resolved_lead_id: str | None, channel: str,
    ) -> str:
        from app.models.session import SessionModel

        sid = str(uuid.uuid4())
        self.db.add(
            SessionModel(
                id=sid,
                admin_id=admin_id,
                luciel_instance_id=instance_id,
                user_id=resolved_lead_id,
                resolved_lead_id=resolved_lead_id,
                channel=channel,
            )
        )
        self.db.commit()
        return sid

    # -- the isolation guarantee -----------------------------------------

    def test_session_key_lookup_never_crosses_tenants(self):
        from app.db.tenant_scope import bind_tenant_scope
        from app.repositories.session_repository import SessionRepository

        admin_a = self._make_admin()
        admin_b = self._make_admin()
        inst_a = self._make_instance(admin_id=admin_a)
        inst_b = self._make_instance(admin_id=admin_b)

        # Same participant id string + same channel under both tenants —
        # an intentional collision the lookup must NOT bridge.
        shared_lead = "lead-collision-xyz"
        sid_a = self._seed_session(
            admin_id=admin_a, instance_id=inst_a,
            resolved_lead_id=shared_lead, channel="web",
        )
        sid_b = self._seed_session(
            admin_id=admin_b, instance_id=inst_b,
            resolved_lead_id=shared_lead, channel="web",
        )

        # Tenant A looks up the session key under A's RLS scope.
        scoped = self.SessionLocal()
        try:
            with bind_tenant_scope(admin_id=admin_a, instance_id=inst_a):
                found = SessionRepository(scoped).find_session_by_key(
                    luciel_instance_id=inst_a,
                    resolved_lead_id=shared_lead,
                    channel="web",
                    admin_id=admin_a,
                )
        finally:
            scoped.close()

        self.assertIsNotNone(found)
        self.assertEqual(found.id, sid_a)
        self.assertNotEqual(found.id, sid_b)
        self.assertEqual(found.admin_id, admin_a)

    def test_null_resolved_lead_never_matches(self):
        """§3.4.9 HARD RULE — NULL never matches another NULL."""
        from app.repositories.session_repository import SessionRepository

        admin_a = self._make_admin()
        inst_a = self._make_instance(admin_id=admin_a)

        # Two anonymous sessions (NULL participant) on the same instance +
        # channel. They must NEVER be treated as the same participant.
        self._seed_session(
            admin_id=admin_a, instance_id=inst_a,
            resolved_lead_id=None, channel="web",
        )
        self._seed_session(
            admin_id=admin_a, instance_id=inst_a,
            resolved_lead_id=None, channel="web",
        )

        found = SessionRepository(self.db).find_session_by_key(
            luciel_instance_id=inst_a,
            resolved_lead_id=None,
            channel="web",
            admin_id=admin_a,
        )
        self.assertIsNone(found)


if __name__ == "__main__":
    unittest.main()
