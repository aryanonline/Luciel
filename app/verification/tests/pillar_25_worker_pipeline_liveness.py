"""Pillar 25 - Worker pipeline liveness via backend-side probe (Step 29.y).

# Why this pillar exists

Step 29.x diag15 found three things in production simultaneously:

  1. Zero `memory_extracted` audit rows EVER -- the canonical signal
     that the Celery worker has successfully processed a chat turn.
  2. Zero `worker_*` audit rows in the last 7 days -- so no rejection
     paths have fired either.
  3. Zero non-verify-tenant messages in the last 7 days -- consistent
     with "no real customer traffic yet" rather than "worker silently
     dead." But (3) on its own does not distinguish between "no
     customers" and "worker is broken and we will only find out when
     the first customer chats."

Before REMAX Tier-3 onboarding (Step 30b) we MUST be able to assert
on every verify run that the worker pipeline is alive and emitting
audit rows. Pillar 11 already enqueues malformed payloads in F4 and
asserts WORKER_MALFORMED_PAYLOAD lands -- but only when MODE=full.
In production the verify ECS task does NOT have broker network
access (broker probe checks REDIS_URL which prod does not set; prod
uses SQS), so P11 falls into MODE=degraded and F4 is skipped. That
is exactly why diag15 found zero observed worker traffic in 7 days
of verify runs: the only thing exercising the broker plane was P11
F4, and F4 has been dark since the SQS migration.

# What Pillar 25 does

Pillar 25 calls a new POST route on the backend container:

    POST /api/v1/admin/forensics/worker_pipeline_probe_step29y

The backend container DOES have broker network access (it is the
Celery producer for every chat turn), so the probe enqueues a
deliberately-malformed task and polls admin_audit_logs for the
worker-emitted WORKER_MALFORMED_PAYLOAD row. On success it returns
200 with `audit_id` and `elapsed_ms`. On 30s timeout it returns
504 with a structured detail explaining what we polled for.

This is "verify task asks the backend, over HTTP, to do a
producer-side enqueue + audit-row poll, and asserts on the result."
Verify task remains pure-HTTP (no broker dependency). The
producer-side path is exercised on the same host that production
chat traffic runs through.

# Three assertions

  G1. ROUTE LIVENESS. The probe route MUST return 200. A 404 means
      the route was not deployed; a 401/403 means the platform_admin
      gate is misconfigured; a 500 means the backend itself is
      unhealthy. Any of these is a critical regression.

  G2. WORKER LIVENESS (THE LOAD-BEARING ASSERTION). The 200 response
      is ITSELF proof that within 30s the worker process ran, Gate 1
      fired on the malformed payload, AdminAuditRepository.record()
      committed a row to admin_audit_logs, and the row became visible
      to the API process. If any link in that chain is broken we get
      504, not 200.

  G3. POLLED ACTION SHAPE. The response's `polled_for_action` must
      be exactly `worker_malformed_payload` (the Gate-1 rejection
      action constant). Asserts the route did not silently default
      to a different polled action -- e.g. a future refactor that
      flipped the default mode to `full` would surface here because
      the polled action would change to `memory_extracted`.

# Mode selection: ALWAYS malformed (default)

Pillar 25 calls the route WITHOUT `?mode=full`. The malformed-payload
mode does NOT consume LLM credits, does NOT create a real MemoryItem,
and is fast (typically < 2s end-to-end). The full-extraction mode
exists on the same route behind `?mode=full` for manual pre-REMAX-
onboarding probes; calling it from CI would burn credits on every
verify run and is explicitly out of scope for this pillar.

# Producer-side exemption -- not needed here

P11 F3/F4/F10 hold a producer-side exemption (B.3 / 4120f8d) because
their assertions are properties of the producer-side path itself
(cross-tenant rejection latency, malformed-payload Gate-1 latency,
instance-deactivation reaction time) and cannot be exercised over
HTTP without defeating the contract. Pillar 25 does NOT take that
exemption -- it asserts a property of the WORKER (does it emit an
audit row when a malformed payload is enqueued?) by asking the
backend to do the producer-side work. The verify task itself is
pure-HTTP. This is a strictly stronger architectural posture than
P11 F4 holds, and it is why P25 is added rather than P11 F4 being
moved to MODE=full unconditionally.

# Scope and ordering

Reads:
  - state.tenant_id           (set by P1)
  - state.platform_admin_key  (env-loaded)
  - state.chat_keys           (set by P4 -- we need a real chat key
                               prefix for the actor_key_prefix field
                               so the worker's audit row carries a
                               valid attribution handle)

Writes (via the backend probe route):
  - admin_audit_logs row with action=worker_malformed_payload
    (worker-emitted; cleaned up at tenant teardown by P10).

Idempotency:
  Each probe call enqueues a brand-new task with a fresh session_id
  (uuid4) so re-running P25 never collides with a previous run.

# Why a direct row-id check is not needed

The route already does the high-water-mark check internally (snapshot
MAX(id) before enqueue, only count rows with id > snapshot). Pillar 25
trusts that contract -- duplicating it in the pillar would be brittle
because the pillar would need direct DB access (against the design
of "verify task is pure-HTTP for assertions").
"""

