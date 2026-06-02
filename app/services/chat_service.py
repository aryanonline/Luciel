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
from app.integrations.llm.base import LLMMessage, LLMRequest
from app.integrations.llm.router import ModelRouter
from app.knowledge.retriever import KnowledgeRetriever
from app.memory.service import MemoryService
from app.persona.composer import (
    compose_business_context_stanza,
    compose_preset_stanza,
)
from app.persona.luciel_core import build_system_prompt
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

        # 3. Resolve per-turn LucielContext (single Admin→Instance).
        ctx = self._resolve_luciel_context(
            luciel_instance_id=luciel_instance_id,
            admin_id=admin_id,
        )

        # Use the instance-preferred provider if caller did not specify.
        if not provider and ctx.preferred_provider:
            provider = ctx.preferred_provider

        # 4. Retrieve long-term memories (consent-gated). Arc 12 EX1b:
        #    v2 single Admin→Instance boundary (§3.7.2); memory rows
        #    are scoped by admin_id + Wall-3 RLS on luciel_instance_id.
        #    The legacy per-agent partitioning has been excised.
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

        # 5. Retrieve relevant knowledge (scope-inherited).
        knowledge = self.knowledge_retriever.retrieve(
            query=message,
            admin_id=admin_id,
            luciel_instance_id=ctx.luciel_instance_id,
        )

        # 6. Load conversation history
        history = self.session_service.list_messages(session_id)

        # 7. Build the full child Luciel prompt. The tool catalog is
        #    the 8-tool WU3 registry; per-instance authorisation
        #    (WU2 default-deny) is enforced at dispatch time inside
        #    the broker, not by an allow-list passed here.
        tool_descriptions = self.tool_registry.get_tool_descriptions()
        system_prompt = build_system_prompt(
            memories=memories if memories else None,
            tool_descriptions=tool_descriptions if tool_descriptions else None,
            preset_stanza=ctx.preset_stanza,
            business_context_stanza=ctx.business_context_stanza,
            knowledge=knowledge if knowledge else None,
            assistant_name=ctx.assistant_name,
        )

        llm_messages = [LLMMessage(role="system", content=system_prompt)]
        for msg in history:
            llm_messages.append(LLMMessage(role=msg.role, content=msg.content))

        # 8. Call LLM
        llm_request = LLMRequest(messages=llm_messages)
        llm_response = self.model_router.generate(
            llm_request, preferred_provider=provider
        )
        raw_reply = llm_response.content

        llm_provider_used = llm_response.provider
        llm_model_used = llm_response.model

        # 9. Cognition step — always-on (§3.4). The cognition module
        #    recognises one of the three intents (escalate /
        #    save_memory / get_session_summary) in ``raw_reply`` and
        #    executes the corresponding behaviour. No tool registry
        #    dispatch, no substring branching in this file.
        cognition_outcome = self.cognition_service.process_turn(
            raw_reply=raw_reply,
            messages=[
                {"role": msg.role, "content": msg.content} for msg in history
            ],
            session_id=session_id,
            user_id=user_id,
            admin_id=admin_id,
        )

        tool_was_called = cognition_outcome.handled
        tool_name = cognition_outcome.intent
        tool_result_metadata = (
            cognition_outcome.metadata if tool_was_called else None
        )

        # Save-memory follow-through: persistence stays on the same
        # call site it had pre-WU7 — PolicyEngine.evaluate_memory_write
        # gate + memory_service.repository.save_memory. The cognition
        # module surfaces the payload; the chat path persists.
        if (
            cognition_outcome.intent == "save_memory"
            and cognition_outcome.memory_payload
        ):
            category = cognition_outcome.memory_payload.get("category", "")
            content = cognition_outcome.memory_payload.get("content", "")
            if user_id and self.policy_engine.evaluate_memory_write(
                category=category, content=content,
            ):
                try:
                    self.memory_service.repository.save_memory(
                        user_id=user_id,
                        admin_id=admin_id,
                        category=category,
                        content=content,
                        source_session_id=session_id,
                    )
                except Exception as exc:
                    logger.warning("Failed to save cognition memory: %s", exc)

        # Follow-up LLM turn for non-escalation cognition (mirrors the
        # pre-WU7 broker follow-through shape).
        if tool_was_called and not cognition_outcome.escalated:
            llm_messages.append(LLMMessage(role="assistant", content=raw_reply))
            llm_messages.append(LLMMessage(
                role="user",
                content=(
                    f"Tool Result: {cognition_outcome.output} — respond "
                    f"to the user based on this result."
                ),
            ))
            followup_request = LLMRequest(messages=llm_messages)
            followup_response = self.model_router.generate(
                followup_request, preferred_provider=provider,
            )
            raw_reply = followup_response.content

        # 10. Policy engine — unchanged. Keys off ``tool_name`` to
        #     swap in the default escalation copy when escalated.
        decision = self.policy_engine.evaluate_response(
            raw_reply=raw_reply,
            tool_was_called=tool_was_called,
            tool_name=tool_name,
            tool_result_metadata=tool_result_metadata,
        )
        final_reply = decision.modified_reply

        # 11. (Escalation side-effect already fired inside cognition
        #     when intent == escalate_to_human. The policy engine's
        #     ``decision.escalated`` is the customer-facing flag for
        #     the trace row.)

        # 12. Persist assistant reply — capture id for idempotency key
        assistant_msg = self.session_service.add_message(
            session_id=session_id, role="assistant", content=final_reply,
        )

        # 13. Memory extraction (consent-gated) — async if flag enabled
        memories_extracted = 0
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
                        memories_extracted = 0
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
                    memories_extracted = self.memory_service.extract_and_save(
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

        # 14. Trace
        try:
            self.trace_service.record_trace(
                session_id=session_id,
                user_id=user_id,
                admin_id=admin_id,
                user_message=message,
                assistant_reply=final_reply,
                llm_provider=llm_provider_used,
                llm_model=llm_model_used,
                memories_retrieved=len(memories),
                memories_used=memories if memories else None,
                tool_called=tool_was_called,
                tool_name=tool_name,
                escalated=decision.escalated,
                policy_flags=decision.flags if decision.flags else None,
                memories_extracted=memories_extracted,
                luciel_instance_id=ctx.luciel_instance_id,
            )
        except Exception as exc:
            logger.warning("Trace recording failed: %s", exc)

        return final_reply

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
        """Token-by-token streaming variant.

        Streaming cannot mid-stream re-dispatch a follow-up LLM turn,
        so the streaming path does not invoke cognition mid-flight.
        Cognition / tool follow-through on a streamed turn is an
        Arc-14 concern (the agentic loop owns multi-step turns).
        The non-streaming ``respond()`` path is where cognition lives
        today. The streaming path preserves answer + policy + trace.
        """

        session = self.session_service.get_session(session_id)
        if session is None:
            raise ValueError("Session not found")
        if caller_tenant_id and session.admin_id != caller_tenant_id:
            raise PermissionError("Session does not belong to this tenant")

        user_id = session.user_id
        admin_id = session.admin_id

        self.session_service.add_message(
            session_id=session_id, role="user", content=message,
        )

        ctx = self._resolve_luciel_context(
            luciel_instance_id=luciel_instance_id,
            admin_id=admin_id,
        )

        if not provider and ctx.preferred_provider:
            provider = ctx.preferred_provider

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

        knowledge = self.knowledge_retriever.retrieve(
            query=message,
            admin_id=admin_id,
            luciel_instance_id=ctx.luciel_instance_id,
        )

        history = self.session_service.list_messages(session_id)

        tool_descriptions = self.tool_registry.get_tool_descriptions()
        system_prompt = build_system_prompt(
            memories=memories if memories else None,
            tool_descriptions=tool_descriptions if tool_descriptions else None,
            preset_stanza=ctx.preset_stanza,
            business_context_stanza=ctx.business_context_stanza,
            knowledge=knowledge if knowledge else None,
            assistant_name=ctx.assistant_name,
        )

        llm_messages = [LLMMessage(role="system", content=system_prompt)]
        for msg in history:
            llm_messages.append(LLMMessage(role=msg.role, content=msg.content))

        llm_request = LLMRequest(messages=llm_messages)

        full_reply_parts: list[str] = []

        def token_generator():
            for token in self.model_router.generate_stream(
                llm_request, preferred_provider=provider
            ):
                full_reply_parts.append(token)
                yield token

            full_reply = "".join(full_reply_parts)

            decision = self.policy_engine.evaluate_response(
                raw_reply=full_reply,
                tool_was_called=False,
                tool_name=None,
                tool_result_metadata=None,
            )
            final_reply = decision.modified_reply

            assistant_msg = self.session_service.add_message(
                session_id=session_id, role="assistant", content=final_reply,
            )

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
                                "enqueue_extraction failed (fail-open, stream): "
                                "type=%s exc_repr=%r session=%s message_id=%s",
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

            try:
                self.trace_service.record_trace(
                    session_id=session_id,
                    user_id=user_id,
                    admin_id=admin_id,
                    user_message=message,
                    assistant_reply=final_reply,
                    llm_provider=None,
                    llm_model=None,
                    memories_retrieved=len(memories),
                    memories_used=memories if memories else None,
                    tool_called=False,
                    tool_name=None,
                    escalated=decision.escalated,
                    policy_flags=decision.flags if decision.flags else None,
                    memories_extracted=0,
                    luciel_instance_id=ctx.luciel_instance_id,
                )
            except Exception as exc:
                logger.warning("Trace recording failed (stream): %s", exc)

        return token_generator()
