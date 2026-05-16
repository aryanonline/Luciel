"""
Regression guard for D-cookie-middleware-billingservice-missing-stripe-client-2026-05-16.

The bug
=======
``app/middleware/session_cookie_auth.py`` constructed ``BillingService(db)``
with only one positional arg, but ``BillingService.__init__`` requires
``(db, stripe_client)``. Every cookied request to ``/api/v1/admin/*`` and
``/api/v1/dashboard/*`` raised ``TypeError`` inside the middleware's
``try`` block, got swallowed by the broad ``except Exception``, logged
an ERROR, and fell through to ``ApiKeyAuthMiddleware`` which returned
its literal ``"Missing or invalid Authorization header. Use Bearer
<api_key>"`` 401 to the browser.

Why this slipped past existing tests
====================================
``app/verification/`` has zero coverage of ``SessionCookieAuthMiddleware``
-- it's a pure-HTTP suite without access to ``magic_link_secret`` and
cannot mint session cookies. The bug surfaced only when the cookied
browser flow was exercised live on prod during the T9 closure test.

What this guard pins
====================
The ``BillingService(...)`` call site in ``session_cookie_auth.py`` must
pass TWO positional args, matching ``BillingService.__init__(db,
stripe_client)``. We pin this at AST level (house style; same pattern
as ``tests/api/test_embed_key_preflight.py``) so any future refactor
that drops the second arg fails this test immediately rather than
silently 401-ing every cookied browser request again.

We also pin that ``get_stripe_client`` is imported in the module --
the only sanctioned way to obtain the singleton client. A regression
that swaps it for ``StripeClient()`` (which would need real config to
construct) would also break the cookied flow at boot, so this import
pin defends against that variant.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest


MIDDLEWARE_PATH = (
    Path(__file__).resolve().parents[2]
    / "app"
    / "middleware"
    / "session_cookie_auth.py"
)


def _load_module_ast() -> ast.Module:
    src = MIDDLEWARE_PATH.read_text(encoding="utf-8")
    return ast.parse(src, filename=str(MIDDLEWARE_PATH))


def _all_calls_to(tree: ast.AST, name: str) -> list[ast.Call]:
    """Return every ``Call`` node whose func is the identifier ``name``."""
    matches: list[ast.Call] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name:
            matches.append(node)
    return matches


def test_billingservice_called_with_two_args() -> None:
    """The fix: ``BillingService(db, stripe_client)``, never just ``BillingService(db)``.

    Regression: a one-arg call falls through silently at runtime because
    the broad ``except Exception`` on the middleware catches the
    resulting ``TypeError`` and logs ERROR rather than returning 500.
    The browser sees ApiKeyAuthMiddleware's "Missing Bearer" 401 with
    no actionable signal in the response body.
    """
    tree = _load_module_ast()
    calls = _all_calls_to(tree, "BillingService")
    assert calls, (
        f"Expected at least one BillingService(...) call in {MIDDLEWARE_PATH}, "
        f"found zero. If this middleware no longer needs BillingService, "
        f"remove this test in the same commit."
    )
    for call in calls:
        total_args = len(call.args) + len(call.keywords)
        assert total_args >= 2, (
            f"BillingService(...) call at line {call.lineno} in "
            f"{MIDDLEWARE_PATH.name} has only {total_args} arg(s). "
            f"BillingService.__init__ requires (db, stripe_client). "
            f"A one-arg call raises TypeError at runtime; the middleware's "
            f"broad `except Exception` swallows it and 401s every cookied "
            f"dashboard/admin request. See "
            f"D-cookie-middleware-billingservice-missing-stripe-client-2026-05-16."
        )


def test_get_stripe_client_imported() -> None:
    """The sanctioned path: ``from app.integrations.stripe import get_stripe_client``.

    Regression guard against a variant where someone swaps the
    construction to ``StripeClient()`` directly (which would need
    config wiring) or to a stub.
    """
    tree = _load_module_ast()
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "app.integrations.stripe":
                for alias in node.names:
                    imported_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported_names.add(alias.asname or alias.name)
    assert "get_stripe_client" in imported_names, (
        f"{MIDDLEWARE_PATH.name} must import get_stripe_client from "
        f"app.integrations.stripe to satisfy the BillingService(db, "
        f"stripe_client) contract. Found imports: {sorted(imported_names)}."
    )


def test_billingservice_constructor_signature_unchanged() -> None:
    """If BillingService's signature ever changes, this test forces a
    deliberate review of the cookie middleware call site rather than a
    silent runtime TypeError.

    If you legitimately change BillingService.__init__, update both the
    call site in session_cookie_auth.py AND this test in the same commit.
    """
    from app.services.billing_service import BillingService

    sig = inspect.signature(BillingService.__init__)
    params = [p for p in sig.parameters.values() if p.name != "self"]
    required = [
        p for p in params
        if p.default is inspect.Parameter.empty
        and p.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    ]
    assert len(required) == 2, (
        f"BillingService.__init__ now requires {len(required)} positional "
        f"args (was 2). Required params: {[p.name for p in required]}. "
        f"Update app/middleware/session_cookie_auth.py to pass them, then "
        f"update the expected count in this test."
    )
    names = [p.name for p in required]
    assert names == ["db", "stripe_client"], (
        f"BillingService.__init__ required-arg names changed: {names}. "
        f"Update session_cookie_auth.py's call site to match, then update "
        f"the expected names in this test."
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
