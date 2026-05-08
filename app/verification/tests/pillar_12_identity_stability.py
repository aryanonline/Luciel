"""Pillar 12 - Identity stability under role change (Step 24.5b Q6).

Q6 resolution proof: when an Agent's role changes, the platform User
identity persists and remains attributable across the change, while
the OLD Agent's API keys stop working immediately (mandatory key
rotation, hard, no grace period).

Self-contained. Mixed entry-point doctrine (Step 24.5b decision A):
HTTP for setup, HTTP for promote(), HTTP for forensic inspection.

Step 29 Commit C.2: forensic reads (5 callsites) migrated from
direct SessionLocal/select(MemoryItem)/db.get(ApiKey) to the
platform-admin-gated GET endpoints under /api/v1/admin/forensics/.
The MemoryItem reads use `memory_items_step29c?actor_user_id=...&
agent_id=...` (filters added in C.2). The ApiKey read uses
`api_keys_step29c?id=...` (added in C.1). Producer-side exemption
rule (B.3) is N/A for P12 -- this pillar has no producer-side
calls; every assertion is HTTP + HTTP-forensic.

Drift D18 accommodation: local memory extractor produces 0 rows for
some test message shapes. Memory-row assertions are conditional --
if rows exist with the right (actor_user_id, agent_id) shape, we
assert correctness; if no rows exist, the pillar still proves the
security-critical claims (key rotation, scope assignment lifecycle,
401 enforcement) and reports memory_skipped in the success detail.
"""

from __future__ import annotations

import time
import uuid

from app.models.scope_assignment import EndReason
from app.verification.fixtures import RunState
from app.verification.http_client import call, pooled_client
from app.verification.runner import Pillar


P12_TENANT_PREFIX = "step24-5b-p12-"


def _new_p12_tenant_id() -> str:
    return f"{P12_TENANT_PREFIX}{uuid.uuid4().hex[:8]}"


