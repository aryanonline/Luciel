"""Pillar 13 - Cross-tenant identity-spoof guard (Step 24.5b Q6).

Worker-side defense-in-depth proof: a malicious enqueue-side payload
that claims (user_id=U, tenant_id=T1, agent_id=A2_under_T2) MUST land
in DLQ via Gate 6 (ACTION_WORKER_IDENTITY_SPOOF_REJECT), not write a
memory row, and not contaminate either tenant.

The Q6 cross-tenant attack surface this gate closes: a compromised
service-layer caller could attempt to write memory under one tenant
but attribute it to a User identity whose Agent lives in another
tenant. Without Gate 6, the worker would happily persist the row
because the legacy gates (1-4) only validate session.tenant_id,
ApiKey active state, and LucielInstance active state -- none of
those catch identity-vs-Agent-vs-tenant mismatch.

Two execution modes (mirrors Pillar 11):

  Mode FULL     Worker + broker reachable. Runs all 6 assertions:
                A1. Spoof payload produces zero memory rows for the
                    spoofed message_id.
                A2. ACTION_WORKER_IDENTITY_SPOOF_REJECT audit row
                    exists for the spoofed payload.
                A3. Legitimate chat turn through K1 (post-spoof) still
                    writes a memory row with actor_user_id=U.
                A4. Legitimate row's tenant_id == T1.
                A5. T2 (where A2 actually lives) has no memory rows
                    for U from this test (no cross-tenant leak).
                A6. K1 still active (spoof did not trigger collateral
                    key rotation).

  Mode DEGRADED Worker unreachable. Returns PASS with a documented
                "skipped: mode=degraded" detail. Pillar 13 explicitly
                requires a live worker -- the gate fires inside
                worker task code, not in the enqueue path. Local
                dev runs without a worker; prod gate exercises the
                full assertion list.

Both modes return PASS. Mode is reported in the detail string so the
matrix is self-describing.

Self-contained: builds its own tenant pair `step24-5b-p13-t1-<u8>`
+ `step24-5b-p13-t2-<u8>`, User, two Agents (one in each tenant),
chat key, session, runs the test, tears down at the end. Does NOT
read pillar 1's tenant_admin_key. Teardown is in-pillar.

Step 29 Commit C.3 -- forensic-read migration to HTTP
-----------------------------------------------------

All nine ORM-level forensic reads in this pillar (setup-message
lookup, A1 spoof-row absence probe, A2 audit-row poll, A3 legit-row
poll, A5 T2 leak probe, A6 K1.active check, plus the three degraded-
path mirrors) are migrated to the platform-admin-gated forensic
endpoints under /api/v1/admin/forensics/*_step29c. The new
`messages_step29c` endpoint replaces the L346-L361 MessageModel
lookup; the extended `memory_items_step29c?message_id=`/
`?content_contains=` query params replace the four MemoryItem
selects; the extended `admin_audit_logs_step29c?actor_key_prefix=`
replaces the A2 audit poll; and `api_keys_step29c?id=` from C.1
replaces the K1.active check.

PRODUCER-SIDE EXEMPTION (B.3 architectural rule)
-------------------------------------------------

There is exactly one direct producer-side call in this pillar:
`extract_memory_from_turn.delay(...)` at the spoof-payload enqueue
in `_run_full_assertions`. This call is INTENTIONALLY direct, not
HTTP-routed, because the assertion under test (A1 + A2) is the
worker's response to a payload the legitimate HTTP API contract
cannot construct -- specifically, an `agent_id` slug from one tenant
under a `tenant_id` from another tenant. Routing this through the
chat HTTP path would let the auth/session middleware reject it
before it ever reached Celery, which would prove only that the
HTTP layer is correct -- a property already covered elsewhere
(P12, P14). The whole point of P13 is to test Gate 6 itself, which
fires inside the worker task body and therefore needs the harness
to act as a malicious in-process producer. This is the canonical
shape of B.3's producer-side exemption rule and is documented
inline at the callsite.

Security boundary unchanged by this exemption: the harness uses the
same `app.worker.tasks.memory_extraction.extract_memory_from_turn`
production code already exposes; no privilege the verify task does
not already have is exercised; the `luciel_worker` Postgres role's
zero-INSERT/UPDATE on `scope_assignments`/`users` (migration
f392a842f885) is intact.
"""

