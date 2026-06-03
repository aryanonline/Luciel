"""Step 30a / 30a.2: outbound transactional email.

v1 behaviour preserved:
  - Single function, ``send_magic_link_email``.
  - The function is synchronous and bounded -- the webhook handler must
    return 2xx quickly to Stripe, so the email send must not block longer
    than a few hundred ms. Boto3 SES ``send_email`` is RTT-bounded by the
    region's SES endpoint (~50-200ms from a co-located task), well inside
    that contract. SES failure modes (throttling, throttle-then-recover,
    region failure) are surfaced as :class:`MagicLinkError` so the caller
    can record an audit row and either fail-loud at the API surface or
    swallow-and-200 at the webhook surface (see
    ``app/services/billing_webhook_service.py`` for the swallow contract).

Step 30a.2 wiring:
  - Real SES delivery via ``boto3.client('sesv2')``. Region is read from
    the ``SES_REGION`` environment variable, defaulting to the AWS region
    the task is running in (``AWS_REGION``), which is ``ca-central-1`` in
    production. The task's IAM role (``luciel-ecs-web-role``) carries the
    ``LucielSESSendEmail`` inline policy scoped to the verified
    ``vantagemind.ai`` identity ARN; no credentials are read here.
  - Backwards-compatible fallback: when ``LUCIEL_EMAIL_TRANSPORT=log``
    is set (the local-dev convention), the function logs the body
    instead of sending. The stable ``[magic-link-email]`` log marker is
    emitted on **both** paths so the e2e harness (and the contract test
    in ``tests/api/test_step30a_billing_shape.py``) keeps passing.
  - Future ``EmailProvider`` ABC remains the right direction for adding a
    second provider (Postmark/Resend/etc.); this commit lands the SES
    integration directly to close ``D-email-service-log-only-no-real-
    delivery-2026-05-14`` without taking on that abstraction in the same
    revision. The single read site for transport selection is the
    ``_transport()`` helper below, which is the seam for that future
    refactor.
"""
from __future__ import annotations

import logging
import os
from typing import Final
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from sqlalchemy.orm import Session

from app.core.config import settings

logger = logging.getLogger(__name__)


# Arc 8 WU-6 -- application-layer SES suppression precheck.
#
# Every outbound send_email call in this module performs a precheck via
# EmailSuppressionService.is_suppressed BEFORE invoking boto3.
# SuppressedRecipientError is the precheck-rejection error -- it is a
# distinct class from MagicLinkError / WelcomeEmailError / RefundEmailError
# so a caller can tell the difference between "SES rejected the send" and
# "the application refused to call SES". The same caller pattern (catch
# at the webhook boundary; audit + return 200 to Stripe; raise at the API
# boundary) applies to both: the suppression rejection is just a faster
# path that doesn't burn the SES API call.
#
# The send functions accept an optional ``db`` Session for callers that
# already hold one (the webhook / API path). When ``db`` is None, we open
# a short-lived session via SessionLocal for the precheck only -- the
# send path itself never writes to the database. This keeps the existing
# kwargs-only signatures backward-compatible.
from app.services.email_suppression_service import (  # noqa: E402
    SuppressedRecipientError,
    is_suppressed as _is_address_suppressed,
)

__all_suppression__ = ("SuppressedRecipientError",)


def _precheck_suppression(to_email: str, db: Session | None, marker: str) -> None:
    """Raise SuppressedRecipientError if ``to_email`` is on the suppression list.

    Opens a short-lived session via SessionLocal when the caller did not
    provide one. The session is read-only for this lookup. Lookup
    failures (DB unreachable, etc.) are logged and treated as
    fail-open -- a suppression check that itself fails MUST NOT take
    down the send path; the operational alarm is the log line, not a
    raised exception. The downstream SES send will still fail loudly
    if the address is unrecoverable.

    ``marker`` is the log-marker prefix of the calling send function
    (e.g. ``[magic-link-email]``) so a suppression-shortcircuit log
    line is grep-able alongside the existing send-attempt log lines.
    """
    try:
        if db is not None:
            suppressed = _is_address_suppressed(db, to_email)
        else:
            # Lazy import to avoid a circular import at module-load time
            # (app.db.session imports app.repositories.audit_chain which
            # imports app.models.admin_audit_log -- the chain is acyclic
            # but loaded lazily here for symmetry with boto3 below).
            from app.db.session import SessionLocal  # noqa: WPS433

            with SessionLocal() as scoped:
                suppressed = _is_address_suppressed(scoped, to_email)
    except Exception:  # noqa: BLE001
        logger.exception(
            "%s suppression precheck failed; failing OPEN (continuing to "
            "SES send). Investigate immediately -- a precheck outage "
            "means HardBounce / Complaint short-circuit is degraded.",
            marker,
        )
        return

    if suppressed:
        logger.warning(
            "%s suppression precheck SHORT-CIRCUITED to=%s (address on "
            "application-layer suppression list; SES call skipped).",
            marker,
            to_email,
        )
        raise SuppressedRecipientError(
            f"Address {to_email!r} is on the application-layer "
            f"suppression list; outbound send refused."
        )


