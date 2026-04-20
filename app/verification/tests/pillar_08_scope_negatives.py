"""Pillar 8 - Scope-policy negatives (gap-5 fix in action).

Landed suite minted the agent admin key AFTER pillar 7 deactivated the
sales domain, which caused the key creation to sometimes 404 and made
the above-scope creation assertion silently skip. Redo pre-mints the
agent admin key in pillar 2 (before any cascade runs), so both negatives
run unconditionally.

Asserts:
  1. Cross-tenant isolation:
     The step26 tenant's agent admin key, when querying
     /admin/luciel-instances?tenant_id=remax-crossroads, must not return
     any items owned by remax-crossroads. Acceptable responses:
       - 403 (scope-policy denial)
       - 404 (route refuses cross-tenant query)
       - 200 with filtered-to-own-tenant items (no remax-crossroads leak)

  2. Above-scope creation rejection:
     The agent-scoped admin key (which should only be able to create
     agent-level instances under its own agent_id) must be rejected
     when attempting to POST a tenant-level LucielInstance. Expected:
     403 (scope-policy denial) or 422 (schema/policy validation).
"""

from __future__ import annotations

from typing import Any

import httpx

from app.verification.fixtures import RunState
from app.verification.http_client import BASE_URL, REQUEST_TIMEOUT, call, h, pooled_client
from app.verification.runner import Pillar


def _items_from(body: Any) -> list[dict]:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        return body.get("items") or body.get("results") or body.get("value") or []
    return []


class ScopeNegativesPillar(Pillar):
    number = 8
    name = "scope-policy negatives"

    def run(self, state: RunState) -> str:
        if not state.agent_admin_key:
            raise AssertionError(
                "pillar 8 requires agent_admin_key pre-minted by pillar 2 (gap-5 fix)"
            )
        ak = state.agent_admin_key

        with pooled_client() as c:
            # ---------- 1. cross-tenant isolation ----------
            r = c.get(
                "/api/v1/admin/luciel-instances?tenant_id=remax-crossroads",
                headers=h(ak),
            )
            if r.status_code not in (200, 403, 404):
                raise AssertionError(
                    f"cross-tenant query expected 200/403/404, got {r.status_code} "
                    f"body={r.text[:300]}"
                )
            leak_note = f"status={r.status_code}"
            if r.status_code == 200:
                items = _items_from(r.json())
                leaks = [
                    i for i in items
                    if i.get("scope_owner_tenant_id") == "remax-crossroads"
                ]
                if leaks:
                    raise AssertionError(
                        f"cross-tenant leak: {len(leaks)} remax-crossroads-owned items "
                        f"returned to step26-verify agent admin key. "
                        f"First leak: {leaks[0]}"
                    )
                leak_note = f"status=200 items={len(items)} remax_leaks=0"

            # ---------- 2. above-scope creation rejection ----------
            r = c.post(
                "/api/v1/admin/luciel-instances",
                headers=h(ak),
                json={
                    "instance_id": "should-fail-step26-above",
                    "scope_level": "tenant",
                    "scope_owner_tenant_id": state.tenant_id,
                    "display_name": "should-fail (above-scope probe)",
                },
            )
            if r.status_code not in (403, 422):
                raise AssertionError(
                    f"above-scope create expected 403/422, got {r.status_code} "
                    f"body={r.text[:300]}. agent-scoped key must not create "
                    f"tenant-level LucielInstance."
                )
            above_note = f"rejected with {r.status_code}"

        return f"cross-tenant {leak_note}; above-scope {above_note}"


PILLAR = ScopeNegativesPillar()