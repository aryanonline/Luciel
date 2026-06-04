"""RESCAN CORE(serving-path) — CRUX live-path gate tests.

These tests prove the SIX audit gaps are closed AT THE SOURCE on the
LIVE serving path. They drive the ACTUAL live entry points —
``ChatService.respond`` and ``ChatService.respond_stream`` — NOT
``LucielOrchestrator.run`` directly. ChatService is now a THIN adapter
that builds a ``RuntimeRequest`` and delegates the gated turn to the
orchestrator (the §3.4.1 agentic loop), so every gate the orchestrator
enforces is exercised through the same path a real /chat request takes.

  GAP-1  budget gate §3.4.1b      — Free at cap → graceful no-LLM,
                                     budget_exhausted acknowledgement.
  GAP-2  human-controlled §3.4.12 — human_controlled session → no
                                     auto-response, ZERO LLM calls.
  GAP-3  grounding floors §3.4    — weak retrieval + low confidence →
                                     canonical "I don't have that
                                     information" (§3.4.13), never an
                                     ungrounded answer.
  GAP-4  tier-gated broker §3.3.4 — off-tier tool call → server-side
                                     DENIED (default-deny ToolResult
                                     success=False), reasoned about by
                                     REFLECT.
  GAP-5  downgrade→inactive §3.6.7— an ``inactive`` instance → lifecycle
                                     no-op, no response, no budget (same
                                     gate as GAP-6, status='inactive').
  GAP-6  /chat lifecycle §3.6     — paused / missing instance → lifecycle
                                     no-op (empty reply), no LLM, no
                                     budget; streaming plays back the
                                     grounded final text verbatim.

All deterministic: fakes for session/memory/consent/instance, an
InMemoryBackend budget meter, a scripted router, and patched DB seams.
No Postgres, no network, no LLM.
"""
from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MODERATION_PROVIDER", "null")
os.environ.setdefault("OPENAI_API_KEY", "dummy")

from app.channels.base import InactiveInstanceDrop
from app.integrations.llm.base import LLMResponse
from app.runtime.budget_ack import budget_exhausted_acknowledgement
from app.runtime.budget_meter import BudgetMeter, InMemoryBackend
from app.runtime.handoff_ack import CANNOT_ANSWER_REPLY
from app.services.chat_service import ChatService


# =====================================================================
# Deterministic fakes for the ChatService adapter collaborators.
# =====================================================================


class _Msg:
    def __init__(self, role, content, mid=1):
        self.role = role
        self.content = content
        self.id = mid


class _Session:
    def __init__(self, admin_id="admin-1", user_id="user-1", channel="web"):
        self.admin_id = admin_id
        self.user_id = user_id
        self.channel = channel


class _FakeSessionService:
    def __init__(self, session):
        self._session = session
        self.added: list[_Msg] = []

    def get_session(self, session_id):
        return self._session

    def add_message(self, *, session_id, role, content):
        msg = _Msg(role, content, mid=len(self.added) + 1)
        self.added.append(msg)
        return msg

    def list_messages(self, session_id):
        return list(self.added)


class _FakeMemoryService:
    def __init__(self):
        self.extracted = []

    def retrieve_memories(self, *, user_id, admin_id):
        return []

    def extract_and_save(self, **kwargs):
        self.extracted.append(kwargs)
        return 0


class _FakeConsent:
    def can_persist_memory(self, *, user_id, admin_id):
        return True


class _FakeInstanceRepo:
    """Resolves an active instance binding so the adapter threads the
    instance id onto the RuntimeRequest. The lifecycle gate (which keys
    off instance_status, not this repo) is patched separately per test."""

    class _DB:
        def execute(self, *a, **k):
            class _R:
                def scalar_one_or_none(self_inner):
                    return "free"
            return _R()

    def __init__(self, instance_id=7, admin_id="admin-1"):
        self._iid = instance_id
        self._admin = admin_id
        self.db = self._DB()

    def get_by_pk(self, pk):
        if pk != self._iid:
            return None
        return type(
            "Inst",
            (),
            {
                "id": self._iid,
                "active": True,
                "admin_id": self._admin,
                "display_name": "Luciel",
                "preferred_provider": None,
                "personality_preset": None,
                "personality_axes": None,
                "business_context": None,
            },
        )()


