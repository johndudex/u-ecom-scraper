"""Unit tests for nested-schema preservation:
  - parse_nested_schema (strict discriminator — the C2 BLOCKER)
  - prune_record_to_schema / _prune_value (type guards — C3, record count, bookkeeping)

Pure Python (src/schema_validation.py + src/content_types.py are stdlib-only).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.schema_validation import parse_nested_schema  # noqa: E402
from src.content_types import (  # noqa: E402
    BOOKKEEPING_FIELDS,
    _prune_value,
    prune_record_to_schema,
)


# ── parse_nested_schema: strict discriminator (C2) ─────────────────────────
class TestParseNestedDiscriminator:
    def test_nested_object_yields_tree(self):
        t = parse_nested_schema('{"type":"object","properties":{"address":{"type":"object","properties":{"city":{"type":"string"},"zip":{"type":"string"}}}}}')
        assert t is not None
        assert "address" in t
        assert set(t["address"]["children"]) == {"city", "zip"}

    def test_array_of_objects_yields_tree(self):
        t = parse_nested_schema('{"type":"object","properties":{"variants":{"type":"array","items":{"type":"object","properties":{"size":{"type":"string"},"color":{"type":"string"}}}}}}')
        assert t is not None
        assert t["variants"]["type"] == "list"
        assert set(t["variants"]["children"]) == {"size", "color"}

    def test_flat_standard_schema_returns_none(self):
        # ALL scalars → no nesting → None (flat path, byte-identical)
        assert parse_nested_schema('{"type":"object","properties":{"title":{"type":"string"},"price":{"type":"number"}}}') is None

    def test_array_of_scalars_returns_none(self):
        assert parse_nested_schema('{"type":"object","properties":{"tags":{"type":"array","items":{"type":"string"}}}}') is None

    def test_internal_shape_returns_none(self):
        assert parse_nested_schema('{"fields":[{"name":"title"}]}') is None

    def test_flat_map_returns_none(self):
        assert parse_nested_schema('{"title":"string","price":"number"}') is None

    def test_bare_array_returns_none(self):
        assert parse_nested_schema('["title","price"]') is None

    def test_object_without_properties_returns_none(self):
        assert parse_nested_schema('{"type":"object","properties":{"addr":{"type":"object"}}}') is None

    def test_invalid_json_returns_none(self):
        assert parse_nested_schema('{not valid') is None

    def test_empty_returns_none(self):
        assert parse_nested_schema('   ') is None

    def test_mixed_nested_and_flat_yields_tree(self):
        # title/price flat, address nested → tree (with address.children populated)
        t = parse_nested_schema('{"type":"object","properties":{"title":{"type":"string"},"address":{"type":"object","properties":{"city":{"type":"string"}}}}}')
        assert t is not None
        assert t["title"]["children"] == {}
        assert set(t["address"]["children"]) == {"city"}


# ── prune: correctness (C3 type guard, C4 allowed-authority, record count, bookkeeping) ─
class TestPrune:
    def _allowed(self, tree):
        return set(tree or {}) | set(BOOKKEEPING_FIELDS)

    def test_nested_object_inner_prune(self):
        tree = {"address": {"type": "object", "children": {"city": {"type": "text", "children": {}}, "zip": {"type": "text", "children": {}}}}}
        rec = {"address": {"city": "NYC", "zip": "10001", "country": "US"}, "url": "http://x"}
        out = prune_record_to_schema(rec, self._allowed(tree), tree)
        assert out["address"] == {"city": "NYC", "zip": "10001"}  # country dropped
        assert out["url"] == "http://x"  # bookkeeping kept

    def test_array_of_objects_inner_prune(self):
        tree = {"variants": {"type": "list", "children": {"size": {"type": "text", "children": {}}, "color": {"type": "text", "children": {}}}}}
        rec = {"variants": [{"size": "M", "color": "red", "price": "9"}, {"size": "L", "color": "blue", "price": "10"}]}
        out = prune_record_to_schema(rec, self._allowed(tree), tree)
        assert out["variants"] == [{"size": "M", "color": "red"}, {"size": "L", "color": "blue"}]  # price dropped per item; COUNT preserved

    def test_type_mismatch_no_crash(self):  # C3 — schema says object, value is scalar
        tree = {"address": {"type": "object", "children": {"city": {"type": "text", "children": {}}}}}
        rec = {"address": "N/A"}
        out = prune_record_to_schema(rec, self._allowed(tree), tree)  # must not raise
        assert out["address"] == "N/A"  # kept verbatim, no null-fill

    def test_type_mismatch_array_scalar(self):
        tree = {"variants": {"type": "list", "children": {"size": {"type": "text", "children": {}}}}}
        rec = {"variants": "none"}
        out = prune_record_to_schema(rec, self._allowed(tree), tree)
        assert out["variants"] == "none"  # not a list → kept verbatim

    def test_bookkeeping_survives_top_level(self):
        tree = {"title": {"type": "text", "children": {}}}
        rec = {"title": "T", "url": "u", "src_url": "s", "scraped_at": "t", "status_code": 200, "extra": "x"}
        out = prune_record_to_schema(rec, self._allowed(tree), tree)
        for b in ("url", "src_url", "scraped_at", "status_code"):
            assert b in out
        assert "extra" not in out

    def test_record_count_stable(self):
        tree = {"variants": {"type": "list", "children": {"size": {"type": "text", "children": {}}}}}
        recs = [{"variants": [{"size": "M", "color": "red"}]}, {"variants": []}, {"variants": [{"size": "L"}]}]
        pruned = [prune_record_to_schema(r, self._allowed(tree), tree) for r in recs]
        assert len(pruned) == 3  # no record dropped

    def test_no_allowed_no_prune(self):  # extract-everything / schema-less
        rec = {"title": "T", "anything": "x"}
        assert prune_record_to_schema(rec, None, {}) == rec
        assert prune_record_to_schema(rec, set(), {}) == rec

    def test_allowed_is_top_level_authority(self):  # C4: chip edits win over stale schema
        # schema still describes address, but user removed the address chip → allowed omits it
        tree = {"title": {"type": "text", "children": {}}, "address": {"type": "object", "children": {"city": {"type": "text", "children": {}}}}}
        allowed = {"title"} | set(BOOKKEEPING_FIELDS)  # address NOT in allowed
        rec = {"title": "T", "address": {"city": "NYC"}, "url": "u"}
        out = prune_record_to_schema(rec, allowed, tree)
        assert "address" not in out  # dropped — allowed (target_fields) wins over schema_nested

    def test_field_in_output_not_in_schema_dropped(self):
        tree = {"title": {"type": "text", "children": {}}}
        rec = {"title": "T", "rogue": "x", "url": "u"}
        out = prune_record_to_schema(rec, self._allowed(tree), tree)
        assert "rogue" not in out

    def test_field_in_schema_absent_in_output_no_null_fill(self):
        tree = {"title": {"type": "text", "children": {}}, "address": {"type": "object", "children": {"city": {"type": "text", "children": {}}}}}
        rec = {"title": "T", "url": "u"}  # no address
        out = prune_record_to_schema(rec, self._allowed(tree), tree)
        assert "address" not in out  # omitted, not null-filled

    def test_leaf_node_passes_through_opaque(self):
        node = {"type": "text", "children": {}}
        assert _prune_value({"anything": "x"}, node) == {"anything": "x"}
        assert _prune_value([1, 2, 3], node) == [1, 2, 3]

    def test_roundtrip_with_parse_nested_schema(self):
        raw = '{"type":"object","properties":{"title":{"type":"string"},"variants":{"type":"array","items":{"type":"object","properties":{"size":{"type":"string"},"color":{"type":"string"}}}}}}'
        tree = parse_nested_schema(raw)
        assert tree is not None
        rec = {"title": "T", "variants": [{"size": "M", "color": "red", "extra": "x"}], "url": "u", "rogue": "z"}
        out = prune_record_to_schema(rec, set(tree) | set(BOOKKEEPING_FIELDS), tree)
        assert out == {"title": "T", "variants": [{"size": "M", "color": "red"}], "url": "u"}
