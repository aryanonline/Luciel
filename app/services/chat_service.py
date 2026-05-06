"""
ChatService coordinates one full chat turn.

Assembles the full child Luciel context:
  1. Luciel Core persona (fixed, with custom name)
  2. Tenant config (tenant-wide rules)
  3. Domain config (role-specific instructions)
  4. Agent config (agent-specific instructions)
  5. Retrieved knowledge (from vector DB)
  6. User memories (from memory_items)
  7. Tool descriptions (filtered by domain config)
  8. Conversation history

PATCHED: agent_id piped through to knowledge retriever,
memory service, and trace recording. Tool descriptions now
filtered by domain_config.allowed_tools.

PATCHED (Step 24.5 File 15):
- New LucielContext dataclass unifies persona/provider/tools/prompt
  resolution so respond() and respond_stream() no longer duplicate
  8 lines of setup.
- New _resolve_luciel_context() helper is the single source of truth
  for "which persona/provider/tools/prompt does this turn use?"
- luciel_instance_id is threaded through both entry points from
  request.state (File 14) and recorded on the trace row.
- When a request is bound to a specific LucielInstance, instance-level
  fields override the legacy tenant/domain/agent chain per Luciel Core
  doctrine: "one fixed mind, layered additions" -- instance persona is
  appended to the tenant+domain+agent chain, never replaces it.
- Fail-safe: inactive/missing/cross-tenant instances fall back to
  legacy resolution with a warning. Never 500 on a stale key binding.
- Domain-agnostic: no vertical branching, no imports from app.domain.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.integrations.llm.base import LLMMessage, LLMRequest
from app.integrations.llm.router import ModelRouter
from app.knowledge.retriever import KnowledgeRetriever
from app.memory.service import MemoryService
from app.persona.luciel_core import build_system_prompt
from app.policy.consent import ConsentPolicy
from app.policy.engine import PolicyEngine
from app.policy.escalation import EscalationService
from app.repositories.config_repository import ConfigRepository
from app.repositories.luciel_instance_repository import LucielInstanceRepository  # Step 24.5 File 15
from app.services.session_service import SessionService
from app.services.trace_service import TraceService
from app.tools.broker import ToolBroker
from app.tools.registry import ToolRegistry
from app.core.config import settings
import uuid

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Step 24.5 File 15 -- unified per-turn context bundle.
# ----------------------------------------------------------------------

@dataclass
class LucielContext:
    """Resolved per-turn persona/provider/tools/prompt bundle.

    Produced by ChatService._resolve_luciel_context. Consumed by the
    prompt-building and LLM-invocation steps of respond()/respond_stream().
    """

    # Prompt layers. Each one is either a string or None; the prompt
    # builder stitches them together as: tenant -> domain -> agent.
    # The instance_prompt (File 15) is appended onto the agent layer
    # via _compose_system_prompt_additions() so the existing
    # build_system_prompt signature doesn't need to change today.
    tenant_prompt: str | None = None
    domain_prompt: str | None = None
    agent_prompt: str | None = None
    instance_prompt: str | None = None

    # LLM provider preference resolved instance > agent > domain > None.
    preferred_provider: str | None = None

    # Tool allow-list. None = no restriction (all tools). Empty list =
    # explicitly no tools. Instance-level list overrides domain-level.
    allowed_tools: list[str] | None = None

    # Human-facing assistant name. Instance.display_name wins when bound.
    assistant_name: str = "Luciel"

    # Trace metadata.
    luciel_instance_id: int | None = None
    tenant_config_id: int | None = None
    domain_config_id: int | None = None
    agent_config_id: int | None = None


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
        luciel_instance_repository: LucielInstanceRepository,  # Step 24.5 File 15
        consent_policy: ConsentPolicy | None = None,
    ) -> None:
        self.session_service = session_service
        self.memory_service = memory_service
        self.model_router = model_router
        self.tool_registry = tool_registry
        self.tool_broker = tool_broker
        self.trace_service = trace_service
        self.knowledge_retriever = knowledge_retriever
        self.config_repository = config_repository
        self.luciel_instance_repository = luciel_instance_repository  # Step 24.5 File 15
        self.consent_policy = consent_policy
        self.policy_engine = PolicyEngine()
        self.escalation_service = EscalationService()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_allowed_tools(self, domain_config) -> list[str] | None:
        """Legacy domain-level tool allow-list. Returns None if unrestricted."""
        if domain_config and domain_config.allowed_tools:
            return domain_config.allowed_tools
        return None

    def _resolve_luciel_context(
        self,
        *,
        luciel_instance_id: int | None,
        tenant_id: str,
        domain_id: str | None,
        agent_id: str | None,
    ) -> LucielContext:
        """
        Unified resolver for the per-turn LucielContext.

        Legacy path (luciel_instance_id is None):
            tenant -> domain -> agent config chain, exactly as pre-24.5.

        Bound path (luciel_instance_id is set AND the row is active AND
        the row belongs to the same tenant):
            legacy chain, then layer instance-level overrides on top:
            - preferred_provider: instance wins if set
            - allowed_tools: instance wins if non-null
            - system_prompt_additions: appended (not replaced) as the
              4th prompt layer -- Luciel Core doctrine: one fixed mind,
              layered additions.
            - assistant_name: instance.display_name wins when bound.

        Fail-safe: if the bound instance is missing/inactive/cross-tenant,
        log a warning and fall back to legacy resolution. Never 500 the
        chat turn on a stale key binding.
        """
        ctx = LucielContext(assistant_name="Luciel")

        # --- Legacy tenant config ---
        tenant_config = self.config_repository.get_tenant_config(tenant_id)
        if tenant_config:
            ctx.tenant_prompt = tenant_config.system_prompt_additions
            ctx.tenant_config_id = tenant_config.id

        # --- Legacy domain config ---
        domain_config = None
        if domain_id:
            domain_config = self.config_repository.get_domain_config(tenant_id, domain_id)
            if domain_config:
                ctx.domain_prompt = domain_config.system_prompt_additions
                ctx.domain_config_id = domain_config.id
                ctx.preferred_provider = domain_config.preferred_provider

        # --- Legacy agent config ---
        if agent_id:
            agent_config = self.config_repository.get_agent_config(tenant_id, agent_id)
            if agent_config:
                ctx.agent_prompt = agent_config.system_prompt_additions
                ctx.agent_config_id = agent_config.id
                ctx.assistant_name = agent_config.display_name or "Luciel"
                if agent_config.preferred_provider:
                    ctx.preferred_provider = agent_config.preferred_provider

        # Legacy allowed_tools fallback (domain-level).
        ctx.allowed_tools = self._resolve_allowed_tools(domain_config)

        # --- Step 24.5 File 15: instance-level overrides ---
        if luciel_instance_id is None:
            return ctx

        instance = self.luciel_instance_repository.get_by_pk(luciel_instance_id)
        if instance is None:
            logger.warning(
                "Chat turn bound to luciel_instance_id=%s but instance not found; "
                "falling back to legacy resolution.",
                luciel_instance_id,
            )
            return ctx
        if not getattr(instance, "active", False):
            logger.warning(
                "Chat turn bound to luciel_instance_id=%s which is inactive; "
                "falling back to legacy resolution.",
                luciel_instance_id,
            )
            return ctx
        if instance.scope_owner_tenant_id != tenant_id:
            logger.warning(
                "Chat turn bound to luciel_instance_id=%s whose tenant=%s does "
                "not match session tenant=%s; falling back to legacy resolution.",
                luciel_instance_id, instance.scope_owner_tenant_id, tenant_id,
            )
            return ctx

        # Apply overrides.
        ctx.luciel_instance_id = instance.id
        ctx.instance_prompt = instance.system_prompt_additions
        ctx.assistant_name = instance.display_name or ctx.assistant_name

        if instance.preferred_provider:
            ctx.preferred_provider = instance.preferred_provider

        if instance.allowed_tools is not None:
            # Empty list means "explicitly no tools" and is respected.
            ctx.allowed_tools = instance.allowed_tools

        return ctx

    def _compose_system_prompt_additions(
        self, ctx: LucielContext,
    ) -> tuple[str | None, str | None, str | None]:
        """
        Return (tenant_prompt, domain_prompt, agent_prompt_merged) where the
        agent layer has the instance_prompt appended onto it -- preserving
        the 3-argument shape build_system_prompt expects today while letting
        instance additions flow through without a signature change.
        """
        agent_layer = ctx.agent_prompt
        if ctx.instance_prompt:
            agent_layer = (
                f"{agent_layer}\n\n{ctx.instance_prompt}"
                if agent_layer
                else ctx.instance_prompt
            )
        return ctx.tenant_prompt, ctx.domain_prompt, agent_layer

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
        luciel_instance_id: int | None = None,  # Step 24.5 File 15
        actor_key_prefix: str | None = None,
        actor_user_id: "uuid.UUID | None" = None,  # Step 24.5b File 2.5
    ) -> str:

        # 1. Verify session
        session = self.session_service.get_session(session_id)
        if session is None:
            raise ValueError("Session not found")
        if caller_tenant_id and session.tenant_id != caller_tenant_id:
            raise PermissionError("Session does not belong to this tenant")

        user_id = session.user_id
        tenant_id = session.tenant_id
        domain_id = session.domain_id
        agent_id = getattr(session, "agent_id", None)

        # 2. Persist user message
        self.session_service.add_message(
            session_id=session_id, role="user", content=message,
        )

        # 3. Resolve the per-turn LucielContext (Step 24.5 File 15 --
        #    replaces the old 3-step tenant/domain/agent resolution).
        ctx = self._resolve_luciel_context(
            luciel_instance_id=luciel_instance_id,
            tenant_id=tenant_id,
            domain_id=domain_id,
            agent_id=agent_id,
        )

        # Use the most specific preferred provider if caller did not specify one.
        if not provider and ctx.preferred_provider:
            provider = ctx.preferred_provider

        # 4. Retrieve long-term memories (agent-scoped, consent-gated)
        memories: list = []
        can_use_memory = True
        if self.consent_policy and user_id:
            can_use_memory = self.consent_policy.can_persist_memory(
                user_id=user_id, tenant_id=tenant_id,
            )
        if user_id and can_use_memory:
            memories = self.memory_service.retrieve_memories(
                user_id=user_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
            )

        # 5. Retrieve relevant knowledge (scope-inherited).
        knowledge = self.knowledge_retriever.retrieve(
            query=message,
            tenant_id=tenant_id,
            domain_id=domain_id,
            agent_id=agent_id,
            luciel_instance_id=ctx.luciel_instance_id,
        )

        # 6. Load conversation history
        history = self.session_service.list_messages(session_id)

        # 7. Build the full child Luciel prompt.
        tool_descriptions = self.tool_registry.get_tool_descriptions(
            allowed=ctx.allowed_tools,
        )
        tenant_prompt, domain_prompt, agent_layer = (
            self._compose_system_prompt_additions(ctx)
        )
        system_prompt = build_system_prompt(
            memories=memories if memories else None,
            tool_descriptions=tool_descriptions if tool_descriptions else None,
            tenant_prompt=tenant_prompt,
            domain_prompt=domain_prompt,
            agent_prompt=agent_layer,
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

        # 9. Tool call parsing / execution
        tool_was_called = False
        tool_name: str | None = None
        tool_result_metadata = None

        tool_result = self.tool_broker.parse_and_execute(
            raw_reply,
            messages=[
                {"role": msg.role, "content": msg.content} for msg in history
            ],
        )

        if tool_result is not None:
            tool_was_called = True
            tool_result_metadata = tool_result.metadata
            if "escalate_to_human" in raw_reply:
                tool_name = "escalate_to_human"
            elif "save_memory" in raw_reply:
                tool_name = "save_memory"
            elif "get_session_summary" in raw_reply:
                tool_name = "get_session_summary"

        # Save-memory tool follow-through
        if tool_name == "save_memory" and tool_result.success:
            category = tool_result.metadata.get("category", "")
            content = tool_result.metadata.get("content", "")
            if user_id and self.policy_engine.evaluate_memory_write(
                category=category, content=content,
            ):
                try:
                    self.memory_service.repository.save_memory(
                        user_id=user_id,
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        category=category,
                        content=content,
                        source_session_id=session_id,
                    )
                except Exception as exc:
                    logger.warning("Failed to save tool memory: %s", exc)

        # Follow-up turn for non-escalation tools
        if tool_was_called and tool_name != "escalate_to_human":
            llm_messages.append(LLMMessage(role="assistant", content=raw_reply))
            llm_messages.append(LLMMessage(
                role="user",
                content=(
                    f"Tool Result: {tool_result.output} — respond to the user "
                    f"based on this result."
                ),
            ))
            followup_request = LLMRequest(messages=llm_messages)
            followup_response = self.model_router.generate(
                followup_request, preferred_provider=provider,
            )
            raw_reply = followup_response.content

        # 10. Policy engine
        decision = self.policy_engine.evaluate_response(
            raw_reply=raw_reply,
            tool_was_called=tool_was_called,
            tool_name=tool_name,
            tool_result_metadata=tool_result_metadata,
        )
        final_reply = decision.modified_reply

        # 11. Escalation
        if decision.escalated:
            self.escalation_service.handle_escalation(
                session_id=session_id,
                user_id=user_id,
                tenant_id=tenant_id,
                reason=decision.escalation_reason,
            )

        # 12. Persist assistant reply
        # 12. Persist assistant reply — capture id for 27b idempotency key
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
                    # Step 27b: enqueue; worker re-reads turn window from DB.
                    # Fail-open: a down worker must NOT break the chat turn.
                    try:
                        self.memory_service.enqueue_extraction(
                            user_id=user_id,
                            tenant_id=tenant_id,
                            session_id=session_id,
                            message_id=assistant_msg.id,
                            actor_key_prefix=actor_key_prefix,
                            agent_id=agent_id,
                            luciel_instance_id=luciel_instance_id,
                            actor_user_id=actor_user_id,  # Step 24.5b File 2.5
                            trace_id=None,  # no trace_id yet at this point in flow
                        )
                        memories_extracted = 0  # real count lands in audit row
                    except Exception as enq_exc:
                        # Broker down, validation fail, etc. — log and move on.
                        # Chat turn already succeeded; memory write pauses until
                        # worker recovers. Queue-depth alarm surfaces the gap.
                        # Step 28 C8 (P3-O sweep): repr(enq_exc) so
                        # broker / validation failures surface the
                        # actual message instead of just the class.
                        logger.warning(
                            "enqueue_extraction failed (fail-open): type=%s "
                            "exc_repr=%r session=%s message_id=%s",
                            type(enq_exc).__name__,
                            enq_exc,
                            session_id,
                            assistant_msg.id,
                        )
                else:
                    # Legacy sync path — still idempotent if message_id provided.
                    memories_extracted = self.memory_service.extract_and_save(
                        user_id=user_id,
                        tenant_id=tenant_id,
                        session_id=session_id,
                        agent_id=agent_id,
                        messages=recent_messages,
                        message_id=assistant_msg.id,
                        luciel_instance_id=luciel_instance_id,
                        actor_user_id=actor_user_id,  # Step 24.5b File 2.5
                    )
            except Exception as exc:
                # Step 28 C8 (P3-O sweep): repr(exc) on the outer
                # extractor wrapper too. Per-item save failures are
                # already audit-rowed inside extract_and_save; this
                # block catches things that happen *before* the save
                # loop (LLM extractor errors, malformed messages,
                # etc), where a durable audit row is not warranted
                # but the diagnostic detail is.
                logger.warning(
                    "Memory extraction failed: type=%s exc_repr=%r "
                    "session=%s message_id=%s",
                    type(exc).__name__, exc, session_id,
                    getattr(assistant_msg, "id", None),
                )

        # 14. Trace (now carries luciel_instance_id)
        try:
            self.trace_service.record_trace(
                session_id=session_id,
                user_id=user_id,
                tenant_id=tenant_id,
                domain_id=domain_id,
                agent_id=agent_id,
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
                tenant_config_id=ctx.tenant_config_id,
                domain_config_id=ctx.domain_config_id,
                agent_config_id=ctx.agent_config_id,
                luciel_instance_id=ctx.luciel_instance_id,  # Step 24.5 File 15
            )
        except Exception as exc:
            logger.warning("Trace recording failed: %s", exc)

        # 15. Return
        return final_reply

    def respond_stream(
        self,
        *,
        session_id: str,
        message: str,
        provider: str | None = None,
        caller_tenant_id: str | None = None,
        luciel_instance_id: int | None = None,  # Step 24.5 File 15
        actor_key_prefix: str | None = None,  # Step 27b
        actor_user_id: "uuid.UUID | None" = None,  # Step 24.5b File 2.5
    ):
        """Token-by-token streaming variant. Same setup as respond()."""

        session = self.session_service.get_session(session_id)
        if session is None:
            raise ValueError("Session not found")
        if caller_tenant_id and session.tenant_id != caller_tenant_id:
            raise PermissionError("Session does not belong to this tenant")

        user_id = session.user_id
        tenant_id = session.tenant_id
        domain_id = session.domain_id
        agent_id = getattr(session, "agent_id", None)

        self.session_service.add_message(
            session_id=session_id, role="user", content=message,
        )

        # Step 24.5 File 15: unified context resolution.
        ctx = self._resolve_luciel_context(
            luciel_instance_id=luciel_instance_id,
            tenant_id=tenant_id,
            domain_id=domain_id,
            agent_id=agent_id,
        )

        if not provider and ctx.preferred_provider:
            provider = ctx.preferred_provider

        memories: list = []
        can_use_memory = True
        if self.consent_policy and user_id:
            can_use_memory = self.consent_policy.can_persist_memory(
                user_id=user_id, tenant_id=tenant_id,
            )
        if user_id and can_use_memory:
            memories = self.memory_service.retrieve_memories(
                user_id=user_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
            )

        knowledge = self.knowledge_retriever.retrieve(
            query=message,
            tenant_id=tenant_id,
            domain_id=domain_id,
            agent_id=agent_id,
            luciel_instance_id=ctx.luciel_instance_id,
        )

        history = self.session_service.list_messages(session_id)

        tool_descriptions = self.tool_registry.get_tool_descriptions(
            allowed=ctx.allowed_tools,
        )
        tenant_prompt, domain_prompt, agent_layer = (
            self._compose_system_prompt_additions(ctx)
        )
        system_prompt = build_system_prompt(
            memories=memories if memories else None,
            tool_descriptions=tool_descriptions if tool_descriptions else None,
            tenant_prompt=tenant_prompt,
            domain_prompt=domain_prompt,
            agent_prompt=agent_layer,
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

            # After streaming completes, persist the full reply.
            full_reply = "".join(full_reply_parts)

            decision = self.policy_engine.evaluate_response(
                raw_reply=full_reply,
                tool_was_called=False,
                tool_name=None,
                tool_result_metadata=None,
            )
            final_reply = decision.modified_reply

            if decision.escalated:
                self.escalation_service.handle_escalation(
                    session_id=session_id,
                    user_id=user_id,
                    tenant_id=tenant_id,
                    reason=decision.escalation_reason,
                )

            # Persist assistant reply — capture id for 27b idempotency key
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
                        # Step 27b async path (fail-open)
                        try:
                            self.memory_service.enqueue_extraction(
                                user_id=user_id,
                                tenant_id=tenant_id,
                                session_id=session_id,
                                message_id=assistant_msg.id,
                                actor_key_prefix=actor_key_prefix,
                                agent_id=agent_id,
                                luciel_instance_id=luciel_instance_id,
                                actor_user_id=actor_user_id,  # Step 24.5b File 2.5
                                trace_id=None,
                            )
                        except Exception as enq_exc:
                            # Step 28 C8 (P3-O sweep): repr(enq_exc).
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
                            tenant_id=tenant_id,
                            session_id=session_id,
                            agent_id=agent_id,
                            messages=recent_messages,
                            message_id=assistant_msg.id,
                            luciel_instance_id=luciel_instance_id,
                            actor_user_id=actor_user_id,  # Step 24.5b File 2.5
                        )
                except Exception as exc:
                    # Step 28 C8 (P3-O sweep): repr(exc) on stream path too.
                    logger.warning(
                        "Memory extraction failed: type=%s exc_repr=%r "
                        "session=%s message_id=%s",
                        type(exc).__name__, exc, session_id,
                        getattr(assistant_msg, "id", None),
                    )

            # Step 24.5 File 15: record trace for the streaming path too,
            # so bound LucielInstances appear in audit regardless of
            # which chat entry point the caller used.
            try:
                self.trace_service.record_trace(
                    session_id=session_id,
                    user_id=user_id,
                    tenant_id=tenant_id,
                    domain_id=domain_id,
                    agent_id=agent_id,
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
                    tenant_config_id=ctx.tenant_config_id,
                    domain_config_id=ctx.domain_config_id,
                    agent_config_id=ctx.agent_config_id,
                    luciel_instance_id=ctx.luciel_instance_id,
                )
            except Exception as exc:
                logger.warning("Trace recording failed (stream): %s", exc)

        return token_generator()