class _ScriptedRouter:
    """ModelRouter stand-in returning scripted PLAN responses."""

    def __init__(self, contents, *, provider="stub", model="stub-x"):
        self._contents = contents
        self._provider = provider
        self._model = model
        self.calls = []

    def generate(self, request, *, preferred_provider=None):
        idx = min(len(self.calls), len(self._contents) - 1)
        self.calls.append(request)
        return LLMResponse(
            content=self._contents[idx], model=self._model, provider=self._provider
        )


class _NoopRetriever:
    def retrieve(self, *a, **k):
        return []


class _NoopRegistry:
    def get_tool_descriptions(self):
        return None


class _NoopConfigRepo:
    pass


class _IntakeSilentJudge:
    """A judge that suppresses ONLY the router-backed INTAKE gate while
    delegating the OUTCOME gate to the real, deterministic logic.

    The real EscalationJudge shares the orchestrator's router; its INTAKE
    classifiers (intent + sentiment) each consume a ``generate()`` call
    BEFORE the PLAN loop runs. In these deterministic gate tests the router
    is scripted with PLAN responses only, so letting the intake gate run
    would silently eat the scripted PLAN entries (e.g. the GAP-4 tool-call
    script would be consumed by the intent classifier, never reaching the
    loop). We therefore stub ``evaluate_intake`` to None.

    The OUTCOME gate's cannot-confidently-answer check is PURELY
    deterministic (confidence + grounding floor — no router), and GAP-3
    depends on it firing, so we delegate ``evaluate_outcome`` to a REAL
    EscalationJudge instance. This keeps the grounding-floor substitution
    on the LIVE path under test while still isolating the scripted router
    to the loop's PLAN calls."""

    def __init__(self):
        from app.runtime.escalation_judge import EscalationJudge

        self._real = EscalationJudge()

    def evaluate_intake(self, req):
        return None

    def evaluate_outcome(self, req, outcome):
        return self._real.evaluate_outcome(req, outcome)


def _plan_json(reply="planned answer", tool_calls=None, confidence=0.9):
    return json.dumps(
        {"reply": reply, "tool_calls": tool_calls or [], "confidence": confidence}
    )


_INSTANCE_ID = 7


def _make_chat_service(*, router, broker=None, instance_id=_INSTANCE_ID):
    from app.tools.broker import ToolBroker
    from app.tools.registry import ToolRegistry

    class _StubTrace:
        def __init__(self):
            self.calls = []

        def record_trace(self, **kwargs):
            self.calls.append(kwargs)
            return "trace-fixed-id"

    svc = ChatService(
        session_service=_FakeSessionService(_Session()),
        memory_service=_FakeMemoryService(),
        model_router=router,
        tool_registry=_NoopRegistry(),
        tool_broker=broker or ToolBroker(ToolRegistry()),
        trace_service=_StubTrace(),
        knowledge_retriever=_NoopRetriever(),
        config_repository=_NoopConfigRepo(),
        instance_repository=_FakeInstanceRepo(instance_id=instance_id),
        consent_policy=_FakeConsent(),
    )

    # Bind the instance id onto every live-path call so the orchestrator's
    # §3.6 lifecycle gate (which only runs when an instance id resolves)
    # is actually exercised — a real /chat request carries this on
    # request.state.luciel_instance_id.
    _respond = svc.respond
    _respond_stream = svc.respond_stream
    svc.respond = lambda **kw: _respond(luciel_instance_id=instance_id, **kw)
    svc.respond_stream = lambda **kw: _respond_stream(
        luciel_instance_id=instance_id, **kw
    )
    return svc


