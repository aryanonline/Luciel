"""Arc 11 Closeout PR-B — crawl stub structured-error-code contract.

The crawl_website Celery task is a deliberate Arc 11 stub: it flips
the ``knowledge_sources`` row to ``failed`` with the canonical
``CRAWL_NOT_YET_AVAILABLE`` code so the frontend can render a
"⏱ Coming soon" badge while the real crawler ships separately.

Closeout PR-A landed instance lifecycle. Closeout PR-B (this PR)
replaces the earlier substring-sniff contract (``"Arc-14"`` literal
in ``ingestion_error``) with a structured ``ingestion_error_code``
column. This test guards the static shape of that contract.

Test posture: static-shape inspection over the source tree and the
crawl module. The full execute-the-task path lives behind a live
Postgres + Celery harness (``LUCIEL_LIVE_POSTGRES_URL``) and is not
part of the non-DB suite; what we lock here is what the contract
promises about the route's response payload and what the task
writes to the row.
"""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

from app.api.v1 import admin_knowledge as ak
from app.models.knowledge_source import KnowledgeSource
from app.models.knowledge_source_errors import IngestionErrorCode
from app.worker.tasks import crawl_website as cw


SRC_PATH = Path(cw.__file__)
SRC = SRC_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------
# Canonical-code locks
# ---------------------------------------------------------------------


class TestCanonicalCodeShape(unittest.TestCase):
    """The structured code is part of the cross-repo contract. The
    frontend matches on the literal string value; renaming or
    reusing the value silently breaks the badge."""

    def test_enum_value_is_canonical_string(self):
        self.assertEqual(
            IngestionErrorCode.CRAWL_NOT_YET_AVAILABLE.value,
            "CRAWL_NOT_YET_AVAILABLE",
            "IngestionErrorCode.CRAWL_NOT_YET_AVAILABLE.value is the "
            "literal the frontend compares against. Do not rename.",
        )

    def test_enum_is_str_enum(self):
        """The enum inherits from ``str`` so members serialise cleanly
        through Pydantic / FastAPI as plain JSON strings."""
        self.assertTrue(
            issubclass(IngestionErrorCode, str),
            "IngestionErrorCode must inherit from str for JSON "
            "serialisation through KnowledgeSourceRead.",
        )

    def test_module_level_constants_carry_the_code(self):
        self.assertEqual(
            cw._DEFERRED_ERROR_CODE, "CRAWL_NOT_YET_AVAILABLE",
        )
        # Human-readable message is plain English, no arc identifier.
        self.assertEqual(
            cw._DEFERRED_ERROR_MESSAGE,
            "Website crawl is not yet available. Coming soon.",
        )


# ---------------------------------------------------------------------
# Crawl-task body shape
# ---------------------------------------------------------------------


class TestCrawlTaskBody(unittest.TestCase):
    """Static AST / source-grep checks on what the task actually does."""

    def test_mark_status_called_with_failed_and_code(self):
        """The task must call ``mark_status(..., status='failed',
        error=_DEFERRED_ERROR_MESSAGE, error_code=_DEFERRED_ERROR_CODE)``
        — the structured code is the contract surface; the text is
        ops-debugging only."""
        self.assertRegex(
            SRC,
            r"mark_status\(\s*[\s\S]*?status\s*=\s*['\"]failed['\"]"
            r"[\s\S]*?error_code\s*=\s*_DEFERRED_ERROR_CODE",
            "crawl_website must pass error_code=_DEFERRED_ERROR_CODE "
            "into mark_status with status='failed'.",
        )
        self.assertRegex(
            SRC,
            r"error\s*=\s*_DEFERRED_ERROR_MESSAGE",
            "crawl_website must also pass the human-readable "
            "_DEFERRED_ERROR_MESSAGE as error=… for ops debugging.",
        )

    def test_task_does_not_carry_arc_identifier_literal(self):
        """No-internal-arc-strings-in-user-facing-contracts: the
        crawl module must not assign or pass any non-docstring
        string literal containing an internal arc identifier.
        Docstrings legitimately discuss the module's history;
        string *values* are the contract surface."""
        tree = ast.parse(SRC)
        docstring_ids: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(
                node,
                (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
            ):
                body = getattr(node, "body", None)
                if not body:
                    continue
                first = body[0]
                if (
                    isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)
                ):
                    docstring_ids.add(id(first.value))

        pattern = re.compile(r"Arc[-\s]\d{1,3}\b")
        offenders: list[str] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstring_ids
                and pattern.search(node.value)
            ):
                offenders.append(node.value)
        self.assertEqual(
            offenders, [],
            f"crawl_website source carries arc-identifier non-docstring "
            f"string literals: {offenders!r}. Use IngestionErrorCode "
            f"instead — internal arc identifiers must not leak into "
            f"any user-facing or cross-repo string value.",
        )


# ---------------------------------------------------------------------
# Response-shape contract
# ---------------------------------------------------------------------


class TestKnowledgeSourceReadShape(unittest.TestCase):
    """The route's response model exposes the structured code. The
    frontend's TypeScript type mirrors this shape."""

    def test_response_model_carries_ingestion_error_code_field(self):
        fields = ak.KnowledgeSourceRead.model_fields
        self.assertIn(
            "ingestion_error_code", fields,
            "KnowledgeSourceRead must expose ingestion_error_code "
            "(str | None). The frontend keys badge rendering on it.",
        )

    def test_ingestion_error_code_is_optional_string(self):
        field = ak.KnowledgeSourceRead.model_fields["ingestion_error_code"]
        # Annotation is ``str | None`` (typing.Optional[str]) — both
        # ``None`` and ``"CRAWL_NOT_YET_AVAILABLE"`` are valid payload
        # values.
        ann = field.annotation
        self.assertIn(
            "str", str(ann),
            f"ingestion_error_code annotation should be str | None, "
            f"got {ann!r}",
        )
        # Default is None — most rows do not carry a code.
        self.assertIs(field.default, None)

    def test_serialise_source_populates_ingestion_error_code(self):
        """The route's serialiser helper must read the model column
        through to the response shape. Source-grep is enough — the
        serialiser is a single straight-line function."""
        ak_src = Path(ak.__file__).read_text(encoding="utf-8")
        self.assertRegex(
            ak_src,
            r"ingestion_error_code\s*=\s*s\.ingestion_error_code",
            "_serialise_source must copy ingestion_error_code from "
            "the model row to the response.",
        )


# ---------------------------------------------------------------------
# Model column shape
# ---------------------------------------------------------------------


class TestKnowledgeSourceModelColumn(unittest.TestCase):

    def test_model_carries_ingestion_error_code_column(self):
        col = getattr(KnowledgeSource, "ingestion_error_code", None)
        self.assertIsNotNone(
            col,
            "KnowledgeSource.ingestion_error_code mapped column is "
            "missing; the migration adds the SQL column, the model "
            "must mirror it.",
        )

    def test_column_is_nullable_varchar64(self):
        col = KnowledgeSource.__table__.c.ingestion_error_code
        self.assertTrue(col.nullable)
        # SQLAlchemy String length lives on the type.
        self.assertEqual(getattr(col.type, "length", None), 64)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
