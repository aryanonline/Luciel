"""Pillar 1 - Onboarding (Option B invariant).

Asserts:
  1. POST /api/v1/admin/tenants/onboard against a fresh throwaway tenant
     returns 200 or 201.
  2. Response includes a usable admin_api_key raw string.
  3. Response MUST NOT include chat_api_key, default_luciel_instance,
     or default_luciel. Option B is: onboarding mints an admin key only,
     the tenant self-services Luciel creation and chat-key minting.

Writes to RunState:
  - tenant_admin_key  (raw bearer string for the throwaway tenant)
"""

from __future__ import annotations

from typing import Any

from app.verification.fixtures import RunState
from app.verification.http_client import call, pooled_client
from app.verification.runner import Pillar


class OnboardingPillar(Pillar):
    number = 1
    name = "onboarding (Option B)"

    def run(self, state: RunState) -> str:
        body: dict[str, Any] = {
            "tenant_id": state.tenant_id,
            "display_name": "Step 26 Verify Test",
            "admin_display_name": "step26-admin",
        }
        with pooled_client() as c:
            r = call(
                "POST",
                "/api/v1/admin/tenants/onboard",
                state.platform_admin_key,
                json=body,
                expect=(200, 201),
                client=c,
            )

        j = r.json()

        # Extract admin key: landed response shape is
        # {"admin_api_key": {"raw_key": "luc_sk_..."}, ...}
        admin_blob = j.get("admin_api_key") or j.get("admin_key") or {}
        if isinstance(admin_blob, dict):
            ak = admin_blob.get("raw_key") or admin_blob.get("key")
        else:
            ak = admin_blob

        if not isinstance(ak, str) or len(ak) < 20:
            raise AssertionError(
                f"onboard admin_api_key not a usable string: type={type(ak).__name__} "
                f"value={ak!r} response_keys={sorted(j.keys())}"
            )

        # Option B invariant: forbidden keys MUST NOT be present
        forbidden = [
            k for k in ("chat_api_key", "default_luciel_instance", "default_luciel")
            if k in j
        ]
        if forbidden:
            raise AssertionError(
                f"Option B violated: response leaked forbidden keys {forbidden}. "
                f"Full response keys: {sorted(j.keys())}"
            )

        state.tenant_admin_key = ak
        return f"tenant={state.tenant_id} admin=***{ak[-6:]} keys={sorted(j.keys())}"


PILLAR = OnboardingPillar()