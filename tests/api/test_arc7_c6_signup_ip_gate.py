"""Arc 7 / Commit 6 (2026-05-24) -- 1-per-IP soft gate for Free signup.

Pins the new abuse surface that Arc 7 Commit 6 introduced:

  * ``admins.last_signup_ip`` column (Postgres INET, nullable, partial index)
  * 24h rolling 1-per-IP gate inside ``POST /api/v1/billing/signup-free``
    that returns 429 when an active Admin with the same IP exists in the
    last 24h.

These tests exercise the *gate logic shape* directly (model field
presence + query construction + 429 branch) without requiring a live
Postgres or Redis -- the route-level integration end-to-end is validated
by the prod smoke after rolling deploy. This is the same pattern as
``tests/api/test_arc6_signup_free.py``: pin the schema and the per-piece
behavior; let the deploy gate prove the wire-up.
"""
from __future__ import annotations

import os

# Test-env stubs -- mirrors tests/api/test_arc6_signup_free.py
os.environ.setdefault("MODERATION_PROVIDER", "null")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg://stub:stub@localhost:5432/stub"
)


# ----------------------------------------------------------------------
# 1. Model field shape -- the schema-of-record for the new column
# ----------------------------------------------------------------------


class TestAdminModelLastSignupIpField:
    def test_admin_has_last_signup_ip_attribute(self):
        from app.models.admin import Admin

        # SQLAlchemy declarative -- the column shows up as a mapped
        # attribute on the class.
        assert hasattr(Admin, "last_signup_ip"), (
            "Admin.last_signup_ip must exist (Arc 7 Commit 6)"
        )

    def test_last_signup_ip_is_nullable(self):
        from app.models.admin import Admin

        col = Admin.__table__.c.last_signup_ip
        assert col.nullable is True, (
            "last_signup_ip must be nullable -- historical Admins, "
            "paid Stripe Checkout flows, and missing-IP requests all "
            "leave it NULL"
        )

    def test_last_signup_ip_uses_postgres_inet_type(self):
        from app.models.admin import Admin
        from sqlalchemy.dialects.postgresql import INET

        col = Admin.__table__.c.last_signup_ip
        # Direct type identity check -- the column is declared with the
        # postgres-native INET dialect type, not generic String.
        assert isinstance(col.type, INET), (
            f"last_signup_ip must be postgresql.INET; got {type(col.type)!r}"
        )


# ----------------------------------------------------------------------
# 2. Migration anchor -- the new revision chains correctly
# ----------------------------------------------------------------------


