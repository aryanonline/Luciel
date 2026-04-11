"""
Memory extractor.

After each chat turn, this module asks the LLM to identify
durable facts worth remembering about the user.

The extractor works by sending the recent conversation to the model
with a specialized extraction prompt, then parsing the structured output.

This is intentionally a separate LLM call from the main chat response,
because extraction requires a different instruction set than conversation.

To improve extraction quality later:
- Tune the extraction prompt.
- Add deduplication against existing memories.
- Add confidence thresholds.
- Use a cheaper/faster model for extraction.
"""

from __future__ import annotations

import json
import logging

from app.integrations.llm.base import LLMMessage, LLMRequest
from app.integrations.llm.router import ModelRouter

logger = logging.getLogger(__name__)

EXTRACTION_PROMPT = """You are a memory extraction system. Your job is to identify durable facts about the user from the conversation below.

Extract ONLY facts that would be useful to remember in future conversations.

Categories:
- preference: Things the user likes or prefers
- constraint: Hard limits or requirements
- goal: What the user is trying to achieve
- fact: Factual information about the user
- operational: How the user prefers to interact

Rules:
- Only extract facts clearly stated or strongly implied by the user.
- Do NOT extract temporary or trivial information.
- Do NOT extract facts about the assistant.
- Each memory should be a short, clear sentence.
- If there is nothing worth remembering, return an empty array.

Return a JSON array of objects with "category" and "content" fields.
Example: [{"category": "preference", "content": "Prefers 2-bedroom condos"}, {"category": "constraint", "content": "Budget is under 700k"}]

Return ONLY the JSON array, no other text."""


def extract_memories(
    messages: list[dict],
    model_router: ModelRouter,
) -> list[dict]:
    """
    Given recent conversation messages, extract durable memories.

    Args:
        messages: List of {"role": "...", "content": "..."} dicts
                  from the recent conversation.
        model_router: The model router to use for the extraction call.

    Returns:
        List of {"category": "...", "content": "..."} dicts.
        Returns empty list if nothing worth remembering or on error.
    """

    # Build a readable conversation transcript for the extraction model.
    transcript = "\n".join(
        f"{msg['role'].upper()}: {msg['content']}"
        for msg in messages
        if msg["role"] in ("user", "assistant")
    )

    llm_request = LLMRequest(
        messages=[
            LLMMessage(role="system", content=EXTRACTION_PROMPT),
            LLMMessage(role="user", content=transcript),
        ],
        temperature=0.1,  # Low temperature for consistent extraction.
        max_tokens=1024,
    )

    try:
        response = model_router.generate(llm_request)
        memories = json.loads(response.content)

        # Basic validation: must be a list of dicts with required keys.
        valid = []
        for item in memories:
            if (
                isinstance(item, dict)
                and "category" in item
                and "content" in item
            ):
                valid.append(item)

        return valid

    except (json.JSONDecodeError, Exception) as exc:
        logger.warning("Memory extraction failed: %s", exc)
        return []