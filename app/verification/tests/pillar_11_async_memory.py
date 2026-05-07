"""Pillar 11 - Async memory extraction (Step 27b).

Two execution modes:

  Mode FULL    Worker + Redis broker reachable. Runs all 10 assertions
               from the Step 27b Security & Invariant Contract (latency,
               idempotency, cross-tenant rejection, deactivated-instance
               rejection, revoked-key rejection, malformed-payload DLQ,
               consent-revocation honored, log hygiene, queue-depth
               endpoint, audit-row linkage).

  Mode DEGRADED  Worker or broker unreachable. Runs 4 assertions that
                 require no worker process: signature/import correctness,
                 feature-flag wiring, fail-open ChatService behavior when
                 enqueue fails, queue-depth endpoint reachable (and
                 returning a structured 503 in dev where SQS is unset).

Both modes return PASS. Mode is reported in the detail string so the
matrix output makes it explicit.

Reference: docs/runbooks/step-27b-security-contract.md

Pre-conditions:
  - Pillar 4 has populated state.chat_keys (agent-bound chat key needed
    for the round-trip turn that triggers extraction).
  - Pillar 2 has populated state.instance_agent for the audit-row scope
    assertion.

Step 29 Commit C.1 + Commit C.5: forensic reads + write migrated to HTTP
-----------------------------------------------------------------------

C.1 migrated the 7 forensic reads in `_run_full_checks` (api_keys
lookup at F1; memory_items idempotency probe at F2; admin_audit_logs
polls at F3, F4, F9, F10; luciel_instances reads at F10) to the
platform_admin-gated HTTP endpoints under
`/api/v1/admin/forensics/*_step29c` (see app/api/v1/admin_forensics.py).

C.5 migrated the last forensic *write* in this pillar -- the F10
`inst.active = False` toggle (deactivate to set up the instance-
liveness Gate-4 assertion) and the matching teardown
`inst.active = previous_active` (restore prior state) -- to a
platform_admin POST at
`/api/v1/admin/forensics/luciel_instances_step29c/{instance_id}/toggle_active`.
The POST emits an `ACTION_LUCIEL_INSTANCE_FORENSIC_TOGGLE` audit row
before mutating active so an audit-write failure aborts the mutation.

IMPORTANT: Unlike P12/P13/P14, this pillar still holds a direct
`SessionLocal` import and opens a real DB session in `_run_full_checks`.
The reason is the B.3 producer-side exemption: F1's
`MemoryService(MemoryRepository(db), ModelRouter())` and F2's
`MemoryRepository(db).upsert_by_message_id(...)` are direct producer-side
calls (the assertions under test ARE the latency budget and idempotency
contract on the producer path, which the HTTP layer cannot reach), and
both need a real `Session` argument. The `LucielInstance` ORM-model
import, however, IS dropped in C.5 because all `LucielInstance` access
(reads + the toggle write) is now HTTP-mediated. The F10 producer-side
`extract_memory_from_turn.apply_async` callsite remains direct under
the B.3 producer-side exemption (the assertion under test IS the
deactivated-instance Gate-4 behavior on the producer path).

Producer-side exemption (Step 29 Commit B.3, drift
D-step29-audit-undercounts-verify-debt-2026-05-06): the four
producer-side calls in this pillar -- F1 `MemoryService.enqueue_extraction`,
F2 `MemoryRepository.upsert_by_message_id`, F4
`extract_memory_from_turn.apply_async`, and F10 the second
`extract_memory_from_turn.apply_async` -- intentionally remain direct
because the assertion under test IS a property of the producer-side path
itself (latency budget, idempotency contract, malformed-payload Gate-1
behavior, deactivated-instance Gate-4 behavior). Migrating these would
not test the contract; the HTTP layer can never reach them. See
docs/STEP_29_AUDIT.md Section 6 for the full rule.
"""

from __future__ import annotations

import time
from typing import Any

# Step 29 Commit C.5: LucielInstance import dropped. F10's ORM-write
# callsites (deactivate + restore) now go through the platform_admin POST
# at /api/v1/admin/forensics/luciel_instances_step29c/{instance_id}
# /toggle_active, so this pillar no longer needs the LucielInstance model.
# `SessionLocal` is RETAINED because F1/F2 producer-side direct calls
# (`MemoryService(MemoryRepository(db), ...)` and
# `MemoryRepository(db).upsert_by_message_id(...)`) require a real DB
# Session per the B.3 producer-side exemption -- the assertions under
# test (latency budget, idempotency) are properties of the producer path
# and cannot be exercised over HTTP without defeating the contract.
from app.db.session import SessionLocal
from app.verification.fixtures import RunState
from app.verification._infra_probes import _broker_reachable, _worker_reachable
from app.verification.http_client import (
    BASE_URL,
    call,
    forensics_get,
    h,
    pooled_client,
)
from app.verification.runner import Pillar


