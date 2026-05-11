"""
Backend-free contract tests for Step 24.5c sub-branch 2:
CrossSessionRetriever.

These tests are AST + import-shape checks. They run in the backend-free
CI lane (no Postgres, no engine). The retriever's runtime correctness
will be exercised end-to-end in sub-branch 5's live-e2e harness against
a real database.

Coverage:
    * Module import + symbol export.
    * CrossSessionPassage dataclass shape (frozen + required fields +
      ARCHITECTURE §3.2.11 provenance triple).
    * CrossSessionRetriever class surface (constructor, retrieve
      signature, type annotations).
    * Input-validation behaviour (TypeError on non-UUID
      conversation_id, ValueError on blank scope).
    * Limit clamping ([1, MAX_LIMIT] bounds).
    * Module docstring referenes §3.2.11 (design-contract pinning).

Scope-filter SQL behaviour is checked in sub-branch 5's live-e2e
harness, not here — that requires a real Postgres engine to assert on.
"""
from __future__ import annotations

import ast
import inspect
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest


# -----------------------------------------------------------------------
# Module-level: source/AST helpers
# -----------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
RETRIEVER_PATH = REPO_ROOT / "app" / "memory" / "cross_session_retriever.py"


def _retriever_ast() -> ast.Module:
    src = RETRIEVER_PATH.read_text()
    return ast.parse(src)


def _find_class(tree: ast.Module, name: str) -> ast.ClassDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    pytest.fail(f"class {name} not found in {RETRIEVER_PATH}")


def _find_function_in_class(
    cls: ast.ClassDef, name: str
) -> ast.FunctionDef:
    for node in cls.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    pytest.fail(f"method {name} not found in class {cls.name}")


# -----------------------------------------------------------------------
# 1. Module import + symbol export
# -----------------------------------------------------------------------

class TestModuleSurface:
    def test_module_imports(self):
        from app.memory import cross_session_retriever  # noqa: F401

    def test_exports_retriever_class(self):
        from app.memory.cross_session_retriever import (
            CrossSessionRetriever,
        )
        assert inspect.isclass(CrossSessionRetriever)

    def test_exports_passage_dataclass(self):
        from app.memory.cross_session_retriever import (
            CrossSessionPassage,
        )
        assert inspect.isclass(CrossSessionPassage)

    def test_exports_max_limit_constant(self):
        from app.memory.cross_session_retriever import MAX_LIMIT
        # MAX_LIMIT bounds the SQL row count irrespective of caller
        # mistakes. Per module design: defense-in-depth against
        # unbounded reads. Concrete value (100) is the v1 ceiling
        # — looser bound checked here so we don't tie tests to an
        # exact number that may be re-tuned.
        assert isinstance(MAX_LIMIT, int)
        assert MAX_LIMIT >= 10
        assert MAX_LIMIT <= 1000

    def test_module_docstring_pins_design_contract(self):
        # The module docstring must reference §3.2.11 so a future
        # agent reading the file sees the design pin without needing
        # to grep ARCHITECTURE.md.
        from app.memory import cross_session_retriever
        doc = cross_session_retriever.__doc__ or ""
        assert "3.2.11" in doc, (
            "module docstring must reference ARCHITECTURE §3.2.11"
        )
        # Also referenes §4.7 for the defense-in-depth scope check.
        assert "4.7" in doc, (
            "module docstring must reference §4.7 defense-in-depth"
        )


# -----------------------------------------------------------------------
# 2. CrossSessionPassage dataclass shape
# -----------------------------------------------------------------------

