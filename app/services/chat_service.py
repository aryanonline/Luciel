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
"""

from __future__ import annotations

import logging

from app.integrations.llm.base import LLMMessage, LLMRequest
from app.integrations.llm.router import ModelRouter
from app.knowledge.retriever import KnowledgeRetriever
from app.memory.service import MemoryService
from app.persona.luciel_core import build_system_prompt
from app.policy.engine import PolicyEngine
from app.policy.escalation import EscalationService
from app.repositories.config_repository import ConfigRepository
from app.services.session_service import SessionService
from app.services.trace_service import TraceService
from app.tools.broker import ToolBroker
from app.tools.registry import ToolRegistry
from app.policy.consent import ConsentPolicy

logger = logging.getLogger(__name__)


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
        self.consent_policy = consent_policy
        self.policy_engine = PolicyEngine()
        self.escalation_service = EscalationService()

    def _resolve_allowed_tools(self, domain_config) -> list[str] | None:
        """
        Determine which tools are allowed for this request.

        Returns None if no restrictions (all tools available).
        Returns a list of tool names if domain config restricts tools.
        """
        if domain_config and domain_config.allowed_tools:
            return domain_config.allowed_tools
        return None

    def respond(
        self,
        *,
        session_id: str,
        message: str,
        provider: str | None = None,
        caller_tenant_id: str | None = None,
    ) -> str:

        # 1. Verify session
        session = self.session_service.get_session(session_id)
        if session is None:
            raise ValueError("Session not found")
        # 1b. Enforce tenant ownership  ← ADD THIS BLOCK
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

        # 3. Load tenant config
        tenant_config = self.config_repository.get_tenant_config(tenant_id)
        tenant_prompt = None
        tenant_config_id = None
        if tenant_config:
            tenant_prompt = tenant_config.system_prompt_additions
            tenant_config_id = tenant_config.id

        # 4. Load domain config
        domain_config = self.config_repository.get_domain_config(tenant_id, domain_id)
        domain_prompt = None
        domain_config_id = None
        preferred_provider = None
        if domain_config:
            domain_prompt = domain_config.system_prompt_additions
            domain_config_id = domain_config.id
            preferred_provider = domain_config.preferred_provider

        # 5. Load agent config
        agent_prompt = None
        agent_config_id = None
        assistant_name = "Luciel"
        if agent_id:
            agent_config = self.config_repository.get_agent_config(tenant_id, agent_id)
            if agent_config:
                agent_prompt = agent_config.system_prompt_additions
                agent_config_id = agent_config.id
                assistant_name = agent_config.display_name or "Luciel"
                if agent_config.preferred_provider:
                    preferred_provider = agent_config.preferred_provider

        # Use the most specific preferred provider if caller did not specify one
        if not provider and preferred_provider:
            provider = preferred_provider

        # 6. Retrieve long-term memories (now agent-scoped)
        memories = []
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

        # 7. Retrieve relevant knowledge from vector DB (now agent-scoped)
        knowledge = self.knowledge_retriever.retrieve(
            query=message,
            tenant_id=tenant_id,
            domain_id=domain_id,
            agent_id=agent_id,
        )

        # 8. Load conversation history
        history = self.session_service.list_messages(session_id)

        # 9. Build the full child Luciel prompt (tools filtered by domain config)
        allowed_tools = self._resolve_allowed_tools(domain_config)
        tool_descriptions = self.tool_registry.get_tool_descriptions(
            allowed=allowed_tools,
        )
        system_prompt = build_system_prompt(
            memories=memories if memories else None,
            tool_descriptions=tool_descriptions if tool_descriptions else None,
            tenant_prompt=tenant_prompt,
            domain_prompt=domain_prompt,
            agent_prompt=agent_prompt,
            knowledge=knowledge if knowledge else None,
            assistant_name=assistant_name,
        )

        llm_messages = [LLMMessage(role="system", content=system_prompt)]
        for msg in history:
            llm_messages.append(LLMMessage(role=msg.role, content=msg.content))

        # 10. Call LLM
        llm_request = LLMRequest(messages=llm_messages)
        llm_response = self.model_router.generate(
            llm_request, preferred_provider=provider
        )
        raw_reply = llm_response.content

        # Track metadata for trace
        llm_provider_used = llm_response.provider
        llm_model_used = llm_response.model

        # 11. Check for tool call
        tool_was_called = False
        tool_name = None
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

        # Handle save_memory (now agent-scoped)
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

        # Follow-up for non-escalation tools
        if tool_was_called and tool_name != "escalate_to_human":
            llm_messages.append(LLMMessage(role="assistant", content=raw_reply))
            llm_messages.append(LLMMessage(
                role="user",
                content=f"Tool Result: {tool_result.output} — respond to the user based on this result.",
            ))
            followup_request = LLMRequest(messages=llm_messages)
            followup_response = self.model_router.generate(
                followup_request, preferred_provider=provider,
            )
            raw_reply = followup_response.content

        # 12. Run policy engine
        decision = self.policy_engine.evaluate_response(
            raw_reply=raw_reply,
            tool_was_called=tool_was_called,
            tool_name=tool_name,
            tool_result_metadata=tool_result_metadata,
        )
        final_reply = decision.modified_reply

        # 13. Handle escalation
        if decision.escalated:
            self.escalation_service.handle_escalation(
                session_id=session_id,
                user_id=user_id,
                tenant_id=tenant_id,
                reason=decision.escalation_reason,
            )

        # 14. Persist the final reply
        self.session_service.add_message(
            session_id=session_id, role="assistant", content=final_reply,
        )

        # 15. Extract and save new memories (now agent-scoped)
        memories_extracted = 0
        if user_id and can_use_memory:
            recent_messages = [
                {"role": "user", "content": message},
                {"role": "assistant", "content": final_reply},
            ]
            try:
                memories_extracted = self.memory_service.extract_and_save(
                    user_id=user_id,
                    tenant_id=tenant_id,
                    session_id=session_id,
                    agent_id=agent_id,
                    messages=recent_messages,
                )

            except Exception as exc:
                logger.warning("Memory extraction failed: %s", exc)

        # 16. Record trace with full metadata (now includes agent_config_id)
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
                tenant_config_id=tenant_config_id,
                domain_config_id=domain_config_id,
                agent_config_id=agent_config_id,
            )
        except Exception as exc:
            logger.warning("Trace recording failed: %s", exc)

        # 17. Return the final reply
        return final_reply

    def respond_stream(
        self,
        *,
        session_id: str,
        message: str,
        provider: str | None = None,
        caller_tenant_id: str | None = None,
    ):
        """
        Stream a response token by token.

        Does the same setup as respond() — loads session, tenant config,
        domain config, agent config, knowledge, memories, history,
        builds the prompt — but returns a generator that yields tokens
        instead of waiting for the full response.

        The full reply is persisted AFTER streaming completes.
        """
        # --- Same setup as respond() ---
        session = self.session_service.get_session(session_id)
        if session is None:
            raise ValueError("Session not found")
        
        # Enforce tenant ownership  ← ADD THIS
        if caller_tenant_id and session.tenant_id != caller_tenant_id:
            raise PermissionError("Session does not belong to this tenant")

        user_id = session.user_id
        tenant_id = session.tenant_id
        domain_id = session.domain_id
        agent_id = getattr(session, "agent_id", None)

        # Persist user message
        self.session_service.add_message(
            session_id=session_id, role="user", content=message,
        )

        # Load tenant config
        tenant_config = self.config_repository.get_tenant_config(tenant_id)
        tenant_prompt = None
        if tenant_config:
            tenant_prompt = tenant_config.system_prompt_additions

        # Load domain config
        domain_config = self.config_repository.get_domain_config(tenant_id, domain_id)
        domain_prompt = None
        preferred_provider = None
        if domain_config:
            domain_prompt = domain_config.system_prompt_additions
            preferred_provider = domain_config.preferred_provider

        # Load agent config
        agent_prompt = None
        assistant_name = "Luciel"
        if agent_id:
            agent_config = self.config_repository.get_agent_config(tenant_id, agent_id)
            if agent_config:
                agent_prompt = agent_config.system_prompt_additions
                assistant_name = agent_config.display_name or "Luciel"
                if agent_config.preferred_provider:
                    preferred_provider = agent_config.preferred_provider

        if not provider and preferred_provider:
            provider = preferred_provider

        # Retrieve memories (now agent-scoped)
        memories = []
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

        # Retrieve knowledge (now agent-scoped)
        knowledge = self.knowledge_retriever.retrieve(
            query=message,
            tenant_id=tenant_id,
            domain_id=domain_id,
            agent_id=agent_id,
        )

        # Load history
        history = self.session_service.list_messages(session_id)

        # Build prompt (tools filtered by domain config)
        allowed_tools = self._resolve_allowed_tools(domain_config)
        tool_descriptions = self.tool_registry.get_tool_descriptions(
            allowed=allowed_tools,
        )
        system_prompt = build_system_prompt(
            memories=memories if memories else None,
            tool_descriptions=tool_descriptions if tool_descriptions else None,
            tenant_prompt=tenant_prompt,
            domain_prompt=domain_prompt,
            agent_prompt=agent_prompt,
            knowledge=knowledge if knowledge else None,
            assistant_name=assistant_name,
        )

        llm_messages = [LLMMessage(role="system", content=system_prompt)]
        for msg in history:
            llm_messages.append(LLMMessage(role=msg.role, content=msg.content))

        llm_request = LLMRequest(messages=llm_messages)

        # --- Stream instead of generate ---
        full_reply_parts = []

        def token_generator():
            for token in self.model_router.generate_stream(
                llm_request, preferred_provider=provider
            ):
                full_reply_parts.append(token)
                yield token

            # After streaming completes, persist the full reply
            full_reply = "".join(full_reply_parts)

            # Run policy engine
            decision = self.policy_engine.evaluate_response(
                raw_reply=full_reply,
                tool_was_called=False,
                tool_name=None,
                tool_result_metadata=None,
            )
            final_reply = decision.modified_reply

            # Handle escalation
            if decision.escalated:
                self.escalation_service.handle_escalation(
                    session_id=session_id,
                    user_id=user_id,
                    tenant_id=tenant_id,
                    reason=decision.escalation_reason,
                )

            # Persist assistant reply
            self.session_service.add_message(
                session_id=session_id, role="assistant", content=final_reply,
            )

            # Extract and save memories (now agent-scoped)
            if user_id and can_use_memory:
                recent_messages = [
                    {"role": "user", "content": message},
                    {"role": "assistant", "content": final_reply},
                ]
                try:
                    self.memory_service.extract_and_save(
                        user_id=user_id,
                        tenant_id=tenant_id,
                        session_id=session_id,
                        agent_id=agent_id,
                        messages=recent_messages,
                    )
                except Exception as exc:
                    logger.warning("Memory extraction failed: %s", exc)

        return token_generator()
