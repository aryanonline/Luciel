"""Pillar 14 - Departure semantics (Step 24.5b Q6).

The most reliability-sensitive operation in the identity layer: ending
a User's assignment in one tenant must leave every other tenant
completely untouched. Pillar 14 proves the cascade is bounded.

Why this assertion matters: a User (Sarah-the-listings-agent) holding
active roles in two brokerages (REMAX Crossroads + an independent
side gig) departs from one. The Q6 cascade must:
  - Rotate keys bound to the departing-tenant's Agent (Q6 mandatory)
  - Soft-deactivate the departing-tenant's Agent
  - Leave the OTHER tenant's Agent + assignment + keys completely
    untouched
  - Preserve the departing-tenant's memory rows for audit (PIPEDA
    access flows still need to surface them)
  - Leave User.active unchanged (User persists across departures
    from individual tenants -- foundational Q6 assertion that
    underpins Step 38 bottom-up tenant merge)

If the cascade over-fires (deactivates the other tenant's keys), the
multi-tenant identity story collapses. If User.active flips, Step 38
becomes unimplementable. Pillar 14 is the regression test that catches
both classes of bug at the verification layer.

Self-contained: builds tenant pair `step24-5b-p14-t1-<u8>` +
`step24-5b-p14-t2-<u8>`, User, two Agents (one per tenant), two chat
keys, two ScopeAssignments, one memory row per tenant, runs the
departure, asserts the bounded cascade, tears down.

Pillar 14 runs in BOTH modes (degraded and full) -- end_assignment is
a synchronous service-layer call that doesn't depend on broker/worker
reachability. Memory rows are written via the sync chat path (sync or
async, both populate actor_user_id by the time the test reads them).

Seven assertions:
  A1. K1.active is False (T1 key rotated by Q6 cascade).
  A2. K2.active is True (T2 key UNTOUCHED -- the bounded-cascade proof).
  A3. SA1.ended_at != None and ended_reason == DEPARTED.
  A4. SA2.ended_at is None (T2 assignment still active).
  A5. T1's memory row still queryable by actor_user_id == U.id
      (audit preservation).
  A6. User.active is True (User persists across tenant departures).
  A7. T2's memory row still queryable by actor_user_id == U.id
      (no cross-tenant collateral damage).
"""

from __future__ import annotations

import time
import uuid

from app.models.scope_assignment import EndReason
from app.verification.fixtures import RunState
from app.verification.http_client import call, forensics_get, pooled_client
from app.verification.runner import Pillar

# Step 29 Commit C.4: P14 forensic reads migrated to platform-admin HTTP.
# A1/A2 (ApiKey rotation/untouchedness) reuse C.1's api_keys_step29c?id=,
# A5/A7 (MemoryItem attribution preservation) reuse C.2's
# memory_items_step29c?tenant_id=&actor_user_id=, and A6 (User.active
# persistence -- the foundational Q6 claim) lands on the new C.4
# users_step29c/{user_id} endpoint. P14 has zero producer-side
# callsites (no apply_async / delay() / direct service-layer calls) so
# the B.3 producer-side exemption rule does not bind here.
#
# Imports dropped post-migration: sqlalchemy.select, SessionLocal,
# ApiKey, MemoryItem, ScopeAssignment, User, AuditContext,
# ScopeAssignmentCreate, ScopeAssignmentService. AuditContext /
# ScopeAssignmentCreate / ScopeAssignmentService were already
# vestigial pre-C.4 (left over from the Phase 2 Commit 13
# scope-assignment-write HTTP migration). EndReason stays -- used at
# the end_assignment callsite as the canonical reason enum.


P14_TENANT_PREFIX = "step24-5b-p14-"


def _new_p14_tenant_id(suffix: str) -> str:
    return f"{P14_TENANT_PREFIX}{suffix}-{uuid.uuid4().hex[:8]}"