class TestCrossSessionPassageShape:
    def test_is_frozen_dataclass(self):
        from app.memory.cross_session_retriever import (
            CrossSessionPassage,
        )
        # dataclasses.fields exists on dataclasses; frozen check is
        # via instance immutability.
        passage = CrossSessionPassage(
            content="hi",
            role="user",
            source_session_id="s-1",
            source_channel="web",
            timestamp=datetime.now(timezone.utc),
            message_id=1,
        )
        with pytest.raises((AttributeError, Exception)):
            passage.content = "mutated"  # type: ignore[misc]

    def test_required_provenance_fields_present(self):
        # ARCHITECTURE §3.2.11: "ranked passages with provenance
        # metadata (source_session_id, source_channel, timestamp)"
        from app.memory.cross_session_retriever import (
            CrossSessionPassage,
        )
        import dataclasses
        field_names = {
            f.name for f in dataclasses.fields(CrossSessionPassage)
        }
        # The §3.2.11 provenance triple. Without ALL THREE, the
        # caller cannot honour the design contract.
        assert "source_session_id" in field_names
        assert "source_channel" in field_names
        assert "timestamp" in field_names
        # Plus content + role to be useful at all to the LLM.
        assert "content" in field_names
        assert "role" in field_names
        # Plus message_id for caller-side dedup.
        assert "message_id" in field_names

    def test_field_count_is_exact(self):
        # If we add or remove a field, that's a contract change and
        # this test should fail to force a doc/spec review.
        from app.memory.cross_session_retriever import (
            CrossSessionPassage,
        )
        import dataclasses
        fields = dataclasses.fields(CrossSessionPassage)
        assert len(fields) == 6, (
            f"CrossSessionPassage has {len(fields)} fields; expected "
            "exactly 6 (content, role, source_session_id, "
            "source_channel, timestamp, message_id). If you intend "
            "to change the contract, update ARCHITECTURE §3.2.11 too."
        )


# -----------------------------------------------------------------------
# 3. CrossSessionRetriever class surface
# -----------------------------------------------------------------------

class TestRetrieverClassSurface:
    def test_constructor_signature(self):
        # Constructor takes a single SQLAlchemy Session parameter
        # called `db` — matches MemoryRepository precedent
        # (app/repositories/memory_repository.py:25).
        from app.memory.cross_session_retriever import (
            CrossSessionRetriever,
        )
        sig = inspect.signature(CrossSessionRetriever.__init__)
        params = list(sig.parameters.keys())
        assert params == ["self", "db"], (
            f"__init__ parameters were {params}, expected ['self', 'db']"
        )

    def test_retrieve_signature_kwonly(self):
        # retrieve() takes keyword-only args. Positional args invite
        # subtle bugs where a caller swaps tenant_id and domain_id —
        # the kw-only signature makes that a compile-time error.
        from app.memory.cross_session_retriever import (
            CrossSessionRetriever,
        )
        sig = inspect.signature(CrossSessionRetriever.retrieve)
        params = sig.parameters
        # Expect: self + kw-only required (conversation_id, tenant_id,
        # domain_id) + kw-only optional (limit, exclude_session_id).
        assert "conversation_id" in params
        assert "tenant_id" in params
        assert "domain_id" in params
        assert "limit" in params
        assert "exclude_session_id" in params
        # Every non-self parameter must be KEYWORD_ONLY.
        for name, p in params.items():
            if name == "self":
                continue
            assert p.kind == inspect.Parameter.KEYWORD_ONLY, (
                f"{name} must be keyword-only "
                f"(got kind={p.kind.name})"
            )

    def test_retrieve_default_limit_is_reasonable(self):
        from app.memory.cross_session_retriever import (
            CrossSessionRetriever,
        )
        sig = inspect.signature(CrossSessionRetriever.retrieve)
        default = sig.parameters["limit"].default
        # Default should be small enough that the retriever does
        # not blow up the LLM context by accident.
        assert isinstance(default, int)
        assert 1 <= default <= 50

    def test_retrieve_exclude_default_none(self):
        from app.memory.cross_session_retriever import (
            CrossSessionRetriever,
        )
        sig = inspect.signature(CrossSessionRetriever.retrieve)
        assert sig.parameters["exclude_session_id"].default is None

    def test_retrieve_returns_list(self):
        # Return annotation is "list[CrossSessionPassage]". The AST
        # check here is robust against typing imports/aliases.
        tree = _retriever_ast()
        cls = _find_class(tree, "CrossSessionRetriever")
        fn = _find_function_in_class(cls, "retrieve")
        assert fn.returns is not None, (
            "retrieve() must have a return annotation"
        )
        ann_src = ast.unparse(fn.returns)
        assert "list" in ann_src
        assert "CrossSessionPassage" in ann_src


# -----------------------------------------------------------------------
# 4. Input validation (fail-loud at boundary)
# -----------------------------------------------------------------------

