"""
Backend-free contract tests for Step 24.5c sub-branch 3:
IdentityResolver + normalise_claim_value.

These tests are AST + import-shape + pure-function checks (no DB engine).
Live SQL behaviour is exercised by sub-branch 5's e2e harness.

Coverage:
    * Module imports + symbol exports.
    * normalise_claim_value rules for EMAIL / PHONE / SSO_SUBJECT,
      including the round-trip identity property and error cases.
    * IdentityResolution dataclass shape (frozen + required fields).
    * IdentityResolver class surface and resolve() signature.
    * Input validation at the boundary.
    * Mint path: with a stub DB session, asserts that resolve() adds
      a User + Conversation + IdentityClaim and returns a resolution
      with is_new_user=is_new_conversation=True.
    * Existing-claim hit path: stub returns a pre-existing claim and
      a recent active session under the same scope, asserts the
      resolver does NOT add anything and returns the right
      conversation_id.
    * Existing-claim hit path with no prior session: asserts the
      resolver mints just a Conversation (is_new_user=False,
      is_new_conversation=True).
"""
from __future__ import annotations

import ast
import inspect
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
RESOLVER_PATH = REPO_ROOT / "app" / "identity" / "resolver.py"


# ---------------------------------------------------------------------
# 1. Module surface
# ---------------------------------------------------------------------

class TestModuleSurface:
    def test_package_imports(self):
        from app import identity  # noqa: F401

    def test_resolver_module_imports(self):
        from app.identity import resolver  # noqa: F401

    def test_public_exports(self):
        from app.identity import (
            IdentityResolution,
            IdentityResolver,
            normalise_claim_value,
        )
        assert inspect.isclass(IdentityResolution)
        assert inspect.isclass(IdentityResolver)
        assert callable(normalise_claim_value)

    def test_module_docstring_pins_design_contract(self):
        from app.identity import resolver
        doc = resolver.__doc__ or ""
        assert "3.2.11" in doc, (
            "resolver docstring must pin ARCHITECTURE §3.2.11"
        )
        # §3.3 step 4 is the runtime hook this resolver implements.
        assert "3.3 step 4" in doc or "§3.3" in doc


# ---------------------------------------------------------------------
# 2. normalise_claim_value
# ---------------------------------------------------------------------

class TestNormaliseEmail:
    def test_lowercases(self):
        from app.identity import normalise_claim_value
        from app.models.identity_claim import ClaimType
        out = normalise_claim_value(ClaimType.EMAIL, "Aryan@Example.COM")
        assert out == "aryan@example.com"

    def test_strips_whitespace(self):
        from app.identity import normalise_claim_value
        from app.models.identity_claim import ClaimType
        out = normalise_claim_value(ClaimType.EMAIL, "  a@b.co  ")
        assert out == "a@b.co"

    def test_idempotent(self):
        # Critical: adapter calls this, resolver calls this. They
        # must agree on the output or the unique constraint fails.
        from app.identity import normalise_claim_value
        from app.models.identity_claim import ClaimType
        once = normalise_claim_value(ClaimType.EMAIL, "Foo@Bar.com")
        twice = normalise_claim_value(ClaimType.EMAIL, once)
        assert once == twice

    def test_rejects_empty(self):
        from app.identity import normalise_claim_value
        from app.models.identity_claim import ClaimType
        with pytest.raises(ValueError):
            normalise_claim_value(ClaimType.EMAIL, "   ")

    def test_rejects_no_at_sign(self):
        from app.identity import normalise_claim_value
        from app.models.identity_claim import ClaimType
        with pytest.raises(ValueError):
            normalise_claim_value(ClaimType.EMAIL, "not-an-email")

    def test_rejects_oversized(self):
        from app.identity import normalise_claim_value
        from app.models.identity_claim import ClaimType
        huge = ("a" * 320) + "@x.com"  # 326 chars > 320 cap
        with pytest.raises(ValueError):
            normalise_claim_value(ClaimType.EMAIL, huge)


