"""Arc 14 U4 — §3.4.7 structured conversation summary.

Summarization is always-on cognition (§3.4): no button, every tier. The
orchestrator's COGNITION FINALIZATION step persists a structured summary
alongside the captured lead row so the operator who picks the lead up
has the conversation recap inline.

Single source of truth (Arc 14 U5 — the fold de-dup)
----------------------------------------------------
The recap formatting lives in exactly ONE place:
``app.cognition.service.format_session_summary``. Both the live chat-path
behaviour (``CognitionService._handle_session_summary``) and this
finalizer-facing entry point delegate to it, so the summary the lead row
carries is BYTE-IDENTICAL to the folded ``get_session_summary`` output —
behaviour-equivalence by construction, not by two copies kept in sync.

This module stays as the finalizer's import seam (``summarize``) so a
richer LLM-backed summarizer could later replace the delegation here
without touching the finalizer or the lead row shape.
"""
from __future__ import annotations

from app.cognition.service import format_session_summary


def summarize(messages: list[dict] | None) -> str:
    """Return a structured recap of the conversation messages.

    ``messages`` is the role/content turn list (``{"role", "content"}``),
    oldest→newest. Delegates to the single ``format_session_summary``
    implementation so the persisted summary equals the folded
    ``get_session_summary`` output exactly. Never raises.
    """
    return format_session_summary(messages)