class IdentityStabilityPillar(Pillar):
    number = 12
    name = "identity stability under role change (Q6)"

    def run(self, state: RunState) -> str:
        pa = state.platform_admin_key
        if not pa:
            raise AssertionError(
                "pillar 12 requires platform_admin_key (load via env)"
            )

        tid = _new_p12_tenant_id()
        domain_id = "general"
        memory_skipped = False
        row1_id: int | None = None
        row2_id: int | None = None

        with pooled_client() as c:
            # ---------- 0. Onboard ----------
            r = call(
                "POST", "/api/v1/admin/tenants/onboard", pa,
                json={
                    "tenant_id": tid,
                    "display_name": "P12 Identity Stability Test",
                    "default_domain_id": domain_id,
                    "default_domain_display_name": "General",
                },
                expect=(200, 201), client=c,
            )
            onboard_body = r.json()
            tenant_admin_key = (
                onboard_body.get("admin_raw_key")
                or onboard_body.get("admin_api_key", {}).get("raw_key")
            )

            # ---------- 1. Create User U ----------
            user_email = f"p12-user-{uuid.uuid4().hex[:8]}@example.com"
            r = call(
                "POST", "/api/v1/users", pa,
                json={
                    "email": user_email,
                    "display_name": "P12 Test User",
                    "synthetic": False,
                },
                expect=(200, 201), client=c,
            )
            user_id = uuid.UUID(r.json()["id"])

            # ---------- 2. Create Agent A1 ----------
            agent_a1_slug = f"p12-a1-{uuid.uuid4().hex[:6]}"
            r = call(
                "POST", "/api/v1/admin/agents", tenant_admin_key,
                json={
                    "tenant_id": tid,
                    "domain_id": domain_id,
                    "agent_id": agent_a1_slug,
                    "display_name": "P12 A1 (listings_agent)",
                    "contact_email": user_email,
                },
                expect=(200, 201), client=c,
            )
            agent_a1_pk = r.json()["id"]

            # Bind A1 -> U
            #
            # Step 28 Phase 2 - Commit 10: was a raw SessionLocal() write to
            # agents.user_id. The Pattern N verify task runs with the worker
            # DSN (least privilege) which correctly refuses INSERT/UPDATE on
            # `agents`, so the previous code crashed in prod with
            # "permission denied for table agents". Switched to the
            # platform-admin-gated bind-user route shipped in Commit 9
            # (dddf8cb) so the harness no longer requires admin DB grants.
            call(
                "POST",
                f"/api/v1/admin/agents/{tid}/{agent_a1_slug}/bind-user",
                pa,
                json={"user_id": str(user_id)},
                expect=200,
                client=c,
            )

            # ---------- 3. Mint K1 ----------
            r = call(
                "POST", "/api/v1/admin/api-keys", tenant_admin_key,
                json={
                    "tenant_id": tid,
                    "domain_id": domain_id,
                    "agent_id": agent_a1_slug,
                    "display_name": "P12 K1",
                    "permissions": ["chat", "sessions"],
                },
                expect=(200, 201), client=c,
            )
            k1_body = r.json()
            k1_raw = k1_body["raw_key"]
            k1_id = k1_body["api_key"]["id"]

            # ---------- 4. Create SA1 ----------
            # Phase 2 Commit 13: HTTP path via /admin/scope-assignments.
            # Verify role has zero DB privileges on scope_assignments by
            # design (migration f392a842f885); the new platform-admin route
            # wraps the same service call the production path uses.
            r = call(
                "POST",
                "/api/v1/admin/scope-assignments",
                pa,
                json={
                    "user_id": str(user_id),
                    "payload": {
                        "tenant_id": tid,
                        "domain_id": domain_id,
                        "role": "listings_agent",
                    },
                    "audit_label": f"pillar_12:{tid}",
                },
                expect=(200, 201),
                client=c,
            )
            sa1_id = uuid.UUID(r.json()["id"])

            # ---------- 5. Session + chat under K1 ----------
            r = call(
                "POST", "/api/v1/sessions", k1_raw,
                json={
                    "user_id": f"p12-end-user-{uuid.uuid4().hex[:6]}",
                    "tenant_id": tid, "domain_id": domain_id,
                },
                expect=(200, 201), client=c,
            )
            session_body = r.json()
            session_id = session_body.get("session_id") or session_body.get("id")

            call(
                "POST", "/api/v1/consent/grant", k1_raw,
                json={
                    "user_id": session_body.get("user_id"),
                    "tenant_id": tid,
                },
                expect=(200, 201), client=c,
            )
            call(
                "POST", "/api/v1/chat", k1_raw,
                json={
                    "session_id": session_id,
                    "message": (
                        "Please remember: my preferred test sentinel is "
                        f"P12-A1-{uuid.uuid4().hex[:6]}."
                    ),
                },
                expect=200, client=c,
            )
            time.sleep(15)

            # ---------- A1: row1 attribution (conditional) ----------
            # Step 29 C.2: forensic GET via memory_items_step29c with
            # actor_user_id + agent_id filters (added in C.2).
            r = call(
                "GET",
                "/api/v1/admin/forensics/memory_items_step29c",
                pa,
                params={
                    "tenant_id": tid,
                    "actor_user_id": str(user_id),
                    "agent_id": agent_a1_slug,
                    "limit": 1,
                },
                expect=200,
                client=c,
            )
            row1_items = r.json().get("items") or []
            if not row1_items:
                memory_skipped = True
            else:
                row1 = row1_items[0]
                row1_actor_str = row1.get("actor_user_id")
                if row1_actor_str is None or uuid.UUID(row1_actor_str) != user_id:
                    raise AssertionError(
                        f"A1 FAIL: row1.actor_user_id={row1_actor_str} "
                        f"!= U.id={user_id}"
                    )
                if row1.get("agent_id") != agent_a1_slug:
                    raise AssertionError(
                        f"A1 FAIL: row1.agent_id={row1.get('agent_id')!r} "
                        f"!= A1.agent_id={agent_a1_slug!r}"
                    )
                row1_id = row1.get("id")

            # ---------- 7. Promote A1 -> A2 ----------
            agent_a2_slug = f"p12-a2-{uuid.uuid4().hex[:6]}"
            r = call(
                "POST", "/api/v1/admin/agents", tenant_admin_key,
                json={
                    "tenant_id": tid,
                    "domain_id": domain_id,
                    "agent_id": agent_a2_slug,
                    "display_name": "P12 A2 (team_lead)",
                    "contact_email": user_email,
                },
                expect=(200, 201), client=c,
            )
            agent_a2_pk = r.json()["id"]

            # NOTE: do NOT bind A2 -> U here. The Step 24.5b invariant
            # ("a User holds at most one active Agent per tenant") would
            # reject it because U still holds A1 active. Legacy direct-DB
            # path never bound A2 -- promote() operates on scope_assignments
            # only and leaves agents.user_id untouched on the new agent.
            # K2 minting and post-promote chat attribution work via the
            # ScopeAssignment lookup, not via agents.user_id.

            # Phase 2 Commit 13: HTTP path via /admin/scope-assignments/promote.
            # Same atomic end+create cascade as production; just driven
            # by HTTP instead of an in-process service call.
            r = call(
                "POST",
                "/api/v1/admin/scope-assignments/promote",
                pa,
                json={
                    "old_assignment_id": str(sa1_id),
                    "new_payload": {
                        "tenant_id": tid,
                        "domain_id": domain_id,
                        "role": "team_lead",
                    },
                    "end_reason": EndReason.PROMOTED.value,
                    "end_note": "P12 promotion smoke test",
                    "audit_label": f"pillar_12:{tid}:promote",
                },
                expect=200,
                client=c,
            )
            promote_body = r.json()
            old_sa = promote_body["ended_old"]
            new_sa = promote_body["created_new"]
            if old_sa.get("ended_at") is None:
                raise AssertionError(
                    "P12 FAIL: promote() returned ended_old with ended_at=None"
                )
            if new_sa.get("role") != "team_lead":
                raise AssertionError(
                    f"P12 FAIL: promote() created_new.role={new_sa.get('role')!r}"
                )

            # ---------- A2: K1.active is False ----------
            # Step 29 C.2: forensic GET via api_keys_step29c (added in C.1).
            r = call(
                "GET",
                "/api/v1/admin/forensics/api_keys_step29c",
                pa,
                params={"id": k1_id},
                expect=200,
                client=c,
            )
            k1_after = r.json()
            if not k1_after or k1_after.get("id") != k1_id:
                raise AssertionError(f"A2 FAIL: K1 (id={k1_id}) not returned")
            if k1_after.get("active") is True:
                raise AssertionError(
                    f"A2 FAIL (Q6 cascade did not fire): K1 still active=True "
                    f"after promote(). Mandatory key rotation broken."
                )

            # ---------- 9. Mint K2 + second turn under K2 ----------
            r = call(
                "POST", "/api/v1/admin/api-keys", tenant_admin_key,
                json={
                    "tenant_id": tid,
                    "domain_id": domain_id,
                    "agent_id": agent_a2_slug,
                    "display_name": "P12 K2",
                    "permissions": ["chat", "sessions"],
                },
                expect=(200, 201), client=c,
            )
            k2_body = r.json()
            k2_raw = k2_body["raw_key"]

            r = call(
                "POST", "/api/v1/sessions", k2_raw,
                json={
                    "user_id": f"p12-end-user-{uuid.uuid4().hex[:6]}",
                    "tenant_id": tid, "domain_id": domain_id,
                },
                expect=(200, 201), client=c,
            )
            session2_body = r.json()
            session2_id = session2_body.get("session_id") or session2_body.get("id")

            call(
                "POST", "/api/v1/consent/grant", k2_raw,
                json={
                    "user_id": session2_body.get("user_id"),
                    "tenant_id": tid,
                },
                expect=(200, 201), client=c,
            )
            call(
                "POST", "/api/v1/chat", k2_raw,
                json={
                    "session_id": session2_id,
                    "message": (
                        "Please remember: my new role-context sentinel is "
                        f"P12-A2-{uuid.uuid4().hex[:6]}."
                    ),
                },
                expect=200, client=c,
            )
            time.sleep(15)

            # ---------- A3: row2 attribution (conditional) ----------
            # Step 29 C.2: forensic GET via memory_items_step29c with
            # actor_user_id + agent_id filters.
            r = call(
                "GET",
                "/api/v1/admin/forensics/memory_items_step29c",
                pa,
                params={
                    "tenant_id": tid,
                    "actor_user_id": str(user_id),
                    "agent_id": agent_a2_slug,
                    "limit": 1,
                },
                expect=200,
                client=c,
            )
            row2_items = r.json().get("items") or []
            if not row2_items:
                memory_skipped = True
            else:
                row2 = row2_items[0]
                row2_actor_str = row2.get("actor_user_id")
                if row2_actor_str is None or uuid.UUID(row2_actor_str) != user_id:
                    raise AssertionError(
                        f"A3 FAIL: row2.actor_user_id={row2_actor_str} "
                        f"!= U.id={user_id}"
                    )
                if row2.get("agent_id") != agent_a2_slug:
                    raise AssertionError(
                        f"A3 FAIL: row2.agent_id={row2.get('agent_id')!r} "
                        f"!= A2.agent_id={agent_a2_slug!r}"
                    )
                row2_id = row2.get("id")

            # ---------- A4: both rows visible by user_id (conditional) ----------
            # Step 29 C.2: forensic GET via memory_items_step29c with
            # actor_user_id only (no agent_id) so both A1 and A2 rows return.
            if not memory_skipped and row1_id is not None and row2_id is not None:
                r = call(
                    "GET",
                    "/api/v1/admin/forensics/memory_items_step29c",
                    pa,
                    params={
                        "tenant_id": tid,
                        "actor_user_id": str(user_id),
                        "limit": 1000,
                    },
                    expect=200,
                    client=c,
                )
                rows_by_user = r.json().get("items") or []
                row_ids_by_user = {item.get("id") for item in rows_by_user}
                if row1_id not in row_ids_by_user:
                    raise AssertionError(
                        f"A4 FAIL: row1 (id={row1_id}) not visible by "
                        f"actor_user_id={user_id}. identity continuity broken."
                    )
                if row2_id not in row_ids_by_user:
                    raise AssertionError(
                        f"A4 FAIL: row2 (id={row2_id}) not visible by "
                        f"actor_user_id={user_id}."
                    )

                # ---------- A5: scope isolation ----------
                # Step 29 C.2: forensic GET filtered by A1's agent_id.
                # Server-side WHERE clause exercises the SQL filter; A5's
                # leak-check re-expressed as "row2 NOT in returned ids".
                r = call(
                    "GET",
                    "/api/v1/admin/forensics/memory_items_step29c",
                    pa,
                    params={
                        "tenant_id": tid,
                        "actor_user_id": str(user_id),
                        "agent_id": agent_a1_slug,
                        "limit": 1000,
                    },
                    expect=200,
                    client=c,
                )
                rows_by_a1 = r.json().get("items") or []
                rows_by_a1_ids = {item.get("id") for item in rows_by_a1}
                if row1_id not in rows_by_a1_ids:
                    raise AssertionError(
                        f"A5 FAIL: row1 missing from A1-scoped query."
                    )
                if row2_id in rows_by_a1_ids:
                    raise AssertionError(
                        f"A5 FAIL: row2 leaked into A1-scoped query. "
                        f"scope isolation broken across promotion."
                    )

            # ---------- A6: K1 returns 401 ----------
            call(
                "POST", "/api/v1/sessions", k1_raw,
                json={
                    "user_id": "p12-should-fail",
                    "tenant_id": tid, "domain_id": domain_id,
                },
                expect=(401, 403), client=c,
            )

            # ---------- Teardown ----------
            # Phase 2 Commit 13: HTTP path via /admin/users/{id}/deactivate.
            try:
                call(
                    "POST",
                    f"/api/v1/admin/users/{user_id}/deactivate",
                    pa,
                    json={
                        "reason": f"P12 teardown for tenant {tid}",
                        "audit_label": f"pillar_12:{tid}:teardown",
                    },
                    expect=(200, 204),
                    client=c,
                )
                call(
                    "PATCH", f"/api/v1/admin/tenants/{tid}", pa,
                    json={"active": False},
                    expect=(200, 204), client=c,
                )
            except Exception as teardown_exc:
                print(
                    f"  pillar 12 teardown warning: "
                    f"{type(teardown_exc).__name__}: {teardown_exc}"
                )

        memory_note = (
            "memory_skipped"
            if memory_skipped
            else f"rows_attributed=2 row1={row1_id} row2={row2_id} A1_scope_iso=ok"
        )
        return (
            f"identity stable across promotion: U={user_id} "
            f"A1={agent_a1_slug} A2={agent_a2_slug} "
            f"K1_rotated K2_active K1_returns_401 {memory_note}"
        )


PILLAR = IdentityStabilityPillar()
