from __future__ import annotations

from pydantic import BaseModel


class ChatRequest(BaseModel):
    # Session ID is still required — the client creates a session first.
    session_id: str

    # The user's message.
    message: str

    # Optional provider override.
    provider: str | None = None


class ChatResponse(BaseModel):
    session_id: str
    reply: str