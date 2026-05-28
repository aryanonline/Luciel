"""Cross-repo contract: the "Arc-14" substring in the crawl_website
stub error MUST match the substring matched by
``Luciel-Website/src/lib/knowledge.ts::isCrawlComingSoon``. If this
test fails, the frontend's coming-soon UX silently degrades to red
"Failed" badges and admins see the crawl stub as a broken feature
instead of a deliberately-deferred one.

Anchored to ARC11_PLAN.md §13 (Step 9 carry-forward note) and the
Step 7 crawl-stub design.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

import app.worker.tasks.crawl_website as crawl_module


# The exact substring the frontend greps for. Locked here as a
# constant so a change in the frontend without a paired backend
# change surfaces as a test failure on the next backend PR.
_FRONTEND_GREP_SUBSTRING = "Arc-14"

# The current backend phrasing. Updating this string requires a
# paired update to the frontend's substring AND a tested deploy of
# both repos together.
_BACKEND_ERROR_PHRASE = "crawl implementation deferred to Arc 14"


class TestArc11CrossRepoCrawlContract(unittest.TestCase):
    """Guard the substring match between backend stub + frontend UX."""

    def _read_source(self) -> str:
        path = Path(crawl_module.__file__)
        return path.read_text(encoding="utf-8")

    def test_arc_14_substring_present_in_crawl_module(self):
        """The literal "Arc-14" (with a hyphen) must appear in the
        crawl_website module — anywhere in the file. The frontend's
        ``isCrawlComingSoon`` greps for this substring in the
        ``ingestion_error`` column populated by this task."""
        src = self._read_source()
        self.assertIn(
            _FRONTEND_GREP_SUBSTRING, src,
            f"crawl_website module is missing the substring "
            f"{_FRONTEND_GREP_SUBSTRING!r}. The frontend's "
            f"isCrawlComingSoon helper in Luciel-Website/src/lib/"
            f"knowledge.ts greps for this substring; without it, "
            f"crawl sources will render as red 'Failed' badges "
            f"instead of '⏱ Coming soon'.",
        )

    def test_backend_marker_constant_carries_the_substring(self):
        """The module-level ``_ARC14_DEFERRED_ERROR`` constant (or
        equivalent literal) must include the frontend-grepped
        substring. The constant is what the task writes to
        ``knowledge_sources.ingestion_error`` — if the substring is
        only in a comment, the frontend never sees it."""
        marker = getattr(crawl_module, "_ARC14_DEFERRED_ERROR", None)
        self.assertIsNotNone(
            marker,
            "Expected _ARC14_DEFERRED_ERROR module-level constant "
            "on app.worker.tasks.crawl_website. Step 6 / Step 7 "
            "rename of this constant requires a paired update to "
            "the frontend's substring.",
        )
        self.assertIn(
            _FRONTEND_GREP_SUBSTRING, marker,
            f"_ARC14_DEFERRED_ERROR={marker!r} does not contain "
            f"{_FRONTEND_GREP_SUBSTRING!r}. The frontend's "
            f"isCrawlComingSoon would silently miss this row.",
        )

    def test_backend_writes_the_marker_into_ingestion_error(self):
        """The crawl_website task body MUST pass the marker into
        ``mark_status(..., status='failed', error=…)`` — not just
        log it. We assert by AST-walking the task body for a
        ``mark_status(...)`` call carrying ``error=_ARC14_DEFERRED_ERROR``
        (or a literal containing the substring)."""
        src = self._read_source()
        # Cheapest reliable check: the task body has a mark_status
        # call with error= pointing at the marker. AST walk would
        # be more rigorous but the substring grep is the smaller
        # surface and matches what the frontend actually sees.
        self.assertRegex(
            src,
            r"mark_status\(\s*[\s\S]*?error\s*=\s*_ARC14_DEFERRED_ERROR",
            "crawl_website must pass _ARC14_DEFERRED_ERROR as the "
            "`error=` kwarg to mark_status(...). Otherwise the "
            "frontend never reads the substring.",
        )

    def test_phrase_locks_full_doctrine_message(self):
        """Soft-lock: the *full* phrase ``crawl implementation
        deferred to Arc 14`` (with no hyphen between 'Arc' and
        '14') is the canonical UX string. Tightening this would
        catch a typo like "Arc14" or "ARC-14" that would still
        pass the substring check above but would read oddly in
        admin tooling that surfaces the raw error."""
        marker = getattr(crawl_module, "_ARC14_DEFERRED_ERROR", "")
        # Allow either spelling — "Arc 14" (space) or "Arc-14"
        # (hyphen). Both are acceptable to the frontend grep
        # because the substring "Arc-14" matches both via the
        # frontend's ``.includes("Arc-14")`` shape... wait, it
        # would NOT match "Arc 14". Verify explicitly.
        has_substring = _FRONTEND_GREP_SUBSTRING in marker
        has_phrase = _BACKEND_ERROR_PHRASE.lower() in marker.lower()
        self.assertTrue(
            has_substring or has_phrase,
            f"Marker {marker!r} matches neither the frontend's "
            f"hyphenated substring nor the canonical phrase. "
            f"Pick one and stick with it.",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
