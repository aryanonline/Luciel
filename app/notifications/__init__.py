"""Escalation notification adapters — §3.5.1 NotificationAdapter contract.

Three concrete adapters (email/sms/slack) each wrap the existing send
stack — never a second transport implementation. Every adapter is gated
behind the CHANNELS_LIVE_PROVISIONING_ENABLED master switch; when the
flag is False the adapter records the full routing+attempt decision and
logs DRY-RUN, matching the existing escalation_routing convention.
"""
from app.notifications.base import NotificationAdapter, NotificationResult
from app.notifications.email_notifier import EmailNotificationAdapter
from app.notifications.sms_notifier import SmsNotificationAdapter
from app.notifications.slack_notifier import SlackNotificationAdapter

__all__ = [
    "NotificationAdapter",
    "NotificationResult",
    "EmailNotificationAdapter",
    "SmsNotificationAdapter",
    "SlackNotificationAdapter",
]
