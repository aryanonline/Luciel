"""Step 30a: Stripe SDK wrapper.

A thin facade over the ``stripe`` library that:

  1. Pins the API version (so a Stripe-side default rollover does not
     silently change the shape of our webhook payloads).
  2. Centralizes the api_key configuration (so callers never touch the
     module-global ``stripe.api_key`` themselves and a unit test can
     patch one place instead of many).
  3. Verifies webhook signatures via ``stripe.Webhook.construct_event``,
     wrapped so the route-side caller gets a typed exception on failure
     and the audit chain can record a "bad-signature" attempt
     deterministically.
  4. Defers ALL Stripe imports to module load (no lazy import inside
     hot paths). The ``stripe`` Python lib is ~150 KB and import-only
     safe; a single top-level import is the right shape.

Why a wrapper instead of inlining ``stripe.checkout.Session.create``
in the service layer:

  - One place to flip the api_version on a future bump.
  - One place to inject the Idempotency-Key header on retries.
  - One place to mock the network surface in contract tests (the
     ``BillingService`` accepts a ``StripeClient`` in its constructor;
     tests inject a fake).
"""
from __future__ import annotations

import logging
from typing import Any

import stripe

# -----------------------------------------------------------------
# Pinned API version. Whenever we knowingly upgrade to a newer
# Stripe API, this constant changes, the webhook signature
# rotation runbook is exercised, and the integration tests are
# replayed against captured payloads from the new version.
# -----------------------------------------------------------------
STRIPE_API_VERSION = "2024-06-20"

logger = logging.getLogger(__name__)


class StripeSignatureError(Exception):
    """Raised when a Stripe webhook payload fails signature verification.

    Distinct from a generic ``ValueError`` so the webhook route can
    catch *only* the signature class and route it to the
    ACTION_BILLING_WEBHOOK_REPLAY_REJECTED audit row + 400 response.
    Any other Stripe-side exception is allowed to bubble.
    """


