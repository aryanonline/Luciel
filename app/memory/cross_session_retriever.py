"""
CrossSessionRetriever — Step 24.5c runtime surface.

Sibling of the per-session retriever (MemoryRepository.get_user_memories +
the message-history thread on SessionModel.messages). Where the per-session
retriever answers "what did THIS session say recently?", the cross-session
retriever answers:

    Given the active session's (conversation_id, tenant_id, domain_id),
    what are the N most recent messages from SIBLING sessions under the
    same conversation, ordered by recency, capped by a per-call budget?

Returns ranked passages with provenance metadata (source_session_id,
source_channel, timestamp) that the runtime layer threads into the
foundation-model context as the cross-session leg of memory retrieval.

Scope filtering happens INSIDE the retriever, not at the caller. The
retriever refuses to return a row whose tenant_id / domain_id does not
match the calling scope, even if the same conversation_id is shared
across scopes — which it cannot be, by the conversations-table FK, but
the retriever asserts it anyway. Defense in depth, same discipline as
ARCHITECTURE §4.7. This is the FOURTH check in the scope-enforcement
chain (the first three live at the auth surface, the runtime scope
resolver, and the persistence layer's row writes).

Shape contract (ARCHITECTURE §3.2.11, §3.2.6):
    The retriever takes the same shape as every other memory retriever
    in §3.2.6. Per-session memory is sibling to cross-session memory,
    not subordinate — they're parallel legs of the same retrieval call.

Non-goals (deferred to later steps, NOT this module):
    * Identity claim resolution and conversation_id minting (sub-branch 3:
      identity_resolver).
    * Adapter wiring that asserts identity claims (sub-branch 4).
    * End-to-end harness exercising widget+programmatic-API → one
      conversation_id (sub-branch 5).
    * Cross-scope continuity reads (Step 38 territory — explicitly
      rejected for v1 in §4.9).
    * Semantic / vector-similarity ranking. v1 ranks by recency
      (newest first). The retriever signature already returns "ranked
      passages" so a future revision can swap the ordering without
      changing the caller contract.

Step 24.5c sub-branch 2 of 5. Design-lock at PR #23 (c98d752);
models + migration at PR #24 (1e761a6).
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session as SqlSession

from app.models.message import MessageModel
from app.models.session import SessionModel

logger = logging.getLogger(__name__)


# Per-call budget cap — defense in depth against caller passing an
# unbounded limit. The runtime layer is expected to pass a smaller value
# (commonly 10-20); this is the upper bound the retriever will honour
# even if asked for more. Keeps the SQL row count bounded irrespective
# of caller mistakes.
MAX_LIMIT = 100


@dataclass(frozen=True)
class CrossSessionPassage:
    """A single retrieved passage from a sibling session.

    Frozen dataclass so callers (chat runtime, prompt builder) cannot
    mutate provenance after retrieval — provenance is a contract output,
    not a hint.

    Fields:
        content:           The raw message text.
        role:              "user" | "assistant" | other role values
                           used by MessageModel.role. Caller decides
                           how to surface to the LLM.
        source_session_id: The sessions.id that produced this message.
                           Provenance per §3.2.11 retriever contract.
        source_channel:    The sessions.channel of the producing session
                           ("web", "voice", "sms", "email",
                           "programmatic_api", ...). Provenance per
                           §3.2.11 retriever contract.
        timestamp:         The message's created_at timestamp.
                           Provenance per §3.2.11 retriever contract.
        message_id:        The integer messages.id PK. Included so a
                           caller can dedupe if the same retriever is
                           run twice in one request (e.g. parallel
                           memory legs). Not part of the §3.2.11
                           provenance triple but useful for auditing.
    """
    content: str
    role: str
    source_session_id: str
    source_channel: str
    timestamp: datetime
    message_id: int


class CrossSessionRetriever:
    """Retriever for messages across sibling sessions of one conversation.

    Stateless — holds only a DB session handle. Construct once per
    request (the standard MemoryRepository pattern) or wire via
    `Depends(get_db)` in a FastAPI route. The retriever takes neither
    a User nor an identity_claim — it operates one level above identity,
    on the (conversation_id, scope) tuple that the identity resolver
    (sub-branch 3) hands it.
    """

    def __init__(self, db: SqlSession) -> None:
        self.db = db

    def retrieve(
        self,
        *,
        conversation_id: uuid.UUID,
        tenant_id: str,
        domain_id: str,
        limit: int = 20,
        exclude_session_id: str | None = None,
    ) -> list[CrossSessionPassage]:
        """Retrieve recent messages from sibling sessions in a conversation.

        Args:
            conversation_id: The Conversation.id whose sibling sessions
                we want to read. Must be a uuid.UUID (NOT a string —
                callers MUST parse before invoking, mirroring the
                MemoryRepository discipline of typed inputs).
            tenant_id:       The natural-key tenant scope. Asserted on
                EVERY returned row's session.tenant_id; mismatched
                rows are dropped (defense-in-depth, §4.7).
            domain_id:       The natural-key domain scope. Asserted on
                EVERY returned row's session.domain_id; mismatched
                rows are dropped.
            limit:           Maximum number of passages to return.
                Clamped to [1, MAX_LIMIT]. Values outside the range
                are normalised silently; a misconfigured caller does
                not break the retrieval.
            exclude_session_id: Optional sessions.id to exclude from
                results — typically the active session. The active
                session's own messages are handled by the per-session
                retriever leg, so re-surfacing them through the
                cross-session leg would double-count. None means
                no exclusion (e.g. for read-only audit replays).

        Returns:
            A list of CrossSessionPassage, newest-first, capped at
            the resolved limit. Empty list if no sibling messages
            exist or if every candidate row failed the scope filter.

        Raises:
            TypeError: if conversation_id is not a uuid.UUID instance.
                Defense against string-typing creeping in from older
                routes — fail loudly at the boundary, not silently
                via SQL-side coercion.
            ValueError: if tenant_id or domain_id is empty / whitespace.
                A blank scope assertion is never legitimate; refusing it
                here prevents a "match anything in tenant_id" bug class.
        """
        # ---- input validation (fail-loud at the boundary) -----------
        if not isinstance(conversation_id, uuid.UUID):
            raise TypeError(
                "conversation_id must be uuid.UUID, "
                f"got {type(conversation_id).__name__}"
            )
        if not tenant_id or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string")
        if not domain_id or not domain_id.strip():
            raise ValueError("domain_id must be a non-empty string")

        # Clamp limit silently — a misconfigured caller does not break
        # retrieval, but the SQL row count is always bounded.
        effective_limit = max(1, min(int(limit), MAX_LIMIT))

        # ---- SQL --------------------------------------------------
        # JOIN: messages → sessions ON sessions.id = messages.session_id.
        # Scope: sessions.conversation_id = X
        #        AND sessions.tenant_id = X
        #        AND sessions.domain_id = X
        # Optional exclusion: sessions.id != exclude_session_id.
        # Order: messages.created_at DESC (newest first; recency-ranking
        #        is v1 — see module docstring).
        # Limit: bounded by effective_limit.
        stmt = (
            select(MessageModel, SessionModel)
            .join(SessionModel, SessionModel.id == MessageModel.session_id)
            .where(
                SessionModel.conversation_id == conversation_id,
                SessionModel.tenant_id == tenant_id,
                SessionModel.domain_id == domain_id,
            )
            .order_by(MessageModel.created_at.desc())
            .limit(effective_limit)
        )
        if exclude_session_id is not None:
            stmt = stmt.where(SessionModel.id != exclude_session_id)

        rows: Sequence[tuple[MessageModel, SessionModel]] = (
            self.db.execute(stmt).all()
        )

        # ---- post-query scope assertion (defense in depth) ---------
        # The SQL filter above is the primary scope check. This loop
        # re-asserts on the materialised row, which guards against:
        #   1. Schema drift: a future migration adds a different
        #      tenant_id source to sessions and someone forgets to
        #      update the retriever filter.
        #   2. ORM hydration bugs: a join that accidentally surfaces
        #      a row whose sessions.tenant_id is NULL or mismatched.
        #   3. SQL injection in upstream caller code: defense-in-depth
        #      assumes the caller could be compromised, not just the
        #      DB layer.
        # Same discipline as §4.7's "three-layer scope enforcement"
        # which this module makes four-layer.
        passages: list[CrossSessionPassage] = []
        dropped = 0
        for message, session in rows:
            if (
                session.tenant_id != tenant_id
                or session.domain_id != domain_id
                or session.conversation_id != conversation_id
            ):
                # Should be impossible via the WHERE clause. Log
                # loudly if it ever happens — that's a real signal
                # something upstream broke its contract.
                dropped += 1
                logger.error(
                    "cross_session_retriever scope mismatch (defense-in-depth "
                    "drop): asked tenant=%s domain=%s conv=%s, got "
                    "session=%s tenant=%s domain=%s conv=%s",
                    tenant_id, domain_id, str(conversation_id),
                    session.id, session.tenant_id, session.domain_id,
                    str(session.conversation_id)
                    if session.conversation_id else None,
                )
                continue
            passages.append(
                CrossSessionPassage(
                    content=message.content,
                    role=message.role,
                    source_session_id=session.id,
                    source_channel=session.channel,
                    # MessageModel.created_at is provided by TimestampMixin.
                    timestamp=message.created_at,
                    message_id=message.id,
                )
            )

        if dropped:
            # Aggregate count alongside the per-row error logs above.
            # This is the metric an operator would alert on.
            logger.error(
                "cross_session_retriever dropped %d row(s) on post-query "
                "scope check for conversation=%s tenant=%s domain=%s",
                dropped, str(conversation_id), tenant_id, domain_id,
            )

        return passages
