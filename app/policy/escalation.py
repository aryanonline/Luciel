"""
Escalation service.

Handles what happens after an escalation is triggered.
For MVP, this just logs the escalation event.

Later, this can:
- Create a support ticket.
- Send a notification to a human agent.
- Trigger a webhook.
- Update session status to "escalated".
- Route to a specific team based on domain/tenant.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class EscalationService:
    """
    Processes escalation events after policy decides one is needed.
    """

    def handle_escalation(
        self,
        *,
        session_id: str,
        user_id: str | None,
        tenant_id: str,
        reason: str,
    ) -> None:
        """
        Process an escalation event.

        For MVP: log the event.
        Later: create ticket, notify human, update session status.
        """
        logger.warning(
            "ESCALATION | session=%s | user=%s | tenant=%s | reason=%s",
            session_id,
            user_id,
            tenant_id,
            reason,
        )