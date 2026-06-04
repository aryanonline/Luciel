"""Base NotificationAdapter contract — §3.5.1."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class NotificationResult:
    """Result of one notification attempt.

    ``sent``         — True only when the live transport actually fired.
    ``dry_run``      — True when the live-switch is off; full routing +
                       attempt decision recorded but no real send made.
    ``provider_id``  — Provider message id when sent; None on dry-run or error.
    ``error``        — Exception class name when the send failed; None otherwise.
    ``channel``      — The channel id (email/sms/slack).
    ``to``           — The recipient address/number/webhook.
    """
    channel: str
    to: str | None
    sent: bool = False
    dry_run: bool = False
    provider_id: str | None = None
    error: str | None = None
    extra: dict[str, Any] | None = None


class NotificationAdapter:
    """Abstract base for escalation notification adapters (§3.5.1).

    Subclasses implement ``send`` to deliver a single notification to
    one recipient. Best-effort contract: never raises; returns a
    NotificationResult that the delivery service records in the audit log.
    """

    channel: str = "unknown"

    def send(
        self,
        *,
        to: str | None,
        subject: str,
        body: str,
        signal: str,
        session_id: str,
    ) -> NotificationResult:
        raise NotImplementedError  # pragma: no cover
