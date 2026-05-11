"""
Backend-free contract tests for Step 24.5c sub-branch 4: adapter claims.

This sub-branch wires the IdentityResolver into the session-creation
path. The hot routes (widget, programmatic-API) are NOT touched in
this sub-branch -- they will adopt create_session_with_identity()
incrementally and that adoption is end-to-end-exercised by sub-branch
5's live harness.

Coverage:
    * SessionRepository.create_session() accepts an optional
      conversation_id parameter (UUID or None), and the parameter
      lands on SessionModel.conversation_id.
    * Legacy callers (no conversation_id passed) still produce a
      NULL-conversation session -- behavioural compat per the
      \u00a73.2.11 nullable-by-design contract.
    * SessionService.create_session() accepts and forwards
      conversation_id.
    * SessionService.create_session_with_identity() exists, has
      a keyword-only signature, and returns SessionWithIdentity.
    * create_session_with_identity() lazy-imports IdentityResolver
      (so legacy routes that never call it don't pay import cost).
    * SessionWithIdentity dataclass shape (frozen + exact fields).
    * create_session_with_identity() wires through the resolver's
      mints (User + Conversation + IdentityClaim added on the same
      db handle, then session row created with the resolved
      conversation_id).
"""
from __future__ import annotations

import ast
import inspect
import uuid
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_PATH = REPO_ROOT / "app" / "repositories" / "session_repository.py"
SVC_PATH = REPO_ROOT / "app" / "services" / "session_service.py"


# ---------------------------------------------------------------------
# 1. SessionRepository.create_session() shape
# ---------------------------------------------------------------------

class TestRepositoryShape:
    def test_create_session_signature_has_conversation_id(self):
        from app.repositories.session_repository import SessionRepository
        sig = inspect.signature(SessionRepository.create_session)
        assert "conversation_id" in sig.parameters
        # Default must be None so legacy callers see no change.
        assert sig.parameters["conversation_id"].default is None

    def test_create_session_conversation_id_is_kw_only(self):
        from app.repositories.session_repository import SessionRepository
        sig = inspect.signature(SessionRepository.create_session)
        p = sig.parameters["conversation_id"]
        assert p.kind == inspect.Parameter.KEYWORD_ONLY

    def test_repository_source_passes_conversation_id_to_model(self):
        # AST-level check: the SessionModel(...) constructor call
        # inside create_session() must include conversation_id=...
        src = REPO_PATH.read_text()
        tree = ast.parse(src)
        # Find create_session() method and look for the
        # SessionModel(...) call.
        for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
            if cls.name != "SessionRepository":
                continue
            for fn in cls.body:
                if (
                    isinstance(fn, ast.FunctionDef)
                    and fn.name == "create_session"
                ):
                    fn_src = ast.unparse(fn)
                    assert "conversation_id=conversation_id" in fn_src
                    return
        pytest.fail("SessionRepository.create_session not found")


# ---------------------------------------------------------------------
# 2. SessionService.create_session() shape (legacy path)
# ---------------------------------------------------------------------

class TestServiceLegacyShape:
    def test_create_session_forwards_conversation_id(self):
        from app.services.session_service import SessionService
        sig = inspect.signature(SessionService.create_session)
        assert "conversation_id" in sig.parameters
        assert sig.parameters["conversation_id"].default is None

    def test_legacy_signature_unchanged_for_other_params(self):
        # Behavioural compat: the params legacy callers already pass
        # must still be accepted with the same defaults. Catches an
        # accidental rename.
        from app.services.session_service import SessionService
        sig = inspect.signature(SessionService.create_session)
        for name, default in [
            ("tenant_id", inspect.Parameter.empty),
            ("domain_id", inspect.Parameter.empty),
            ("agent_id", None),
            ("user_id", None),
            ("channel", "web"),
        ]:
            assert name in sig.parameters
            assert sig.parameters[name].default == default


# ---------------------------------------------------------------------
# 3. SessionService.create_session_with_identity() surface
# ---------------------------------------------------------------------

class TestServiceIdentityShape:
    def test_method_exists(self):
        from app.services.session_service import SessionService
        assert hasattr(SessionService, "create_session_with_identity")

    def test_signature_is_kw_only(self):
        from app.services.session_service import SessionService
        sig = inspect.signature(
            SessionService.create_session_with_identity
        )
        required = {
            "tenant_id", "domain_id", "claim_type",
            "claim_value", "issuing_adapter",
        }
        optional = {"agent_id", "channel"}
        for name, p in sig.parameters.items():
            if name == "self":
                continue
            assert p.kind == inspect.Parameter.KEYWORD_ONLY, (
                f"{name} must be keyword-only"
            )
        seen = set(sig.parameters.keys())
        assert required.issubset(seen)
        assert optional.issubset(seen)


