from __future__ import annotations

from typing import Literal

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


class ClientClaim(BaseModel):
    """Step 31 sub-branch 1: client-asserted channel-equivalent identifier.

    Shape mirrors the `identity_claims` table (Step 24.5c §3.2.11):
    a (claim_type, claim_value) pair the ingress adapter -- here the
    widget bundle -- swears identifies a user under the issuing scope.
    The widget's `issuing_adapter` is fixed to 'widget' server-side;
    the client cannot spoof which adapter asserted the claim.

    End-user-driven verification (email-confirm link, SMS one-time code,
    SSO subject match) is deliberately out of scope at v1; the v1 trust
    model is adapter-asserted per §3.2.11's `verified_at=NULL` path.
    """

    claim_type: Literal["email", "phone", "sso_subject"]
    claim_value: str = Field(..., min_length=1, max_length=512)


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

    # Step 31 sub-branch 1: optional client-asserted identity claim.
    #
    # When the customer's site has already authenticated the visitor
    # (logged-in CRM session, signed-in portal, etc.) it can assert a
    # channel-equivalent identifier alongside the very first widget
    # turn. The backend resolves the claim through the §3.3 step 4
    # IdentityResolver (Step 24.5c primitives) so subsequent widget
    # sessions for the same visitor join the same `conversation_id`
    # and the cross-session retriever surfaces the prior turns.
    #
    # The field is OPTIONAL and NULLABLE -- omitting it preserves the
    # legacy anonymous widget path (lazy session creation with
    # user_id=None). When present, the backend calls
    # SessionService.create_session_with_identity() instead.
    #
    # Only meaningful on the FIRST turn of a conversation (the turn
    # without a session_id). Subsequent turns ignore client_claim
    # because the session_id already binds the identity.
    client_claim: ClientClaim | None = None