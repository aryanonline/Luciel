"""send_sms — v1 catalog tool (§3.3.2).

Sends an SMS via the configured channel adapter. Action-
classification tier: NOTIFY_AND_PROCEED — outbound SMS is external-
facing, expected within the customer pattern, and recoverable by a
clarifying follow-up.

DEPLOY-GATED LIVE (Arc 17 connectors)
=====================================
The full live send path is built. Activation is purely credential-
driven, mirroring the Twilio SMS adapter's live-switch discipline
(``channels_live_provisioning_enabled`` is the SAME master gate Arc 13
already wired for the inbound/outbound SMS adapter — this tool threads
it through the agent-initiated send path end-to-end):

  * UNCONFIGURED (Twilio account sid / auth token absent) → an HONEST
    no-op receipt: success=False, not_yet_available=True. NO Twilio
    call. Boot-safe default, so a mis-wired test can never bill Twilio.
  * CONFIGURED + master live-switch OFF
    (``settings.channels_live_provisioning_enabled`` False) → an honest
    no-op receipt (logged, not sent).
  * CONFIGURED + live-switch ON → a REAL Twilio REST message send,
    returning the provider message sid.

``requires_channels={"sms"}`` documents the structural dependency.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from app.core.config import settings
from app.policy.action_classification import ActionTier
from app.tools.base import LucielTool, ToolContext

logger = logging.getLogger(__name__)


class SendSmsTool(LucielTool):

    declared_tier = ActionTier.NOTIFY_AND_PROCEED

    requires_channels = frozenset({"sms"})

    # Arc 15 WU4/WU5 — connection-contract gate (§3.3.2).
    requires_connection = "sms_sender"

    @property
    def tool_id(self) -> str:
        return "send_sms"

    @property
    def display_name(self) -> str:
        return "Send SMS"

    @property
    def description(self) -> str:
        return (
            "Send a short SMS message to the customer or an internal "
            "recipient via the configured SMS channel."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "E.164 phone number.",
                    "minLength": 4,
                    "pattern": r"^\+?[0-9\- ]+$",
                },
                "body": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 1600,
                },
            },
            "required": ["to", "body"],
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
        return ("pro", "enterprise")

    @property
    def execution_mode(self) -> str:
        return "in_process"

    async def execute(
        self,
        input: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        # --- Honesty gate 1: Twilio creds absent → unconfigured. ---
        # No Twilio call. Equivalent to OAuthProvider.is_configured().
        if not (settings.twilio_account_sid and settings.twilio_auth_token):
            return {
                "success": False,
                "output": (
                    "send_sms is registered but the Twilio sender is not "
                    "configured (sms_sender connector is unconfigured). "
                    "No SMS was sent."
                ),
                "not_yet_available": True,
                "owning_arc": "ARC17",
            }

        # --- Honesty gate 2: master live-switch OFF → no-op receipt. ---
        if not settings.channels_live_provisioning_enabled:
            synthetic_sid = f"SMfake{uuid.uuid4().hex[:24]}"
            logger.info(
                "send_sms: (live switch off) to=%s synthetic_sid=%s",
                input["to"],
                synthetic_sid,
            )
            return {
                "success": True,
                "output": (
                    f"SMS logged (live provisioning off) to {input['to']}."
                ),
                "not_yet_available": False,
                "provider_message_id": synthetic_sid,
            }

        # --- LIVE: configured + live-switch on → real Twilio send. ---
        sid = self._send_live(to=input["to"], body=input["body"])
        return {
            "success": True,
            "output": f"SMS sent to {input['to']}.",
            "not_yet_available": False,
            "provider_message_id": sid or "",
        }

    def _send_live(
        self, *, to: str, body: str
    ) -> str | None:  # pragma: no cover - DEPLOY-GATED (live Twilio only)
        # DEPLOY-GATED: reached only when Twilio creds are present AND the
        # master live-switch is on. Never reached in dev / CI / test.
        from twilio.rest import Client

        client = Client(
            settings.twilio_account_sid, settings.twilio_auth_token
        )
        kwargs: dict[str, Any] = {"to": to, "body": body}
        if settings.twilio_messaging_service_sid:
            kwargs["messaging_service_sid"] = settings.twilio_messaging_service_sid
        sent = client.messages.create(**kwargs)
        return sent.sid