class TestNormalisePhone:
    def test_accepts_e164(self):
        from app.identity import normalise_claim_value
        from app.models.identity_claim import ClaimType
        out = normalise_claim_value(ClaimType.PHONE, "+14165551234")
        assert out == "+14165551234"

    def test_strips_whitespace(self):
        from app.identity import normalise_claim_value
        from app.models.identity_claim import ClaimType
        out = normalise_claim_value(ClaimType.PHONE, "  +12025550100 ")
        assert out == "+12025550100"

    def test_rejects_no_plus(self):
        from app.identity import normalise_claim_value
        from app.models.identity_claim import ClaimType
        with pytest.raises(ValueError):
            normalise_claim_value(ClaimType.PHONE, "14165551234")

    def test_rejects_leading_zero_after_plus(self):
        from app.identity import normalise_claim_value
        from app.models.identity_claim import ClaimType
        # E.164: first digit after + must be 1-9.
        with pytest.raises(ValueError):
            normalise_claim_value(ClaimType.PHONE, "+0145551234")

    def test_rejects_letters(self):
        from app.identity import normalise_claim_value
        from app.models.identity_claim import ClaimType
        with pytest.raises(ValueError):
            normalise_claim_value(ClaimType.PHONE, "+1abc555")


class TestNormaliseSsoSubject:
    def test_preserves_case(self):
        from app.identity import normalise_claim_value
        from app.models.identity_claim import ClaimType
        out = normalise_claim_value(
            ClaimType.SSO_SUBJECT, "okta|AbC123_XYZ"
        )
        assert out == "okta|AbC123_XYZ"

    def test_strips_whitespace_only(self):
        from app.identity import normalise_claim_value
        from app.models.identity_claim import ClaimType
        out = normalise_claim_value(
            ClaimType.SSO_SUBJECT, "  google|sub-007  "
        )
        assert out == "google|sub-007"

    def test_rejects_empty(self):
        from app.identity import normalise_claim_value
        from app.models.identity_claim import ClaimType
        with pytest.raises(ValueError):
            normalise_claim_value(ClaimType.SSO_SUBJECT, "")


class TestNormaliseTypeErrors:
    def test_rejects_non_claim_type(self):
        from app.identity import normalise_claim_value
        with pytest.raises(TypeError):
            normalise_claim_value("EMAIL", "a@b.co")  # type: ignore[arg-type]

    def test_rejects_non_string_value(self):
        from app.identity import normalise_claim_value
        from app.models.identity_claim import ClaimType
        with pytest.raises(TypeError):
            normalise_claim_value(ClaimType.EMAIL, 12345)  # type: ignore[arg-type]


# ---------------------------------------------------------------------
# 3. IdentityResolution dataclass
# ---------------------------------------------------------------------

class TestIdentityResolutionShape:
    def test_required_fields(self):
        import dataclasses
        from app.identity import IdentityResolution
        names = {f.name for f in dataclasses.fields(IdentityResolution)}
        assert names == {
            "user_id",
            "conversation_id",
            "identity_claim_id",
            "is_new_user",
            "is_new_conversation",
        }

    def test_frozen(self):
        from app.identity import IdentityResolution
        res = IdentityResolution(
            user_id=uuid.uuid4(),
            conversation_id=uuid.uuid4(),
            identity_claim_id=uuid.uuid4(),
            is_new_user=False,
            is_new_conversation=False,
        )
        with pytest.raises((AttributeError, Exception)):
            res.is_new_user = True  # type: ignore[misc]


# ---------------------------------------------------------------------
# 4. IdentityResolver class surface
# ---------------------------------------------------------------------

