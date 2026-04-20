"""Pillar 4 - Chat-key binding + blast-radius isolation.

Asserts:
  1. For each LucielInstance (tenant/domain/agent), mint a chat key via
     POST /api/v1/admin/api-keys with lucielinstanceid=<id>, permissions
     ['chat','sessions'] (NO admin). Expect 200/201, raw_key in response.
  2. Blast-radius check: each chat key, when used against an admin-only
     route (/api/v1/admin/tenants), MUST return 403. Proves that a chat
     key cannot escalate to admin regardless of which instance it is
     bound to.
  3. Each chat key's id is appended to state.keys_to_deactivate so
     teardown cleans them up.

Gap 6 setup: each chat_key entry records scope_level so pillar 5 can
chat against the domain-bound key and the agent-bound key separately,
proving scope-correct retrieval (the gap-6 positive test).

Writes to RunState:
  - chat_keys: list of {key, instance_id, id, scope_level}
  - keys_to_deactivate: extended with the three new key ids
"""

from __future__ import annotations

from typing import Any

import httpx

from app.verification.fixtures import RunState
from app.verification.http_client import BASE_URL, REQUEST_TIMEOUT, call, h, pooled_client
from app.verification.runner import Pillar


class ChatKeyBindingPillar(Pillar):
    number = 4
    name = "chat-key binding + blast radius"

    def run(self, state: RunState) -> str:
        if not state.tenant_admin_key:
            raise AssertionError("pillar 4 requires tenant_admin_key from pillar 1")
        targets = [
            ("tenant", state.instance_tenant),
            ("domain", state.instance_domain),
            ("agent", state.instance_agent),
        ]
        for level, inst_id in targets:
            if inst_id is None:
                raise AssertionError(f"pillar 4 requires instance_{level} from pillar 2")

        ak = state.tenant_admin_key
        tid = state.tenant_id

        with pooled_client() as c:
            for scope_level, inst_id in targets:
                r = call(
                    "POST",
                    "/api/v1/admin/api-keys",
                    ak,
                    json={
                        "tenant_id": tid,
                        "display_name": f"step26-chat-{scope_level}-{inst_id}",
                        "permissions": ["chat", "sessions"],
                        "rate_limit": 1000,
                        "luciel_instance_id": inst_id,
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
                        f"chat-key create malformed for scope={scope_level}: {j}"
                    )
                state.chat_keys.append({
                    "key": raw,
                    "instance_id": inst_id,
                    "id": kid,
                    "scope_level": scope_level,
                })
                state.keys_to_deactivate.append(kid)

            # Blast-radius: every chat key must 403 on an admin route.
            # Use raw httpx here so we don't raise on 403 (call() would).
            for ck in state.chat_keys:
                r = c.get("/api/v1/admin/tenants", headers=h(ck["key"]))
                if r.status_code != 403:
                    raise AssertionError(
                        f"chat key bound to instance {ck['instance_id']} "
                        f"({ck['scope_level']}-scope) reached admin route: "
                        f"got {r.status_code}, expected 403. body={r.text[:200]}"
                    )

        return (
            f"minted 3 chat keys "
            f"(T=***{state.chat_keys[0]['key'][-4:]} "
            f"D=***{state.chat_keys[1]['key'][-4:]} "
            f"A=***{state.chat_keys[2]['key'][-4:]}), "
            f"all 403 on admin routes"
        )


PILLAR = ChatKeyBindingPillar()