"""Arc 8 WU-6 Phase A regression tests -- EmailSuppressionService shape.

Closes (at the test layer) the WU-6 cohort drifts:
  * D-ses-feedback-loop-not-wired-2026-05-22
  * D-ses-suppression-app-layer-not-implemented-2026-05-22

Test strategy (mirroring the Step 30a.2-pilot Commit 3j doctrine):
  - AST/text assertions against the shipped source. The service module,
    the model module, the migration module, and the audit-log constants
    module are all static-text fixtures; we pin the exact public surface
    and the wiring rather than executing a live DB path (which would
    need a Postgres fixture and the Alembic chain applied).
  - Cases cover: SuppressedRecipientError exists; is_suppressed +
    record_suppression + clear_suppression are module-level callables
    with the right signatures; the three audit-action constants are
    whitelisted in ALLOWED_ACTIONS; the two resource-type constants
    are whitelisted in ALLOWED_RESOURCE_TYPES; the service writes a
    matching audit row in the same session; the LOWER(address) unique
    index lives in the migration; the reason CHECK constraint lives
    in the migration; the source_event_id FK has ON DELETE SET NULL.

Pattern E: pure addition. No existing tests mutated. The Phase A code-
only commit lands the schema migrations, the service module, the
suppression precheck wiring at the three send sites, the action /
resource constants, and these tests.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICE_PATH = REPO_ROOT / "app" / "services" / "email_suppression_service.py"
MODEL_PATH = REPO_ROOT / "app" / "models" / "email_suppression.py"
EVENT_MODEL_PATH = REPO_ROOT / "app" / "models" / "email_send_event.py"
AUDIT_LOG_PATH = REPO_ROOT / "app" / "models" / "admin_audit_log.py"
MIGRATION_SUPPRESSION_PATH = (
    REPO_ROOT
    / "alembic"
    / "versions"
    / "b2e5f17a3d9c_arc8_wu6_email_suppression.py"
)
MIGRATION_EVENT_PATH = (
    REPO_ROOT
    / "alembic"
    / "versions"
    / "a91c4d2e7f08_arc8_wu6_email_send_event.py"
)


@pytest.fixture(scope="module")
def service_src() -> str:
    return SERVICE_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def model_src() -> str:
    return MODEL_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def event_model_src() -> str:
    return EVENT_MODEL_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def audit_log_src() -> str:
    return AUDIT_LOG_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def migration_suppression_src() -> str:
    return MIGRATION_SUPPRESSION_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def migration_event_src() -> str:
    return MIGRATION_EVENT_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def service_ast(service_src: str) -> ast.Module:
    return ast.parse(service_src)


# ---------------------------------------------------------------------
# CASE 1 -- SuppressedRecipientError exists as a public exception class
# ---------------------------------------------------------------------
def test_suppressed_recipient_error_is_module_level_class(service_ast):
    """SuppressedRecipientError must be importable from the service
    module so the email_service.py precheck can raise it and callers
    can catch it.
    """
    classes = [n for n in service_ast.body if isinstance(n, ast.ClassDef)]
    names = {c.name for c in classes}
    assert "SuppressedRecipientError" in names, (
        "SuppressedRecipientError must be a module-level class in "
        "app/services/email_suppression_service.py."
    )


def test_suppressed_recipient_error_inherits_runtime_error(service_ast):
    """SuppressedRecipientError must inherit RuntimeError so callers
    that catch RuntimeError or the broader Exception base catch it
    without surprise.
    """
    klass = next(
        c
        for c in service_ast.body
        if isinstance(c, ast.ClassDef) and c.name == "SuppressedRecipientError"
    )
    base_names = [b.id for b in klass.bases if isinstance(b, ast.Name)]
    assert "RuntimeError" in base_names, (
        "SuppressedRecipientError must inherit from RuntimeError to "
        "match the MagicLinkError / WelcomeEmailError / RefundEmailError "
        "convention."
    )


# ---------------------------------------------------------------------
# CASE 2 -- is_suppressed is a module-level function with the right shape
# ---------------------------------------------------------------------
def test_is_suppressed_is_module_level_function(service_ast):
    funcs = [n for n in service_ast.body if isinstance(n, ast.FunctionDef)]
    names = {f.name for f in funcs}
    assert "is_suppressed" in names, (
        "is_suppressed must be a module-level def for the email_service "
        "precheck to import it."
    )


def test_is_suppressed_signature(service_ast):
    fn = next(
        f
        for f in service_ast.body
        if isinstance(f, ast.FunctionDef) and f.name == "is_suppressed"
    )
    # Must accept (session, address) as the two positional-or-keyword args.
    arg_names = [a.arg for a in fn.args.args]
    assert arg_names[:2] == ["session", "address"], (
        f"is_suppressed must take (session, address) as its first two "
        f"positional params; got {arg_names!r}."
    )


# ---------------------------------------------------------------------
# CASE 3 -- record_suppression is a module-level function with the right shape
# ---------------------------------------------------------------------
def test_record_suppression_is_module_level_function(service_ast):
    funcs = [n for n in service_ast.body if isinstance(n, ast.FunctionDef)]
    names = {f.name for f in funcs}
    assert "record_suppression" in names, (
        "record_suppression must be a module-level def for the SES "
        "feedback route to import it."
    )


def test_record_suppression_signature_pins_required_params(service_ast):
    fn = next(
        f
        for f in service_ast.body
        if isinstance(f, ast.FunctionDef) and f.name == "record_suppression"
    )
    arg_names = [a.arg for a in fn.args.args]
    # session, address, reason are required positional-or-keyword;
    # source_event_id is optional positional-or-keyword.
    assert arg_names[:4] == [
        "session",
        "address",
        "reason",
        "source_event_id",
    ], (
        f"record_suppression must take (session, address, reason, "
        f"source_event_id) as its first four params; got {arg_names!r}."
    )
    # actor_label and note are keyword-only.
    kwonly_names = [a.arg for a in fn.args.kwonlyargs]
    assert "actor_label" in kwonly_names and "note" in kwonly_names, (
        f"record_suppression must accept keyword-only actor_label and "
        f"note; got kwonly={kwonly_names!r}."
    )


# ---------------------------------------------------------------------
# CASE 4 -- clear_suppression exists for the future admin route
# ---------------------------------------------------------------------
def test_clear_suppression_is_module_level_function(service_ast):
    funcs = [n for n in service_ast.body if isinstance(n, ast.FunctionDef)]
    names = {f.name for f in funcs}
    assert "clear_suppression" in names, (
        "clear_suppression must be a module-level def so the WU-6 "
        "action-constants landing has a service surface that uses them, "
        "even if the admin HTTP route lands later."
    )


# ---------------------------------------------------------------------
# CASE 5 -- the three new action constants are defined in admin_audit_log
# ---------------------------------------------------------------------
def test_email_action_constants_defined(audit_log_src):
    for action in (
        "ACTION_EMAIL_SUPPRESSION_RECORDED",
        "ACTION_EMAIL_SUPPRESSION_CLEARED",
        "ACTION_EMAIL_SEND_EVENT_RECEIVED",
    ):
        assert f"{action} = " in audit_log_src, (
            f"{action} must be defined in app/models/admin_audit_log.py."
        )


# ---------------------------------------------------------------------
# CASE 6 -- the three action constants are whitelisted in ALLOWED_ACTIONS
# ---------------------------------------------------------------------
def _extract_tuple_block(src: str, tuple_name: str) -> str:
    """Return the substring spanning the entire ``NAME = ( ... )`` block.

    Naive ``str.index(')')`` does not work because in-comment parentheses
    inside the tuple body (e.g. ``# Step 29.y Cluster 4 (E-3)``) short-
    circuit the scan. We instead walk character-by-character tracking
    paren depth, ignoring parens inside line comments.
    """
    start = src.index(f"{tuple_name} = (")
    # Move past the opening paren so depth starts at 1.
    depth = 0
    i = start + len(tuple_name) + len(" = (")
    depth = 1
    # Re-anchor: scan forward from i, tracking paren depth and comments.
    in_comment = False
    while i < len(src) and depth > 0:
        ch = src[i]
        if in_comment:
            if ch == "\n":
                in_comment = False
        elif ch == "#":
            in_comment = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
        i += 1
    raise AssertionError(
        f"Could not locate closing paren for {tuple_name} tuple in source."
    )


def test_email_action_constants_whitelisted(audit_log_src):
    # Locate the ALLOWED_ACTIONS tuple and verify membership.
    block = _extract_tuple_block(audit_log_src, "ALLOWED_ACTIONS")
    for action in (
        "ACTION_EMAIL_SUPPRESSION_RECORDED",
        "ACTION_EMAIL_SUPPRESSION_CLEARED",
        "ACTION_EMAIL_SEND_EVENT_RECEIVED",
    ):
        assert action in block, (
            f"{action} must appear in ALLOWED_ACTIONS so AdminAuditRepository "
            f"will accept it without raising ValueError."
        )


# ---------------------------------------------------------------------
# CASE 7 -- the two new resource-type constants are defined + whitelisted
# ---------------------------------------------------------------------
def test_email_resource_constants_defined(audit_log_src):
    for resource in (
        "RESOURCE_EMAIL_SUPPRESSION",
        "RESOURCE_EMAIL_SEND_EVENT",
    ):
        assert f"{resource} = " in audit_log_src, (
            f"{resource} must be defined in app/models/admin_audit_log.py."
        )


def test_email_resource_constants_whitelisted(audit_log_src):
    block = _extract_tuple_block(audit_log_src, "ALLOWED_RESOURCE_TYPES")
    for resource in (
        "RESOURCE_EMAIL_SUPPRESSION",
        "RESOURCE_EMAIL_SEND_EVENT",
    ):
        assert resource in block, (
            f"{resource} must appear in ALLOWED_RESOURCE_TYPES."
        )


# ---------------------------------------------------------------------
# CASE 8 -- record_suppression constructs an AdminAuditLog row in-session
# ---------------------------------------------------------------------
def test_record_suppression_writes_audit_row(service_src):
    """The service must construct an AdminAuditLog inside the same
    session as the EmailSuppression INSERT, so the audit-chain
    before_flush handler picks it up atomically.
    """
    assert "AdminAuditLog(" in service_src, (
        "record_suppression must construct AdminAuditLog directly so "
        "the audit_chain before_flush handler populates the chain "
        "hashes inside the same flush as the suppression INSERT."
    )
    assert "ACTION_EMAIL_SUPPRESSION_RECORDED" in service_src, (
        "The audit row's action must be ACTION_EMAIL_SUPPRESSION_RECORDED."
    )


def test_clear_suppression_writes_audit_row(service_src):
    """Symmetric audit row on the clearance path."""
    assert "ACTION_EMAIL_SUPPRESSION_CLEARED" in service_src, (
        "clear_suppression must write an ACTION_EMAIL_SUPPRESSION_CLEARED "
        "audit row capturing the cleared row's state in before_json."
    )


# ---------------------------------------------------------------------
# CASE 9 -- migration b2e5f17a3d9c lands the required schema gates
# ---------------------------------------------------------------------
def test_suppression_migration_has_check_constraint(migration_suppression_src):
    """CHECK constraint must enforce the allowed reason set at the
    schema layer as defence-in-depth.
    """
    assert "reason IN ('HardBounce', 'Complaint', 'ManualBlock')" in (
        migration_suppression_src
    ), (
        "Migration b2e5f17a3d9c must carry a CHECK constraint on the "
        "reason column enforcing the SUPPRESSION_REASONS set."
    )


def test_suppression_migration_has_lower_address_unique_index(
    migration_suppression_src,
):
    """LOWER(address) UNIQUE expression index must exist so the
    is_suppressed lookup hits the index and case-insensitive
    uniqueness is enforced at the schema layer.
    """
    assert "ux_email_suppression_lower_address" in migration_suppression_src, (
        "Migration must create ux_email_suppression_lower_address."
    )
    assert "LOWER(address)" in migration_suppression_src, (
        "The UNIQUE index must use LOWER(address) as its expression."
    )
    assert "unique=True" in migration_suppression_src, (
        "The LOWER(address) index must be UNIQUE."
    )


def test_suppression_migration_fk_has_set_null_ondelete(migration_suppression_src):
    """source_event_id FK must use ON DELETE SET NULL so a feedback-
    event retention purge does not cascade-delete the suppression rows.
    """
    assert "ondelete=\"SET NULL\"" in migration_suppression_src, (
        "FK source_event_id -> email_send_event.event_id must use "
        "ON DELETE SET NULL."
    )


def test_suppression_migration_down_revision_chains_email_send_event(
    migration_suppression_src,
):
    """Migration must chain off a91c4d2e7f08 (email_send_event) so the
    FK target exists at upgrade time.
    """
    assert "down_revision = \"a91c4d2e7f08\"" in migration_suppression_src, (
        "b2e5f17a3d9c must have down_revision = a91c4d2e7f08 so the "
        "email_send_event table exists before the suppression FK is "
        "created."
    )


# ---------------------------------------------------------------------
# CASE 10 -- migration a91c4d2e7f08 lands the email_send_event schema
# ---------------------------------------------------------------------
def test_event_migration_has_check_constraint(migration_event_src):
    """email_send_event CHECK constraint must enforce the SES event-
    type set.
    """
    assert "event_type IN ('Bounce', 'Complaint', 'Reject', " in (
        migration_event_src
    ), (
        "Migration a91c4d2e7f08 must carry a CHECK constraint on "
        "event_type enforcing the SES_EVENT_TYPES set."
    )


def test_event_migration_has_unique_event_id(migration_event_src):
    """UNIQUE constraint on event_id is the schema-layer idempotency
    gate for SNS at-least-once delivery.
    """
    assert "uq_email_send_event_event_id" in migration_event_src, (
        "Migration must create a UNIQUE constraint named "
        "uq_email_send_event_event_id."
    )


def test_event_migration_down_revision_is_pre_wu6_head(migration_event_src):
    """a91c4d2e7f08 must chain off b4d8a2e7c1f3 (Step 30a head)."""
    assert "down_revision = \"b4d8a2e7c1f3\"" in migration_event_src, (
        "a91c4d2e7f08 must have down_revision = b4d8a2e7c1f3 "
        "(Step 30a.4 owner-scope-backfill, the pre-WU-6 head)."
    )


# ---------------------------------------------------------------------
# CASE 11 -- SUPPRESSION_REASONS constants exist on the model module
# ---------------------------------------------------------------------
def test_suppression_reason_constants_defined(model_src):
    for const in (
        "SUPPRESSION_REASON_HARD_BOUNCE",
        "SUPPRESSION_REASON_COMPLAINT",
        "SUPPRESSION_REASON_MANUAL_BLOCK",
        "SUPPRESSION_REASONS",
    ):
        assert f"{const} = " in model_src or f"{const} =" in model_src, (
            f"{const} must be a module-level constant in "
            f"app/models/email_suppression.py."
        )


def test_suppression_reasons_values(model_src):
    """The reason string values must match the CHECK constraint in the
    migration. Drift here = INSERT failures at runtime.
    """
    assert 'SUPPRESSION_REASON_HARD_BOUNCE = "HardBounce"' in model_src
    assert 'SUPPRESSION_REASON_COMPLAINT = "Complaint"' in model_src
    assert 'SUPPRESSION_REASON_MANUAL_BLOCK = "ManualBlock"' in model_src


# ---------------------------------------------------------------------
# CASE 12 -- SES event-type constants exist on the event model module
# ---------------------------------------------------------------------
def test_ses_event_type_constants_defined(event_model_src):
    for const in (
        "SES_EVENT_BOUNCE",
        "SES_EVENT_COMPLAINT",
        "SES_EVENT_REJECT",
        "SES_EVENT_RENDERING_FAILURE",
        "SES_EVENT_TYPES",
        "SES_EVENT_TYPES_TRIGGER_SUPPRESSION",
    ):
        assert f"{const} = " in event_model_src, (
            f"{const} must be a module-level constant in "
            f"app/models/email_send_event.py."
        )


def test_ses_event_trigger_suppression_subset(event_model_src):
    """Only Bounce and Complaint events should be in the auto-
    suppression subset (Reject / RenderingFailure are recorded but
    don't auto-suppress).
    """
    # Find the SES_EVENT_TYPES_TRIGGER_SUPPRESSION block.
    start = event_model_src.index("SES_EVENT_TYPES_TRIGGER_SUPPRESSION = frozenset(")
    end = event_model_src.index(")", start)
    block = event_model_src[start:end]
    assert "SES_EVENT_BOUNCE" in block, (
        "Bounce must be in SES_EVENT_TYPES_TRIGGER_SUPPRESSION."
    )
    assert "SES_EVENT_COMPLAINT" in block, (
        "Complaint must be in SES_EVENT_TYPES_TRIGGER_SUPPRESSION."
    )
    assert "SES_EVENT_REJECT" not in block, (
        "Reject must NOT auto-suppress -- it's a same-account policy "
        "rejection, not a recipient signal."
    )
    assert "SES_EVENT_RENDERING_FAILURE" not in block, (
        "RenderingFailure must NOT auto-suppress -- it's a sender-side "
        "template defect, not a recipient signal."
    )


# ---------------------------------------------------------------------
# CASE 13 -- model registration in app/models/__init__.py
# ---------------------------------------------------------------------
def test_models_init_registers_new_models():
    init_src = (REPO_ROOT / "app" / "models" / "__init__.py").read_text(
        encoding="utf-8"
    )
    assert "from app.models.email_send_event import" in init_src, (
        "app/models/__init__.py must import from email_send_event so "
        "SQLAlchemy registers the table on metadata."
    )
    assert "from app.models.email_suppression import" in init_src, (
        "app/models/__init__.py must import from email_suppression so "
        "SQLAlchemy registers the table on metadata."
    )
    assert "EmailSendEvent" in init_src
    assert "EmailSuppression" in init_src
