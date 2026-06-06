"""Arc 11 Step 5 — TraceService.record_trace gains source_ids_used.

Two surfaces to lock:

  S1  ``collect_source_pks`` reduces a list of ``RetrievedChunk``
      to a deduped ``list[int]`` preserving relevance-rank order.
  S2  ``TraceService.record_trace(..., source_ids_used=[...])``
      writes the list to ``Trace.source_ids_used``; omitting it
      writes ``[]``.

S1 is pure; tested in-memory with synthetic ``RetrievedChunk``
instances. No DB, no fixtures.

S2 is partly testable in-memory too: we substitute a fake
``TraceRepository`` that just records the ``Trace`` instance it
was handed, then inspect the instance's ``source_ids_used``
attribute. No live Postgres needed (the ``ARRAY(BigInteger)``
column accepts a Python list at construct-time even outside a
session). This mirrors the established service-test pattern in
``tests/services/test_knowledge_ingestion_two_table.py`` from
Arc 11 Step 3.
"""
from __future__ import annotations

import unittest
from typing import Any

from app.runtime.knowledge_retrieval import RetrievedChunk, collect_source_pks
from app.models.trace import Trace
from app.services.trace_service import TraceService


def _chunk(
    source_identifier: int,
    *,
    chunk_id: int = 1,
    distance: float = 0.0,
) -> RetrievedChunk:
    """Build a minimal ``RetrievedChunk`` for the helper tests.

    Post-Cleanup-B: ``source_identifier`` is int-only; the legacy
    ``str | None`` cases (which the helper used to filter out) are
    impossible because the chunk-side ``source_id`` FK is NOT NULL.
    """
    return RetrievedChunk(
        content="content",
        knowledge_type="luciel_knowledge",
        title=None,
        distance=distance,
        chunk_id=chunk_id,
        source_identifier=source_identifier,
        formatted="[luciel_knowledge] content",
    )


class _FakeTraceRepository:
    """Drop-in for ``TraceRepository`` that captures the Trace it
    was handed without touching a session."""

    def __init__(self) -> None:
        self.saved: list[Trace] = []

    def save_trace(self, trace: Trace) -> Trace:
        self.saved.append(trace)
        return trace


# ---------------------------------------------------------------------
# S1 — collect_source_pks
# ---------------------------------------------------------------------


class TestCollectSourcePks(unittest.TestCase):
    """The dedup + filter contract spelled out in the Step 5 brief."""

    def test_empty_input_returns_empty_list(self):
        self.assertEqual(collect_source_pks([]), [])

    def test_keeps_int_ids(self):
        out = collect_source_pks([_chunk(42), _chunk(7)])
        self.assertEqual(out, [42, 7])

    def test_dedupes_preserving_insertion_order(self):
        """The first chunk that contributed a source ranks higher
        than later chunks reusing the same source. Architecture §5.1
        observability semantics."""
        chunks = [
            _chunk(42, chunk_id=1, distance=0.1),
            _chunk(99, chunk_id=2, distance=0.2),
            _chunk(42, chunk_id=3, distance=0.3),
            _chunk(7,  chunk_id=4, distance=0.4),
            _chunk(99, chunk_id=5, distance=0.5),
        ]
        self.assertEqual(collect_source_pks(chunks), [42, 99, 7])

    def test_dedupes_with_repeats_preserves_insertion_order(self):
        """Post-Cleanup-B every source_identifier is an int (the
        ``knowledge_sources.id`` FK is NOT NULL). The helper's only
        responsibility is dedupe + insertion-order preservation."""
        chunks = [
            _chunk(10, chunk_id=1),
            _chunk(10, chunk_id=2),
            _chunk(20, chunk_id=3),
            _chunk(10, chunk_id=4),
            _chunk(30, chunk_id=5),
        ]
        self.assertEqual(collect_source_pks(chunks), [10, 20, 30])

    def test_return_type_is_list_of_int(self):
        out = collect_source_pks([_chunk(1), _chunk(2)])
        self.assertIsInstance(out, list)
        for v in out:
            self.assertIsInstance(v, int)

    def test_pure_function_does_not_mutate_input(self):
        """Defence: the helper must not mutate caller-owned chunks."""
        chunks = [_chunk(1), _chunk(2), _chunk(2)]
        before = [c.source_identifier for c in chunks]
        collect_source_pks(chunks)
        after = [c.source_identifier for c in chunks]
        self.assertEqual(before, after)


