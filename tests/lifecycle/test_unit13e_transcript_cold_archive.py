"""Unit 13e §3.4.10 — transcript cold-archive policy (SELECT/mark leg).

Pure tests on the deterministic 90-day cold-archive horizon selector.
The actual S3 move is flagged deploy-phase; this proves the eligibility
decision is deterministic and testable without a DB or S3.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.lifecycle.transcript_cold_archive import (
    TRANSCRIPT_COLD_ARCHIVE_DAYS,
    select_cold_archive_candidates,
)

_NOW = datetime(2026, 6, 6, tzinfo=timezone.utc)


@dataclass
class _FakeSession:
    id: str
    admin_id: str
    luciel_instance_id: int | None
    updated_at: datetime


def _sess(*, sid="s", admin="a", inst=1, days_ago=0):
    return _FakeSession(
        id=sid,
        admin_id=admin,
        luciel_instance_id=inst,
        updated_at=_NOW - timedelta(days=days_ago),
    )


def test_horizon_constant_is_90_days():
    assert TRANSCRIPT_COLD_ARCHIVE_DAYS == 90


def test_past_90d_eligible_fresh_excluded():
    sessions = [
        _sess(sid="old", days_ago=120),
        _sess(sid="fresh", days_ago=30),
        _sess(sid="edge-under", days_ago=89),
        _sess(sid="edge-over", days_ago=91),
    ]
    candidates = select_cold_archive_candidates(sessions, now=_NOW)
    ids = {c.session_id for c in candidates}
    assert "old" in ids
    assert "edge-over" in ids
    assert "fresh" not in ids
    assert "edge-under" not in ids


def test_candidate_carries_scope_provenance():
    sessions = [_sess(sid="x", admin="tenant-7", inst=42, days_ago=200)]
    (cand,) = select_cold_archive_candidates(sessions, now=_NOW)
    assert cand.session_id == "x"
    assert cand.admin_id == "tenant-7"
    assert cand.luciel_instance_id == 42


def test_none_last_activity_skipped():
    s = _sess(sid="noact", days_ago=200)
    s.updated_at = None
    candidates = select_cold_archive_candidates([s], now=_NOW)
    assert candidates == []
