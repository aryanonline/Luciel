"""Unit 13d (§3.9) — analytics API route behavioral tests.

Calls the REAL ``get_analytics`` / ``export_analytics_csv`` route bodies
against live Postgres with a synthesised platform_admin request:
  * Free tier → GET returns ONLY the basic subset; CSV export → 403.
  * Pro tier  → GET returns the full surface; CSV export → 200 text/csv
    with a Content-Disposition filename and the seeded value in the body.
  * Bad period → 422; unknown export view → 422.

Tier is resolved the production way (resolve_billing_context): an admin
with no Subscription row resolves to Free; a Pro Subscription resolves to
Pro. The route never receives another tenant's data (proven separately in
tests/db/test_unit13d_analytics_isolation.py).
"""
from __future__ import annotations

import os
import types
import unittest
import uuid
from datetime import datetime, timezone

os.environ.setdefault("MODERATION_PROVIDER", "null")

_DB_URL = os.environ.get("DATABASE_URL", "")
_LIVE = _DB_URL.startswith("postgresql+psycopg://") or bool(
    os.environ.get("LUCIEL_LIVE_POSTGRES_URL")
)


def _request(*, admin_id: str):
    req = types.SimpleNamespace()
    req.state = types.SimpleNamespace(
        admin_id=admin_id,
        permissions=["platform_admin"],
        actor_user_id=None,
    )
    req.headers = {}
    req.client = types.SimpleNamespace(host="127.0.0.1")
    return req