class _StubDb:
    """Minimal stand-in for a SQLAlchemy Session.

    The validation tests below short-circuit BEFORE any DB call, so
    we never need a real engine. If validation ever stops short-
    circuiting and falls through to .execute(), this stub will raise
    a clear AttributeError, which is itself a test failure signal.
    """

    pass


class TestInputValidation:
    def test_rejects_non_uuid_conversation_id(self):
        from app.memory.cross_session_retriever import (
            CrossSessionRetriever,
        )
        r = CrossSessionRetriever(db=_StubDb())  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            r.retrieve(
                conversation_id="not-a-uuid",  # type: ignore[arg-type]
                tenant_id="t-1",
                domain_id="d-1",
            )

    def test_rejects_blank_tenant_id(self):
        from app.memory.cross_session_retriever import (
            CrossSessionRetriever,
        )
        r = CrossSessionRetriever(db=_StubDb())  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            r.retrieve(
                conversation_id=uuid.uuid4(),
                tenant_id="",
                domain_id="d-1",
            )

    def test_rejects_whitespace_tenant_id(self):
        from app.memory.cross_session_retriever import (
            CrossSessionRetriever,
        )
        r = CrossSessionRetriever(db=_StubDb())  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            r.retrieve(
                conversation_id=uuid.uuid4(),
                tenant_id="   ",
                domain_id="d-1",
            )

    def test_rejects_blank_domain_id(self):
        from app.memory.cross_session_retriever import (
            CrossSessionRetriever,
        )
        r = CrossSessionRetriever(db=_StubDb())  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            r.retrieve(
                conversation_id=uuid.uuid4(),
                tenant_id="t-1",
                domain_id="",
            )

    def test_rejects_whitespace_domain_id(self):
        from app.memory.cross_session_retriever import (
            CrossSessionRetriever,
        )
        r = CrossSessionRetriever(db=_StubDb())  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            r.retrieve(
                conversation_id=uuid.uuid4(),
                tenant_id="t-1",
                domain_id="\t\n",
            )


# -----------------------------------------------------------------------
# 5. Limit clamping (defense in depth)
# -----------------------------------------------------------------------

class _CapturingDb:
    """Captures the limit out of the SQL statement without running it.

    We rely on SQLAlchemy's compiled-statement representation, NOT on
    string parsing — that's brittle. The Select object exposes its
    ._limit_clause directly.
    """

    def __init__(self) -> None:
        self.captured_stmt = None

    def execute(self, stmt):
        self.captured_stmt = stmt
        # Return an object whose .all() returns an empty list, so
        # retrieve() walks the empty-result path without DB I/O.

        class _R:

            def all(_self):
                return []
        return _R()


def _captured_limit(db: _CapturingDb) -> int | None:
    """Pull the integer limit out of the captured Select.

    Robust to SQLAlchemy version drift via getattr fallback.
    """
    stmt = db.captured_stmt
    if stmt is None:
        return None
    # SQLAlchemy 2.x exposes the limit via _limit_clause as a
    # BindParameter; .value carries the int.
    lc = getattr(stmt, "_limit_clause", None)
    if lc is None:
        return None
    return getattr(lc, "value", None)


class TestLimitClamping:
    def test_limit_below_one_clamps_up(self):
        from app.memory.cross_session_retriever import (
            CrossSessionRetriever,
        )
        db = _CapturingDb()
        r = CrossSessionRetriever(db=db)  # type: ignore[arg-type]
        r.retrieve(
            conversation_id=uuid.uuid4(),
            tenant_id="t-1",
            domain_id="d-1",
            limit=0,
        )
        assert _captured_limit(db) == 1

    def test_negative_limit_clamps_up(self):
        from app.memory.cross_session_retriever import (
            CrossSessionRetriever,
        )
        db = _CapturingDb()
        r = CrossSessionRetriever(db=db)  # type: ignore[arg-type]
        r.retrieve(
            conversation_id=uuid.uuid4(),
            tenant_id="t-1",
            domain_id="d-1",
            limit=-7,
        )
        assert _captured_limit(db) == 1

    def test_limit_above_max_clamps_down(self):
        from app.memory.cross_session_retriever import (
            CrossSessionRetriever,
            MAX_LIMIT,
        )
        db = _CapturingDb()
        r = CrossSessionRetriever(db=db)  # type: ignore[arg-type]
        r.retrieve(
            conversation_id=uuid.uuid4(),
            tenant_id="t-1",
            domain_id="d-1",
            limit=MAX_LIMIT * 10,
        )
        assert _captured_limit(db) == MAX_LIMIT

    def test_limit_in_range_passes_through(self):
        from app.memory.cross_session_retriever import (
            CrossSessionRetriever,
        )
        db = _CapturingDb()
        r = CrossSessionRetriever(db=db)  # type: ignore[arg-type]
        r.retrieve(
            conversation_id=uuid.uuid4(),
            tenant_id="t-1",
            domain_id="d-1",
            limit=17,
        )
        assert _captured_limit(db) == 17


