"""Unit tests for src/field_discovery.py (pure Python parts — no LLM/Django)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.field_discovery import (
    _content_summary,
    _strip_fences,
    _parse_llm_response,
    _build_flat_schema,
    _fallback_jsonld,
)


class TestContentSummary:
    def test_strips_boilerplate(self):
        html = "<nav>nav stuff</nav><p>hello world</p><script>var x=1;</script><footer>foot</footer>"
        text = _content_summary(html)
        assert "hello world" in text
        assert "nav" not in text.lower()
        assert "foot" not in text.lower()
        assert "var x" not in text

    def test_truncates(self):
        html = "<p>" + "x" * 10000 + "</p>"
        assert len(_content_summary(html, limit=100)) <= 100

    def test_empty(self):
        assert _content_summary("") == ""


class TestStripFences:
    def test_json_fence(self):
        assert _strip_fences("```json\n{\"a\":1}\n```") == '{"a":1}'

    def test_plain_fence(self):
        assert _strip_fences("```\n{\"a\":1}\n```") == '{"a":1}'

    def test_no_fence(self):
        assert _strip_fences('{"a":1}') == '{"a":1}'

    def test_bare_json_prefix(self):
        assert _strip_fences('json{"a":1}') == '{"a":1}'

    def test_empty(self):
        assert _strip_fences("") == ""


class TestParseLLMResponse:
    def test_valid_product(self):
        text = '{"content_type":"product","fields":[{"name":"title","type":"text","required":true},{"name":"price","type":"number","required":true}],"json_schema":{"type":"object","properties":{"title":{"type":"string"},"price":{"type":"number"}},"required":["title","price"]},"model_notes":"ok"}'
        r = _parse_llm_response(text)
        assert r is not None
        assert r["fields"] == ["title", "price"]
        assert r["content_type"] == "product"
        assert "title" in r["json_schema"]["properties"]

    def test_nested_fields_preserved(self):
        text = '{"content_type":"product","fields":[{"name":"variants","type":"array"},{"name":"title","type":"text"}],"json_schema":{"type":"object","properties":{"variants":{"type":"array","items":{"type":"object"}},"title":{"type":"string"}}},"required":["title"]}'
        r = _parse_llm_response(text)
        assert "variants" in r["fields"]

    def test_normalizes_names(self):
        text = '{"fields":[{"name":"My Field","type":"text"}],"json_schema":{"type":"object","properties":{"my_field":{"type":"string"}}}}'
        r = _parse_llm_response(text)
        assert r["fields"] == ["my_field"]

    def test_rebuilds_garbled_schema(self):
        text = '{"fields":[{"name":"title","type":"text","required":true}],"json_schema":"garbled"}'
        r = _parse_llm_response(text)
        assert r is not None
        assert "title" in r["json_schema"]["properties"]

    def test_garbage_returns_none(self):
        assert _parse_llm_response("not json at all") is None
        assert _parse_llm_response('{"fields":[]}') is None
        assert _parse_llm_response("") is None

    def test_fenced_json(self):
        text = '```json\n{"fields":[{"name":"title","type":"text"}],"json_schema":{"type":"object","properties":{"title":{"type":"string"}}}}\n```'
        r = _parse_llm_response(text)
        assert r is not None
        assert r["fields"] == ["title"]


class TestBuildFlatSchema:
    def test_basic(self):
        fields = [{"name": "title", "type": "text", "required": True},
                  {"name": "price", "type": "number", "required": False}]
        schema = _build_flat_schema(fields)
        assert schema["type"] == "object"
        assert set(schema["properties"].keys()) == {"title", "price"}
        assert schema["properties"]["price"]["type"] == "number"
        assert schema["required"] == ["title"]


class TestFallbackJsonld:
    def test_product_jsonld(self):
        blocks = [{"@type": "Product", "name": "Test", "offers": {"price": "9.99"}}]
        r = _fallback_jsonld(blocks)
        assert r["source"] == "jsonld"
        assert r["content_type"] == "product"
        assert "title" in r["fields"]
        assert "price" in r["fields"]

    def test_job_jsonld(self):
        blocks = [{"@type": "JobPosting", "title": "Dev", "hiringOrganization": {"name": "Corp"}}]
        r = _fallback_jsonld(blocks)
        assert r["content_type"] == "job_posting"
        assert len(r["fields"]) > 0

    def test_no_match(self):
        blocks = [{"@type": "BreadcrumbList", "itemListElement": []}]
        r = _fallback_jsonld(blocks)
        assert r["fields"] == []
        assert r["source"] == "jsonld"

    def test_empty(self):
        r = _fallback_jsonld([])
        assert r["fields"] == []
