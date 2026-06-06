"""Unit 13e §3.4.10 — cross-session memory injection bound.

Pure tests on the deterministic injection-bound:
  * N=10 default cap.
  * 12-month rolling window excludes older summaries.
  * Recency-precedence picks the newer fact on conflict (same lead).
  * Token ceiling stops bulk-loading.
  * Platform constants are the §3.4.10 values (not the old 20/100).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.memory.cross_session_retriever import (
    DEFAULT_RECENT_SUMMARIES_N,
    INJECTION_TOKEN_CEILING,
    RECALL_WINDOW_DAYS,
    SummaryRecord,
    bound_summaries_for_injection,
)

_NOW = datetime(2026, 6, 6, tzinfo=timezone.utc)


def _summary(*, lead="lead-1", days_ago=0, text="s", facts=None):
    return SummaryRecord(
        resolved_lead_id=lead,
        session_id=f"sess-{days_ago}-{lead}",
        summary=text,
        created_at=_NOW - timedelta(days=days_ago),
        facts=facts,
    )


def test_platform_constants_are_the_doc_values():
    assert DEFAULT_RECENT_SUMMARIES_N == 10
    assert RECALL_WINDOW_DAYS == 365
    assert INJECTION_TOKEN_CEILING > 0


def test_n_equals_10_cap():
    # 15 recent summaries, all in-window, all tiny — only 10 are loaded.
    summaries = [_summary(days_ago=i, text="x") for i in range(15)]
    selected, _ = bound_summaries_for_injection(summaries, now=_NOW)
    assert len(selected) == 10
    # Newest-first: the 10 most-recent (days_ago 0..9).
    ages = [(_NOW - s.created_at).days for s in selected]
    assert ages == sorted(ages)
    assert max(ages) == 9


def test_twelve_month_window_excludes_older():
    summaries = [
        _summary(days_ago=10, text="recent"),
        _summary(days_ago=400, text="too-old"),  # > 365 days
    ]
    selected, _ = bound_summaries_for_injection(summaries, now=_NOW)
    texts = {s.summary for s in selected}
    assert "recent" in texts
    assert "too-old" not in texts


def test_recency_precedence_newer_fact_wins():
    older = _summary(
        lead="lead-A", days_ago=30, text="old",
        facts={"budget": "100k", "city": "Markham"},
    )
    newer = _summary(
        lead="lead-A", days_ago=2, text="new",
        facts={"budget": "250k"},  # conflicts with older budget
    )
    selected, resolved = bound_summaries_for_injection(
        [older, newer], now=_NOW
    )
    # Newest-first ordering.
    assert selected[0].summary == "new"
    # Recency-precedence: newer budget wins; non-conflicting city retained.
    assert resolved["lead-A"]["budget"] == "250k"
    assert resolved["lead-A"]["city"] == "Markham"


def test_token_ceiling_stops_loading():
    # Each summary ~ ceiling/2 tokens; only ~2 fit before the ceiling.
    big_text = "a" * (INJECTION_TOKEN_CEILING * 4 // 2)  # ~ceiling/2 tokens
    summaries = [_summary(days_ago=i, text=big_text) for i in range(10)]
    selected, _ = bound_summaries_for_injection(summaries, now=_NOW)
    assert 1 <= len(selected) <= 3
    assert len(selected) < 10


def test_per_lead_fact_isolation():
    # Two different leads — facts must not bleed across leads.
    a = _summary(lead="A", days_ago=1, facts={"budget": "1"})
    b = _summary(lead="B", days_ago=1, facts={"budget": "2"})
    _, resolved = bound_summaries_for_injection([a, b], now=_NOW)
    assert resolved["A"]["budget"] == "1"
    assert resolved["B"]["budget"] == "2"