@unittest.skipUnless(_LIVE, "Requires a live Postgres DATABASE_URL")
class TestUnit13dAnalyticsApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from app.db.session import SessionLocal

        cls.SessionLocal = SessionLocal

    def setUp(self) -> None:
        self.db = self.SessionLocal()
        self._admin_ids: list[str] = []
        self._user_ids: list = []

    def tearDown(self) -> None:
        self.db.rollback()
        self.db.close()
        self._purge()

    def _purge(self) -> None:
        from app.models.admin import Admin
        from app.models.instance import Instance
        from app.models.session import SessionModel
        from app.models.subscription import Subscription
        from app.models.user import User

        cleanup = self.SessionLocal()
        try:
            if self._admin_ids:
                cleanup.query(SessionModel).filter(
                    SessionModel.admin_id.in_(self._admin_ids)
                ).delete(synchronize_session=False)
                cleanup.query(Subscription).filter(
                    Subscription.admin_id.in_(self._admin_ids)
                ).delete(synchronize_session=False)
                cleanup.query(Instance).filter(
                    Instance.admin_id.in_(self._admin_ids)
                ).delete(synchronize_session=False)
                if self._user_ids:
                    cleanup.query(User).filter(
                        User.id.in_(self._user_ids)
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

        admin_id = f"u13da-{uuid.uuid4().hex[:10]}"
        self._admin_ids.append(admin_id)
        self.db.add(Admin(id=admin_id, name="u13da", tier="free", active=True))
        self.db.commit()
        return admin_id

    def _make_instance(self, *, admin_id: str) -> int:
        from app.models.instance import Instance

        inst = Instance(
            admin_id=admin_id,
            instance_slug=f"slug-{uuid.uuid4().hex[:8]}",
            display_name="api inst",
        )
        self.db.add(inst)
        self.db.commit()
        self.db.refresh(inst)
        return inst.id

    def _make_pro_subscription(self, *, admin_id: str) -> None:
        import uuid as _uuid

        from app.models.subscription import Subscription
        from app.models.user import User

        uid = _uuid.uuid4()
        self._user_ids.append(uid)
        self.db.add(
            User(
                id=uid,
                email=f"{uid.hex[:10]}@example.test",
                display_name="u13da buyer",
            )
        )
        self.db.commit()
        suffix = _uuid.uuid4().hex[:12]
        self.db.add(
            Subscription(
                admin_id=admin_id,
                user_id=uid,
                customer_email=f"{uid.hex[:10]}@example.test",
                stripe_customer_id=f"cus_{suffix}",
                stripe_subscription_id=f"sub_{suffix}",
                stripe_price_id=f"price_{suffix}",
                tier="pro",
                status="active",
                billing_cadence="monthly",
                instance_count_cap=1,
                active=True,
                current_period_start=datetime(2026, 6, 1, tzinfo=timezone.utc),
            )
        )
        self.db.commit()

    def _make_session(self, *, admin_id: str, instance_id: int) -> None:
        from app.models.session import SessionModel

        s = SessionModel(
            id=str(uuid.uuid4()),
            admin_id=admin_id,
            luciel_instance_id=instance_id,
            user_id="cust",
            channel="web",
        )
        self.db.add(s)
        self.db.commit()

    # -- tier-shaped GET --------------------------------------------------

    def test_get_analytics_free_basic_only(self):
        from app.api.v1.analytics import get_analytics

        admin = self._make_admin()
        inst = self._make_instance(admin_id=admin)
        self._make_session(admin_id=admin, instance_id=inst)

        resp = get_analytics(
            request=_request(admin_id=admin), db=self.db, period="last_30d"
        )
        self.assertEqual(resp["tier"], "free")
        self.assertIn("conversations", resp)
        self.assertIn("leads", resp)
        self.assertIn("budget_utilization", resp)
        self.assertNotIn("conversion", resp)
        self.assertNotIn("channel_mix", resp)

    def test_get_analytics_pro_full_surface(self):
        from app.api.v1.analytics import get_analytics

        admin = self._make_admin()
        self._make_pro_subscription(admin_id=admin)
        inst = self._make_instance(admin_id=admin)
        self._make_session(admin_id=admin, instance_id=inst)

        resp = get_analytics(
            request=_request(admin_id=admin), db=self.db, period="last_30d"
        )
        self.assertEqual(resp["tier"], "pro")
        for key in (
            "conversion",
            "channel_mix",
            "escalations_by_signal",
            "escalation_first_response",
            "appointments_booked",
            "top_knowledge_sources",
            "busiest_times",
        ):
            self.assertIn(key, resp)

    def test_get_analytics_bad_period_422(self):
        from fastapi import HTTPException

        from app.api.v1.analytics import get_analytics

        admin = self._make_admin()
        with self.assertRaises(HTTPException) as ctx:
            get_analytics(
                request=_request(admin_id=admin), db=self.db, period="garbage"
            )
        self.assertEqual(ctx.exception.status_code, 422)

    # -- CSV export -------------------------------------------------------

    def test_export_free_forbidden_403(self):
        from fastapi import HTTPException

        from app.api.v1.analytics import export_analytics_csv

        admin = self._make_admin()
        with self.assertRaises(HTTPException) as ctx:
            export_analytics_csv(
                request=_request(admin_id=admin), db=self.db,
                view="conversations", period="last_30d",
            )
        self.assertEqual(ctx.exception.status_code, 403)

    def test_export_pro_returns_csv(self):
        from app.api.v1.analytics import export_analytics_csv

        admin = self._make_admin()
        self._make_pro_subscription(admin_id=admin)
        inst = self._make_instance(admin_id=admin)
        self._make_session(admin_id=admin, instance_id=inst)
        self._make_session(admin_id=admin, instance_id=inst)

        resp = export_analytics_csv(
            request=_request(admin_id=admin), db=self.db,
            view="conversations", period="last_30d",
        )
        self.assertEqual(resp.media_type, "text/csv")
        self.assertIn(
            "attachment", resp.headers["Content-Disposition"]
        )
        import asyncio

        async def _collect() -> str:
            out = []
            async for chunk in resp.body_iterator:
                out.append(
                    chunk.decode() if isinstance(chunk, bytes) else chunk
                )
            return "".join(out)

        body = asyncio.run(_collect())
        # conversations view flattens to metric,value rows including total=2.
        self.assertIn("metric,value", body)
        self.assertIn("total", body)
        self.assertIn("2", body)

    def test_export_unknown_view_422(self):
        from fastapi import HTTPException

        from app.api.v1.analytics import export_analytics_csv

        admin = self._make_admin()
        self._make_pro_subscription(admin_id=admin)
        with self.assertRaises(HTTPException) as ctx:
            export_analytics_csv(
                request=_request(admin_id=admin), db=self.db,
                view="not_a_view", period="last_30d",
            )
        self.assertEqual(ctx.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