# Arc 2 (2026-05-20) -- D-set-password-token-logged-plaintext-2026-05-17 fix.
# Every token-bearing URL emitted from this module passes through
# `_redact_token_url` before being passed to `logger.info`/`logger.warning`.
# The redacted form preserves the URL path (useful for ops debugging --
# "was this a magic-link or a set-password link?") and strips the
# `token` query param to the literal string `<redacted>`. Other query
# params (if any) are preserved verbatim. The path-only URL never
# carries the signed HS256 JWT, so a `logs:GetLogEvents` reader on the
# CloudWatch log group cannot lift a usable invite/welcome/reset link.
# The plaintext URL is still available in-process for the SES `Body`
# field (which is the only legitimate carrier of the token: the
# customer's inbox). See DRIFTS `~~D-set-password-token-logged-
# plaintext-2026-05-17~~` for the closing stanza and full audit trail.
def _redact_token_url(url: str) -> str:
    """Strip the `token=...` query param from a URL for safe logging.

    The returned URL is path-and-fragment identical to the input, with
    the `token` query param replaced by the literal `<redacted>`. All
    other query params are preserved verbatim. If the URL has no
    `token` query param, the URL is returned unchanged.

    This is a defensive emitter-layer redaction -- the in-process URL
    value is unchanged, and the email body still carries the real
    token-bearing link to the customer's inbox (the only legitimate
    carrier). Only the CloudWatch log line is sanitized.
    """
    try:
        parts = urlsplit(url)
        query_pairs = parse_qsl(parts.query, keep_blank_values=True)
        if not any(k == "token" for k, _ in query_pairs):
            return url
        redacted_pairs = [
            (k, "<redacted>" if k == "token" else v) for k, v in query_pairs
        ]
        new_query = urlencode(redacted_pairs)
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, new_query, parts.fragment)
        )
    except (ValueError, TypeError):
        # If urllib chokes on a malformed URL (e.g. None, empty string,
        # non-string input), fall back to the literal `<redacted-url>`
        # rather than emit the original. Belt-and-suspenders: the
        # caller never sees a logged secret even on a parse failure.
        return "<redacted-url>"


# Arc 2 (2026-05-20) -- the body-logging legs of the SES failure path
# also need to be sanitized because the email body itself carries the
# token-bearing link as plain text (the customer needs to click it).
# `_redact_body` rewrites every URL-with-token-query in the body to its
# `_redact_token_url` form. Conservative regex: matches `https://...?...`
# strings up to the first whitespace or end-of-string, redacts each
# match in place. Non-token URLs (e.g. the legal/support links in the
# email footer, if any) are untouched because they have no `token=`
# query param.
import re

_TOKEN_URL_PATTERN: Final = re.compile(
    r"https?://[^\s]*[?&]token=[^\s&]+(?:&[^\s]*)?"
)


def _redact_body(body: str) -> str:
    """Rewrite every token-bearing URL in an email body to its redacted form.

    Used on the SES-failure log path where the full body is emitted so
    on-call can manually relay. Body is logged with token query param
    stripped; the in-process body sent to SES is unchanged.
    """
    if not body:
        return body
    return _TOKEN_URL_PATTERN.sub(
        lambda m: _redact_token_url(m.group(0)), body
    )


