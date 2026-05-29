"""Arc 12 WU6 — BYO webhook subprocess sandbox tests.

Covers the §3.3.5 security envelope:

  1. 30s timeout kills the subprocess and yields tool failure.
  2. Output-schema-invalid response ⇒ tool failure, NO retry.
  3. Input-schema-invalid payload ⇒ rejected before dispatch.
  4. Transport error ⇒ retried with exponential backoff (timing +
     attempt counts asserted).
  5. Circuit breaker: 5 consecutive transport failures within 60s
     opens the breaker; half-open after 60s; closed on first
     success.
  6. Egress to a non-allowlisted domain is blocked.
  7. Audit row written with all required fields including
     circuit-breaker state at dispatch.

The sandbox is exercised with ``SPAWN_OVERRIDE`` set so we do not
actually spawn subprocesses — the subprocess boundary is unit-tested
separately in the timeout test. The retry/backoff/circuit-breaker
logic is the load-bearing part.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("MODERATION_PROVIDER", "null")
os.environ.setdefault("OPENAI_API_KEY", "dummy")


# =====================================================================
# Helpers
# =====================================================================


def _basic_input_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "amount": {"type": "integer", "minimum": 0},
        },
        "required": ["name"],
        "additionalProperties": True,
    }


def _basic_output_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "id": {"type": "string"},
        },
        "required": ["ok"],
        "additionalProperties": True,
    }


def _fresh_breaker():
    from app.tools.byo.circuit_breaker import (
        CircuitBreaker,
        InMemoryBackend,
    )

    return CircuitBreaker(backend=InMemoryBackend())


def _fast_breaker():
    """Breaker with shorter window/duration for tests."""
    from app.tools.byo.circuit_breaker import (
        CircuitBreaker,
        InMemoryBackend,
    )

    return CircuitBreaker(
        backend=InMemoryBackend(),
        failure_threshold=5,
        failure_window_seconds=60,
        open_duration_seconds=60,
    )


def _override_spawn(envelopes: list[dict]):
    """Install a SPAWN_OVERRIDE that returns successive envelopes
    from the given list. Returns a recorder list of calls made."""
    from app.tools.byo import sandbox

    calls: list[dict] = []
    iterator = iter(envelopes)

    async def fake(endpoint_url, payload, allowed_domains, timeout_seconds):
        calls.append({
            "endpoint_url": endpoint_url,
            "payload": payload,
            "allowed_domains": allowed_domains,
            "timeout_seconds": timeout_seconds,
        })
        try:
            return next(iterator)
        except StopIteration:
            raise AssertionError(
                "Sandbox attempted more dispatches than envelopes "
                "provided by the test."
            )

    sandbox.SPAWN_OVERRIDE = fake
    return calls


def _clear_spawn_override():
    from app.tools.byo import sandbox

    sandbox.SPAWN_OVERRIDE = None


# =====================================================================
# 1. Input schema validated before dispatch
# =====================================================================


def test_input_schema_invalid_rejected_before_dispatch() -> None:
    """An input that fails the admin-registered input schema MUST be
    rejected before the subprocess is ever spawned. The dispatch
    layer never runs and the circuit breaker is not touched."""
    from app.tools.byo.sandbox import dispatch_byo_webhook

    spawn_calls = _override_spawn([
        {"ok": True, "status_code": 200, "response_body": {"ok": True},
         "error_kind": None, "error_message": None},
    ])
    try:
        breaker = _fresh_breaker()
        envelope = asyncio.run(dispatch_byo_webhook(
            endpoint_id=1,
            endpoint_url="https://api.example.com/hook",
            payload={"amount": 5},  # missing required "name"
            endpoint_input_schema=_basic_input_schema(),
            endpoint_output_schema=_basic_output_schema(),
            allowed_domains=["api.example.com"],
            breaker=breaker,
        ))
        assert envelope.success is False
        assert envelope.error_class == "schema_input"
        assert envelope.attempts == 0
        assert spawn_calls == [], (
            "Subprocess MUST NOT be spawned when input schema fails."
        )
        # Breaker untouched.
        assert breaker.failure_count(1) == 0
    finally:
        _clear_spawn_override()


# =====================================================================
# 2. Output schema invalid → tool failure, NO retry
# =====================================================================


def test_output_schema_invalid_is_terminal_no_retry() -> None:
    """A response that fails the admin-registered output schema MUST
    be treated as a tool failure with NO retry. Subsequent dispatch
    attempts MUST NOT happen even though we configured 2 retries."""
    from app.tools.byo.sandbox import dispatch_byo_webhook

    spawn_calls = _override_spawn([
        # First (and ONLY) dispatch — returns 200 with an object that
        # the output_schema rejects (missing required "ok").
        {"ok": True, "status_code": 200,
         "response_body": {"extra": "field"},
         "error_kind": None, "error_message": None},
    ])
    try:
        breaker = _fresh_breaker()
        envelope = asyncio.run(dispatch_byo_webhook(
            endpoint_id=2,
            endpoint_url="https://api.example.com/hook",
            payload={"name": "alice"},
            endpoint_input_schema=_basic_input_schema(),
            endpoint_output_schema=_basic_output_schema(),
            allowed_domains=["api.example.com"],
            breaker=breaker,
        ))
        assert envelope.success is False
        assert envelope.error_class == "schema_output"
        assert envelope.attempts == 1, (
            "Schema-output failure MUST NOT be retried."
        )
        assert len(spawn_calls) == 1
        # A schema failure must NOT count against the circuit
        # breaker (the endpoint is alive; the response shape is just
        # wrong).
        assert breaker.failure_count(2) == 0
    finally:
        _clear_spawn_override()


# =====================================================================
# 3. Egress allowlist blocks non-listed domain
# =====================================================================


def test_egress_denied_to_non_allowlisted_domain() -> None:
    from app.tools.byo.sandbox import dispatch_byo_webhook

    spawn_calls = _override_spawn([
        {"ok": True, "status_code": 200,
         "response_body": {"ok": True},
         "error_kind": None, "error_message": None},
    ])
    try:
        breaker = _fresh_breaker()
        envelope = asyncio.run(dispatch_byo_webhook(
            endpoint_id=3,
            endpoint_url="https://evil.example.org/hook",
            payload={"name": "alice"},
            endpoint_input_schema=_basic_input_schema(),
            endpoint_output_schema=_basic_output_schema(),
            allowed_domains=["api.example.com"],  # NOT evil.example.org
            breaker=breaker,
        ))
        assert envelope.success is False
        assert envelope.error_class == "egress_denied"
        assert spawn_calls == [], (
            "Egress-denied URLs MUST NOT spawn a subprocess."
        )
        assert breaker.failure_count(3) == 0
    finally:
        _clear_spawn_override()


# =====================================================================
# 4. Transport error retried with exponential backoff
# =====================================================================


def test_transport_error_retried_with_exponential_backoff() -> None:
    """A transport error MUST be retried up to ``retry_count`` times
    with exponential backoff (initial 500ms, max 5s by default).
    On the 3rd attempt success we observe sleeps of 500ms then
    1000ms."""
    from app.tools.byo.sandbox import dispatch_byo_webhook

    spawn_calls = _override_spawn([
        {"ok": False, "status_code": 0, "response_body": {},
         "error_kind": "transport", "error_message": "conn reset"},
        {"ok": False, "status_code": 0, "response_body": {},
         "error_kind": "transport", "error_message": "conn reset"},
        {"ok": True, "status_code": 200,
         "response_body": {"ok": True},
         "error_kind": None, "error_message": None},
    ])
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    try:
        breaker = _fresh_breaker()
        envelope = asyncio.run(dispatch_byo_webhook(
            endpoint_id=4,
            endpoint_url="https://api.example.com/hook",
            payload={"name": "alice"},
            endpoint_input_schema=_basic_input_schema(),
            endpoint_output_schema=_basic_output_schema(),
            allowed_domains=["api.example.com"],
            breaker=breaker,
            sleep_fn=fake_sleep,
        ))
        assert envelope.success is True
        assert envelope.attempts == 3
        assert len(spawn_calls) == 3
        # Default backoff: 0.5s, 1.0s
        assert sleeps == [pytest.approx(0.5), pytest.approx(1.0)], (
            f"Expected exponential backoff [0.5, 1.0], got {sleeps!r}"
        )
        # After success the breaker resets — failure_count back to 0.
        assert breaker.failure_count(4) == 0
    finally:
        _clear_spawn_override()


def test_transport_error_exhausts_retries_and_fails() -> None:
    from app.tools.byo.sandbox import dispatch_byo_webhook

    spawn_calls = _override_spawn([
        {"ok": False, "status_code": 0, "response_body": {},
         "error_kind": "transport", "error_message": "conn reset"},
        {"ok": False, "status_code": 0, "response_body": {},
         "error_kind": "transport", "error_message": "conn reset"},
        {"ok": False, "status_code": 0, "response_body": {},
         "error_kind": "transport", "error_message": "conn reset"},
    ])
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    try:
        breaker = _fresh_breaker()
        envelope = asyncio.run(dispatch_byo_webhook(
            endpoint_id=5,
            endpoint_url="https://api.example.com/hook",
            payload={"name": "alice"},
            endpoint_input_schema=_basic_input_schema(),
            endpoint_output_schema=_basic_output_schema(),
            allowed_domains=["api.example.com"],
            breaker=breaker,
            sleep_fn=fake_sleep,
        ))
        assert envelope.success is False
        assert envelope.error_class == "transport"
        assert envelope.attempts == 3
        # 3 attempts ⇒ 2 backoff sleeps
        assert len(sleeps) == 2
        # 3 failures recorded against the breaker (still under the
        # default threshold of 5).
        assert breaker.failure_count(5) == 3
    finally:
        _clear_spawn_override()


# =====================================================================
# 5. Circuit breaker — opens / half-opens / closes
# =====================================================================


def test_circuit_breaker_opens_after_five_consecutive_failures() -> None:
    """5 consecutive transport failures within the 60s window MUST
    open the breaker. Subsequent dispatch attempts return
    ``circuit_open`` without spawning the subprocess."""
    from app.tools.byo.circuit_breaker import (
        CircuitBreaker,
        InMemoryBackend,
        STATE_OPEN,
    )
    from app.tools.byo.sandbox import dispatch_byo_webhook

    spawn_calls = _override_spawn(
        # 5 dispatches × 3 attempts each = 15 transport failures,
        # but the breaker should open after 5 total ⇒ later
        # dispatches should short-circuit. We provide a generous
        # buffer just in case the breaker semantics differ.
        [
            {"ok": False, "status_code": 0, "response_body": {},
             "error_kind": "transport", "error_message": "x"}
        ] * 20
    )
    try:
        # Use retry_count=0 so each call is a single attempt — makes
        # the 5-failure budget audit easy.
        breaker = CircuitBreaker(backend=InMemoryBackend())
        for i in range(5):
            env = asyncio.run(dispatch_byo_webhook(
                endpoint_id=10,
                endpoint_url="https://api.example.com/hook",
                payload={"name": "x"},
                endpoint_input_schema=_basic_input_schema(),
                endpoint_output_schema=_basic_output_schema(),
                allowed_domains=["api.example.com"],
                breaker=breaker,
                retry_count=0,
            ))
            assert env.success is False, f"call {i} should fail"

        # After 5 transport failures the breaker is open.
        assert breaker.current_state(10) == STATE_OPEN

        # Next dispatch — short-circuits with circuit_open.
        env = asyncio.run(dispatch_byo_webhook(
            endpoint_id=10,
            endpoint_url="https://api.example.com/hook",
            payload={"name": "x"},
            endpoint_input_schema=_basic_input_schema(),
            endpoint_output_schema=_basic_output_schema(),
            allowed_domains=["api.example.com"],
            breaker=breaker,
            retry_count=0,
        ))
        assert env.success is False
        assert env.error_class == "circuit_open"
        assert env.attempts == 0
        # circuit_state recorded in the audit envelope
        assert env.circuit_state_at_dispatch == STATE_OPEN
    finally:
        _clear_spawn_override()


def test_circuit_breaker_half_opens_after_window_then_closes_on_success() -> None:
    """After the open-duration elapses, the breaker should report
    half_open on the next read, allow ONE probe, and close on the
    probe's success."""
    from app.tools.byo.circuit_breaker import (
        CircuitBreaker,
        InMemoryBackend,
        STATE_CLOSED,
        STATE_HALF_OPEN,
        STATE_OPEN,
    )

    # Inject a controllable clock so we don't sleep 60s in the test.
    now_value = [1000.0]

    def fake_now():
        return now_value[0]

    breaker = CircuitBreaker(
        backend=InMemoryBackend(),
        failure_threshold=5,
        failure_window_seconds=60,
        open_duration_seconds=60,
        now_fn=fake_now,
    )

    # Trip the breaker by recording 5 failures manually (avoids
    # needing the dispatch path here).
    for _ in range(5):
        breaker.record_failure(99)
    assert breaker.current_state(99) == STATE_OPEN

    # Advance time past the open window — current_state should report
    # half_open.
    now_value[0] += 61.0
    assert breaker.current_state(99) == STATE_HALF_OPEN

    # before_dispatch grants the probe slot.
    snap = breaker.before_dispatch(99)
    assert snap.state == STATE_HALF_OPEN

    # A second concurrent before_dispatch should be refused (probe
    # lock held).
    from app.tools.byo.circuit_breaker import CircuitOpenError
    with pytest.raises(CircuitOpenError):
        breaker.before_dispatch(99)

    # Probe succeeds — breaker closes.
    breaker.record_success(99)
    assert breaker.current_state(99) == STATE_CLOSED

    # The probe slot is released and failure_count is reset.
    assert breaker.failure_count(99) == 0


