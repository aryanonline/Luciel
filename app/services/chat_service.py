"""
ChatService — coordinates one full chat turn.

Now includes full observability:
- Every chat turn produces a trace record.
- Traces capture memories, tool calls, policy decisions, and LLM metadata.
"""

from __future__ import annotations

import logging

from app.integrations.llm.base import LLMMessage, LLMRequest
from app.integrations.llm.router import ModelRouter
from app.memory.service import MemoryService
from app.persona.luciel_core import build_system_prompt
from app.policy.engine import PolicyEngine
from app.policy.escalation import EscalationService
from app.services.session_service import SessionService
from app.services.trace_service import TraceService
from app.tools.broker import ToolBroker
from app.tools.registry import ToolRegistry

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
    ) -> None:
        self.session_service = session_service
        self.memory_service = memory_service
        self.model_router = model_router
        self.tool_registry = tool_registry
        self.tool_broker = tool_broker
        self.trace_service = trace_service
        self.policy_engine = PolicyEngine()
        self.escalation_service = EscalationService()

    def respond(
        self,
        *,
        session_id: str,
        message: str,
        provider: str | None = None,
    ) -> str:
        # 1. Verify session
        session = self.session_service.get_session(session_id)
        if session is None:
            raise ValueError("Session not found")

        user_id = session.user_id
        tenant_id = session.tenant_id

        # 2. Persist user message
        self.session_service.add_message(
            session_id=session_id,
            role="user",
            content=message,
        )

        # 3. Retrieve long-term memories
        memories = []
        if user_id:
            memories = self.memory_service.retrieve_memories(
                user_id=user_id,
                tenant_id=tenant_id,
            )

        # 4. Load conversation history
        history = self.session_service.list_messages(session_id)

        # 5. Build prompt
        tool_descriptions = self.tool_registry.get_tool_descriptions()
        system_prompt = build_system_prompt(
            memories=memories if memories else None,
            tool_descriptions=tool_descriptions,
        )

        llm_messages = [
            LLMMessage(role="system", content=system_prompt),
        ]
        for msg in history:
            llm_messages.append(LLMMessage(role=msg.role, content=msg.content))

        # 6. Call LLM
        llm_request = LLMRequest(messages=llm_messages)
        llm_response = self.model_router.generate(llm_request, provider=provider)
        raw_reply = llm_response.content

        # Track metadata for trace
        llm_provider_used = llm_response.provider
        llm_model_used = llm_response.model

        # 7. Check for tool call
        tool_was_called = False
        tool_name = None
        tool_result_metadata = None

        tool_result = self.tool_broker.parse_and_execute(
            raw_reply,
            _messages=[
                {"role": msg.role, "content": msg.content}
                for msg in history
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

            # Handle save_memory
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
                            category=category,
                            content=content,
                            source_session_id=session_id,
                        )
                    except Exception as exc:
                        logger.warning("Failed to save tool memory: %s", exc)

            # Follow-up for non-escalation tools
            if tool_name != "escalate_to_human":
                llm_messages.append(LLMMessage(role="assistant", content=raw_reply))
                llm_messages.append(LLMMessage(
                    role="user",
                    content=f"[Tool Result: {tool_result.output}]\nNow respond to the user based on this result.",
                ))
                followup_request = LLMRequest(messages=llm_messages)
                followup_response = self.model_router.generate(
                    followup_request, provider=provider,
                )
                raw_reply = followup_response.content

        # 8. Run policy engine
        decision = self.policy_engine.evaluate_response(
            raw_reply=raw_reply,
            tool_was_called=tool_was_called,
            tool_name=tool_name,
            tool_result_metadata=tool_result_metadata,
        )

        final_reply = decision.modified_reply

        # 9. Handle escalation
        if decision.escalated:
            self.escalation_service.handle_escalation(
                session_id=session_id,
                user_id=user_id,
                tenant_id=tenant_id,
                reason=decision.escalation_reason,
            )

        # 10. Persist the final reply
        self.session_service.add_message(
            session_id=session_id,
            role="assistant",
            content=final_reply,
        )

        # 11. Extract and save new memories
        memories_extracted = 0
        if user_id:
            recent_messages = [
                {"role": "user", "content": message},
                {"role": "assistant", "content": final_reply},
            ]
            try:
                memories_extracted = self.memory_service.extract_and_save(
                    user_id=user_id,
                    tenant_id=tenant_id,
                    session_id=session_id,
                    messages=recent_messages,
                )
            except Exception as exc:
                logger.warning("Memory extraction failed: %s", exc)

        # 12. Record trace
        try:
            self.trace_service.record_trace(
                session_id=session_id,
                user_id=user_id,
                tenant_id=tenant_id,
                user_message=message,
                assistant_reply=final_reply,
                llm_provider=llm_provider_used,
                llm_model=llm_model_used,
                memories_retrieved=len(memories),
                tool_called=tool_was_called,
                tool_name=tool_name,
                escalated=decision.escalated,
                policy_flags=decision.flags if decision.flags else None,
                memories_extracted=memories_extracted,
            )
        except Exception as exc:
            # Trace failure should never break the chat flow.
            logger.warning("Trace recording failed: %s", exc)

        # 13. Return the final reply
        return final_reply