# ---------------------------------------------------------------------
# S2 — TraceService.record_trace
# ---------------------------------------------------------------------


class TestRecordTraceSourceIdsUsed(unittest.TestCase):
    """``record_trace`` must persist ``source_ids_used`` exactly as
    handed in (modulo de-dup, which is the helper's job — the
    service is a passthrough)."""

    def _make_service(self) -> tuple[TraceService, _FakeTraceRepository]:
        repo = _FakeTraceRepository()
        return TraceService(repo), repo  # type: ignore[arg-type]

    def _base_kwargs(self) -> dict[str, Any]:
        return {
            "session_id": "sess-1",
            "user_id": "user-1",
            "admin_id": "admin-1",
            "user_message": "hi",
            "assistant_reply": "hello",
            "luciel_instance_id": 100,
        }

    def test_source_ids_used_persisted_when_provided(self):
        svc, repo = self._make_service()
        svc.record_trace(**self._base_kwargs(), source_ids_used=[1, 2, 3])
        self.assertEqual(len(repo.saved), 1)
        self.assertEqual(repo.saved[0].source_ids_used, [1, 2, 3])

    def test_source_ids_used_default_is_empty_list(self):
        """Omitting the kwarg → ``[]`` (matches DB ``DEFAULT '{}'``).
        Crucial: must NEVER store ``None`` because the column is
        ``NOT NULL``; an INSERT with NULL would fail at the DB."""
        svc, repo = self._make_service()
        svc.record_trace(**self._base_kwargs())
        self.assertEqual(repo.saved[0].source_ids_used, [])

    def test_source_ids_used_none_normalised_to_empty_list(self):
        """Explicit ``None`` also normalises to ``[]``."""
        svc, repo = self._make_service()
        svc.record_trace(**self._base_kwargs(), source_ids_used=None)
        self.assertEqual(repo.saved[0].source_ids_used, [])

    def test_empty_list_passes_through(self):
        svc, repo = self._make_service()
        svc.record_trace(**self._base_kwargs(), source_ids_used=[])
        self.assertEqual(repo.saved[0].source_ids_used, [])

    def test_source_ids_used_is_copied_not_shared(self):
        """The Trace must not hold the caller's list reference —
        otherwise a later mutation of the caller-owned list would
        retroactively change the Trace's recorded sources. Step 8's
        orchestrator builds the list via ``collect_source_pks`` and
        might reuse the buffer; the defensive copy is cheap and
        worth pinning."""
        svc, repo = self._make_service()
        external = [1, 2]
        svc.record_trace(**self._base_kwargs(), source_ids_used=external)
        external.append(999)
        self.assertEqual(
            repo.saved[0].source_ids_used,
            [1, 2],
            "Trace.source_ids_used shares storage with caller's list",
        )

    def test_record_trace_signature_has_source_ids_used_kwonly(self):
        """Locking the kw-only nature so positional drift can't
        silently re-order other arguments through it."""
        import inspect

        sig = inspect.signature(TraceService.record_trace)
        param = sig.parameters.get("source_ids_used")
        self.assertIsNotNone(param, "record_trace must accept source_ids_used")
        self.assertEqual(
            param.kind,
            inspect.Parameter.KEYWORD_ONLY,
            "source_ids_used must be keyword-only",
        )
        self.assertIsNone(
            param.default,
            "source_ids_used default must be None (normalised to [] inside)",
        )

    def test_memories_used_independent_of_source_ids_used(self):
        """Two parallel observability surfaces; pinning that they
        don't accidentally share state. Per the Step 5 brief:
        ``memories_used`` is conversation history; ``source_ids_used``
        is knowledge-base sources. They stay independent."""
        svc, repo = self._make_service()
        svc.record_trace(
            **self._base_kwargs(),
            memories_used=["mem-a", "mem-b"],
            source_ids_used=[1, 2, 3],
        )
        t = repo.saved[0]
        self.assertEqual(t.memories_used, ["mem-a", "mem-b"])
        self.assertEqual(t.source_ids_used, [1, 2, 3])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