def test_circuit_breaker_half_open_failure_reopens() -> None:
    """A failure during half-open re-opens the breaker for another
    open-duration window."""
    from app.tools.byo.circuit_breaker import (
        CircuitBreaker,
        InMemoryBackend,
        STATE_HALF_OPEN,
        STATE_OPEN,
    )

    now_value = [1000.0]
    breaker = CircuitBreaker(
        backend=InMemoryBackend(),
        failure_threshold=5,
        failure_window_seconds=60,
        open_duration_seconds=60,
        now_fn=lambda: now_value[0],
    )
    for _ in range(5):
        breaker.record_failure(77)
    assert breaker.current_state(77) == STATE_OPEN

    now_value[0] += 61.0
    assert breaker.current_state(77) == STATE_HALF_OPEN
    breaker.before_dispatch(77)  # take the probe slot

    breaker.record_failure(77)
    # The failure during half-open trips the breaker open again.
    assert breaker.current_state(77) == STATE_OPEN


# =====================================================================
# 6. 30s timeout kills the subprocess and yields tool failure
#    (exercises the real subprocess boundary).
# =====================================================================


def test_subprocess_timeout_kills_and_yields_tool_failure() -> None:
    """End-to-end subprocess test: a child that sleeps longer than
    the boundary timeout MUST be killed and the dispatch MUST return
    a structured failure with ``error_class='timeout'``.

    We swap the real ``SUBPROCESS_CMD`` for a tiny python one-liner
    that sleeps forever, and we shorten the boundary to 1 second
    via ``BYO_HARD_TIMEOUT_SECONDS`` override. The subprocess is
    real — the parent's ``asyncio.wait_for`` + ``proc.kill()``
    enforce the kill.
    """
    from app.tools.byo import sandbox

    # Replace SUBPROCESS_CMD with a hung sleeper, and shrink the
    # timeout. Restore on exit.
    original_cmd = sandbox.SUBPROCESS_CMD
    original_timeout = sandbox.BYO_HARD_TIMEOUT_SECONDS
    sandbox.SUBPROCESS_CMD = [
        sys.executable, "-c",
        "import sys, time; sys.stdin.read(); time.sleep(60)",
    ]
    sandbox.BYO_HARD_TIMEOUT_SECONDS = 1
    sandbox.SPAWN_OVERRIDE = None  # ensure we use the real spawn

    try:
        breaker = _fresh_breaker()
        started = time.monotonic()
        envelope = asyncio.run(sandbox.dispatch_byo_webhook(
            endpoint_id=20,
            endpoint_url="https://api.example.com/hook",
            payload={"name": "alice"},
            endpoint_input_schema=_basic_input_schema(),
            endpoint_output_schema=_basic_output_schema(),
            allowed_domains=["api.example.com"],
            breaker=breaker,
            retry_count=0,
            sleep_fn=lambda s: asyncio.sleep(0),
        ))
        elapsed = time.monotonic() - started
        assert envelope.success is False
        assert envelope.error_class == "timeout"
        # The dispatch killed the child within roughly the timeout
        # window — must NOT have waited the full 60s sleep.
        assert elapsed < 10.0, (
            f"timeout enforcement leaked: dispatch ran {elapsed:.1f}s"
        )
        # Timeout counted against the breaker as a transport-class
        # failure.
        assert breaker.failure_count(20) == 1
    finally:
        sandbox.SUBPROCESS_CMD = original_cmd
        sandbox.BYO_HARD_TIMEOUT_SECONDS = original_timeout


