"""Arc 10 re-open Gap 5 -- ReactivationService contract regression.

Locks the Vision Section 6.4 reactivation contract:

  C1  Service + 5 typed error classes exist (NotInGrace,
      WindowExpired, AlreadyTombstone, StripeCheckoutFailed,
      StripeMismatch).

  C2  Two methods exist with the route-required signature shape:
        * stage_reactivation(admin_id, target_tier, success_url, cancel_url)
          -> ReactivationStaged
        * complete_reactivation(admin_id, stripe_checkout_session_id, audit_ctx)
          -> ReactivationCompleted

  C3  ReactivationCompleted dataclass declares
      api_keys_revoked_count and team_members_restored as fields.
      These MUST be returned as 0 per Vision 6.4 (revoked keys stay
      revoked; team members re-invite manually). The fields exist so
      the frontend can surface "X instances restored; please reissue
      your keys" copy without re-fetching.

  C4  Reactivation flow CLEARS closure_initiated_at on success --
      otherwise the admin would still register as 'closed' to the
      retention worker and be tombstoned at the original 30-day
      mark. (This was the e2e test contract proven against prod RDS
      in the original arc.)

  C5  Reactivation flow does NOT rehydrate
      pending_downgrade_archived_at rows (knowledge that was archived
      during a separate downgrade-grace flow is recovered only on
      re-upgrade, not on closure reactivation).

  C6  Window-expired guard: stage_reactivation must check the 30-day
      window and raise ReactivationWindowExpiredError when the
      grace_window has elapsed. This is the doctrine boundary that
      Vision 6.5 enforces -- post-window, the only path is fresh signup.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICE_PATH = REPO_ROOT / "app" / "services" / "reactivation_service.py"


def _parse(p: Path) -> ast.Module:
    return ast.parse(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------
# C1: error classes.
# ---------------------------------------------------------------------

EXPECTED_ERRORS = [
    "ReactivationError",
    "AccountNotInGraceError",
    "ReactivationWindowExpiredError",
    "AccountAlreadyTombstoneError",
    "StripeReactivationCheckoutFailedError",
    "StripeSubscriptionMismatchError",
]


@pytest.mark.parametrize("name", EXPECTED_ERRORS)
def test_reactivation_error_class_exists(name: str):
    classes = {n.name for n in ast.walk(_parse(SERVICE_PATH)) if isinstance(n, ast.ClassDef)}
    assert name in classes, (
        f"ReactivationService must expose {name!r} so the route layer "
        "can map each error to the right HTTP status (NotInGrace -> 409, "
        "WindowExpired -> 410, Tombstone -> 410, StripeFailed -> 502, "
        "Mismatch -> 409)."
    )


@pytest.mark.parametrize("name", [
    "AccountNotInGraceError",
    "ReactivationWindowExpiredError",
    "AccountAlreadyTombstoneError",
    "StripeReactivationCheckoutFailedError",
    "StripeSubscriptionMismatchError",
])
def test_specific_errors_inherit_reactivation_error(name: str):
    cls = next(
        n for n in ast.walk(_parse(SERVICE_PATH))
        if isinstance(n, ast.ClassDef) and n.name == name
    )
    bases = [b.id for b in cls.bases if isinstance(b, ast.Name)]
    assert "ReactivationError" in bases


# ---------------------------------------------------------------------
# C2: method surface + signature.
# ---------------------------------------------------------------------

def _methods() -> dict[str, ast.FunctionDef]:
    cls = next(
        n for n in ast.walk(_parse(SERVICE_PATH))
        if isinstance(n, ast.ClassDef) and n.name == "ReactivationService"
    )
    return {
        n.name: n for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_stage_reactivation_exists_with_required_kwargs():
    methods = _methods()
    assert "stage_reactivation" in methods
    method = methods["stage_reactivation"]
    # Required keyword-only args.
    kw_names = [a.arg for a in method.args.kwonlyargs]
    for required in ("admin_id", "target_tier", "success_url", "cancel_url"):
        assert required in kw_names, (
            f"stage_reactivation must accept keyword arg {required!r}; "
            f"got {kw_names}"
        )


def test_complete_reactivation_exists_with_required_kwargs():
    methods = _methods()
    assert "complete_reactivation" in methods
    method = methods["complete_reactivation"]
    kw_names = [a.arg for a in method.args.kwonlyargs]
    for required in ("admin_id", "stripe_checkout_session_id", "audit_ctx"):
        assert required in kw_names, (
            f"complete_reactivation must accept keyword arg {required!r}; "
            f"got {kw_names}"
        )


# ---------------------------------------------------------------------
# C3: ReactivationCompleted dataclass fields.
# ---------------------------------------------------------------------

def test_reactivation_completed_dataclass_fields():
    """Vision 6.4 keys: instances are restored; api_keys stay revoked;
    team_members stay cleared. These three counters expose the contract
    to the frontend in a single response."""
    cls = next(
        n for n in ast.walk(_parse(SERVICE_PATH))
        if isinstance(n, ast.ClassDef) and n.name == "ReactivationCompleted"
    )
    annotated = [
        n.target.id for n in cls.body
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
    ]
    for required in (
        "admin_id",
        "reactivated_at",
        "new_subscription_id",
        "instances_restored",
        "api_keys_revoked_count",
        "team_members_restored",
    ):
        assert required in annotated, (
            f"ReactivationCompleted must declare {required}; got {annotated}. "
            "Frontend uses these counts to render the post-reactivation "
            "explainer; missing fields trigger silent UI gaps."
        )


def test_reactivation_completed_documents_vision_64_zeros():
    """The two 'always 0' counters carry Vision 6.4 doctrine inline.
    The comments are part of the contract -- a reviewer reading the
    dataclass needs to know why these aren't computed from current
    state."""
    src = SERVICE_PATH.read_text(encoding="utf-8")
    # Look at the lines following the two field declarations.
    for field in ("api_keys_revoked_count", "team_members_restored"):
        pat = re.search(
            rf"{field}.*$",
            src, re.MULTILINE,
        )
        assert pat, f"could not locate {field} declaration"
        line = pat.group(0)
        # Each field's line OR an adjacent comment must mention 0 or
        # the rationale ('stay revoked' / 'always 0').
        assert "0" in line or "stay" in line.lower(), (
            f"{field} declaration should document why it's always 0. "
            f"Got: {line!r}"
        )


# ---------------------------------------------------------------------
# C4: complete_reactivation CLEARS closure_initiated_at.
# ---------------------------------------------------------------------

def test_complete_reactivation_clears_closure_initiated_at():
    """Without this, the retention worker would tombstone a reactivated
    admin at the original 30-day mark. Critical correctness boundary."""
    src = SERVICE_PATH.read_text(encoding="utf-8")
    method = re.search(
        r"def complete_reactivation\(.*?\)(.*?)(?=\n    def |\n\nclass |\Z)",
        src, re.DOTALL,
    )
    assert method, "complete_reactivation not found"
    body = method.group(1)
    # The body must reference closure_initiated_at and either set it to
    # None or null.
    assert "closure_initiated_at" in body, (
        "complete_reactivation must clear closure_initiated_at. Without "
        "this, the retention worker still sees the admin as closed and "
        "tombstones them at the original 30-day mark."
    )
    # Look for an assignment to None or a SQL UPDATE setting it NULL.
    cleared = (
        re.search(r"closure_initiated_at\s*=\s*None", body)
        or re.search(r"closure_initiated_at\s*=\s*NULL", body, re.IGNORECASE)
        or re.search(r"SET\s+closure_initiated_at\s*=\s*NULL", body, re.IGNORECASE)
    )
    assert cleared, (
        "complete_reactivation must explicitly set closure_initiated_at "
        "to NULL/None. Found the reference but not the clearing pattern."
    )


def test_complete_reactivation_reactivates_admin_active_flag():
    """A reactivated admin's active flag flips back to True so the
    rest of the stack treats them as a live tenant."""
    src = SERVICE_PATH.read_text(encoding="utf-8")
    method = re.search(
        r"def complete_reactivation\(.*?\)(.*?)(?=\n    def |\n\nclass |\Z)",
        src, re.DOTALL,
    )
    body = method.group(1)
    # Either Admin.active = True via ORM or SET active = true via SQL.
    reactivated = (
        re.search(r"\.active\s*=\s*True", body)
        or re.search(r"SET\s+active\s*=\s*true", body, re.IGNORECASE)
    )
    assert reactivated, (
        "complete_reactivation must set admin.active back to True. "
        "Without it the tenant remains in the deactivated state even "
        "after Stripe checkout succeeds."
    )


# ---------------------------------------------------------------------
# C5: pending_downgrade_archived_at NOT rehydrated.
# ---------------------------------------------------------------------

def test_reactivation_does_not_touch_pending_downgrade_archived_at():
    """Knowledge that was archived during a separate downgrade-grace
    flow is recovered only on re-upgrade, not on closure reactivation.
    The service must NOT clear pending_downgrade_archived_at as part of
    the closure-reactivation path."""
    src = SERVICE_PATH.read_text(encoding="utf-8")
    method = re.search(
        r"def complete_reactivation\(.*?\)(.*?)(?=\n    def |\n\nclass |\Z)",
        src, re.DOTALL,
    )
    body = method.group(1)
    # Asserting absence is tricky because a comment mentioning the field
    # is fine. Look only for an assignment-style clearing.
    bad = (
        re.search(r"pending_downgrade_archived_at\s*=\s*None", body)
        or re.search(r"SET\s+pending_downgrade_archived_at\s*=\s*NULL", body, re.IGNORECASE)
    )
    assert not bad, (
        "complete_reactivation must NOT clear pending_downgrade_archived_at. "
        "Downgrade archives are recovered on re-upgrade, not on closure "
        "reactivation. Mixing the two breaks the 'downgrade-grace is "
        "separate from closure-grace' invariant."
    )


# ---------------------------------------------------------------------
# C6: stage_reactivation enforces the 30-day window.
# ---------------------------------------------------------------------

def test_stage_reactivation_raises_window_expired():
    """The window-expired guard is the single boundary preventing
    post-grace zombie reactivation. Vision 6.5 mandates that after
    30 days, the only path is fresh signup."""
    src = SERVICE_PATH.read_text(encoding="utf-8")
    method = re.search(
        r"def stage_reactivation\(.*?\)(.*?)(?=\n    def |\n\nclass |\Z)",
        src, re.DOTALL,
    )
    assert method, "stage_reactivation not found"
    body = method.group(1)
    assert "ReactivationWindowExpiredError" in body, (
        "stage_reactivation must raise ReactivationWindowExpiredError "
        "when the 30-day grace window has elapsed. Found the method "
        "but it does not reference the error."
    )


def test_stage_reactivation_uses_grace_window_days_constant():
    """The window check must reference GRACE_WINDOW_DAYS, not a literal
    30. Single source of truth at closure_service."""
    src = SERVICE_PATH.read_text(encoding="utf-8")
    assert "GRACE_WINDOW_DAYS" in src, (
        "reactivation_service.py must import + use GRACE_WINDOW_DAYS "
        "from closure_service. Hardcoded 30 would drift."
    )
