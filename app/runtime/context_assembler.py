"""Runtime context assembler — composes the prompt layers per
Architecture v1 §3.5.1.

Arc 11 Step 8 adds the ``retrieved_chunks`` kw-only parameter to
``build_prompt``. When provided + non-empty, a ``KNOWLEDGE_CONTEXT``
stanza is appended to the prompt, carrying the chunks' content in
relevance order (the retriever already returned them sorted by
cosine distance ascending). When ``None`` or empty, the prompt is
byte-identical to the pre-Step-8 shape, preserving backward
compatibility.

Budget
------

Architecture v1 §3.5.1 says "include the top-k chunks." It does not
spell out a byte budget. v1 enforces a soft 8 KB ceiling on the
concatenated KNOWLEDGE_CONTEXT body, dropping later (less-relevant)
chunks if the budget overruns. The constant ``_KNOWLEDGE_CONTEXT_BUDGET_BYTES``
is documented here as the single knob; Arc 14 will likely promote it
to a per-tier setting once the agentic loop is real and prompt-
budget arithmetic actually matters.
"""
from __future__ import annotations

from typing import Sequence

from app.knowledge.retriever import RetrievedChunk
from app.persona.luciel_core import build_system_prompt
from app.runtime.contracts import RuntimeRequest


# v1 ceiling for the KNOWLEDGE_CONTEXT stanza body. Sized so the
# combined system prompt + identity + 5 chunks fits comfortably
# inside an 8K-token context window with headroom. Conservative.
_KNOWLEDGE_CONTEXT_BUDGET_BYTES: int = 8 * 1024


class ContextAssembler:
    """Arc 5 Path A: assembles the runtime prompt from the canonical
    ``build_system_prompt`` layer-builder. The legacy ``LUCIEL_IDENTITY``
    string constant was retired during the persona refactor; the
    replacement is ``build_system_prompt(...)`` which formats the
    canonical ``LUCIEL_SYSTEM_PROMPT`` template with ``assistant_name``.
    Arc 12 EX1d collapsed the v1 tenant/domain/agent layering to the
    v2 single Admin→Instance boundary (Architecture §3.7.2).
    """

    def build_prompt(
        self,
        req: RuntimeRequest,
        *,
        retrieved_chunks: Sequence[RetrievedChunk] | None = None,
    ) -> str:
        identity = build_system_prompt()
        # Arc 12 EX1d: v1 ``Domain:`` line removed — v2 has a single
        # Admin→Instance boundary (Architecture §3.7.2). The prompt now
        # carries the Admin slug and the channel only.
        base = (
            f"{identity}\n\n"
            f"Tenant: {req.admin_id}\nChannel: {req.channel}\n"
        )
        knowledge_stanza = self._render_knowledge_context(retrieved_chunks)
        return (
            f"{base}"
            f"{knowledge_stanza}"
            f"User message: {req.message}\n"
            "Respond as Luciel with clarity and restraint."
        )

    @staticmethod
    def _render_knowledge_context(
        chunks: Sequence[RetrievedChunk] | None,
    ) -> str:
        """Return the ``KNOWLEDGE_CONTEXT`` stanza or an empty string.

        Truncates at ``_KNOWLEDGE_CONTEXT_BUDGET_BYTES`` of chunk
        body (not counting headers / separators). Chunks arrive
        sorted by relevance — later chunks are the ones we drop.
        """
        if not chunks:
            return ""

        budget = _KNOWLEDGE_CONTEXT_BUDGET_BYTES
        body_parts: list[str] = []
        included = 0
        for chunk in chunks:
            content = chunk.content or ""
            # Use UTF-8 byte length so the budget is meaningful for
            # non-ASCII content (knowledge bases in French, Arabic
            # etc.; data_residency in ca-central-1 doesn't preclude
            # multi-language content).
            chunk_bytes = len(content.encode("utf-8"))
            if chunk_bytes > budget and included == 0:
                # The first (most-relevant) chunk alone overruns the
                # budget. Truncate it at the byte boundary instead of
                # dropping the whole thing — partial context is more
                # useful than no context.
                truncated = content.encode("utf-8")[:budget].decode(
                    "utf-8", errors="ignore",
                )
                body_parts.append(truncated)
                included = 1
                break
            if chunk_bytes > budget:
                break
            body_parts.append(content)
            budget -= chunk_bytes
            included += 1

        if not body_parts:
            return ""

        # Architecture §3.5.1 names the stanza ``KNOWLEDGE_CONTEXT``;
        # the prompt template is whitespace-delimited so we wrap with
        # a clearly-bounded header + separator so the LLM can lift
        # the boundaries reliably.
        separator = "\n---\n"
        joined = separator.join(body_parts)
        return f"KNOWLEDGE_CONTEXT:\n{joined}\n\n"
