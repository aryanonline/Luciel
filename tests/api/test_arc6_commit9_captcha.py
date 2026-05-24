"""Arc 6 Commit 9 -- CAPTCHA hard-gate pinning tests.

Closes D-free-tier-captcha-missing-2026-05-22 (P1). These tests are
the structural pins that guarantee the Commit-8 soft-pass window
cannot be reintroduced by accident:

  * TestCaptchaSchemaRequiredPin -- ``SignupFreeRequest.captcha_token``
    must be a REQUIRED ``str`` field with ``min_length=1``. Catches
    any future regression that flips the field back to ``Optional``
    or relaxes the length constraint.

  * TestSoftPassRemoved -- greps the route source to verify the
    Commit-8 ``signup_free.captcha_soft_pass`` log literal is GONE.
    A new occurrence of that string anywhere in the billing route
    file is a regression -- the soft-pass branch must stay deleted.

  * TestHardGateOrdering -- uses ``ast`` to walk the
    ``signup_free`` route function and assert that the
    ``verify_captcha`` call sits at the top level of the function
    body (inside the dedicated try/except), NOT wrapped in a
    conditional like ``if body.captcha_token:``. The captcha
    verification is unconditional in Commit 9.

This file is intentionally narrow: it is a structural/AST pin, not
a behavioural test. The behavioural surface (422 envelope, 501 on
unconfigured secret, 422 with error_codes on upstream-fail) is
covered by tests/api/test_signup_free_shape.py and
tests/api/test_arc6_signup_free.py.
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

# Match the moderation-import-time-failure mitigation pattern from
# the rest of tests/api/ (see drift
# D-step-30a-billing-shape-test-moderation-config-failure-2026-05-13
# resolution). Must come BEFORE any ``from app...`` import.
os.environ.setdefault("MODERATION_PROVIDER", "null")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg://stub:stub@localhost:5432/stub"
)

# Source-path constant. We deliberately read the route source from
# disk (not via ``inspect.getfile(billing_module)``) so the AST/grep
# pins below do NOT have to import ``app.api.v1.billing`` -- which
# would pull in the SQLAlchemy engine and require the psycopg DBAPI
# in the sandbox. The structural pin is a pure-source check.
BILLING_ROUTE_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "app" / "api" / "v1" / "billing.py"
)


# ---------------------------------------------------------------------
# 1. Schema-level pin: captcha_token is required str, min_length=1
# ---------------------------------------------------------------------

class TestCaptchaSchemaRequiredPin:
    """Pin the Pydantic schema shape for SignupFreeRequest.captcha_token.

    Commit 8 had this slot as ``str | None = Field(default=None,
    min_length=0)``. Commit 9 flips to ``str = Field(...,
    min_length=1)``. A regression that re-introduces a default value,
    relaxes min_length, or widens the type to Optional must FAIL here.
    """

    def test_field_is_required(self):
        from app.schemas.billing import SignupFreeRequest

        fields = SignupFreeRequest.model_fields
        assert "captcha_token" in fields, (
            "SignupFreeRequest must declare captcha_token"
        )
        field = fields["captcha_token"]
        # Pydantic v2 -- a required field has ``is_required()`` True.
        assert field.is_required(), (
            "captcha_token must be REQUIRED in Arc 6 Commit 9 "
            "(no default; not Optional). The Commit-8 soft-pass "
            "window is closed."
        )

    def test_field_annotation_is_plain_str_not_optional(self):
        from app.schemas.billing import SignupFreeRequest

        annotation = SignupFreeRequest.model_fields["captcha_token"].annotation
        # Plain ``str`` -- not ``str | None``, not ``Optional[str]``.
        assert annotation is str, (
            f"captcha_token annotation must be plain `str`, got: "
            f"{annotation!r}. Optional/Union types reintroduce the "
            f"Commit-8 soft-pass surface."
        )

    def test_field_min_length_is_at_least_one(self):
        from app.schemas.billing import SignupFreeRequest

        field = SignupFreeRequest.model_fields["captcha_token"]
        # Pydantic v2 surfaces validation constraints in
        # ``field.metadata`` as a list of constraint objects. We
        # look for the MinLen marker (annotated_types.MinLen) and
        # assert its value is >= 1.
        min_lens = []
        for marker in field.metadata:
            value = getattr(marker, "min_length", None)
            if value is not None:
                min_lens.append(value)
        assert min_lens, (
            f"captcha_token must declare a min_length constraint; "
            f"field.metadata = {field.metadata!r}"
        )
        assert all(v >= 1 for v in min_lens), (
            f"captcha_token min_length must be >= 1, got: {min_lens}"
        )


# ---------------------------------------------------------------------
# 2. Source-grep pin: the Commit-8 soft-pass log literal is gone
# ---------------------------------------------------------------------

class TestSoftPassRemoved:
    """The Commit-8 ``signup_free.captcha_soft_pass`` log literal
    must be absent from the billing route source. Re-introducing
    that string anywhere in the route file is a regression -- the
    branch that emitted it has been deleted.
    """

    def _billing_route_source(self) -> str:
        # Read the route source from disk. We intentionally do NOT
        # import ``app.api.v1.billing`` here -- importing it triggers
        # the SQLAlchemy engine eagerly and requires the psycopg DBAPI
        # which is not installed in the sandbox. A pure-source grep
        # is sufficient for this structural pin and stays sandbox-
        # runnable.
        assert BILLING_ROUTE_SOURCE.exists(), (
            f"Expected billing route at {BILLING_ROUTE_SOURCE} -- "
            f"repo layout changed?"
        )
        return BILLING_ROUTE_SOURCE.read_text(encoding="utf-8")

    def test_soft_pass_log_literal_absent(self):
        source = self._billing_route_source()
        # The literal log key from the Commit-8 implementation.
        assert "signup_free.captcha_soft_pass" not in source, (
            "Commit-8 'signup_free.captcha_soft_pass' WARN log "
            "literal must be REMOVED from app/api/v1/billing.py in "
            "Commit 9. Its presence means the soft-pass branch was "
            "(re)introduced."
        )

    def test_no_optional_captcha_token_branch_marker(self):
        # The Commit-8 branch checked ``if body.captcha_token:`` to
        # decide between hard-gate and soft-pass. A naive grep for
        # exactly that expression catches a copy-paste regression.
        source = self._billing_route_source()
        pattern = re.compile(r"if\s+body\.captcha_token\s*:")
        match = pattern.search(source)
        assert match is None, (
            "Found an ``if body.captcha_token:`` guard in "
            "app/api/v1/billing.py -- the Commit-8 soft-pass branch "
            "has been reintroduced. Captcha verification must be "
            "unconditional in Commit 9."
        )


# ---------------------------------------------------------------------
# 3. AST pin: verify_captcha is called unconditionally in the route
# ---------------------------------------------------------------------

class TestHardGateOrdering:
    """Walk the ``signup_free`` async function with ``ast`` and
    assert that the ``verify_captcha`` call sits inside a top-level
    ``try`` block of the function body -- NOT inside any ``If``
    statement. This catches a regression where someone wraps the
    verification call in a conditional (the Commit-8 shape).
    """

    def _signup_free_func_ast(self) -> ast.AsyncFunctionDef:
        # As in TestSoftPassRemoved, read source from disk and parse
        # with ``ast`` -- no module import required, so the test is
        # sandbox-runnable (no psycopg dependency).
        assert BILLING_ROUTE_SOURCE.exists(), (
            f"Expected billing route at {BILLING_ROUTE_SOURCE}"
        )
        source = BILLING_ROUTE_SOURCE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.AsyncFunctionDef)
                and node.name == "signup_free"
            ):
                return node
        pytest.fail(
            "Could not find async def signup_free in "
            "app/api/v1/billing.py source -- route renamed?"
        )

    def _find_verify_captcha_calls(
        self, node: ast.AST,
    ) -> list[ast.Call]:
        out: list[ast.Call] = []
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                func = sub.func
                if (
                    isinstance(func, ast.Name)
                    and func.id == "verify_captcha"
                ):
                    out.append(sub)
                elif (
                    isinstance(func, ast.Attribute)
                    and func.attr == "verify_captcha"
                ):
                    out.append(sub)
        return out

    def test_verify_captcha_called_exactly_once_in_route(self):
        func = self._signup_free_func_ast()
        calls = self._find_verify_captcha_calls(func)
        assert len(calls) == 1, (
            f"Expected exactly one verify_captcha call in the "
            f"signup_free route body, found {len(calls)}. Multiple "
            f"calls suggest a branched (conditional) gate."
        )

    def test_verify_captcha_not_inside_if_statement(self):
        # Walk the function body and locate the verify_captcha call.
        # Then check none of its ancestors (within the function) is
        # an ``If`` node. We do this by recording (node, parent)
        # pairs via a small visitor.
        func = self._signup_free_func_ast()
        parents: dict[int, ast.AST] = {}

        def assign_parents(parent: ast.AST) -> None:
            for child in ast.iter_child_nodes(parent):
                parents[id(child)] = parent
                assign_parents(child)

        assign_parents(func)

        target_calls = self._find_verify_captcha_calls(func)
        assert target_calls, "verify_captcha call must exist in route"
        call = target_calls[0]

        # Walk up the parent chain from the call to the function
        # node. None of the ancestors may be ast.If.
        current: ast.AST | None = parents.get(id(call))
        ancestors: list[ast.AST] = []
        while current is not None and current is not func:
            ancestors.append(current)
            current = parents.get(id(current))

        if_ancestors = [a for a in ancestors if isinstance(a, ast.If)]
        assert not if_ancestors, (
            "verify_captcha must be called UNCONDITIONALLY in the "
            "signup_free route in Arc 6 Commit 9. Found it nested "
            f"inside {len(if_ancestors)} If-statement ancestor(s) -- "
            "the Commit-8 soft-pass branch has been reintroduced."
        )

    def test_verify_captcha_is_inside_a_try_block(self):
        # The hard-gate path is: try: verify_captcha(...) except
        # CaptchaNotConfiguredError / CaptchaInvalidError. Pin that
        # the call sits inside a try block so the except handlers
        # remain wired.
        func = self._signup_free_func_ast()
        parents: dict[int, ast.AST] = {}

        def assign_parents(parent: ast.AST) -> None:
            for child in ast.iter_child_nodes(parent):
                parents[id(child)] = parent
                assign_parents(child)

        assign_parents(func)

        call = self._find_verify_captcha_calls(func)[0]
        current: ast.AST | None = parents.get(id(call))
        saw_try = False
        while current is not None and current is not func:
            if isinstance(current, ast.Try):
                saw_try = True
                break
            current = parents.get(id(current))
        assert saw_try, (
            "verify_captcha must be wrapped in a try/except block "
            "so CaptchaNotConfiguredError -> 501 and "
            "CaptchaInvalidError -> 422 handlers remain wired."
        )
