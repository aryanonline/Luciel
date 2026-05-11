"""IdentityClaim model -- channel-specific identifier bound to a User within a scope.

Step 24.5c (Cross-channel identity and conversation continuity).

An IdentityClaim records the channel-specific identifier (email, phone, sso
subject) that resolves back to a durable User identity. At request time the
ingress adapter (widget, programmatic API, voice/SMS/email when 34a lands)
asserts a claim; the identity resolver looks it up against this table within
the calling scope and binds the session to the matching User.

Design contract (see ARCHITECTURE.md §3.2.11 "Identity & conversation
continuity" for the canonical spec):

- Claims are ORTHOGONAL to scope the same way Users are. A single User
  can have many IdentityClaim rows, possibly under different scopes
  (e.g. Sarah's work-email claim at REMAX Crossroads + Sarah's personal
  phone claim at the same scope, all rolling up to one User UUID).

- Each claim is SCOPED to its issuing scope -- (tenant_id, domain_id) --
  in v1. Cross-scope continuity is explicitly out of scope at v1
  (Step 38 territory, ARCHITECTURE §4.9 rejected-alternative bullet).
  Uniqueness is enforced on (claim_type, claim_value, tenant_id, domain_id)
  so two scopes can independently assert the same number or email
  without colliding -- a number that belongs to Brokerage A's prospect
  and to Brokerage B's prospect is two facts, both true.

- claim_type is a closed enum: email, phone, sso_subject. The runtime
  passes the type alongside the value to the resolver; the table stores
  both columns so a sparse index on type can answer "find all phone
  claims for this scope" cheaply.

- claim_value normalisation is the writer's responsibility (service
  layer / adapter): email values are case-folded (lowercased) before
  write; phone values are E.164-normalised (e.g. "+14165550100") before
  write; sso_subject is opaque -- stored as the provider gave it, no
  normalisation. The DB does not enforce normalisation; the uniqueness
  constraint catches collisions after normalisation has happened.
  This mirrors how User.email's case-insensitive uniqueness is split
  between service-layer LOWER()ing and the LOWER(email) expression
  index in 24.5b.

- issuing_adapter is a free-form string label identifying which ingress
  adapter asserted the claim. v1 values land as 'widget' or
  'programmatic_api'; Step 34a will add 'voice_gateway', 'sms_gateway',
  'email_gateway'. Kept as a String rather than an enum because the
  set grows with Step 34a -- no schema migration required to add a
  new adapter, only a new enum-string convention.

- verified_at is nullable. v1 trust model is ADAPTER-ASSERTED: the
  phone gateway swears the call came from a particular E.164 number;
  the widget swears the embed-key request came from a particular
  logged-in User (when the customer's site has authenticated the
  visitor); the programmatic API caller swears the message belongs
  to a particular email known out-of-band. End-user-driven verification
  (email-confirm link, SMS code, SSO subject match) lands with Step
  34a + Step 31 and populates verified_at when it does. v1 records
  the claim with verified_at=NULL and the cross-session retriever
  treats asserted-but-unverified claims as sufficient FOR RETRIEVAL
  WITHIN THE ISSUING SCOPE (never across scopes).

- active is the soft-delete flag, mirroring User/ScopeAssignment.
  Invariant 3: never hard-delete -- the audit chain must remain
  walkable backwards.

- user_id is UUID FK to users.id (ON DELETE RESTRICT, matching the
  discipline in 24.5b that protects identity history from cascade-
  delete on User removal -- User deactivation is soft-delete only).

- PK is UUID, server-default gen_random_uuid() (pgcrypto already present
  from 24.5b's migration).

Relationships:
- IdentityClaim N..1 User. Back-populated via User.identity_claims
  relationship added in this sub-branch's __init__ wiring.

Migration:
- Schema lands alongside conversations and sessions.conversation_id in
  the same hand-written migration (alembic revision 3dbbc70d0105).

Cross-refs:
- ARCHITECTURE §3.2.11 (canonical spec).
- CANONICAL_RECAP §11 Q8 (strategic answer).
- CANONICAL_RECAP §12 Step 24.5c row.
- DRIFTS.md D-step-24-5c-impl-backlog-2026-05-11.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.user import User


class ClaimType(str, enum.Enum):
    """Channel-equivalent identifier type. Closed enum.

    EMAIL          -- RFC-5321-ish email address; case-folded at write.
    PHONE          -- E.164-formatted phone number; normalised at write.
    SSO_SUBJECT    -- opaque subject identifier from an SSO provider.
                      Stored verbatim; provider+subject together identify
                      the user uniquely within that SSO's namespace.
    """

    EMAIL = "EMAIL"
    PHONE = "PHONE"
    SSO_SUBJECT = "SSO_SUBJECT"


# Postgres enum metadata, mirrored in the migration. Kept as a module-level
# constant so the migration and the SQLAlchemy column declaration agree
# on type name + value list without duplication drift.
CLAIM_TYPE_ENUM_NAME = "identity_claim_type"
CLAIM_TYPE_VALUES = tuple(c.value for c in ClaimType)


class IdentityClaim(Base):
    """Channel-specific identifier bound to a User within a scope.

    See module docstring for the design contract.
    """

    __tablename__ = "identity_claims"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )

    # The durable User identity this claim points to. RESTRICT mirrors
    # 24.5b's discipline: a User cannot be hard-deleted while claims
    # reference them. User deactivation is soft-delete (User.active=False).
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "users.id",
            ondelete="RESTRICT",
            name="fk_identity_claims_user_id_users",
        ),
        nullable=False,
        index=True,
    )

    claim_type: Mapped[ClaimType] = mapped_column(
        SAEnum(
            ClaimType,
            name=CLAIM_TYPE_ENUM_NAME,
            native_enum=True,
            create_constraint=True,
            validate_strings=True,
        ),
        nullable=False,
    )

    # Normalised value: lowercase email, E.164 phone, opaque sso_subject.
    # Normalisation responsibility lives at the writer (service / adapter
    # layer), not here. The uniqueness constraint catches collisions
    # after normalisation has happened. Length 320 matches RFC 5321 email
    # max with margin; phone E.164 max is 15 digits + leading '+'; sso
    # subject lengths are provider-dependent (Google ~21 digits, Microsoft
    # ~32 chars, Okta ~20 chars). 320 covers all three with margin.
    claim_value: Mapped[str] = mapped_column(
        String(320),
        nullable=False,
    )

    # Scope this claim was issued under. Same shape as everywhere else:
    # tenant_id String(100) with FK to tenant_configs.tenant_id RESTRICT;
    # domain_id String(100) without FK (composite natural key in
    # domain_configs validated at service layer). Mirrors
    # scope_assignments File 1.2 convention.
    tenant_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey(
            "tenant_configs.tenant_id",
            ondelete="RESTRICT",
            name="fk_identity_claims_tenant_id_tenant_configs",
        ),
        nullable=False,
        index=True,
    )

    domain_id: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
    )

    # Free-form label for which ingress adapter asserted the claim.
    # v1: 'widget', 'programmatic_api'. Step 34a adds: 'voice_gateway',
    # 'sms_gateway', 'email_gateway'. String not enum so adding an
    # adapter does not require a schema migration -- only a string
    # convention update.
    issuing_adapter: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    # Populated when end-user-driven verification lands (Step 34a + Step
    # 31). v1 leaves this NULL; the cross-session retriever treats
    # asserted-but-unverified claims as sufficient FOR RETRIEVAL WITHIN
    # THE ISSUING SCOPE. Cross-scope continuity remains out of scope at
    # v1 by design (Step 38 territory).
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Soft-delete flag per Invariant 3. A revoked or stale claim is
    # active=False, never DELETEd.
    active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    # ------ relationships ------
    user: Mapped["User"] = relationship(
        "User",
        back_populates="identity_claims",
        foreign_keys=[user_id],
        lazy="select",
    )

    # ------ table-level constraints + indexes ------
    __table_args__ = (
        # The load-bearing uniqueness. Two facts about the same value
        # under two scopes are independent; the same fact asserted twice
        # under the same scope is a duplicate. Writer is responsible for
        # normalising claim_value before insert (LOWER for email, E.164
        # for phone) so the comparison is meaningful.
        UniqueConstraint(
            "claim_type",
            "claim_value",
            "tenant_id",
            "domain_id",
            name="uq_identity_claims_type_value_scope",
        ),
        # Resolver hot path: "given (tenant_id, domain_id, claim_type,
        # claim_value), find the matching active claim". The unique
        # constraint above already creates a unique btree on those four
        # columns; we add an active-filter partial index so the resolver
        # never scans inactive claims.
        Index(
            "ix_identity_claims_active_resolver",
            "tenant_id",
            "domain_id",
            "claim_type",
            "claim_value",
            postgresql_where=text("active = true"),
        ),
        # "All claims for this user under this scope" -- the inverse
        # lookup used when an adapter has the User but needs to surface
        # known identifiers (Step 31 dashboards territory; v1 doesn't
        # use this path but the index is cheap and the shape is correct).
        Index(
            "ix_identity_claims_user_tenant_domain_active",
            "user_id",
            "tenant_id",
            "domain_id",
            postgresql_where=text("active = true"),
        ),
        {"comment": (
            "Step 24.5c -- channel-specific identifier bound to a User "
            "within a scope. Orthogonal to scope the same way Users are. "
            "Uniqueness scoped to (claim_type, claim_value, tenant_id, "
            "domain_id). v1 trust model is adapter-asserted; "
            "verified_at lands with Step 34a + Step 31. See "
            "ARCHITECTURE §3.2.11."
        )},
    )

    def __repr__(self) -> str:  # pragma: no cover -- debug only
        return (
            f"<IdentityClaim id={self.id} user_id={self.user_id} "
            f"type={self.claim_type.value} "
            f"scope=({self.tenant_id!r},{self.domain_id!r}) "
            f"adapter={self.issuing_adapter!r} "
            f"verified={self.verified_at is not None} "
            f"active={self.active}>"
        )
