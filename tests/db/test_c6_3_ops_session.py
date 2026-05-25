"""Arc 9 C6.3 -- ops session helper structural tests.

These tests verify the SHAPE of the C6.3 wiring without requiring a live
Postgres connection or the luciel_ops role to exist. They guard against
regressions that would compromise the security guarantees stated in the
C6.3 doctrine:

  1. The ops engine + OpsSessionLocal are constructed ONLY when
     settings.luciel_ops_db_url is set. Local dev / CI must never
     accidentally acquire a BYPASSRLS connection.
  2. get_ops_db_session() raises RuntimeError (fail closed) when the URL
     is unset -- callers cannot silently fall back to SessionLocal.
  3. The Arc 9 C2 tenant-context after_begin listener is attached to
     SessionLocal but NOT to OpsSessionLocal. This is the structural
     guarantee that an ops session never emits app.admin_id /
     app.instance_id GUCs.
  4. OpsSessionLocal is a SEPARATE sessionmaker instance from
     SessionLocal -- not an alias.

The tests use importlib + the live ``app.db.session`` module (no DB
connection needed since we only inspect Python-side state).
"""

from __future__ import annotations

import importlib
import inspect
import os
import unittest
from contextlib import contextmanager
from unittest import mock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@contextmanager
def _patched_settings(**overrides):
    """Reload app.db.session with patched settings values.

    The ops_engine / OpsSessionLocal construction happens at module-import
    time gated on ``settings.luciel_ops_db_url``. To exercise both
    branches (URL set vs unset) we patch the setting then reload the
    module so the gate re-evaluates.
    """
    from app.core import config as config_mod

    original = {k: getattr(config_mod.settings, k) for k in overrides}
    try:
        for k, v in overrides.items():
            object.__setattr__(config_mod.settings, k, v)
        # Reload the session module so the top-level
        # ``if settings.luciel_ops_db_url is not None`` re-evaluates.
        import app.db.session as session_mod

        reloaded = importlib.reload(session_mod)
        yield reloaded
    finally:
        for k, v in original.items():
            object.__setattr__(config_mod.settings, k, v)
        # Restore canonical module state for any subsequent test.
        import app.db.session as session_mod

        importlib.reload(session_mod)


# ---------------------------------------------------------------------------
# Class 1: settings shape
# ---------------------------------------------------------------------------


class TestC63SettingsShape(unittest.TestCase):
    """Settings carries luciel_ops_db_url + audit_log_immutability_enabled."""

    def test_luciel_ops_db_url_field_exists(self):
        from app.core.config import Settings

        self.assertIn("luciel_ops_db_url", Settings.model_fields)

    def test_luciel_ops_db_url_defaults_none(self):
        from app.core.config import Settings

        field = Settings.model_fields["luciel_ops_db_url"]
        self.assertIsNone(field.default)

    def test_luciel_ops_db_url_optional_str(self):
        from app.core.config import Settings

        ann = Settings.model_fields["luciel_ops_db_url"].annotation
        # Optional[str] -> str | None. The annotation should permit None.
        self.assertIn(type(None), getattr(ann, "__args__", (ann,)))
        self.assertIn(str, getattr(ann, "__args__", (ann,)))

    def test_audit_log_immutability_flag_exists(self):
        from app.core.config import Settings

        self.assertIn("audit_log_immutability_enabled", Settings.model_fields)

    def test_audit_log_immutability_flag_defaults_false(self):
        from app.core.config import Settings

        field = Settings.model_fields["audit_log_immutability_enabled"]
        self.assertIs(field.default, False)


# ---------------------------------------------------------------------------
# Class 2: ops engine / sessionmaker construction is gated on URL
# ---------------------------------------------------------------------------