class TestResolverSurface:
    def test_constructor_signature(self):
        from app.identity import IdentityResolver
        sig = inspect.signature(IdentityResolver.__init__)
        assert list(sig.parameters.keys()) == ["self", "db"]

    def test_resolve_signature_kwonly(self):
        from app.identity import IdentityResolver
        sig = inspect.signature(IdentityResolver.resolve)
        params = sig.parameters
        assert "claim_type" in params
        assert "claim_value" in params
        assert "tenant_id" in params
        assert "domain_id" in params
        assert "issuing_adapter" in params
        for name, p in params.items():
            if name == "self":
                continue
            assert p.kind == inspect.Parameter.KEYWORD_ONLY, (
                f"{name} must be keyword-only"
            )

    def test_supported_claim_types_helper(self):
        from app.identity import IdentityResolver
        from app.models.identity_claim import ClaimType
        types = list(IdentityResolver.supported_claim_types())
        assert ClaimType.EMAIL in types
        assert ClaimType.PHONE in types
        assert ClaimType.SSO_SUBJECT in types


# ---------------------------------------------------------------------
# 5. Input validation at the boundary
# ---------------------------------------------------------------------

class _NoCallDb:
    """Asserts no DB call is made on the validation-rejected path."""

    def execute(self, *a, **kw):  # pragma: no cover
        raise AssertionError(
            "validation must reject before any DB query is executed"
        )

    def add(self, *a, **kw):  # pragma: no cover
        raise AssertionError("validation must reject before any .add()")

    def flush(self):  # pragma: no cover
        raise AssertionError("validation must reject before any .flush()")


class TestResolverInputValidation:
    def test_rejects_blank_tenant_id(self):
        from app.identity import IdentityResolver
        from app.models.identity_claim import ClaimType
        r = IdentityResolver(db=_NoCallDb())  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            r.resolve(
                claim_type=ClaimType.EMAIL,
                claim_value="a@b.co",
                tenant_id="",
                domain_id="d-1",
                issuing_adapter="widget",
            )

    def test_rejects_blank_domain_id(self):
        from app.identity import IdentityResolver
        from app.models.identity_claim import ClaimType
        r = IdentityResolver(db=_NoCallDb())  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            r.resolve(
                claim_type=ClaimType.EMAIL,
                claim_value="a@b.co",
                tenant_id="t-1",
                domain_id="   ",
                issuing_adapter="widget",
            )

    def test_rejects_blank_issuing_adapter(self):
        from app.identity import IdentityResolver
        from app.models.identity_claim import ClaimType
        r = IdentityResolver(db=_NoCallDb())  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            r.resolve(
                claim_type=ClaimType.EMAIL,
                claim_value="a@b.co",
                tenant_id="t-1",
                domain_id="d-1",
                issuing_adapter="",
            )

    def test_rejects_malformed_email(self):
        from app.identity import IdentityResolver
        from app.models.identity_claim import ClaimType
        r = IdentityResolver(db=_NoCallDb())  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            r.resolve(
                claim_type=ClaimType.EMAIL,
                claim_value="not-an-email",
                tenant_id="t-1",
                domain_id="d-1",
                issuing_adapter="widget",
            )


# ---------------------------------------------------------------------
# 6. Mint path (no existing claim, no existing session) — stub DB
# ---------------------------------------------------------------------

class _ScriptedDb:
    """A scripted SQLAlchemy-Session-shaped stub.

    Provides:
        * .execute(stmt) -> object with .scalar_one_or_none() that
          returns the next value from `scripted_results` (FIFO).
        * .add(obj) -> records the object in .added.
        * .flush() -> populates server-default-y fields on added
          objects (just bumps a counter).
    """

    def __init__(self, scripted_results: list):
        # Each entry is the value .scalar_one_or_none() should return
        # on successive .execute() calls.
        self.scripted_results = list(scripted_results)
        self.added: list = []
        self.executed: list = []
        self.flush_count = 0

    def execute(self, stmt):
        self.executed.append(stmt)
        if not self.scripted_results:
            raise AssertionError(
                "_ScriptedDb ran out of scripted results "
                f"(execute call #{len(self.executed)})"
            )
        result = self.scripted_results.pop(0)

        class _R:

            def __init__(_self, val):
                _self._val = val

            def scalar_one_or_none(_self):
                return _self._val
        return _R(result)

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        self.flush_count += 1


