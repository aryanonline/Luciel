"""Arc 13 Slice 2 — channel-config admin API route tests (real Postgres).

Exercises the ``/admin/instances/{id}/channels`` route bodies directly
with a synthesised Request (mirroring the arc12 route-test pattern) and
a real-Postgres ORM session so the provisioning mutations + audit
hash-chain run as in prod.

DOD-critical assertions:
  * Free-tier SMS enable is REJECTED at the API boundary with HTTP 403
    and the structured ``channel_not_available_on_tier`` body
    (error / channel / tier / message / upgrade_required).
  * Pro-tier SMS enable PROVISIONS a dedicated number (enabled_channels
    gains 'sms', instance.sms_provisioned_number set, sms route live).
  * SMS disable DEPROVISIONS (number cleared, route revoked).
  * GET reflects per-channel state + tier availability.
  * Email enable on Free is likewise rejected; on Pro flips the flag.

A platform_admin caller is used so the test isolates the tier-gate +
provisioning logic from the role-resolution machinery (covered by the
Arc 12b permission tests). The tier reject is independent of the caller
role — it keys on the Admin's tier, not the caller's permissions.

Skipped unless DATABASE_URL points at a real Postgres.
"""
from __future__ import annotations

import os
import types
import unittest
import uuid

os.environ.setdefault("MODERATION_PROVIDER", "null")
os.environ.setdefault("OPENAI_API_KEY", "dummy")

_DB_URL = os.environ.get("DATABASE_URL", "")
_LIVE = _DB_URL.startswith("postgresql+psycopg://") or bool(
    os.environ.get("LUCIEL_LIVE_POSTGRES_URL")
)


def _request(*, admin_id: str):
    """Synthesise a platform_admin Request bound to ``admin_id``."""
    req = types.SimpleNamespace()
    req.state = types.SimpleNamespace(
        admin_id=admin_id,
        permissions=["platform_admin"],
        actor_user_id=None,
    )
    # AuditContext.from_request reads headers/client; give it the minimum.
    req.headers = {}
    req.client = types.SimpleNamespace(host="127.0.0.1")
    return req


