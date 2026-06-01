"""Arc 14 U4 — §3.4.7 structured conversation summary.

Summarization is always-on cognition (§3.4): no button, every tier. The
orchestrator's COGNITION FINALIZATION step persists a structured summary
alongside the captured lead row so the operator who picks the lead up
has the conversation recap inline.

Why deterministic
-----------------
Like ``lead_capture.detect``, this is deterministic (no LLM call) so it
is hermetic + free in tests and a stable, assertable boundary. It mirrors
the pre-WU7 ``SessionSummaryTool`` shape that ``CognitionService.
_handle_session_summary`` preserves (role-prefixed, 150-char preview per
message) so the summary the lead row carries is the SAME recap shape the
folded ``get_session_summary`` behaviour produces. A richer LLM-backed
summarizer is a later hook — it can replace ``summarize`` without
touching the finalizer or the lead row shape.
"""
from __future__ import annotations

_PREVIEW_CHARS = 150


def summarize(messages: list[dict] | None) -> str:
    """Return a structured recap of the conversation messages.

    ``messages`` is the role/content turn list (``{"role", "content"}``),
    oldest→newest. Matches the pre-WU7 ``SessionSummaryTool`` formatting
    exactly (uppercased role prefix, 150-char preview per message) so the
    persisted summary equals the folded ``get_session_summary`` output.

    Never raises — a malformed turn list degrades to the empty-session
    line rather than crashing finalization.
    """
    try:
        if not messages:
            return "No messages in this session yet."

        parts = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "") or ""
            preview = (
                content[:_PREVIEW_CHARS] + "..."
                if len(content) > _PREVIEW_CHARS
                else content
            )
            parts.append(f"{role.upper()}: {preview}")

        body = "\n".join(parts)
        return f"Session summary ({len(messages)} messages):\n{body}"
    except Exception:  # noqa: BLE001 — never crash finalization over a summary
        return "No messages in this session yet."
