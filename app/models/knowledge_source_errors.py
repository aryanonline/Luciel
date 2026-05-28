"""Canonical ingestion-error codes for ``knowledge_sources.ingestion_error_code``.

Arc 11 Closeout PR-B introduced this column to replace an earlier
substring-sniff contract (the frontend used to grep
``ingestion_error.includes("Arc-14")``). The structured code is what
the frontend keys badge rendering on now; the human-readable
``ingestion_error`` text column survives for ops debugging but is
NOT part of the cross-repo contract.

Cross-repo contract
-------------------

These values are part of the public API contract surfaced via
``KnowledgeSourceRead.ingestion_error_code``. The frontend in
``Luciel-Website/src/components/knowledge/SourceList.tsx`` matches
on the literal string values defined here. Rules:

* Never reuse a code value for a different meaning.
* Never put an arc identifier (``Arc-14``, ``Arc-15`` …) into a
  code value — the whole point of this enum is to keep internal
  arc identifiers out of the user-facing contract.
* Names should describe the user-visible condition, not the
  implementation arc.
* Adding a new code requires a paired frontend update.
"""
from __future__ import annotations

from enum import Enum


class IngestionErrorCode(str, Enum):
    """Canonical values for ``knowledge_sources.ingestion_error_code``."""

    CRAWL_NOT_YET_AVAILABLE = "CRAWL_NOT_YET_AVAILABLE"
    """Written by the crawl-website stub task. The frontend renders a
    "⏱ Coming soon" badge for this code. Real crawler ships in Arc 14;
    when it does, this code stops being written on new rows but
    remains a legal historical value."""


__all__ = ["IngestionErrorCode"]