SUBJECT_MAGIC_LINK: Final[str] = "Your VantageMind login link"
SUBJECT_PILOT_REFUND: Final[str] = "Your VantageMind pilot has been refunded"
# Step 30a.3: subject lines for the three first-password-set surfaces.
# Subject differs by purpose so a buyer who pays $100 and immediately
# pays $300 (signup -> upgrade) does not see two identical-looking
# "set your password" emails in their inbox. Subject copy is locked to
# the CANONICAL_RECAP §12 Step 30a.3 row.
# Arc 2 (2026-05-20) -- D-welcome-email-subject-mojibake-2026-05-17: SES v2
# `send_email` with `Simple.Subject` does not RFC-2047-wrap the header, so
# any U+2019 (right-single-quote) or U+2014 (em-dash) in the subject
# degrades to `Æ` / `ù` in Gmail's local-fallback decoding (Latin-1 path).
# Per drift resolution path option (a), subjects are now ASCII-only;
# body copy keeps typographer's quotes/dashes because Body.Text.Data is
# carried with explicit `Charset="UTF-8"` and decodes correctly.
# Arc 18 (§3.4.1b) — conversation-budget alert subjects. ASCII-only per the
# SES Simple.Subject mojibake constraint documented above. Distinct subjects
# per threshold so an admin who crosses 80% then 100% sees two separable
# inbox entries rather than one overwritten alert.
SUBJECT_BUDGET_ALERT_80: Final[str] = "VantageMind: conversation budget at 80%"
SUBJECT_BUDGET_ALERT_100: Final[str] = "VantageMind: conversation budget reached"
SUBJECT_BUDGET_EXHAUSTED: Final[str] = "VantageMind: conversation budget exhausted"
SUBJECT_WELCOME_SET_PASSWORD: Final[str] = "Welcome to VantageMind - set your password"
SUBJECT_INVITE_SET_PASSWORD: Final[str] = "You've been invited to VantageMind - set your password"
SUBJECT_RESET_PASSWORD: Final[str] = "Reset your VantageMind password"
_LOG_TRANSPORT: Final[str] = "log"
_SES_TRANSPORT: Final[str] = "ses"


class MagicLinkError(RuntimeError):
    """Raised when the magic-link email cannot be delivered.

    Callers in the webhook path catch this and record an audit row while
    still returning 200 to Stripe; callers in the synchronous API path
    surface it as a 5xx so the caller's UI can show a retry affordance.
    """


class WelcomeEmailError(RuntimeError):
    """Raised when the welcome / set-password / reset email cannot be delivered.

    Step 30a.3: the email is the load-bearing claim of "password mandatory
    at signup" -- if SES is unreachable when the Stripe webhook commits
    the User row, the buyer never receives the welcome link and cannot
    set a password. The webhook handler catches this AFTER ``db.commit()``
    (same swallow-and-audit posture as ``send_magic_link_email``) so the
    payment + subscription rows stay correct; on the next backend boot
    the on-call dashboard surfaces the missed delivery and the operator
    can manually relay a ``/forgot-password`` link from CloudWatch.

    The API-path caller (``POST /api/v1/auth/forgot-password``) catches
    this and returns the same generic 200 it always returns -- the SES
    failure is invisible to a probing client.
    """


class RefundEmailError(RuntimeError):
    """Raised when the pilot-refund confirmation email cannot be delivered.

    Step 30a.2-pilot Commit 3j: the post-refund customer email is a
    courtesy/polish leg that is OUT OF BAND from the refund cascade itself.
    Callers in ``BillingService.process_pilot_refund`` catch this AFTER the
    refund / cancel / DB cascade has already committed, write a follow-up
    audit row with action='pilot_refund_email_send_failed', and do NOT roll
    back the cascade. The customer has been made whole financially and the
    on-page success surface has already rendered the refund id; the email
    is the third confirmation leg only. This is the same swallow-and-audit
    posture the webhook handlers use against Stripe.
    """


class BudgetAlertEmailError(RuntimeError):
    """Raised when a conversation-budget alert email cannot be delivered.

    Arc 18 (§3.4.1b): the budget alert is a best-effort admin notification
    leg, NOT a transactional one. The metering counter and (for the Free
    exhausted case) the customer's graceful handoff have already happened;
    callers catch this and degrade to a warning + audit row rather than
    crashing the turn or the webhook. Same swallow-and-audit posture as
    :class:`RefundEmailError`.
    """


def _transport() -> str:
    """Return the configured email transport: ``ses`` or ``log``."""
    raw = (os.getenv("LUCIEL_EMAIL_TRANSPORT") or _SES_TRANSPORT).strip().lower()
    if raw not in {_SES_TRANSPORT, _LOG_TRANSPORT}:
        # Unknown value -> fail closed to log so we never silently mis-route.
        logger.warning(
            "[magic-link-email] unknown LUCIEL_EMAIL_TRANSPORT=%r; falling back to %r",
            raw,
            _LOG_TRANSPORT,
        )
        return _LOG_TRANSPORT
    return raw