# -----------------------------------------------------------------------
# 6. SQL shape — scope predicates are present
# -----------------------------------------------------------------------

class TestSqlScopePredicates:
    """Verify the SQL statement filters on all three scope columns.

    We do this by AST inspection of the retrieve() method body — the
    statement is built declaratively, so the .where() call chain is
    visible in the source.
    """

    def test_retrieve_where_clauses_include_three_scope_predicates(self):
        tree = _retriever_ast()
        cls = _find_class(tree, "CrossSessionRetriever")
        fn = _find_function_in_class(cls, "retrieve")
        src = ast.unparse(fn)
        # The three required scope predicates as they appear in the
        # source. Token-based check is robust against whitespace.
        assert "SessionModel.conversation_id == conversation_id" in src
        assert "SessionModel.tenant_id == tenant_id" in src
        assert "SessionModel.domain_id == domain_id" in src

    def test_retrieve_orders_by_message_created_at_desc(self):
        tree = _retriever_ast()
        cls = _find_class(tree, "CrossSessionRetriever")
        fn = _find_function_in_class(cls, "retrieve")
        src = ast.unparse(fn)
        assert "MessageModel.created_at.desc()" in src

    def test_retrieve_joins_messages_to_sessions(self):
        tree = _retriever_ast()
        cls = _find_class(tree, "CrossSessionRetriever")
        fn = _find_function_in_class(cls, "retrieve")
        src = ast.unparse(fn)
        # The join condition: SessionModel.id == MessageModel.session_id.
        # Either ordering is acceptable in SQLAlchemy; check both.
        assert (
            "SessionModel.id == MessageModel.session_id" in src
            or "MessageModel.session_id == SessionModel.id" in src
        )

    def test_retrieve_supports_exclude_session_id_filter(self):
        tree = _retriever_ast()
        cls = _find_class(tree, "CrossSessionRetriever")
        fn = _find_function_in_class(cls, "retrieve")
        src = ast.unparse(fn)
        # The exclusion predicate must be conditional on the arg
        # being non-None.
        assert "exclude_session_id is not None" in src
        assert "SessionModel.id != exclude_session_id" in src


# -----------------------------------------------------------------------
# 7. Post-query defense-in-depth assertion
# -----------------------------------------------------------------------

class TestDefenseInDepthScopeCheck:
    """The retriever re-asserts scope on the materialised row.

    This is the FOURTH check in the §4.7 scope-enforcement chain.
    The first three are auth surface, runtime scope resolver, and
    persistence layer. This module makes it four.
    """

    def test_post_query_scope_loop_present(self):
        tree = _retriever_ast()
        cls = _find_class(tree, "CrossSessionRetriever")
        fn = _find_function_in_class(cls, "retrieve")
        src = ast.unparse(fn)
        # Look for the explicit per-row scope assertion. We check
        # for the three predicates as they appear in the post-loop
        # `if (...)` guard.
        assert "session.tenant_id != tenant_id" in src
        assert "session.domain_id != domain_id" in src
        assert "session.conversation_id != conversation_id" in src

    def test_post_query_scope_drops_are_logged(self):
        tree = _retriever_ast()
        cls = _find_class(tree, "CrossSessionRetriever")
        fn = _find_function_in_class(cls, "retrieve")
        src = ast.unparse(fn)
        # Drops must be observable. logger.error on the per-row
        # branch and on the aggregate count after the loop.
        assert "logger.error" in src
        # Drop counter is incremented on the mismatch branch.
        assert "dropped" in src
