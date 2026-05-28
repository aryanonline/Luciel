"""Arc 11 Step 4 — static-shape contract for the three migrations.

This is the Pillar 5 alignment precursor (ARC11_PLAN.md §8.5): the
migration shape is asserted by code, not by humans reading SQL. It
runs without a live Postgres so CI catches regressions even on the
no-DB sandbox.

Contracts guarded:

  S1  d1 — ``arc11_d1_rls_knowledge_sources``:
      * ``ENABLE ROW LEVEL SECURITY``  on knowledge_sources
      * ``FORCE ROW LEVEL SECURITY``   on knowledge_sources
      * Creates ``knowledge_sources_admin_isolation``  (RESTRICTIVE, USING + WITH CHECK)
      * Creates ``knowledge_sources_admin_isolation_write`` (FOR INSERT, WITH CHECK)
      * Predicates use ``current_setting('app.admin_id', true)``
      * downgrade() drops both policies and disables RLS
      * Chains to ``arc11_b_rename_embeddings_to_chunks``

  S2  d2 — ``arc11_d2_rls_chunks_postrename_verify``:
      * Renames policies on ``knowledge_chunks`` from
        ``knowledge_embeddings_*`` to ``knowledge_chunks_*`` via a
        ``pg_policies`` DO loop
      * downgrade() is symmetric
      * Chains to ``arc11_d1_rls_knowledge_sources``

  S3  d3 — ``arc11_d3_hnsw_index_chunks``:
      * ``CREATE INDEX IF NOT EXISTS ix_knowledge_chunks_embedding_hnsw``
      * ``USING hnsw (embedding vector_cosine_ops)``
      * ``m = 16, ef_construction = 64``
      * downgrade drops the index
      * Chains to ``arc11_d2_rls_chunks_postrename_verify``

If any of these contracts breaks, the test fails at PR time, not
during prod deploy.
"""
from __future__ import annotations

import importlib.util
import re
import unittest
from pathlib import Path


VERSIONS_DIR = (
    Path(__file__).resolve().parents[2] / "alembic" / "versions"
)


def _read(rev_id: str) -> str:
    return (VERSIONS_DIR / f"{rev_id}.py").read_text(encoding="utf-8")


