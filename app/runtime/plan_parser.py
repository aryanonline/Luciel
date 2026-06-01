"""Arc 14 U1 — PLAN structured-output contract + tolerant parser.

The agentic loop's PLAN step needs structured output —
``{reply, tool_calls, confidence}`` — but the provider-agnostic
``LLMResponse.content`` is plain TEXT (§5.4: we do NOT add structured
fields to ``LLMBase`` / provider clients). So PLAN layers a JSON-mode
*prompt instruction* on top of the assembled prompt and parses the
text the model returns back into a ``Plan`` here.

Doctrine: PLAN must NEVER crash the turn. A malformed / non-JSON
response degrades gracefully to a low-confidence, no-tool reply that
carries the raw text as the customer-facing answer (§3.4.1: "on parse
failure, degrade gracefully — treat as low-confidence no-tool reply").
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# The instruction appended to the assembled prompt so the model emits
# a single JSON object we can lift back into a ``Plan``. Kept terse and
# provider-neutral — it rides on the existing text ``content`` channel,
# so no provider client changes are required (§5.4).
PLAN_JSON_INSTRUCTION: str = (
    "\n\nRespond with a SINGLE JSON object and nothing else, matching:\n"
    '{"reply": "<text for the customer>", '
    '"tool_calls": [{"tool": "<tool_id>", "parameters": {}}], '
    '"confidence": <number between 0 and 1>}\n'
    "Use an empty tool_calls list when no tool is needed."
)

# Confidence assigned when PLAN output cannot be parsed as structured
# JSON. Low enough that the (future U2) outcome gate's "cannot
# confidently answer" signal can key off it, but the turn still
# produces a customer-facing reply rather than crashing.
DEGRADED_CONFIDENCE: float = 0.3


@dataclass
class ToolCall:
    """One tool invocation requested by PLAN. ``parameters`` is the
    payload the broker validates against the tool's input schema."""

    tool: str
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass
class Plan:
    """Structured PLAN output.

    ``parsed`` records whether the JSON parse succeeded; ``False`` means
    the loop degraded gracefully (``reply`` carries the raw model text,
    ``tool_calls`` is empty, ``confidence`` is ``DEGRADED_CONFIDENCE``).
    """

    reply: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    confidence: float = 0.0
    parsed: bool = True


def parse_plan(raw_text: str) -> Plan:
    """Tolerantly parse a PLAN response into a ``Plan``.

    Strategy, most- to least-strict:
      1. Parse the whole string as JSON.
      2. If that fails, lift the first ``{...}`` span and parse that
         (models often wrap JSON in prose or a ``` fence).
      3. If that fails too, degrade: the raw text becomes the reply,
         no tools, ``DEGRADED_CONFIDENCE``.

    Never raises.
    """
    obj = _load_json_object(raw_text)
    if obj is None:
        logger.info("PLAN parse degraded — no JSON object in response")
        return Plan(
            reply=raw_text.strip(),
            tool_calls=[],
            confidence=DEGRADED_CONFIDENCE,
            parsed=False,
        )

    reply = obj.get("reply")
    if not isinstance(reply, str):
        reply = raw_text.strip()

    confidence = obj.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(
        confidence, bool
    ):
        confidence = DEGRADED_CONFIDENCE
    else:
        # Clamp to [0, 1] — a model can hallucinate out-of-range values.
        confidence = max(0.0, min(1.0, float(confidence)))

    return Plan(
        reply=reply,
        tool_calls=_parse_tool_calls(obj.get("tool_calls")),
        confidence=confidence,
        parsed=True,
    )


def _load_json_object(raw_text: str) -> dict[str, Any] | None:
    """Return the parsed JSON object, or None if the text holds none."""
    text = raw_text.strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, ValueError):
        pass

    # Fall back to the first balanced-looking {...} span.
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


def _parse_tool_calls(raw: Any) -> list[ToolCall]:
    """Normalise the ``tool_calls`` field into ``list[ToolCall]``.

    Drops malformed entries rather than failing the whole parse — a
    single bad tool-call shape should not crash the turn.
    """
    if not isinstance(raw, list):
        return []
    calls: list[ToolCall] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        tool = entry.get("tool")
        if not isinstance(tool, str) or not tool:
            continue
        params = entry.get("parameters")
        if not isinstance(params, dict):
            params = {}
        calls.append(ToolCall(tool=tool, parameters=params))
    return calls