# ---------------------------------------------------------------------
# 4. SessionWithIdentity dataclass shape
# ---------------------------------------------------------------------

class TestSessionWithIdentityShape:
    def test_required_fields(self):
        import dataclasses
        from app.services.session_service import SessionWithIdentity
        names = {f.name for f in dataclasses.fields(SessionWithIdentity)}
        assert names == {
            "session",
            "user_id",
            "conversation_id",
            "identity_claim_id",
            "is_new_user",
            "is_new_conversation",
        }

    def test_is_frozen(self):
        from app.services.session_service import SessionWithIdentity
        s = SessionWithIdentity(
            session=object(),
            user_id=uuid.uuid4(),
            conversation_id=uuid.uuid4(),
            identity_claim_id=uuid.uuid4(),
            is_new_user=False,
            is_new_conversation=False,
        )
        with pytest.raises((AttributeError, Exception)):
            s.is_new_user = True  # type: ignore[misc]


# ---------------------------------------------------------------------
# 5. Lazy-import discipline
# ---------------------------------------------------------------------

class TestLazyImport:
    """The IdentityResolver must NOT be imported at module load.

    Legacy routes that never call create_session_with_identity() must
    not pay the resolver's import cost on cold start. We assert this
    by parsing the top-level imports.
    """

    def test_resolver_not_in_top_level_imports(self):
        src = SVC_PATH.read_text()
        tree = ast.parse(src)
        # Walk only TOP-LEVEL imports (tree.body), not nested.
        top_level_import_targets: list[str] = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top_level_import_targets.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    top_level_import_targets.append(
                        f"{module}.{alias.name}"
                    )
        forbidden = [
            t for t in top_level_import_targets
            if "identity" in t and "resolver" in t.lower()
        ]
        assert forbidden == [], (
            "IdentityResolver must be lazy-imported inside "
            f"create_session_with_identity (got top-level: {forbidden})"
        )

    def test_resolver_imported_inside_method(self):
        # The lazy import must actually be IN create_session_with_identity.
        src = SVC_PATH.read_text()
        tree = ast.parse(src)
        for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
            if cls.name != "SessionService":
                continue
            for fn in cls.body:
                if (
                    isinstance(fn, ast.FunctionDef)
                    and fn.name == "create_session_with_identity"
                ):
                    fn_src = ast.unparse(fn)
                    assert (
                        "from app.identity.resolver import IdentityResolver"
                        in fn_src
                    )
                    return
        pytest.fail("create_session_with_identity not found")


# ---------------------------------------------------------------------
# 6. End-to-end wiring via stub DB (no Postgres)
# ---------------------------------------------------------------------

class _CountingRepo:
    """A SessionRepository-shaped stub that captures create_session
    invocations without hitting a DB.

    We assemble a tiny db-handle stub that the IdentityResolver
    interacts with (scripted execute() + add() + flush()), and
    expose it as .db so SessionService can hand it to the resolver.
    """

    def __init__(self, scripted_resolver_results: list):
        self.db = _ResolverScriptedDb(scripted_resolver_results)
        self.created_sessions: list[dict] = []

    def create_session(
        self, *, session_id, tenant_id, domain_id, agent_id, user_id,
        channel, status="active", conversation_id=None,
    ):
        captured = {
            "session_id": session_id,
            "tenant_id": tenant_id,
            "domain_id": domain_id,
            "agent_id": agent_id,
            "user_id": user_id,
            "channel": channel,
            "status": status,
            "conversation_id": conversation_id,
        }
        self.created_sessions.append(captured)

        # Return a minimal object exposing the captured fields so
        # callers can inspect; mirroring SessionModel surface enough
        # for the contract tests.
        class _StubSession:

            def __init__(_self, **kw):
                for k, v in kw.items():
                    setattr(_self, k, v)
        return _StubSession(**captured)


class _ResolverScriptedDb:
    """Scripted db-handle that the IdentityResolver uses inside
    create_session_with_identity()."""

    def __init__(self, scripted_results: list):
        self.scripted_results = list(scripted_results)
        self.added: list = []
        self.flush_count = 0

    def execute(self, stmt):
        if not self.scripted_results:
            raise AssertionError("ran out of scripted results")
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


