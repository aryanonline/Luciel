"""Arc 10 regression tests -- POST /admin/account/close route surface.

Protects the end-to-end customer-facing closure contract:

  * Route exists at the expected path with the expected verb.
  * Request schema requires cancel_mode in {'immediate', 'period_end'}.
  * Request schema requires confirm_account_name (typed confirmation).
  * Route returns AccountCloseResponse with grace_window_expires_at.
  * Route maps the four typed service errors to the right HTTP codes:
      InvalidConfirmationError       -> 400
      AccountAlreadyClosedError      -> 409
      AccountAlreadyTombstoneError   -> 410
      ExportAlreadyInFlightError     -> n/a (export is best-effort,
                                       does not block closure)

Test strategy: AST / text assertions against the shipped route file
and the lifecycle schema module. Integration tests that exercise the
live route through FastAPI's TestClient land in a follow-up coverage
PR.
"""
from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
ADMIN_ROUTES_PATH = REPO_ROOT / "app" / "api" / "v1" / "admin" / "__init__.py"
LIFECYCLE_SCHEMA_PATH = REPO_ROOT / "app" / "schemas" / "lifecycle.py"
CLOSURE_SERVICE_PATH = REPO_ROOT / "app" / "lifecycle" / "closure.py"


# ---------------------------------------------------------------------
# Route registration.
# ---------------------------------------------------------------------

def test_close_account_route_is_registered():
    """POST /account/close exists at the admin router level."""
    src = ADMIN_ROUTES_PATH.read_text(encoding="utf-8")
    # The router prefix is /admin (defined at the top of the file)
    # so the decorator path is "/account/close".
    assert '"/account/close"' in src, (
        "Admin router must register POST /account/close."
    )


def test_close_account_route_uses_correct_response_model():
    """The decorator declares AccountCloseResponse as response_model."""
    src = ADMIN_ROUTES_PATH.read_text(encoding="utf-8")
    # The decorator block must mention both the path and the response.
    assert "AccountCloseResponse" in src, (
        "close_account route must declare response_model=AccountCloseResponse."
    )


def test_close_account_route_handler_signature():
    """def close_account exists with the expected (request, body, db) signature."""
    src = ADMIN_ROUTES_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "close_account":
            fn = node
            break
    assert fn is not None, "close_account function not found in admin.py"

    arg_names = [a.arg for a in fn.args.args]
    assert "request" in arg_names, "close_account must take a request param"
    assert "body" in arg_names, (
        "close_account must take a body param (AccountCloseRequest)"
    )
    assert "db" in arg_names, "close_account must take a db dependency"


# ---------------------------------------------------------------------
# Request/response schemas.
# ---------------------------------------------------------------------

def test_account_close_request_schema_requires_cancel_mode_literal():
    """cancel_mode must be a Literal['immediate', 'period_end']."""
    src = LIFECYCLE_SCHEMA_PATH.read_text(encoding="utf-8")
    assert "class AccountCloseRequest" in src, (
        "AccountCloseRequest schema must exist in lifecycle schemas."
    )
    # The Literal constraint is the doctrine: the route cannot accept
    # arbitrary cancel-mode strings.
    assert "Literal" in src, (
        "lifecycle schemas must import typing.Literal for the "
        "cancel_mode constraint."
    )
    assert '"immediate"' in src and '"period_end"' in src, (
        "AccountCloseRequest.cancel_mode must be "
        "Literal['immediate', 'period_end']."
    )


def test_account_close_request_requires_confirm_account_name():
    """The typed-confirmation guard must be a required field."""
    src = LIFECYCLE_SCHEMA_PATH.read_text(encoding="utf-8")
    assert "confirm_account_name" in src, (
        "AccountCloseRequest must require confirm_account_name."
    )


def test_account_close_request_has_request_export_flag():
    """The closure modal's 'Download my data' checkbox is request_export."""
    src = LIFECYCLE_SCHEMA_PATH.read_text(encoding="utf-8")
    assert "request_export" in src, (
        "AccountCloseRequest must carry request_export bool for the "
        "pre-closure data export trigger."
    )


