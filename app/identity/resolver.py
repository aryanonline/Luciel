"""
IdentityResolver — the Step 24.5c §3.3 step 4 hook.

At the start of every request, after the requesting key resolves to its
(tenant, domain, agent) scope, the runtime asks this resolver:

    Is there a User whose identity_claims include the channel-specific
    identifier this adapter just asserted, within this scope?

    * If YES: bind the session to that User and resolve a conversation_id
      by walking the User's other recent active sessions under the same
      scope (most recent active conversation wins).
    * If NO:  mint a brand-new User (synthetic, per §4 of app/models/user.py)
      AND a brand-new conversation_id in the SAME transaction.

The resolver's contract is "one query for identity, one query for
conversation, both bounded" (ARCHITECTURE §3.2.11 paragraph "How a
request resolves continuity"). The result tuple tells the caller
everything it needs to create the session row with a populated
conversation_id and user_id.

Non-goals (deferred):
    * Adapter wiring -- sub-branch 4 of Step 24.5c.
    * Cross-scope identity reads -- Step 38 (§4.9 rejected for v1).
    * End-user-driven verification (email-confirm link, SMS OTP, SSO
      subject match). v1 records claims as asserted by the adapter
      with verified_at=NULL and the resolver treats asserted-but-
      unverified claims as sufficient WITHIN THE ISSUING SCOPE.
      Step 34a + Step 31 add the verification layer additively.

Step 24.5c sub-branch 3 of 5. Builds on PR #24 (1e761a6, models) and
PR #25 (cross-session retriever). Sub-branch 4 will wire this into
the widget + programmatic-API adapters.
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session as SqlSession

from app.models.conversation import Conversation
from app.models.identity_claim import ClaimType, IdentityClaim
from app.models.session import SessionModel
from app.models.user import User

logger = logging.getLogger(__name__)


# Synthetic email format for users minted by the resolver when no
# identity_claim matches. Mirrors the existing synthetic-email pattern
# in app/models/user.py (Step 23 Option B onboarding backward-compat
# path) so PIPEDA access/erasure filters on User.synthetic continue
# to work. Format keeps the User locally identifiable for debugging
# without leaking any channel-asserted identifier into the email
# column itself (the email is still the natural lookup key for the
# legacy auth path, NOT the cross-channel identity claim path).
_SYNTHETIC_EMAIL_TEMPLATE = "identity-{user_id}@{tenant_id}.luciel.local"

# RFC 5321 email length cap, matched to identity_claims.claim_value
# String(320). Anything longer is invalid per the spec.
_EMAIL_MAX_LEN = 320

# Liberal email shape check. We do NOT do RFC-grade validation here;
# the adapter is the source of truth and is trusted within its scope
# per §3.2.11 v1 (verified_at=NULL is the v1 trust model). This regex
# rejects obvious garbage (no @, multiple @s, control chars) so a
# misformatted asserted claim does not pollute the unique constraint.
_EMAIL_SHAPE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# E.164 phone shape: + followed by 1-15 digits. We don't validate
# country code semantics; the adapter has done its own normalisation.
_E164_SHAPE = re.compile(r"^\+[1-9]\d{1,14}$")


def normalise_claim_value(claim_type: ClaimType, raw: str) -> str:
    """Canonicalise a raw claim value per §3.2.11 normalisation rules.

    Called by BOTH the adapter (before INSERT) and the resolver
    (before SELECT) so the unique constraint sees the same string
    on both sides.

    Rules (ARCHITECTURE §3.2.11):
        EMAIL:       case-folded (lowercased) and surrounding
                     whitespace stripped.
        PHONE:       E.164 normalised. v1 assumes the adapter has
                     already done country-code/format normalisation
                     and we strip whitespace + assert the +DIGITS
                     shape. If the shape check fails we raise; the
                     adapter must hand us a properly formed value.
        SSO_SUBJECT: opaque -- whitespace strip only (case-sensitive
                     because SSO subjects are issuer-defined and the
                     issuer's case is authoritative).

    Raises:
        ValueError on empty input, oversized email, or PHONE that
            does not match E.164 shape.
        TypeError on non-ClaimType.
    """
    if not isinstance(claim_type, ClaimType):
        raise TypeError(
            f"claim_type must be ClaimType, got {type(claim_type).__name__}"
        )
    if not isinstance(raw, str):
        raise TypeError(
            f"claim_value must be str, got {type(raw).__name__}"
        )
    stripped = raw.strip()
    if not stripped:
        raise ValueError("claim_value cannot be empty / whitespace")

    if claim_type is ClaimType.EMAIL:
        # Case-fold via lowercase. casefold() is more aggressive
        # (handles Unicode like 'ß'->'ss') but the unique constraint
        # is on the literal column value, so we use lowercase to
        # stay round-trippable with what the column stores.
        normalised = stripped.lower()
        if len(normalised) > _EMAIL_MAX_LEN:
            raise ValueError(
                f"email claim_value exceeds {_EMAIL_MAX_LEN} chars"
            )
        if not _EMAIL_SHAPE.match(normalised):
            raise ValueError(
                "email claim_value failed shape check (must look "
                "like local@host.tld)"
            )
        return normalised

    if claim_type is ClaimType.PHONE:
        if not _E164_SHAPE.match(stripped):
            raise ValueError(
                "phone claim_value must be E.164 (+ followed by "
                "1-15 digits, first digit non-zero)"
            )
        return stripped

    if claim_type is ClaimType.SSO_SUBJECT:
        # Opaque; case-sensitive. Whitespace strip only.
        return stripped

    # Unreachable -- exhaustive over ClaimType. Defense in depth in
    # case a new enum value is added without updating this function.
    raise ValueError(f"unsupported claim_type: {claim_type!r}")


@dataclass(frozen=True)
class IdentityResolution:
    """The result of resolve_identity().

    Fields:
        user_id:         The User.id (uuid.UUID) the caller should
                         attribute this session to.
        conversation_id: The Conversation.id the caller should
                         populate on the new session row.
        identity_claim_id: The IdentityClaim.id used to make the
                         match, or NEWLY-CREATED claim id if this
                         was a mint path. Useful for caller-side
                         audit logging.
        is_new_user:     True iff this resolution minted a User.
                         False iff an existing claim matched.
        is_new_conversation: True iff this resolution minted a
                         Conversation. Can be True even when
                         is_new_user is False -- e.g. a returning
                         User whose only prior session was archived
                         or under a different scope.
    """
    user_id: uuid.UUID
    conversation_id: uuid.UUID
    identity_claim_id: uuid.UUID
    is_new_user: bool
    is_new_conversation: bool


class IdentityResolver:
    """Identity + conversation resolver for the §3.3 step 4 hook.

    Stateless aside from the DB session handle. Construct per request.

    The resolver does NOT commit. It mutates the SQLAlchemy session
    (adding new User / Conversation / IdentityClaim rows when minting)
    and lets the caller's transaction boundary commit them together
    with the session row. This matches the §3.2.11 contract: "a
    brand-new User and a brand-new conversation_id are minted in the
    same transaction" as the session row creation.
    """

    def __init__(self, db: SqlSession) -> None:
        self.db = db

    def resolve(
        self,
        *,
        claim_type: ClaimType,
        claim_value: str,
        tenant_id: str,
        domain_id: str,
        issuing_adapter: str,
    ) -> IdentityResolution:
        """Resolve an asserted claim to (User, Conversation).

        Args:
            claim_type:       EMAIL | PHONE | SSO_SUBJECT.
            claim_value:      The RAW asserted value. The resolver
                              normalises internally; callers don't
                              need to pre-normalise (but may, since
                              normalise_claim_value() is idempotent).
            tenant_id:        Natural-key tenant scope. Asserted on
                              the claim lookup.
            domain_id:        Natural-key domain scope. Asserted on
                              the claim lookup.
            issuing_adapter:  Free-form adapter identifier (e.g.
                              "widget", "programmatic_api",
                              "voice_gateway"). Stored on the
                              IdentityClaim row when minting.

        Returns:
            IdentityResolution with user_id, conversation_id,
            identity_claim_id, and the two booleans flagging which
            path was taken.

        Raises:
            ValueError / TypeError via normalise_claim_value() on
            malformed input.
            ValueError on blank tenant_id / domain_id / issuing_adapter.
        """
        # ---- input validation ---------------------------------------
        if not tenant_id or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string")
        if not domain_id or not domain_id.strip():
            raise ValueError("domain_id must be a non-empty string")
        if not issuing_adapter or not issuing_adapter.strip():
            raise ValueError("issuing_adapter must be a non-empty string")

        # Canonicalise the value the SAME way the adapter does (or
        # should). Raises on garbage input.
        normalised_value = normalise_claim_value(claim_type, claim_value)

        # ---- step A: look up an existing claim ----------------------
        # ONE query, indexed by the unique constraint
        # uq_identity_claims_type_value_scope on
        # (claim_type, claim_value, tenant_id, domain_id).
        # active=True so soft-deleted claims do not resolve.
        existing_claim_stmt = (
            select(IdentityClaim)
            .where(
                IdentityClaim.claim_type == claim_type,
                IdentityClaim.claim_value == normalised_value,
                IdentityClaim.tenant_id == tenant_id,
                IdentityClaim.domain_id == domain_id,
                IdentityClaim.active.is_(True),
            )
            .limit(1)
        )
        existing_claim = self.db.execute(existing_claim_stmt).scalar_one_or_none()

        if existing_claim is not None:
            # HIT: bind to existing user, resolve conversation_id.
            return self._resolve_existing(
                claim=existing_claim,
                tenant_id=tenant_id,
                domain_id=domain_id,
            )

        # MISS: mint user + claim + conversation in the same txn.
        return self._mint_fresh(
            claim_type=claim_type,
            normalised_value=normalised_value,
            tenant_id=tenant_id,
            domain_id=domain_id,
            issuing_adapter=issuing_adapter,
        )

    # ---------------------------------------------------------------
    # Existing-claim path
    # ---------------------------------------------------------------

    def _resolve_existing(
        self,
        *,
        claim: IdentityClaim,
        tenant_id: str,
        domain_id: str,
    ) -> IdentityResolution:
        """Bind to claim.user_id; find or mint conversation_id.

        Walks the User's recent active sessions under the SAME scope.
        Most recent active conversation wins (§3.2.11 "most recent
        active conversation wins; configurable per scope" -- v1 is
        not configurable, just the default rule).
        """
        # ONE query. Latest session under the same scope for this
        # user whose conversation_id is non-NULL. We deliberately
        # require conversation_id NOT NULL because a NULL-conversation
        # session is itself an unbound single-session conversation,
        # not a "join target".
        latest_session_stmt = (
            select(SessionModel)
            .where(
                SessionModel.user_id == str(claim.user_id),
                SessionModel.tenant_id == tenant_id,
                SessionModel.domain_id == domain_id,
                SessionModel.conversation_id.is_not(None),
                SessionModel.status == "active",
            )
            .order_by(SessionModel.created_at.desc())
            .limit(1)
        )
        latest_session = self.db.execute(
            latest_session_stmt
        ).scalar_one_or_none()

        if latest_session is not None and latest_session.conversation_id:
            # Bind to existing conversation. No mint.
            return IdentityResolution(
                user_id=claim.user_id,
                conversation_id=latest_session.conversation_id,
                identity_claim_id=claim.id,
                is_new_user=False,
                is_new_conversation=False,
            )

        # No prior active conversation under this scope. The User
        # exists (from another scope, or from a session that's been
        # archived), but we need a fresh conversation to host this
        # session's sibling thread. Mint just the conversation.
        new_conv = self._mint_conversation(
            tenant_id=tenant_id, domain_id=domain_id
        )
        return IdentityResolution(
            user_id=claim.user_id,
            conversation_id=new_conv.id,
            identity_claim_id=claim.id,
            is_new_user=False,
            is_new_conversation=True,
        )

    # ---------------------------------------------------------------
    # Mint path
    # ---------------------------------------------------------------

    def _mint_fresh(
        self,
        *,
        claim_type: ClaimType,
        normalised_value: str,
        tenant_id: str,
        domain_id: str,
        issuing_adapter: str,
    ) -> IdentityResolution:
        """Mint User + Conversation + IdentityClaim in one transaction.

        The caller's outer transaction commits them together with
        the session row creation. The resolver itself does NOT call
        commit() -- it adds to self.db and flushes so caller code
        receives populated PKs.
        """
        # Mint User. UUID generated by Postgres server_default; we
        # flush to pull the PK back. Synthetic=True per the §3.2.11
        # design: this User has not provided an email out-of-band,
        # just an asserted channel-specific identifier. Synthetic
        # flag keeps PIPEDA filters honest.
        new_user_id = uuid.uuid4()
        synth_email = _SYNTHETIC_EMAIL_TEMPLATE.format(
            user_id=str(new_user_id), tenant_id=tenant_id,
        )
        new_user = User(
            id=new_user_id,
            email=synth_email,
            # Display name fallback -- the adapter may patch this
            # later if the channel surfaces a friendlier name (e.g.
            # Caller ID, SSO claims). v1 stores the claim value as
            # display_name for traceability.
            display_name=f"{claim_type.value}:{normalised_value}"[:200],
            synthetic=True,
            active=True,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        self.db.add(new_user)

        # Mint Conversation.
        new_conv = self._mint_conversation(
            tenant_id=tenant_id, domain_id=domain_id
        )

        # Mint IdentityClaim, binding the new User. verified_at=NULL
        # per §3.2.11 v1: claims are asserted by the adapter and
        # trusted within issuing scope; end-user-driven verification
        # comes later (Step 34a / Step 31).
        new_claim = IdentityClaim(
            id=uuid.uuid4(),
            user_id=new_user_id,
            claim_type=claim_type,
            claim_value=normalised_value,
            tenant_id=tenant_id,
            domain_id=domain_id,
            issuing_adapter=issuing_adapter,
            verified_at=None,
            active=True,
            created_at=datetime.now(timezone.utc),
        )
        self.db.add(new_claim)

        # Flush so the caller sees populated PKs even before the
        # outer commit fires. Required so the session row created
        # downstream can reference new_conv.id and new_user_id
        # before commit.
        self.db.flush()

        logger.info(
            "identity_resolver minted fresh user=%s conversation=%s "
            "claim_type=%s tenant=%s domain=%s adapter=%s",
            str(new_user_id), str(new_conv.id), claim_type.value,
            tenant_id, domain_id, issuing_adapter,
        )

        return IdentityResolution(
            user_id=new_user_id,
            conversation_id=new_conv.id,
            identity_claim_id=new_claim.id,
            is_new_user=True,
            is_new_conversation=True,
        )

    def _mint_conversation(
        self,
        *,
        tenant_id: str,
        domain_id: str,
    ) -> Conversation:
        """Helper: mint a new Conversation row, flush, return."""
        new_conv = Conversation(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            domain_id=domain_id,
            last_activity_at=datetime.now(timezone.utc),
            active=True,
            created_at=datetime.now(timezone.utc),
        )
        self.db.add(new_conv)
        self.db.flush()
        return new_conv

    # ---------------------------------------------------------------
    # Helper for tests / introspection
    # ---------------------------------------------------------------

    @staticmethod
    def supported_claim_types() -> Iterable[ClaimType]:
        """List the claim types this resolver normalises.

        Mirrors the ClaimType enum exactly today; the helper exists
        so a future revision (adding e.g. PASSKEY) can update one
        list rather than two.
        """
        return (ClaimType.EMAIL, ClaimType.PHONE, ClaimType.SSO_SUBJECT)