class StripeClient:
    """Per-process Stripe facade.

    Instantiated once at startup via ``app.integrations.stripe.get_stripe_client``
    and passed to the billing services. Stateless beyond the api_key
    + api_version, so test code can spin up a fresh instance with a
    test-mode key and discard it.
    """

    def __init__(self, *, api_key: str, webhook_secret: str | None = None) -> None:
        if not api_key:
            # We tolerate empty api_key at construction time -- the
            # billing routes fail closed on first use -- so the
            # backend still boots in environments without billing
            # configured (CI, dev tenants on Team / Company tiers).
            logger.info("StripeClient constructed with empty api_key; billing routes will 501.")
        self._api_key = api_key
        self._webhook_secret = webhook_secret or ""
        self.api_version = STRIPE_API_VERSION

    @property
    def is_configured(self) -> bool:
        """True iff the secret key is non-empty. Routes use this for the 501 gate."""
        return bool(self._api_key)

    @property
    def webhook_secret(self) -> str:
        return self._webhook_secret

    # -----------------------------------------------------------------
    # Checkout
    # -----------------------------------------------------------------

    def create_checkout_session(
        self,
        *,
        customer_email: str,
        price_id: str,
        success_url: str,
        cancel_url: str,
        trial_period_days: int | None = None,
        metadata: dict[str, str] | None = None,
        idempotency_key: str | None = None,
        intro_fee_price_id: str | None = None,
    ) -> Any:
        """Create a Stripe Checkout session in subscription mode.

        Returns the Stripe Session object (dict-like). The caller is
        responsible for handing the ``.url`` field back to the buyer
        and the ``.id`` back to the marketing site for the eventual
        claim flow.

        We intentionally do NOT create a Customer object up-front. The
        buyer is prospective at this point; if checkout is abandoned,
        we want zero Stripe-side artifacts to clean up. Stripe will
        create a Customer for us on a successful charge.

        Step 30a.2 — intro_fee_price_id ("Stripe Option A"):
          When passed, a SECOND line item is appended for the one-time
          $100 CAD intro fee Price (type=one_time). Combined with
          ``trial_period_days=90`` on the recurring line, Stripe Checkout
          renders "$100 due today, then $X/mo starting in 90 days". Stripe
          charges the one-time line at checkout and starts the recurring
          line's trial clock immediately; the first recurring invoice
          (at full rate) fires when the trial expires. The caller — and
          ONLY the caller — owns the first-time gate (see
          ``BillingService.is_first_time_customer``); passing a non-None
          intro_fee_price_id to a repeat customer is a programmer error.
        """
        stripe.api_key = self._api_key
        line_items: list[dict[str, Any]] = [{"price": price_id, "quantity": 1}]
        if intro_fee_price_id:
            # Order matters only for human readability in the Stripe-hosted
            # checkout UI; the intro fee renders first because buyers expect
            # to see the today-charge at the top.
            line_items.insert(0, {"price": intro_fee_price_id, "quantity": 1})

        params: dict[str, Any] = dict(
            mode="subscription",
            customer_email=customer_email,
            line_items=line_items,
            success_url=success_url,
            cancel_url=cancel_url,
            # Stripe-managed tax for CAD/GST/HST/PST/QST without us
            # having to hand-roll the table. Quietly degrades if the
            # account does not have Stripe Tax enabled, which is fine
            # for dev / test environments.
            automatic_tax={"enabled": True},
            # Echo metadata into the resulting subscription object so
            # the webhook handler can correlate without an extra GET.
            metadata=dict(metadata or {}),
            subscription_data={
                "metadata": dict(metadata or {}),
            },
            # The buyer can edit their address in checkout; we capture
            # it on the customer for the eventual invoice / receipt.
            billing_address_collection="auto",
            allow_promotion_codes=False,
            currency="cad",
        )
        if trial_period_days and trial_period_days > 0:
            params["subscription_data"]["trial_period_days"] = trial_period_days

        idem_kwargs: dict[str, Any] = {}
        if idempotency_key:
            idem_kwargs["idempotency_key"] = idempotency_key

        return stripe.checkout.Session.create(**params, **idem_kwargs)

    def retrieve_checkout_session(self, session_id: str) -> Any:
        stripe.api_key = self._api_key
        # ``expand`` returns the resolved subscription + customer so
        # the claim flow can answer "did the webhook arrive yet?"
        # without a follow-up round trip.
        return stripe.checkout.Session.retrieve(
            session_id,
            expand=["subscription", "customer"],
        )

    def retrieve_subscription(self, subscription_id: str) -> Any:
        """Retrieve the canonical Stripe Subscription by id.

        Step 30a.2-pilot Commit 3f: ``checkout.session.completed`` only
        carries the subscription id as a string plus an unreliable
        ``status`` field (it stores the *session* status, not the
        Subscription's). The Subscription object itself is the source
        of truth for ``status``, ``trial_end``, and
        ``current_period_start/end``. ``billing_webhook_service``
        uses this immediately after locating the sub id on the
        session, before writing the Subscription row, so the row is
        accurate on creation rather than waiting for the eventual
        ``customer.subscription.updated`` backfill.

        See drift
        ``D-stripe-webhook-checkout-vs-subscription-field-source-2026-05-15``.
        """
        stripe.api_key = self._api_key
        return stripe.Subscription.retrieve(subscription_id)

    # -----------------------------------------------------------------
    # Customer Portal
    # -----------------------------------------------------------------

    def create_portal_session(self, *, customer_id: str, return_url: str) -> Any:
        stripe.api_key = self._api_key
        return stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=return_url,
        )

    # -----------------------------------------------------------------
    # Step 30a.2-pilot: self-serve refund of the one-time $100 intro fee.
    #
    # Locates the Charge that corresponds to the intro-fee line item on
    # the subscription's first Invoice, refunds it in full, then cancels
    # the subscription. The two are done as separate Stripe API calls
    # because Stripe does not expose a combined refund-and-cancel primitive
    # in subscription mode; the caller (BillingService.process_pilot_refund)
    # wraps both writes inside a single DB transaction plus a tenant
    # cascade so the local state never drifts from Stripe's view.
    # -----------------------------------------------------------------

    def find_intro_charge_id(
        self,
        *,
        stripe_subscription_id: str,
        intro_fee_price_id: str,
    ) -> str | None:
        """Resolve the Charge id for the one-time $100 intro line item.

        Stripe's subscription-mode checkout creates a parent Invoice that
        contains both the trial recurring line ($0 today, prorated to 0)
        and the ``add_invoice_items`` one-time intro line ($100). The
        Invoice is paid via a single PaymentIntent which produces exactly
        one Charge that covers the whole amount. We return that Charge
        id; the caller refunds it in full.

        Returns None if Stripe cannot locate the Invoice or its Charge --
        the route layer maps None to HTTP 404, matching the same
        "refund-target-missing" semantics as a stale subscription that
        Stripe has already wiped.

        We deliberately do NOT verify the Charge's amount equals 10000
        cents (CAD $100.00) here. The amount-verification belongs in the
        service layer where the test-mode / live-mode Price values are
        consulted; this method is the I/O primitive only.
        """
        stripe.api_key = self._api_key
        try:
            sub = stripe.Subscription.retrieve(
                stripe_subscription_id,
                expand=["latest_invoice", "latest_invoice.payment_intent"],
            )
        except Exception:  # pragma: no cover - network boundary
            return None
        invoice = getattr(sub, "latest_invoice", None)
        if invoice is None:
            return None
        # Stripe-Python may return either an expanded Invoice object or
        # the bare invoice id depending on the API version; normalize.
        if isinstance(invoice, str):
            try:
                invoice = stripe.Invoice.retrieve(
                    invoice, expand=["payment_intent"]
                )
            except Exception:  # pragma: no cover - network boundary
                return None
        # Stripe 2026-04 returns Charge id on Invoice.charge as the
        # legacy path AND on PaymentIntent.latest_charge as the modern
        # path. Try the modern path first.
        payment_intent = getattr(invoice, "payment_intent", None)
        if payment_intent is not None and not isinstance(payment_intent, str):
            latest_charge = getattr(payment_intent, "latest_charge", None)
            if isinstance(latest_charge, str) and latest_charge:
                return latest_charge
            if latest_charge is not None and getattr(latest_charge, "id", None):
                return latest_charge.id
        legacy_charge = getattr(invoice, "charge", None)
        if isinstance(legacy_charge, str) and legacy_charge:
            return legacy_charge
        if legacy_charge is not None and getattr(legacy_charge, "id", None):
            return legacy_charge.id
        return None

    def refund_charge(self, *, charge_id: str, idempotency_key: str | None = None) -> Any:
        """Refund the given Charge in full.

        Stripe will refund whatever amount remains on the Charge -- if
        the charge has already been partially refunded, this completes
        the refund. The Refund object's ``id`` (re_...) is what we audit.
        """
        stripe.api_key = self._api_key
        idem_kwargs: dict[str, Any] = {}
        if idempotency_key:
            idem_kwargs["idempotency_key"] = idempotency_key
        return stripe.Refund.create(charge=charge_id, **idem_kwargs)

    def cancel_subscription(self, *, stripe_subscription_id: str) -> Any:
        """Cancel a subscription immediately (no proration).

        Stripe will fire ``customer.subscription.deleted`` to our webhook
        on the next event delivery; that handler is idempotent against
        an already-flipped subscription row, so the local cancel we do
        synchronously in process_pilot_refund will not be undone or
        double-applied.
        """
        stripe.api_key = self._api_key
        # invoice_now=False + prorate=False: do not bill any remaining
        # cycle since the intro fee was the entire "due today" amount
        # and we are refunding it; an outstanding proration would defeat
        # the refund.
        return stripe.Subscription.cancel(
            stripe_subscription_id,
            invoice_now=False,
            prorate=False,
        )

    # -----------------------------------------------------------------
    # Webhook signature verification
    # -----------------------------------------------------------------

    def construct_event(self, *, payload: bytes, sig_header: str) -> Any:
        """Verify and parse an inbound webhook payload.

        Raises ``StripeSignatureError`` on any failure -- bad signature,
        unparseable JSON, or absent secret. The caller MUST catch this
        and emit an ACTION_BILLING_WEBHOOK_REPLAY_REJECTED audit row.
        """
        if not self._webhook_secret:
            raise StripeSignatureError("Stripe webhook secret is not configured.")
        try:
            return stripe.Webhook.construct_event(
                payload=payload,
                sig_header=sig_header,
                secret=self._webhook_secret,
            )
        except stripe.error.SignatureVerificationError as exc:  # pragma: no cover - boundary
            raise StripeSignatureError(f"Stripe signature verification failed: {exc}") from exc
        except (ValueError, KeyError) as exc:  # pragma: no cover - boundary
            raise StripeSignatureError(f"Unparseable Stripe payload: {exc}") from exc


# ---------------------------------------------------------------------
# Module-level accessor
# ---------------------------------------------------------------------

_CLIENT: StripeClient | None = None


def get_stripe_client() -> StripeClient:
    """Lazy singleton accessor.

    Reads settings at first call so test code that injects env vars
    via ``monkeypatch.setenv`` before app startup observes the right
    values. Subsequent calls return the cached client.
    """
    global _CLIENT
    if _CLIENT is None:
        # Local import keeps the integration package importable even
        # when ``app.core.config`` cannot load (e.g. during a tooling
        # invocation that does not set DATABASE_URL).
        from app.core.config import settings

        _CLIENT = StripeClient(
            api_key=settings.stripe_secret_key,
            webhook_secret=settings.stripe_webhook_secret,
        )
    return _CLIENT


def reset_stripe_client() -> None:
    """Test hook -- clears the cached client so the next ``get_stripe_client``
    re-reads ``settings``. Production code never calls this."""
    global _CLIENT
    _CLIENT = None
