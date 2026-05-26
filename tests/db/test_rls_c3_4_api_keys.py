"""Shape tests for Arc 9 C3.4 — RLS on api_keys (read-permissive, write-strict).

These tests assert the *textual* shape of the Alembic migration file
plus its importability. Real-DB enforcement (round-trip INSERT/SELECT
under varying GUCs, including the auth-perimeter no-context case) is
deferred to the C7 tenant-leak regression suite which spins up an
ephemeral Postgres and exercises every C3.x policy with real rows.

C3.4 is the architecturally unique commit in the C3 series: it is the
only table whose USING clause is intentionally permissive (TRUE) rather
than tenant-strict. The auth middleware queries api_keys BEFORE any
tenant context exists, so a strict-USING policy would 401 every
request. The defence on writes is strict WITH CHECK with the same
'platform' sentinel + tenant-equality structure as C3.3.

See the migration file's module docstring for the full doctrine and
the three remediation options considered (bootstrap sentinel GUC,
middleware refactor, permissive USING) before settling on Option C.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path


# Repo-relative path to the migration under test.
MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "arc9_c3_4_rls_api_keys.py"
)


def _read_migration_text() -> str:
    """Read the migration file as plain text for regex assertions.

    We deliberately do not import-and-introspect-functions: the
    upgrade()/downgrade() bodies use op.execute(...) with SQL string
    literals that are easier to assert against textually than by
    monkey-patching Alembic's op module.
    """
    assert MIGRATION_PATH.exists(), (
        f"Migration file missing at {MIGRATION_PATH}. Did the rename "
        f"or move skip git? Check `git status`."
    )
    return MIGRATION_PATH.read_text(encoding="utf-8")


class TestC34MigrationShape:
    """Textual / structural assertions on the migration file."""

    def test_migration_file_exists(self) -> None:
        """The expected path must hold a readable file."""
        assert MIGRATION_PATH.is_file()

    def test_revision_id(self) -> None:
        """The Alembic revision id must match the filename convention."""
        text = _read_migration_text()
        assert 'revision = "arc9_c3_4_rls_api_keys"' in text

    def test_chains_after_c33(self) -> None:
        """down_revision must point at the C3.3 head.

        This is the assertion that catches accidental branching of the
        Alembic DAG. The C3 series is required to be a single linear
        chain; if a future commit lands at C3.3's head without rebasing
        on top of C3.4, this test fires.
        """
        text = _read_migration_text()
        assert (
            'down_revision = "arc9_c3_3_rls_knowledge_embeddings"' in text
        )

    def test_enables_rls_on_api_keys(self) -> None:
        """upgrade() must ENABLE RLS on the api_keys table."""
        text = _read_migration_text()
        # Tolerate whitespace variants but require the exact table.
        pattern = r"ALTER\s+TABLE\s+api_keys\s+ENABLE\s+ROW\s+LEVEL\s+SECURITY"
        assert re.search(pattern, text, re.IGNORECASE), (
            "ENABLE ROW LEVEL SECURITY on api_keys not found in upgrade()."
        )

    def test_using_is_permissive_true(self) -> None:
        """USING clause must be unconditionally TRUE.

        This is the architecturally unique property of C3.4. If a
        future refactor accidentally tightens USING to a tenant-strict
        comparison, every authenticated request returns 401 because
        the middleware cannot find its own api_keys row. This test is
        the canary.
        """
        text = _read_migration_text()
        # The USING clause must be `USING (TRUE)` (with optional
        # whitespace). We deliberately do NOT match `current_setting`
        # anywhere in USING — its presence here would indicate the
        # tenant-strict regression we are guarding against.
        pattern = r"USING\s*\(\s*TRUE\s*\)"
        assert re.search(pattern, text, re.IGNORECASE), (
            "USING clause must be `USING (TRUE)` — see migration "
            "docstring on auth-perimeter requirement."
        )

    def test_using_does_not_check_tenant(self) -> None:
        """USING clause must NOT reference current_setting or admin_id.

        Defence-in-depth on top of test_using_is_permissive_true: we
        assert the USING line specifically does not contain a tenant-
        equality check (which would re-introduce the chicken-and-egg).
        """
        text = _read_migration_text()
        # Find the USING(...) block specifically (before WITH CHECK)
        # and assert it contains neither current_setting nor admin_id.
        match = re.search(
            r"USING\s*\((?P<using>[^)]*)\)", text, re.IGNORECASE
        )
        assert match is not None, "Could not locate USING clause"
        using_body = match.group("using")
        assert "current_setting" not in using_body.lower(), (
            "USING clause must not reference current_setting — that "
            "would break the auth-perimeter lookup."
        )
        assert "admin_id" not in using_body.lower(), (
            "USING clause must not reference admin_id — see C3.4 "
            "docstring for the auth chicken-and-egg rationale."
        )

    def test_with_check_enforces_tenant_strict_or_platform_null(
        self,
    ) -> None:
        """WITH CHECK must gate writes by tenant equality OR platform NULL.

        This is where the actual security defence lives in C3.4: an
        admin cannot INSERT/UPDATE an api_keys row tagged with another
        tenant's id (which would let them mint impersonation keys).
        """
        text = _read_migration_text()
        # The WITH CHECK clause must reference both the platform
        # sentinel branch and the tenant-equality branch.
        assert "WITH CHECK" in text, "WITH CHECK clause missing"
        # Platform sentinel: NULL admin_id is only writable when GUC
        # is the literal 'platform'.
        assert re.search(
            r"admin_id\s+IS\s+NULL", text, re.IGNORECASE
        ), "WITH CHECK must handle NULL admin_id branch"
        assert "'platform'" in text, (
            "WITH CHECK must reference the 'platform' sentinel literal"
        )
        # Tenant-equality branch.
        assert re.search(
            r"admin_id\s*=\s*current_setting", text, re.IGNORECASE
        ), "WITH CHECK must include admin_id = current_setting() branch"

    def test_current_setting_uses_missing_ok_arg(self) -> None:
        """current_setting must use the two-arg form returning '' on miss.

        `current_setting('app.admin_id', true)` returns '' rather than
        raising when the GUC is unset, matching the C2 listener's
        empty-string write for background paths. Without the `true`
        second arg, the migration would raise at WITH CHECK evaluation
        on any path where the GUC is not set, breaking the auth
        bootstrap.
        """
        text = _read_migration_text()
        # Every current_setting reference in the migration must use
        # the two-arg form.
        for match in re.finditer(
            r"current_setting\s*\([^)]*\)", text, re.IGNORECASE
        ):
            call = match.group(0)
            assert "true" in call.lower(), (
                f"current_setting call missing missing_ok=true: {call}"
            )

    def test_downgrade_drops_policy_and_disables_rls(self) -> None:
        """downgrade() must be reversible: drop policy then disable RLS."""
        text = _read_migration_text()
        # DROP POLICY must come before DISABLE — otherwise on
        # downgrade-then-reupgrade you'd attempt to re-create a policy
        # that may still exist if the disable was skipped.
        drop_pos = text.lower().find("drop policy")
        disable_pos = text.lower().find("disable row level security")
        assert drop_pos != -1, "downgrade() must DROP POLICY"
        assert disable_pos != -1, "downgrade() must DISABLE ROW LEVEL SECURITY"
        assert drop_pos < disable_pos, (
            "downgrade() must DROP POLICY before DISABLE RLS"
        )
        # And it must target api_keys, not some other table left over
        # from copy-paste.
        assert re.search(
            r"DROP\s+POLICY[^;]*\bON\s+api_keys\b",
            text,
            re.IGNORECASE,
        ), "DROP POLICY must target api_keys"
        assert re.search(
            r"ALTER\s+TABLE\s+api_keys\s+DISABLE\s+ROW\s+LEVEL\s+SECURITY",
            text,
            re.IGNORECASE,
        ), "DISABLE must target api_keys"


class TestC34MigrationImports:
    """Importability sanity check.

    Catches Python-level syntax/import errors before Alembic ever
    tries to use the revision. If this passes, `alembic upgrade head`
    at least gets past the parse step.
    """

    def test_module_imports_cleanly(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "arc9_c3_4_rls_api_keys_under_test",
            str(MIGRATION_PATH),
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        # Should not raise.
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        # And expose the required Alembic-revision globals.
        assert getattr(mod, "revision", None) == "arc9_c3_4_rls_api_keys"
        assert (
            getattr(mod, "down_revision", None)
            == "arc9_c3_3_rls_knowledge_embeddings"
        )
        assert callable(getattr(mod, "upgrade", None))
        assert callable(getattr(mod, "downgrade", None))
