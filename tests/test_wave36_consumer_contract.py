"""[wave-36 Fix 1a] Consumer contract — resolved_fields across the pipeline.

Locks (docs/plans/wave36-field-mapping-plan.md §1a):
- normalize_fields prunes to resolved ∪ raw ∪ DIRECT (two-vocabulary, B1)
- run_execution passes the UNION at the in-process dispatch, prune and
  quality-gate seams (B2 caller rows)
- route_after_testing judges rows against the UNION
- check_tracker compares resolved-vs-resolved (skip economics survive a
  mapped re-drive; legacy prior jobs fall back to raw-vs-raw)
- finalize reads job.field_mapping: rename raw→resolved THEN prune to
  resolved-only (identity-safe when no mapping exists — legacy byte-compat)
"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

from src.field_mapping import (  # noqa: E402
    rename_map_from_mapping,
    resolved_fields_from_mapping,
)

MAPPING = {
    "product name": {"target": "title", "confidence": 0.97,
                     "rationale": "alias", "source": "alias"},
    "avaliability": {"target": "availability", "confidence": 0.97,
                     "rationale": "alias", "source": "alias"},
    "rrp": {"target": "original_price", "confidence": 0.9,
            "rationale": "alias", "source": "alias"},
    "sku code": {"target": "CUSTOM", "confidence": 0.9,
                 "rationale": "domain term", "source": "llm"},
}


class TestMappingHelpers:
    def test_resolved_fields_ordered_canonical(self):
        got = resolved_fields_from_mapping(MAPPING)
        assert got == ["title", "availability", "original_price", "sku_code"]

    def test_resolved_fields_none_when_absent(self):
        assert resolved_fields_from_mapping(None) is None
        assert resolved_fields_from_mapping({}) is None

    def test_rename_map_only_changed_keys(self):
        got = rename_map_from_mapping(MAPPING)
        assert got == {
            "product name": "title",
            "avaliability": "availability",
            "rrp": "original_price",
            "sku code": "sku_code",  # CUSTOM: sanitized key
        }

    def test_rename_map_identity_target_not_included(self):
        m = {"price": {"target": "price", "confidence": 1.0,
                       "rationale": "verbatim", "source": "verbatim"}}
        assert rename_map_from_mapping(m) == {}


class TestRenameOutputKeys:
    def _write(self, tmp_path, data):
        path = tmp_path / "out.json"
        path.write_text(json.dumps(data))
        return str(path)

    def test_records_renamed(self, tmp_path):
        from scraper.tasks import _rename_output_keys

        path = self._write(tmp_path, {
            "products": [
                {"product name": "Chair", "avaliability": "InStock",
                 "sku code": "X1", "price": 10},
                {"product name": "Lamp", "price": 5},
            ],
            "metadata": {"discovered_urls": 2},
        })
        assert _rename_output_keys(path, MAPPING) is True
        data = json.load(open(path))
        recs = data["products"]
        assert recs[0]["title"] == "Chair"
        assert recs[0]["availability"] == "InStock"
        assert recs[0]["sku_code"] == "X1"
        assert recs[0]["price"] == 10  # unmapped keys untouched
        assert "product name" not in recs[0]
        assert data["metadata"] == {"discovered_urls": 2}

    def test_no_mapping_is_noop(self, tmp_path):
        from scraper.tasks import _rename_output_keys

        data = {"products": [{"product name": "Chair"}]}
        path = self._write(tmp_path, data)
        assert _rename_output_keys(path, None) is False
        assert json.load(open(path)) == data


class TestCheckTrackerResolvedDiff:
    """The pure comparison helper — no DB needed."""

    def _changed(self, state, prior):
        from types import SimpleNamespace

        from webapp.agents.nodes.check_tracker import _fields_changed

        return _fields_changed(state, SimpleNamespace(
            target_fields=prior.get("target_fields", []),
            field_mapping=prior.get("field_mapping"),
        ))

    def test_same_resolved_is_unchanged(self):
        state = {"target_fields": ["product name"], "resolved_fields": ["title"]}
        prior = {"target_fields": ["title"],
                 "field_mapping": {"resolved_fields": ["title"],
                                   "mapping": {}, "content_hash": "h"}}
        assert self._changed(state, prior) is False

    def test_resolved_diff_changed(self):
        state = {"target_fields": ["product name"],
                 "resolved_fields": ["title", "brand"]}
        prior = {"target_fields": ["title"],
                 "field_mapping": {"resolved_fields": ["title"],
                                   "mapping": {}, "content_hash": "h"}}
        assert self._changed(state, prior) is True

    def test_legacy_prior_falls_back_to_raw(self):
        state = {"target_fields": ["product name"], "resolved_fields": ["title"]}
        prior = {"target_fields": ["title"], "field_mapping": None}
        assert self._changed(state, prior) is True

    def test_legacy_pair_identical_raw_unchanged(self):
        state = {"target_fields": ["title"]}
        prior = {"target_fields": ["title"], "field_mapping": None}
        assert self._changed(state, prior) is False


class TestConsumerSeams:
    def test_normalize_prune_admits_resolved_and_raw(self):
        from webapp.agents.nodes.normalize_fields import _prune_to_schema

        state = {"target_fields": ["product name"],
                 "resolved_fields": ["title"]}
        merged = {"title": {"method": "x"}, "product name": {"method": "y"},
                  "brand": {"method": "z"}}
        out = _prune_to_schema(merged, state)
        assert "title" in out and "product name" in out
        assert "brand" not in out

    def test_run_execution_uses_union(self):
        src = open(os.path.join(
            ROOT, "webapp", "agents", "nodes", "run_execution.py"
        )).read()
        assert src.count("union_output_fields(") >= 4, (
            "run_execution must pass the two-vocabulary union at the dispatch/"
            "prune/quality-gate seams (round-2 B2 caller rows)"
        )

    def test_route_after_testing_uses_union(self):
        src = open(os.path.join(
            ROOT, "webapp", "agents", "nodes", "route_after_testing.py"
        )).read()
        assert "union_output_fields(" in src

    def test_finalize_uses_field_mapping(self):
        src = open(os.path.join(ROOT, "webapp", "scraper", "tasks.py")).read()
        assert "resolved_fields_from_mapping(" in src
        assert "_rename_output_keys(" in src


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
