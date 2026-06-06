"""
CrossSessionRetriever — Step 24.5c runtime surface.

Sibling of the per-session retriever (MemoryRepository.get_user_memories +
the message-history thread on SessionModel.messages). Where the per-session
retriever answers "what did THIS session say recently?", the cross-session
retriever answers:

    Given the active session's (conversation_id, admin_id),
    what are the N most recent messages from SIBLING sessions under the
    same conversation, ordered by recency, capped by a per-call budget?

Returns ranked passages with provenance metadata (source_session_id,
source_channel, timestamp) that the runtime layer threads into the
foundation-model context as the cross-session leg of memory retrieval.

Scope filtering happens INSIDE the retriever, not at the caller. The
retriever refuses to return a row whose admin_id does not match the
calling scope, even if the same conversation_id is shared across
scopes — which it cannot be, by the conversations-table FK, but the
retriever asserts it anyway. Defense in depth, same discipline as
ARCHITECTURE §4.7. This is the FOURTH check in the scope-enforcement
chain (the first three live at the auth surface, the runtime scope
resolver, and the persistence layer's row writes).

Arc 12 EX1d (founder-directed agent_id/domain_id excision): the v1
``domain_id`` parameter is removed from the retriever surface. v2 has
a single Admin→Instance boundary (Architecture §3.7.2). Arc 12 EX3
subsequently dropped ``sessions.domain_id`` / ``sessions.agent_id`` at
the schema level, so the column is gone from the ORM as well. The
retriever is feature-flag-gated OFF at v1 (§3.5.2 / §5.6), so removing
the filter has no production behaviour impact.

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
from datetime import datetime, timedelta, timezone
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

# ---------------------------------------------------------------------------
# Unit 13e §3.4.10 — cross-session memory INJECTION bound (platform
# constants, NOT admin-configurable).
# ---------------------------------------------------------------------------
# §3.4.10: at injection time the runtime bulk-loads the N most-recent
# summaries within a 12-month rolling window, subject to a token ceiling.
# Older / beyond-N summaries are recall-eligible only on explicit
# relevance match, not bulk-loaded.
DEFAULT_RECENT_SUMMARIES_N = 10
# 12-month rolling window. Summaries older than this are excluded from the
# bulk injection. ~365 days; leap-year drift is immaterial at a 12-month
# recall horizon.
RECALL_WINDOW_DAYS = 365
# Token ceiling for the bulk-injected block. Approximated by a chars/4
# heuristic at injection time (the runtime's real tokenizer refines it);
# kept as a platform constant so the bound is deterministic and testable.
INJECTION_TOKEN_CEILING = 2000
# chars-per-token heuristic for the deterministic token estimate.
_CHARS_PER_TOKEN = 4


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
        admin_id: str,
        limit: int = DEFAULT_RECENT_SUMMARIES_N,
        exclude_session_id: str | None = None,
        within_days: int | None = RECALL_WINDOW_DAYS,
    ) -> list[CrossSessionPassage]:
        """Retrieve recent messages from sibling sessions in a conversation.

        Args:
            conversation_id: The Conversation.id whose sibling sessions
                we want to read. Must be a uuid.UUID (NOT a string —
                callers MUST parse before invoking, mirroring the
                MemoryRepository discipline of typed inputs).
            admin_id:       The natural-key tenant scope. Asserted on
                EVERY returned row's session.admin_id; mismatched
                rows are dropped (defense-in-depth, §4.7).
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
            ValueError: if admin_id is empty / whitespace.
                A blank scope assertion is never legitimate; refusing it
                here prevents a "match anything in admin_id" bug class.
        """
        # ---- input validation (fail-loud at the boundary) -----------
        if not isinstance(conversation_id, uuid.UUID):
            raise TypeError(
                "conversation_id must be uuid.UUID, "
                f"got {type(conversation_id).__name__}"
            )
        if not admin_id or not admin_id.strip():
            raise ValueError("admin_id must be a non-empty string")

        # ---- Arc 9.1 Phase D1 feature-flag gate (G1) -----------------
        #
        # This retriever has zero production callers as of Arc 9.1. It
        # was authored under Step 24.5c as a Memory v1 runtime surface
        # but never wired into a chat-runtime code path. Until it is,
        # we refuse to execute its SQL — even though the SQL is itself
        # scope-bounded, defense-in-depth says an unused runtime is
        # safer dormant than live.
        #
        # To enable in production, set the env var
        # LUCIEL_CROSS_SESSION_RETRIEVER_ENABLED=1 at the task-def
        # level. The gate sits AFTER input validation so the existing
        # shape tests (which assert TypeError / ValueError on bad
        # input) continue to pass without setting the flag.
        #
        # The gate ALSO refuses if the calling DB session does not
        # have the tenant GUC set — a second wall in case the flag is
        # ever flipped on without the standard middleware that binds
        # app.admin_id on every BEGIN. We probe the GUC via
        # current_setting('app.admin_id', true) which returns NULL if
        # unset (the `true` flag suppresses the "unrecognised parameter"
        # error). An unset GUC raises RuntimeError.
        import os
        if os.environ.get("LUCIEL_CROSS_SESSION_RETRIEVER_ENABLED") != "1":
            raise RuntimeError(
                "CrossSessionRetriever is quarantined (Arc 9.1 G1). "
                "Set LUCIEL_CROSS_SESSION_RETRIEVER_ENABLED=1 to enable. "
                "Until then this module exists for shape conformance only."
            )
        # GUC presence assertion — belt + braces with RLS.
        try:
            from sqlalchemy import text as _sa_text
            admin_guc = self.db.execute(
                _sa_text("SELECT current_setting('app.admin_id', true)")
            ).scalar()
        except Exception as _e:
            # If the probe itself fails, refuse rather than fall through.
            raise RuntimeError(
                f"CrossSessionRetriever could not verify tenant GUC: {_e!r}"
            ) from _e
        if not admin_guc:
            raise RuntimeError(
                "CrossSessionRetriever refusal: app.admin_id GUC is unset "
                "on the calling DB session. Tenant binding must be "
                "established by the standard request-scope middleware "
                "BEFORE invoking the retriever."
            )

        # Clamp limit silently — a misconfigured caller does not break
        # retrieval, but the SQL row count is always bounded.
        effective_limit = max(1, min(int(limit), MAX_LIMIT))

        # ---- SQL --------------------------------------------------
        # JOIN: messages → sessions ON sessions.id = messages.session_id.
        # Scope: sessions.conversation_id = X
        #        AND sessions.admin_id = X
        # Optional exclusion: sessions.id != exclude_session_id.
        # Order: messages.created_at DESC (newest first; recency-ranking
        #        is v1 — see module docstring).
        # Limit: bounded by effective_limit.
        stmt = (
            select(MessageModel, SessionModel)
            .join(SessionModel, SessionModel.id == MessageModel.session_id)
            .where(
                SessionModel.conversation_id == conversation_id,
                SessionModel.admin_id == admin_id,
            )
            .order_by(MessageModel.created_at.desc())
            .limit(effective_limit)
        )
        if exclude_session_id is not None:
            stmt = stmt.where(SessionModel.id != exclude_session_id)
        # Unit 13e §3.4.10 — 12-month rolling window. Rows older than the
        # window are NOT bulk-loaded (they remain recall-eligible only on
        # explicit relevance match, which is a different code path).
        if within_days is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=within_days)
            stmt = stmt.where(MessageModel.created_at >= cutoff)

        rows: Sequence[tuple[MessageModel, SessionModel]] = (
            self.db.execute(stmt).all()
        )

        # ---- post-query scope assertion (defense in depth) ---------
        # The SQL filter above is the primary scope check. This loop
        # re-asserts on the materialised row, which guards against:
        #   1. Schema drift: a future migration adds a different
        #      admin_id source to sessions and someone forgets to
        #      update the retriever filter.
        #   2. ORM hydration bugs: a join that accidentally surfaces
        #      a row whose sessions.admin_id is NULL or mismatched.
        #   3. SQL injection in upstream caller code: defense-in-depth
        #      assumes the caller could be compromised, not just the
        #      DB layer.
        # Same discipline as §4.7's "three-layer scope enforcement"
        # which this module makes four-layer.
        passages: list[CrossSessionPassage] = []
        dropped = 0
        for message, session in rows:
            if (
                session.admin_id != admin_id
                or session.conversation_id != conversation_id
            ):
                # Should be impossible via the WHERE clause. Log
                # loudly if it ever happens — that's a real signal
                # something upstream broke its contract.
                dropped += 1
                logger.error(
                    "cross_session_retriever scope mismatch (defense-in-depth "
                    "drop): asked tenant=%s conv=%s, got "
                    "session=%s tenant=%s conv=%s",
                    admin_id, str(conversation_id),
                    session.id, session.admin_id,
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
                "scope check for conversation=%s tenant=%s",
                dropped, str(conversation_id), admin_id,
            )

        return passages


# ---------------------------------------------------------------------------
# Unit 13e §3.4.10 — deterministic injection-bound for session summaries.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SummaryRecord:
    """A persisted session summary considered for cross-session injection.

    Mirrors the session_summaries store (BUILD 4). ``facts`` is the
    structured fact map used for recency-precedence conflict resolution;
    an empty dict means "no structured facts" (the summary still counts
    toward the N / window / token bounds via its ``summary`` text).
    """

    resolved_lead_id: str
    session_id: str
    summary: str
    created_at: datetime
    facts: dict | None = None


def _estimate_tokens(text: str) -> int:
    """Deterministic token estimate (chars/4 heuristic, ceil)."""
    if not text:
        return 0
    return -(-len(text) // _CHARS_PER_TOKEN)


def bound_summaries_for_injection(
    summaries: list[SummaryRecord],
    *,
    now: datetime | None = None,
    n: int = DEFAULT_RECENT_SUMMARIES_N,
    within_days: int = RECALL_WINDOW_DAYS,
    token_ceiling: int = INJECTION_TOKEN_CEILING,
) -> tuple[list[SummaryRecord], dict[str, object]]:
    """Apply the §3.4.10 injection bound to a list of session summaries.

    Deterministic (no LLM). Steps, in order:
      1. 12-month window — drop summaries older than ``within_days``.
      2. Newest-first ordering by ``created_at``.
      3. Recency-precedence — when two summaries for the SAME
         ``resolved_lead_id`` carry CONFLICTING facts (same fact key,
         different value), the NEWER summary's fact wins; the older
         summary's conflicting fact is shadowed. The newer summary is
         what gets injected.
      4. N cap — keep at most ``n`` most-recent summaries.
      5. Token ceiling — stop adding summaries once the running token
         estimate would exceed ``token_ceiling``.

    Returns (selected_summaries_newest_first, resolved_facts_per_lead).
    ``resolved_facts_per_lead`` maps resolved_lead_id → the winning
    (recency-resolved) fact map, so the caller can inject a coherent,
    non-conflicting fact view.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=within_days)

    # 1 + 2: window filter, newest-first.
    in_window = [s for s in summaries if s.created_at >= cutoff]
    in_window.sort(key=lambda s: s.created_at, reverse=True)

    # 3: recency-precedence fact resolution per lead. Walk newest→oldest;
    # the first value seen for a (lead, fact_key) wins (it is the newest).
    resolved_facts: dict[str, dict] = {}
    for s in in_window:
        if not s.facts:
            continue
        bucket = resolved_facts.setdefault(s.resolved_lead_id, {})
        for k, v in s.facts.items():
            if k not in bucket:  # newest wins — first seen, never overwritten
                bucket[k] = v

    # 4 + 5: N cap + token ceiling.
    selected: list[SummaryRecord] = []
    running_tokens = 0
    for s in in_window:
        if len(selected) >= n:
            break
        cost = _estimate_tokens(s.summary)
        if selected and running_tokens + cost > token_ceiling:
            # Keep at least one summary even if it alone exceeds the
            # ceiling (so a single long summary is not silently dropped),
            # but stop once a subsequent one would overflow.
            break
        selected.append(s)
        running_tokens += cost

    return selected, resolved_facts
