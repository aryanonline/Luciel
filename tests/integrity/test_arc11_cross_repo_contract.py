"""Cross-repo contract: the crawl_website stub MUST signal the
"deferred feature" state to the frontend through the structured
``ingestion_error_code`` column carrying the canonical
``CRAWL_NOT_YET_AVAILABLE`` value. If this test fails, the
frontend's "⏱ Coming soon" badge silently degrades to a red
"Failed" badge and admins see the crawl stub as a broken feature
instead of a deliberately-deferred one.

History
-------

Arc 11 Closeout PR-B replaced an earlier substring-sniff contract.
The original Arc 11 stub wrote the literal substring ``"Arc-14"``
into ``knowledge_sources.ingestion_error`` and the frontend grepped
for that substring. That leaked an internal arc identifier into
the cross-repo data contract. The structured code below is the
no-internal-arc-strings-in-user-facing-contracts replacement.

Anchored to the founder's no-internal-arc-strings principle and
the Arc 11 Closeout PR-B spec.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

import app.worker.tasks.crawl_website as crawl_module
from app.models.knowledge_source_errors import IngestionErrorCode


def _non_docstring_str_literals(source: str) -> list[str]:
    """Walk the AST and return every ``ast.Constant(value=str)`` node
    that is NOT a module/class/function docstring. Docstrings are
    user-visible only in admin tooling that surfaces __doc__ — they
    are not part of the cross-repo data contract. String *values*
    (assignments, function arguments, return values, etc.) ARE part
    of the contract surface.
    """
    tree = ast.parse(source)
    docstring_nodes: set[int] = set()
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
                docstring_nodes.add(id(first.value))

    literals: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstring_nodes
        ):
            literals.append(node.value)
    return literals


# The canonical machine-readable code the frontend keys badge
# rendering on. Locked here as a constant so a change in either
# repo without a paired update surfaces as a test failure.
_CONTRACT_ERROR_CODE = "CRAWL_NOT_YET_AVAILABLE"


class TestArc11CrossRepoCrawlContract(unittest.TestCase):
    """Guard the structured error-code contract between backend stub
    and frontend "Coming soon" badge."""

    def _read_source(self) -> str:
        path = Path(crawl_module.__file__)
        return path.read_text(encoding="utf-8")

    def test_enum_carries_the_canonical_code(self):
        """The ``IngestionErrorCode`` enum must define
        ``CRAWL_NOT_YET_AVAILABLE`` with the exact string value the
        frontend matches on. Renaming the enum member or changing
        the string value silently breaks the frontend badge."""
        self.assertEqual(
            IngestionErrorCode.CRAWL_NOT_YET_AVAILABLE.value,
            _CONTRACT_ERROR_CODE,
            "IngestionErrorCode.CRAWL_NOT_YET_AVAILABLE.value must "
            f"equal {_CONTRACT_ERROR_CODE!r}. The frontend's "
            "SourceList.tsx compares ingestion_error_code against "
            "this literal — any drift here turns 'Coming soon' "
            "badges into red 'Failed' badges.",
        )

    def test_crawl_module_references_the_canonical_code(self):
        """The crawl_website module must carry the canonical code as
        a module-level constant. The constant is what the task writes
        to ``knowledge_sources.ingestion_error_code``; if the code is
        only in a comment or docstring, the frontend never sees it."""
        marker = getattr(crawl_module, "_DEFERRED_ERROR_CODE", None)
        self.assertEqual(
            marker, _CONTRACT_ERROR_CODE,
            f"Expected _DEFERRED_ERROR_CODE == {_CONTRACT_ERROR_CODE!r} "
            "on app.worker.tasks.crawl_website. The task writes this "
            "constant to ingestion_error_code; any drift here breaks "
            "the frontend's badge contract.",
        )

    def test_crawl_module_writes_the_code_into_ingestion_error_code(self):
        """The crawl_website task body MUST pass the marker into
        ``mark_status(..., status='failed', error_code=…)`` — not just
        log it. We assert by source-grep for a ``mark_status(...)``
        call carrying ``error_code=_DEFERRED_ERROR_CODE``."""
        src = self._read_source()
        self.assertRegex(
            src,
            r"mark_status\(\s*[\s\S]*?error_code\s*=\s*_DEFERRED_ERROR_CODE",
            "crawl_website must pass _DEFERRED_ERROR_CODE as the "
            "`error_code=` kwarg to mark_status(...). Otherwise the "
            "frontend never sees the structured code and falls back "
            "to a red 'Failed' badge.",
        )

    def test_no_internal_arc_identifier_in_crawl_module(self):
        """No-internal-arc-strings-in-user-facing-contracts: the crawl
        module must not carry an ``Arc-14`` (or similar) identifier as
        a non-docstring string literal. Docstrings legitimately
        mention the history; what we ban is any string *value* — an
        assignment target, a function argument, a return value — that
        would surface to the user or to the cross-repo contract."""
        src = self._read_source()
        offenders = [
            lit for lit in _non_docstring_str_literals(src)
            if "Arc-14" in lit
        ]
        self.assertEqual(
            offenders, [],
            "crawl_website carries an 'Arc-14' non-docstring string "
            f"literal: {offenders!r}. Internal arc identifiers must "
            "not appear in any string the user might surface. Use "
            "the structured IngestionErrorCode instead.",
        )

    def test_human_readable_error_text_has_no_arc_identifier(self):
        """The human-readable ``_DEFERRED_ERROR_MESSAGE`` is written
        to ``ingestion_error`` for ops debugging. It must read as
        plain English with no internal arc identifier."""
        msg = getattr(crawl_module, "_DEFERRED_ERROR_MESSAGE", "")
        self.assertNotRegex(
            msg,
            r"Arc[-\s]?\d+",
            f"_DEFERRED_ERROR_MESSAGE={msg!r} contains an arc "
            "identifier. The ingestion_error text is user-visible "
            "in admin tooling — keep it plain-English.",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
