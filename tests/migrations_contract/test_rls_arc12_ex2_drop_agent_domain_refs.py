"""
Arc 12 EX2 regression tests — re-seal RLS to v2 admin_id (+ luciel_instance_id),
removing any residual agent_id / domain_id reference from active policy SQL.

CONTRACT GUARDED:
    The Arc 12 excision plan (``arc12_specs/02_EXCISION_PLAN.md`` §EX2)
    requires that BEFORE EX3 drops ``memory.agent_id`` /
    ``session.agent_id`` / ``trace.agent_id`` / ``api_key.agent_id``
    (and the various ``*.domain_id`` columns), every live RLS policy
    must have its USING / WITH CHECK predicates scoped to admin_id
    (and luciel_instance_id where Wall-3) — never to agent_id /
    domain_id. EX2 is the migration that enforces that invariant.

    These tests pin the EX2 migration's shape:

      1. Chains off the Arc 12 WU6 head.
      2. Targets the ``knowledge_chunks_tenant_isolation`` policy on
         ``knowledge_chunks`` — the residual the EX_RESIDUAL_MAP
         called out by name.
      3. Re-creates the policy under the §3.7.5 canonical Wall-1 shape
         (USING + WITH CHECK on ``admin_id``, fail-closed), preserving
         the documented C3.3 NULL-permissive READ carveout for
         platform-curated rows.
      4. Includes a live ``pg_policies`` gate that RAISES EXCEPTION if
         any policy in the public schema still references ``agent_id``
         or ``domain_id`` in its predicate text — so EX3's column drop
         cannot proceed against a still-live agent_id-referencing
         policy.
      5. Static check: no other CREATE POLICY / ALTER POLICY body in
         ``alembic/versions/`` references agent_id / domain_id in a
         USING/WITH CHECK clause.

WHY UNIT (not DB-backed):
    Per the Arc 9 C3 convention — shape tests catch text-level drift
    on the policy DDL and the chain pointers; live-DB behavioural
    tests (fail-closed, isolation) ride on the existing
    ``test_rls_c3_3_knowledge_embeddings.py`` and the Arc 9 WS4b
    live-RLS suite, which exercise the post-arc9_2_pr97 policy
    semantics this re-seal preserves byte-for-byte.

RUN:
    python -m pytest tests/db/test_rls_arc12_ex2_drop_agent_domain_refs.py -v
"""
from __future__ import annotations

import importlib.util
import re
import unittest
from pathlib import Path


VERSIONS_DIR = (
    Path(__file__).parent.parent.parent / "app" / "migrations" / "versions"
)
MIGRATION_PATH = VERSIONS_DIR / "arc12_ex2_rls_drop_agent_domain_refs.py"


