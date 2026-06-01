"""Always-on cognition module (§3.4).

================================================================
This module hosts the three always-on cognition behaviours —
escalate (human handoff), save_memory (deliberate fact
persistence), and session_summary (conversation recap). Per
Architecture §3.4 cognition is always-on, every tier, never
admin-configurable, and per Decision #20 it is NOT in the tool
registry.

Arc 14 reality (corrected at U5 closeout)
-----------------------------------------
The pre-U5 header claimed cognition would be fully "absorbed into
the Arc 14 agentic loop" and that "this module disappears at that
point". That did NOT happen. ``CognitionService`` remains the LIVE
chat-path implementation (``ChatService.respond`` → ``/v1/chat``,
``chat_widget``, ``twilio_webhook``), wired via
``app.api.deps.get_chat_service``. What Arc 14 DID do: the agentic
loop runs its OWN cognition finalization (``app.cognition.
finalizer``) and SHARES one summary implementation with this module
(``service.format_session_summary``) rather than duplicating it.
See ``service.py`` for the full fold note.

Founder ruling 4 (Arc 12), still in force here:
  4b — behaviour-preserving, NOT behaviour-expanding for the
       chat-path intents.
  4c — minimal & non-tier-gated. NO broker, NO registry, NO
       tier-gating. Called DIRECTLY by chat_service.

================================================================
"""
from __future__ import annotations

from app.cognition.service import CognitionOutcome, CognitionService

__all__ = ["CognitionOutcome", "CognitionService"]