from __future__ import annotations

from typing import Any

from app.verification.fixtures import RunState
from app.verification.http_client import (
    BASE_URL,
    call,
    pooled_client,
)
from app.verification.runner import Pillar


_PROBE_PATH = "/api/v1/admin/forensics/worker_pipeline_probe_step29y"
_EXPECTED_ACTION = "worker_malformed_payload"

# Wall-clock budget for the entire HTTP round-trip (route's own deadline
# is 30s; we allow a small grace window for ALB latency + the route's
# own poll cadence). If the route returns 504 it does so inside its
# 30s deadline, so a hard ~40s here is a safety net rather than the
# primary timeout.
_HTTP_TIMEOUT_SECONDS = 40.0


class WorkerPipelineLivenessPillar(Pillar):
    number = 25
    name = "Worker pipeline liveness via backend-side probe (Step 29.y)"

    def run(self, state: RunState) -> str:
        if not state.platform_admin_key:
            raise AssertionError(
                "Pillar 25 requires state.platform_admin_key (env-loaded). "
                "The probe route is platform_admin gated."
            )
        if not state.tenant_id:
            raise AssertionError(
                "Pillar 25 requires state.tenant_id (set by P1)."
            )

        # The backend probe route requires actor_key_prefix as a 12-char
        # string so the worker-emitted audit row carries a valid
        # attribution handle. We use the agent-bound chat key created by
        # P4 -- it is a real ApiKey row owned by the verify tenant, so
        # the worker's actor-key-prefix lookup will resolve.
        agent_ck = (
            state.chat_key_for(state.instance_agent)
            if state.instance_agent
            else None
        )
        if agent_ck is None:
            raise AssertionError(
                "Pillar 25 requires a chat key for state.instance_agent "
                "(set by P2/P4); none found. Probe needs a real "
                "actor_key_prefix so the worker can attribute the audit row."
            )
        # state.chat_keys is a list of dicts with shape
        # {"key": raw, "instance_id": int, "id": int, "scope_level": ...}
        # (see fixtures.py RunState). The 12-char public prefix is the
        # first 12 chars of the raw key (Step 24 / api_key_service.py
        # `key_prefix=raw_key[:12]`). We never put the full secret on the
        # wire -- only the prefix is sent in the request body.
        raw_key = agent_ck.get("key") if isinstance(agent_ck, dict) else None
        if not isinstance(raw_key, str) or len(raw_key) < 12:
            raise AssertionError(
                f"Pillar 25 expected agent chat_key dict to carry a raw "
                f"key of length >=12; got {type(raw_key).__name__} "
                f"len={len(raw_key) if isinstance(raw_key, str) else 'n/a'}."
            )
        actor_key_prefix = raw_key[:12]

        # G1 + G2: the 200 response IS the worker-liveness assertion.
        # call(..., expect=200) raises if the route returns anything
        # other than 200 (incl. 504 from the route's own timeout).
        with pooled_client(timeout=_HTTP_TIMEOUT_SECONDS) as c:
            r = call(
                "POST",
                _PROBE_PATH,
                state.platform_admin_key,
                json={
                    "tenant_id": state.tenant_id,
                    "actor_key_prefix": actor_key_prefix,
                },
                expect=200,
                client=c,
            )
        body: dict[str, Any] = r.json()

        # G3: polled_for_action shape.
        polled = body.get("polled_for_action")
        if polled != _EXPECTED_ACTION:
            raise AssertionError(
                f"Pillar 25 expected polled_for_action="
                f"{_EXPECTED_ACTION!r} (default mode=malformed); "
                f"got {polled!r}. A future refactor that flipped the "
                f"route's default mode would surface here."
            )

        audit_id = body.get("audit_id")
        elapsed_ms = body.get("elapsed_ms")
        if not isinstance(audit_id, int) or audit_id <= 0:
            raise AssertionError(
                f"Pillar 25 expected positive int audit_id; got "
                f"{audit_id!r}."
            )
        if not isinstance(elapsed_ms, int) or elapsed_ms < 0:
            raise AssertionError(
                f"Pillar 25 expected non-negative int elapsed_ms; got "
                f"{elapsed_ms!r}."
            )

        return (
            f"worker pipeline alive: probe at {BASE_URL}{_PROBE_PATH} "
            f"observed {_EXPECTED_ACTION!r} audit_id={audit_id} in "
            f"{elapsed_ms}ms (mode=malformed, no LLM cost)."
        )


PILLAR = WorkerPipelineLivenessPillar()
