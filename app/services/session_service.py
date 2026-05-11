"""SessionService -- thin orchestration over SessionRepository.

Step 24.5c sub-branch 4 extends create_session() with an optional
conversation_id parameter, and adds create_session_with_identity()
which wires the IdentityResolver into the session-creation path.
Existing callers keep the same behaviour: omitting conversation_id
produces a NULL-conversation session per the §3.2.11
nullable-by-design contract.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.repositories.session_repository import SessionRepository


@dataclass(frozen=True)
class SessionWithIdentity:
    """Return type of SessionService.create_session_with_identity().

    Exposes both the new SessionModel and the IdentityResolution that
    produced its conversation_id binding, so the caller can audit-log
    which path was taken (new-User mint vs. existing-User bind).

    Fields:
        session:           The newly-created SessionModel (with
                           conversation_id populated).
        user_id:           The resolved User.id.
        conversation_id:   The resolved (or freshly minted)
                           Conversation.id.
        identity_claim_id: The IdentityClaim.id used or minted.
        is_new_user:       Whether the resolver minted a User.
        is_new_conversation: Whether the resolver minted a Conversation.
    """
    session: object  # SessionModel; typed as object to avoid an
    # extra import dependency in callers.
    user_id: uuid.UUID
    conversation_id: uuid.UUID
    identity_claim_id: uuid.UUID
    is_new_user: bool
    is_new_conversation: bool


class SessionService:

    def __init__(self, repository: SessionRepository) -> None:
        self.repository = repository

    def create_session(
        self,
        *,
        tenant_id: str,
        domain_id: str,
        agent_id: str | None = None,
        user_id: str | None = None,
        channel: str = "web",
        conversation_id: uuid.UUID | None = None,
    ):
        session_id = str(uuid.uuid4())
        return self.repository.create_session(
            session_id=session_id,
            tenant_id=tenant_id,
            domain_id=domain_id,
            agent_id=agent_id,
            user_id=user_id,
            channel=channel,
            conversation_id=conversation_id,
        )

    def create_session_with_identity(
        self,
        *,
        tenant_id: str,
        domain_id: str,
        agent_id: str | None = None,
        channel: str = "web",
        claim_type,  # ClaimType -- imported lazily inside body
        claim_value: str,
        issuing_adapter: str,
    ) -> SessionWithIdentity:
        """Resolve identity, then create a session bound to it.

        Step 24.5c §3.3 step 4 hook. The adapter (widget,
        programmatic-API, voice/SMS/email gateways at Step 34a) calls
        this when it has a channel-specific identifier to assert. The
        method runs the IdentityResolver inside the same SQLAlchemy
        session that the repository uses (one transaction), so the
        resolver's mints (User / Conversation / IdentityClaim) commit
        together with the new SessionModel row.

        IMPORTANT lazy import: app.identity.resolver depends on
        app.models.conversation / identity_claim which are loaded by
        app.models.__init__'s eager-import block; we still lazy-import
        the resolver class here so a route that NEVER calls this
        method does not pay any extra import cost on cold start.

        Args:
            tenant_id, domain_id, agent_id, channel: same as
                create_session().
            claim_type:      ClaimType -- EMAIL / PHONE / SSO_SUBJECT.
            claim_value:     The raw asserted value (resolver normalises).
            issuing_adapter: The ingress adapter identifier, e.g.
                'widget', 'programmatic_api', 'voice_gateway'.

        Returns:
            SessionWithIdentity bundling the new SessionModel and the
            identity-resolution metadata (which path was taken).
        """
        # Lazy import to keep cold-start cost off the legacy session
        # creation path. Identity resolution is opt-in; routes that
        # never call this method never import the resolver.
        from app.identity.resolver import IdentityResolver

        resolver = IdentityResolver(db=self.repository.db)
        resolution = resolver.resolve(
            claim_type=claim_type,
            claim_value=claim_value,
            tenant_id=tenant_id,
            domain_id=domain_id,
            issuing_adapter=issuing_adapter,
        )

        # The session row carries the resolved User.id as a STRING
        # in the existing sessions.user_id column (which predates the
        # platform User layer and is free-form). Storing str(uuid)
        # keeps backward compatibility with all legacy tooling that
        # treats sessions.user_id as opaque, while still being
        # unambiguously joinable on User.id when needed.
        session_id = str(uuid.uuid4())
        new_session = self.repository.create_session(
            session_id=session_id,
            tenant_id=tenant_id,
            domain_id=domain_id,
            agent_id=agent_id,
            user_id=str(resolution.user_id),
            channel=channel,
            conversation_id=resolution.conversation_id,
        )

        return SessionWithIdentity(
            session=new_session,
            user_id=resolution.user_id,
            conversation_id=resolution.conversation_id,
            identity_claim_id=resolution.identity_claim_id,
            is_new_user=resolution.is_new_user,
            is_new_conversation=resolution.is_new_conversation,
        )

    def get_session(self, session_id: str):
        return self.repository.get_session(session_id)

    def list_sessions(
        self,
        *,
        tenant_id: str | None = None,
        user_id: str | None = None,
        limit: int = 50,
    ):
        return self.repository.list_sessions(
            tenant_id=tenant_id, user_id=user_id, limit=limit,
        )

    def add_message(
        self,
        *,
        session_id: str,
        role: str,
        content: str,
        trace_id: str | None = None,
    ):
        return self.repository.add_message(
            session_id=session_id, role=role, content=content, trace_id=trace_id,
        )

    def list_messages(self, session_id: str):
        return self.repository.list_messages(session_id)