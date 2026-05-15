"""Step 30a.2-pilot Commit 3f — contract tests.

After the first live $100 pilot purchase on 2026-05-15 we discovered that
``checkout.session.completed`` was producing Subscription rows with:

  * ``status='complete'``         (checkout.session.status, NOT a
                                   Stripe Subscription status)
  * ``trial_end=None``            (the checkout.session object has no
                                   trial_end field; the Subscription
                                   does)
  * ``current_period_start=None``,
    ``current_period_end=None``   (same reason -- moved to items in
                                   Stripe basil 2025-03-31, but were
                                   never on the session)

Downstream the ``/me`` endpoint additionally required
``sub.trial_end is not None`` for ``is_pilot`` to compute True, so the
self-serve refund CTA never rendered.

Commit 3f makes three coordinated changes:

  1. ``StripeClient.retrieve_subscription`` -- new method that fetches
     the canonical Subscription object by id.
  2. ``BillingWebhookService._on_checkout_completed`` -- calls
     ``retrieve_subscription`` before INSERT and uses a ``_from_sub``
     helper that prefers Subscription fields, with graceful degradation
     to ``data_object`` if the fetch raises.
  3. ``/api/v1/billing/me`` -- ``is_pilot`` is now driven by
     ``provider_snapshot.metadata.luciel_intro_applied`` ALONE.
     ``pilot_window_end`` prefers ``sub.trial_end`` when populated, and
     falls back to ``sub.created_at + 90 days`` otherwise so older rows
     (e.g. the live one from earlier 2026-05-15) display the correct
     deadline.

All assertions here are pure AST/text pins -- no Stripe SDK, no DB.
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
STRIPE_CLIENT = ROOT / "app" / "integrations" / "stripe" / "client.py"
WEBHOOK_SVC = ROOT / "app" / "services" / "billing_webhook_service.py"
BILLING_API = ROOT / "app" / "api" / "v1" / "billing.py"


# ---------------------------------------------------------------------
# Case 1: StripeClient gains retrieve_subscription
# ---------------------------------------------------------------------


def test_stripe_client_has_retrieve_subscription_method():
    """retrieve_subscription must exist on StripeClient with a single
    positional ``subscription_id: str`` parameter."""
    tree = ast.parse(STRIPE_CLIENT.read_text())
    method = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if (
                    isinstance(item, ast.FunctionDef)
                    and item.name == "retrieve_subscription"
                ):
                    method = item
                    break
    assert method is not None, (
        "retrieve_subscription method missing from StripeClient"
    )
    # Signature: (self, subscription_id: str) -> Any
    arg_names = [a.arg for a in method.args.args]
    assert arg_names == ["self", "subscription_id"], (
        f"unexpected retrieve_subscription signature: {arg_names}"
    )


def test_stripe_client_retrieve_subscription_calls_sdk():
    """retrieve_subscription must call ``stripe.Subscription.retrieve(id)``."""
    text = STRIPE_CLIENT.read_text()
    # Find the method body
    marker = "def retrieve_subscription("
    idx = text.find(marker)
    assert idx >= 0, "retrieve_subscription declaration missing"
    # The next method or end of file
    tail = text[idx:]
    # Trim to next top-level/method boundary -- a generous slice is fine
    sdk_call = "stripe.Subscription.retrieve(subscription_id)"
    assert sdk_call in tail[:2000], (
        f"retrieve_subscription does not call {sdk_call!r}"
    )


# ---------------------------------------------------------------------
# Case 2: Webhook handler reads from Subscription, not checkout.session
# ---------------------------------------------------------------------


def test_webhook_handler_calls_retrieve_subscription():
    """_on_checkout_completed must call self.stripe.retrieve_subscription
    before writing the Subscription row."""
    text = WEBHOOK_SVC.read_text()
    assert "self.stripe.retrieve_subscription(" in text, (
        "_on_checkout_completed does not call retrieve_subscription"
    )


def test_webhook_handler_uses_from_sub_helper():
    """The handler must define a ``_from_sub`` helper that prefers the
    Subscription object's fields over the checkout.session data."""
    text = WEBHOOK_SVC.read_text()
    assert "def _from_sub(" in text, "_from_sub helper missing"
    # The four migrated fields must all flow through _from_sub
    for field in ("status", "trial_end", "current_period_start", "current_period_end"):
        assert f'_from_sub("{field}"' in text, (
            f"field {field!r} is not read via _from_sub helper"
        )