def _load(rev_id: str):
    """Import the migration file as a module — catches Python-level
    breakage that text greps miss."""
    path = VERSIONS_DIR / f"{rev_id}.py"
    spec = importlib.util.spec_from_file_location(
        f"_arc11_d_test_{rev_id}", str(path)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestD1RlsKnowledgeSourcesShape(unittest.TestCase):

    REV = "arc11_d1_rls_knowledge_sources"
    EXPECTED_DOWN = "arc11_b_rename_embeddings_to_chunks"

    def setUp(self) -> None:
        self.text = _read(self.REV)
        self.lower = self.text.lower()
        self.module = _load(self.REV)

    def test_revision_id_matches_filename(self):
        self.assertEqual(self.module.revision, self.REV)

    def test_down_revision_chain(self):
        self.assertEqual(self.module.down_revision, self.EXPECTED_DOWN)

    def test_enable_row_level_security(self):
        self.assertIn(
            "alter table knowledge_sources enable row level security",
            self.lower,
        )

    def test_force_row_level_security(self):
        self.assertIn(
            "alter table knowledge_sources force row level security",
            self.lower,
        )

    def test_admin_isolation_policy_is_restrictive(self):
        # The big-policy must be CREATE POLICY <name> ON <table> AS RESTRICTIVE.
        self.assertRegex(
            self.lower,
            r"create\s+policy\s+knowledge_sources_admin_isolation\s+"
            r"on\s+knowledge_sources\s+as\s+restrictive",
        )

    def test_admin_isolation_policy_for_all(self):
        # The first policy must cover SELECT/INSERT/UPDATE/DELETE.
        # We look for the FOR ALL clause within the same CREATE POLICY block.
        block = self._policy_block("knowledge_sources_admin_isolation")
        self.assertIn("for all", block.lower())

    def test_admin_isolation_using_and_with_check(self):
        block = self._policy_block(
            "knowledge_sources_admin_isolation"
        ).lower()
        self.assertRegex(block, r"using\s*\(")
        self.assertRegex(block, r"with\s+check\s*\(")

    def test_admin_isolation_uses_current_setting_admin_id(self):
        block = self._policy_block(
            "knowledge_sources_admin_isolation"
        ).lower()
        self.assertRegex(
            block,
            r"current_setting\(\s*'app\.admin_id'\s*,\s*true\s*\)",
        )

    def test_insert_only_write_policy_exists(self):
        # The brief asks for a separate FOR INSERT WITH CHECK policy.
        self.assertRegex(
            self.lower,
            r"create\s+policy\s+knowledge_sources_admin_isolation_write\s+"
            r"on\s+knowledge_sources",
        )
        block = self._policy_block(
            "knowledge_sources_admin_isolation_write"
        ).lower()
        self.assertIn("for insert", block)
        self.assertRegex(block, r"with\s+check\s*\(")

    def test_no_grant_to_luciel_app(self):
        """Doctrine: arc9_c10_b set ALTER DEFAULT PRIVILEGES so new
        tables inherit the CRUD grant. This migration must NOT
        issue an explicit GRANT or it would be a second source of
        truth that can drift."""
        # Allow the literal "luciel_app" in comments but not in a
        # GRANT statement. Cheapest reliable check: no "grant" verb
        # anywhere outside docstrings. The migration body is small
        # enough that a text-grep is safe; we strip the docstring
        # by reading the module's body source AST.
        import ast
        tree = ast.parse(self.text)
        # Find the upgrade() function and only check its body.
        upgrade_fn = next(
            n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "upgrade"
        )
        body_text = ast.unparse(upgrade_fn)
        self.assertNotRegex(
            body_text.lower(),
            r"\bgrant\b",
            "Step 4 d1 must not issue explicit GRANTs; the default-"
            "privilege ALTER from arc9_c10_b is the source of truth.",
        )

    def test_downgrade_reverses_everything(self):
        self.assertIn(
            "drop policy if exists knowledge_sources_admin_isolation_write",
            self.lower,
        )
        self.assertIn(
            "drop policy if exists knowledge_sources_admin_isolation",
            self.lower,
        )
        self.assertIn(
            "alter table knowledge_sources no force row level security",
            self.lower,
        )
        self.assertIn(
            "alter table knowledge_sources disable row level security",
            self.lower,
        )

    def _policy_block(self, polname: str) -> str:
        """Extract the substring beginning at ``CREATE POLICY <name>``
        and ending at the next semicolon. Lets the assertions check
        per-policy clauses without bleeding into siblings."""
        m = re.search(
            rf"CREATE\s+POLICY\s+{re.escape(polname)}\b.*?;",
            self.text,
            re.DOTALL | re.IGNORECASE,
        )
        self.assertIsNotNone(
            m, f"Policy block for {polname} not found"
        )
        return m.group(0)


class TestD2RlsChunksPostrenameVerifyShape(unittest.TestCase):

    REV = "arc11_d2_rls_chunks_postrename_verify"
    EXPECTED_DOWN = "arc11_d1_rls_knowledge_sources"

    def setUp(self) -> None:
        self.text = _read(self.REV)
        self.lower = self.text.lower()
        self.module = _load(self.REV)

    def test_revision_id_matches_filename(self):
        self.assertEqual(self.module.revision, self.REV)

    def test_down_revision_chain(self):
        self.assertEqual(self.module.down_revision, self.EXPECTED_DOWN)

    def test_renames_via_pg_policies_loop(self):
        """The migration must walk pg_policies — not hard-code a
        list — so any policy variant we missed in inventory is
        still caught."""
        self.assertIn("pg_policies", self.lower)
        self.assertIn("alter policy", self.lower)
        self.assertIn("rename to", self.lower)

    def test_upgrade_targets_legacy_prefix(self):
        # The upgrade must rename the legacy ``knowledge_embeddings_``
        # prefix to ``knowledge_chunks_``.
        import ast
        upgrade_src = ast.unparse(
            next(
                n for n in ast.parse(self.text).body
                if isinstance(n, ast.FunctionDef) and n.name == "upgrade"
            )
        )
        self.assertIn("knowledge_embeddings_", upgrade_src)
        self.assertIn("knowledge_chunks_", upgrade_src)

    def test_downgrade_inverse(self):
        import ast
        downgrade_src = ast.unparse(
            next(
                n for n in ast.parse(self.text).body
                if isinstance(n, ast.FunctionDef) and n.name == "downgrade"
            )
        )
        self.assertIn("knowledge_chunks_", downgrade_src)
        self.assertIn("knowledge_embeddings_", downgrade_src)


class TestD3HnswIndexChunksShape(unittest.TestCase):

    REV = "arc11_d3_hnsw_index_chunks"
    EXPECTED_DOWN = "arc11_d2_rls_chunks_postrename_verify"

    def setUp(self) -> None:
        self.text = _read(self.REV)
        self.lower = self.text.lower()
        self.module = _load(self.REV)

    def test_revision_id_matches_filename(self):
        self.assertEqual(self.module.revision, self.REV)

    def test_down_revision_chain(self):
        self.assertEqual(self.module.down_revision, self.EXPECTED_DOWN)

    def test_index_creation_present(self):
        # The plan spells out the index name, table, opclass, and
        # parameters. Lock all four.
        self.assertIn(
            "ix_knowledge_chunks_embedding_hnsw", self.lower
        )
        self.assertRegex(
            self.lower,
            r"create\s+index\s+if\s+not\s+exists\s+"
            r"ix_knowledge_chunks_embedding_hnsw",
        )
        self.assertIn("on knowledge_chunks", self.lower)
        self.assertIn("using hnsw", self.lower)
        self.assertIn("vector_cosine_ops", self.lower)

    def test_index_params_m_and_ef_construction(self):
        # ARC11_PLAN.md §2.3 pins the parameters; assert exact text.
        self.assertRegex(
            self.lower,
            r"m\s*=\s*16",
        )
        self.assertRegex(
            self.lower,
            r"ef_construction\s*=\s*64",
        )

    def test_no_concurrently(self):
        """Doctrine across the project: alembic runs DDL inside a
        transaction and CONCURRENTLY would error out. We do not
        use CONCURRENTLY here. (If the project ever switches to a
        non-transactional migration runner, drop this assertion.)"""
        # Permit the word "CONCURRENTLY" in comments (the docstring
        # explains why we DON'T use it); forbid it in upgrade() body.
        import ast
        upgrade_src = ast.unparse(
            next(
                n for n in ast.parse(self.text).body
                if isinstance(n, ast.FunctionDef) and n.name == "upgrade"
            )
        )
        # ast.unparse drops comments; what remains are docstrings +
        # actual statements. We assert the upgrade body does not
        # carry CONCURRENTLY as an executable token.
        # The migration's upgrade has no internal docstring so
        # any occurrence here would be in a string passed to op.execute.
        if "CONCURRENTLY" in upgrade_src.upper():
            # Allow it ONLY inside a triple-quoted docstring at the
            # top of the function — unlikely here but worth a soft
            # check.
            self.fail(
                "Migration d3 must not use CREATE INDEX CONCURRENTLY: "
                "alembic wraps DDL in a transaction and CONCURRENTLY "
                "would error. See the migration docstring for why."
            )

    def test_downgrade_drops_index(self):
        import ast
        downgrade_src = ast.unparse(
            next(
                n for n in ast.parse(self.text).body
                if isinstance(n, ast.FunctionDef) and n.name == "downgrade"
            )
        )
        self.assertRegex(
            downgrade_src.lower(),
            r"drop\s+index\s+if\s+exists\s+ix_knowledge_chunks_embedding_hnsw",
        )


class TestArc11DMigrationsChainAndImport(unittest.TestCase):
    """Cross-cutting: the three d-step migrations form a linear
    chain off the Step 2 head, and the resulting head is the d3
    migration."""

    EXPECTED_CHAIN = [
        ("arc11_d1_rls_knowledge_sources",
         "arc11_b_rename_embeddings_to_chunks"),
        ("arc11_d2_rls_chunks_postrename_verify",
         "arc11_d1_rls_knowledge_sources"),
        ("arc11_d3_hnsw_index_chunks",
         "arc11_d2_rls_chunks_postrename_verify"),
    ]

    def test_chain_is_linear_and_correct(self):
        for rev, expected_down in self.EXPECTED_CHAIN:
            module = _load(rev)
            self.assertEqual(module.revision, rev, f"{rev}: bad revision id")
            self.assertEqual(
                module.down_revision, expected_down,
                f"{rev}: chain broken (expected down={expected_down})",
            )

    def test_branch_labels_and_depends_on_unset(self):
        """The three migrations are part of the main chain. Branch
        labels or depends_on would fork the graph; neither should
        be set."""
        for rev, _ in self.EXPECTED_CHAIN:
            module = _load(rev)
            self.assertIsNone(getattr(module, "branch_labels", None))
            self.assertIsNone(getattr(module, "depends_on", None))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