def test_account_close_response_carries_grace_window():
    """Response must include grace_window_expires_at so the UI can render."""
    src = LIFECYCLE_SCHEMA_PATH.read_text(encoding="utf-8")
    assert "class AccountCloseResponse" in src
    assert "grace_window_expires_at" in src, (
        "AccountCloseResponse must include grace_window_expires_at "
        "so the frontend can render the deletion-date countdown."
    )
    assert "closure_initiated_at" in src, (
        "AccountCloseResponse must include closure_initiated_at."
    )


# ---------------------------------------------------------------------
# Error -> HTTP code mapping.
# ---------------------------------------------------------------------

def test_close_account_maps_invalid_confirmation_to_400():
    """InvalidConfirmationError -> HTTP 400."""
    src = ADMIN_ROUTES_PATH.read_text(encoding="utf-8")
    # Find the close_account function body via AST.
    fn_src = _function_source(src, "close_account")
    # The handler must mention HTTP_400_BAD_REQUEST in proximity to
    # InvalidConfirmationError. Simplest check: both substrings
    # present in the function body.
    assert "InvalidConfirmationError" in fn_src, (
        "close_account must catch InvalidConfirmationError."
    )
    assert "HTTP_400_BAD_REQUEST" in fn_src, (
        "close_account must map InvalidConfirmationError to HTTP 400."
    )


def test_close_account_maps_already_closed_to_409():
    """AccountAlreadyClosedError -> HTTP 409."""
    src = ADMIN_ROUTES_PATH.read_text(encoding="utf-8")
    fn_src = _function_source(src, "close_account")
    assert "AccountAlreadyClosedError" in fn_src
    assert "HTTP_409_CONFLICT" in fn_src


def test_close_account_maps_tombstone_to_410():
    """AccountAlreadyTombstoneError -> HTTP 410."""
    src = ADMIN_ROUTES_PATH.read_text(encoding="utf-8")
    fn_src = _function_source(src, "close_account")
    assert "AccountAlreadyTombstoneError" in fn_src
    assert "HTTP_410_GONE" in fn_src


# ---------------------------------------------------------------------
# ClosureService doctrine surface.
# ---------------------------------------------------------------------

def test_closure_service_default_grace_window_is_30_days():
    """ClosureService imports GRACE_WINDOW_DAYS = 30."""
    src = CLOSURE_SERVICE_PATH.read_text(encoding="utf-8")
    assert "GRACE_WINDOW_DAYS = 30" in src, (
        "ClosureService must declare GRACE_WINDOW_DAYS = 30 per "
        "Arc 10 L1 (matches the retention worker)."
    )


def test_closure_service_stamps_closure_initiated_at_not_just_deactivated_at():
    """initiate_closure stamps closure_initiated_at, not only the legacy column.

    Closure must START the grace clock specifically; setting only
    deactivated_at (the cascade's legacy stamp) would leave the
    retention worker unable to ever fire hard-delete on the row.
    """
    src = CLOSURE_SERVICE_PATH.read_text(encoding="utf-8")
    assert "admin.closure_initiated_at = now" in src, (
        "ClosureService.initiate_closure must stamp closure_initiated_at."
    )


def test_closure_service_requires_typed_confirmation():
    """Empty / mis-typed confirm_account_name must raise InvalidConfirmationError."""
    src = CLOSURE_SERVICE_PATH.read_text(encoding="utf-8")
    # Casefold comparison is the doctrine -- trivial whitespace
    # differences shouldn't block, but the name must match.
    assert "casefold" in src, (
        "ClosureService must compare confirm_account_name with "
        "casefold normalization."
    )
    assert "InvalidConfirmationError" in src, (
        "ClosureService must raise InvalidConfirmationError on mismatch."
    )


# ---------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------

def _function_source(src: str, name: str) -> str:
    """Return the source segment of the named function."""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            seg = ast.get_source_segment(src, node)
            assert seg is not None, f"could not extract source for {name}"
            return seg
    raise AssertionError(f"function {name} not found")
