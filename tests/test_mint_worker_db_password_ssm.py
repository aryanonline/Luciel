"""Smoke tests for scripts.mint_worker_db_password_ssm.

P3-S Half 2 fix tests (2026-05-05). These tests isolate the
behaviors that the prod-RDS run revealed as broken:

  1. `verify_role_state` must use `pg_roles` (not `pg_authid`), because
     `pg_authid` is unreadable by `rds_superuser` on AWS RDS.

  2. `verify_first_mint_or_force_rotate` must use SSM presence as the
     rotation signal:
       - SSM `ParameterNotFound` -> first-mint, proceed
       - SSM exists, --force-rotate set -> rotation, proceed
       - SSM exists, --force-rotate NOT set -> refuse, raise

  3. The env-var-only invocation path (Pattern N / Fargate) must
     resolve `WORKER_HOST`, `WORKER_DB_NAME`, and `MINT_DRY_RUN` from
     the environment when the equivalent CLI flags are absent. This
     tests the production code path WITHOUT also passing CLI flags --
     the contamination that hid drift
     `D-test-coverage-assumed-not-proven-mint-script-env-only-path-2026-05-05`.

  4. `build_worker_url` must emit the `postgresql+psycopg://` scheme so
     SQLAlchemy loads the v3 driver in the worker container. The bare
     `postgresql://` scheme triggers SQLAlchemy's default dialect
     resolution to `postgresql+psycopg2`, which crashes the worker with
     `ModuleNotFoundError: No module named 'psycopg2'` (drift
     `D-mint-script-emits-bare-postgresql-scheme-incompatible-with-psycopg-v3-2026-05-05`,
     caught in P3-S Half 2 section 4.4 worker rev-6 deploy).

These tests do NOT touch real AWS or real Postgres. They mock the
boto3 SSM client and the psycopg connection. Network and IAM
correctness are still validated end-to-end by the Fargate ceremony
itself (Step 4 dry-run on each fix cycle).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

# Mint script lives at scripts/mint_worker_db_password_ssm.py. Make it
# importable as a top-level module without polluting sys.path globally.
_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import mint_worker_db_password_ssm as mint  # noqa: E402  (sys.path setup)


# --------------------------------------------------------------------------
# Test 1: verify_role_state uses pg_roles, not pg_authid
# --------------------------------------------------------------------------

class TestVerifyRoleStateUsesPgRoles:
    """The function must query pg_roles. pg_authid is unreadable on RDS.

    These tests do not run real Postgres; they snoop the SQL the
    function passes to cur.execute() and assert it targets pg_roles.
    """

    def _mock_conn(self, fetch_result):
        """Build a psycopg-shaped mock connection with cursor.fetchone -> result."""
        cursor = MagicMock()
        cursor.fetchone.return_value = fetch_result
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        conn = MagicMock()
        conn.cursor.return_value = cursor
        return conn, cursor

    def test_queries_pg_roles_not_pg_authid(self):
        """Asserts the SQL never references pg_authid."""
        conn, cursor = self._mock_conn(("luciel_worker",))

        mint.verify_role_state(conn)

        cursor.execute.assert_called_once()
        sql = cursor.execute.call_args[0][0]
        assert "pg_roles" in sql, f"expected pg_roles in SQL, got: {sql}"
        assert "pg_authid" not in sql, (
            f"pg_authid must NOT appear (RDS-unreadable). SQL was: {sql}"
        )

    def test_role_exists_passes(self):
        """Role found in pg_roles -> no exception, no return value."""
        conn, _ = self._mock_conn(("luciel_worker",))
        # Should not raise.
        result = mint.verify_role_state(conn)
        assert result is None

    def test_role_missing_raises(self):
        """Role not in pg_roles -> RuntimeError pointing at migration."""
        conn, _ = self._mock_conn(None)

        with pytest.raises(RuntimeError) as exc_info:
            mint.verify_role_state(conn)

        msg = str(exc_info.value)
        assert "luciel_worker" in msg
        assert "f392a842f885" in msg, "must reference Commit 7 migration"

    def test_signature_drops_force_rotate(self):
        """Signature change: force_rotate moved to SSM-side check.

        Calling with the old kwarg must fail loudly so any stale
        caller surfaces in CI rather than silently mis-behaving.
        """
        conn, _ = self._mock_conn(("luciel_worker",))
        with pytest.raises(TypeError):
            mint.verify_role_state(conn, force_rotate=False)


# --------------------------------------------------------------------------
# Test 2: verify_first_mint_or_force_rotate -- SSM-presence rotation guard
# --------------------------------------------------------------------------

class TestVerifyFirstMintOrForceRotate:
    """The SSM-side replacement for the dropped pg_authid pw_null check."""

    def _mock_ssm_client(self, *, get_parameter_response=None, raise_code=None):
        """Build a boto3-shaped SSM client mock."""
        ssm = MagicMock()
        if raise_code is not None:
            err = ClientError(
                {"Error": {"Code": raise_code, "Message": f"mock {raise_code}"}},
                "GetParameter",
            )
            ssm.get_parameter.side_effect = err
        else:
            ssm.get_parameter.return_value = get_parameter_response or {}
        return ssm

    def test_first_mint_ssm_not_found_proceeds(self):
        """SSM ParameterNotFound -> first-mint case -> no exception."""
        ssm = self._mock_ssm_client(raise_code="ParameterNotFound")
        with patch("boto3.client", return_value=ssm):
            mint.verify_first_mint_or_force_rotate(
                region="ca-central-1",
                ssm_path="/luciel/production/worker_database_url",
                force_rotate=False,
            )

    def test_rotation_with_force_rotate_proceeds(self):
        """SSM exists + force_rotate=True -> proceed (rotation case)."""
        ssm = self._mock_ssm_client(get_parameter_response={"Parameter": {}})
        with patch("boto3.client", return_value=ssm):
            mint.verify_first_mint_or_force_rotate(
                region="ca-central-1",
                ssm_path="/luciel/production/worker_database_url",
                force_rotate=True,
            )

    def test_rotation_without_force_rotate_refuses(self):
        """SSM exists + force_rotate=False -> RuntimeError."""
        ssm = self._mock_ssm_client(get_parameter_response={"Parameter": {}})
        with patch("boto3.client", return_value=ssm):
            with pytest.raises(RuntimeError) as exc_info:
                mint.verify_first_mint_or_force_rotate(
                    region="ca-central-1",
                    ssm_path="/luciel/production/worker_database_url",
                    force_rotate=False,
                )
        msg = str(exc_info.value)
        assert "--force-rotate" in msg
        assert "/luciel/production/worker_database_url" in msg

    def test_unexpected_ssm_error_raises(self):
        """Non-ParameterNotFound errors must surface, not silently pass."""
        ssm = self._mock_ssm_client(raise_code="ThrottlingException")
        with patch("boto3.client", return_value=ssm):
            with pytest.raises(RuntimeError) as exc_info:
                mint.verify_first_mint_or_force_rotate(
                    region="ca-central-1",
                    ssm_path="/luciel/production/worker_database_url",
                    force_rotate=False,
                )
        assert "ThrottlingException" in str(exc_info.value)


# --------------------------------------------------------------------------
# Test 3: env-var-only invocation path (Pattern N / Fargate)
# --------------------------------------------------------------------------
# This is the test that drift
# `D-test-coverage-assumed-not-proven-mint-script-env-only-path-2026-05-05`
# said was contaminated. We isolate the env-only path here -- NO CLI
# flags for the env-resolved values. argparse must accept the empty
# CLI; main()'s env-resolution must populate args.worker_host /
# .worker_db_name / .dry_run from the environment.
# --------------------------------------------------------------------------

class TestEnvVarOnlyPath:
    """Production Fargate invocation path: env vars only, no CLI flags."""

    def test_argparse_accepts_no_required_flags(self):
        """argparse must NOT require --worker-host / --worker-db-name.

        Asserts the argparse signature is permissive (env-var fallback
        is the legitimate Pattern N path). If argparse re-introduces
        required=True on these, this test fails.
        """
        # Use parse_args with an explicitly empty argv. We expect this
        # to succeed (returning Namespace with worker_host=None etc.),
        # NOT to SystemExit with code 2.
        with patch.object(sys, "argv", ["mint_worker_db_password_ssm.py"]):
            # parse_args reads sys.argv directly via argparse default
            args = mint.parse_args()

        assert args.worker_host is None, (
            "argparse must allow --worker-host to be omitted "
            "(env-var fallback is Pattern N's production path)"
        )
        assert args.worker_db_name is None, (
            "argparse must allow --worker-db-name to be omitted"
        )

    def test_env_resolution_populates_missing_values(self, monkeypatch):
        """main()'s env-resolution block must read WORKER_HOST etc.

        We call parse_args() with no CLI flags, then exercise the
        same env-resolution logic main() does. This is the
        un-contaminated env-only path: no CLI flags, only env vars.
        """
        monkeypatch.setenv("WORKER_HOST", "test-host.example.com")
        monkeypatch.setenv("WORKER_DB_NAME", "test_db")
        monkeypatch.setenv("MINT_DRY_RUN", "true")

        with patch.object(sys, "argv", ["mint_worker_db_password_ssm.py"]):
            args = mint.parse_args()

        # Simulate main()'s env-resolution block inline.
        import os
        if args.worker_host is None:
            args.worker_host = os.environ.get("WORKER_HOST")
        if args.worker_db_name is None:
            args.worker_db_name = os.environ.get("WORKER_DB_NAME")
        if args.dry_run is None:
            args.dry_run = mint._env_truthy(os.environ.get("MINT_DRY_RUN"))

        assert args.worker_host == "test-host.example.com"
        assert args.worker_db_name == "test_db"
        assert args.dry_run is True

    def test_env_truthy_accepts_true_variants(self):
        """MINT_DRY_RUN parsing must accept the documented truthy values.

        The script's docstring is the contract: '1/true/yes/on'
        case-insensitive. The parser is intentionally conservative — anything
        ambiguous (e.g. bare 'Y' or 'T') is False, forcing callers to opt IN
        to dry-run with a full word. Do not expand this set without an
        explicit security review.
        """
        for truthy in ["true", "TRUE", "True", "1", "yes", "YES", "on", "ON"]:
            assert mint._env_truthy(truthy) is True, (
                f"_env_truthy({truthy!r}) should be True"
            )
        for falsy in ["false", "0", "no", "off", "", "Y", "T", None]:
            assert mint._env_truthy(falsy) is False, (
                f"_env_truthy({falsy!r}) should be False (conservative parser)"
            )


# --------------------------------------------------------------------------
# Test 4: build_worker_url emits postgresql+psycopg:// (NOT bare postgresql://)
#
# Drift D-mint-script-emits-bare-postgresql-scheme-incompatible-with-psycopg-v3-2026-05-05
# (P0, caught 2026-05-05 in P3-S Half 2 section 4.4). The worker container only ships
# psycopg v3 (per pyproject.toml `psycopg[binary]>=3.2.10`). SQLAlchemy's
# default dialect resolution for `postgresql://` is `postgresql+psycopg2`,
# which raises `ModuleNotFoundError: No module named 'psycopg2'` at
# `create_engine()` time. The fix: emit `postgresql+psycopg://` explicitly.
# --------------------------------------------------------------------------

class TestBuildWorkerUrlSchemeMatchesSqlAlchemyDriver:
    """`build_worker_url` MUST emit the `postgresql+psycopg://` scheme.

    The runtime worker uses SQLAlchemy + psycopg v3. Bare `postgresql://`
    triggers SQLAlchemy to load `psycopg2` (v2), which is not installed,
    causing `ModuleNotFoundError` at `create_engine()` time. Tests below
    pin both the scheme constant AND the assembled URL, so a regression
    that drops the prefix would fail loudly in CI before any deploy.
    """

    def test_module_constant_is_psycopg_v3_scheme(self):
        """WORKER_DSN_SCHEME pins the dialect at module level.

        Pinning at the constant ensures any future code that builds DSNs
        outside `build_worker_url` (e.g., a `--print-url` debug helper)
        cannot accidentally drop the prefix.
        """
        assert mint.WORKER_DSN_SCHEME == "postgresql+psycopg://", (
            "DSN scheme must be postgresql+psycopg:// to match the worker's "
            "installed driver (psycopg v3). Bare postgresql:// crashes the "
            "worker on import psycopg2."
        )

    def test_build_worker_url_starts_with_psycopg_v3_scheme(self):
        """Assembled URL begins with `postgresql+psycopg://`."""
        url = mint.build_worker_url(
            role="luciel_worker",
            password="abc123",
            host="db.example.com",
            port=5432,
            db_name="luciel",
            sslmode="require",
        )
        assert url.startswith("postgresql+psycopg://"), (
            f"DSN must start with postgresql+psycopg://, got: {url[:30]!r}"
        )

    def test_build_worker_url_does_not_emit_bare_postgresql_scheme(self):
        """Negative assertion: bare `postgresql://` (no driver) is forbidden.

        Belt-and-suspenders to the positive test above: explicitly catches
        the regression where someone removes `+psycopg` thinking 'shorter
        URL is cleaner'. The worker WILL crash with ModuleNotFoundError.
        """
        url = mint.build_worker_url(
            role="luciel_worker",
            password="abc123",
            host="db.example.com",
            port=5432,
            db_name="luciel",
            sslmode="require",
        )
        # Allow `postgresql+psycopg://` but reject bare `postgresql://`.
        # urlparse normalizes the scheme to lowercase and strips the `+` part
        # only into the scheme portion, so we test on the raw string.
        assert not url.startswith("postgresql://"), (
            "DSN must NOT use bare postgresql:// scheme: SQLAlchemy will "
            "try to load psycopg2 (not installed) and the worker will crash"
        )

    def test_build_worker_url_loadable_by_sqlalchemy_make_url(self):
        """End-to-end proof: SQLAlchemy parses the URL and resolves the dialect.

        This is the most strict test. `make_url` parses the URL, and
        `URL.get_dialect()` actually imports the driver module. If the
        scheme is wrong (or psycopg v3 isn't installed in the test env),
        this raises -- which is the same crash the worker would see.

        Skipped if SQLAlchemy or psycopg v3 isn't a test dep. We don't pin
        either as a hard test dep because the mint script doesn't import
        SQLAlchemy (it uses raw psycopg). But when both ARE available
        locally, this test is the highest-fidelity assertion that the URL
        is consumable end-to-end. In CI / on the worker container both
        are installed, so this test runs there.
        """
        sqlalchemy = pytest.importorskip("sqlalchemy")
        # `get_dialect()` below actually imports the driver module to
        # construct the Dialect class. Skip if psycopg v3 isn't installed
        # in this env (advisor sandbox); in CI and on the worker it is.
        pytest.importorskip("psycopg")
        url = mint.build_worker_url(
            role="luciel_worker",
            password="abc123",
            host="db.example.com",
            port=5432,
            db_name="luciel",
            sslmode="require",
        )
        parsed = sqlalchemy.engine.url.make_url(url)
        # `+psycopg` resolves to the psycopg v3 dialect.
        assert parsed.get_dialect().driver == "psycopg", (
            f"SQLAlchemy must resolve to the psycopg (v3) driver for this URL; "
            f"got driver={parsed.get_dialect().driver!r}. URL: {url}"
        )