def _build_body(to_email: str, magic_link_url: str, display_name: str | None) -> str:
    salutation = display_name or to_email
    return (
        f"Hi {salutation},\n\n"
        f"Thanks for signing up for VantageMind. Click the link below to "
        f"finish setting up your account:\n\n"
        f"  {magic_link_url}\n\n"
        f"This link expires in {settings.magic_link_ttl_hours} hours. "
        f"If you did not initiate this, you can ignore this email.\n\n"
        f"-- The VantageMind team\n"
    )


def send_magic_link_email(
    *,
    to_email: str,
    magic_link_url: str,
    display_name: str | None = None,
    db: Session | None = None,
) -> None:
    """Send (or log) a magic-link email.

    Behaviour:
      - With ``LUCIEL_EMAIL_TRANSPORT=ses`` (production default): sends
        through Amazon SES v2 ``send_email``. On any SES failure, logs the
        full body at WARNING (so the on-call engineer can manually relay)
        AND raises :class:`MagicLinkError` so the caller's audit row is
        accurate. The ``[magic-link-email]`` marker is emitted on both
        the success log line and the failure log line so the e2e harness
        keeps working regardless of the delivery outcome.
      - With ``LUCIEL_EMAIL_TRANSPORT=log`` (local dev): logs the body
        only. Useful for offline development and for the existing
        contract test, which greps the source for the marker string.

    Arc 8 WU-6: BEFORE either branch, the address is prechecked against
    the application-layer suppression list. If suppressed,
    :class:`SuppressedRecipientError` is raised and neither SES nor the
    log path is touched. The optional ``db`` Session lets the caller
    pass in its own session (the webhook / API path); when None, a
    short-lived session is opened for the precheck only.

    The stable marker prefix `[magic-link-email]` lets the e2e
    harness (tests/e2e/step_30a_live_e2e.py) assert the URL was
    produced without needing a real mailbox to read from.
    """
    # Arc 8 WU-6 -- application-layer suppression precheck. Runs in
    # BOTH transports (log and ses) because the suppression list is the
    # application-layer source of truth; bypassing it in log mode would
    # let local dev send to an address production refuses to send to.
    _precheck_suppression(to_email, db, "[magic-link-email]")

    body = _build_body(to_email, magic_link_url, display_name)
    transport = _transport()

    if transport == _LOG_TRANSPORT:
        logger.warning(
            "[magic-link-email] (log-only transport) from=%s to=%s subject=%r url=%s\n%s",
            settings.from_email,
            to_email,
            SUBJECT_MAGIC_LINK,
            _redact_token_url(magic_link_url),
            _redact_body(body),
        )
        return

    # Real SES delivery. Boto3 is imported lazily so the log-only path
    # (used in unit tests and local dev) does not require boto3 to be
    # importable; the production task always has it installed.
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError as exc:  # pragma: no cover - prod always has boto3
        logger.exception("[magic-link-email] boto3 unavailable; cannot send")
        raise MagicLinkError("boto3 is not installed") from exc

    region = (
        os.getenv("SES_REGION")
        or os.getenv("AWS_REGION")
        or os.getenv("AWS_DEFAULT_REGION")
        or "ca-central-1"
    )

    try:
        client = boto3.client("sesv2", region_name=region)
        # Arc 8 WU-6 -- ConfigurationSetName activates the feedback
        # event destination (Bounce / Complaint / Reject /
        # RenderingFailure -> SNS topic luciel-ses-events -> backend
        # POST /api/v1/ses-events). ReplyToAddresses routes any buyer
        # reply into the monitored support inbox instead of the
        # unmonitored noreply mailbox.
        response = client.send_email(
            FromEmailAddress=settings.from_email,
            Destination={"ToAddresses": [to_email]},
            ReplyToAddresses=[settings.ses_reply_to_address],
            ConfigurationSetName=settings.ses_configuration_set_name,
            Content={
                "Simple": {
                    "Subject": {"Data": SUBJECT_MAGIC_LINK, "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
                },
            },
        )
        message_id = response.get("MessageId", "<unknown>")
        logger.info(
            "[magic-link-email] sent via SES from=%s to=%s subject=%r url=%s message_id=%s",
            settings.from_email,
            to_email,
            SUBJECT_MAGIC_LINK,
            _redact_token_url(magic_link_url),
            message_id,
        )
    except (ClientError, BotoCoreError) as exc:
        # Log the redacted body so on-call can manually relay (after
        # re-minting the token via `/forgot-password`) if SES is down,
        # then raise so the caller's audit row records the failure.
        # Arc 2 (2026-05-20): both url and body now pass through
        # `_redact_token_url` / `_redact_body` before logging --
        # CloudWatch never carries an unredeemed token.
        logger.warning(
            "[magic-link-email] SES send FAILED from=%s to=%s subject=%r url=%s error=%s\n%s",
            settings.from_email,
            to_email,
            SUBJECT_MAGIC_LINK,
            _redact_token_url(magic_link_url),
            exc,
            _redact_body(body),
        )
        raise MagicLinkError(f"SES send_email failed: {exc}") from exc


def _build_welcome_set_password_body(
    *,
    to_email: str,
    set_password_url: str,
    display_name: str | None,
    purpose: str,
) -> str:
    """Render the plaintext body for the welcome / invite / reset email.

    Body copy varies by ``purpose``:

      * ``signup``  -- the post-Checkout welcome. Frames the link as the
        final step of account setup so the buyer understands why they
        need to click it.
      * ``invite``  -- the team / company invite-acceptance. Frames the
        link as "you've been invited" so the recipient knows what
        organisation they are joining.
      * ``reset``   -- the /forgot-password recovery. Frames the link
        as a password reset and reminds the user that the link expires.

    All three variants point at the same ``/auth/set-password`` page;
    the page reads the token's ``typ`` claim and (in the invite case)
    the ``purpose`` claim to render the right header.
    """
    salutation = display_name or to_email
    if purpose == "signup":
        opener = (
            "Welcome to VantageMind. Your subscription is active. To finish "
            "setting up your account, choose a password using the link below:"
        )
        closer = (
            "After you set a password you'll be signed in automatically. "
            "From then on, you can log in any time at "
            f"{settings.marketing_site_url.rstrip('/')}/login with your email "
            "and password \u2014 no inbox round-trip required."
        )
    elif purpose == "invite":
        opener = (
            "You've been invited to VantageMind. To accept the invitation, "
            "choose a password using the link below:"
        )
        closer = (
            "After you set a password you'll be signed in automatically and "
            "land on your team's workspace."
        )
    else:  # reset (or any unknown purpose, defensive)
        opener = (
            "We received a request to reset your VantageMind password. "
            "To choose a new password, use the link below:"
        )
        closer = (
            "If you did not request a password reset, you can safely "
            "ignore this email \u2014 your current password remains in effect."
        )

    return (
        f"Hi {salutation},\n\n"
        f"{opener}\n\n"
        f"  {set_password_url}\n\n"
        f"This link expires in {settings.magic_link_ttl_hours} hours.\n\n"
        f"{closer}\n\n"
        f"-- The VantageMind team\n"
    )


def send_welcome_set_password_email(
    *,
    to_email: str,
    set_password_url: str,
    display_name: str | None = None,
    purpose: str = "signup",
    db: Session | None = None,
) -> None:
    """Send (or log) the welcome / invite / reset email for password set.

    Step 30a.3 Option-B welcome-email mechanic. The webhook path calls
    this with ``purpose='signup'`` after committing the User row; the
    Step 30a.4 / 30a.5 invite flows call it with ``purpose='invite'``;
    the ``POST /api/v1/auth/forgot-password`` route calls it with
    ``purpose='reset'``.

    Behaviour mirrors :func:`send_magic_link_email` and
    :func:`send_pilot_refund_email` exactly:
      * Transport selection via ``LUCIEL_EMAIL_TRANSPORT=ses|log``.
      * Log-only transport emits the stable marker
        ``[welcome-set-password-email]`` for e2e harness scraping.
      * SES delivery uses sesv2 ``send_email`` from the task IAM role's
        SES inline policy on the ``vantagemind.ai`` identity.
      * On SES failure: logs the full body at WARNING + raises
        :class:`WelcomeEmailError`.
    """
    # Arc 8 WU-6 -- application-layer suppression precheck. See
    # send_magic_link_email for the design contract; same gate, same
    # SuppressedRecipientError surface.
    _precheck_suppression(to_email, db, "[welcome-set-password-email]")

    body = _build_welcome_set_password_body(
        to_email=to_email,
        set_password_url=set_password_url,
        display_name=display_name,
        purpose=purpose,
    )
    transport = _transport()

    if purpose == "signup":
        subject = SUBJECT_WELCOME_SET_PASSWORD
    elif purpose == "invite":
        subject = SUBJECT_INVITE_SET_PASSWORD
    else:
        subject = SUBJECT_RESET_PASSWORD

    if transport == _LOG_TRANSPORT:
        logger.warning(
            "[welcome-set-password-email] (log-only transport) "
            "from=%s to=%s subject=%r purpose=%s url=%s\n%s",
            settings.from_email,
            to_email,
            subject,
            purpose,
            _redact_token_url(set_password_url),
            _redact_body(body),
        )
        return

    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError as exc:  # pragma: no cover
        logger.exception("[welcome-set-password-email] boto3 unavailable")
        raise WelcomeEmailError("boto3 is not installed") from exc

    region = (
        os.getenv("SES_REGION")
        or os.getenv("AWS_REGION")
        or os.getenv("AWS_DEFAULT_REGION")
        or "ca-central-1"
    )

    try:
        client = boto3.client("sesv2", region_name=region)
        # Arc 8 WU-6 -- ConfigurationSetName + ReplyToAddresses. See
        # send_magic_link_email for the design contract.
        response = client.send_email(
            FromEmailAddress=settings.from_email,
            Destination={"ToAddresses": [to_email]},
            ReplyToAddresses=[settings.ses_reply_to_address],
            ConfigurationSetName=settings.ses_configuration_set_name,
            Content={
                "Simple": {
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
                },
            },
        )
        message_id = response.get("MessageId", "<unknown>")
        logger.info(
            "[welcome-set-password-email] sent via SES from=%s to=%s "
            "subject=%r purpose=%s url=%s message_id=%s",
            settings.from_email,
            to_email,
            subject,
            purpose,
            _redact_token_url(set_password_url),
            message_id,
        )
    except (ClientError, BotoCoreError) as exc:
        # Arc 2 (2026-05-20): both url and body now pass through
        # `_redact_token_url` / `_redact_body` before logging --
        # CloudWatch never carries an unredeemed token. See
        # `~~D-set-password-token-logged-plaintext-2026-05-17~~`.
        logger.warning(
            "[welcome-set-password-email] SES send FAILED from=%s to=%s "
            "subject=%r purpose=%s url=%s error=%s\n%s",
            settings.from_email,
            to_email,
            subject,
            purpose,
            _redact_token_url(set_password_url),
            exc,
            _redact_body(body),
        )
        raise WelcomeEmailError(f"SES send_email failed: {exc}") from exc


def _format_amount(amount_cents: int, currency: str) -> str:
    """Format an integer cent amount as a human-readable currency string.

    Examples:
      _format_amount(10000, 'CAD') -> '$100.00 CAD'
      _format_amount(9999, 'USD')  -> '$99.99 USD'
    """
    dollars = amount_cents / 100.0
    return f"${dollars:.2f} {currency.upper()}"


def _build_refund_body(
    *,
    to_email: str,
    refund_id: str,
    amount_cents: int,
    currency: str,
    display_name: str | None,
) -> str:
    """Render the plaintext body for the pilot-refund confirmation email.

    The body mirrors the locked refund-success surface copy from
    CANONICAL_RECAP section 14 paragraph 273 line-for-line so the email and
    the on-page confirmation are interchangeable confirmations of the same
    event:

      - Refunded amount + currency
      - Stripe refund id (for the buyer's records)
      - Subscription canceled + account closed in the same step
      - 5-7 business day expected card-credit window
      - Single CTA: feedback request

    The feedback CTA points to the mailto: on the on-page success surface
    (privacy@vantagemind.ai) as a stopgap until a dedicated survey URL is
    wired in a follow-up commit.
    """
    salutation = display_name or to_email
    amount_str = _format_amount(amount_cents, currency)
    return (
        f"Hi {salutation},\n\n"
        f"Your VantageMind pilot has been refunded.\n\n"
        f"Amount refunded: {amount_str}\n"
        f"Stripe refund id: {refund_id}\n\n"
        f"Your refund has been issued to the original card and should "
        f"appear within 5-7 business days. Your subscription has been "
        f"canceled and your account has been closed in the same step.\n\n"
        f"We are sorry to see you go. If there is anything we could have "
        f"done better, please reply to this email or write to us at "
        f"privacy@vantagemind.ai -- we read every note.\n\n"
        f"-- The VantageMind team\n"
    )


def send_pilot_refund_email(
    *,
    to_email: str,
    refund_id: str,
    amount_cents: int,
    currency: str,
    display_name: str | None = None,
    db: Session | None = None,
) -> None:
    """Send (or log) the pilot-refund confirmation email.

    Step 30a.2-pilot Commit 3j: closes drift
    ``D-pilot-refund-customer-email-missing-2026-05-15``. Mirrors the
    structure of :func:`send_magic_link_email` -- same transport selection
    (LUCIEL_EMAIL_TRANSPORT=ses|log), same SES v2 send_email API, same
    region selection, same lazy boto3 import for the log-only path.

    Behaviour:
      - With ``LUCIEL_EMAIL_TRANSPORT=ses`` (production default): sends
        through Amazon SES v2 ``send_email``. On any SES failure, logs the
        full body at WARNING (so the on-call engineer can manually relay)
        AND raises :class:`RefundEmailError` so the caller's audit row is
        accurate. The ``[pilot-refund-email]`` marker is emitted on both
        the success and failure log lines.
      - With ``LUCIEL_EMAIL_TRANSPORT=log`` (local dev): logs the body only.

    Caller contract: :class:`BillingService.process_pilot_refund` calls
    this AFTER ``self.db.commit()`` and wraps it in ``try/except
    RefundEmailError`` -- the email is a courtesy leg, not a transactional
    leg. SES failure must NOT roll back the refund cascade.
    """
    # Arc 8 WU-6 -- application-layer suppression precheck. See
    # send_magic_link_email for the design contract; same gate, same
    # SuppressedRecipientError surface. The refund email is the
    # courtesy leg AFTER the cascade commits, so a suppression
    # rejection here is benign -- the financial refund still landed.
    _precheck_suppression(to_email, db, "[pilot-refund-email]")

    body = _build_refund_body(
        to_email=to_email,
        refund_id=refund_id,
        amount_cents=amount_cents,
        currency=currency,
        display_name=display_name,
    )
    transport = _transport()

    if transport == _LOG_TRANSPORT:
        logger.warning(
            "[pilot-refund-email] (log-only transport) from=%s to=%s "
            "subject=%r refund_id=%s amount_cents=%s currency=%s\n%s",
            settings.from_email,
            to_email,
            SUBJECT_PILOT_REFUND,
            refund_id,
            amount_cents,
            currency,
            body,
        )
        return

    # Real SES delivery. Boto3 imported lazily; production task always has it.
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError as exc:  # pragma: no cover - prod always has boto3
        logger.exception("[pilot-refund-email] boto3 unavailable; cannot send")
        raise RefundEmailError("boto3 is not installed") from exc

    region = (
        os.getenv("SES_REGION")
        or os.getenv("AWS_REGION")
        or os.getenv("AWS_DEFAULT_REGION")
        or "ca-central-1"
    )

    try:
        client = boto3.client("sesv2", region_name=region)
        # Arc 8 WU-6 -- ConfigurationSetName + ReplyToAddresses. See
        # send_magic_link_email for the design contract.
        response = client.send_email(
            FromEmailAddress=settings.from_email,
            Destination={"ToAddresses": [to_email]},
            ReplyToAddresses=[settings.ses_reply_to_address],
            ConfigurationSetName=settings.ses_configuration_set_name,
            Content={
                "Simple": {
                    "Subject": {"Data": SUBJECT_PILOT_REFUND, "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
                },
            },
        )
        message_id = response.get("MessageId", "<unknown>")
        logger.info(
            "[pilot-refund-email] sent via SES from=%s to=%s subject=%r "
            "refund_id=%s amount_cents=%s currency=%s message_id=%s",
            settings.from_email,
            to_email,
            SUBJECT_PILOT_REFUND,
            refund_id,
            amount_cents,
            currency,
            message_id,
        )
    except (ClientError, BotoCoreError) as exc:
        logger.warning(
            "[pilot-refund-email] SES send FAILED from=%s to=%s subject=%r "
            "refund_id=%s amount_cents=%s currency=%s error=%s\n%s",
            settings.from_email,
            to_email,
            SUBJECT_PILOT_REFUND,
            refund_id,
            amount_cents,
            currency,
            exc,
            body,
        )
        raise RefundEmailError(f"SES send_email failed: {exc}") from exc


def _build_budget_alert_body(
    *,
    to_email: str,
    threshold: int,
    current: int,
    cap: int,
    instance_label: str | None,
    exhausted: bool,
) -> str:
    """Render the plaintext body for a conversation-budget alert.

    The copy is admin-facing (billing/account state) — the OPPOSITE of the
    customer-facing budget_ack copy, which never mentions billing. This
    body names the cap, the usage, and the consequence so the admin can
    decide whether to upgrade.
    """
    scope = f" for {instance_label}" if instance_label else ""
    if exhausted:
        headline = (
            f"Your conversation budget{scope} is exhausted "
            f"({current} of {cap} used)."
        )
        consequence = (
            "New conversations on this instance are now being gracefully "
            "handed off to your team instead of answered automatically. "
            "Upgrade your plan to restore automatic handling."
        )
    elif threshold >= 100:
        headline = (
            f"Your conversation budget{scope} has been reached "
            f"({current} of {cap} used)."
        )
        consequence = (
            "Additional conversations this period will be billed as overage "
            "at your plan's rate. No conversations are blocked."
        )
    else:
        headline = (
            f"Your conversation budget{scope} is at {threshold}% "
            f"({current} of {cap} used)."
        )
        consequence = (
            "This is a heads-up — nothing is blocked. You may want to review "
            "your plan if usage continues at this pace."
        )
    return (
        f"Hi,\n\n"
        f"{headline}\n\n"
        f"{consequence}\n\n"
        f"You can review per-instance usage any time from your VantageMind "
        f"dashboard.\n\n"
        f"-- The VantageMind team\n"
    )


def send_budget_alert_email(
    *,
    to_email: str,
    threshold: int,
    current: int,
    cap: int,
    instance_label: str | None = None,
    exhausted: bool = False,
    db: Session | None = None,
) -> None:
    """Send (or log) a conversation-budget alert to an admin (Arc 18 §3.4.1b).

    Mirrors :func:`send_pilot_refund_email` exactly: suppression precheck,
    ``LUCIEL_EMAIL_TRANSPORT=ses|log`` transport selection, SES v2
    ``send_email``, lazy boto3 import for the log path. On SES failure logs
    the body at WARNING and raises :class:`BudgetAlertEmailError` so the
    caller's audit row is accurate; the alert is a best-effort leg and a
    failure here must never block a turn or a webhook.
    """
    _precheck_suppression(to_email, db, "[budget-alert-email]")

    if exhausted:
        subject = SUBJECT_BUDGET_EXHAUSTED
    elif threshold >= 100:
        subject = SUBJECT_BUDGET_ALERT_100
    else:
        subject = SUBJECT_BUDGET_ALERT_80

    body = _build_budget_alert_body(
        to_email=to_email,
        threshold=threshold,
        current=current,
        cap=cap,
        instance_label=instance_label,
        exhausted=exhausted,
    )
    transport = _transport()

    if transport == _LOG_TRANSPORT:
        logger.warning(
            "[budget-alert-email] (log-only transport) from=%s to=%s "
            "subject=%r threshold=%s current=%s cap=%s exhausted=%s\n%s",
            settings.from_email,
            to_email,
            subject,
            threshold,
            current,
            cap,
            exhausted,
            body,
        )
        return

    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError as exc:  # pragma: no cover - prod always has boto3
        logger.exception("[budget-alert-email] boto3 unavailable; cannot send")
        raise BudgetAlertEmailError("boto3 is not installed") from exc

    region = (
        os.getenv("SES_REGION")
        or os.getenv("AWS_REGION")
        or os.getenv("AWS_DEFAULT_REGION")
        or "ca-central-1"
    )

    try:
        client = boto3.client("sesv2", region_name=region)
        response = client.send_email(
            FromEmailAddress=settings.from_email,
            Destination={"ToAddresses": [to_email]},
            ReplyToAddresses=[settings.ses_reply_to_address],
            ConfigurationSetName=settings.ses_configuration_set_name,
            Content={
                "Simple": {
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
                },
            },
        )
        message_id = response.get("MessageId", "<unknown>")
        logger.info(
            "[budget-alert-email] sent via SES from=%s to=%s subject=%r "
            "threshold=%s current=%s cap=%s exhausted=%s message_id=%s",
            settings.from_email,
            to_email,
            subject,
            threshold,
            current,
            cap,
            exhausted,
            message_id,
        )
    except (ClientError, BotoCoreError) as exc:
        logger.warning(
            "[budget-alert-email] SES send FAILED from=%s to=%s subject=%r "
            "threshold=%s current=%s cap=%s exhausted=%s error=%s\n%s",
            settings.from_email,
            to_email,
            subject,
            threshold,
            current,
            cap,
            exhausted,
            exc,
            body,
        )
        raise BudgetAlertEmailError(f"SES send_email failed: {exc}") from exc
