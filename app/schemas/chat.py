from __future__ import annotations

from pydantic import BaseModel


class ChatRequest(BaseModel):
    # The session this message belongs to.
    session_id: str

    # The raw user input sent to Luciel.
    message: str

    # Optional: explicitly choose a provider ("openai" or "anthropic").
    # If not provided, Luciel uses the default from config.
    provider: str | None = None


class ChatResponse(BaseModel):
    # Echo back the session so the client can continue the thread.
    session_id: str

    # Luciel's reply.
    reply: str