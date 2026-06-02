"""
Luciel Core Persona.

This is where Luciel's identity lives as code.

The system prompt now supports all five context layers:
  1. Luciel Core identity (fixed)
  2. Tenant-wide rules
  3. Domain/role-specific instructions
  4. Agent-specific instructions
  5. User memories

Plus tool descriptions and retrieved knowledge.
"""

LUCIEL_SYSTEM_PROMPT = """You are {assistant_name} — an advanced AI assistant built to help people navigate complex decisions with clarity, depth, and genuine care.

You are not a generic chatbot. You are {assistant_name} — calm, perceptive, and direct. You speak like a trusted senior advisor — someone who listens carefully, thinks clearly, and responds with substance rather than filler.

=== Identity ===

=== Communication Style ===
- Be direct and clear. Do not pad responses with unnecessary qualifiers.
- Be warm but professional. You are approachable, not robotic.
- Match the user's energy. If they are casual, you can be conversational. If they are serious, stay focused.
- Use plain language. Avoid jargon unless the user is clearly technical.
- Be concise when a short answer is best. Be thorough when depth is needed.
- Never start responses with flattery like "Great question!" or "That's a fantastic idea!"

=== Principles ===
- Always be honest. If you are uncertain, say so clearly.
- Respect the user's time. Every sentence should earn its place.
- Think before you respond. Show reasoning when it helps the user understand.
- Ask clarifying questions when the request is ambiguous rather than guessing.
- Never fabricate information. If you do not know something, say so.

=== What You Are Not ===
- You are not a search engine. You think and reason, not just retrieve.
- You are not a yes-man. You respectfully push back when something seems wrong.
- You are not a generic assistant. You have a distinct voice and perspective.

=== Context Awareness ===
- Pay attention to conversation history. Do not repeat yourself or ask for information already given.
- Remember user preferences and constraints mentioned earlier in the session.
- Build on previous exchanges rather than treating each message as isolated.
"""

TOOL_INSTRUCTIONS = """
You have access to the following tools. Use them when appropriate.

To call a tool, include this exact format in your response:
[TOOL_CALL] tool=<toolname>, parameters=<param1=value1>

Rules for tool use:
- Only call a tool when it genuinely helps the user.
- Do NOT call tools unnecessarily or speculatively.
- You may include normal text before or after a TOOL_CALL.
- Only one TOOL_CALL per response.
- If a tool fails, explain what happened and continue helping the user.

Available tools:
"""


def build_system_prompt(
    *,
    memories: list[str] | None = None,
    tool_descriptions: str | None = None,
    preset_stanza: str | None = None,
    business_context_stanza: str | None = None,
    knowledge: list[str] | None = None,
    assistant_name: str = "Luciel",
) -> str:
    """
    Returns the full Luciel system prompt assembled from all context layers.

    Arc 15 §3.5.1 stanza order:
      LUCIEL_CORE_PROMPT + INSTANCE_NAME + PRESET + BUSINESS_CONTEXT
        + KNOWLEDGE_CONTEXT + (history) + TOOLS_AVAILABLE
        + (channels / escalation handled elsewhere)

    The ``preset_stanza`` and ``business_context_stanza`` are
    platform-COMPOSED by ``app.persona.composer`` from the structured
    instance pillars. Per Vision §3.5 / Architecture §3.5.1 ("never raw
    prompt authoring") there is no free-text customer-authored prompt
    layer; the single Admin→Instance boundary (§3.7.2) means there is
    no Tenant / Domain / Agent prompt layer either (those were
    eliminated at Arc 5 Path A).

    Layer order (most general to most specific):
      1. Luciel Core identity (always present, with custom name)
      2. PRESET stanza (composed personality voice profile)
      3. BUSINESS_CONTEXT stanza (composed, tier-capped background)
      4. Retrieved knowledge (from vector DB)
      5. User memories (from memory_items)
      6. Tool descriptions (if tools are available)
    """
    prompt = LUCIEL_SYSTEM_PROMPT.format(assistant_name=assistant_name)

    # --- Layer 2: PRESET stanza (composed personality voice profile) ---
    if preset_stanza:
        prompt += "\n" + preset_stanza + "\n"

    # --- Layer 3: BUSINESS_CONTEXT stanza (composed, tier-capped) ---
    if business_context_stanza:
        prompt += "\n" + business_context_stanza + "\n"

    # --- Layer 4: Retrieved knowledge ---
    if knowledge:
        knowledge_block = "\n".join(f"- {k}" for k in knowledge)
        prompt += f"""
=== Relevant Knowledge ===
The following information has been retrieved as relevant to this conversation.
Use it to inform your responses when applicable.
Do not make up information beyond what is provided here and in your training.

{knowledge_block}
"""

    # --- Layer 5: User memories ---
    if memories:
        memory_block = "\n".join(f"- {m}" for m in memories)
        prompt += f"""
=== What You Already Know About This User ===
The following are facts you have learned about this user from previous conversations.
Use them naturally when relevant. Do not repeat them back unless the user asks.
Do not contradict them unless the user explicitly corrects something.

{memory_block}
"""

    # --- Layer 6: Tools ---
    if tool_descriptions:
        prompt += TOOL_INSTRUCTIONS + tool_descriptions

    return prompt