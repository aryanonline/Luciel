"""Arc 8 WU-6 Phase A regression tests -- email_service.py precheck + headers.

Closes (at the test layer) the four WU-6 wiring contracts:
  1. SuppressedRecipientError is imported into email_service.py from
     the suppression service.
  2. Each of the three send functions (magic-link / welcome-set-password
     / pilot-refund) calls _precheck_suppression BEFORE its transport
     branch.
  3. Each of the three SES send_email call sites carries
     ConfigurationSetName + ReplyToAddresses, sourced from the new
     settings.ses_configuration_set_name / settings.ses_reply_to_address.
  4. The settings module declares both slots with the documented defaults
     ('luciel-default' and 'support@vantagemind.ai').

These four contracts are the load-bearing precondition for the WU-6
Phase B prod-touch ceremony (SNS topic + SES configuration set +
event destination) to be visible on the wire: without
ConfigurationSetName on the outbound send, the feedback events never
reach the SNS topic regardless of what AWS-side state exists.

Test strategy: AST/text assertions against the shipped sources. The
three send functions are static-text fixtures; we pin the precheck
call line, the SES kwarg names, and the settings attribute lookups.

Pattern E: pure addition. No existing tests mutated. No call-site
mutation outside the WU-6 wiring this file pins.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
EMAIL_SERVICE_PATH = REPO_ROOT / "app" / "services" / "email_service.py"
SETTINGS_PATH = REPO_ROOT / "app" / "core" / "config.py"


@pytest.fixture(scope="module")
def email_service_src() -> str:
    return EMAIL_SERVICE_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def settings_src() -> str:
    return SETTINGS_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def email_service_ast(email_service_src: str) -> ast.Module:
    return ast.parse(email_service_src)


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found in email_service.py")


def _function_calls_helper(fn: ast.FunctionDef, helper_name: str) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == helper_name:
                return True
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == helper_name
            ):
                return True
    return False


def _find_send_email_call_kwargs(fn: ast.FunctionDef) -> set[str]:
    """Return the set of kwarg names passed to any ``client.send_email``
    call inside ``fn``. There is exactly one such call per send
    function, so the returned set is the full kwarg set for that
    function's SES call.
    """
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "send_email"
        ):
            return {kw.arg for kw in node.keywords if kw.arg is not None}
    raise AssertionError(
        f"no client.send_email call found in {fn.name!r}; the WU-6 wiring "
        f"test assumes one SES call site per send function."
    )


# ---------------------------------------------------------------------
# CASE 1 -- SuppressedRecipientError is imported into email_service.py
# ---------------------------------------------------------------------
def test_suppressed_recipient_error_imported(email_service_src):
    """email_service.py must import SuppressedRecipientError so the
    precheck helper can raise it without depending on a deeper
    module-resolution path.
    """
    assert "SuppressedRecipientError" in email_service_src, (
        "SuppressedRecipientError must be referenced (imported) in "
        "app/services/email_service.py for the precheck wiring."
    )
    assert (
        "from app.services.email_suppression_service import" in email_service_src
    ), (
        "email_service.py must import from email_suppression_service "
        "(direct import, not a transitive one)."
    )


# ---------------------------------------------------------------------
# CASE 2 -- _precheck_suppression helper exists at module scope
# ---------------------------------------------------------------------
def test_precheck_suppression_helper_exists(email_service_ast):
    funcs = {n.name for n in email_service_ast.body if isinstance(n, ast.FunctionDef)}
    assert "_precheck_suppression" in funcs, (
        "_precheck_suppression must be a module-level helper that "
        "wraps the EmailSuppressionService.is_suppressed lookup with "
        "the fail-open exception policy."
    )


# ---------------------------------------------------------------------
# CASE 3-5 -- each send function calls _precheck_suppression
# ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "send_fn",
    [
        "send_magic_link_email",
        "send_welcome_set_password_email",
        "send_pilot_refund_email",
    ],
)
def test_send_function_calls_precheck(email_service_ast, send_fn):
    fn = _find_function(email_service_ast, send_fn)
    assert _function_calls_helper(fn, "_precheck_suppression"), (
        f"{send_fn} must call _precheck_suppression BEFORE its transport "
        f"branch so the SES API call is skipped for suppressed addresses."
    )


# ---------------------------------------------------------------------
# CASE 6-8 -- each send function accepts an optional `db` Session kwarg
# ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "send_fn",
    [
        "send_magic_link_email",
        "send_welcome_set_password_email",
        "send_pilot_refund_email",
    ],
)
def test_send_function_accepts_optional_db(email_service_ast, send_fn):
    fn = _find_function(email_service_ast, send_fn)
    # ``db`` lives in the kwonlyargs because the existing signatures
    # are keyword-only (the ``*,`` separator is the first arg slot).
    kwonly_names = {a.arg for a in fn.args.kwonlyargs}
    assert "db" in kwonly_names, (
        f"{send_fn} must accept an optional keyword-only ``db`` "
        f"Session so callers that already hold a session can pass it "
        f"through to the precheck (avoiding a redundant SessionLocal "
        f"open)."
    )


# ---------------------------------------------------------------------
# CASE 9-11 -- each SES send_email call carries ConfigurationSetName
# ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "send_fn",
    [
        "send_magic_link_email",
        "send_welcome_set_password_email",
        "send_pilot_refund_email",
    ],
)
def test_send_email_passes_configuration_set_name(email_service_ast, send_fn):
    fn = _find_function(email_service_ast, send_fn)
    kwargs = _find_send_email_call_kwargs(fn)
    assert "ConfigurationSetName" in kwargs, (
        f"{send_fn}'s client.send_email call must pass "
        f"ConfigurationSetName so SES routes Bounce / Complaint / "
        f"Reject / RenderingFailure events through the configuration "
        f"set's event destination to the SNS topic. Without it the "
        f"feedback loop is dormant regardless of AWS-side state."
    )


# ---------------------------------------------------------------------
# CASE 12-14 -- each SES send_email call carries ReplyToAddresses
# ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "send_fn",
    [
        "send_magic_link_email",
        "send_welcome_set_password_email",
        "send_pilot_refund_email",
    ],
)
def test_send_email_passes_reply_to_addresses(email_service_ast, send_fn):
    fn = _find_function(email_service_ast, send_fn)
    kwargs = _find_send_email_call_kwargs(fn)
    assert "ReplyToAddresses" in kwargs, (
        f"{send_fn}'s client.send_email call must pass ReplyToAddresses "
        f"so buyer replies are routed into the monitored support inbox "
        f"instead of the unmonitored noreply mailbox. This is a "
        f"deliverability signal AWS evaluates during sandbox-exit "
        f"review."
    )


# ---------------------------------------------------------------------
# CASE 15-17 -- the SES kwargs reference settings.* not literal strings
# ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "send_fn",
    [
        "send_magic_link_email",
        "send_welcome_set_password_email",
        "send_pilot_refund_email",
    ],
)
def test_ses_kwargs_reference_settings(email_service_src, send_fn):
    """The ConfigurationSetName and Reply-To addresses must come from
    settings so an operator can override them via env / SSM without a
    code change. A literal string in the call site would be a
    re-introduction of the configuration-pinning anti-pattern Step
    28 explicitly rejected.
    """
    # The simpler text-level assertion is sufficient -- we just need to
    # confirm the literal kwargs are wired to settings attributes.
    assert "settings.ses_configuration_set_name" in email_service_src, (
        "ConfigurationSetName must read from settings."
        "ses_configuration_set_name, not a literal string."
    )
    assert "settings.ses_reply_to_address" in email_service_src, (
        "ReplyToAddresses must read from settings."
        "ses_reply_to_address, not a literal string."
    )


# ---------------------------------------------------------------------
# CASE 18 -- settings module declares both slots with documented defaults
# ---------------------------------------------------------------------
def test_settings_declares_ses_configuration_set_name(settings_src):
    assert 'ses_configuration_set_name: str = "luciel-default"' in settings_src, (
        "Settings.ses_configuration_set_name must default to "
        "'luciel-default' (matches the configuration set name the "
        "Phase B prod-touch ceremony creates in the SES console)."
    )


def test_settings_declares_ses_reply_to_address(settings_src):
    assert 'ses_reply_to_address: str = "support@vantagemind.ai"' in settings_src, (
        "Settings.ses_reply_to_address must default to "
        "'support@vantagemind.ai' (the monitored support inbox; "
        "closes D-ses-reply-to-monitored-inbox-not-confirmed)."
    )


# ---------------------------------------------------------------------
# CASE 19 -- precheck happens BEFORE the transport branch
# ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "send_fn",
    [
        "send_magic_link_email",
        "send_welcome_set_password_email",
        "send_pilot_refund_email",
    ],
)
def test_precheck_runs_before_transport(email_service_ast, send_fn):
    """The precheck must execute before _transport() is consulted.
    Otherwise the log-only transport would happily 'send' to a
    suppressed address, masking bugs in the precheck wiring during
    local dev.
    """
    fn = _find_function(email_service_ast, send_fn)
    seen_precheck = False
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "_precheck_suppression":
                seen_precheck = True
            elif node.func.id == "_transport":
                assert seen_precheck, (
                    f"{send_fn} must call _precheck_suppression BEFORE "
                    f"_transport(). Otherwise the log-only branch would "
                    f"bypass the suppression list, masking bugs."
                )
                return
    # If neither was found, the earlier tests will fail with a clearer
    # message; don't double-report here.