# Patch context: neutralise the orchestrator's DB-touching seams so the
# live path runs without Postgres. The lifecycle gate, human-controlled
# gate, retrieval, and billing context are each patched per the test's
# need; the budget meter is injected through a patched BudgetMeter.
class _LivePath:
    """Patches the orchestrator's DB seams for a deterministic live run.

    - lifecycle_drop: InactiveInstanceDrop or None (the §3.6 gate result)
    - human_controlled: bool
    - retrieval_enabled / chunks: drive the grounding/retrieval-failed leg
    - billing: (tier, cap-preload) for the budget gate; None disables it
    - meter: an injected BudgetMeter (InMemoryBackend) or None
    """

    def __init__(
        self,
        *,
        lifecycle_drop=None,
        human_controlled=False,
        retrieval_enabled=False,
        chunks=None,
        billing_tier=None,
        meter=None,
    ):
        self.lifecycle_drop = lifecycle_drop
        self.human_controlled = human_controlled
        self.retrieval_enabled = retrieval_enabled
        self.chunks = chunks or []
        self.billing_tier = billing_tier
        self.meter = meter
        self._patches = []

    def __enter__(self):
        from app.runtime.billing_period import BillingContext

        p = self._patches.append
        p(patch(
            "app.runtime.orchestrator.LucielOrchestrator._lifecycle_gate",
            return_value=self.lifecycle_drop,
        ))
        p(patch(
            "app.runtime.orchestrator.LucielOrchestrator._is_session_human_controlled",
            return_value=self.human_controlled,
        ))
        # Isolate the scripted router to the PLAN loop: the real
        # EscalationJudge would otherwise consume scripted entries via its
        # router-backed intake/outcome classifiers (see _NoopJudge).
        p(patch(
            "app.runtime.orchestrator.LucielOrchestrator._judge",
            return_value=_IntakeSilentJudge(),
        ))
        p(patch(
            "app.runtime.orchestrator.LucielOrchestrator._retrieve",
            return_value=self.chunks,
        ))
        p(patch(
            "app.core.config.settings.knowledge_retrieval_enabled",
            self.retrieval_enabled,
        ))
        if self.billing_tier is not None:
            p(patch(
                "app.runtime.billing_period.resolve_billing_context",
                return_value=BillingContext(
                    tier=self.billing_tier,
                    cadence="monthly",
                    period_start="2026-06-01",
                ),
            ))

            class _FakeDB:
                def close(self_inner):
                    pass

            p(patch("app.db.session.SessionLocal", side_effect=_FakeDB))
        if self.meter is not None:
            p(patch(
                "app.runtime.budget_meter.BudgetMeter",
                return_value=self.meter,
            ))
        for patcher in self._patches:
            patcher.start()
        return self

    def __exit__(self, *exc):
        for patcher in self._patches:
            patcher.stop()
        return False


# =====================================================================
# GAP-6 / GAP-5 — lifecycle gate on the LIVE /chat path.
# =====================================================================


class TestLifecycleGateLivePath(unittest.TestCase):
    """GAP-6 (§3.6) + GAP-5 (§3.6.7): a non-active / inactive / missing
    instance short-circuits to a lifecycle no-op — empty reply, NO LLM
    call, NO assistant message persisted, NO budget accrual."""

    def test_paused_instance_no_response_no_llm(self):
        router = _ScriptedRouter([_plan_json(reply="SHOULD NOT RUN")])
        svc = _make_chat_service(router=router)
        drop = InactiveInstanceDrop(instance_id=7, status="paused")
        with _LivePath(lifecycle_drop=drop):
            reply = svc.respond(session_id="s", message="hi")
        self.assertEqual(reply, "")
        self.assertEqual(len(router.calls), 0)  # no LLM
        # Only the user message was persisted; no assistant reply.
        roles = [m.role for m in svc.session_service.added]
        self.assertEqual(roles, ["user"])

    def test_inactive_downgraded_instance_no_response_no_llm(self):
        # GAP-5: a Pro→Free downgrade sets instance_status='inactive'.
        # The same lifecycle gate treats it as non-active.
        router = _ScriptedRouter([_plan_json(reply="SHOULD NOT RUN")])
        svc = _make_chat_service(router=router)
        drop = InactiveInstanceDrop(instance_id=7, status="inactive")
        with _LivePath(lifecycle_drop=drop):
            reply = svc.respond(session_id="s", message="hi")
        self.assertEqual(reply, "")
        self.assertEqual(len(router.calls), 0)
        self.assertEqual(
            [m.role for m in svc.session_service.added], ["user"]
        )

    def test_missing_instance_no_response(self):
        router = _ScriptedRouter([_plan_json(reply="SHOULD NOT RUN")])
        svc = _make_chat_service(router=router)
        drop = InactiveInstanceDrop(instance_id=7, status="missing")
        with _LivePath(lifecycle_drop=drop):
            reply = svc.respond(session_id="s", message="hi")
        self.assertEqual(reply, "")
        self.assertEqual(len(router.calls), 0)

    def test_paused_instance_stream_emits_nothing(self):
        # /chat/stream lifecycle no-op: the playback generator yields
        # nothing because the gated answer is empty.
        router = _ScriptedRouter([_plan_json(reply="SHOULD NOT RUN")])
        svc = _make_chat_service(router=router)
        drop = InactiveInstanceDrop(instance_id=7, status="paused")
        with _LivePath(lifecycle_drop=drop):
            gen = svc.respond_stream(session_id="s", message="hi")
            tokens = list(gen)
        self.assertEqual(tokens, [])
        self.assertEqual(len(router.calls), 0)


