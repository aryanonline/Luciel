"""Step 30a.2-pilot Commit 3e — Stripe Event -> dict conversion contract test.

On 2026-05-15 the live GATE 4 Path B smoke produced two webhook 500s with
the traceback:

    File "/app/app/api/v1/billing.py", line 201, in stripe_webhook
        result = BillingWebhookService(db).handle(dict(event))
      File ".../stripe/_stripe_object.py", line 203, in __getitem__
      File ".../stripe/_stripe_object.py", line 224, in __getitem__
    KeyError: 0

Root cause: ``stripe.StripeObject`` overrides ``__getitem__`` to raise
``KeyError`` on missing keys. CPython's ``dict()`` constructor probes
positional index ``0`` during one of its iteration code paths, which
``StripeObject`` correctly rejects -- so ``dict(event)`` raises before
``BillingWebhookService.handle()`` is ever called.

Commit 3e replaces ``dict(event)`` with a version-resilient
materialisation chain: ``json.loads(str(event))`` first (uses the SDK's
own recursive serializer via ``StripeObject.__str__`` -- present since
v1.x), with a ``getattr(event, 'to_dict_recursive', getattr(event,
'_to_dict_recursive', None))`` fallback covering every SDK major from
10.x (public ``to_dict_recursive``) to 15.x (underscored
``_to_dict_recursive``).

This file pins:
  1. ``dict(event)`` is no longer present anywhere in
     ``app/api/v1/billing.py``.
  2. The webhook route imports ``json`` (needed by the primary path).
  3. The materialisation block lives BEFORE the
     ``BillingWebhookService(db).handle(...)`` call and the result of
     ``json.loads(str(event))`` (named ``event_dict``) is what is passed
     into ``handle()``.
  4. The drift marker ``D-stripe-event-dict-conversion-python314-2026-05-15``
     is referenced in source so future maintainers can trace the why.
  5. ``stripe`` is pinned ``<16`` in ``pyproject.toml`` so a future SDK
     major rev cannot silently break this contract again
     (D-stripe-sdk-major-pin-2026-05-15).

This is AST + import only -- no Postgres, no FastAPI runtime, no Stripe
network. End-to-end correctness is covered by tests/e2e/step_30a_live_e2e.py
and the production CloudWatch smoke after Commit 3e is deployed.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
BILLING_ROUTE = REPO_ROOT / "app" / "api" / "v1" / "billing.py"
PYPROJECT = REPO_ROOT / "pyproject.toml"


# ---------------------------------------------------------------------
# Source artifacts
# ---------------------------------------------------------------------

@pytest.fixture(scope="module")
def billing_source() -> str:
    return BILLING_ROUTE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def billing_tree(billing_source: str) -> ast.AST:
    return ast.parse(billing_source, filename=str(BILLING_ROUTE))


@pytest.fixture(scope="module")
def pyproject_source() -> str:
    return PYPROJECT.read_text(encoding="utf-8")


# ---------------------------------------------------------------------
# 1. dict(event) is gone
# ---------------------------------------------------------------------

class TestDictEventCallSiteRemoved:
    """The exact line that raised KeyError(0) in prod must be absent."""

    def test_no_executable_dict_event_text(self, billing_source: str):
        # Loose textual check that ignores comment lines (the drift
        # block intentionally mentions ``dict(event)`` as historical
        # context). We scan only executable / non-``#`` lines for the
        # literal call form.
        offending_lines: list[tuple[int, str]] = []
        for i, line in enumerate(billing_source.splitlines(), start=1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if "dict(event)" in line:
                offending_lines.append((i, line.rstrip()))
        assert not offending_lines, (
            "dict(event) reappeared as executable code in billing.py at "
            f"line(s) {offending_lines}. Step 30a.2-pilot Commit 3e "
            "removed it because StripeObject.__getitem__ raises "
            "KeyError(0) during CPython dict() iteration. Use "
            "json.loads(str(event)) instead. See "
            "D-stripe-event-dict-conversion-python314-2026-05-15."
        )

    def test_no_dict_call_on_event_in_ast(self, billing_tree: ast.AST):
        """AST-level check: no ``dict(event)`` call survives.

        Robust against whitespace / comment variations the textual check
        would miss.
        """
        offenders: list[int] = []
        for node in ast.walk(billing_tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id == "dict":
                if len(node.args) == 1 and isinstance(node.args[0], ast.Name):
                    if node.args[0].id == "event":
                        offenders.append(node.lineno)
        assert not offenders, (
            f"dict(event) AST call(s) still present at line(s) {offenders}. "
            f"Commit 3e removed this -- it raises KeyError(0) on production "
            f"stripe 15.1.0. Use json.loads(str(event))."
        )


# ---------------------------------------------------------------------
# 2. json is imported (primary materialisation path needs it)
# ---------------------------------------------------------------------

class TestJsonImported:
    def test_json_module_imported_at_top(self, billing_tree: ast.AST):
        json_imports = [
            n for n in billing_tree.body
            if isinstance(n, ast.Import) and any(a.name == "json" for a in n.names)
        ]
        assert json_imports, (
            "billing.py must `import json` at module top -- the Commit 3e "
            "primary path is `json.loads(str(event))`."
        )


# ---------------------------------------------------------------------
# 3. The new materialisation block exists and is wired in correctly
# ---------------------------------------------------------------------

class TestMaterialisationBlock:
    """The new conversion lives BEFORE handle(...) and feeds into it."""

    def test_json_loads_str_event_is_present(self, billing_source: str):
        assert "json.loads(str(event))" in billing_source, (
            "Commit 3e primary path missing. Expected `json.loads(str(event))` "
            "to be the version-resilient materialisation call."
        )

    def test_handle_is_called_with_event_dict_not_event(self, billing_source: str):
        # The handle() call must receive the materialised plain dict,
        # not the raw StripeObject.
        assert "BillingWebhookService(db).handle(event_dict)" in billing_source, (
            "BillingWebhookService(db).handle(...) must be called with the "
            "materialised plain `event_dict`, not the raw StripeObject. "
            "Commit 3e renamed the conversion result to `event_dict`."
        )

    def test_fallback_to_dict_recursive_getattr_chain_present(self, billing_source: str):
        # The belt-and-suspenders fallback so this still works if a
        # future SDK breaks __str__ JSON emission.
        assert "to_dict_recursive" in billing_source, (
            "Commit 3e fallback path missing -- must probe both "
            "to_dict_recursive (10.x-12.x public) and _to_dict_recursive "
            "(13.x+ underscored) via getattr."
        )
        assert "_to_dict_recursive" in billing_source, (
            "Commit 3e fallback must explicitly probe the underscored "
            "`_to_dict_recursive` name (current 15.x prod SDK only "
            "exposes the underscored form)."
        )


# ---------------------------------------------------------------------
# 4. Drift markers present in source (traceability for future maintainers)
# ---------------------------------------------------------------------

class TestDriftMarkersPresent:
    def test_event_dict_drift_marker(self, billing_source: str):
        assert "D-stripe-event-dict-conversion-python314-2026-05-15" in billing_source, (
            "Commit 3e must reference the drift marker "
            "`D-stripe-event-dict-conversion-python314-2026-05-15` in the "
            "source comment so future maintainers can trace the why."
        )

    def test_sdk_major_pin_drift_marker_in_pyproject(self, pyproject_source: str):
        assert "D-stripe-sdk-major-pin-2026-05-15" in pyproject_source, (
            "Commit 3e must reference the drift marker "
            "`D-stripe-sdk-major-pin-2026-05-15` in pyproject.toml so the "
            "stripe<16 upper-bound pin is traceable."
        )


# ---------------------------------------------------------------------
# 5. Stripe SDK upper-bound pin
# ---------------------------------------------------------------------

class TestStripeSdkUpperBoundPin:
    """``stripe<16`` is the upper bound -- 13.x renamed the materialiser
    and 16.x might do something even more disruptive. Pin so the next
    image rebuild cannot silently regress this."""

    def test_stripe_pinned_below_16(self, pyproject_source: str):
        # Match both "stripe>=10.0.0,<16" and any reasonable variant.
        import re
        # Look for a stripe dep line containing ``<16`` (allow whitespace).
        match = re.search(
            r'"stripe>=\s*10\.0\.0\s*,\s*<\s*16"',
            pyproject_source,
        )
        assert match, (
            "pyproject.toml must pin stripe with an upper bound of <16. "
            "Expected a dependency line matching "
            '`"stripe>=10.0.0,<16"`. Got pyproject content without it.'
        )


# ---------------------------------------------------------------------
# 6. The materialisation block precedes the handle() invocation
# ---------------------------------------------------------------------

class TestMaterialisationOrder:
    """Order matters: convert FIRST, THEN call handle()."""

    def test_json_loads_appears_before_handle_call(self, billing_source: str):
        json_pos = billing_source.find("json.loads(str(event))")
        handle_pos = billing_source.find("BillingWebhookService(db).handle(event_dict)")
        assert json_pos > 0, "json.loads(str(event)) missing"
        assert handle_pos > 0, "handle(event_dict) missing"
        assert json_pos < handle_pos, (
            "json.loads(str(event)) must appear BEFORE "
            "BillingWebhookService(db).handle(event_dict) in source order. "
            "The materialised dict must exist before handle() reads it."
        )
