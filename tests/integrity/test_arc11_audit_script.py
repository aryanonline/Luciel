"""Arc 11 Step 10 — meta-test for the close-audit script.

The audit script itself is testable: import its functions, call
them, and assert on the result shape. Live-infrastructure sections
(RDS, AWS) skip gracefully when their env vars are absent — we
assert the SKIP behavior, not the actual live check.

Run:
    python -m pytest tests/integrity/test_arc11_audit_script.py

The actual close audit lives at ``scripts/arc11_close_audit.py``;
see its docstring for the canonical run instructions.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AUDIT_SCRIPT = REPO_ROOT / "scripts" / "arc11_close_audit.py"


def _load_audit_module():
    # Register the module in sys.modules BEFORE exec_module so the
    # @dataclass decorator inside the script can resolve forward
    # references against ``sys.modules[module_name].__dict__``.
    # Without this, Python's dataclass machinery raises
    # AttributeError on a None module entry.
    module_name = "_arc11_close_audit_under_test"
    spec = importlib.util.spec_from_file_location(
        module_name, str(AUDIT_SCRIPT),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------
# Module-level imports + shape
# ---------------------------------------------------------------------


class TestAuditScriptShape(unittest.TestCase):

    def setUp(self) -> None:
        self.audit = _load_audit_module()

    def test_script_exists(self):
        self.assertTrue(AUDIT_SCRIPT.exists())

    def test_module_imports_cleanly(self):
        # Re-import to prove it isn't environment-dependent.
        self.audit  # noqa: B018 — the load already ran

    def test_check_result_dataclass_present(self):
        self.assertTrue(hasattr(self.audit, "CheckResult"))
        # status field is the discriminator.
        result = self.audit.CheckResult(
            section="x", name="y", status="PASS", detail="",
        )
        self.assertEqual(result.status, "PASS")

    def test_main_callable(self):
        self.assertTrue(callable(self.audit.main))


# ---------------------------------------------------------------------
# Section-by-section: invoke the underlying section_* functions and
# assert the result shape + that the right tests SKIP vs PASS in
# the no-live-infra sandbox.
# ---------------------------------------------------------------------


class TestSectionsInSandbox(unittest.TestCase):
    """Each section returns a list of CheckResult. In the sandbox
    (no LUCIEL_LIVE_POSTGRES_URL), some checks SKIP. Verify the
    SKIP behaviour is correct and no static check FAILs."""

    def setUp(self) -> None:
        self.audit = _load_audit_module()
        # Ensure the test environment has the minimum env vars the
        # backend imports need; without these, the model imports
        # crash and cascade-fail every static check.
        os.environ.setdefault(
            "DATABASE_URL", "postgresql+psycopg://x:x@localhost/x"
        )
        os.environ.setdefault("MODERATION_PROVIDER", "null")

    def test_section_1_returns_list_of_check_results(self):
        results = self.audit.section_1_migrations_and_schema(live=False)
        self.assertIsInstance(results, list)
        self.assertGreater(len(results), 0)
        for r in results:
            self.assertIn(r.status, ("PASS", "FAIL", "SKIP"))

    def test_section_1_rls_skips_without_live_db(self):
        results = self.audit.section_1_migrations_and_schema(live=False)
        rls_results = [r for r in results if "rls_policies" in r.name]
        self.assertEqual(
            len(rls_results), 1,
            "expected exactly one RLS check in section 1",
        )
        self.assertEqual(
            rls_results[0].status, "SKIP",
            f"RLS check should SKIP in the sandbox; got {rls_results[0].status}",
        )

    def test_section_1_other_checks_pass(self):
        results = self.audit.section_1_migrations_and_schema(live=False)
        non_rls = [r for r in results if "rls_policies" not in r.name]
        for r in non_rls:
            self.assertEqual(
                r.status, "PASS",
                f"section 1 check {r.name!r} failed: {r.detail}",
            )

    def test_section_2_entitlements_all_pass(self):
        results = self.audit.section_2_entitlements()
        for r in results:
            self.assertEqual(
                r.status, "PASS",
                f"section 2 check {r.name!r} failed: {r.detail}",
            )

    def test_section_3_feature_flag_pass(self):
        results = self.audit.section_3_feature_flag()
        self.assertEqual(len(results), 1)
        self.assertEqual(
            results[0].status, "PASS",
            f"feature flag check failed: {results[0].detail}",
        )

    def test_section_4_infrastructure_all_pass(self):
        results = self.audit.section_4_infrastructure()
        for r in results:
            self.assertEqual(
                r.status, "PASS",
                f"section 4 check {r.name!r} failed: {r.detail}",
            )


# ---------------------------------------------------------------------
# End-to-end: invoke the script as a subprocess and assert exit 0.
# ---------------------------------------------------------------------


class TestAuditScriptEndToEnd(unittest.TestCase):
    """Runs the actual script as a subprocess. Sections 5 + 6
    invoke nested pytest calls so we keep this test isolated to its
    own subprocess to avoid pytest-in-pytest weirdness."""

    def test_script_runs_to_completion_with_exit_zero(self):
        proc = subprocess.run(
            [sys.executable, str(AUDIT_SCRIPT)],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env={
                **os.environ,
                "DATABASE_URL": os.environ.get(
                    "DATABASE_URL", "postgresql+psycopg://x:x@localhost/x",
                ),
                "MODERATION_PROVIDER": os.environ.get(
                    "MODERATION_PROVIDER", "null",
                ),
            },
            timeout=120,
        )
        # Exit 0 is the contract; we tolerate stderr noise.
        self.assertEqual(
            proc.returncode, 0,
            f"audit script exited {proc.returncode}; "
            f"stdout tail:\n{proc.stdout[-2000:]}\n"
            f"stderr tail:\n{proc.stderr[-1000:]}",
        )
        # Stdout contains the human-readable table and the summary.
        self.assertIn("Summary:", proc.stdout)
        self.assertIn("PASS", proc.stdout)

    def test_script_json_mode_returns_parseable_json(self):
        proc = subprocess.run(
            [sys.executable, str(AUDIT_SCRIPT), "--json"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env={
                **os.environ,
                "DATABASE_URL": os.environ.get(
                    "DATABASE_URL", "postgresql+psycopg://x:x@localhost/x",
                ),
                "MODERATION_PROVIDER": os.environ.get(
                    "MODERATION_PROVIDER", "null",
                ),
            },
            timeout=120,
        )
        self.assertEqual(proc.returncode, 0)
        parsed = json.loads(proc.stdout)
        self.assertIsInstance(parsed, list)
        # Every entry must have the four CheckResult fields.
        for entry in parsed:
            self.assertIn("section", entry)
            self.assertIn("name", entry)
            self.assertIn("status", entry)
            self.assertIn("detail", entry)


# Cleanup A removed the _arc11/ directory; the founder's rule is
# that "only source-of-truth documents are the business documents
# in this space" — the close-audit artifacts (DECISIONS.md,
# CLEANUP_CANDIDATES.md, DRIFT_FROM_DOCTRINE.md, SECURITY_FOLLOWUPS.md,
# PRODUCTION_DEPLOY_CHECKLIST.md) now live outside the repo at
# /home/user/workspace/arc11_audit/. The corresponding
# TestArc11ArtifactsPresent test class is therefore removed.


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
