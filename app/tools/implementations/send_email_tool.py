"""send_email — v1 catalog tool (§3.3.2).

Sends an email via the configured channel adapter. Action-
classification tier: NOTIFY_AND_PROCEED — an outbound email is
external-facing but reversible (within reason — sending one to the
wrong recipient is recoverable by clarification) and is expected
within the customer-facing pattern.

DEPLOY-GATED LIVE (Arc 17 connectors)
=====================================
The full live send path is built. Activation is purely credential-
driven, mirroring the Twilio / Google-Calendar template:

  * UNCONFIGURED (no verified sender identity in
    ``settings.email_sender_from_address``) → an HONEST no-op receipt:
    success=False, not_yet_available=True. NO SES call. This is the
    boot-safe default (dev / CI / test, no creds), so a mis-wired test
    can never send a real email.
  * CONFIGURED + master live-switch OFF
    (``settings.connectors_live_enabled`` False) → an honest no-op
    receipt (logged, not sent). The path is exercised but bills no one.
  * CONFIGURED + live-switch ON → a REAL SES ``send_email`` via the
    Arc 13 email transport, returning the provider message id.

``requires_channels={"email"}`` documents the structural dependency.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from app.core.config import settings
from app.policy.action_classification import ActionTier
from app.tools.base import LucielTool, ToolContext

logger = logging.getLogger(__name__)


class SendEmailTool(LucielTool):

    declared_tier = ActionTier.NOTIFY_AND_PROCEED

    requires_channels = frozenset({"email"})

    # Arc 15 WU4/WU5 — connection-contract gate (§3.3.2).
    requires_connection = "email_sender"

    @property
    def tool_id(self) -> str:
        return "send_email"

    @property
    def display_name(self) -> str:
        return "Send email"

    @property
    def description(self) -> str:
        return (
            "Send an email to the customer or an internal recipient "
            "via the configured email channel."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "Recipient email address.",
                    "minLength": 3,
                },
                "subject": {"type": "string", "minLength": 1},
                "body": {"type": "string", "minLength": 1},
            },
            "required": ["to", "subject", "body"],
            "additionalProperties": False,
        }

    @property
    def output_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "success": {"type": "boolean"},
                "output": {"type": "string"},
                "not_yet_available": {"type": "boolean"},
                "owning_arc": {"type": "string"},
                "provider_message_id": {"type": "string"},
            },
            "required": ["success", "output"],
            "additionalProperties": True,
        }

    @property
    def requires_tier(self) -> tuple[str, ...]:
        # Action tools are Pro-only; Enterprise tier deferred
        # (Open Decision #8 -- ratified 2-tier Free/Pro model).
        return ("pro",)

    @property
    def execution_mode(self) -> str:
        return "in_process"

    async def execute(
        self,
        input: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        from_address = settings.email_sender_from_address

        # --- Honesty gate 1: no verified sender identity → unconfigured. ---
        # No SES call. Mirrors OAuthProvider.is_configured().
        if not from_address:
            return {
                "success": False,
                "output": (
                    "send_email is registered but no verified sender "
                    "identity is configured (email_sender connector is "
                    "unconfigured). No email was sent."
                ),
                "not_yet_available": True,
                "owning_arc": "ARC17",
            }

        # --- Honesty gate 2: master live-switch OFF → no-op receipt. ---
        # The path is exercised but the non-live default never bills SES.
        if not settings.connectors_live_enabled:
            synthetic_id = f"log-email-{uuid.uuid4().hex}"
            logger.info(
                "send_email: (live switch off) to=%s subject=%s synthetic_id=%s",
                input["to"],
                input["subject"],
                synthetic_id,
            )
            return {
                "success": True,
                "output": (
                    f"Email logged (live provisioning off) to {input['to']}."
                ),
                "not_yet_available": False,
                "provider_message_id": synthetic_id,
            }

        # --- LIVE: configured + live-switch on → real SES send. ---
        provider_id = self._send_live(
            to=input["to"],
            subject=input["subject"],
            body=input["body"],
            from_address=from_address,
        )
        return {
            "success": True,
            "output": f"Email sent to {input['to']}.",
            "not_yet_available": False,
            "provider_message_id": provider_id or "",
        }

    def _send_live(
        self, *, to: str, subject: str, body: str, from_address: str
    ) -> str | None:  # pragma: no cover - DEPLOY-GATED (live SES only)
        # DEPLOY-GATED: reached only when a verified sender identity is
        # present AND the master live-switch is on. Uses the Arc 13 SES
        # transport (sesv2 send_email). Never reached in dev / CI / test.
        import os

        import boto3

        region = (
            os.getenv("SES_REGION")
            or os.getenv("AWS_REGION")
            or os.getenv("AWS_DEFAULT_REGION")
            or "ca-central-1"
        )
        from_name = settings.email_sender_from_name
        source = f"{from_name} <{from_address}>" if from_name else from_address
        client = boto3.client("sesv2", region_name=region)
        resp = client.send_email(
            FromEmailAddress=source,
            Destination={"ToAddresses": [to]},
            Content={
                "Simple": {
                    "Subject": {"Data": subject},
                    "Body": {"Text": {"Data": body}},
                }
            },
        )
        return resp.get("MessageId") if isinstance(resp, dict) else None
