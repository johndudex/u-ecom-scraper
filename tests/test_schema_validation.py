"""Unit tests for src/schema_validation.py (pure Python — no Django)."""

import os
import sys

# Make `src` importable regardless of cwd (matches the repo's top-level tests/ layout).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.schema_validation import (
    MAX_SCHEMA_BYTES,
    validate_user_schema,
)


def _codes(result):
    return [i.code for i in result.issues]


class TestDialects:
    def test_standard_json_schema(self):
        r = validate_user_schema('{"type":"object","properties":{"title":{"type":"string"},"price":{"type":"number"}}}')
        assert r.valid
        assert r.shape == "standard"
        assert r.derived_fields == ["title", "price"]
        assert r.detected_content_type == "product"
        assert r.normalized["fields"] == [{"name": "title"}, {"name": "price"}]

    def test_internal_shape(self):
        r = validate_user_schema('{"content_type":"product","fields":[{"name":"title"},{"name":"price"}]}')
        assert r.valid and r.shape == "internal"
        assert r.derived_fields == ["title", "price"]

    def test_bare_array_of_names(self):
        r = validate_user_schema('["title","price","sku"]')
        assert r.valid and r.shape == "array"
        assert r.derived_fields == ["title", "price", "sku"]

    def test_flat_map(self):
        r = validate_user_schema('{"title":"string","price":"number"}')
        assert r.valid and r.shape == "flat_map"
        assert r.derived_fields == ["title", "price"]

    def test_bare_array_of_field_dicts(self):
        r = validate_user_schema('[{"name":"title"},{"name":"price"}]')
        assert r.valid and r.derived_fields == ["title", "price"]

    def test_already_parsed_dict_accepted(self):
        r = validate_user_schema({"type": "object", "properties": {"a": {"type": "string"}}})
        assert r.valid and r.derived_fields == ["a"]


class TestStructuralErrors:
    def test_empty(self):
        r = validate_user_schema("   ")
        assert not r.valid and "EMPTY" in _codes(r)

    def test_invalid_json_reports_line_col(self):
        r = validate_user_schema('{not valid json')
        assert not r.valid and "INVALID_JSON" in _codes(r)
        assert "line 1" in r.issues[0].message and "column" in r.issues[0].message

    def test_non_object_string(self):
        r = validate_user_schema('"just a string"')
        assert not r.valid and "NOT_OBJECT" in _codes(r)

    def test_non_object_number(self):
        r = validate_user_schema("42")
        assert not r.valid and "NOT_OBJECT" in _codes(r)

    def test_too_large(self):
        # Build a valid-but-huge JSON object over the byte cap.
        blob = "{" + ", ".join(f'"f{i}": "x"' for i in range(MAX_SCHEMA_BYTES // 6)) + "}"
        assert len(blob) > MAX_SCHEMA_BYTES
        r = validate_user_schema(blob)
        assert not r.valid and "TOO_LARGE" in _codes(r)

    def test_recursion_overflow(self, monkeypatch):
        # A pathologically deep payload overflows json.loads → RecursionError,
        # which the validator must catch as TOO_DEEP (not crash). Patch json.loads
        # so the test is robust across CPython builds rather than depending on the
        # exact depth that trips the C scanner.
        import src.schema_validation as sv

        def boom(_):
            raise RecursionError()

        monkeypatch.setattr(sv.json, "loads", boom)
        r = validate_user_schema('{"a": 1}')  # a string → goes through _parse_json
        assert not r.valid and "TOO_DEEP" in _codes(r)


class TestSemanticErrors:
    def test_duplicate_field(self):
        r = validate_user_schema('["title","title"]')
        assert not r.valid and "DUP_PROP" in _codes(r)
        assert r.derived_fields == ["title"]  # de-duped

    def test_remote_ref_rejected(self):
        r = validate_user_schema('{"type":"object","properties":{"a":{"$ref":"https://evil.com/x.json"}}}')
        assert not r.valid and "REMOTE_REF" in _codes(r)
        assert _codes(r).count("REMOTE_REF") == 1  # no duplicate

    def test_local_ref_allowed(self):
        r = validate_user_schema('{"type":"object","properties":{"a":{"$ref":"#/$defs/A"}},"$defs":{"A":{"type":"string"}}}')
        assert r.valid and "REMOTE_REF" not in _codes(r)

    def test_nested_too_deep(self):
        doc = {"type": "object", "properties": {}}
        cur = doc
        for _ in range(6):  # 6 levels of object-with-properties (> MAX_NESTING_DEPTH=5)
            cur["properties"] = {"n": {"type": "object", "properties": {}}}
            cur = cur["properties"]["n"]
        r = validate_user_schema(doc)
        assert not r.valid and "NESTED_TOO_DEEP" in _codes(r)

    def test_unsupported_internal_type(self):
        r = validate_user_schema('{"fields":[{"name":"x","type":"geometry"}]}')
        assert not r.valid and "UNSUPPORTED_TYPE" in _codes(r)

    def test_internal_missing_name(self):
        r = validate_user_schema('{"fields":[{"type":"text"}]}')
        assert not r.valid and "INTERNAL_BAD_FIELD" in _codes(r)

    def test_no_fields(self):
        r = validate_user_schema('{"type":"object","properties":{}}')
        assert not r.valid and "MUST_BE_OBJECT" in _codes(r)

    def test_name_with_spaces_is_warning_not_error(self):
        r = validate_user_schema('["title","my price"]')
        assert r.valid  # warning only — proceeds
        assert ("WARN_PROP_CHARS", "warning") in [(i.code, i.severity) for i in r.issues]


class TestRoundTrip:
    def test_normalized_feeds_schema_field_names(self):
        from src.content_types import schema_field_names

        r = validate_user_schema('{"type":"object","properties":{"title":{"type":"string"},"price":{"type":"number"},"brand":{"type":"string"}}}')
        assert r.valid
        # The pipeline's own reader accepts the normalized shape.
        assert schema_field_names(None, r.normalized) == ["title", "price", "brand"]

    def test_detected_content_type_needs_two_matches(self):
        # A single matching field is not enough to claim a content type.
        r = validate_user_schema('["title"]')
        assert r.detected_content_type is None
