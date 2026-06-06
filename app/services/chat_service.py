"""
ChatService coordinates one full chat turn.

Single Admin→Instance boundary (Architecture §3.7.2). Per-turn
context:
  1. Luciel Core persona (fixed, optionally renamed by instance)
  2. Composed PRESET + BUSINESS_CONTEXT stanzas (§3.5.1; from the
     structured instance pillars — never raw customer prompt)
  3. Retrieved knowledge (Arc 11, scope-inherited)
  4. User memories (consent-gated)
  5. Tool descriptions — for the 8-tool v1 catalog (WU3), filtered
     by per-instance authorisation (WU2 default-deny)
  6. This-session conversation history (Wall 4)
  7. LLM call

Cognition (escalate / save_memory / get_session_summary) is
ALWAYS-ON per Architecture §3.4 and Decision #20. It runs through
``app.cognition.CognitionService`` — directly invoked by this
service, NOT routed through the broker or registry. Cognition is
not tier-gated and not admin-configurable.

Arc 12 WU7 sweep:
  * Removed v1 three-layer Domain/Agent prompt scaffold (the
    tenant_prompt / domain_prompt / agent_prompt threading,
    ``_resolve_luciel_context``'s domain/agent resolution, and
    ``_compose_system_prompt_additions``). V2 collapsed to a
    single Admin→Instance boundary per §3.7.2.
  * Removed substring tool-detection (the old
    ``"escalate_to_human" in raw_reply`` / save_memory /
    get_session_summary branches). Cognition lives in
    ``app.cognition`` now and recognises intent internally.
  * Removed the ``instances.allowed_tools`` getattr fallback —
    superseded by the WU2 ``instance_tool_authorizations`` table
    (the broker's default-deny gate is the source of truth).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from app.cognition import CognitionService
from app.core.config import settings
from app.runtime.llm_router import ModelRouter
from app.runtime.knowledge_retrieval import KnowledgeRetriever
from app.memory.service import MemoryService
from app.persona.composer import (
    compose_business_context_stanza,
    compose_preset_stanza,
)
from app.policy.consent import ConsentPolicy
from app.policy.engine import PolicyEngine
from app.repositories.config_repository import ConfigRepository
from app.repositories.instance_repository import InstanceRepository
from app.services.session_service import SessionService
from app.services.trace_service import TraceService
from app.tools.broker import ToolBroker
from app.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


@dataclass
class LucielContext:
    """Resolved per-turn persona/provider/tools/prompt bundle.

    Single Admin→Instance shape post-WU7. The chat path no longer
    threads a Domain or Agent layer.
    """

    # Arc 15 WU2 — platform-composed persona stanzas (§3.5.1). Derived
    # from instance.personality_preset (+ personality_axes when custom)
    # and instance.business_context. None when no instance binding is
    # active or the pillar is unset.
    preset_stanza: str | None = None
    business_context_stanza: str | None = None

    # LLM provider preference. Instance.preferred_provider when set,
    # else caller-supplied.
    preferred_provider: str | None = None

    # Human-facing assistant name. Instance.display_name wins when
    # bound; otherwise the Luciel Core default.
    assistant_name: str = "Luciel"

    # Trace metadata.
    luciel_instance_id: int | None = None


class ChatService:

    def __init__(
        self,
        session_service: SessionService,
        memory_service: MemoryService,
        model_router: ModelRouter,
        tool_registry: ToolRegistry,
        tool_broker: ToolBroker,
        trace_service: TraceService,
        knowledge_retriever: KnowledgeRetriever,
        config_repository: ConfigRepository,
        instance_repository: InstanceRepository,
        consent_policy: ConsentPolicy | None = None,
        cognition_service: CognitionService | None = None,
    ) -> None:
        self.session_service = session_service
        self.memory_service = memory_service
        self.model_router = model_router
        self.tool_registry = tool_registry
        self.tool_broker = tool_broker
        self.trace_service = trace_service
        self.knowledge_retriever = knowledge_retriever
        self.config_repository = config_repository
        self.instance_repository = instance_repository
        self.consent_policy = consent_policy
        self.policy_engine = PolicyEngine()
        # Cognition is always-on (§3.4); construct a default if one
        # is not injected so callers don't have to know about it.
        self.cognition_service = cognition_service or CognitionService()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_luciel_context(
        self,
        *,
        luciel_instance_id: int | None,
        admin_id: str,
    ) -> LucielContext:
        """Resolve the per-turn ``LucielContext``.

        Single Admin→Instance boundary post-WU7. When
        ``luciel_instance_id`` is set AND the row is active AND
        belongs to the same admin, we layer:

          * instance.personality_preset/_axes → ``preset_stanza``
          * instance.business_context        → ``business_context_stanza``
          * instance.display_name            → ``assistant_name``
          * instance.preferred_provider      → ``preferred_provider``

        When the instance is missing / inactive / cross-tenant, we
        fall back to defaults and log a warning. Never 500 the chat
        turn on a stale key binding.
        """
        ctx = LucielContext(assistant_name="Luciel")

        if luciel_instance_id is None:
            return ctx

        instance = self.instance_repository.get_by_pk(luciel_instance_id)
        if instance is None:
            logger.warning(
                "Chat turn bound to luciel_instance_id=%s but instance "
                "not found; falling back to defaults.",
                luciel_instance_id,
            )
            return ctx
        if not getattr(instance, "active", False):
            logger.warning(
                "Chat turn bound to luciel_instance_id=%s which is "
                "inactive; falling back to defaults.",
                luciel_instance_id,
            )
            return ctx
        if getattr(instance, "admin_id", None) != admin_id:
            logger.warning(
                "Chat turn bound to luciel_instance_id=%s whose admin=%s "
                "does not match session admin=%s; falling back to "
                "defaults.",
                luciel_instance_id,
                getattr(instance, "admin_id", None),
                admin_id,
            )
            return ctx

        ctx.luciel_instance_id = instance.id
        ctx.assistant_name = (
            getattr(instance, "display_name", None) or ctx.assistant_name
        )
        preferred_provider = getattr(instance, "preferred_provider", None)
        if preferred_provider:
            ctx.preferred_provider = preferred_provider

        # Arc 15 WU2 — compose the PRESET + BUSINESS_CONTEXT stanzas from
        # the structured instance pillars (§3.5.1). This replaces the
        # deprecated free-text system_prompt_additions layer entirely.
        ctx.preset_stanza = compose_preset_stanza(
            personality_preset=getattr(instance, "personality_preset", None),
            personality_axes=getattr(instance, "personality_axes", None),
        )
        business_context = getattr(instance, "business_context", None)
        if business_context:
            tier = self._resolve_admin_tier(admin_id)
            ctx.business_context_stanza = compose_business_context_stanza(
                business_context=business_context,
                tier=tier,
            )

        return ctx

    def _resolve_admin_tier(self, admin_id: str) -> str:
        """Resolve the owning Admin's tier for tier-capped composition.

        Fail-closed to Free when the row / tier is unrecognised — the
        same posture as the route layer's pillar-tier resolver. A wrong
        tier here only affects the defensive business_context truncation
        ceiling; the API layer already enforced the real cap on write.
        """
        from sqlalchemy import select

        from app.models.admin import Admin
        from app.policy.entitlements import TIER_ENTITLEMENTS, TIER_FREE

        try:
            row = self.instance_repository.db.execute(
                select(Admin.tier).where(Admin.id == admin_id)
            ).scalar_one_or_none()
        except Exception:
            logger.warning(
                "tier resolution failed for admin_id=%s; defaulting to free",
                admin_id,
            )
            return TIER_FREE
        return row if row in TIER_ENTITLEMENTS else TIER_FREE

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def respond(
        self,
        *,
        session_id: str,
        message: str,
        provider: str | None = None,
        caller_tenant_id: str | None = None,
        luciel_instance_id: int | None = None,
        actor_key_prefix: str | None = None,
        actor_user_id: "uuid.UUID | None" = None,
    ) -> str:
        # RESCAN CORE(serving-path) — HYBRID adapter. ChatService is now a
        # THIN session/persona adapter over the LucielOrchestrator (the
        # §3.4.1 agentic loop), which is the live serving engine. This
        # method does the route-layer concerns ChatService owns (session
        # verify, user-message persist, persona resolution, assistant-reply
        # persist, consent-gated memory extraction) and delegates the
        # ACTUAL turn — budget gate §3.4.1b, human-controlled handoff
        # §3.4.12, grounding floors §3.4/§3.4.13, tier-gated tool broker
        # §3.3.4, lifecycle gate §3.6 — to the orchestrator, which enforces
        # them AT THE SOURCE (closing audit GAP-1/2/3/4/6). Retrieval +
        # grounding are owned by the orchestrator's self-contained,
        # tenant-scoped _retrieve step, so this adapter no longer retrieves
        # knowledge or builds the prompt itself.
        resp, _resolved = self._run_turn(
            session_id=session_id,
            message=message,
            provider=provider,
            caller_tenant_id=caller_tenant_id,
            luciel_instance_id=luciel_instance_id,
            actor_key_prefix=actor_key_prefix,
            actor_user_id=actor_user_id,
        )
        return resp.message

    def _run_turn(
        self,
        *,
        session_id: str,
        message: str,
        provider: str | None,
        caller_tenant_id: str | None,
        luciel_instance_id: int | None,
        actor_key_prefix: str | None,
        actor_user_id: "uuid.UUID | None",
    ):
        """Drive ONE full gated turn through the orchestrator and apply the
        adapter-owned side effects (persist user msg → run() → persist
        assistant reply → memory extraction). Returns ``(RuntimeResponse,
        admin_id)`` so both ``respond`` and ``respond_stream`` share the
        exact same gated path (Option A: stream plays back the already-
        grounded ``resp.message``, never a pre-grounding token)."""
        from app.runtime.contracts import RuntimeRequest
        from app.runtime.orchestrator import LucielOrchestrator

        # 1. Verify session
        session = self.session_service.get_session(session_id)
        if session is None:
            raise ValueError("Session not found")
        if caller_tenant_id and session.admin_id != caller_tenant_id:
            raise PermissionError("Session does not belong to this tenant")

        user_id = session.user_id
        admin_id = session.admin_id

        # 2. Persist user message
        self.session_service.add_message(
            session_id=session_id, role="user", content=message,
        )

        # 3. Resolve per-turn LucielContext (single Admin→Instance) — the
        #    persona stanzas + display name + preferred provider the
        #    orchestrator's prompt must carry to match the legacy path.
        ctx = self._resolve_luciel_context(
            luciel_instance_id=luciel_instance_id,
            admin_id=admin_id,
        )
        if not provider and ctx.preferred_provider:
            provider = ctx.preferred_provider

        # 4. Retrieve long-term memories (consent-gated). The consent gate
        #    is an adapter concern (it keys off the ConsentPolicy this
        #    service owns); the resolved memories ride the RuntimeRequest
        #    so the orchestrator's prompt is identity-aware.
        memories: list = []
        can_use_memory = True
        if self.consent_policy and user_id:
            can_use_memory = self.consent_policy.can_persist_memory(
                user_id=user_id, admin_id=admin_id,
            )
        if user_id and can_use_memory:
            memories = self.memory_service.retrieve_memories(
                user_id=user_id,
                admin_id=admin_id,
            )

        # 5. Build the orchestrator request. enforce_lifecycle_gate=True so
        #    the live path runs the §3.6 lifecycle gate FIRST (GAP-6): a
        #    non-active / inactive / missing instance short-circuits to a
        #    no-LLM, no-budget lifecycle no-op.
        req = RuntimeRequest(
            message=message,
            session_id=session_id,
            user_id=str(user_id) if user_id is not None else None,
            admin_id=admin_id,
            channel=getattr(session, "channel", "web"),
            luciel_instance_id=ctx.luciel_instance_id,
            persona_preset_stanza=ctx.preset_stanza,
            persona_business_context_stanza=ctx.business_context_stanza,
            assistant_name=ctx.assistant_name,
            memories=list(memories) if memories else [],
            provider=provider,
            enforce_lifecycle_gate=True,
        )

        # 6. Run the gated agentic loop. Reuse the SAME injected router /
        #    broker / trace service the legacy path used so behaviour and
        #    test doubles carry over unchanged; the orchestrator lazily
        #    builds the rest (judge, budget meter, channel arbiter,
        #    cognition finalizer) exactly as the production default.
        orchestrator = LucielOrchestrator(
            trace_service=self.trace_service,
            model_router=self.model_router,
            tool_broker=self.tool_broker,
        )
        resp = orchestrator.run(req)

        # 7. Lifecycle no-op (§3.6): the instance is not ACTIVE. NO assistant
        #    reply is persisted, NO memory extraction runs, NO budget was
        #    accrued. The route/channel layer maps the empty message onto
        #    its documented no-op shape (widget 204, SMS 204, /chat empty).
        if resp.lifecycle_blocked:
            return resp, admin_id

        final_reply = resp.message

        # 8. Persist assistant reply — capture id for memory-extraction key.
        assistant_msg = self.session_service.add_message(
            session_id=session_id, role="assistant", content=final_reply,
        )

        # 9. Memory extraction (consent-gated) — async if flag enabled.
        if user_id and can_use_memory:
            recent_messages = [
                {"role": "user", "content": message},
                {"role": "assistant", "content": final_reply},
            ]
            try:
                if settings.memory_extraction_async and actor_key_prefix:
                    try:
                        self.memory_service.enqueue_extraction(
                            user_id=user_id,
                            admin_id=admin_id,
                            session_id=session_id,
                            message_id=assistant_msg.id,
                            actor_key_prefix=actor_key_prefix,
                            luciel_instance_id=luciel_instance_id,
                            actor_user_id=actor_user_id,
                            trace_id=None,
                        )
                    except Exception as enq_exc:
                        logger.warning(
                            "enqueue_extraction failed (fail-open): type=%s "
                            "exc_repr=%r session=%s message_id=%s",
                            type(enq_exc).__name__,
                            enq_exc,
                            session_id,
                            assistant_msg.id,
                        )
                else:
                    self.memory_service.extract_and_save(
                        user_id=user_id,
                        admin_id=admin_id,
                        session_id=session_id,
                        messages=recent_messages,
                        message_id=assistant_msg.id,
                        luciel_instance_id=luciel_instance_id,
                        actor_user_id=actor_user_id,
                    )
            except Exception as exc:
                logger.warning(
                    "Memory extraction failed: type=%s exc_repr=%r "
                    "session=%s message_id=%s",
                    type(exc).__name__, exc, session_id,
                    getattr(assistant_msg, "id", None),
                )

        # 10. Trace — the orchestrator already recorded the §3.4.1 trace
        #     (provider/model/tool/escalation/grounding) at the source. The
        #     adapter does NOT double-write.
        return resp, admin_id

    def respond_stream(
        self,
        *,
        session_id: str,
        message: str,
        provider: str | None = None,
        caller_tenant_id: str | None = None,
        luciel_instance_id: int | None = None,
        actor_key_prefix: str | None = None,
        actor_user_id: "uuid.UUID | None" = None,
    ):
        """RESCAN CORE(serving-path) — Option A streaming (founder-ratified).

        Streaming cannot emit pre-grounding tokens: the orchestrator's
        PLAN→ACT→REFLECT loop may re-enter and the §3.4/§3.4.13 grounding
        gate may REPLACE the answer with the canonical "I don't have that
        information" phrase. Streaming raw LLM tokens would defeat the
        grounding promise (Vision §1). So we COMPUTE the full gated answer
        through the same ``_run_turn`` path ``respond`` uses, THEN play the
        already-grounded final text back word-by-word.

        ``_run_turn`` has ALREADY persisted the assistant reply, run memory
        extraction, and (via the orchestrator) recorded the trace — so this
        generator ONLY replays text; it performs NO further side effects.
        A lifecycle no-op (instance not ACTIVE) yields an empty answer, so
        the playback emits nothing and the route maps it onto its no-op
        shape (widget 204 / SMS 204 / /chat empty)."""
        resp, _admin_id = self._run_turn(
            session_id=session_id,
            message=message,
            provider=provider,
            caller_tenant_id=caller_tenant_id,
            luciel_instance_id=luciel_instance_id,
            actor_key_prefix=actor_key_prefix,
            actor_user_id=actor_user_id,
        )
        final_reply = resp.message

        def token_generator():
            # Play back the grounded final answer word-by-word, preserving
            # the inter-word whitespace so the reassembled stream is
            # byte-identical to ``final_reply`` (the widget concatenates
            # token frames verbatim). ``str.split`` would drop spacing; we
            # re-append a single space between words and rely on the widget
            # rendering — but to keep reassembly exact we split on a regex
            # that KEEPS the separators.
            import re

            if not final_reply:
                return
            for token in re.findall(r"\S+\s*", final_reply):
                yield token

        return token_generator()
