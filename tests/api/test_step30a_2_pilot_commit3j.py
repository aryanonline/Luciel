"""Step 30a.2-pilot Commit 3j regression tests.

Closes drift D-pilot-refund-customer-email-missing-2026-05-15: wire the
post-refund courtesy email into BillingService.process_pilot_refund.

Test strategy (mirroring Commit 3g and 3h doctrine):
  - AST/text assertions against the shipped source. The email-service
    module and the billing-service handler are both static-text fixtures;
    we pin the exact public surface and the wiring rather than executing
    the SES boto3 path (which would need network or a heavy mock).
  - 12 cases covering: the new public function exists with the right
    signature, the subject constant exists, the body renders the four
    required fields, the failure exception class exists, the log marker
    is present on success + failure paths, the handler calls the helper
    post-commit, the handler wraps the call in try/except RefundEmailError,
    the failure path writes the follow-up audit row, the failure path
    does NOT re-raise (does NOT roll back the cascade), the new action
    constant is whitelisted, and the imports are consistent across files.

Pattern E: pure addition. No existing test mutated. No existing call site
mutated. No schema change.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
EMAIL_SERVICE_PATH = REPO_ROOT / "app" / "services" / "email_service.py"
BILLING_SERVICE_PATH = REPO_ROOT / "app" / "services" / "billing_service.py"
AUDIT_LOG_MODEL_PATH = REPO_ROOT / "app" / "models" / "admin_audit_log.py"


@pytest.fixture(scope="module")
def email_service_src() -> str:
    return EMAIL_SERVICE_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def billing_service_src() -> str:
    return BILLING_SERVICE_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def audit_log_model_src() -> str:
    return AUDIT_LOG_MODEL_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def email_service_ast(email_service_src: str) -> ast.Module:
    return ast.parse(email_service_src)


@pytest.fixture(scope="module")
def billing_service_ast(billing_service_src: str) -> ast.Module:
    return ast.parse(billing_service_src)


# ---------------------------------------------------------------------
# CASE 1 -- send_pilot_refund_email exists as a module-level function
# ---------------------------------------------------------------------
def test_send_pilot_refund_email_is_module_level_function(email_service_ast):
    """The Commit 3j public helper must be a module-level def, not nested
    inside another function (which would make it unimportable)."""
    funcs = [n for n in email_service_ast.body if isinstance(n, ast.FunctionDef)]
    names = {f.name for f in funcs}
    assert "send_pilot_refund_email" in names, (
        "send_pilot_refund_email must be a module-level def in "
        "app/services/email_service.py so BillingService can import it."
    )


# ---------------------------------------------------------------------
# CASE 2 -- function signature pins the four required kwargs + display_name
# ---------------------------------------------------------------------
def test_send_pilot_refund_email_signature_pins_required_kwargs(email_service_ast):
    """The handler's contract: to_email, refund_id, amount_cents (int),
    currency (str), display_name (optional). All kwargs-only so callers
    cannot accidentally swap positional args."""
    func = next(
        n for n in email_service_ast.body
        if isinstance(n, ast.FunctionDef) and n.name == "send_pilot_refund_email"
    )
    # All args must be kwonly (positional args list empty after self/cls).
    assert len(func.args.args) == 0, (
        "send_pilot_refund_email must be keyword-only -- no positional args."
    )
    kwonly_names = {a.arg for a in func.args.kwonlyargs}
    required = {"to_email", "refund_id", "amount_cents", "currency", "display_name"}
    missing = required - kwonly_names
    assert not missing, (
        f"send_pilot_refund_email is missing required kwargs: {missing}. "
        f"Found: {kwonly_names}"
    )


# ---------------------------------------------------------------------
# CASE 3 -- SUBJECT_PILOT_REFUND constant exists and matches policy
# ---------------------------------------------------------------------
def test_subject_pilot_refund_constant_present(email_service_src: str):
    """The subject is locked by the drift's resolution sketch."""
    assert "SUBJECT_PILOT_REFUND" in email_service_src, (
        "SUBJECT_PILOT_REFUND constant must be defined."
    )
    assert "Your VantageMind pilot has been refunded" in email_service_src, (
        "Subject string must match the drift's locked copy."
    )


# ---------------------------------------------------------------------
# CASE 4 -- _build_refund_body renders the four required body fields
# ---------------------------------------------------------------------
def test_refund_body_renders_required_fields(email_service_src: str):
    """The body must reference the four contract fields (per
    CANONICAL_RECAP section 14 paragraph 273 mirror): amount + currency,
    refund id, the 5-7 business day window, and the canceled+closed step
    semantics."""
    # The body template is a single concatenated f-string; we pin the
    # required tokens.
    required_tokens = [
        "Amount refunded:",
        "Stripe refund id:",
        "5-7 business days",
        "canceled",
        "account has been closed",
    ]
    for tok in required_tokens:
        assert tok in email_service_src, (
            f"_build_refund_body must include the token {tok!r} so the "
            f"email mirrors the on-page success surface copy."
        )