class TestEx2MigrationShape(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.text = MIGRATION_PATH.read_text()
        cls.text_lower = cls.text.lower()

    # ------------------------------------------------------------------
    # Existence + chain.
    # ------------------------------------------------------------------

    def test_migration_file_exists(self):
        self.assertTrue(MIGRATION_PATH.exists())

    def test_revision_id(self):
        m = re.search(
            r'^revision\s*=\s*"([^"]+)"', self.text, re.MULTILINE
        )
        self.assertIsNotNone(m)
        self.assertEqual(
            m.group(1), "arc12_ex2_rls_drop_agent_domain_refs"
        )

    def test_chains_off_arc12_wu6(self):
        """EX2 lands as the next head after the Arc 12 WU6
        byo_webhook + tool_execution_log migration (which is the
        current tree-tip at the start of the excision tail)."""
        m = re.search(
            r'^down_revision\s*=\s*"([^"]+)"',
            self.text, re.MULTILINE,
        )
        self.assertIsNotNone(m)
        self.assertEqual(
            m.group(1), "arc12_wu6_byo_webhook_and_tool_execution_log"
        )

    # ------------------------------------------------------------------
    # Policy DDL — the v2 §3.7.5 + C3.3-NULL-carveout shape.
    # ------------------------------------------------------------------

    def test_targets_knowledge_chunks_tenant_isolation(self):
        """EX2 must DROP + RE-CREATE the policy on knowledge_chunks
        named per the post-rename (arc11_d2) convention. The DDL
        f-string substitutes ``{_POLICY}`` / ``{_TABLE}`` for the
        names — check both the substituted form (in the body) and
        the constants themselves."""
        # Constants pinned to the right values.
        self.assertRegex(
            self.text,
            r'_POLICY\s*=\s*"knowledge_chunks_tenant_isolation"',
        )
        self.assertRegex(
            self.text,
            r'_TABLE\s*=\s*"knowledge_chunks"',
        )
        # DDL body uses the constants.
        body_match = re.search(
            r"_V2_POLICY_DDL\s*=\s*f?\"\"\"(.*?)\"\"\"",
            self.text, re.DOTALL,
        )
        self.assertIsNotNone(body_match)
        body_lower = body_match.group(1).lower()
        self.assertIn("drop policy if exists {_policy}", body_lower)
        self.assertIn("create policy {_policy}", body_lower)
        self.assertIn("on {_table}", body_lower)

    def test_policy_does_not_reference_agent_id_or_domain_id(self):
        """The whole point of EX2 — the rewritten policy DDL must
        contain NO agent_id / domain_id reference. A future author
        re-introducing one will trip this test."""
        # Limit the search to the policy DDL block(s) inside the file.
        # We look only inside the strings that contain USING / WITH
        # CHECK, since the module-level docstring talks about agent_id
        # / domain_id (correctly — describing what is REMOVED).
        policy_ddls = re.findall(
            r'"""(?:[^"]|"(?!""))*"""|'
            r"'''(?:[^']|'(?!''))*'''|"
            r'"((?:[^"\\]|\\.)*)"|'
            r"'((?:[^'\\]|\\.)*)'",
            self.text,
        )
        # Crude: scan every CREATE POLICY...; chunk via the
        # ``_V2_POLICY_DDL`` / ``_PRE_EX2_POLICY_DDL`` constants we
        # know are the policy bodies in this file.
        body_match = re.search(
            r"_V2_POLICY_DDL\s*=\s*f?\"\"\"(.*?)\"\"\"",
            self.text, re.DOTALL,
        )
        self.assertIsNotNone(
            body_match,
            "_V2_POLICY_DDL constant not found in EX2 migration",
        )
        body = body_match.group(1).lower()
        self.assertNotIn(
            "agent_id", body,
            "EX2 policy DDL must not reference agent_id — that's "
            "the column EX3 is about to drop.",
        )
        self.assertNotIn(
            "domain_id", body,
            "EX2 policy DDL must not reference domain_id — that's "
            "a column EX3 is about to drop.",
        )

    def test_policy_scopes_on_admin_id_via_current_setting(self):
        """The v2 §3.7.5 pattern: predicate compares admin_id to
        current_setting('app.admin_id', true)."""
        body_match = re.search(
            r"_V2_POLICY_DDL\s*=\s*f?\"\"\"(.*?)\"\"\"",
            self.text, re.DOTALL,
        )
        body = body_match.group(1).lower()
        self.assertRegex(
            body,
            r"admin_id::text\s*=\s*current_setting\(\s*'app\.admin_id'\s*,\s*true\s*\)",
            "EX2 policy must compare admin_id::text against "
            "current_setting('app.admin_id', true).",
        )

    def test_policy_has_both_using_and_with_check(self):
        """§3.7.5 + fail-closed: both halves of the predicate
        present. USING gates reads; WITH CHECK gates writes."""
        body_match = re.search(
            r"_V2_POLICY_DDL\s*=\s*f?\"\"\"(.*?)\"\"\"",
            self.text, re.DOTALL,
        )
        body = body_match.group(1).lower()
        self.assertIn("using", body)
        self.assertIn("with check", body)

    def test_policy_preserves_c3_3_null_carveout(self):
        """The asymmetric NULL-permissive READ + platform-gated NULL
        WRITE carveout for platform-curated rows must be preserved
        from C3.3. Without this, the retriever loses cross-tenant
        domain_knowledge rows; with the WRONG asymmetry, an ordinary
        admin can write a row as if it were platform-curated."""
        body_match = re.search(
            r"_V2_POLICY_DDL\s*=\s*f?\"\"\"(.*?)\"\"\"",
            self.text, re.DOTALL,
        )
        body = body_match.group(1).lower()
        # USING half: admin_id IS NULL permitted unconditionally.
        m_using = re.search(
            r"using\s*\((.*?)\)\s*with\s+check",
            body, re.DOTALL,
        )
        self.assertIsNotNone(m_using)
        using_body = m_using.group(1)
        self.assertIn("admin_id is null", using_body)
        self.assertNotIn(
            "'platform'", using_body,
            "USING clause MUST NOT gate NULL on the platform GUC — "
            "that would break the cross-tenant domain_knowledge "
            "read surface.",
        )

        # WITH CHECK half: admin_id IS NULL requires platform GUC.
        m_check = re.search(
            r"with\s+check\s*\((.*?)\)\s*;",
            body, re.DOTALL,
        )
        self.assertIsNotNone(m_check)
        check_body = m_check.group(1)
        self.assertIn("admin_id is null", check_body)
        self.assertIn(
            "'platform'", check_body,
            "WITH CHECK MUST gate admin_id IS NULL writes by "
            "current_setting('app.admin_id', true) = 'platform'.",
        )

    # ------------------------------------------------------------------
    # Live pg_policies gate — the EX3-unblock invariant.
    # ------------------------------------------------------------------

    def test_includes_live_gate_against_agent_domain_refs(self):
        """The migration must scan pg_policies for any residual
        agent_id / domain_id reference and RAISE EXCEPTION on hit —
        otherwise a future drift could re-introduce one and EX3
        would not be safe."""
        self.assertIn("pg_policies", self.text_lower)
        # Both columns named in the live-state predicate scan.
        self.assertIn("agent_id", self.text_lower)
        self.assertIn("domain_id", self.text_lower)
        self.assertIn("raise exception", self.text_lower)

    def test_live_gate_inspects_both_qual_and_with_check(self):
        """pg_policies exposes the USING predicate as ``qual`` and
        the WITH CHECK predicate as ``with_check``. The gate must
        scan BOTH — a future drift that puts agent_id only in WITH
        CHECK must still trip the gate."""
        self.assertIn("qual", self.text_lower)
        self.assertIn("with_check", self.text_lower)

    # ------------------------------------------------------------------
    # Downgrade.
    # ------------------------------------------------------------------

    def test_downgrade_re_executes_the_pre_ex2_policy_ddl(self):
        """Reversibility: the downgrade must re-execute the pre-EX2
        policy DDL constant. Since pre-EX2 SQL was already v2-shaped
        (courtesy of arc9_2_pr97), this is byte-identical, but the
        downgrade contract demands an explicit revert path."""
        m = re.search(
            r"def downgrade\(\) -> None:(.*?)(?=\Z|\ndef )",
            self.text, re.DOTALL,
        )
        self.assertIsNotNone(m)
        body = m.group(1).lower()
        self.assertIn("op.execute", body)
        self.assertIn("_pre_ex2_policy_ddl", body)


class TestEx2NoOtherPolicyReferencesAgentDomain(unittest.TestCase):
    """Repo-wide invariant: after EX2 lands, no Alembic CREATE POLICY
    / ALTER POLICY body in ``alembic/versions/`` references agent_id
    or domain_id inside a USING / WITH CHECK clause. (Docstring
    mentions are fine — they describe v1 semantics.)"""

    # The migrations whose docstrings mention agent_id/domain_id are
    # tolerated; we scan only ACTIVE policy DDL bodies.

    # A simple grep across the file content for the dangerous shape:
    # ``USING ( ... agent_id ... )`` or ``WITH CHECK ( ... agent_id ... )``.
    _BAD_PATTERN = re.compile(
        r"(?:using|with\s+check)\s*\("
        r"(?:[^()]|\([^()]*\))*?"
        r"\b(?:agent_id|domain_id)\b",
        re.IGNORECASE | re.DOTALL,
    )

    def test_no_active_policy_references_agent_or_domain_id(self):
        offenders: list[tuple[Path, str]] = []
        for path in sorted(VERSIONS_DIR.glob("*.py")):
            text = path.read_text()
            # Strip docstrings so we don't false-positive on the
            # documentation in arc9_c3_3 / EX2 itself / etc.
            # A coarse strip: drop triple-quoted blocks first.
            stripped = re.sub(
                r'"""(?:[^"]|"(?!""))*"""', "", text,
            )
            stripped = re.sub(
                r"'''(?:[^']|'(?!''))*'''", "", stripped,
            )
            for m in self._BAD_PATTERN.finditer(stripped):
                offenders.append((path, m.group(0)[:120]))
        self.assertEqual(
            offenders, [],
            "After EX2, no Alembic CREATE POLICY body may reference "
            "agent_id or domain_id inside a USING / WITH CHECK "
            "clause. Offenders: " + repr(offenders),
        )


class TestEx2MigrationImports(unittest.TestCase):
    """Importability — catches Python-level defects in the new
    migration."""

    def test_module_imports_cleanly(self):
        spec = importlib.util.spec_from_file_location(
            "_ex2_test", str(MIGRATION_PATH)
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(
            module.revision,
            "arc12_ex2_rls_drop_agent_domain_refs",
        )
        self.assertEqual(
            module.down_revision,
            "arc12_wu6_byo_webhook_and_tool_execution_log",
        )
        self.assertTrue(callable(module.upgrade))
        self.assertTrue(callable(module.downgrade))


if __name__ == "__main__":
    import sys
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