class TestEndToEndWiring:
    def test_mint_path_creates_session_with_conversation_id(self):
        from app.models.identity_claim import ClaimType
        from app.services.session_service import (
            SessionService,
            SessionWithIdentity,
        )
        # Resolver script: claim lookup -> miss (None).
        repo = _CountingRepo(scripted_resolver_results=[None])
        svc = SessionService(repository=repo)  # type: ignore[arg-type]
        result = svc.create_session_with_identity(
            tenant_id="t-1",
            domain_id="d-1",
            agent_id=None,
            channel="web",
            claim_type=ClaimType.EMAIL,
            claim_value="newperson@example.com",
            issuing_adapter="widget",
        )
        # 1) Resolver minted a User + Conversation + IdentityClaim.
        kinds = [type(o).__name__ for o in repo.db.added]
        assert kinds.count("User") == 1
        assert kinds.count("Conversation") == 1
        assert kinds.count("IdentityClaim") == 1
        # 2) Repository created one session with a populated
        #    conversation_id (UUID) and user_id (str(UUID)).
        assert len(repo.created_sessions) == 1
        sess = repo.created_sessions[0]
        assert isinstance(sess["conversation_id"], uuid.UUID)
        assert sess["user_id"] == str(result.user_id)
        assert sess["tenant_id"] == "t-1"
        assert sess["domain_id"] == "d-1"
        assert sess["channel"] == "web"
        # 3) Returned SessionWithIdentity flags both mints true.
        assert isinstance(result, SessionWithIdentity)
        assert result.is_new_user is True
        assert result.is_new_conversation is True
        # conversation_id is consistent across resolution and session.
        assert sess["conversation_id"] == result.conversation_id

    def test_hit_path_creates_session_bound_to_existing(self):
        from app.models.identity_claim import ClaimType
        from app.services.session_service import SessionService

        # Stand-in for an existing claim row.
        class _Claim:

            def __init__(_self, user_id, claim_id):
                _self.user_id = user_id
                _self.id = claim_id

        # Stand-in for a prior session with non-NULL conversation_id.
        class _PriorSession:

            def __init__(_self, conv_id):
                _self.conversation_id = conv_id

        existing_user = uuid.uuid4()
        existing_claim = uuid.uuid4()
        existing_conv = uuid.uuid4()
        repo = _CountingRepo(scripted_resolver_results=[
            _Claim(existing_user, existing_claim),
            _PriorSession(existing_conv),
        ])
        svc = SessionService(repository=repo)  # type: ignore[arg-type]
        result = svc.create_session_with_identity(
            tenant_id="t-1",
            domain_id="d-1",
            channel="programmatic_api",
            claim_type=ClaimType.EMAIL,
            claim_value="returning@example.com",
            issuing_adapter="programmatic_api",
        )
        # No mints (existing claim + existing session).
        assert repo.db.added == []
        assert repo.db.flush_count == 0
        # Session row created with the existing conversation_id and
        # the resolved user_id.
        assert len(repo.created_sessions) == 1
        sess = repo.created_sessions[0]
        assert sess["conversation_id"] == existing_conv
        assert sess["user_id"] == str(existing_user)
        assert sess["channel"] == "programmatic_api"
        # Returned flags reflect the bind, not a mint.
        assert result.is_new_user is False
        assert result.is_new_conversation is False
        assert result.user_id == existing_user
        assert result.conversation_id == existing_conv


# ---------------------------------------------------------------------
# 7. Legacy create_session() is behaviourally unchanged
# ---------------------------------------------------------------------

class _LegacyRepo:
    """Captures create_session() calls without doing anything else."""

    def __init__(self):
        self.calls: list[dict] = []

    def create_session(self, **kwargs):
        self.calls.append(kwargs)

        class _S:

            def __init__(_self, **kw):
                for k, v in kw.items():
                    setattr(_self, k, v)
        return _S(**kwargs)


class TestLegacyBehaviourUnchanged:
    def test_create_session_without_conversation_id_passes_none(self):
        from app.services.session_service import SessionService
        repo = _LegacyRepo()
        svc = SessionService(repository=repo)  # type: ignore[arg-type]
        svc.create_session(
            tenant_id="t-1",
            domain_id="d-1",
            agent_id="agent-1",
            user_id="legacy-user",
            channel="web",
        )
        assert len(repo.calls) == 1
        call = repo.calls[0]
        # The forwarded conversation_id is None -> legacy NULL-
        # conversation session per the nullable-by-design contract.
        assert call["conversation_id"] is None
        assert call["user_id"] == "legacy-user"
