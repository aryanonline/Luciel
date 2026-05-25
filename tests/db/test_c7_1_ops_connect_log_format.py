"""Arc 9 C7.1 -- OpsSessionLocal connect-event log format contract.

These tests lock the structural contract between three artifacts that
all encode the same string and MUST stay in lockstep:

  1. ``app/db/session.py`` -- the listener
     ``_arc9_c7_emit_ops_connect_event`` emits a single
     ``logger.info("arc9.c7.ops_role_connect role=luciel_ops event=connect")``
     line on every BYPASSRLS connect when
     ``settings.audit_log_immutability_enabled`` is True.

  2. ``cfn/luciel-prod-alarms.yaml`` -- the
     ``OpsRoleConnectMetricFilter`` resource has FilterPattern
     ``'"arc9.c7.ops_role_connect"'`` and counts every matching line
     into ``Luciel/Backend/OpsRoleConnectCount``, which powers the
     Medium-severity ``luciel-ops-role-connect-velocity`` alarm.

  3. ``docs/runbook/arc9_c7_observability.md`` -- the ops doc lists
     the exact substring on-call should grep for when triaging a
     velocity alarm.

If the literal string emitted by (1) drifts out of sync with the
FilterPattern in (2) the alarm goes dark silently -- there is no
runtime error, just zero datapoints. These tests catch that
regression at PR time.

Doctrine reference: D7.1 -- "Every grant the ops role exercises emits
a CloudWatch event." Burning the contract into a test is what makes
D7.1 a structural rather than aspirational guarantee.

The tests do NOT require a Postgres connection -- they exercise the
SQLAlchemy connect event on an in-memory sqlite engine that is
substituted for the real ops_engine via the ``LUCIEL_OPS_DB_URL``
sqlite URL. The same listener registration code path runs because
``app.db.session`` reloads with the URL set.
"""

from __future__ import annotations

import importlib
import logging
import unittest
from contextlib import contextmanager

from sqlalchemy import text


# ---------------------------------------------------------------------------
# Contract constant -- this string MUST appear character-for-character in
# both the listener (app/db/session.py) and the CloudWatch metric filter
# pattern (cfn/luciel-prod-alarms.yaml). Changing it requires updating
# all three sites listed in the module docstring.
# ---------------------------------------------------------------------------

EXPECTED_LOG_LINE = "arc9.c7.ops_role_connect role=luciel_ops event=connect"

# The CloudWatch FilterPattern is the quoted substring below. The
# emitted log line MUST contain it; the test asserts exactly that
# substring presence, mirroring how CloudWatch Logs evaluates the
# filter at metric-extraction time.
CLOUDWATCH_FILTER_SUBSTRING = "arc9.c7.ops_role_connect"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@contextmanager
def _patched_settings(**overrides):
    """Reload app.db.session with patched settings values.

    Mirrors the helper in tests/db/test_c6_3_ops_session.py. The
    ops_engine + OpsSessionLocal + connect listener are all created at
    module-import time gated on ``settings.luciel_ops_db_url``; to
    exercise the listener we patch the setting and reload.
    """
    from app.core import config as config_mod

    original = {k: getattr(config_mod.settings, k) for k in overrides}
    try:
        for k, v in overrides.items():
            object.__setattr__(config_mod.settings, k, v)
        import app.db.session as session_mod

        reloaded = importlib.reload(session_mod)
        yield reloaded
    finally:
        for k, v in original.items():
            object.__setattr__(config_mod.settings, k, v)
        import app.db.session as session_mod

        importlib.reload(session_mod)


def _force_connect(session_mod) -> None:
    """Force the SQLAlchemy ops_engine to open a real DBAPI connection.

    ``create_engine`` is lazy -- the connect event does not fire until
    something actually pulls a connection from the pool. A trivial
    ``SELECT 1`` is enough to drive the listener.
    """
    assert session_mod.ops_engine is not None, "ops_engine not constructed"
    with session_mod.ops_engine.connect() as conn:
        conn.execute(text("SELECT 1"))


# ---------------------------------------------------------------------------
# Class 1: literal format contract
# ---------------------------------------------------------------------------