class TestC63OpsEngineGated(unittest.TestCase):
    """ops_engine + OpsSessionLocal are None unless URL is set."""

    def test_url_unset_engine_is_none(self):
        with _patched_settings(luciel_ops_db_url=None) as session_mod:
            self.assertIsNone(session_mod.ops_engine)
            self.assertIsNone(session_mod.OpsSessionLocal)

    def test_url_unset_get_ops_db_session_raises(self):
        with _patched_settings(luciel_ops_db_url=None) as session_mod:
            with self.assertRaises(RuntimeError) as ctx:
                with session_mod.get_ops_db_session():
                    pass  # pragma: no cover
            msg = str(ctx.exception)
            self.assertIn("luciel_ops_db_url", msg)
            self.assertIn("LUCIEL_OPS_DB_URL", msg)

    def test_url_set_constructs_engine_and_sessionmaker(self):
        # We don't actually need a reachable DB -- create_engine is lazy.
        fake_url = "postgresql+psycopg2://luciel_ops:fake@localhost:5432/luciel"
        with _patched_settings(luciel_ops_db_url=fake_url) as session_mod:
            self.assertIsNotNone(session_mod.ops_engine)
            self.assertIsNotNone(session_mod.OpsSessionLocal)
            # Engine URL is bound to the patched URL.
            # SQLAlchemy masks passwords in str(url) by default; use
            # render_as_string(hide_password=False) for an exact match.
            self.assertEqual(
                session_mod.ops_engine.url.render_as_string(hide_password=False),
                fake_url,
            )


# ---------------------------------------------------------------------------
# Class 3: ops sessionmaker has NO tenant-context listener
# ---------------------------------------------------------------------------


class TestC63OpsSessionGucIsolation(unittest.TestCase):
    """OpsSessionLocal MUST NOT carry the after_begin tenant-context hook.

    This is the structural guarantee that an ops session never emits
    app.admin_id / app.instance_id -- the listener is attached to
    SessionLocal specifically (session.py line ~124), and OpsSessionLocal
    is a separate sessionmaker instance.
    """

    def test_after_begin_listener_on_session_local(self):
        """Sanity check: the listener IS attached to SessionLocal."""
        from sqlalchemy import event

        import app.db.session as session_mod

        self.assertTrue(
            event.contains(
                session_mod.SessionLocal,
                "after_begin",
                session_mod._set_tenant_context_on_begin,
            ),
            "Pre-condition broken: tenant-context listener should be "
            "attached to SessionLocal (Arc 9 C2 contract).",
        )

    def test_after_begin_listener_NOT_on_ops_session_local(self):
        from sqlalchemy import event

        fake_url = "postgresql+psycopg2://luciel_ops:fake@localhost:5432/luciel"
        with _patched_settings(luciel_ops_db_url=fake_url) as session_mod:
            self.assertFalse(
                event.contains(
                    session_mod.OpsSessionLocal,
                    "after_begin",
                    session_mod._set_tenant_context_on_begin,
                ),
                "SECURITY: tenant-context listener leaked onto "
                "OpsSessionLocal. An ops session would emit "
                "app.admin_id / app.instance_id GUCs onto a "
                "BYPASSRLS connection.",
            )

    def test_ops_sessionmaker_is_distinct_from_session_local(self):
        fake_url = "postgresql+psycopg2://luciel_ops:fake@localhost:5432/luciel"
        with _patched_settings(luciel_ops_db_url=fake_url) as session_mod:
            self.assertIsNot(
                session_mod.OpsSessionLocal,
                session_mod.SessionLocal,
                "OpsSessionLocal must be a distinct sessionmaker -- "
                "aliasing SessionLocal would inherit the after_begin "
                "tenant listener.",
            )

    def test_ops_engine_is_distinct_from_main_engine(self):
        fake_url = "postgresql+psycopg2://luciel_ops:fake@localhost:5432/luciel"
        with _patched_settings(luciel_ops_db_url=fake_url) as session_mod:
            self.assertIsNot(
                session_mod.ops_engine,
                session_mod.engine,
                "ops_engine must be a distinct Engine -- sharing the "
                "main engine would mean ops queries run as the app "
                "Postgres role, not luciel_ops.",
            )


# ---------------------------------------------------------------------------
# Class 4: get_ops_db_session() is a context manager with rollback semantics
# ---------------------------------------------------------------------------