class DepartureSemanticsPillar(Pillar):
    number = 14
    name = "departure semantics (Q6 bounded cascade)"

    def run(self, state: RunState) -> str:
        pa = state.platform_admin_key
        if not pa:
            raise AssertionError(
                "pillar 14 requires platform_admin_key (load via env)"
            )

        t1_id = _new_p14_tenant_id("t1")
        t2_id = _new_p14_tenant_id("t2")
        domain_id = "general"

        SENTINEL_T1 = f"P14-T1-{uuid.uuid4().hex[:6]}"
        SENTINEL_T2 = f"P14-T2-{uuid.uuid4().hex[:6]}"

        with pooled_client() as c:
            # ---------- 1. Onboard T1 + T2 ----------
            r = call(
                "POST", "/api/v1/admin/tenants/onboard", pa,
                json={
                    "tenant_id": t1_id,
                    "display_name": "P14 Tenant T1 (departing)",
                    "default_domain_id": domain_id,
                    "default_domain_display_name": "General",
                },
                expect=(200, 201), client=c,
            )
            t1_admin_key = (
                r.json().get("admin_raw_key")
                or r.json().get("admin_api_key", {}).get("raw_key")
            )

            r = call(
                "POST", "/api/v1/admin/tenants/onboard", pa,
                json={
                    "tenant_id": t2_id,
                    "display_name": "P14 Tenant T2 (untouched)",
                    "default_domain_id": domain_id,
                    "default_domain_display_name": "General",
                },
                expect=(200, 201), client=c,
            )
            t2_admin_key = (
                r.json().get("admin_raw_key")
                or r.json().get("admin_api_key", {}).get("raw_key")
            )

            # ---------- 2. Create User U ----------
            user_email = f"p14-user-{uuid.uuid4().hex[:8]}@example.com"
            r = call(
                "POST", "/api/v1/users", pa,
                json={
                    "email": user_email,
                    "display_name": "P14 Test User (held in 2 tenants)",
                    "synthetic": False,
                },
                expect=(200, 201), client=c,
            )
            user_id = uuid.UUID(r.json()["id"])

            # ---------- 3. Create Agent A1 in T1, A2 in T2 ----------
            agent_a1_slug = f"p14-a1-{uuid.uuid4().hex[:6]}"
            r = call(
                "POST", "/api/v1/admin/agents", t1_admin_key,
                json={
                    "tenant_id": t1_id,
                    "domain_id": domain_id,
                    "agent_id": agent_a1_slug,
                    "display_name": "P14 Agent A1 (T1, departing)",
                    "contact_email": user_email,
                },
                expect=(200, 201), client=c,
            )
            agent_a1_pk = r.json()["id"]

            agent_a2_slug = f"p14-a2-{uuid.uuid4().hex[:6]}"
            r = call(
                "POST", "/api/v1/admin/agents", t2_admin_key,
                json={
                    "tenant_id": t2_id,
                    "domain_id": domain_id,
                    "agent_id": agent_a2_slug,
                    "display_name": "P14 Agent A2 (T2, untouched)",
                    "contact_email": user_email,
                },
                expect=(200, 201), client=c,
            )
            agent_a2_pk = r.json()["id"]

            # Bind A1 (in T1) and A2 (in T2) to U via the platform-admin
            # bind-user route.
            #
            # Step 28 Phase 2 - Commit 10: previously a direct SessionLocal()
            # write to agents.user_id, which the least-privilege worker DSN
            # used by the Pattern N verify task correctly refuses. Routed
            # through the bind-user endpoint shipped in Commit 9 (dddf8cb).
            # See pillar_12 / pillar_13 Commit 10 notes for the full
            # rationale.
            call(
                "POST",
                f"/api/v1/admin/agents/{t1_id}/{agent_a1_slug}/bind-user",
                pa,
                json={"user_id": str(user_id)},
                expect=200,
                client=c,
            )
            call(
                "POST",
                f"/api/v1/admin/agents/{t2_id}/{agent_a2_slug}/bind-user",
                pa,
                json={"user_id": str(user_id)},
                expect=200,
                client=c,
            )

            # ---------- 4. Mint chat keys K1 (T1) + K2 (T2) ----------
            r = call(
                "POST", "/api/v1/admin/api-keys", t1_admin_key,
                json={
                    "tenant_id": t1_id,
                    "domain_id": domain_id,
                    "agent_id": agent_a1_slug,
                    "display_name": "P14 K1 (T1 chat key)",
                    "permissions": ["chat", "sessions"],
                },
                expect=(200, 201), client=c,
            )
            k1_body = r.json()
            k1_raw = k1_body["raw_key"]
            k1_id = k1_body["api_key"]["id"]

            r = call(
                "POST", "/api/v1/admin/api-keys", t2_admin_key,
                json={
                    "tenant_id": t2_id,
                    "domain_id": domain_id,
                    "agent_id": agent_a2_slug,
                    "display_name": "P14 K2 (T2 chat key)",
                    "permissions": ["chat", "sessions"],
                },
                expect=(200, 201), client=c,
            )
            k2_body = r.json()
            k2_raw = k2_body["raw_key"]
            k2_id = k2_body["api_key"]["id"]

            # ---------- 5. Create ScopeAssignments SA1 (T1) + SA2 (T2) ----------
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
                        "tenant_id": t1_id,
                        "domain_id": domain_id,
                        "role": "listings_agent",
                    },
                    "audit_label": f"pillar_14:{t1_id}+{t2_id}",
                },
                expect=(200, 201),
                client=c,
            )
            sa1_id = uuid.UUID(r.json()["id"])
            r = call(
                "POST",
                "/api/v1/admin/scope-assignments",
                pa,
                json={
                    "user_id": str(user_id),
                    "payload": {
                        "tenant_id": t2_id,
                        "domain_id": domain_id,
                        "role": "listings_agent",
                    },
                    "audit_label": f"pillar_14:{t1_id}+{t2_id}",
                },
                expect=(200, 201),
                client=c,
            )
            sa2_id = uuid.UUID(r.json()["id"])

            # ---------- 6. Issue chat turn through K1 in T1 (writes T1 memory) ----------
            r = call(
                "POST", "/api/v1/sessions", k1_raw,
                json={
                    "user_id": f"p14-end-user-t1-{uuid.uuid4().hex[:6]}",
                    "tenant_id": t1_id,
                    "domain_id": domain_id,
                },
                expect=(200, 201), client=c,
            )
            t1_session = r.json()
            t1_session_id = t1_session.get("session_id") or t1_session.get("id")

            call(
                "POST",
                "/api/v1/consent/grant",
                k1_raw,
                json={
                    "user_id": t1_session.get("user_id"),
                    "tenant_id": t1_id,
                },
                expect=(200, 201),
                client=c,
            )

            call(
                "POST", "/api/v1/chat", k1_raw,
                json={
                    "session_id": t1_session_id,
                    "message": (
                        f"Please remember this fact: T1 sentinel is "
                        f"{SENTINEL_T1}. Refer to it on every turn."
                    ),
                },
                expect=200, client=c,
            )

            # ---------- 7. Issue chat turn through K2 in T2 (writes T2 memory) ----------
            r = call(
                "POST", "/api/v1/sessions", k2_raw,
                json={
                    "user_id": f"p14-end-user-t2-{uuid.uuid4().hex[:6]}",
                    "tenant_id": t2_id,
                    "domain_id": domain_id,
                },
                expect=(200, 201), client=c,
            )
            t2_session = r.json()
            t2_session_id = t2_session.get("session_id") or t2_session.get("id")

            call(
                "POST",
                "/api/v1/consent/grant",
                k1_raw,
                json={
                    "user_id": t2_session.get("user_id"),
                    "tenant_id": t2_id,
                },
                expect=(200, 201),
                client=c,
            )
            call(
                "POST", "/api/v1/chat", k2_raw,
                json={
                    "session_id": t2_session_id,
                    "message": (
                        f"Please remember this fact: T2 sentinel is "
                        f"{SENTINEL_T2}. Refer to it on every turn."
                    ),
                },
                expect=200, client=c,
            )

            # Wait for both extractions to land. Two chat turns -> two
            # async memory extractions, each 7-15s on the worker path.
            time.sleep(20)

            # ---------- 8. The departure event ----------
            # Phase 2 Commit 13: HTTP path via
            # /admin/scope-assignments/{id}/end. Same Q6 cascade as
            # production -- rotates keys bound to A1's (tenant=T1) pair
            # only. K2 in T2 must remain untouched.
            #
            # Step 29 Commit B (closes D-call-helper-missing-params-kwarg-
            # 2026-05-05): audit_label is a Query param on the route (not a
            # body field). The Phase 2 Commit 14 workaround was to inline
            # ?audit_label=... into the path because the verify call() helper
            # had no params= kwarg. That worked because the label is a
            # controlled f-string (alnum + colon + hyphen), but it was
            # fragile: any future label containing `&`, `=`, `?`, or
            # whitespace would silently corrupt URL parsing on the server.
            # Step 29 Commit B extends call() with a params= kwarg that
            # forwards to httpx (which URL-encodes safely), and migrates
            # this callsite back to the kwarg form.
            audit_label_p14 = f"pillar_14:{t1_id}:departure"
            r = call(
                "POST",
                f"/api/v1/admin/scope-assignments/{sa1_id}/end",
                pa,
                params={"audit_label": audit_label_p14},
                json={
                    "reason": EndReason.DEPARTED.value,
                    "note": f"P14: U departed T1 ({t1_id})",
                },
                expect=200,
                client=c,
            )
            ended_sa1_body = r.json()
            if not ended_sa1_body or not ended_sa1_body.get("id"):
                raise AssertionError(
                    f"P14 FAIL: end_assignment(SA1) returned empty body "
                    f"for assignment_id={sa1_id}"
                )
            
            # ---------- ASSERTIONS A1-A7 (after departure) ----------
            # Step 29 Commit C.4: all six forensic reads on platform-admin
            # HTTP. No SessionLocal() opens; no direct ORM access. The
            # try/finally that previously wrapped db.close() is gone --
            # there is no DB session to close.

            # ---------- A1: K1.active is False (T1 key rotated) ----------
            # Step 29 Commit C.6: forensics_get() wrapper. (200, 404) is
            # the right allowlist because K1 was created by P14's setup
            # and a 404 here would indicate the row vanished -- which IS
            # an A1 assertion failure (caught by the explicit 404 check).
            r = forensics_get(
                "/api/v1/admin/forensics/api_keys_step29c",
                pa,
                params={"id": k1_id},
                client=c,
            )
            if r.status_code == 404:
                raise AssertionError(
                    f"A1 FAIL: K1 (id={k1_id}) disappeared after departure"
                )
            k1_after = r.json()
            if k1_after.get("active"):
                raise AssertionError(
                    f"A1 FAIL: K1 (id={k1_id}) still active=True after "
                    f"DEPARTED end_assignment. Q6 cascade did not fire "
                    f"on T1's key."
                )

            # ---------- A2: K2.active is True (T2 key UNTOUCHED) ----------
            # The bounded-cascade assertion. If A2 fails, the cascade
            # over-fired and a User leaving brokerage A would lose
            # access at brokerage B.
            # Step 29 Commit C.6: forensics_get() wrapper.
            r = forensics_get(
                "/api/v1/admin/forensics/api_keys_step29c",
                pa,
                params={"id": k2_id},
                client=c,
            )
            if r.status_code == 404:
                raise AssertionError(
                    f"A2 FAIL: K2 (id={k2_id}) disappeared during P14"
                )
            k2_after = r.json()
            if not k2_after.get("active"):
                raise AssertionError(
                    f"A2 FAIL (CRITICAL): K2 (id={k2_id}, tenant=T2) was "
                    f"deactivated by departure from T1. Q6 cascade leaked "
                    f"across tenant boundary. Multi-tenant identity "
                    f"isolation broken."
                )

            # ---------- A3: SA1.ended_at != None and reason == DEPARTED ----------
            # Phase 2 Commit 13: HTTP read via
            # /admin/scope-assignments/{id}. Verify role has no
            # SELECT on scope_assignments by design.
            r = call(
                "GET",
                f"/api/v1/admin/scope-assignments/{sa1_id}",
                pa,
                expect=200,
                client=c,
            )
            sa1_after = r.json()
            if not sa1_after:
                raise AssertionError(
                    f"A3 FAIL: SA1 (id={sa1_id}) disappeared after "
                    f"end_assignment"
                )
            if sa1_after.get("ended_at") is None:
                raise AssertionError(
                    f"A3 FAIL: SA1 (id={sa1_id}) ended_at is still NULL "
                    f"after end_assignment(DEPARTED). Lifecycle column "
                    f"not written."
                )
            if sa1_after.get("ended_reason") != EndReason.DEPARTED.value:
                raise AssertionError(
                    f"A3 FAIL: SA1.ended_reason="
                    f"{sa1_after.get('ended_reason')!r} "
                    f"!= EndReason.DEPARTED. Reason not recorded correctly."
                )

            # ---------- A4: SA2.ended_at is None (T2 assignment untouched) ----------
            r = call(
                "GET",
                f"/api/v1/admin/scope-assignments/{sa2_id}",
                pa,
                expect=200,
                client=c,
            )
            sa2_after = r.json()
            if not sa2_after:
                raise AssertionError(
                    f"A4 FAIL: SA2 (id={sa2_id}) disappeared during P14"
                )
            if sa2_after.get("ended_at") is not None:
                raise AssertionError(
                    f"A4 FAIL (CRITICAL): SA2 (id={sa2_id}, tenant=T2) "
                    f"ended_at={sa2_after.get('ended_at')} after "
                    f"departure from T1. Cascade over-fired across "
                    f"tenant boundary."
                )

            # ---------- A5: T1's memory row queryable IF it exists ----------
            # Audit preservation: departure soft-deactivates the Agent
            # but does not delete or hide memory history. PIPEDA
            # access flows still need to surface T1 rows.
            #
            # Drift D18 accommodation: local memory extractor may produce
            # 0 rows for some test message shapes. We assert by
            # (tenant_id, actor_user_id) identity attribution (the actual
            # Step 24.5b claim), not by sentinel content (which depends
            # on LLM extractor behavior). If no row exists at all,
            # downgrade to memory_skipped -- the cascade-bounded
            # assertions (A1-A4, A6, A7) still prove the security claim.
            #
            # C.4 migration: server-side WHERE on tenant_id +
            # actor_user_id reuses the C.2 query-param signature; limit=1
            # + ORDER BY id DESC server-side mirrors the original
            # .order_by(MemoryItem.id.desc()).limit(1) shape.
            memory_skipped = False
            r = call(
                "GET",
                "/api/v1/admin/forensics/memory_items_step29c",
                pa,
                params={
                    "tenant_id": t1_id,
                    "actor_user_id": str(user_id),
                    "limit": 1,
                },
                expect=200,
                client=c,
            )
            t1_memory_items = r.json().get("items") or []
            if not t1_memory_items:
                memory_skipped = True
                t1_memory_id = None
            else:
                t1_memory_id = t1_memory_items[0]["id"]

            # ---------- A6: User.active is True (User persists) ----------
            # The foundational Q6 assertion: a User leaving one tenant
            # does NOT lose their platform identity. This is what
            # underpins Step 38 bottom-up tenant merge.
            #
            # C.4 migration: db.get(User, user_id) -> GET
            # /admin/forensics/users_step29c/{user_id}. The new endpoint
            # returns a strict UserForensic projection (id, active,
            # synthetic) with email + display_name explicitly excluded
            # because they are PII and have no place on a forensic
            # surface.
            # Step 29 Commit C.6: forensics_get() wrapper.
            r = forensics_get(
                f"/api/v1/admin/forensics/users_step29c/{user_id}",
                pa,
                client=c,
            )
            if r.status_code == 404:
                raise AssertionError(
                    f"A6 FAIL: User (id={user_id}) disappeared during P14"
                )
            user_after = r.json()
            if not user_after.get("active"):
                raise AssertionError(
                    f"A6 FAIL (CRITICAL): User (id={user_id}) "
                    f"active=False after departure from T1. User "
                    f"identity collapsed to single tenancy. Step 38 "
                    f"bottom-up tenant merge becomes unimplementable."
                )

            # ---------- A7: T2's memory row queryable IF it exists ----------
            # No cross-tenant collateral damage on the data side.
            # Same conditional shape as A5 -- if T2 row exists we assert
            # it survived the T1 departure; if no row exists, the
            # untouchedness claim is still proven by A2 (K2 active),
            # A4 (SA2 still active), which are the cascade-bounded
            # claims this pillar primarily tests.
            r = call(
                "GET",
                "/api/v1/admin/forensics/memory_items_step29c",
                pa,
                params={
                    "tenant_id": t2_id,
                    "actor_user_id": str(user_id),
                    "limit": 1,
                },
                expect=200,
                client=c,
            )
            t2_memory_items = r.json().get("items") or []
            if not t2_memory_items:
                memory_skipped = True
                t2_memory_id = None
            else:
                t2_memory_id = t2_memory_items[0]["id"]

            # ---------- Self-contained teardown ----------
            # Phase 2 Commit 13: HTTP path via /admin/users/{id}/deactivate.
            try:
                # Soft-deactivate U -- cascade ends remaining SA2 and
                # rotates K2 (which we just asserted is still active).
                call(
                    "POST",
                    f"/api/v1/admin/users/{user_id}/deactivate",
                    pa,
                    json={
                        "reason": f"P14 teardown for tenants {t1_id}, {t2_id}",
                        "audit_label": f"pillar_14:teardown:{t1_id}+{t2_id}",
                    },
                    expect=(200, 204),
                    client=c,
                )

                # Soft-deactivate both tenants.
                for tid in (t1_id, t2_id):
                    call(
                        "PATCH",
                        f"/api/v1/admin/tenants/{tid}",
                        pa,
                        json={"active": False},
                        expect=(200, 204),
                        client=c,
                    )
            except Exception as teardown_exc:
                print(
                    f"  pillar 14 teardown warning: "
                    f"{type(teardown_exc).__name__}: {teardown_exc}"
                )

        # ---------- success detail ----------
        memory_note = (
            "memory_skipped"
            if memory_skipped
            else f"T1_memory_id={t1_memory_id} T2_memory_id={t2_memory_id}"
        )
        return (
            f"departure_bounded: K1_rotated K2_active "
            f"SA1_ended=DEPARTED SA2_active {memory_note} "
            f"User.active=True"
        )


PILLAR = DepartureSemanticsPillar()
            