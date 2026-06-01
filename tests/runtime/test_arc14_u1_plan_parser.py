"""Arc 14 U1 — PLAN tolerant-parser tests.

The parser layers structured output onto the provider-agnostic plain
TEXT ``LLMResponse.content``. It must NEVER raise (a parse failure
degrades to a low-confidence no-tool reply, §3.4.1).
"""
from __future__ import annotations

import unittest

from app.runtime.plan_parser import DEGRADED_CONFIDENCE, parse_plan


class TestParsePlan(unittest.TestCase):

    def test_clean_json_object(self):
        plan = parse_plan(
            '{"reply": "hello", '
            '"tool_calls": [{"tool": "t1", "parameters": {"a": 1}}], '
            '"confidence": 0.85}'
        )
        self.assertTrue(plan.parsed)
        self.assertEqual(plan.reply, "hello")
        self.assertEqual(plan.confidence, 0.85)
        self.assertEqual(len(plan.tool_calls), 1)
        self.assertEqual(plan.tool_calls[0].tool, "t1")
        self.assertEqual(plan.tool_calls[0].parameters, {"a": 1})

    def test_json_wrapped_in_prose_or_fence(self):
        plan = parse_plan(
            'Sure! ```json\n{"reply": "hi", "tool_calls": [], '
            '"confidence": 0.7}\n``` hope that helps'
        )
        self.assertTrue(plan.parsed)
        self.assertEqual(plan.reply, "hi")
        self.assertEqual(plan.confidence, 0.7)

    def test_non_json_degrades(self):
        plan = parse_plan("just some prose, no json here")
        self.assertFalse(plan.parsed)
        self.assertEqual(plan.reply, "just some prose, no json here")
        self.assertEqual(plan.tool_calls, [])
        self.assertEqual(plan.confidence, DEGRADED_CONFIDENCE)

    def test_empty_string_degrades(self):
        plan = parse_plan("")
        self.assertFalse(plan.parsed)
        self.assertEqual(plan.tool_calls, [])

    def test_confidence_clamped_to_unit_interval(self):
        self.assertEqual(parse_plan('{"reply":"x","confidence": 5}').confidence, 1.0)
        self.assertEqual(parse_plan('{"reply":"x","confidence": -2}').confidence, 0.0)

    def test_missing_confidence_uses_degraded_value(self):
        plan = parse_plan('{"reply": "x", "tool_calls": []}')
        self.assertTrue(plan.parsed)
        self.assertEqual(plan.confidence, DEGRADED_CONFIDENCE)

    def test_bool_confidence_rejected(self):
        # JSON `true` would coerce to 1.0 under a naive float() — guard it.
        plan = parse_plan('{"reply": "x", "confidence": true}')
        self.assertEqual(plan.confidence, DEGRADED_CONFIDENCE)

    def test_malformed_tool_calls_dropped_not_fatal(self):
        plan = parse_plan(
            '{"reply": "x", "confidence": 0.5, '
            '"tool_calls": ["bad", {"no_tool": 1}, {"tool": "good"}]}'
        )
        self.assertTrue(plan.parsed)
        self.assertEqual([c.tool for c in plan.tool_calls], ["good"])
        self.assertEqual(plan.tool_calls[0].parameters, {})

    def test_non_string_reply_falls_back_to_raw_text(self):
        plan = parse_plan('{"reply": 123, "confidence": 0.5}')
        self.assertTrue(plan.parsed)
        self.assertIn("123", plan.reply)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