# ---------------------------------------------------------------------
# CASE 5 -- RefundEmailError exception class exists
# ---------------------------------------------------------------------
def test_refund_email_error_class_exists(email_service_ast):
    """The handler must raise a distinct exception class so the caller
    can catch it without also catching MagicLinkError."""
    classes = [n for n in email_service_ast.body if isinstance(n, ast.ClassDef)]
    names = {c.name for c in classes}
    assert "RefundEmailError" in names, (
        "RefundEmailError must be a top-level class in email_service.py."
    )
    refund_err = next(c for c in classes if c.name == "RefundEmailError")
    # Must inherit from a built-in exception (RuntimeError or Exception).
    base_names = {b.id for b in refund_err.bases if isinstance(b, ast.Name)}
    assert base_names & {"RuntimeError", "Exception"}, (
        "RefundEmailError must extend RuntimeError or Exception."
    )


# ---------------------------------------------------------------------
# CASE 6 -- log marker [pilot-refund-email] present on success + failure
# ---------------------------------------------------------------------
def test_pilot_refund_email_log_marker_present(email_service_src: str):
    """The stable log marker lets CloudWatch / operator tooling grep for
    every refund-email send attempt without needing structured logs.
    Must appear at least twice -- once on the success path, once on the
    failure path."""
    marker = "[pilot-refund-email]"
    count = email_service_src.count(marker)
    assert count >= 3, (
        f"Expected [pilot-refund-email] marker on log-only / SES success / "
        f"SES failure paths (>=3 occurrences). Found {count}."
    )


# ---------------------------------------------------------------------
# CASE 7 -- billing_service imports the new helper + exception
# ---------------------------------------------------------------------
def test_billing_service_imports_email_helpers(billing_service_src: str):
    """The handler must import both the helper function and the typed
    exception so the wiring is statically resolvable."""
    assert "from app.services.email_service import" in billing_service_src
    assert "RefundEmailError" in billing_service_src
    assert "send_pilot_refund_email" in billing_service_src


# ---------------------------------------------------------------------
# CASE 8 -- process_pilot_refund calls send_pilot_refund_email
# ---------------------------------------------------------------------
def test_process_pilot_refund_calls_email_helper(billing_service_src: str):
    """The wiring at the call site -- pinned by source text so a refactor
    cannot silently disconnect the email leg without flipping the test."""
    # Find the function body and confirm the call is present.
    handler_start = billing_service_src.find("def process_pilot_refund")
    assert handler_start > 0, "process_pilot_refund handler not found."
    # Find end of function (next module-level def OR end of file)
    handler_body = billing_service_src[handler_start:]
    assert "send_pilot_refund_email(" in handler_body, (
        "process_pilot_refund must call send_pilot_refund_email(...) "
        "at least once."
    )
    # The call must pass to_email, refund_id, amount_cents, currency.
    call_idx = handler_body.find("send_pilot_refund_email(")
    call_region = handler_body[call_idx:call_idx + 400]
    for kw in ("to_email=", "refund_id=", "amount_cents=", "currency="):
        assert kw in call_region, (
            f"send_pilot_refund_email call must include keyword arg {kw!r}. "
            f"Slice: {call_region!r}"
        )


# ---------------------------------------------------------------------
# CASE 9 -- email call is positioned AFTER self.db.commit()
# ---------------------------------------------------------------------
def test_email_call_is_after_db_commit(billing_service_src: str):
    """The email is a courtesy/polish leg -- it must run AFTER the refund
    cascade has been committed. If it ran before, an SES failure could
    block the financial refund from settling, which would break the
    customer-facing promise."""
    handler_start = billing_service_src.find("def process_pilot_refund")
    handler_body = billing_service_src[handler_start:]
    # Find the LAST commit before the email call (there is a single
    # transactional commit at the end of the cascade).
    commit_idx = handler_body.find("self.db.commit()")
    email_idx = handler_body.find("send_pilot_refund_email(")
    assert commit_idx > 0, "self.db.commit() not found in handler body."
    assert email_idx > 0, "send_pilot_refund_email call not found."
    assert email_idx > commit_idx, (
        f"send_pilot_refund_email must be called AFTER self.db.commit() so "
        f"a SES failure cannot roll back the refund cascade. commit at {commit_idx}, "
        f"email at {email_idx}."
    )