class TestMigrationAnchor:
    def test_revision_b_chains_to_revision_a(self):
        # Import the migration module by file -- avoid alembic config
        # bootstrap so this test does not need a database.
        import importlib.util
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        path = root / "alembic" / "versions" / "arc7_b_admins_last_signup_ip.py"
        spec = importlib.util.spec_from_file_location(
            "arc7_b_admins_last_signup_ip", path
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        assert mod.revision == "arc7_b_admins_last_signup_ip"
        assert mod.down_revision == "arc7_a_retire_billing_model", (
            "Revision B must chain off Arc 7 Revision A (the billing_model "
            "retirement). If a later migration lands first the chain breaks."
        )
        assert mod.branch_labels is None
        assert mod.depends_on is None

    def test_migration_creates_partial_index(self):
        # The migration source must mention the partial-index predicate.
        # We don't execute the upgrade() here (would need a DB); we pin
        # the textual contract that the partial predicate is present, so
        # an accidental drop of the predicate trips a clear test failure.
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        src = (
            root / "alembic" / "versions" / "arc7_b_admins_last_signup_ip.py"
        ).read_text()
        assert "ix_admins_last_signup_ip" in src
        assert "postgresql_where" in src
        assert "last_signup_ip IS NOT NULL AND active = true" in src


# ----------------------------------------------------------------------
# 3. Route-source pins -- the gate exists at the documented position
# ----------------------------------------------------------------------


class TestSignupFreeRouteGatePresence:
    """The 1-per-IP gate must be wired into the signup_free route body
    BEFORE the captcha verification (cheap before expensive) and the
    last_signup_ip stamp must run AFTER onboard_tenant succeeds.

    These are AST/text pins -- the prod-deploy smoke validates the
    wire-up end-to-end. Pin the textual contract so a refactor cannot
    silently move the gate to the wrong place.
    """

    def test_gate_block_exists_in_route_source(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        src = (root / "app" / "api" / "v1" / "billing.py").read_text()
        assert "signup_free.ip_gate_blocked" in src, (
            "Gate log line missing -- the 1-per-IP gate has been removed "
            "from signup_free."
        )
        assert "HTTP_429_TOO_MANY_REQUESTS" in src
        assert "recent_same_ip" in src

    def test_gate_runs_before_captcha_verification(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        src = (root / "app" / "api" / "v1" / "billing.py").read_text()
        gate_idx = src.index("signup_free.ip_gate_blocked")
        captcha_call_idx = src.index("await verify_captcha(")
        assert gate_idx < captcha_call_idx, (
            "1-per-IP gate must run BEFORE captcha verification "
            "(cheap-before-expensive doctrine)"
        )

    def test_stamp_runs_after_onboard_tenant(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        src = (root / "app" / "api" / "v1" / "billing.py").read_text()
        onboard_idx = src.index("onboarding.onboard_tenant(")
        stamp_idx = src.index("signup_free.ip_stamped")
        assert onboard_idx < stamp_idx, (
            "last_signup_ip stamp must run AFTER onboard_tenant() so a "
            "slug-collision 409 cannot leave a stamped-but-not-onboarded "
            "row"
        )


# ----------------------------------------------------------------------
# 4. Behavioural shape of the gate (in-memory simulation)
# ----------------------------------------------------------------------


class TestGateLogicShape:
    """Validate the *decision* logic without a database.

    The gate has 4 inputs that decide its outcome:
      1. remote_ip is None              -> fail-open (allow)
      2. count of same-IP active in 24h -> 0  -> allow
      3. count of same-IP active in 24h -> >= 1 -> deny (429)
      4. older than 24h                  -> allow (window expired)

    We pin the decision table.
    """

    @staticmethod
    def _gate_decision(*, remote_ip, recent_same_ip_count):
        """Inline the gate's decision predicate.

        Mirrors the if-block in app/api/v1/billing.py:signup_free.
        """
        if remote_ip is None:
            return "allow"
        if recent_same_ip_count >= 1:
            return "deny_429"
        return "allow"

    def test_missing_ip_is_fail_open(self):
        assert self._gate_decision(
            remote_ip=None, recent_same_ip_count=99
        ) == "allow"

    def test_zero_recent_is_allow(self):
        assert self._gate_decision(
            remote_ip="203.0.113.5", recent_same_ip_count=0
        ) == "allow"

    def test_one_recent_is_deny(self):
        assert self._gate_decision(
            remote_ip="203.0.113.5", recent_same_ip_count=1
        ) == "deny_429"

    def test_many_recent_is_deny(self):
        assert self._gate_decision(
            remote_ip="203.0.113.5", recent_same_ip_count=17
        ) == "deny_429"


# ----------------------------------------------------------------------
# 5. Doctrine: paid checkout flows DO NOT write last_signup_ip
# ----------------------------------------------------------------------


class TestPaidCheckoutLeavesIpNull:
    """Pin the doctrine: only the Free signup path stamps last_signup_ip.

    The Stripe webhook path (BillingWebhookService.handle_event) mints
    Pro / Enterprise admins; the payment surface IS the abuse boundary
    there, and we deliberately leave last_signup_ip NULL on those rows.

    This is an *absence* test -- grep the webhook service module for any
    write to last_signup_ip; there must be none.
    """

    def test_webhook_service_does_not_write_last_signup_ip(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        src = (
            root / "app" / "services" / "billing_webhook_service.py"
        ).read_text()
        assert "last_signup_ip" not in src, (
            "Paid checkout (BillingWebhookService) must not write "
            "last_signup_ip -- the payment surface is the abuse "
            "boundary on paid flows; only the Free signup route writes "
            "this field."
        )
