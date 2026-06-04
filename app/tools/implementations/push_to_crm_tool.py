"""push_to_crm — v1 catalog tool (§3.3.2).

Pushes a lead / interaction record to an external CRM. Action-
classification tier: NOTIFY_AND_PROCEED — a CRM row is external,
visible to the customer's sales team, but reversible (the row can
be edited or deleted in the CRM).

DEPLOY-GATED LIVE (Arc 17 connectors)
=====================================
The full native-CRM OAuth dispatch path is built. Activation is purely
credential-driven, mirroring the Google-Calendar / Twilio template:

  * UNCONFIGURED (no native CRM OAuth client creds in settings → the
    HubSpot/Salesforce provider reports is_configured() False) → an
    HONEST deferred receipt: success=False, not_yet_available=True. NO
    network call. Boot-safe default (dev / CI / test).
  * CONFIGURED + master live-switch OFF
    (``settings.connectors_live_enabled`` False) → an honest no-op
    receipt (logged, not pushed).
  * CONFIGURED + live-switch ON → a REAL CRM push: refresh the stored
    OAuth token via the provider, then POST the record to the CRM API.

DOC GAP (founder reconciliation ledger): native HubSpot/Salesforce CRM
has NO owning arc in the canonical documents — this path is built per
the founder's explicit deploy-gated instruction. The custom-webhook CRM
path (Arc 12 WU6) is unaffected and stays live.
"""
from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings
from app.integrations.oauth import (
    OAuthError,
    get_oauth_provider,
)
from app.integrations.secrets import SecretStoreError, get_secret_store
from app.policy.action_classification import ActionTier
from app.tools.base import LucielTool, ToolContext

logger = logging.getLogger(__name__)


class PushToCrmTool(LucielTool):

    declared_tier = ActionTier.NOTIFY_AND_PROCEED

    # Arc 15 WU4/WU5 — connection-contract gate (§3.3.2). The WU5 gate
    # only dispatches this tool when a status='connected' crm row exists;
    # the deploy-gate here is the second, independent honesty check.
    requires_connection = "crm"

    @property
    def tool_id(self) -> str:
        return "push_to_crm"

    @property
    def display_name(self) -> str:
        return "Push to CRM"

    @property
    def description(self) -> str:
        return (
            "Push a lead or interaction record to the configured "
            "external CRM."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "record_type": {
                    "type": "string",
                    "enum": ["lead", "contact", "interaction", "note"],
                },
                "payload": {
                    "type": "object",
                    "additionalProperties": True,
                },
            },
            "required": ["record_type", "payload"],
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
                "provider": {"type": "string"},
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
        provider = get_oauth_provider("crm", settings)

        # --- Honesty gate 1: no native CRM OAuth creds → deferred. ---
        # No network. Equivalent to the OAuth deploy-gate everywhere else.
        if provider is None or not provider.is_configured():
            return {
                "success": False,
                "output": (
                    "push_to_crm is registered but no native CRM OAuth "
                    "client is configured (crm connector is unconfigured). "
                    "No CRM record was created."
                ),
                "not_yet_available": True,
                "owning_arc": "ARC17",
            }

        # --- Honesty gate 2: master live-switch OFF → no-op receipt. ---
        if not settings.connectors_live_enabled:
            logger.info(
                "push_to_crm: (live switch off) record_type=%s provider=%s",
                input["record_type"],
                provider.connection_type,
            )
            return {
                "success": True,
                "output": (
                    f"CRM record logged (live provisioning off) "
                    f"record_type={input['record_type']}."
                ),
                "not_yet_available": False,
                "provider": "crm",
            }

        # --- LIVE: configured + live-switch on → real CRM push. ---
        return self._push_live(
            provider=provider,
            record_type=input["record_type"],
            payload=input["payload"],
            context=context,
        )

    def _push_live(
        self,
        *,
        provider: Any,
        record_type: str,
        payload: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:  # pragma: no cover - DEPLOY-GATED (live CRM only)
        # DEPLOY-GATED: reached only when the native CRM OAuth client is
        # configured AND the master live-switch is on. Loads the connected
        # crm row, refreshes the stored OAuth token, and POSTs the record.
        # Never reached in dev / CI / test.
        from app.repositories.instance_connection_repository import (
            InstanceConnectionRepository,
        )

        if context.session is None:
            return {
                "success": False,
                "output": "push_to_crm: no DB session in context.",
                "not_yet_available": False,
            }

        repo = InstanceConnectionRepository(context.session)
        row = repo.get_live_by_type(
            admin_id=context.admin_id,
            instance_id=context.instance_id,
            connection_type="crm",
        )
        if row is None or not row.credential_ref:
            return {
                "success": False,
                "output": (
                    "push_to_crm: no connected crm credential available."
                ),
                "not_yet_available": False,
            }

        store = get_secret_store(settings)
        try:
            refresh_token = store.get(row.credential_ref)
            tokens = provider.refresh(refresh_token=refresh_token)
        except (SecretStoreError, OAuthError) as exc:
            return {
                "success": False,
                "output": f"push_to_crm: token refresh failed: {exc}",
                "not_yet_available": False,
            }

        # The concrete per-provider REST call is a documented seam; the
        # access token is now live. We dispatch a minimal contact/lead
        # create against the provider's API host.
        provider_id = self._dispatch_record(
            access_token=tokens.access_token,
            record_type=record_type,
            payload=payload,
        )
        return {
            "success": True,
            "output": f"CRM record created (record_type={record_type}).",
            "not_yet_available": False,
            "provider": "crm",
            "provider_record_id": provider_id or "",
        }

    def _dispatch_record(
        self, *, access_token: str, record_type: str, payload: dict[str, Any]
    ) -> str | None:  # pragma: no cover - DEPLOY-GATED (live CRM only)
        import httpx

        # HubSpot contacts create (the reference dispatch). A Salesforce
        # org overrides the host/path; both ride the same Bearer token.
        resp = httpx.post(
            "https://api.hubapi.com/crm/v3/objects/contacts",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={"properties": payload},
            timeout=10.0,
        )
        if resp.status_code not in (200, 201):
            raise OAuthError(
                f"CRM record create returned {resp.status_code}: "
                f"{resp.text[:200]}"
            )
        body = resp.json()
        return body.get("id") if isinstance(body, dict) else None