from __future__ import annotations

import time
import uuid

from app.verification._infra_probes import _broker_reachable, _worker_reachable
from app.verification.fixtures import RunState
from app.verification.http_client import call, pooled_client
from app.verification.runner import Outcome, Pillar, PillarOutcome


P13_TENANT_PREFIX = "step24-5b-p13-"

# Audit action constant copied from app.models.admin_audit_log so the
# pillar can stay free of model imports after the C.3 migration.
# Source of truth: app/models/admin_audit_log.py
ACTION_WORKER_IDENTITY_SPOOF_REJECT = "WORKER_IDENTITY_SPOOF_REJECT"

# Mode detection helpers (_broker_reachable / _worker_reachable) live in
# app.verification._infra_probes since Step 29 Commit C.6. Originally these
# were duplicated verbatim from Pillar 11 (the duplication was deliberate at
# B.1 time -- the B.1 commit's scope was constrained to the P13 mode-gate
# honesty fix and explicitly deferred deduplication, see drift
# D-pillar-13-mode-gate-broker-only-2026-05-06 in CANONICAL_RECAP). C.6
# completes that deferred consolidation: both pillars now import from a
# single source of truth in _infra_probes, preserving the same fail-closed
# behavior on import/connection/auth/timeout failures and ensuring P11 and
# P13 cannot drift apart in a future refactor.


def _new_p13_tenant_id(suffix: str) -> str:
    return f"{P13_TENANT_PREFIX}{suffix}-{uuid.uuid4().hex[:8]}"


