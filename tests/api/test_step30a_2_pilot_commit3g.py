"""Step 30a.2-pilot Commit 3g — contract tests.

Commit 3f fixed the *read* path: ``/api/v1/billing/me`` now derives
``is_pilot`` from ``provider_snapshot.metadata.luciel_intro_applied``
ALONE, and ``pilot_window_end`` falls back to ``created_at + 90 days``
when ``trial_end`` is null.

Commit 3g closes the symmetric gap on the *write* path:
``BillingService.process_pilot_refund``. Pre-3g it required the
conjunction ``luciel_intro_applied == "true" AND sub.trial_end is not
None`` for eligibility (raising ``NotFirstTimePilotError`` otherwise),
and its 90-day window check used ``sub.trial_end`` exclusively. With
3f shipped but 3g not, the website rendered the refund CTA on degraded
rows and the click then 403'd with code ``not_first_time_customer``.

Commit 3g:

  1. Drops the ``or sub.trial_end is None`` clause from the eligibility
     predicate -- pilot-ness is purely metadata-driven, mirroring 3f.
  2. Computes ``effective_window_end = sub.trial_end if not None else
     sub.created_at + timedelta(days=90)``, mirroring 3f's
     ``pilot_window_end`` derivation, and uses that for the 90-day
     window check.
  3. Imports ``timedelta`` alongside ``datetime`` and ``timezone``.
  4. Repeat-customer protection is preserved upstream at
     ``BillingService.is_first_time_customer`` (guards
     ``create_checkout``), which is unchanged.

The Stripe-charge existence check (eligibility 4) is also unchanged --
it is the real "defence in depth" because it asks the canonical source
(Stripe) whether there is a charge to refund.

All assertions here are pure AST/text pins -- no Stripe SDK, no DB.
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
BILLING_SVC = ROOT / "app" / "services" / "billing_service.py"
BILLING_API = ROOT / "app" / "api" / "v1" / "billing.py"


def _module(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _function(mod: ast.Module, cls_name: str, fn_name: str) -> ast.FunctionDef:
    for node in mod.body:
        if isinstance(node, ast.ClassDef) and node.name == cls_name:
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef) and sub.name == fn_name:
                    return sub
    raise AssertionError(f"{cls_name}.{fn_name} not found in {mod}")


# ---------------------------------------------------------------------
# Case 1: timedelta import landed on billing_service.py
# ---------------------------------------------------------------------
def test_billing_service_imports_timedelta():
    """Commit 3g needs timedelta for the 90-day fallback in
    process_pilot_refund. The pre-3g import line was
    ``from datetime import datetime, timezone``.
    """
    src = BILLING_SVC.read_text(encoding="utf-8")
    # We expect a single canonical import statement bringing in all three.
    assert "from datetime import datetime, timedelta, timezone" in src, (
        "billing_service.py must import datetime, timedelta, timezone "
        "from the datetime module on one line. Commit 3g."
    )


# ---------------------------------------------------------------------
# Case 2: eligibility (2) no longer requires trial_end
# ---------------------------------------------------------------------
def test_process_pilot_refund_eligibility_predicate_is_metadata_only():
    """The first-time gate in process_pilot_refund must check
    ``intro_applied`` alone, not ``intro_applied AND trial_end is not
    None``.
    """
    src = BILLING_SVC.read_text(encoding="utf-8")
    # Negative: the asymmetric belt-and-suspenders predicate must be gone.
    assert "if not intro_applied or sub.trial_end is None:" not in src, (
        "Commit 3g must drop the `or sub.trial_end is None` clause from "
        "the first-time eligibility check in process_pilot_refund. The "
        "read-path (Commit 3f) and the write-path must use the same "
        "metadata-only signal."
    )
    # Positive: the new metadata-only predicate is present.
    assert "if not intro_applied:" in src, (
        "process_pilot_refund must raise NotFirstTimePilotError purely "
        "from `if not intro_applied:` (no trial_end conjunction)."
    )


# ---------------------------------------------------------------------
# Case 3: NotFirstTimePilotError is still raised on the metadata branch
# ---------------------------------------------------------------------
def test_process_pilot_refund_still_raises_not_first_time_on_metadata_false():
    """The eligibility relaxation must NOT remove the NotFirstTimePilotError
    raise -- repeat-customer protection (when metadata is missing or
    falsy) is preserved.
    """
    mod = _module(BILLING_SVC)
    fn = _function(mod, "BillingService", "process_pilot_refund")
    raised = {
        ast.unparse(n.exc.func) if isinstance(n.exc, ast.Call) else None
        for n in ast.walk(fn)
        if isinstance(n, ast.Raise) and n.exc is not None
    }
    assert "NotFirstTimePilotError" in raised, (
        "process_pilot_refund must still raise NotFirstTimePilotError "
        "for rows where luciel_intro_applied is not true. The relaxation "
        "in Commit 3g only removes the trial_end conjunction, not the "
        "metadata gate itself."
    )


# ---------------------------------------------------------------------
# Case 4: effective_window_end is computed with timedelta fallback
# ---------------------------------------------------------------------
def test_process_pilot_refund_uses_effective_window_end_with_fallback():
    """The 90-day window check must use a derived ``effective_window_end``
    that falls back to ``created_at + timedelta(days=90)`` when
    ``trial_end`` is null.
    """
    src = BILLING_SVC.read_text(encoding="utf-8")
    # The variable name is a load-bearing convention because the drift
    # doc names it explicitly. Pin the name.
    assert "effective_window_end" in src, (
        "process_pilot_refund must introduce an `effective_window_end` "
        "variable that holds either trial_end or the created_at fallback. "
        "Commit 3g."
    )
    # The fallback expression must appear textually somewhere in the file.
    assert "timedelta(days=90)" in src, (
        "process_pilot_refund must use `timedelta(days=90)` for the "
        "fallback window. Commit 3g."
    )
    # Negative: the old strict trial_end-only check must be gone.
    assert "if trial_end is None or now > trial_end:" not in src, (
        "Commit 3g must replace `if trial_end is None or now > trial_end:` "
        "with the effective_window_end-based check."
    )


# ---------------------------------------------------------------------
# Case 5: PilotWindowExpiredError is still raised on the expired branch
# ---------------------------------------------------------------------
def test_process_pilot_refund_still_raises_window_expired():
    """The window-end relaxation must NOT remove the
    PilotWindowExpiredError raise -- it just moves the source of
    truth from trial_end to effective_window_end.
    """
    mod = _module(BILLING_SVC)
    fn = _function(mod, "BillingService", "process_pilot_refund")
    raised = {
        ast.unparse(n.exc.func) if isinstance(n.exc, ast.Call) else None
        for n in ast.walk(fn)
        if isinstance(n, ast.Raise) and n.exc is not None
    }
    assert "PilotWindowExpiredError" in raised, (
        "process_pilot_refund must still raise PilotWindowExpiredError "
        "when the computed effective_window_end is in the past. "
        "Commit 3g preserves the rejection, only widens the source."
    )


# ---------------------------------------------------------------------
# Case 6: charge-existence eligibility (4) is unchanged
# ---------------------------------------------------------------------
def test_process_pilot_refund_still_checks_stripe_charge_exists():
    """Eligibility (4) -- Stripe must be able to find the intro Charge --
    is the real defence-in-depth and must be preserved in Commit 3g.
    """
    src = BILLING_SVC.read_text(encoding="utf-8")
    assert "find_intro_charge_id" in src, (
        "process_pilot_refund must still call "
        "self.stripe.find_intro_charge_id; this is the canonical-source "
        "check that prevents refunding a phantom charge. Commit 3g."
    )
    assert "PilotChargeNotFoundError" in src, (
        "process_pilot_refund must still raise PilotChargeNotFoundError "
        "when Stripe cannot find the intro charge. Commit 3g."
    )


# ---------------------------------------------------------------------
# Case 7: Commit 3f read-path is still in place (no regression)
# ---------------------------------------------------------------------
def test_commit_3f_read_path_unchanged():
    """The Commit 3f derivation in /api/v1/billing/me must still be in
    place: is_pilot from metadata alone, pilot_window_end with the
    created_at fallback.
    """
    src = BILLING_API.read_text(encoding="utf-8")
    assert "from datetime import timedelta" in src, (
        "Commit 3f's import of timedelta in billing.py must remain."
    )
    assert 'snapshot_meta.get("luciel_intro_applied", "")' in src, (
        "Commit 3f's metadata-driven is_pilot derivation must remain."
    )
    assert "sub.created_at + timedelta(days=90)" in src, (
        "Commit 3f's created_at + 90 days fallback must remain in the "
        "read path."
    )


# ---------------------------------------------------------------------
# Case 8: write-path and read-path use the same fallback expression
# ---------------------------------------------------------------------
def test_write_path_uses_same_90_day_fallback_as_read_path():
    """Symmetry pin: both /me (read) and process_pilot_refund (write)
    must compute the 90-day fallback the same way. The exact textual
    form is ``created_at + timedelta(days=90)`` on both sides.
    """
    api_src = BILLING_API.read_text(encoding="utf-8")
    svc_src = BILLING_SVC.read_text(encoding="utf-8")
    fallback_expr = "timedelta(days=90)"
    assert api_src.count(fallback_expr) >= 1, (
        "read path lost the timedelta(days=90) fallback expression"
    )
    assert svc_src.count(fallback_expr) >= 1, (
        "write path must have a timedelta(days=90) fallback expression "
        "matching the read path's"
    )


# ---------------------------------------------------------------------
# Case 9: tz-aware coercion for created_at fallback
# ---------------------------------------------------------------------
def test_process_pilot_refund_tz_coerces_created_at_for_fallback():
    """When trial_end is null and we fall back to created_at, the
    comparison ``now > effective_window_end`` requires both sides to
    be tz-aware. created_at is server-default tz-aware in the model
    but a defensive coerce is consistent with the trial_end defensive
    coerce already present.
    """
    src = BILLING_SVC.read_text(encoding="utf-8")
    # We look for any tz-coerce on a created_at variable inside
    # process_pilot_refund. The exact local variable name is the
    # convention pinned by the drift doc: ``sub_created``.
    assert "sub_created" in src, (
        "process_pilot_refund must introduce a `sub_created` local for "
        "the tz-aware coerced created_at before computing the 90-day "
        "fallback."
    )
    assert "sub_created.replace(tzinfo=timezone.utc)" in src, (
        "process_pilot_refund must defensively tz-coerce the created_at "
        "fallback the same way it tz-coerces trial_end."
    )


# ---------------------------------------------------------------------
# Case 10: ECS exec rejection log line preserves intro_applied + trial_end
# ---------------------------------------------------------------------
def test_pilot_refund_rejection_log_still_includes_intro_applied_and_trial_end():
    """The rejection log line must continue to include both
    ``intro_applied`` and ``sub.trial_end`` so a future drift trace
    can distinguish a metadata-false rejection from an audit of the
    underlying row.
    """
    src = BILLING_SVC.read_text(encoding="utf-8")
    assert "intro_applied=%s trial_end=%s" in src, (
        "process_pilot_refund's rejection log line must still emit "
        "intro_applied=%s trial_end=%s so we can tell a metadata-false "
        "rejection from an unrelated structural anomaly."
    )


# ---------------------------------------------------------------------
# Case 11: window-expired rejection log line names the effective_window_end
# ---------------------------------------------------------------------
def test_window_expired_rejection_log_names_effective_window_end():
    """The window-expired rejection log line must include
    ``window_end=%s`` so a triage agent can tell whether the trial_end
    was null and the fallback was used.
    """
    src = BILLING_SVC.read_text(encoding="utf-8")
    assert "window_end=%s" in src, (
        "process_pilot_refund's window-expired log line must include "
        "window_end=%s so the fallback case is observable."
    )


# ---------------------------------------------------------------------
# Case 12: is_first_time_customer guard is upstream, unchanged
# ---------------------------------------------------------------------
def test_is_first_time_customer_guard_still_in_create_checkout():
    """Repeat-customer protection lives upstream at create_checkout.
    Commit 3g must not touch that path.
    """
    src = BILLING_SVC.read_text(encoding="utf-8")
    assert "is_first_time_customer" in src, (
        "is_first_time_customer must remain on BillingService."
    )
    # The method should still be invoked somewhere in the file (the
    # create_checkout path); a pure-text search is sufficient.
    assert src.count("is_first_time_customer") >= 2, (
        "is_first_time_customer should be both defined and invoked at "
        "least once; Commit 3g must not remove the call site."
    )