# =====================================================================
# GAP-2 — human-controlled handoff on the LIVE path.
# =====================================================================


class TestHumanControlledLivePath(unittest.TestCase):
    """§3.4.12: a human_controlled session makes ZERO LLM calls and
    emits no auto-response (the admin owns the conversation)."""

    def test_human_controlled_no_llm_call(self):
        router = _ScriptedRouter([_plan_json(reply="SHOULD NOT RUN")])
        svc = _make_chat_service(router=router)
        with _LivePath(human_controlled=True):
            reply = svc.respond(session_id="s", message="I want a human")
        # The human-controlled finalizer returns an empty customer-facing
        # message (the human will reply); ZERO LLM calls were made.
        self.assertEqual(len(router.calls), 0)
        self.assertEqual(reply, "")


# =====================================================================
# GAP-1 — conversation budget gate on the LIVE path.
# =====================================================================


class TestBudgetGateLivePath(unittest.TestCase):
    """§3.4.1b: a Free instance at/over the conversation cap → graceful
    budget_exhausted acknowledgement, NO LLM call."""

    def test_free_at_cap_graceful_no_llm(self):
        from app.policy.entitlements import TIER_FREE, conversation_budget

        meter = BudgetMeter(backend=InMemoryBackend())
        cap = conversation_budget(TIER_FREE, "monthly")
        for i in range(cap):
            meter.count_session_once(
                admin_id="admin-1", instance_id=7,
                period_start="2026-06-01", session_id=f"seed-{i}",
            )
        router = _ScriptedRouter([_plan_json(reply="SHOULD NOT RUN")])
        svc = _make_chat_service(router=router)
        with _LivePath(billing_tier=TIER_FREE, meter=meter):
            reply = svc.respond(session_id="over-cap", message="hi")
        self.assertEqual(reply, budget_exhausted_acknowledgement())
        self.assertEqual(len(router.calls), 0)  # NO LLM call

    def test_free_within_budget_proceeds(self):
        from app.policy.entitlements import TIER_FREE

        meter = BudgetMeter(backend=InMemoryBackend())
        router = _ScriptedRouter([_plan_json(reply="real answer", confidence=0.95)])
        svc = _make_chat_service(router=router)
        with _LivePath(billing_tier=TIER_FREE, meter=meter):
            reply = svc.respond(session_id="ok", message="hi")
        # Within budget → the loop ran and emitted the real answer (the
        # PLAN reply). The router WAS called (PLAN + judge classifiers);
        # the point is the turn was NOT short-circuited by the budget gate.
        self.assertEqual(reply, "real answer")
        self.assertGreaterEqual(len(router.calls), 1)


# =====================================================================
# GAP-3 — grounding floor on the LIVE path.
# =====================================================================


class TestGroundingFloorLivePath(unittest.TestCase):
    """§3.4/§3.4.13: a low-confidence answer with failed retrieval is
    REPLACED by the canonical 'I don't have that information' phrase —
    we never ship an ungrounded answer (Vision §1)."""

    def test_weak_retrieval_low_confidence_returns_canonical_phrase(self):
        # Retrieval RAN (enabled) but returned no chunks → retrieval_failed.
        # PLAN's confidence is below the low-confidence threshold. Together
        # they fire SIGNAL_CANNOT_CONFIDENTLY_ANSWER, and the orchestrator
        # substitutes the canonical phrase for the LLM's ungrounded reply.
        router = _ScriptedRouter(
            [_plan_json(reply="Here is a made-up answer.", confidence=0.1)]
        )
        svc = _make_chat_service(router=router)
        with _LivePath(retrieval_enabled=True, chunks=[]):
            reply = svc.respond(session_id="s", message="obscure question")
        self.assertEqual(reply, CANNOT_ANSWER_REPLY)
        self.assertNotIn("made-up", reply)


