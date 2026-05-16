"""Step 30a.2-pilot Commit 3h — contract tests.

Commit 3g made the write-path eligibility metadata-driven, but the
first live click after :57 rolled produced a NEW failure: the toast
"We couldn't find the pilot charge to refund. Contact support."

Diagnosis (via live ECS-exec probing of the real Stripe state for
``sub_1TXUN1RytQVRVXw7wmsfATXJ``):

  * Stripe API version on file: ``2026-04-22.dahlia`` (basil).
  * ``Subscription.latest_invoice`` returns a paid invoice
    (``amount_paid=10000``, ``status=paid``) but ``invoice.charge`` is
    None and ``invoice.payment_intent`` is None. Basil 2025+ deprecated
    those Invoice-level fields and moved the payment linkage to a
    separate resource: ``InvoicePayment``.
  * ``stripe.InvoicePayment.list(invoice=<id>)`` returns one row whose
    ``payment`` block is ``{'payment_intent': 'pi_...', 'type':
    'payment_intent'}``. ``PaymentIntent.retrieve(pi_id).latest_charge``
    is the Charge id we need (``py_...``).

Our pre-3h ``find_intro_charge_id`` looked only at the two deprecated
Invoice fields, so it returned None for every live row produced by the
basil-2025+ API, and the route layer raised PilotChargeNotFoundError.

Commit 3h adds the InvoicePayment-based traversal as the new primary
path, with the two legacy paths preserved as fallbacks (test fixtures,
older API versions, mocked SDKs). Assertions here are pure AST/text
pins -- no Stripe SDK calls, no live network.
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
STRIPE_CLIENT = ROOT / "app" / "integrations" / "stripe" / "client.py"


def _module(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _function(mod: ast.Module, name: str) -> ast.FunctionDef:
    for node in mod.body:
        if isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef) and sub.name == name:
                    return sub
    raise AssertionError(f"function {name} not found")


# ---------------------------------------------------------------------
# Case 1: InvoicePayment.list is invoked
# ---------------------------------------------------------------------
def test_find_intro_charge_id_uses_invoice_payment_list():
    """The basil-2025+ primary path must call
    ``stripe.InvoicePayment.list(invoice=...)``.
    """
    src = STRIPE_CLIENT.read_text(encoding="utf-8")
    assert "stripe.InvoicePayment.list" in src, (
        "Commit 3h must add a stripe.InvoicePayment.list call to "
        "find_intro_charge_id. This is the basil-2025+ primary path "
        "for resolving an invoice's payment linkage."
    )


# ---------------------------------------------------------------------
# Case 2: invoice keyword filter is used
# ---------------------------------------------------------------------
def test_invoice_payment_list_filters_by_invoice():
    """The InvoicePayment.list call must filter by the invoice id, not
    by customer or by sweeping the whole account.
    """
    src = STRIPE_CLIENT.read_text(encoding="utf-8")
    # The exact textual shape from the implementation.
    assert "stripe.InvoicePayment.list(invoice=invoice_id" in src, (
        "InvoicePayment.list must be called with invoice=invoice_id. "
        "Filtering by customer or by no filter would pull unrelated "
        "payments and may return the wrong charge for refund."
    )


# ---------------------------------------------------------------------
# Case 3: payment.payment_intent is read from the InvoicePayment dict
# ---------------------------------------------------------------------
def test_invoice_payment_payment_intent_field_is_read():
    """The traversal must reach into the nested ``payment`` block on
    each InvoicePayment and read ``payment_intent`` from it.
    """
    src = STRIPE_CLIENT.read_text(encoding="utf-8")
    assert 'payment_block.get("payment_intent")' in src, (
        "find_intro_charge_id must read payment_intent from the "
        "payment block of each InvoicePayment row."
    )


# ---------------------------------------------------------------------
# Case 4: PaymentIntent.retrieve is called for each candidate
# ---------------------------------------------------------------------
def test_payment_intent_retrieve_is_invoked_for_candidate():
    """For each InvoicePayment row with a payment_intent reference, the
    traversal must fetch the PaymentIntent to read ``latest_charge``.
    """
    src = STRIPE_CLIENT.read_text(encoding="utf-8")
    assert "stripe.PaymentIntent.retrieve" in src, (
        "find_intro_charge_id must call stripe.PaymentIntent.retrieve "
        "to read latest_charge from each candidate PaymentIntent."
    )


# ---------------------------------------------------------------------
# Case 5: legacy modern-path (invoice.payment_intent.latest_charge) preserved
# ---------------------------------------------------------------------
def test_legacy_modern_path_preserved():
    """The pre-3h modern path (invoice.payment_intent.latest_charge)
    must remain as a fallback so test fixtures and older API versions
    keep working.
    """
    src = STRIPE_CLIENT.read_text(encoding="utf-8")
    assert 'getattr(invoice, "payment_intent", None)' in src, (
        "find_intro_charge_id must preserve the invoice.payment_intent "
        "fallback as a secondary path."
    )
    assert 'getattr(payment_intent, "latest_charge", None)' in src, (
        "find_intro_charge_id must still read latest_charge off the "
        "payment_intent in the legacy modern path."
    )


# ---------------------------------------------------------------------
# Case 6: legacy oldest path (invoice.charge) preserved
# ---------------------------------------------------------------------
def test_legacy_oldest_path_preserved():
    """The pre-3h oldest path (invoice.charge) must remain as the
    final fallback.
    """
    src = STRIPE_CLIENT.read_text(encoding="utf-8")
    assert 'getattr(invoice, "charge", None)' in src, (
        "find_intro_charge_id must preserve the invoice.charge "
        "fallback as the oldest legacy path."
    )


# ---------------------------------------------------------------------
# Case 7: traversal order -- InvoicePayment first, then legacy
# ---------------------------------------------------------------------
def test_invoice_payment_path_comes_before_legacy():
    """The basil-2025+ InvoicePayment traversal must run BEFORE the
    legacy invoice.payment_intent and invoice.charge fallbacks, so
    live data is resolved through the canonical basil path and the
    legacy paths only fire when InvoicePayment is empty.
    """
    src = STRIPE_CLIENT.read_text(encoding="utf-8")
    ip_pos = src.find("stripe.InvoicePayment.list")
    pi_legacy_pos = src.find('getattr(invoice, "payment_intent", None)')
    charge_legacy_pos = src.find('getattr(invoice, "charge", None)')
    assert ip_pos > 0, "InvoicePayment.list call not found"
    assert pi_legacy_pos > ip_pos, (
        "invoice.payment_intent fallback must come AFTER the "
        "InvoicePayment.list primary path."
    )
    assert charge_legacy_pos > pi_legacy_pos, (
        "invoice.charge fallback must come AFTER both the "
        "InvoicePayment primary and the invoice.payment_intent "
        "secondary."
    )


# ---------------------------------------------------------------------
# Case 8: returns None when every path is exhausted
# ---------------------------------------------------------------------
def test_returns_none_when_all_paths_exhausted():
    """The trailing ``return None`` after all fallbacks must remain
    so genuinely missing charges still produce a 404 at the route
    layer (matching the existing PilotChargeNotFoundError semantics).
    """
    src = STRIPE_CLIENT.read_text(encoding="utf-8")
    # The function must still end with `return None` after the legacy
    # block (we pin by the unique terminator pattern that appears
    # only inside find_intro_charge_id's tail).
    lines = src.splitlines()
    in_fn = False
    saw_terminal = False
    for idx, line in enumerate(lines):
        if "def find_intro_charge_id" in line:
            in_fn = True
            continue
        if in_fn and line.startswith("    def "):
            # next method -- stop
            break
        if in_fn and line.strip() == "return None":
            saw_terminal = True
    assert saw_terminal, (
        "find_intro_charge_id must end with a `return None` after "
        "all fallback paths have been tried."
    )


# ---------------------------------------------------------------------
# Case 9: traversal is exception-safe at every Stripe boundary
# ---------------------------------------------------------------------
def test_each_stripe_call_wrapped_in_try_except():
    """All three Stripe calls in the new primary path
    (InvoicePayment.list, PaymentIntent.retrieve) must be wrapped in
    try/except so a network error or SDK-version-mismatch does not
    crash the whole refund flow -- the function should fall through
    to the legacy paths or to the final None.
    """
    mod = _module(STRIPE_CLIENT)
    fn = _function(mod, "find_intro_charge_id")
    # Count `Try` nodes that contain a Stripe call.
    try_count = 0
    for node in ast.walk(fn):
        if isinstance(node, ast.Try):
            for child in ast.walk(node):
                if (
                    isinstance(child, ast.Attribute)
                    and isinstance(child.value, ast.Name)
                    and child.value.id == "stripe"
                ):
                    try_count += 1
                    break
    # Pre-3h had 2 try-wrapped Stripe calls (Subscription.retrieve,
    # Invoice.retrieve). 3h adds 2 more (InvoicePayment.list,
    # PaymentIntent.retrieve). The minimum is 4 to be safe.
    assert try_count >= 4, (
        f"find_intro_charge_id must wrap at least 4 Stripe calls in "
        f"try/except (Subscription.retrieve, Invoice.retrieve, "
        f"InvoicePayment.list, PaymentIntent.retrieve). Found "
        f"{try_count}."
    )


# ---------------------------------------------------------------------
# Case 10: invoice_id is captured for the InvoicePayment.list call
# ---------------------------------------------------------------------
def test_invoice_id_is_captured_for_list_filter():
    """The new traversal needs the invoice id as a string for the
    InvoicePayment.list filter. The function must normalize the
    Stripe SDK's expanded-vs-bare-id return shapes into a local
    string named ``invoice_id``.
    """
    src = STRIPE_CLIENT.read_text(encoding="utf-8")
    assert "invoice_id: str | None" in src or "invoice_id = invoice" in src, (
        "find_intro_charge_id must capture the invoice id into a "
        "local variable named invoice_id so it can be passed to "
        "InvoicePayment.list."
    )


# ---------------------------------------------------------------------
# Case 11: Subscription.retrieve still expands latest_invoice
# ---------------------------------------------------------------------
def test_subscription_retrieve_still_expands_latest_invoice():
    """The Subscription.retrieve call must continue to expand
    latest_invoice so the no-roundtrip happy path still works for
    pre-basil API versions.
    """
    src = STRIPE_CLIENT.read_text(encoding="utf-8")
    assert 'expand=["latest_invoice", "latest_invoice.payment_intent"]' in src, (
        "Subscription.retrieve must still expand "
        "latest_invoice + latest_invoice.payment_intent. The new "
        "primary path uses latest_invoice; the legacy paths use "
        "latest_invoice.payment_intent."
    )


# ---------------------------------------------------------------------
# Case 12: docstring records the basil 2025+ traversal contract
# ---------------------------------------------------------------------
def test_docstring_records_commit_3h_traversal_contract():
    """Future maintainers must be able to grep the source for the
    Commit 3h contract: which paths exist, in what order, and why.
    """
    mod = _module(STRIPE_CLIENT)
    fn = _function(mod, "find_intro_charge_id")
    doc = ast.get_docstring(fn) or ""
    assert "InvoicePayment" in doc, (
        "find_intro_charge_id's docstring must reference "
        "InvoicePayment so a future reader can find the basil-2025+ "
        "primary path."
    )
    assert "Commit 3h" in doc, (
        "find_intro_charge_id's docstring must reference Commit 3h "
        "so the change is greppable from git log."
    )