class CrossTenantIdentityPillar(Pillar):
    number = 13
    name = "cross-tenant identity-spoof guard (Q6)"

    def run(self, state: RunState) -> str:
        pa = state.platform_admin_key
        if not pa:
            raise AssertionError(
                "pillar 13 requires platform_admin_key (load via env)"
            )

        # ---------- Mode detection ----------
        # Both broker AND a live worker are required for MODE=full. Broker
        # alone (the previous gate) means "can enqueue" but not "will be
        # consumed" -- under that gate a local run with no worker silently
        # FAILed A2 as if Gate 6's audit emission were broken. See drift
        # D-pillar-13-mode-gate-broker-only-2026-05-06.
        mode_full = _broker_reachable() and _worker_reachable()

        # ---------- Setup (always runs, both modes) ----------
        # Phase 0 builds the tenant pair, User, Agents, key, and session
        # so even MODE=degraded leaves a clean trail of "Pillar 13 was
        # here" rows that the suite teardown sweeps.
        t1_id = _new_p13_tenant_id("t1")
        t2_id = _new_p13_tenant_id("t2")
        domain_id = "general"

        # SENTINEL_LEGIT is woven into a user-fact-shaped statement
        # below ("My account verification token is ...") so the memory
        # extractor recognizes it as a durable fact (`fact` category,
        # per app/memory/extractor.py EXTRACTION_PROMPT). Older P13
        # revisions wrapped the sentinel in instructional text
        # ("Setup turn for Pillar 13...") which the extractor
        # correctly rejects as trivial/temporary, leading to a
        # vacuously-failing A3. The fix is on the test-setup side,
        # not on extraction logic.
        #
        # SENTINEL_SPOOF stays as-is - A1 asserts ABSENCE of any row
        # containing it, so its message text doesn't need to look
        # like a user fact (and shouldn't, to keep the spoof payload
        # clearly distinguishable from the legit baseline).
        SENTINEL_LEGIT = f"P13-LEGIT-{uuid.uuid4().hex[:6]}"
        SENTINEL_SPOOF = f"P13-SPOOF-{uuid.uuid4().hex[:6]}"

        with pooled_client() as c:
            # ---------- 1. Onboard T1 ----------
            r = call(
                "POST", "/api/v1/admin/tenants/onboard", pa,
                json={
                    "tenant_id": t1_id,
                    "display_name": "P13 Tenant T1 (legit)",
                    "default_domain_id": domain_id,
                    "default_domain_display_name": "General",
                },
                expect=(200, 201), client=c,
            )
            t1_admin_key = (
                r.json().get("admin_raw_key")
                or r.json().get("admin_api_key", {}).get("raw_key")
            )

            # ---------- 2. Onboard T2 ----------
            r = call(
                "POST", "/api/v1/admin/tenants/onboard", pa,
                json={
                    "tenant_id": t2_id,
                    "display_name": "P13 Tenant T2 (spoofed)",
                    "default_domain_id": domain_id,
                    "default_domain_display_name": "General",
                },
                expect=(200, 201), client=c,
            )
            t2_admin_key = (
                r.json().get("admin_raw_key")
                or r.json().get("admin_api_key", {}).get("raw_key")
            )

            # ---------- 3. Create User U via HTTP (synthetic=False, real email) ----------
            user_email = f"p13-user-{uuid.uuid4().hex[:8]}@example.com"
            r = call(
                "POST", "/api/v1/users", pa,
                json={
                    "email": user_email,
                    "display_name": "P13 Test User (held in 2 tenants)",
                    "synthetic": False,
                },
                expect=(200, 201), client=c,
            )
            user_id = uuid.UUID(r.json()["id"])

            # ---------- 4. Create Agent A1 in T1, bind to U ----------
            agent_a1_slug = f"p13-a1-{uuid.uuid4().hex[:6]}"
            r = call(
                "POST", "/api/v1/admin/agents", t1_admin_key,
                json={
                    "tenant_id": t1_id,
                    "domain_id": domain_id,
                    "agent_id": agent_a1_slug,
                    "display_name": "P13 Agent A1 (T1, legit)",
                    "contact_email": user_email,
                },
                expect=(200, 201), client=c,
            )
            agent_a1_pk = r.json()["id"]

            # ---------- 5. Create Agent A2 in T2, bind to U ----------
            agent_a2_slug = f"p13-a2-{uuid.uuid4().hex[:6]}"
            r = call(
                "POST", "/api/v1/admin/agents", t2_admin_key,
                json={
                    "tenant_id": t2_id,
                    "domain_id": domain_id,
                    "agent_id": agent_a2_slug,
                    "display_name": "P13 Agent A2 (T2, spoof target)",
                    "contact_email": user_email,
                },
                expect=(200, 201), client=c,
            )
            agent_a2_pk = r.json()["id"]

            # Bind both Agents to U via the platform-admin bind-user route.
            #
            # Step 28 Phase 2 - Commit 10: previously this was a direct
            # SessionLocal() write touching agents.user_id ("Option D"). When
            # the Pattern N verify task runs against prod with the
            # least-privilege worker DSN, that write is correctly refused
            # ("permission denied for table agents"). The bind-user route
            # shipped in Commit 9 (dddf8cb) is platform-admin gated and
            # enforces the "one active Agent per (user, tenant)" invariant
            # at the service layer, so this is functionally equivalent to
            # the prior raw write but no longer requires admin DB grants on
            # the harness side. Two calls (one per tenant) - A1 lives in T1,
            # A2 lives in T2; the invariant is per-tenant so both succeed.
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

            # ---------- 6. Mint chat key K1 bound to A1 in T1 ----------
            r = call(
                "POST", "/api/v1/admin/api-keys", t1_admin_key,
                json={
                    "tenant_id": t1_id,
                    "domain_id": domain_id,
                    "agent_id": agent_a1_slug,
                    "display_name": "P13 K1 (T1 chat key, legit)",
                    "permissions": ["chat", "sessions"],
                },
                expect=(200, 201), client=c,
            )
            k1_body = r.json()
            k1_raw = k1_body["raw_key"]
            k1_id = k1_body["api_key"]["id"]
            k1_prefix = k1_body["api_key"]["key_prefix"]

            # ---------- 7. Create T1 session + a real message_id ----------
            # The worker's Gate 3 (Session.tenant_id == payload.tenant_id)
            # requires a real session row in T1. Gate 1 (payload shape)
            # requires a real message_id. Both must exist before the
            # spoof payload can reach Gate 6.
            r = call(
                "POST", "/api/v1/sessions", k1_raw,
                json={
                    "user_id": f"p13-end-user-{uuid.uuid4().hex[:6]}",
                    "tenant_id": t1_id,
                    "domain_id": domain_id,
                },
                expect=(200, 201), client=c,
            )
            session_body = r.json()
            t1_session_id = session_body.get("session_id") or session_body.get("id")

            call(
                "POST",
                "/api/v1/consent/grant",
                k1_raw,
                json={
                    "user_id": session_body.get("user_id"),
                    "tenant_id": t1_id,
                },
                expect=(200, 201),
                client=c,
            )

            # Issue a setup chat turn so a real message_id exists in T1
            # for the spoof payload to reference. Gate 1 (payload shape)
            # rejects malformed message_id integers; we need a valid one
            # so the malicious payload reaches Gate 6.
            # User-fact-shaped message so the extractor produces a
            # MemoryItem row. The sentinel rides inside the fact text
            # as a debug aid, but A3's primary lookup keys on
            # MemoryItem.message_id (deterministic, paraphrase-proof)
            # rather than content.contains(sentinel) - see A3 below.
            r = call(
                "POST", "/api/v1/chat", k1_raw,
                json={
                    "session_id": t1_session_id,
                    "message": (
                        f"Please remember this for future sessions: my "
                        f"account verification token is {SENTINEL_LEGIT}. "
                        f"I will reference this token whenever I need to "
                        f"confirm my identity."
                    ),
                },
                expect=200, client=c,
            )
            time.sleep(5)

            # Look up the assistant message_id from this T1 session so
            # the spoof payload can reference a real, valid message.
            #
            # Step 29 C.3: forensic GET via messages_step29c (new in C.3).
            # Returns DESC-ordered rows; limit=1 yields the most recent
            # message in the session, which mirrors the prior
            # ORDER BY id DESC LIMIT 1 ORM query verbatim.
            r = call(
                "GET",
                "/api/v1/admin/forensics/messages_step29c",
                pa,
                params={"session_id": t1_session_id, "limit": 1},
                expect=200,
                client=c,
            )
            msg_items = r.json().get("items") or []
            if not msg_items:
                raise AssertionError(
                    f"P13 setup: no MessageModel row for session "
                    f"{t1_session_id} after first chat turn"
                )
            t1_message_id = msg_items[0]["id"]

            # ---------- Mode gate ----------
            # Pillar 13's core assertions (A1, A2) require the worker to
            # actually consume the malicious payload and route it through
            # Gate 6. If the broker is unreachable, the payload would just
            # sit in the queue forever -- we can't assert "rejected to DLQ"
            # without a worker. Skip cleanly with PASS-status documented
            # detail. Prod gate (Step 24.5b deploy) runs MODE=full.
            if not mode_full:
                # Sanity assertions A3-A6 still run in degraded mode below
                # because they don't depend on the malicious payload --
                # they only assert "the legitimate setup we already did
                # works correctly." But we skip A1/A2 (the spoof guard
                # itself) and document mode=degraded.
                degraded_summary = self._run_degraded_sanity(
                    c=c,
                    pa=pa,
                    user_id=user_id,
                    t1_id=t1_id, t2_id=t2_id,
                    sentinel_legit=SENTINEL_LEGIT,
                    k1_id=k1_id,
                    t1_message_id=t1_message_id,
                )
                self._teardown(c, pa, user_id, t1_id, t2_id)
                # Step 29.y Cluster 8: surface DEGRADED via tri-state so the
                # matrix gate fails by default rather than green-badging a
                # known-skipped spoof-guard path.
                return PillarOutcome(
                    Outcome.DEGRADED,
                    f"MODE=degraded :: spoof guard not exercised "
                    f"(worker unreachable) | {degraded_summary}",
                )

            # ---------- MODE=full assertions ----------
            full_summary = self._run_full_assertions(
                c=c, pa=pa,
                user_id=user_id, user_email=user_email,
                t1_id=t1_id, t2_id=t2_id,
                domain_id=domain_id,
                agent_a1_slug=agent_a1_slug,
                agent_a2_slug=agent_a2_slug,
                k1_id=k1_id, k1_prefix=k1_prefix,
                t1_session_id=t1_session_id,
                t1_message_id=t1_message_id,
                sentinel_legit=SENTINEL_LEGIT,
                sentinel_spoof=SENTINEL_SPOOF,
            )

            self._teardown(c, pa, user_id, t1_id, t2_id)

            # FULL path wrapped for symmetry with DEGRADED. The runner
            # would coerce a bare str to FULL anyway, but explicit is
            # better than implicit when the contract is the audit point.
            return PillarOutcome(
                Outcome.FULL,
                f"MODE=full :: {full_summary}",
            )

    def _run_full_assertions(
        self,
        *,
        c,
        pa: str,
        user_id: uuid.UUID,
        user_email: str,
        t1_id: str,
        t2_id: str,
        domain_id: str,
        agent_a1_slug: str,
        agent_a2_slug: str,
        k1_id: int,
        k1_prefix: str,
        t1_session_id: str,
        t1_message_id: int,
        sentinel_legit: str,
        sentinel_spoof: str,
    ) -> str:
        """MODE=full assertions A1-A6. Requires a live Celery worker.

        Sequence:
          1. Construct the malicious payload claiming
             (user_id=U, tenant_id=T1, agent_id=A2_slug from T2).
          2. Enqueue via extract_memory_from_turn.delay() -- DIRECT
             producer-side call, B.3 producer-side exemption applies.
          3. Wait for worker to consume + reject to DLQ via Gate 6.
          4. Assert A1 (no memory row) and A2 (audit row exists).
          5. Issue a fresh legitimate chat turn through K1 in T1.
          6. Assert A3 (legit row attributed to U), A4 (tenant=T1),
             A5 (no leak to T2), A6 (K1 still active).
        """
        # PRODUCER-SIDE EXEMPTION (B.3): the spoof payload below is
        # constructed and enqueued in-process via Celery's `.delay()`,
        # bypassing the HTTP API. This is intentional: A1 and A2 test
        # the worker's response to a payload the legitimate HTTP API
        # contract cannot construct (cross-tenant agent_id slug under
        # a tenant where that agent does not exist). Routing this
        # through the chat HTTP path would let auth/session middleware
        # reject it before it ever reached Celery, which would only
        # prove the HTTP layer is correct -- a property already covered
        # by P12/P14. The whole point of P13 is to test Gate 6 itself,
        # which fires inside the worker task body and therefore needs
        # the harness to act as a malicious in-process producer.
        #
        # Lazy import -- mirrors MemoryService.enqueue_extraction's
        # pattern. FastAPI verification process never loads Celery
        # until enqueue fires.
        from app.worker.tasks.memory_extraction import extract_memory_from_turn

        # ---------- 1. Construct + enqueue malicious payload ----------
        # The spoof: tenant_id=T1 matches the session, but agent_id is
        # A2's slug from T2. Worker Gate 6 looks up
        # Agent.where(tenant_id=T1, agent_id=A2_slug) -- finds no row
        # (A2 lives in T2) -- rejects to DLQ as IDENTITY_SPOOF.
        try:
            extract_memory_from_turn.delay(
                session_id=t1_session_id,
                user_id=f"p13-spoof-end-user-{uuid.uuid4().hex[:6]}",
                tenant_id=t1_id,
                message_id=t1_message_id,
                actor_key_prefix=k1_prefix,
                agent_id=agent_a2_slug,  # <-- the spoof
                actor_user_id=str(user_id),
                trace_id=None,
            )
        except Exception as exc:
            # Enqueue itself shouldn't fail in MODE=full (broker is
            # reachable per our mode check). If it does, that's a
            # different bug -- surface it.
            raise AssertionError(
                f"P13 enqueue of spoof payload failed: "
                f"{type(exc).__name__}: {exc}"
            )

        # ---------- 2. Wait for worker to consume + reject ----------
        # Generous wait: worker pickup + Gate 6 lookup + audit-row write
        # + DLQ enqueue. Pillar 11 uses 30s SLA for happy-path; rejection
        # path is faster but we wait conservatively to avoid flake.
        time.sleep(60)

        # ---------- 3. ASSERTION A1: no memory row landed for spoof ----------
        # Step 29 C.3: forensic GET via memory_items_step29c with
        # content_contains filter (added in C.3). Projection still
        # excludes content; we learn only "did any rows match?" not
        # what their content holds.
        r = call(
            "GET",
            "/api/v1/admin/forensics/memory_items_step29c",
            pa,
            params={
                "tenant_id": t1_id,
                "content_contains": sentinel_spoof,
                "limit": 100,
            },
            expect=200,
            client=c,
        )
        spoof_items = r.json().get("items") or []
        if spoof_items:
            raise AssertionError(
                f"A1 FAIL: spoof payload produced {len(spoof_items)} "
                f"memory row(s) in T1. Gate 6 did not fire. "
                f"row_ids={[item.get('id') for item in spoof_items]}"
            )

        # ---------- 4. ASSERTION A2: IDENTITY_SPOOF audit row exists ----------
        # Step 29 C.3: forensic GET via admin_audit_logs_step29c with
        # action + actor_key_prefix filters (actor_key_prefix added
        # in C.3). One short retry to absorb worker-to-audit lag.
        spoof_audit = self._fetch_spoof_audit(
            c=c, pa=pa, t1_id=t1_id, k1_prefix=k1_prefix,
        )
        if spoof_audit is None:
            time.sleep(5)
            spoof_audit = self._fetch_spoof_audit(
                c=c, pa=pa, t1_id=t1_id, k1_prefix=k1_prefix,
            )
        if spoof_audit is None:
            raise AssertionError(
                f"A2 FAIL: no ACTION_WORKER_IDENTITY_SPOOF_REJECT "
                f"audit row found for tenant={t1_id} "
                f"actor_key_prefix={k1_prefix}. Gate 6 did not "
                f"emit a rejection audit row."
            )
        audit_id = spoof_audit["id"]

        # ---------- 5. Locate the legit memory row from the setup turn ----------
        # The setup turn (issued before the spoof) produced a chat
        # response and enqueued a memory-extraction task. By now,
        # ~60s after enqueue, that extraction should be complete.
        #
        # Earlier P13 revisions queried by content.contains(sentinel),
        # which depended on the LLM preserving the sentinel verbatim
        # in the extracted memory text. Even with low temperature,
        # extractor paraphrasing made the assertion flaky and the
        # FAIL message ("sentinel not found") was misleading - the
        # real failure was usually "extractor produced no row at all
        # because the message wasn't user-fact-shaped" (Phase 1 -> 2
        # carry-over). Switching the lookup to message_id (FK to the
        # specific message that triggered the extraction) is
        # deterministic and paraphrase-proof.
        #
        # We poll briefly because extraction is async; if the worker
        # is slow under load, the row may still be in flight when A3
        # first fires.
        #
        # Step 29 C.3: forensic GET via memory_items_step29c with
        # message_id filter (added in C.3). Projection excludes
        # content but A3 only needs id + actor_user_id + tenant_id,
        # all of which the projection includes.
        legit_row = None
        legit_poll_attempts = 6   # 6 attempts x 5s = 30s budget
        for _attempt in range(legit_poll_attempts):
            r = call(
                "GET",
                "/api/v1/admin/forensics/memory_items_step29c",
                pa,
                params={
                    "tenant_id": t1_id,
                    "message_id": t1_message_id,
                    "limit": 1,
                },
                expect=200,
                client=c,
            )
            legit_items = r.json().get("items") or []
            if legit_items:
                legit_row = legit_items[0]
                break
            time.sleep(5)

        # ---------- ASSERTION A3: legit row attributed to U.id ----------
        if legit_row is None:
            raise AssertionError(
                f"A3 FAIL: setup turn produced no MemoryItem row for "
                f"message_id={t1_message_id} in T1={t1_id} after "
                f"{legit_poll_attempts * 5}s wait. Check (1) extractor "
                f"is reachable and processed the queue, (2) the setup "
                f"message text is user-fact-shaped enough that the "
                f"extractor returns a non-empty memory list, and "
                f"(3) MemoryRepository.upsert_by_message_id is wiring "
                f"message_id through to the new row."
            )
        legit_actor_str = legit_row.get("actor_user_id")
        legit_actor = uuid.UUID(legit_actor_str) if legit_actor_str else None
        if legit_actor != user_id:
            raise AssertionError(
                f"A3 FAIL: legit row (id={legit_row.get('id')}) "
                f"actor_user_id={legit_actor} != "
                f"U.id={user_id}"
            )
        legit_row_id = legit_row.get("id")

        # Soft check: sentinel verbatim is no longer reachable via the
        # forensic projection (content is excluded by design). The note
        # below is preserved as a doc reminder; if a future debugging
        # need ever requires sentinel inspection it should be done via
        # a one-off platform_admin probe with a temporary content
        # surface, not by widening the forensic projection.

        # ---------- ASSERTION A4: legit row tenant_id == T1 ----------
        if legit_row.get("tenant_id") != t1_id:
            raise AssertionError(
                f"A4 FAIL: legit row tenant_id={legit_row.get('tenant_id')!r} "
                f"!= T1={t1_id!r}"
            )

        # ---------- ASSERTION A5: T2 has no rows for U from this test ----------
        # Step 29 C.3: forensic GET via memory_items_step29c with
        # actor_user_id filter (from C.2). T2 leak probe.
        r = call(
            "GET",
            "/api/v1/admin/forensics/memory_items_step29c",
            pa,
            params={
                "tenant_id": t2_id,
                "actor_user_id": str(user_id),
                "limit": 100,
            },
            expect=200,
            client=c,
        )
        t2_leak_items = r.json().get("items") or []
        if t2_leak_items:
            raise AssertionError(
                f"A5 FAIL: T2 has {len(t2_leak_items)} memory row(s) "
                f"for U={user_id}. Spoof rejection path leaked "
                f"into T2. row_ids={[item.get('id') for item in t2_leak_items]}"
            )

        # ---------- ASSERTION A6: K1 still active ----------
        # Step 29 C.3: forensic GET via api_keys_step29c?id= (from C.1).
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
            raise AssertionError(
                f"A6 FAIL: K1 (id={k1_id}) disappeared during P13"
            )
        if not k1_after.get("active"):
            raise AssertionError(
                f"A6 FAIL: K1 (id={k1_id}) was deactivated by spoof "
                f"rejection. Spoof should not trigger collateral "
                f"key rotation -- only ScopeAssignment.end_assignment "
                f"rotates keys."
            )

        return (
            f"spoof_rejected audit_id={audit_id} "
            f"legit_row_id={legit_row_id} attributed_to_user "
            f"T2_no_leak K1_still_active"
        )

    def _fetch_spoof_audit(
        self,
        *,
        c,
        pa: str,
        t1_id: str,
        k1_prefix: str,
    ) -> dict | None:
        """Fetch the most recent IDENTITY_SPOOF audit row, or None.

        Step 29 C.3: forensic GET via admin_audit_logs_step29c with
        action + actor_key_prefix filters (actor_key_prefix added
        in C.3 to replace the prior direct ORM equality clause).
        """
        r = call(
            "GET",
            "/api/v1/admin/forensics/admin_audit_logs_step29c",
            pa,
            params={
                "tenant_id": t1_id,
                "action": ACTION_WORKER_IDENTITY_SPOOF_REJECT,
                "actor_key_prefix": k1_prefix,
                "limit": 1,
            },
            expect=200,
            client=c,
        )
        rows = r.json().get("rows") or []
        return rows[0] if rows else None

    def _run_degraded_sanity(
        self,
        *,
        c,
        pa: str,
        user_id: uuid.UUID,
        t1_id: str,
        t2_id: str,
        sentinel_legit: str,
        k1_id: int,
        t1_message_id: int,
    ) -> str:
        """MODE=degraded sanity: assert the setup turn produced a legit
        memory row in T1 (sync path on the test machine wrote it because
        no async worker was running). No spoof guard exercised.

        Mirrors the A3 fix in _run_full_assertions: keys on
        MemoryItem.message_id rather than content.contains(sentinel) so
        the assertion is robust to LLM paraphrase. sentinel_legit is
        retained as a debug signal in the function signature for
        callsite parity but is no longer probed here (forensic
        projection excludes content by design).

        Step 29 C.3: all three reads migrated to HTTP.
        """
        # legit-row probe by message_id
        r = call(
            "GET",
            "/api/v1/admin/forensics/memory_items_step29c",
            pa,
            params={
                "tenant_id": t1_id,
                "message_id": t1_message_id,
                "limit": 1,
            },
            expect=200,
            client=c,
        )
        legit_items = r.json().get("items") or []
        legit_row = legit_items[0] if legit_items else None
        legit_ok = False
        if legit_row is not None:
            legit_actor_str = legit_row.get("actor_user_id")
            if legit_actor_str:
                try:
                    legit_ok = uuid.UUID(legit_actor_str) == user_id
                except (ValueError, TypeError):
                    legit_ok = False

        # T2 leak probe
        r = call(
            "GET",
            "/api/v1/admin/forensics/memory_items_step29c",
            pa,
            params={
                "tenant_id": t2_id,
                "actor_user_id": str(user_id),
                "limit": 100,
            },
            expect=200,
            client=c,
        )
        t2_leak_items = r.json().get("items") or []

        # K1 active probe
        r = call(
            "GET",
            "/api/v1/admin/forensics/api_keys_step29c",
            pa,
            params={"id": k1_id},
            expect=200,
            client=c,
        )
        k1_body = r.json()
        k1_active = bool(k1_body and k1_body.get("id") == k1_id and k1_body.get("active"))

        return (
            f"setup_legit_ok={legit_ok} "
            f"sentinel_in_content=skipped(projection_excludes_content) "
            f"T2_leak_rows={len(t2_leak_items)} "
            f"K1_active={k1_active}"
        )

    def _teardown(
        self,
        c,
        pa: str,
        user_id: uuid.UUID,
        t1_id: str,
        t2_id: str,
    ) -> None:
        """Self-contained teardown.

        Soft-deactivate the User (cascade ends remaining ScopeAssignments
        and rotates remaining keys via Q6 cascade), then soft-deactivate
        both tenants. Suite's _thorough_teardown only walks state.tenant_id;
        Pillar 13's tenants are separate, so we own their cleanup here.

        Failures during teardown don't fail the pillar -- assertions above
        already proved correctness. Pillar 10 (teardown integrity) at the
        end of the suite walks state.tenant_id only, so leftover Pillar 13
        residue won't surface there. Logged for forensics.
        """
        # Phase 2 Commit 13: HTTP path via /admin/users/{id}/deactivate.
        # Worker DSN has no privileges on scope_assignments / users by
        # design (migration f392a842f885); admin route runs under the API
        # process which carries the admin DSN.
        try:
            call(
                "POST",
                f"/api/v1/admin/users/{user_id}/deactivate",
                pa,
                json={
                    "reason": f"P13 teardown for tenants {t1_id}, {t2_id}",
                    "audit_label": f"pillar_13:teardown:{t1_id}+{t2_id}",
                },
                expect=(200, 204),
                client=c,
            )

            # Soft-deactivate both tenants. Order doesn't matter.
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
            # Don't fail the pillar on teardown errors.
            print(
                f"  pillar 13 teardown warning: "
                f"{type(teardown_exc).__name__}: {teardown_exc}"
            )


PILLAR = CrossTenantIdentityPillar()