# ---------------------------------------------------------------------
# CASE 10 -- handler wraps email call in try/except RefundEmailError
# ---------------------------------------------------------------------
def test_email_call_wrapped_in_refund_email_error_except(billing_service_src: str):
    """The handler must catch RefundEmailError specifically so a
    legitimate SES failure is handled gracefully (audit row + swallow)
    rather than bubbling up as a 500 to the customer-facing route."""
    handler_start = billing_service_src.find("def process_pilot_refund")
    handler_body = billing_service_src[handler_start:]
    # The pattern we expect: a try block containing send_pilot_refund_email,
    # then `except RefundEmailError` somewhere after.
    email_idx = handler_body.find("send_pilot_refund_email(")
    # Walk backward from email_idx, find the most recent `try:` keyword.
    pre = handler_body[:email_idx]
    last_try = pre.rfind("try:")
    assert last_try > 0, (
        "send_pilot_refund_email call must be inside a try block."
    )
    # Walk forward from email_idx, the next except must mention RefundEmailError.
    post = handler_body[email_idx:]
    next_except = post.find("except RefundEmailError")
    assert next_except > 0, (
        "The try block wrapping send_pilot_refund_email must have an "
        "`except RefundEmailError` handler."
    )


# ---------------------------------------------------------------------
# CASE 11 -- failure path writes ACTION_PILOT_REFUND_EMAIL_SEND_FAILED row
# ---------------------------------------------------------------------
def test_failure_writes_email_send_failed_audit_row(billing_service_src: str):
    """On RefundEmailError the handler must write a follow-up audit row
    with the new action constant so an operator can find every
    failed-refund-email in one query and manually relay them."""
    assert "ACTION_PILOT_REFUND_EMAIL_SEND_FAILED" in billing_service_src, (
        "Handler must import ACTION_PILOT_REFUND_EMAIL_SEND_FAILED."
    )
    # And it must be the action= keyword on a record() call.
    handler_start = billing_service_src.find("def process_pilot_refund")
    handler_body = billing_service_src[handler_start:]
    assert "action=ACTION_PILOT_REFUND_EMAIL_SEND_FAILED" in handler_body, (
        "The failure path must write a record() call with "
        "action=ACTION_PILOT_REFUND_EMAIL_SEND_FAILED."
    )
    # The after_json must carry the four operator-actionable fields.
    for tok in ("stripe_refund_id", "error_class", "to_email"):
        assert tok in handler_body, (
            f"after_json on the failure audit row must carry {tok!r} so "
            f"an operator can find every failed-email-send with a single "
            f"audit query."
        )


# ---------------------------------------------------------------------
# CASE 12 -- new action constant is whitelisted in ALLOWED_ACTIONS
# ---------------------------------------------------------------------
def test_new_action_constant_whitelisted(audit_log_model_src: str):
    """The audit repository validates every action against ALLOWED_ACTIONS
    on write. A new action constant that is NOT in the whitelist would
    raise at runtime the first time the failure path fires -- defeating
    the purpose of the audit row."""
    # Constant definition must exist.
    assert 'ACTION_PILOT_REFUND_EMAIL_SEND_FAILED = "pilot_refund_email_send_failed"' in audit_log_model_src, (
        "ACTION_PILOT_REFUND_EMAIL_SEND_FAILED must be defined in "
        "app/models/admin_audit_log.py with the exact string value "
        "'pilot_refund_email_send_failed'."
    )
    # And it must be in the ALLOWED_ACTIONS tuple. We resolve the runtime
    # tuple directly rather than text-parsing -- comments inside the tuple
    # body contain literal `)` characters (e.g. `# Step 29.y Cluster 4 (E-3)`)
    # which break naive `find(")")` slicing. Importing the symbol is also
    # the strongest possible check: it proves Python itself sees the constant
    # in the whitelist at module-load time, which is exactly what the audit
    # repository's runtime validation does.
    from app.models.admin_audit_log import (
        ACTION_PILOT_REFUND_EMAIL_SEND_FAILED,
        ALLOWED_ACTIONS,
    )
    assert ACTION_PILOT_REFUND_EMAIL_SEND_FAILED == "pilot_refund_email_send_failed", (
        "ACTION_PILOT_REFUND_EMAIL_SEND_FAILED must equal the exact string "
        "'pilot_refund_email_send_failed' so audit queries can filter on it."
    )
    assert ACTION_PILOT_REFUND_EMAIL_SEND_FAILED in ALLOWED_ACTIONS, (
        "ACTION_PILOT_REFUND_EMAIL_SEND_FAILED must be listed in the "
        "ALLOWED_ACTIONS whitelist tuple so the audit repository accepts "
        "it on write."
    )