@unittest.skipUnless(
    _LIVE,
    "Requires DATABASE_URL=postgresql+psycopg://... or LUCIEL_LIVE_POSTGRES_URL",
)
class TestArc13ChannelRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from app.db.session import SessionLocal

        cls.SessionLocal = SessionLocal

    def _make_admin_instance(self, tier: str):
        from app.models.admin import Admin
        from app.models.instance import Instance

        admin_id = f"arc13rt-{uuid.uuid4().hex[:10]}"
        self._admin_ids.append(admin_id)
        self.db.add(Admin(id=admin_id, name="arc13 rt", tier=tier, active=True))
        self.db.flush()
        inst = Instance(
            admin_id=admin_id,
            instance_slug=f"i-{uuid.uuid4().hex[:8]}",
            display_name="arc13 rt instance",
        )
        self.db.add(inst)
        self.db.flush()
        return admin_id, inst

    def setUp(self) -> None:
        self.db = self.SessionLocal()
        # Track admins this test creates. The route handlers under test
        # call db.commit(), so the admin/instance/ChannelRoute rows they
        # write are NOT undone by a tearDown rollback — they persist in the
        # real DB. Left uncleaned, the committed sms ChannelRoute rows
        # accumulate and break this suite's ``route_value == number``
        # ``.one()`` queries on a second run. tearDown purges them in a
        # fresh session (audit rows are append-only and intentionally kept).
        self._admin_ids: list[str] = []

    def tearDown(self) -> None:
        self.db.rollback()
        self.db.close()
        if self._admin_ids:
            self._purge(self._admin_ids)

    def _purge(self, admin_ids: list[str]) -> None:
        from app.models.channel_route import ChannelRoute
        from app.models.instance import Instance

        cleanup = self.SessionLocal()
        try:
            cleanup.query(ChannelRoute).filter(
                ChannelRoute.admin_id.in_(admin_ids)
            ).delete(synchronize_session=False)
            cleanup.query(Instance).filter(
                Instance.admin_id.in_(admin_ids)
            ).delete(synchronize_session=False)
            cleanup.commit()
        except Exception:
            cleanup.rollback()
        finally:
            cleanup.close()

    def _instance_service(self):
        from app.services.instance_service import InstanceService

        return InstanceService(self.db)

    def _audit_ctx(self):
        from app.repositories.admin_audit_repository import AuditContext

        return AuditContext.system("test")

    # -----------------------------------------------------------------
    # Free-tier SMS reject — DOD-critical.
    # -----------------------------------------------------------------

    def test_free_sms_enable_rejected_at_api_boundary(self):
        from fastapi import HTTPException

        from app.api.v1.admin_channels import (
            ChannelToggleRequest,
            set_sms_channel,
        )
        from app.policy.entitlements import TIER_FREE

        admin_id, inst = self._make_admin_instance(TIER_FREE)
        with self.assertRaises(HTTPException) as caught:
            set_sms_channel(
                request=_request(admin_id=admin_id),
                instance_id=inst.id,
                body=ChannelToggleRequest(enabled=True),
                db=self.db,
                instance_service=self._instance_service(),
                audit_ctx=self._audit_ctx(),
            )
        exc = caught.exception
        self.assertEqual(exc.status_code, 403)
        self.assertIsInstance(exc.detail, dict)
        self.assertEqual(exc.detail["error"], "channel_not_available_on_tier")
        self.assertEqual(exc.detail["channel"], "sms")
        self.assertEqual(exc.detail["tier"], "free")
        self.assertTrue(exc.detail["upgrade_required"])
        # No number was provisioned.
        self.db.refresh(inst)
        self.assertIsNone(inst.sms_provisioned_number)

    # -----------------------------------------------------------------
    # Pro-tier SMS provision + deprovision.
    # -----------------------------------------------------------------

    def test_pro_sms_enable_provisions_then_disable_deprovisions(self):
        from app.api.v1.admin_channels import (
            ChannelToggleRequest,
            set_sms_channel,
        )
        from app.models.channel_route import CHANNEL_SMS, ChannelRoute
        from app.policy.entitlements import TIER_PRO

        admin_id, inst = self._make_admin_instance(TIER_PRO)

        resp = set_sms_channel(
            request=_request(admin_id=admin_id),
            instance_id=inst.id,
            body=ChannelToggleRequest(enabled=True),
            db=self.db,
            instance_service=self._instance_service(),
            audit_ctx=self._audit_ctx(),
        )
        self.assertIsNotNone(resp.sms_provisioned_number)
        self.assertEqual(resp.sms_number_mode, "dedicated")
        sms_view = next(c for c in resp.channels if c.channel == "sms")
        self.assertTrue(sms_view.enabled)
        self.assertTrue(sms_view.tier_available)

        number = resp.sms_provisioned_number
        live = (
            self.db.query(ChannelRoute)
            .filter(
                ChannelRoute.channel == CHANNEL_SMS,
                ChannelRoute.route_value == number,
                ChannelRoute.revoked_at.is_(None),
            )
            .one()
        )
        self.assertEqual(live.admin_id, admin_id)

        # Now disable → deprovision.
        resp2 = set_sms_channel(
            request=_request(admin_id=admin_id),
            instance_id=inst.id,
            body=ChannelToggleRequest(enabled=False),
            db=self.db,
            instance_service=self._instance_service(),
            audit_ctx=self._audit_ctx(),
        )
        self.assertIsNone(resp2.sms_provisioned_number)
        sms_view2 = next(c for c in resp2.channels if c.channel == "sms")
        self.assertFalse(sms_view2.enabled)

        revoked = (
            self.db.query(ChannelRoute)
            .filter(ChannelRoute.route_value == number)
            .one()
        )
        self.assertIsNotNone(revoked.revoked_at)

    # -----------------------------------------------------------------
    # GET state.
    # -----------------------------------------------------------------

    def test_get_channel_state_reflects_tier_and_enabled(self):
        from app.api.v1.admin_channels import get_channel_state
        from app.policy.entitlements import TIER_PRO

        admin_id, inst = self._make_admin_instance(TIER_PRO)
        resp = get_channel_state(
            request=_request(admin_id=admin_id),
            instance_id=inst.id,
            db=self.db,
            instance_service=self._instance_service(),
        )
        by_ch = {c.channel: c for c in resp.channels}
        self.assertTrue(by_ch["widget"].enabled)  # structural floor
        self.assertTrue(by_ch["widget"].tier_available)
        self.assertTrue(by_ch["sms"].tier_available)  # Pro
        self.assertFalse(by_ch["sms"].enabled)  # not yet provisioned
        self.assertTrue(by_ch["email"].tier_available)

    # -----------------------------------------------------------------
    # Email toggle.
    # -----------------------------------------------------------------

    def test_free_email_enable_rejected(self):
        from fastapi import HTTPException

        from app.api.v1.admin_channels import (
            ChannelToggleRequest,
            set_email_channel,
        )
        from app.policy.entitlements import TIER_FREE

        admin_id, inst = self._make_admin_instance(TIER_FREE)
        with self.assertRaises(HTTPException) as caught:
            set_email_channel(
                request=_request(admin_id=admin_id),
                instance_id=inst.id,
                body=ChannelToggleRequest(enabled=True),
                db=self.db,
                instance_service=self._instance_service(),
                audit_ctx=self._audit_ctx(),
            )
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(
            caught.exception.detail["error"], "channel_not_available_on_tier"
        )

    def test_pro_email_enable_sets_flag(self):
        from app.api.v1.admin_channels import (
            ChannelToggleRequest,
            set_email_channel,
        )
        from app.policy.entitlements import TIER_PRO

        admin_id, inst = self._make_admin_instance(TIER_PRO)
        resp = set_email_channel(
            request=_request(admin_id=admin_id),
            instance_id=inst.id,
            body=ChannelToggleRequest(enabled=True),
            db=self.db,
            instance_service=self._instance_service(),
            audit_ctx=self._audit_ctx(),
        )
        email_view = next(c for c in resp.channels if c.channel == "email")
        self.assertTrue(email_view.enabled)


if __name__ == "__main__":
    unittest.main()