# =====================================================================
# GAP-4 — tier-gated tool broker on the LIVE path.
# =====================================================================


class TestToolBrokerTierGateLivePath(unittest.TestCase):
    """§3.3.4: a PLAN that requests an off-tier / unauthorised tool gets
    a server-side DENIAL from the default-deny broker — the tool never
    executes. We assert the broker returned success=False (the structured
    refusal REFLECT reasons about), proving the gate is enforced AT THE
    SOURCE rather than trusting the model not to call the tool."""

    def test_unauthorised_tool_denied_server_side(self):
        from app.tools.base import ToolResult
        from app.tools.broker import ToolBroker
        from app.tools.registry import ToolRegistry

        # Record every broker dispatch + its result, wrapping the REAL
        # default-deny broker so the authorisation gate runs for real.
        real = ToolBroker(ToolRegistry())
        dispatched = []

        class _RecordingBroker:
            def execute_tool(self, tool_name, parameters=None, *, context=None, **extra):
                result = real.execute_tool(
                    tool_name, parameters, context=context, **extra
                )
                dispatched.append((tool_name, result))
                return result

        # PLAN asks to send_email (a tier/authorisation-gated tool) that no
        # instance has authorised. First pass requests the tool; second
        # pass (after the denial feedback) returns a plain reply so the
        # loop terminates.
        router = _ScriptedRouter([
            _plan_json(
                reply="",
                tool_calls=[{"tool": "send_email", "parameters": {}}],
                confidence=0.9,
            ),
            _plan_json(reply="I cannot do that right now.", confidence=0.9),
        ])
        svc = _make_chat_service(router=router, broker=_RecordingBroker())
        with _LivePath():
            svc.respond(session_id="s", message="email my landlord")

        # The broker WAS asked to run the tool (the model tried) and the
        # default-deny gate DENIED it server-side.
        self.assertTrue(dispatched, "broker should have been dispatched")
        names = [d[0] for d in dispatched]
        self.assertIn("send_email", names)
        denied = [r for (n, r) in dispatched if n == "send_email"]
        self.assertTrue(
            all(isinstance(r, ToolResult) and not r.success for r in denied),
            f"off-tier tool must be denied server-side; got {denied!r}",
        )


# =====================================================================
# Streaming — Option A: played-back text == grounded final answer.
# =====================================================================


class TestStreamingPlaybackLivePath(unittest.TestCase):
    """Option A: respond_stream computes the FULL gated answer through the
    same path as respond, then plays it back. The reassembled token stream
    is byte-identical to the grounded final answer (never a pre-grounding
    token)."""

    def test_stream_playback_reassembles_to_grounded_answer(self):
        router = _ScriptedRouter(
            [_plan_json(reply="The office opens at nine.", confidence=0.95)]
        )
        svc = _make_chat_service(router=router)
        with _LivePath():
            gen = svc.respond_stream(session_id="s", message="hours?")
            calls_after_compute = len(router.calls)
            tokens = list(gen)
        self.assertEqual("".join(tokens), "The office opens at nine.")
        # The gated answer is computed BEFORE any token is yielded (Option
        # A). Draining the playback generator makes NO further LLM calls —
        # the stream replays the grounded text, it does not re-run PLAN.
        self.assertEqual(len(router.calls), calls_after_compute)

    def test_stream_grounding_floor_plays_back_canonical_phrase(self):
        # Even on the streaming path, a weak-retrieval/low-confidence turn
        # plays back the GROUNDED canonical phrase, never the raw ungrounded
        # tokens the model produced.
        router = _ScriptedRouter(
            [_plan_json(reply="A confident-sounding fabrication.", confidence=0.1)]
        )
        svc = _make_chat_service(router=router)
        with _LivePath(retrieval_enabled=True, chunks=[]):
            gen = svc.respond_stream(session_id="s", message="obscure")
            tokens = list(gen)
        self.assertEqual("".join(tokens), CANNOT_ANSWER_REPLY)
        self.assertNotIn("fabrication", "".join(tokens))


if __name__ == "__main__":
    unittest.main()