# =====================================================================
# 7. Audit row contains all the required fields
# =====================================================================


def test_audit_row_recorded_with_all_required_fields() -> None:
    """The tool body MUST write a tool_execution_log row carrying
    execution_mode, input/output hashes, latency, error_class, and
    circuit_breaker_state at dispatch (§3.3.5)."""

    from sqlalchemy import (
        Column,
        DateTime,
        ForeignKey,
        Integer,
        JSON,
        MetaData,
        String,
        Table,
        create_engine,
        func,
        text as sa_text,
    )
    from sqlalchemy.orm import sessionmaker

    from app.tools.base import ToolContext
    from app.tools.byo import sandbox
    from app.tools.implementations.bring_your_own_webhook_tool import (
        BringYourOwnWebhookTool,
        set_circuit_breaker,
    )

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    md = MetaData()
    Table(
        "admins", md,
        Column("id", String(100), primary_key=True),
        Column("name", String(200), nullable=False),
    )
    Table(
        "instances", md,
        Column("id", Integer, primary_key=True),
        Column("admin_id", String(100),
               ForeignKey("admins.id"), nullable=False),
        Column("instance_slug", String(100), nullable=False),
    )
    Table(
        "byo_webhook_endpoints", md,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("admin_id", String(100),
               ForeignKey("admins.id"), nullable=False, index=True),
        Column("instance_id", Integer,
               ForeignKey("instances.id"), nullable=False, index=True),
        Column("endpoint_url", String(2048), nullable=False),
        Column("input_schema", JSON, nullable=False),
        Column("output_schema", JSON, nullable=False),
        Column("allowed_domains", JSON, nullable=False),
        Column("created_at", DateTime(timezone=True),
               nullable=False, server_default=func.now()),
        Column("updated_at", DateTime(timezone=True),
               nullable=False, server_default=func.now()),
        Column("revoked_at", DateTime(timezone=True), nullable=True),
    )
    Table(
        "tool_execution_log", md,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("admin_id", String(100),
               ForeignKey("admins.id"), nullable=False, index=True),
        Column("instance_id", Integer,
               ForeignKey("instances.id"), nullable=False, index=True),
        Column("tool_id", String(64), nullable=False),
        Column("execution_mode", String(20), nullable=False),
        Column("input_hash", String(64), nullable=False),
        Column("output_hash", String(64), nullable=True),
        Column("latency_ms", Integer, nullable=False),
        Column("error_class", String(40), nullable=True),
        Column("circuit_breaker_state", String(20), nullable=True),
        Column("error_message", String(500), nullable=True),
        Column("created_at", DateTime(timezone=True),
               nullable=False, server_default=func.now(), index=True),
    )
    md.create_all(engine)

    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    session.execute(
        sa_text("INSERT INTO admins (id, name) VALUES ('adm', 'A')")
    )
    session.execute(sa_text(
        "INSERT INTO instances (id, admin_id, instance_slug) "
        "VALUES (1, 'adm', 'i-1')"
    ))
    import json as _json
    session.execute(sa_text(
        "INSERT INTO byo_webhook_endpoints "
        "(id, admin_id, instance_id, endpoint_url, input_schema, "
        " output_schema, allowed_domains) "
        "VALUES (1, 'adm', 1, 'https://api.example.com/hook', "
        ":input_s, :output_s, :allowed)"
    ), {
        "input_s": _json.dumps(_basic_input_schema()),
        "output_s": _json.dumps(_basic_output_schema()),
        "allowed": _json.dumps(["api.example.com"]),
    })
    session.commit()

    # Override spawn to return success, install fresh breaker.
    spawn_calls = _override_spawn([
        {"ok": True, "status_code": 200,
         "response_body": {"ok": True, "id": "abc"},
         "error_kind": None, "error_message": None},
    ])
    breaker = _fresh_breaker()
    set_circuit_breaker(breaker)

    try:
        tool = BringYourOwnWebhookTool()
        ctx = ToolContext(
            admin_id="adm", instance_id=1, session=session
        )
        out = asyncio.run(tool.execute(
            {"endpoint_id": 1, "payload": {"name": "alice"}},
            ctx,
        ))
        assert out["success"] is True

        rows = session.execute(sa_text(
            "SELECT admin_id, instance_id, tool_id, execution_mode, "
            "input_hash, output_hash, latency_ms, error_class, "
            "circuit_breaker_state FROM tool_execution_log "
            "ORDER BY id"
        )).fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row[0] == "adm"
        assert row[1] == 1
        assert row[2] == "bring_your_own_webhook"
        assert row[3] == "subprocess"
        assert isinstance(row[4], str) and len(row[4]) == 64  # input_hash
        assert isinstance(row[5], str) and len(row[5]) == 64  # output_hash
        assert isinstance(row[6], int) and row[6] >= 0  # latency_ms
        assert row[7] is None  # error_class on success
        assert row[8] == "closed"  # circuit_breaker_state at dispatch
    finally:
        _clear_spawn_override()
        set_circuit_breaker(None)


