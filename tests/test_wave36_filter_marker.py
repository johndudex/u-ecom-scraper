"""[wave-36 Fix 1a] Hash-stamped output-filter marker — cut-then-insert re-patch
+ the two-vocabulary field union.

Round-2 M1: re-patching by presence-check alone double-injects (each injected
block carries its own predicate → both run → AND semantics → row loss, the
exact F2 class). The marker must carry a content stamp; a mismatch CUTS the
stale block before inserting. Legacy un-hashed markers count as mismatch;
ancient ``_OUTPUT_PRICE_FILTER_APPLIED`` blocks (unknown shape) keep the bail.

Round-2 B1 (TWO-VOCABULARY RULE): prod drafts emit BOTH canonical and
chip-verbatim record keys, so the in-draft filter predicate must be built from
resolved ∪ raw ∪ custom — union_output_fields() is that seam.
"""

from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402
from agents import graph as graph_mod  # noqa: E402

DRAFT = (
    "import json\n"
    "output = {'products': [{'title': 'a', 'price': '1'}, {'title': ''}]}\n"
    "json.dump(output, open('out.json', 'w'))\n"
)


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setattr(graph_mod, "_get_project_root", lambda: str(tmp_path))
    return graph_mod, tmp_path


def _write_draft(tmp_path, slug, text=DRAFT):
    slug_dir = tmp_path / "workspace" / slug
    slug_dir.mkdir(parents=True, exist_ok=True)
    path = slug_dir / "scraper_draft.py"
    path.write_text(text)
    return path


def _stamp_of(code):
    m = re.search(r"# _OUTPUT_FILTER_APPLIED:([0-9a-f]{12})", code)
    return m.group(1) if m else None


class TestStampedMarker:
    def test_fresh_draft_gets_stamped_marker(self, ws):
        graph_mod, tmp_path = ws
        draft = _write_draft(tmp_path, "fresh")
        graph_mod._patch_scraper_output_filter("fresh", "product")
        code = draft.read_text()
        assert _stamp_of(code), "marker comment carries no stamp"
        compile(code, "d.py", "exec")

    def test_same_stamp_is_idempotent(self, ws):
        graph_mod, tmp_path = ws
        draft = _write_draft(tmp_path, "idem")
        graph_mod._patch_scraper_output_filter("idem", "product")
        once = draft.read_text()
        graph_mod._patch_scraper_output_filter("idem", "product")
        assert draft.read_text() == once

    def test_changed_fields_repatch_cuts_stale_block(self, ws):
        """Different fields → new stamp → old block CUT (no double-inject)."""
        graph_mod, tmp_path = ws
        draft = _write_draft(tmp_path, "rep")
        graph_mod._patch_scraper_output_filter("rep", "product", ["price"])
        old = draft.read_text()
        assert old.count("_FILTER_FIELDS") == 1
        graph_mod._patch_scraper_output_filter(
            "rep", "product", ["price", "availability"]
        )
        new = draft.read_text()
        assert new.count("_FILTER_FIELDS") == 1, "stale block survived re-patch"
        assert new.count("# _OUTPUT_FILTER_APPLIED") == 1
        assert _stamp_of(new) != _stamp_of(old)
        compile(new, "d.py", "exec")
        # executing the re-patched draft must filter exactly once
        ns: dict = {}
        exec(compile(new, "d.py", "exec"), ns)  # noqa: S102
        assert len(ns["output"]["products"]) == 1

    def test_legacy_unhashed_marker_is_cut_and_reinserted(self, ws):
        graph_mod, tmp_path = ws
        draft = _write_draft(tmp_path, "legacy")
        graph_mod._patch_scraper_output_filter("legacy", "product")
        legacy = re.sub(
            r"# _OUTPUT_FILTER_APPLIED:[0-9a-f]{12}",
            "# _OUTPUT_FILTER_APPLIED",
            draft.read_text(),
        )
        draft.write_text(legacy)
        graph_mod._patch_scraper_output_filter(
            "legacy", "product", ["price", "availability"]
        )
        new = draft.read_text()
        assert new.count("# _OUTPUT_FILTER_APPLIED") == 1
        assert _stamp_of(new), "re-inserted block must carry the new stamp"

    def test_legacy_double_injection_both_cut(self, ws):
        """Two stale blocks (the F2 harm shape) → both cut, one inserted."""
        graph_mod, tmp_path = ws
        draft = _write_draft(tmp_path, "dbl")
        graph_mod._patch_scraper_output_filter("dbl", "product", ["price"])
        once = draft.read_text()
        block = re.search(
            r"# _OUTPUT_FILTER_APPLIED[^\n]*\n(?:.*?\n)*?except Exception:\n    pass\n\n",
            once,
        )
        assert block
        draft.write_text(DRAFT + "\n" + block.group(0) + block.group(0))
        graph_mod._patch_scraper_output_filter("dbl", "product", ["price"])
        new = draft.read_text()
        assert new.count("# _OUTPUT_FILTER_APPLIED") == 1
        compile(new, "d.py", "exec")

    def test_ancient_price_filter_block_still_bails(self, ws):
        graph_mod, tmp_path = ws
        draft = _write_draft(tmp_path, "ancient")
        draft.write_text(DRAFT.replace(
            "json.dump(output",
            "# _OUTPUT_PRICE_FILTER_APPLIED\njson.dump(output", 1,
        ))
        before = draft.read_text()
        graph_mod._patch_scraper_output_filter("ancient", "product")
        assert draft.read_text() == before


class TestUnionFields:
    def test_union_is_resolved_raw_custom_deduped(self):
        from src.field_mapping import union_output_fields

        state = {
            "target_fields": ["product name", "price"],
            "resolved_fields": ["title", "price", "brand"],
            "field_mapping": {
                "sku code": {"target": "CUSTOM", "confidence": 0.9,
                             "rationale": "x", "source": "llm"},
            },
        }
        got = union_output_fields(state)
        assert got[:4] == ["title", "price", "brand", "product name"]
        assert "sku_code" in got  # CUSTOM keys pass (sanitized to key charset)
        assert len(got) == len(set(got))

    def test_union_falls_back_to_raw(self):
        from src.field_mapping import union_output_fields

        assert union_output_fields({"target_fields": ["title"]}) == ["title"]
        assert union_output_fields({}) == []


class TestKeepRawReaders:
    """Round-2 B2: these readers MUST stay on raw chips (page-derived names /
    user-facing chip sets) — migrating them breaks coverage credit + interrupt
    payloads."""

    def test_validate_coverage_stays_raw(self):
        path = os.path.join(
            ROOT, "webapp", "agents", "nodes", "validate_coverage.py"
        )
        src = open(path).read()
        assert 'state.get("target_fields")' in src or (
            'state["target_fields"]' in src
        ), "validate_coverage no longer reads raw target_fields — verify B2"

    def test_field_confirmation_stays_raw(self):
        path = os.path.join(
            ROOT, "webapp", "agents", "nodes", "field_confirmation.py"
        )
        src = open(path).read()
        assert "target_fields" in src


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