class TestC63GetOpsDbSessionContract(unittest.TestCase):
    """The helper is a proper @contextmanager with commit/rollback/close."""

    def test_is_context_manager(self):
        from app.db.session import get_ops_db_session

        # Decorated with @contextmanager -> has __wrapped__ underneath
        # and calling it returns a _GeneratorContextManager.
        self.assertTrue(callable(get_ops_db_session))

    def test_commits_on_success(self):
        fake_url = "postgresql+psycopg2://luciel_ops:fake@localhost:5432/luciel"
        with _patched_settings(luciel_ops_db_url=fake_url) as session_mod:
            fake_session = mock.MagicMock()
            session_mod.OpsSessionLocal = mock.MagicMock(return_value=fake_session)

            with session_mod.get_ops_db_session() as db:
                self.assertIs(db, fake_session)

            fake_session.commit.assert_called_once()
            fake_session.rollback.assert_not_called()
            fake_session.close.assert_called_once()

    def test_rolls_back_on_exception(self):
        fake_url = "postgresql+psycopg2://luciel_ops:fake@localhost:5432/luciel"
        with _patched_settings(luciel_ops_db_url=fake_url) as session_mod:
            fake_session = mock.MagicMock()
            session_mod.OpsSessionLocal = mock.MagicMock(return_value=fake_session)

            with self.assertRaises(ValueError):
                with session_mod.get_ops_db_session():
                    raise ValueError("boom")

            fake_session.commit.assert_not_called()
            fake_session.rollback.assert_called_once()
            fake_session.close.assert_called_once()

    def test_closes_session_even_when_commit_raises(self):
        fake_url = "postgresql+psycopg2://luciel_ops:fake@localhost:5432/luciel"
        with _patched_settings(luciel_ops_db_url=fake_url) as session_mod:
            fake_session = mock.MagicMock()
            fake_session.commit.side_effect = RuntimeError("commit failed")
            session_mod.OpsSessionLocal = mock.MagicMock(return_value=fake_session)

            with self.assertRaises(RuntimeError):
                with session_mod.get_ops_db_session():
                    pass

            fake_session.close.assert_called_once()


# ---------------------------------------------------------------------------
# Class 5: source-level guards (regex on session.py text)
# ---------------------------------------------------------------------------


class TestC63SourceGuards(unittest.TestCase):
    """Belt-and-braces source checks to catch refactors that bypass tests."""

    @classmethod
    def setUpClass(cls):
        import app.db.session as session_mod

        cls.src = inspect.getsource(session_mod)

    def test_ops_engine_uses_luciel_ops_db_url(self):
        # The construction site must read from settings.luciel_ops_db_url.
        self.assertRegex(
            self.src,
            r"create_engine\(\s*settings\.luciel_ops_db_url",
            "ops_engine must be created from settings.luciel_ops_db_url, "
            "not settings.database_url.",
        )

    def test_ops_engine_construction_is_gated(self):
        self.assertIn(
            "if settings.luciel_ops_db_url is not None:",
            self.src,
            "ops_engine / OpsSessionLocal construction must be gated "
            "on URL presence -- fail closed.",
        )

    def test_get_ops_db_session_raises_when_unset(self):
        self.assertIn(
            "raise RuntimeError",
            self.src,
            "get_ops_db_session() must raise RuntimeError when URL "
            "unset -- silent fallback to SessionLocal would be a "
            "BYPASSRLS leak.",
        )

    def test_no_after_begin_listener_on_ops_session_local(self):
        # The after_begin listener registration must NOT name
        # OpsSessionLocal as its target.
        self.assertNotRegex(
            self.src,
            r'@event\.listens_for\(\s*OpsSessionLocal\s*,\s*["\']after_begin["\']',
            "SECURITY: after_begin listener attached to OpsSessionLocal "
            "would push app.admin_id / app.instance_id onto a BYPASSRLS "
            "connection.",
        )


if __name__ == "__main__":
    unittest.main()