class TestMintPath:
    def test_mints_user_conversation_and_claim(self):
        from app.identity import IdentityResolver
        from app.models.conversation import Conversation
        from app.models.identity_claim import ClaimType, IdentityClaim
        from app.models.user import User
        # Script: first execute (claim lookup) returns None -> miss.
        db = _ScriptedDb(scripted_results=[None])
        r = IdentityResolver(db=db)  # type: ignore[arg-type]
        res = r.resolve(
            claim_type=ClaimType.EMAIL,
            claim_value="Aryan@Example.COM",
            tenant_id="t-1",
            domain_id="d-1",
            issuing_adapter="widget",
        )
        # Should add exactly one of each kind.
        kinds = [type(o).__name__ for o in db.added]
        assert kinds.count("User") == 1
        assert kinds.count("Conversation") == 1
        assert kinds.count("IdentityClaim") == 1
        # Resolution flags
        assert res.is_new_user is True
        assert res.is_new_conversation is True
        # PKs are non-None UUIDs
        assert isinstance(res.user_id, uuid.UUID)
        assert isinstance(res.conversation_id, uuid.UUID)
        assert isinstance(res.identity_claim_id, uuid.UUID)
        # Claim was minted with normalised value
        new_claim = next(o for o in db.added if isinstance(o, IdentityClaim))
        assert new_claim.claim_value == "aryan@example.com"
        assert new_claim.tenant_id == "t-1"
        assert new_claim.domain_id == "d-1"
        assert new_claim.issuing_adapter == "widget"
        assert new_claim.verified_at is None
        assert new_claim.active is True
        # User is synthetic
        new_user = next(o for o in db.added if isinstance(o, User))
        assert new_user.synthetic is True
        assert new_user.active is True
        # Conversation scoped to (t-1, d-1)
        new_conv = next(o for o in db.added if isinstance(o, Conversation))
        assert new_conv.tenant_id == "t-1"
        assert new_conv.domain_id == "d-1"
        assert new_conv.active is True
        # Resolver MUST flush so caller sees PKs before commit.
        assert db.flush_count >= 1


# ---------------------------------------------------------------------
# 7. Existing-claim hit path (with prior active session)
# ---------------------------------------------------------------------

class _FakeIdentityClaim:
    """Tiny stand-in for an IdentityClaim row from the DB."""

    def __init__(self, user_id: uuid.UUID, claim_id: uuid.UUID):
        self.user_id = user_id
        self.id = claim_id


class _FakeSession:
    """Tiny stand-in for a SessionModel row."""

    def __init__(self, conversation_id: uuid.UUID | None):
        self.conversation_id = conversation_id


