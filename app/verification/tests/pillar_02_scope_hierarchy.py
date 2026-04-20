"""Pillar 2 - Scope hierarchy + agent admin key pre-mint.

Asserts:
  1. Tenant admin can create a domain under its own tenant (201).
  2. Tenant admin can create an agent under that domain (201).
  3. Tenant admin can create three LucielInstances: one at tenant scope,
     one at domain scope, one at agent scope (all 201).
  4. Gap-5 fix: mint an agent-scoped admin key here, BEFORE pillar 7
     deactivates the domain. Pillar 8's above-scope negative test depends
     on this key existing and being usable; landed suite minted it after
     cascade and sometimes got 404/deactivated tenancy errors that caused
     the above-scope assertion to silently skip.

Writes to RunState:
  - domain_id                = "sales"
  - agent_id                 = "sarah-listings"
  - instance_tenant / domain / agent (int PKs)
  - agent_admin_key          (raw bearer, scoped to agent)
  - agent_admin_key_id       (int PK, for teardown)
  - keys_to_deactivate       (appended with agent_admin_key_id)
"""

from __future__ import annotations

from typing import Any

from app.verification.fixtures import RunState
from app.verification.http_client import call, pooled_client
from app.verification.runner import Pillar


class ScopeHierarchyPillar(Pillar):
    number = 2
    name = "scope hierarchy + agent admin key"

    def run(self, state: RunState) -> str:
        if not state.tenant_admin_key:
            raise AssertionError("pillar 2 requires tenant_admin_key from pillar 1")

        ak = state.tenant_admin_key
        tid = state.tenant_id

        with pooled_client() as c:
            # --- 1. domain ---
            call(
                "POST",
                "/api/v1/admin/domains",
                ak,
                json={
                    "tenant_id": tid,
                    "domain_id": "sales",
                    "display_name": "Sales",
                },
                expect=(200, 201),
                client=c,
            )
            state.domain_id = "sales"

            # --- 2. agent ---
            call(
                "POST",
                "/api/v1/admin/agents",
                ak,
                json={
                    "tenant_id": tid,
                    "domain_id": "sales",
                    "agent_id": "sarah-listings",
                    "display_name": "Sarah",
                },
                expect=(200, 201),
                client=c,
            )
            state.agent_id = "sarah-listings"

            # --- 3. three LucielInstances across all three scope levels ---
            specs = [
                ("instance_tenant", {
                    "instance_id": "step26-tenant-luciel",
                    "scope_level": "tenant",
                    "scope_owner_tenant_id": tid,
                    "display_name": "Tenant Luciel",
                }),
                ("instance_domain", {
                    "instance_id": "step26-domain-luciel",
                    "scope_level": "domain",
                    "scope_owner_tenant_id": tid,
                    "scope_owner_domain_id": "sales",
                    "display_name": "Sales Luciel",
                }),
                ("instance_agent", {
                    "instance_id": "step26-agent-luciel",
                    "scope_level": "agent",
                    "scope_owner_tenant_id": tid,
                    "scope_owner_domain_id": "sales",
                    "scope_owner_agent_id": "sarah-listings",
                    "display_name": "Sarah's Luciel",
                }),
            ]
            for attr, payload in specs:
                r = call(
                    "POST",
                    "/api/v1/admin/luciel-instances",
                    ak,
                    json=payload,
                    expect=(200, 201),
                    client=c,
                )
                pk = r.json().get("id")
                if not isinstance(pk, int):
                    raise AssertionError(
                        f"luciel-instance create for {attr} did not return int id: {r.json()}"
                    )
                setattr(state, attr, pk)

            # --- 4. GAP-5 FIX: agent-scoped admin key, minted pre-cascade ---
            r = call(
                "POST",
                "/api/v1/admin/api-keys",
                ak,
                json={
                    "tenant_id": tid,
                    "display_name": "step26-agent-admin",
                    "permissions": ["chat", "sessions", "admin"],
                    "rate_limit": 1000,
                    "domain_id": "sales",
                    "agent_id": "sarah-listings",
                    "created_by": "step26-verify",
                },
                expect=(200, 201),
                client=c,
            )
            j = r.json()
            raw = j.get("raw_key") or (j.get("api_key") or {}).get("raw_key")
            kid = j.get("id") or (j.get("api_key") or {}).get("id")
            if not isinstance(raw, str) or not isinstance(kid, int):
                raise AssertionError(
                    f"agent admin key create malformed response: {j}"
                )
            state.agent_admin_key = raw
            state.agent_admin_key_id = kid
            state.keys_to_deactivate.append(kid)

        return (
            f"domain=sales agent=sarah-listings "
            f"T={state.instance_tenant} D={state.instance_domain} A={state.instance_agent} "
            f"agent_admin=***{raw[-6:]} (pre-cascade)"
        )


PILLAR = ScopeHierarchyPillar()