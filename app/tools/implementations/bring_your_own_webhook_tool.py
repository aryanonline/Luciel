"""bring_your_own_webhook — v1 catalog tool (§3.3.2, §3.3.5).

Dispatches a customer-configured outbound webhook with the full
security envelope from §3.3.5 (Decisions #5 / #6): subprocess
isolation, 30s hard timeout, input/output JSON-Schema validation,
restricted egress allowlist, transport-error retry with exponential
backoff, per-endpoint circuit breaker, audit row per invocation.

Action-classification tier: APPROVAL_REQUIRED — a BYO webhook fires
admin-defined code against an admin-defined endpoint with effects
the platform cannot reason about. The senior-advisor default Recap
§4 names for unknown-blast-radius work is approval-required.

``execution_mode`` is ``"subprocess"`` (Decision #5) — the only
catalog tool that runs outside the worker process.

Arc 12 WU6 — the body
=====================

The tool body now performs the real dispatch:

1. Resolve the admin-registered ``byo_webhook_endpoints`` row via
   ``endpoint_id`` (input parameter) within the
   ``(admin_id, instance_id)`` scope. Default-deny: no live row ⇒
   structured failure with no outbound call.
2. Hand off to :func:`app.tools.byo.sandbox.dispatch_byo_webhook`
   which enforces the §3.3.5 envelope and returns a
   ``DispatchEnvelope``.
3. Write a ``tool_execution_log`` audit row carrying
   ``execution_mode='subprocess'``, input/output hashes, latency,
   error_class, and the circuit-breaker state at dispatch.
4. Return a §3.3.1 output dict reflecting the dispatch result.

The circuit breaker is a process-singleton — one
``CircuitBreaker`` instance for the worker, backed by Redis in
production (``RedisBackend.from_settings()``) and by an in-memory
backend in tests (injected via ``set_circuit_breaker``).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.policy.action_classification import ActionTier
from app.tools.base import LucielTool, ToolContext
from app.tools.byo.circuit_breaker import (
    CircuitBreaker,
    InMemoryBackend,
    RedisBackend,
)
from app.tools.byo.sandbox import (
    DispatchEnvelope,
    canonical_hash,
    dispatch_byo_webhook,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Process-singleton circuit breaker
# ---------------------------------------------------------------------

_circuit_breaker: Optional[CircuitBreaker] = None


def get_circuit_breaker() -> CircuitBreaker:
    """Return the process-wide ``CircuitBreaker`` instance.

    Constructed lazily on first use. In production we wire it to
    Redis via ``RedisBackend.from_settings()``; if Redis is not
    available (e.g. a unit test that forgot to inject), we fall
    back to an in-memory backend with a logged warning so the
    test path remains functional.
    """
    global _circuit_breaker
    if _circuit_breaker is None:
        try:
            backend = RedisBackend.from_settings()
            _circuit_breaker = CircuitBreaker(backend=backend)
        except Exception:  # noqa: BLE001
            logger.warning(
                "BYO circuit breaker: Redis backend unavailable; "
                "falling back to in-memory backend.",
                exc_info=True,
            )
            _circuit_breaker = CircuitBreaker(
                backend=InMemoryBackend()
            )
    return _circuit_breaker


def set_circuit_breaker(breaker: Optional[CircuitBreaker]) -> None:
    """Test seam — inject a breaker (or clear it with ``None``)."""
    global _circuit_breaker
    _circuit_breaker = breaker


class BringYourOwnWebhookTool(LucielTool):

    declared_tier = ActionTier.APPROVAL_REQUIRED

    # Arc 15 WU4/WU5 — connection-contract gate (§3.3.2). The
    # ``outbound_webhook`` connector connects LIVE in this slice (the
    # webhook URL is non-secret config), so a configured endpoint yields
    # a ``connected`` row and the WU5 gate admits dispatch.
    requires_connection = "outbound_webhook"

    @property
    def tool_id(self) -> str:
        return "bring_your_own_webhook"

    @property
    def display_name(self) -> str:
        return "Bring-your-own webhook"

    @property
    def description(self) -> str:
        return (
            "Dispatch a customer-configured outbound webhook in a "
            "sandboxed subprocess with input/output schema validation, "
            "egress allowlist, retry policy, and circuit breaker."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "endpoint_id": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Identifier of the admin-registered BYO "
                        "endpoint (URL + schemas + allowlist live "
                        "there)."
                    ),
                },
                "payload": {
                    "type": "object",
                    "additionalProperties": True,
                    "description": (
                        "Payload validated against the endpoint's "
                        "admin-registered input schema BEFORE "
                        "subprocess dispatch."
                    ),
                },
            },
            "required": ["endpoint_id", "payload"],
            "additionalProperties": False,
        }

    @property
    def output_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "success": {"type": "boolean"},
                "output": {"type": "string"},
                "response": {
                    "type": "object",
                    "additionalProperties": True,
                },
                "error_class": {"type": ["string", "null"]},
                "status_code": {"type": ["integer", "null"]},
                "attempts": {"type": "integer"},
                "latency_ms": {"type": "integer"},
                "circuit_state_at_dispatch": {"type": "string"},
            },
            "required": ["success", "output"],
            "additionalProperties": True,
        }

    @property
    def requires_tier(self) -> tuple[str, ...]:
        return ("pro", "enterprise")

    @property
    def execution_mode(self) -> str:
        # Decision #5: BYO webhooks run in a subprocess so a hung or
        # crashing webhook cannot take the worker down with it.
        return "subprocess"

    async def execute(
        self,
        input: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        endpoint_id = int(input["endpoint_id"])
        payload = dict(input.get("payload") or {})

        # ------------------------------------------------------------
        # 1. Resolve the endpoint config (default-deny on missing).
        # ------------------------------------------------------------
        endpoint = self._resolve_endpoint(
            context, endpoint_id=endpoint_id
        )
        if endpoint is None:
            return self._failure_output(
                error_class="other",
                error_message=(
                    f"BYO endpoint {endpoint_id} not registered on "
                    "this instance."
                ),
                latency_ms=0,
                circuit_state="closed",
                attempts=0,
            )

        # ------------------------------------------------------------
        # 2. Dispatch through the sandbox.
        # ------------------------------------------------------------
        breaker = get_circuit_breaker()
        envelope: DispatchEnvelope = await dispatch_byo_webhook(
            endpoint_id=endpoint_id,
            endpoint_url=endpoint.endpoint_url,
            payload=payload,
            endpoint_input_schema=endpoint.input_schema,
            endpoint_output_schema=endpoint.output_schema,
            allowed_domains=list(endpoint.allowed_domains or []),
            breaker=breaker,
        )

        # ------------------------------------------------------------
        # 3. Write the audit row.
        # ------------------------------------------------------------
        self._write_audit_row(
            context=context,
            envelope=envelope,
            input_payload=payload,
        )

        # ------------------------------------------------------------
        # 4. Build the §3.3.1 output dict.
        # ------------------------------------------------------------
        if envelope.success:
            return {
                "success": True,
                "output": "Webhook dispatched successfully.",
                "response": envelope.output,
                "error_class": None,
                "status_code": envelope.status_code,
                "attempts": envelope.attempts,
                "latency_ms": envelope.latency_ms,
                "circuit_state_at_dispatch": (
                    envelope.circuit_state_at_dispatch
                ),
            }
        return self._failure_output(
            error_class=envelope.error_class or "other",
            error_message=envelope.error_message or "",
            latency_ms=envelope.latency_ms,
            circuit_state=envelope.circuit_state_at_dispatch,
            attempts=envelope.attempts,
            status_code=envelope.status_code,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _resolve_endpoint(
        self,
        context: ToolContext,
        *,
        endpoint_id: int,
    ):
        """Look up the live BYO endpoint row via the repo. Returns
        ``None`` if there's no live row (default-deny on missing
        config)."""
        if context.session is None:
            logger.warning(
                "BYO dispatch refused: no DB session in ToolContext "
                "(admin=%s instance=%s endpoint=%s).",
                context.admin_id, context.instance_id, endpoint_id,
            )
            return None
        try:
            from app.repositories.byo_webhook_endpoint_repository import (
                ByoWebhookEndpointRepository,
            )
            repo = ByoWebhookEndpointRepository(context.session)
            return repo.get_live_by_id(
                admin_id=context.admin_id,
                instance_id=context.instance_id,
                endpoint_id=endpoint_id,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "BYO endpoint lookup failed (admin=%s instance=%s "
                "endpoint=%s)",
                context.admin_id, context.instance_id, endpoint_id,
            )
            return None

    def _write_audit_row(
        self,
        *,
        context: ToolContext,
        envelope: DispatchEnvelope,
        input_payload: dict[str, Any],
    ) -> None:
        """Best-effort audit. Never raises — auditing must not take
        down a tool call."""
        if context.session is None:
            return
        try:
            from app.repositories.tool_execution_log_repository import (
                ToolExecutionLogRepository,
            )
            repo = ToolExecutionLogRepository(context.session)
            repo.record(
                admin_id=context.admin_id,
                instance_id=context.instance_id,
                tool_id="bring_your_own_webhook",
                execution_mode="subprocess",
                input_hash=canonical_hash(input_payload),
                output_hash=(
                    canonical_hash(envelope.output)
                    if envelope.output else None
                ),
                latency_ms=envelope.latency_ms,
                error_class=envelope.error_class,
                circuit_breaker_state=(
                    envelope.circuit_state_at_dispatch
                ),
                error_message=envelope.error_message,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "BYO audit-row write failed (admin=%s instance=%s)",
                context.admin_id, context.instance_id,
            )

    def _failure_output(
        self,
        *,
        error_class: str,
        error_message: str,
        latency_ms: int,
        circuit_state: str,
        attempts: int,
        status_code: Optional[int] = None,
    ) -> dict[str, Any]:
        return {
            "success": False,
            "output": (
                f"Webhook dispatch failed: {error_class}"
                + (f" — {error_message}" if error_message else "")
            ),
            "response": {},
            "error_class": error_class,
            "status_code": status_code,
            "attempts": attempts,
            "latency_ms": latency_ms,
            "circuit_state_at_dispatch": circuit_state,
        }
