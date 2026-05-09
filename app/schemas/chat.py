from __future__ import annotations

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    # Session ID is still required — the client creates a session first.
    session_id: str

    # The user's message. Max 10,000 chars (~2,500 tokens).
    message: str = Field(..., min_length=1, max_length=10_000)

    # Optional provider override.
    provider: str | None = None


class ChatResponse(BaseModel):
    session_id: str
    reply: str


class ChatWidgetRequest(BaseModel):
    """Step 30b: payload for the public widget SSE endpoint.

    Distinct from ChatRequest in two ways:
      * NO `provider` override field. The widget is locked to whatever
        the bound LucielInstance configures. A public-facing surface
        must not let an attacker steer the model choice via a JSON
        field; provider selection is a server-controlled invariant.
      * `session_id` is OPTIONAL. The widget creates a session lazily
        on first message via the existing session-create path. The
        backend echoes the resolved session_id on the first SSE frame
        so the widget can persist it locally for follow-up turns.
    """

    session_id: str | None = None
    message: str = Field(..., min_length=1, max_length=10_000)