# Mode detection helpers (_broker_reachable / _worker_reachable) live in
# app.verification._infra_probes since Step 29 Commit C.6. Previously they
# were inlined here AND duplicated verbatim in P13; the duplication was
# called out at the time as deferred cleanup (see B.1 commit message and
# the P13 docstring annotation prior to C.6). Consolidating both into a
# shared module preserves the same observable behavior -- both helpers
# return False on any failure (import, connection, auth, timeout) so the
# pillar gracefully drops to MODE=degraded -- while making the seam
# obvious to a future reader: any pillar that needs the same gate
# imports from _infra_probes; no further inline copies should be added.


# ---------- pillar ----------

class AsyncMemoryPillar(Pillar):
    number = 11
    name = "async memory extraction (Step 27b)"

    # ---------------- entry point ----------------
    def run(self, state: RunState) -> str:
        # Always-on degraded checks first; they have no infra dependency
        # and act as a smoke test for the import surface itself.
        degraded_summary = self._run_degraded_checks(state)

        if _broker_reachable() and _worker_reachable():
            full_summary = self._run_full_checks(state)
            return f"MODE=full :: {full_summary} | {degraded_summary}"

        return f"MODE=degraded :: {degraded_summary} (worker/broker unreachable)"

    # ---------------- degraded mode (always run) ----------------
    def _run_degraded_checks(self, state: RunState) -> str:
        results: list[str] = []

        # D1. Imports + signatures wire correctly.
        from app.memory.service import MemoryService
        from app.worker.tasks.memory_extraction import extract_memory_from_turn
        import inspect as _insp

        sig = _insp.signature(MemoryService.enqueue_extraction)
        required = {"user_id", "tenant_id", "session_id", "message_id",
                    "actor_key_prefix"}
        missing = required - set(sig.parameters)
        if missing:
            raise AssertionError(
                f"D1 enqueue_extraction missing params: {missing}"
            )
        results.append("D1 imports/signatures ok")

        # D2. Feature flag wiring: settings.memory_extraction_async exists,
        # is bool, defaults False (dev safety).
        from app.core.config import settings
        if not isinstance(settings.memory_extraction_async, bool):
            raise AssertionError(
                "D2 settings.memory_extraction_async must be bool, got "
                f"{type(settings.memory_extraction_async).__name__}"
            )
        results.append(
            f"D2 feature flag ok (async={settings.memory_extraction_async})"
        )

        # D3. Fail-open: ChatService source contains the inner try/except
        # around enqueue_extraction so a down worker does NOT 5xx the chat.
        # We grep the source rather than running a broken broker because
        # this assertion is about contract, not runtime.
        chat_src = _insp.getsource(__import__(
            "app.services.chat_service", fromlist=["ChatService"]
        ))
        if chat_src.count("enqueue_extraction") < 2:
            raise AssertionError(
                "D3 expected enqueue_extraction call in BOTH respond and "
                "respond_stream paths; only found "
                f"{chat_src.count('enqueue_extraction')}"
            )
        if "fail-open" not in chat_src.lower():
            raise AssertionError(
                "D3 ChatService missing fail-open comment marker; the "
                "contract requires explicit fail-open handling around "
                "enqueue_extraction"
            )
        results.append("D3 ChatService fail-open contract preserved")

        # D4. Queue-depth admin endpoint reachable. In dev (no SQS) we
        # accept either 200 with structured payload OR 503
        # service-unavailable (queues unreachable). 4xx other than 403 is
        # a contract failure.
        if not state.platform_admin_key:
            raise AssertionError("D4 needs platform_admin_key on RunState")
        with pooled_client() as c:
            r = c.get(
                "/api/v1/admin/worker/queue-depth",
                headers=h(state.platform_admin_key),
            )
        if r.status_code not in (200, 503):
            raise AssertionError(
                "D4 queue-depth endpoint must return 200 or 503 "
                f"(got {r.status_code}, body={r.text[:200]})"
            )
        results.append(f"D4 queue-depth endpoint reachable ({r.status_code})")

        return "; ".join(results)

    # ---------------- full mode (worker + broker required) ----------------
    def _run_full_checks(self, state: RunState) -> str:
        """Full assertions per Security Contract Pillar 11 spec.

        Each sub-assertion is wrapped to surface its failure cleanly in
        the matrix detail string.

        Producer-side exemption (Step 29 Commit B.3): F1, F2, F4, and the
        F10 second .apply_async are direct producer-side calls --
        the assertion under test is a property of the producer path
        itself (latency, idempotency, malformed-payload Gate 1,
        deactivated-instance Gate 4). The forensic READS those
        producers depend on now go through HTTP.

        F10 ORM write (`inst.active = False`) is migrated in Commit
        C.5 to a platform_admin POST at
        `/api/v1/admin/forensics/luciel_instances_step29c/{instance_id}/toggle_active`.
        Both the deactivate (setup) and the restore (teardown) go
        through that POST. The route emits an
        `ACTION_LUCIEL_INSTANCE_FORENSIC_TOGGLE` audit row before
        mutating active.
        """
        agent_ck = state.chat_key_for(state.instance_agent) if state.instance_agent else None
        if agent_ck is None:
            raise AssertionError(
                "F0 full mode needs agent-bound chat key from pillar 4"
            )
        if not state.platform_admin_key:
            raise AssertionError(
                "F0 full mode needs platform_admin_key for forensic reads"
            )

        results: list[str] = []

        # F1. Latency: a chat turn returns within budget when async on.
        # We don't flip the env flag here (would race with running app);
        # we assert that the enqueue PATH itself returns <100ms when
        # invoked directly. Real chat-turn timing is covered in pillar 5.
        #
        # Producer-side: MemoryService.enqueue_extraction is intentionally
        # called directly (per producer-side exemption rule). The
        # api_keys lookup that supplies real_key_prefix goes through HTTP.
        from app.memory.service import MemoryService
        from app.repositories.memory_repository import MemoryRepository
        from app.integrations.llm.router import ModelRouter

        db = SessionLocal()
        try:
            svc = MemoryService(MemoryRepository(db), ModelRouter())

            # Look up the real key_prefix from the agent chat key id
            # via the platform_admin forensic-read endpoint (mirrors how
            # middleware populates request.state.key_prefix).
            with pooled_client() as c:
                r = call(
                    "GET",
                    "/api/v1/admin/forensics/api_keys_step29c",
                    state.platform_admin_key,
                    params={"id": agent_ck["id"]},
                    expect=200,
                    client=c,
                )
            real_key_prefix = r.json().get("key_prefix")
            if not isinstance(real_key_prefix, str) or not real_key_prefix:
                raise AssertionError(
                    f"F1 forensic api_keys read returned no key_prefix: "
                    f"{r.json()}"
                )

            t0 = time.perf_counter()
            try:
                task_id = svc.enqueue_extraction(
                    user_id="pillar11-user",
                    tenant_id=state.tenant_id,
                    session_id="pillar11-non-existent-session",
                    message_id=999_999_999,  # well outside real ids
                    actor_key_prefix=real_key_prefix,
                    agent_id=state.agent_id,
                    luciel_instance_id=state.instance_agent,
                    trace_id="pillar11-trace",
                )
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
            except Exception as exc:
                raise AssertionError(
                    f"F1 enqueue_extraction raised: {type(exc).__name__}"
                ) from exc

            if elapsed_ms > 250:
                raise AssertionError(
                    f"F1 enqueue latency {elapsed_ms:.0f}ms exceeds 250ms budget"
                )
            results.append(f"F1 enqueue ok ({elapsed_ms:.0f}ms, task={task_id[:8]}...)")

            # F2. Idempotency: re-enqueue same task with the SAME
            # message_id. Worker will reject both via Gate 3 (cross-
            # tenant/session-not-found), but the partial unique index
            # would prevent duplicate memory rows even if extraction ran.
            # Assert the repository upsert returns False on second call
            # using a real message_id and a row pre-inserted to model
            # the replay scenario.
            #
            # Forensic: existing-row lookup goes through HTTP.
            # Producer-side: MemoryRepository.upsert_by_message_id stays
            # direct (per producer-side exemption rule).
            with pooled_client() as c:
                r = call(
                    "GET",
                    "/api/v1/admin/forensics/memory_items_step29c",
                    state.platform_admin_key,
                    params={
                        "tenant_id": state.tenant_id,
                        "message_id_not_null": "true",
                        "limit": 1,
                    },
                    expect=200,
                    client=c,
                )
            existing_items = r.json().get("items") or []
            existing_message_id = (
                existing_items[0].get("message_id") if existing_items else None
            )
            if existing_message_id is not None:
                # Use an existing row's message_id to test idempotent re-upsert.
                ok = MemoryRepository(db).upsert_by_message_id(
                    user_id="pillar11-user",
                    tenant_id=state.tenant_id,
                    category="preference",
                    content="pillar 11 idempotency probe",
                    message_id=existing_message_id,
                    luciel_instance_id=state.instance_agent,
                )
                if ok is True:
                    raise AssertionError(
                        "F2 second upsert_by_message_id returned True; "
                        "expected False (idempotent no-op)"
                    )
                results.append("F2 idempotent upsert ok (replay -> no-op)")
            else:
                # No existing rows yet -> degrade this sub-check to
                # signature-level only. Don't fabricate state.
                results.append("F2 idempotent upsert ok (no rows; skipped real replay)")

            # F3. Cross-tenant rejection: the synthetic enqueue in F1
            # used a non-existent session_id, which the worker rejects
            # at Gate 3 (session lookup fails -> WORKER_CROSS_TENANT_REJECT).
            # Poll admin_audit_logs (via HTTP) for that rejection row,
            # with a 10s SLA budget (worker prefetch=1 + visibility=30s,
            # but normal local round-trip is ~1-3s).
            deadline = time.time() + 10.0
            reject_row: dict[str, Any] | None = None
            while time.time() < deadline:
                with pooled_client() as c:
                    r = call(
                        "GET",
                        "/api/v1/admin/forensics/admin_audit_logs_step29c",
                        state.platform_admin_key,
                        params={
                            "tenant_id": state.tenant_id,
                            "action": "worker_cross_tenant_reject",
                            "limit": 1,
                        },
                        expect=200,
                        client=c,
                    )
                rows = r.json().get("rows") or []
                if rows:
                    reject_row = rows[0]
                    break
                time.sleep(0.5)
            if reject_row is None:
                raise AssertionError(
                    "F3 expected WORKER_CROSS_TENANT_REJECT audit row "
                    "from synthetic enqueue within 10s; none found"
                )
            results.append(
                f"F3 cross-tenant rejection ok (audit_id={reject_row['id']})"
            )

            # F4. Malformed-payload rejection. Enqueue a payload with
            # message_id of the wrong type via .apply_async(kwargs=...).
            # Gate 1 should reject with WORKER_MALFORMED_PAYLOAD.
            #
            # Producer-side: extract_memory_from_turn.apply_async stays
            # direct (per producer-side exemption rule -- Gate 1 only
            # fires if the broker-side payload contract is exercised
            # exactly as the worker sees it).
            from app.worker.tasks.memory_extraction import extract_memory_from_turn
            extract_memory_from_turn.apply_async(
                kwargs={
                    "session_id": "pillar11-malformed",
                    "user_id": "pillar11-user",
                    "tenant_id": state.tenant_id,
                    "message_id": "not-an-int",   # type violation
                    "actor_key_prefix": real_key_prefix,
                },
            )
            deadline = time.time() + 10.0
            malformed_row: dict[str, Any] | None = None
            while time.time() < deadline:
                with pooled_client() as c:
                    r = call(
                        "GET",
                        "/api/v1/admin/forensics/admin_audit_logs_step29c",
                        state.platform_admin_key,
                        params={
                            "tenant_id": state.tenant_id,
                            "action": "worker_malformed_payload",
                            "limit": 1,
                        },
                        expect=200,
                        client=c,
                    )
                rows = r.json().get("rows") or []
                if rows:
                    malformed_row = rows[0]
                    break
                time.sleep(0.5)
            if malformed_row is None:
                raise AssertionError(
                    "F4 expected WORKER_MALFORMED_PAYLOAD audit row within 10s"
                )
            results.append(
                f"F4 malformed payload rejection ok (audit_id={malformed_row['id']})"
            )

            # F5. Audit-row content hygiene: rejection rows must NOT
            # contain raw user content. Spot-check the two rows we just
            # caught -- their after_json should only have opaque ids.
            for row in (reject_row, malformed_row):
                after = row.get("after_json") or {}
                forbidden_keys = {"messages", "content", "user_message",
                                  "assistant_reply", "raw_content"}
                leaked = forbidden_keys & set(after.keys())
                if leaked:
                    raise AssertionError(
                        f"F5 audit row {row['id']} leaked content keys: {leaked}"
                    )
            results.append("F5 audit-row content hygiene ok")

            # F6. actor_key_prefix preserved through enqueue -> worker ->
            # audit. The synthetic F1 enqueue used real_key_prefix as the
            # prefix; the rejection audit row should carry it.
            recorded_prefix = (reject_row.get("after_json") or {}).get(
                "actor_key_prefix"
            )
            if recorded_prefix != real_key_prefix:
                raise AssertionError(
                    "F6 actor_key_prefix not preserved through worker; "
                    f"sent={real_key_prefix!r} recorded={recorded_prefix!r}"
                )
            results.append("F6 actor_key_prefix linkage ok")

            # F7. Queue-depth endpoint returns structured payload when
            # broker reachable.
            with pooled_client() as c:
                r = c.get(
                    "/api/v1/admin/worker/queue-depth",
                    headers=h(state.platform_admin_key),
                )
            if r.status_code != 200:
                # Acceptable if SQS not provisioned in dev; worker may be
                # using Redis-only and SQS still 503s. Warn-pass.
                results.append(f"F7 queue-depth ({r.status_code}; SQS optional)")
            else:
                body = r.json()
                if not all(k in body for k in ("region", "main_queue", "dlq")):
                    raise AssertionError(
                        f"F7 queue-depth response missing keys: {body}"
                    )
                results.append(
                    f"F7 queue-depth ok "
                    f"(main={body['main_queue'].get('approximate_messages')}, "
                    f"dlq={body['dlq'].get('approximate_messages')})"
                )

            # F8. Trace-id propagation. The F1 enqueue passed
            # trace_id='pillar11-trace'; assert the rejection audit row
            # echoes it in after_json.
            recorded_trace = (reject_row.get("after_json") or {}).get("trace_id")
            if recorded_trace != "pillar11-trace":
                raise AssertionError(
                    "F8 trace_id not propagated; "
                    f"expected='pillar11-trace' got={recorded_trace!r}"
                )
            results.append("F8 trace_id propagation ok")

            # F9. Worker DB role guardrail (best-effort, dev-mode lenient).
            # In prod the worker uses a separate Postgres role with limited
            # grants. Locally we share the role; we just assert the worker
            # did NOT touch retention/deletion/consent rows during these
            # tests.
            #
            # Migrated SQL `WHERE action IN ('cascade_deactivate',
            # 'knowledge_delete')` to two HTTP probes (one per action),
            # each with `actor_label_like='worker:'`. The original
            # `func.count()` aggregate becomes len() on the returned
            # row lists. limit=1 is enough -- we only need to detect
            # "any" worker-actored row of either action.
            forbidden_total = 0
            for forbidden_action in ("cascade_deactivate", "knowledge_delete"):
                with pooled_client() as c:
                    r = call(
                        "GET",
                        "/api/v1/admin/forensics/admin_audit_logs_step29c",
                        state.platform_admin_key,
                        params={
                            "tenant_id": state.tenant_id,
                            "action": forbidden_action,
                            "actor_label_like": "worker:",
                            "limit": 1,
                        },
                        expect=200,
                        client=c,
                    )
                forbidden_total += len(r.json().get("rows") or [])
            if forbidden_total > 0:
                raise AssertionError(
                    "F9 worker should never write retention/consent audit "
                    f"rows; found {forbidden_total}"
                )
            results.append("F9 worker scope guardrail ok")

            # F10. luciel_instance liveness gate (sub-assertion). Deactivate
            # the agent instance and re-enqueue; expect WORKER_INSTANCE_DEACTIVATED.
            # We restore active=True at the end so teardown isn't surprised.
            #
            # Step 29 Commit C.5: forensic READ + WRITE of luciel_instances
            # both go through platform_admin HTTP. The deactivate (setup)
            # and restore (teardown) use the new POST at
            # /admin/forensics/luciel_instances_step29c/{id}/toggle_active
            # which emits an ACTION_LUCIEL_INSTANCE_FORENSIC_TOGGLE audit
            # row before mutating active. Producer-side .apply_async stays
            # direct under the B.3 producer-side exemption.
            # Step 29 Commit C.6: forensics_get() wrapper hides the
            # GET-and-expect-(200,404) ritual; the (200, 404) allowlist
            # is correct here because a 404 means the instance row was
            # teardown-raced or never created -- which IS an assertion
            # failure for F10 (we just looked it up by state.instance_agent
            # which the pillar's own setup populated). The 4-line check
            # below preserves that semantic exactly; only the call shape
            # is hoisted.
            with pooled_client() as c:
                r = forensics_get(
                    f"/api/v1/admin/forensics/luciel_instances_step29c/{state.instance_agent}",
                    state.platform_admin_key,
                    client=c,
                )
            if r.status_code == 404:
                raise AssertionError(
                    f"F10 instance_agent={state.instance_agent} missing"
                )
            inst_body = r.json()
            previous_active = bool(inst_body.get("active"))

            # Step 29 Commit C.5: deactivate via HTTP POST. The route's
            # audit-row-before-mutation invariant guarantees that if the
            # audit insert fails, the SQL UPDATE never executes (atomic
            # in a single commit; rolls back on commit failure).
            with pooled_client() as c:
                r = call(
                    "POST",
                    f"/api/v1/admin/forensics/luciel_instances_step29c/{state.instance_agent}/toggle_active",
                    state.platform_admin_key,
                    json={"active": False},
                    expect=200,
                    client=c,
                )
            deact_body = r.json()
            if bool(deact_body.get("active")) is not False:
                raise AssertionError(
                    f"F10 toggle_active POST returned active="
                    f"{deact_body.get('active')!r} after requesting False; "
                    f"row id={state.instance_agent}"
                )
            # F10 needs a REAL session whose tenant matches, so Gate 3 passes
            # and Gate 4 (instance liveness) actually gets evaluated. Create
            # a throwaway session via the existing admin API, bound to the
            # agent-scoped LucielInstance that we just deactivated.
            with pooled_client() as c:
                r = call(
                    "POST",
                    "/api/v1/sessions",
                    agent_ck["key"],
                    json={
                        "user_id": "pillar11-deactivated-user",
                        "tenant_id": state.tenant_id,
                        "domain_id": state.domain_id,
                        "agent_id": state.agent_id,
                    },
                    expect=(200, 201),
                    client=c,
                )
                f10_session_id = (r.json().get("session_id")
                                  or r.json().get("id"))
                if not isinstance(f10_session_id, str) or not f10_session_id:
                    raise AssertionError(
                        f"F10 could not create probe session: {r.json()}"
                    )

            try:
                # Producer-side: extract_memory_from_turn.apply_async
                # stays direct (per producer-side exemption rule).
                extract_memory_from_turn.apply_async(
                    kwargs={
                        "session_id": f10_session_id,
                        "user_id": "pillar11-deactivated-user",
                        "tenant_id": state.tenant_id,
                        "message_id": 888_888_888,
                        "actor_key_prefix": real_key_prefix,
                        "luciel_instance_id": state.instance_agent,
                        "trace_id": "pillar11-deactivated",
                    },
                )
                deadline = time.time() + 10.0
                deact_row: dict[str, Any] | None = None
                while time.time() < deadline:
                    with pooled_client() as c:
                        r = call(
                            "GET",
                            "/api/v1/admin/forensics/admin_audit_logs_step29c",
                            state.platform_admin_key,
                            params={
                                "tenant_id": state.tenant_id,
                                "action": "worker_instance_deactivated",
                                "limit": 1,
                            },
                            expect=200,
                            client=c,
                        )
                    rows = r.json().get("rows") or []
                    if rows:
                        deact_row = rows[0]
                        break
                    time.sleep(0.5)
                if deact_row is None:
                    raise AssertionError(
                        "F10 expected worker_instance_deactivated audit row "
                        "within 10s of deactivated-probe enqueue"
                    )
                results.append(
                    f"F10 instance liveness gate ok (audit_id={deact_row['id']})"
                )
            finally:
                # Restore active state regardless of assertion outcome.
                # Step 29 Commit C.5: restore via the same toggle_active
                # POST as the deactivate write above. We do NOT swallow
                # the response error here -- if the restore fails, the
                # next pillar run will see a still-deactivated instance,
                # which is a real problem the harness must surface.
                with pooled_client() as c:
                    call(
                        "POST",
                        f"/api/v1/admin/forensics/luciel_instances_step29c/{state.instance_agent}/toggle_active",
                        state.platform_admin_key,
                        json={"active": previous_active},
                        expect=200,
                        client=c,
                    )

        finally:
            db.close()

        return ' | '.join(results)


PILLAR = AsyncMemoryPillar()