def test_webhook_handler_no_longer_reads_status_from_session():
    """Regression pin: ``data_object.get("status")`` must NOT appear in
    the Subscription row construction block. (It may legitimately appear
    elsewhere -- e.g. logging -- but not as the status field source.)"""
    text = WEBHOOK_SVC.read_text()
    # The specific buggy idiom that copied session.status into sub.status
    bad = 'status=data_object.get("status")'
    assert bad not in text, (
        f"webhook still reads status directly from session: {bad!r}"
    )


def test_webhook_handler_fetch_is_wrapped_in_try():
    """The retrieve_subscription call must be wrapped so a Stripe outage
    does not 500 the webhook (graceful degrade to inline session data)."""
    text = WEBHOOK_SVC.read_text()
    idx = text.find("self.stripe.retrieve_subscription(")
    assert idx >= 0
    # Walk backwards to confirm a try: precedes this call within the
    # nearest 500 chars.
    preamble = text[max(0, idx - 500): idx]
    assert "try:" in preamble, (
        "retrieve_subscription call is not wrapped in try/except"
    )


# ---------------------------------------------------------------------
# Case 3: /me endpoint -- is_pilot driven by metadata alone
# ---------------------------------------------------------------------


def test_billing_api_imports_timedelta():
    """The /me endpoint needs timedelta to synthesize pilot_window_end."""
    text = BILLING_API.read_text()
    assert "from datetime import timedelta" in text, (
        "billing.py is missing 'from datetime import timedelta'"
    )


def test_is_pilot_no_longer_requires_trial_end():
    """The is_pilot derivation must NOT require ``trial_end is not None``.

    Pin: the exact 2-line conjunctive form from Commit 3c/3e is gone.
    We look for the buggy pattern: the literal ``"and sub.trial_end is
    not None"`` directly inside an is_pilot assignment.
    """
    text = BILLING_API.read_text()
    # The buggy conjunction (with arbitrary whitespace) must not survive.
    # We're loose on whitespace but strict on the structure.
    bad_lines = [
        line for line in text.splitlines()
        if "is_pilot" in line and "trial_end is not None" in line
        and "=" in line and "and" in line
    ]
    assert not bad_lines, (
        "is_pilot still requires trial_end is not None: "
        f"{bad_lines!r}"
    )


def test_pilot_window_end_falls_back_to_created_at():
    """When ``sub.trial_end`` is None, ``pilot_window_end`` must be
    derived from ``sub.created_at + timedelta(days=90)``."""
    text = BILLING_API.read_text()
    # The exact synthesizing expression must be present.
    assert "sub.created_at + timedelta(days=90)" in text, (
        "pilot_window_end fallback to created_at + 90 days missing"
    )


def test_is_pilot_drives_solely_from_metadata():
    """The is_pilot assignment must read ``luciel_intro_applied`` and
    nothing else (the metadata IS the source of truth that a subscription
    was sold on the pilot path)."""
    text = BILLING_API.read_text()
    # The new shape: a single-expression assignment
    expected = (
        'is_pilot = str(snapshot_meta.get("luciel_intro_applied", "")).lower() == "true"'
    )
    assert expected in text, (
        "is_pilot derivation does not match Commit 3f shape"
    )


# ---------------------------------------------------------------------
# Case 4: cross-file invariant -- the three changes ship together
# ---------------------------------------------------------------------


def test_all_three_files_reference_commit_3f():
    """Doc-truthing pin: each file modified by Commit 3f must mention
    'Commit 3f' in a comment so future readers can grep for the trail."""
    for path in (STRIPE_CLIENT, WEBHOOK_SVC, BILLING_API):
        text = path.read_text()
        assert "Commit 3f" in text, (
            f"{path.name} does not mention Commit 3f"
        )


def test_no_dangling_python_syntax_errors():
    """Defensive: each of the three modified files must still parse."""
    for path in (STRIPE_CLIENT, WEBHOOK_SVC, BILLING_API):
        ast.parse(path.read_text())  # raises on failure
