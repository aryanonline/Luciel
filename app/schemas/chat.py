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