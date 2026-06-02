"""ARC 16 — LLM entity extractor adapter tests (no network).

Validates the parse path of LLMEntityExtractor with a fake LLM client
returning canned content. The live model call is out of scope (runbook
with a real key); what's proven here is that whatever JSON a model
returns is parsed into ExtractionResult correctly, domain-agnostically,
and that malformed output degrades to an empty result (never raises).
"""
from __future__ import annotations

import unittest

from app.integrations.llm.base import LLMResponse
from app.knowledge.graph_extractor import (
    ExtractionResult,
    LLMEntityExtractor,
)


class _FakeClient:
    def __init__(self, content: str) -> None:
        self._content = content

    def generate(self, request):  # noqa: ANN001
        return LLMResponse(
            content=self._content, model="fake", provider="fake"
        )

    def generate_stream(self, request):  # noqa: ANN001
        yield self._content


class TestLLMEntityExtractor(unittest.TestCase):
    def test_parses_clean_json_domain_agnostic(self):
        # A med-spa shape — proves no real-estate ontology is assumed.
        content = (
            '{"entities":['
            '{"entity_type":"Service","entity_label":"Microneedling",'
            '"attributes":{"price":300}},'
            '{"entity_type":"Practitioner","entity_label":"Dr. Lee"}],'
            '"relations":['
            '{"src_label":"Dr. Lee","src_type":"Practitioner",'
            '"dst_label":"Microneedling","dst_type":"Service",'
            '"relationship_type":"OFFERS"}]}'
        )
        res = LLMEntityExtractor(_FakeClient(content)).extract(
            text="Dr. Lee offers microneedling for $300."
        )
        self.assertEqual(len(res.entities), 2)
        self.assertEqual(len(res.relations), 1)
        self.assertEqual(res.entities[0].entity_type, "Service")
        self.assertEqual(res.entities[0].attributes, {"price": 300})
        self.assertEqual(res.relations[0].relationship_type, "OFFERS")

    def test_parses_json_wrapped_in_markdown_fence(self):
        content = (
            "Here is the graph:\n```json\n"
            '{"entities":[{"entity_type":"Role","entity_label":"Engineer"}],'
            '"relations":[]}\n```\n'
        )
        res = LLMEntityExtractor(_FakeClient(content)).extract(text="x")
        self.assertEqual(len(res.entities), 1)
        self.assertEqual(res.entities[0].entity_type, "Role")

    def test_malformed_output_returns_empty_never_raises(self):
        for content in ["not json at all", "", "{broken json", "[]"]:
            res = LLMEntityExtractor(_FakeClient(content)).extract(text="x")
            self.assertIsInstance(res, ExtractionResult)
            self.assertEqual(len(res.entities), 0)
            self.assertEqual(len(res.relations), 0)

    def test_empty_text_short_circuits(self):
        res = LLMEntityExtractor(_FakeClient("{}")).extract(text="   ")
        self.assertEqual(len(res.entities), 0)

    def test_client_exception_degrades_to_empty(self):
        class _Boom:
            def generate(self, request):  # noqa: ANN001
                raise RuntimeError("provider down")

            def generate_stream(self, request):  # noqa: ANN001
                raise RuntimeError("provider down")

        res = LLMEntityExtractor(_Boom()).extract(text="something")
        self.assertEqual(len(res.entities), 0)