def test_audit_row_records_failure_class_and_circuit_state() -> None:
    """A failure path (transport error exhausts retries) MUST land
    in the audit row with the correct error_class + the breaker
    state visible at dispatch attempt."""

    from sqlalchemy import (
        Column,
        DateTime,
        ForeignKey,
        Integer,
        JSON,
        MetaData,
        String,
        Table,
        create_engine,
        func,
        text as sa_text,
    )
    from sqlalchemy.orm import sessionmaker

    from app.tools.base import ToolContext
    from app.tools.byo.circuit_breaker import STATE_CLOSED
    from app.tools.implementations.bring_your_own_webhook_tool import (
        BringYourOwnWebhookTool,
        set_circuit_breaker,
    )

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    md = MetaData()
    Table(
        "admins", md,
        Column("id", String(100), primary_key=True),
        Column("name", String(200), nullable=False),
    )
    Table(
        "instances", md,
        Column("id", Integer, primary_key=True),
        Column("admin_id", String(100),
               ForeignKey("admins.id"), nullable=False),
        Column("instance_slug", String(100), nullable=False),
    )
    Table(
        "byo_webhook_endpoints", md,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("admin_id", String(100), nullable=False, index=True),
        Column("instance_id", Integer, nullable=False, index=True),
        Column("endpoint_url", String(2048), nullable=False),
        Column("input_schema", JSON, nullable=False),
        Column("output_schema", JSON, nullable=False),
        Column("allowed_domains", JSON, nullable=False),
        Column("created_at", DateTime(timezone=True),
               nullable=False, server_default=func.now()),
        Column("updated_at", DateTime(timezone=True),
               nullable=False, server_default=func.now()),
        Column("revoked_at", DateTime(timezone=True), nullable=True),
    )
    Table(
        "tool_execution_log", md,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("admin_id", String(100), nullable=False, index=True),
        Column("instance_id", Integer, nullable=False, index=True),
        Column("tool_id", String(64), nullable=False),
        Column("execution_mode", String(20), nullable=False),
        Column("input_hash", String(64), nullable=False),
        Column("output_hash", String(64), nullable=True),
        Column("latency_ms", Integer, nullable=False),
        Column("error_class", String(40), nullable=True),
        Column("circuit_breaker_state", String(20), nullable=True),
        Column("error_message", String(500), nullable=True),
        Column("created_at", DateTime(timezone=True),
               nullable=False, server_default=func.now(), index=True),
    )
    md.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    session.execute(
        sa_text("INSERT INTO admins (id, name) VALUES ('adm', 'A')")
    )
    session.execute(sa_text(
        "INSERT INTO instances (id, admin_id, instance_slug) "
        "VALUES (1, 'adm', 'i-1')"
    ))
    import json as _json
    session.execute(sa_text(
        "INSERT INTO byo_webhook_endpoints "
        "(id, admin_id, instance_id, endpoint_url, input_schema, "
        " output_schema, allowed_domains) "
        "VALUES (1, 'adm', 1, 'https://api.example.com/hook', "
        ":input_s, :output_s, :allowed)"
    ), {
        "input_s": _json.dumps(_basic_input_schema()),
        "output_s": _json.dumps(_basic_output_schema()),
        "allowed": _json.dumps(["api.example.com"]),
    })
    session.commit()

    # All 3 attempts fail with transport.
    spawn_calls = _override_spawn([
        {"ok": False, "status_code": 0, "response_body": {},
         "error_kind": "transport", "error_message": "x"},
        {"ok": False, "status_code": 0, "response_body": {},
         "error_kind": "transport", "error_message": "x"},
        {"ok": False, "status_code": 0, "response_body": {},
         "error_kind": "transport", "error_message": "x"},
    ])
    breaker = _fresh_breaker()
    set_circuit_breaker(breaker)

    # Patch the sandbox's sleep so we don't actually wait.
    from app.tools.byo import sandbox

    original_sleep_fn = None  # the sandbox uses asyncio.sleep by default
    try:
        # We can patch dispatch_byo_webhook's sleep_fn directly by
        # monkeypatching asyncio.sleep at module level — simpler is
        # to wrap the tool call so sleep_fn is injected. But the
        # tool body does not expose sleep_fn. We patch asyncio.sleep.
        import asyncio as _asyncio
        original_sleep = _asyncio.sleep

        async def fast_sleep(s):
            return await original_sleep(0)

        _asyncio.sleep = fast_sleep
        try:
            tool = BringYourOwnWebhookTool()
            ctx = ToolContext(
                admin_id="adm", instance_id=1, session=session
            )
            out = asyncio.run(tool.execute(
                {"endpoint_id": 1, "payload": {"name": "alice"}},
                ctx,
            ))
        finally:
            _asyncio.sleep = original_sleep

        assert out["success"] is False
        assert out["error_class"] == "transport"

        rows = session.execute(sa_text(
            "SELECT tool_id, execution_mode, error_class, "
            "circuit_breaker_state, latency_ms FROM tool_execution_log"
        )).fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row[0] == "bring_your_own_webhook"
        assert row[1] == "subprocess"
        assert row[2] == "transport"
        # The breaker state recorded is what was visible at the FIRST
        # attempt of this dispatch — closed, because the call started
        # below the threshold even though all 3 attempts failed.
        assert row[3] == STATE_CLOSED
        assert isinstance(row[4], int)
    finally:
        _clear_spawn_override()
        set_circuit_breaker(None)