class TestExistingClaimHitPath:
    def test_existing_claim_existing_session_binds_to_conversation(self):
        from app.identity import IdentityResolver
        from app.models.identity_claim import ClaimType
        existing_user = uuid.uuid4()
        existing_claim = uuid.uuid4()
        existing_conv = uuid.uuid4()
        # Script: 1st execute -> existing claim, 2nd execute ->
        # latest active session with a non-NULL conversation_id.
        db = _ScriptedDb(scripted_results=[
            _FakeIdentityClaim(existing_user, existing_claim),
            _FakeSession(existing_conv),
        ])
        r = IdentityResolver(db=db)  # type: ignore[arg-type]
        res = r.resolve(
            claim_type=ClaimType.PHONE,
            claim_value="+14165551234",
            tenant_id="t-1",
            domain_id="d-1",
            issuing_adapter="voice_gateway",
        )
        # No mints.
        assert db.added == []
        assert db.flush_count == 0
        # Returns the bound user + conversation.
        assert res.user_id == existing_user
        assert res.conversation_id == existing_conv
        assert res.identity_claim_id == existing_claim
        assert res.is_new_user is False
        assert res.is_new_conversation is False

    def test_existing_claim_no_session_mints_conversation_only(self):
        from app.identity import IdentityResolver
        from app.models.conversation import Conversation
        from app.models.identity_claim import ClaimType
        from app.models.identity_claim import IdentityClaim
        from app.models.user import User
        existing_user = uuid.uuid4()
        existing_claim = uuid.uuid4()
        # Script: 1st -> existing claim, 2nd -> no recent session.
        db = _ScriptedDb(scripted_results=[
            _FakeIdentityClaim(existing_user, existing_claim),
            None,
        ])
        r = IdentityResolver(db=db)  # type: ignore[arg-type]
        res = r.resolve(
            claim_type=ClaimType.SSO_SUBJECT,
            claim_value="okta|sub-007",
            tenant_id="t-1",
            domain_id="d-1",
            issuing_adapter="programmatic_api",
        )
        # ONE conversation mint, NO user mint, NO claim mint.
        kinds = [type(o).__name__ for o in db.added]
        assert kinds == ["Conversation"]
        assert all(not isinstance(o, User) for o in db.added)
        assert all(not isinstance(o, IdentityClaim) for o in db.added)
        new_conv = next(o for o in db.added if isinstance(o, Conversation))
        assert new_conv.tenant_id == "t-1"
        assert new_conv.domain_id == "d-1"
        assert res.user_id == existing_user
        assert res.identity_claim_id == existing_claim
        assert res.is_new_user is False
        assert res.is_new_conversation is True


# ---------------------------------------------------------------------
# 8. Resolver does NOT commit (caller owns the transaction)
# ---------------------------------------------------------------------

class TestResolverDoesNotCommit:
    """§3.2.11 contract: caller's outer transaction commits.

    AST check: the resolver module must NOT call self.db.commit().
    """

    def test_no_commit_call_in_resolver_source(self):
        src = RESOLVER_PATH.read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "commit":
                # Only fail if the receiver is self.db. Reduces
                # false positives if some unrelated code calls
                # commit() on something else.
                if (
                    isinstance(node.value, ast.Attribute)
                    and node.value.attr == "db"
                    and isinstance(node.value.value, ast.Name)
                    and node.value.value.id == "self"
                ):
                    pytest.fail(
                        "IdentityResolver must NOT call self.db.commit(); "
                        "the caller's transaction boundary commits the "
                        "resolver's mints together with the session row."
                    )


# ---------------------------------------------------------------------
# 9. Caller-side audit signal: existing claim path doesn't pollute add()
# ---------------------------------------------------------------------

class TestExistingClaimPathDoesNotAddNewClaim:
    """Defense: when a claim exists, the resolver MUST NOT INSERT
    another row with the same (claim_type, claim_value, scope).

    The unique constraint would catch it server-side, but tripping the
    constraint adds latency and rolls back the parent txn unnecessarily.
    """

    def test_no_new_claim_on_hit(self):
        from app.identity import IdentityResolver
        from app.models.identity_claim import ClaimType, IdentityClaim
        existing_user = uuid.uuid4()
        existing_claim = uuid.uuid4()
        existing_conv = uuid.uuid4()
        db = _ScriptedDb(scripted_results=[
            _FakeIdentityClaim(existing_user, existing_claim),
            _FakeSession(existing_conv),
        ])
        r = IdentityResolver(db=db)  # type: ignore[arg-type]
        r.resolve(
            claim_type=ClaimType.EMAIL,
            claim_value="hit@example.com",
            tenant_id="t-1",
            domain_id="d-1",
            issuing_adapter="widget",
        )
        new_claims = [o for o in db.added if isinstance(o, IdentityClaim)]
        assert new_claims == []
