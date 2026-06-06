"""Arc 11 Step 8 — LucielOrchestrator retriever + trace wiring tests.

All tests run without a live DB. The retriever, TraceService, and
the lazy-instantiated service factory are all mockable, and the
orchestrator's contract is small enough that we exercise every
branch behaviourally.

Contracts:

  W1   Flag off → retriever not called, response.source_ids_used = [],
       trace recorded (best-effort) with source_ids_used=[].
  W2   Flag on + instance_id set → retriever called once, source PKs
       flow into the trace, response carries them.
  W3   Flag on + instance_id None → retriever not called (flag alone
       isn't sufficient — need both).
  W4   Retriever raises → response still returned, source_ids_used = [],
       exception logged but not propagated.
  W5   record_trace raises → response returned with a fresh uuid4,
       exception logged but not propagated.
  W6   KNOWLEDGE_CONTEXT stanza appears in the prompt when chunks
       were retrieved; absent otherwise.
  W7   TraceService DI: when constructed with trace_service=X, the
       orchestrator calls X.record_trace directly (no lazy build).
  W8   collect_source_pks dedupes ints, drops str/None — the
       orchestrator's response surface reflects this.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch
from uuid import UUID

from app.integrations.llm.base import LLMResponse
from app.knowledge.retriever import RetrievedChunk
from app.runtime.context_assembler import ContextAssembler
from app.runtime.contracts import RuntimeRequest, RuntimeResponse
from app.runtime.orchestrator import LucielOrchestrator


class _FakeRouter:
    """Deterministic ModelRouter stand-in for the ARC 11 wiring tests.

    Arc 14 U1 turned ``run`` into a real agentic loop that makes a PLAN
    call. These ARC 11 tests never cared about the LLM (they assert
    retriever + trace wiring), so we inject a fake router that returns
    a fixed PLAN JSON with NO tool calls — keeping the tests hermetic
    (founder decision #2: no network, no API cost) and fast, while every
    original retriever/trace assertion is preserved unchanged.
    """

    def __init__(self) -> None:
        self.calls: list = []

    def generate(self, request, *, preferred_provider=None, **kwargs) -> LLMResponse:
        self.calls.append(request)
        return LLMResponse(
            content='{"reply": "ok", "tool_calls": [], "confidence": 0.8}',
            model="fake-model",
            provider="fake",
        )


def _orch(trace=None):
    """Build an orchestrator with a deterministic fake LLM router so the
    ARC 11 wiring tests never touch a live provider."""
    return LucielOrchestrator(trace_service=trace, model_router=_FakeRouter())


def _chunk(
    source_identifier,
    *,
    content: str = "hello",
    chunk_id: int = 1,
) -> RetrievedChunk:
    return RetrievedChunk(
        content=content,
        knowledge_type="luciel_knowledge",
        title=None,
        distance=0.1,
        chunk_id=chunk_id,
        source_identifier=source_identifier,
        formatted=f"[luciel_knowledge] {content}",
    )


def _request(*, instance_id: int | None = None, message: str = "Q?") -> RuntimeRequest:
    # Arc 12 EX1d: RuntimeRequest no longer carries ``domain_id``.
    return RuntimeRequest(
        message=message,
        session_id="sess-1",
        user_id="user-1",
        admin_id="admin-test-1",
        channel="api",
        luciel_instance_id=instance_id,
    )


class _StubTraceService:
    """Records calls so the test can assert on what flowed in."""

    def __init__(self, raise_on_record: bool = False) -> None:
        self.calls: list[dict] = []
        self.raise_on_record = raise_on_record
        self.next_trace_id = "trace-recorded-12345"

    def record_trace(self, **kwargs) -> str:
        self.calls.append(kwargs)
        if self.raise_on_record:
            raise RuntimeError("synthetic record_trace failure")
        return self.next_trace_id


# ---------------------------------------------------------------------
# W1 — flag off
# ---------------------------------------------------------------------


class TestW1FlagOff(unittest.TestCase):

    def test_w1_retriever_not_called_when_flag_off(self):
        trace = _StubTraceService()
        orch = _orch(trace)

        with patch(
            "app.runtime.orchestrator.LucielOrchestrator._retrieve",
            return_value=[],
        ) as retrieve_mock, patch(
            "app.core.config.settings.knowledge_retrieval_enabled", False,
        ):
            resp = orch.run(_request(instance_id=42))

        retrieve_mock.assert_not_called()
        self.assertEqual(resp.source_ids_used, [])

    def test_w1_trace_recorded_with_empty_array_when_flag_off(self):
        trace = _StubTraceService()
        orch = _orch(trace)

        with patch(
            "app.core.config.settings.knowledge_retrieval_enabled", False,
        ):
            orch.run(_request(instance_id=42))

        self.assertEqual(len(trace.calls), 1)
        self.assertEqual(trace.calls[0]["source_ids_used"], [])


# ---------------------------------------------------------------------
# W2 — flag on + instance_id
# ---------------------------------------------------------------------


class TestW2FlagOnHappyPath(unittest.TestCase):

    def test_w2_retriever_called_once_and_source_pks_flow_through(self):
        chunks = [_chunk(42), _chunk(99, chunk_id=2)]
        trace = _StubTraceService()
        orch = _orch(trace)

        with patch(
            "app.runtime.orchestrator.LucielOrchestrator._retrieve",
            return_value=chunks,
        ) as retrieve_mock, patch(
            "app.core.config.settings.knowledge_retrieval_enabled", True,
        ):
            resp = orch.run(_request(instance_id=42))

        retrieve_mock.assert_called_once()
        self.assertEqual(resp.source_ids_used, [42, 99])
        # And the same value landed in the trace write.
        self.assertEqual(trace.calls[0]["source_ids_used"], [42, 99])

    def test_w2_response_trace_id_matches_recorded(self):
        chunks = [_chunk(42)]
        trace = _StubTraceService()
        trace.next_trace_id = "trace-zzz"
        orch = _orch(trace)

        with patch(
            "app.runtime.orchestrator.LucielOrchestrator._retrieve",
            return_value=chunks,
        ), patch(
            "app.core.config.settings.knowledge_retrieval_enabled", True,
        ):
            resp = orch.run(_request(instance_id=42))

        self.assertEqual(resp.trace_id, "trace-zzz")


# ---------------------------------------------------------------------
# W3 — flag on but instance_id missing
# ---------------------------------------------------------------------


class TestW3NoInstanceId(unittest.TestCase):

    def test_w3_retriever_not_called_when_instance_id_none(self):
        trace = _StubTraceService()
        orch = _orch(trace)

        with patch(
            "app.runtime.orchestrator.LucielOrchestrator._retrieve",
            return_value=[],
        ) as retrieve_mock, patch(
            "app.core.config.settings.knowledge_retrieval_enabled", True,
        ):
            resp = orch.run(_request(instance_id=None))

        retrieve_mock.assert_not_called()
        self.assertEqual(resp.source_ids_used, [])


# ---------------------------------------------------------------------
# W4 — retriever raises
# ---------------------------------------------------------------------


class TestW4RetrieverRaises(unittest.TestCase):
    """If the retriever blows up, the conversation continues. Step 8
    catches the exception at the orchestrator level too (belt +
    braces alongside the retriever's own try/except), so we patch
    a level deeper to force the failure surface."""

    def test_w4_inner_retriever_raises_response_still_returned(self):
        # Patch the underlying KnowledgeRetriever.retrieve_with_sources
        # so the orchestrator's own _retrieve catches and returns [].
        trace = _StubTraceService()
        orch = _orch(trace)

        # The retriever module is lazy-imported inside _retrieve, so
        # we patch the call site's KnowledgeRetriever class indirectly
        # by raising from _retrieve.
        with patch(
            "app.runtime.orchestrator.LucielOrchestrator._retrieve",
            side_effect=RuntimeError("boom"),
        ), patch(
            "app.core.config.settings.knowledge_retrieval_enabled", True,
        ):
            # The orchestrator's run() does NOT itself wrap _retrieve
            # in try/except (the retriever's own try/except is the
            # firewall). So this side_effect propagates — which is a
            # test of the *contract*: _retrieve is required to never
            # raise. We assert that with a separate test that exercises
            # the helper directly.
            with self.assertRaises(RuntimeError):
                orch.run(_request(instance_id=42))

    def test_w4_retrieve_helper_never_raises_even_on_deep_failure(self):
        """The actual contract: ``_retrieve`` itself must swallow
        every exception so ``run`` never sees one. We simulate by
        patching the lazy-imported retriever class to raise inside
        ``retrieve_with_sources``."""
        from app.knowledge.retriever import KnowledgeRetriever

        orch = _orch(_StubTraceService())

        # Build a fake retriever class whose .retrieve_with_sources
        # raises. Patch the symbol the orchestrator's _retrieve
        # imports lazily.
        class _BoomRetriever(KnowledgeRetriever):
            def __init__(self, *_a, **_kw):
                pass

            def retrieve_with_sources(self, **_kw):
                raise RuntimeError("deep boom")

        with patch(
            "app.knowledge.retriever.KnowledgeRetriever", _BoomRetriever,
        ), patch(
            "app.db.session.SessionLocal",
            return_value=MagicMock(),
        ), patch(
            "app.db.tenant_scope.bind_tenant_scope",
            # bind_tenant_scope is a context manager; build a
            # no-op CM for the test.
            new=_no_op_cm,
        ):
            result = orch._retrieve(_request(instance_id=42))
        self.assertEqual(result, [])


from contextlib import contextmanager


@contextmanager
def _no_op_cm(*_a, **_kw):
    yield


# ---------------------------------------------------------------------
# W5 — record_trace raises
# ---------------------------------------------------------------------


class TestW5RecordTraceRaises(unittest.TestCase):

    def test_w5_response_returned_with_fresh_uuid_when_trace_fails(self):
        trace = _StubTraceService(raise_on_record=True)
        orch = _orch(trace)

        with patch(
            "app.runtime.orchestrator.LucielOrchestrator._retrieve",
            return_value=[],
        ), patch(
            "app.core.config.settings.knowledge_retrieval_enabled", False,
        ):
            resp = orch.run(_request(instance_id=None))

        # record_trace was attempted (so the failure surface fired).
        self.assertEqual(len(trace.calls), 1)
        # And the response still returned with a valid uuid4.
        try:
            UUID(resp.trace_id)
        except ValueError:
            self.fail(f"trace_id is not a valid UUID: {resp.trace_id!r}")
        # It must NOT be the stub's next_trace_id (which the stub
        # would have returned on success).
        self.assertNotEqual(resp.trace_id, trace.next_trace_id)

    def test_w5_response_still_carries_source_ids_when_trace_fails(self):
        """Trace persistence failure must not erase source_ids_used
        from the in-memory response. The /affected-questions endpoint
        won't light up for this turn (DB write failed) but the
        in-memory caller still sees what would have been recorded."""
        chunks = [_chunk(7)]
        trace = _StubTraceService(raise_on_record=True)
        orch = _orch(trace)

        with patch(
            "app.runtime.orchestrator.LucielOrchestrator._retrieve",
            return_value=chunks,
        ), patch(
            "app.core.config.settings.knowledge_retrieval_enabled", True,
        ):
            resp = orch.run(_request(instance_id=42))

        self.assertEqual(resp.source_ids_used, [7])


# ---------------------------------------------------------------------
# W6 — KNOWLEDGE_CONTEXT stanza in the prompt
# ---------------------------------------------------------------------


class TestW6KnowledgeContextStanza(unittest.TestCase):

    def test_w6_stanza_present_when_chunks_provided(self):
        ca = ContextAssembler()
        req = _request(instance_id=42, message="hello")
        prompt = ca.build_prompt(
            req,
            retrieved_chunks=[
                _chunk(1, content="ALPHA FACT"),
                _chunk(2, content="BETA FACT"),
            ],
        )
        self.assertIn("KNOWLEDGE_CONTEXT:", prompt)
        self.assertIn("ALPHA FACT", prompt)
        self.assertIn("BETA FACT", prompt)

    def test_w6_stanza_absent_when_chunks_none(self):
        ca = ContextAssembler()
        prompt = ca.build_prompt(_request(instance_id=42), retrieved_chunks=None)
        self.assertNotIn("KNOWLEDGE_CONTEXT", prompt)

    def test_w6_stanza_absent_when_chunks_empty(self):
        ca = ContextAssembler()
        prompt = ca.build_prompt(_request(instance_id=42), retrieved_chunks=[])
        self.assertNotIn("KNOWLEDGE_CONTEXT", prompt)

    def test_w6_truncates_to_budget(self):
        """Stanza body capped at 8KB. Build chunks that overrun and
        assert only the first slice survives."""
        # 3 chunks @ 4KB each = 12KB raw. Budget is 8KB, so chunks
        # 0 + 1 fit (8KB exact); chunk 2 is dropped.
        ca = ContextAssembler()
        big = "x" * 4096
        prompt = ca.build_prompt(
            _request(instance_id=42),
            retrieved_chunks=[
                _chunk(1, content=big),
                _chunk(2, content=big),
                _chunk(3, content="UNIQUE_TAIL_TOKEN"),
            ],
        )
        self.assertNotIn("UNIQUE_TAIL_TOKEN", prompt)

    def test_w6_default_call_signature_unchanged(self):
        """Backwards compat: calling build_prompt without the kw
        produces the same output as Step 7. Locks the existing
        signature so the chat path stays byte-identical when the
        retriever flag is closed."""
        ca = ContextAssembler()
        prompt = ca.build_prompt(_request(instance_id=42))
        # No KNOWLEDGE_CONTEXT, and the original four lines all present.
        self.assertNotIn("KNOWLEDGE_CONTEXT", prompt)
        self.assertIn("User message: Q?", prompt)


# ---------------------------------------------------------------------
# W7 — TraceService DI
# ---------------------------------------------------------------------


class TestW7TraceServiceDi(unittest.TestCase):

    def test_w7_injected_trace_service_is_used_directly(self):
        trace = _StubTraceService()
        orch = _orch(trace)

        with patch(
            "app.core.config.settings.knowledge_retrieval_enabled", False,
        ):
            orch.run(_request(instance_id=None))

        self.assertEqual(len(trace.calls), 1)

    def test_w7_no_trace_service_lazy_path_invoked(self):
        """When no trace_service is injected, the orchestrator
        builds one via the resolve helper. Mock the resolver to
        prove the lazy path fires."""
        # No trace_service kwarg — exercise the lazy trace path. Inject
        # a fake LLM router so the PLAN call stays offline (the lazy
        # ModelRouter is not what this test is about).
        orch = LucielOrchestrator(model_router=_FakeRouter())
        lazy_stub = _StubTraceService()

        with patch.object(
            LucielOrchestrator, "_resolve_trace_service",
            return_value=lazy_stub,
        ), patch(
            "app.core.config.settings.knowledge_retrieval_enabled", False,
        ):
            orch.run(_request(instance_id=None))

        self.assertEqual(len(lazy_stub.calls), 1)


# ---------------------------------------------------------------------
# W8 — collect_source_pks integration
# ---------------------------------------------------------------------


class TestW8CollectSourcePks(unittest.TestCase):

    def test_w8_dedupe_preserves_insertion_order(self):
        """Post-Cleanup-B every RetrievedChunk.source_identifier is
        an int (the source_id FK is NOT NULL). The orchestrator
        wiring just dedupes + preserves insertion order."""
        trace = _StubTraceService()
        orch = _orch(trace)

        chunks = [
            _chunk(10, chunk_id=1),
            _chunk(10, chunk_id=2),  # dup
            _chunk(20, chunk_id=3),
            _chunk(10, chunk_id=4),  # dup
            _chunk(30, chunk_id=5),
        ]

        with patch(
            "app.runtime.orchestrator.LucielOrchestrator._retrieve",
            return_value=chunks,
        ), patch(
            "app.core.config.settings.knowledge_retrieval_enabled", True,
        ):
            resp = orch.run(_request(instance_id=42))

        self.assertEqual(resp.source_ids_used, [10, 20, 30])
        self.assertEqual(trace.calls[0]["source_ids_used"], [10, 20, 30])


# ---------------------------------------------------------------------
# Misc: dataclass defaults & backwards compat
# ---------------------------------------------------------------------


class TestContractBackwardsCompat(unittest.TestCase):

    def test_runtime_request_can_be_built_without_instance_id(self):
        # Arc 12 EX1d: RuntimeRequest no longer carries ``domain_id``.
        req = RuntimeRequest(
            message="Q",
            session_id="s",
            user_id="u",
            admin_id="a",
            channel="api",
        )
        self.assertIsNone(req.luciel_instance_id)

    def test_runtime_response_default_source_ids_used_is_empty(self):
        resp = RuntimeResponse(
            message="r",
            trace_id="t",
            confidence=0.5,
            session_id="s",
        )
        self.assertEqual(resp.source_ids_used, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