class TestC71LogFormatContract(unittest.TestCase):
    """The listener emits EXACTLY the string the CFN metric filter parses."""

    def test_emits_exact_expected_line_when_flag_enabled(self):
        """With the immutability flag on, a connect emits the literal line."""
        with self.assertLogs("app.db.session", level="INFO") as captured:
            with _patched_settings(
                luciel_ops_db_url="sqlite:///:memory:",
                audit_log_immutability_enabled=True,
            ) as session_mod:
                _force_connect(session_mod)

        # Exactly one INFO record carrying the contract string.
        contract_records = [
            r for r in captured.records if EXPECTED_LOG_LINE in r.getMessage()
        ]
        self.assertEqual(
            len(contract_records),
            1,
            f"expected exactly one connect-event log line, "
            f"got {len(contract_records)} from records: "
            f"{[r.getMessage() for r in captured.records]}",
        )
        self.assertEqual(contract_records[0].getMessage(), EXPECTED_LOG_LINE)
        self.assertEqual(contract_records[0].levelno, logging.INFO)

    def test_emitted_line_contains_cloudwatch_filter_substring(self):
        """The CFN FilterPattern substring must be present in the emission."""
        with self.assertLogs("app.db.session", level="INFO") as captured:
            with _patched_settings(
                luciel_ops_db_url="sqlite:///:memory:",
                audit_log_immutability_enabled=True,
            ) as session_mod:
                _force_connect(session_mod)

        messages = [r.getMessage() for r in captured.records]
        matching = [m for m in messages if CLOUDWATCH_FILTER_SUBSTRING in m]
        self.assertGreaterEqual(
            len(matching),
            1,
            f"CloudWatch FilterPattern substring "
            f"{CLOUDWATCH_FILTER_SUBSTRING!r} not found in emitted log "
            f"records {messages!r}. The metric filter in "
            f"cfn/luciel-prod-alarms.yaml will produce zero datapoints "
            f"-- the OpsRoleConnect velocity alarm has been silently "
            f"disarmed by a drift between session.py and the CFN.",
        )

    def test_emitted_line_carries_required_kv_tokens(self):
        """The role= and event= tokens are stable so future structured
        parsing (e.g. CloudWatch Logs Insights) keeps working.
        """
        with self.assertLogs("app.db.session", level="INFO") as captured:
            with _patched_settings(
                luciel_ops_db_url="sqlite:///:memory:",
                audit_log_immutability_enabled=True,
            ) as session_mod:
                _force_connect(session_mod)

        line = next(
            r.getMessage()
            for r in captured.records
            if CLOUDWATCH_FILTER_SUBSTRING in r.getMessage()
        )
        self.assertIn("role=luciel_ops", line)
        self.assertIn("event=connect", line)


# ---------------------------------------------------------------------------
# Class 2: flag gating
# ---------------------------------------------------------------------------


class TestC71FlagGating(unittest.TestCase):
    """The listener is silent unless audit_log_immutability_enabled is True."""

    def test_no_emission_when_immutability_flag_disabled(self):
        """Dev / CI must not spam CloudWatch with baseline-skewing noise."""
        # The assertLogs context manager fails if NOTHING is logged on
        # the requested logger -- so we install a no-op handler on the
        # session logger up front and inspect records manually.
        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        capture = _Capture(level=logging.DEBUG)
        logger = logging.getLogger("app.db.session")
        logger.addHandler(capture)
        previous_level = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            with _patched_settings(
                luciel_ops_db_url="sqlite:///:memory:",
                audit_log_immutability_enabled=False,
            ) as session_mod:
                _force_connect(session_mod)
        finally:
            logger.removeHandler(capture)
            logger.setLevel(previous_level)

        contract_records = [
            r for r in records if CLOUDWATCH_FILTER_SUBSTRING in r.getMessage()
        ]
        self.assertEqual(
            len(contract_records),
            0,
            f"connect-event log line emitted with immutability flag "
            f"OFF -- the flag gate is broken. Found: "
            f"{[r.getMessage() for r in contract_records]}",
        )


# ---------------------------------------------------------------------------
# Class 3: listener attachment shape
# ---------------------------------------------------------------------------


class TestC71ListenerAttachment(unittest.TestCase):
    """The connect listener is attached to ops_engine, not the app engine."""

    def test_listener_only_attached_when_ops_url_set(self):
        """No ops URL -> no listener registration site executed."""
        with _patched_settings(
            luciel_ops_db_url=None,
            audit_log_immutability_enabled=True,
        ) as session_mod:
            # When URL is unset the entire branch that registers the
            # listener never runs, so ops_engine itself is None.
            self.assertIsNone(session_mod.ops_engine)
            self.assertIsNone(session_mod.OpsSessionLocal)

    def test_listener_present_on_ops_engine_when_url_set(self):
        """When URL is set the listener is registered on ops_engine.

        The ``connect`` event lives on the engine's underlying Pool
        dispatcher (not the engine itself), so we walk the pool's
        listener registry.
        """
        with _patched_settings(
            luciel_ops_db_url="sqlite:///:memory:",
            audit_log_immutability_enabled=True,
        ) as session_mod:
            self.assertIsNotNone(session_mod.ops_engine)

            # Pool-level connect listeners. SQLAlchemy 2.x exposes the
            # active listener tuple via ``dispatch.connect.listeners``
            # on the pool dispatcher.
            pool = session_mod.ops_engine.pool
            registry = pool.dispatch.connect
            names = [
                getattr(fn, "__name__", "") for fn in registry.listeners
            ]
            self.assertIn(
                "_arc9_c7_emit_ops_connect_event",
                names,
                f"C7.1 connect listener not registered on ops_engine's "
                f"pool. Found connect listeners: {names!r}. Without "
                f"this listener the OpsRoleConnect metric filter has "
                f"nothing to count.",
            )


if __name__ == "__main__":
    unittest.main()
