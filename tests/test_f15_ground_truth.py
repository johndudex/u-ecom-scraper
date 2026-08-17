"""F15: the ground-truth override must not bless output whose items carry no
core field. Job 337 shipped 36 rows whose only populated field was ``brand``
(price/availability/original_price all empty) with a FAIL/0.35 test report —
the override + OUTPUT-AS-TRUTH rescue both passed it because
``any(core) OR has_substantive_field`` let brand-only rows count.

Pure-python: exercises _scraper_has_real_items via source extraction (its
module imports langgraph types; the function itself only needs stdlib +
src.content_types, both importable from the repo root).
"""
from __future__ import annotations

import os
import re
import sys
import json
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_fn():
    """Extract _scraper_has_real_items + _is_dead_product from the node module
    by executing only the needed defs in a namespace with stubbed imports."""
    src = open(os.path.join(ROOT, "webapp/agents/nodes/route_after_testing.py")).read()

    def grab(name):
        m = re.search(rf"^def {name}\(.*?(?=^def |\Z)", src, re.M | re.S)
        assert m, f"{name} not found"
        return m.group(0)

    fn_src = grab("_is_dead_product") + grab("_scraper_has_real_items")

    import logging
    from src.content_types import output_filter_fields, has_substantive_field

    # minimal stubs for the module-level names the two fns reference
    DEAD_STATUS_CODES = {"out of stock", "sold out", "unavailable", "discontinued"}
    SOFT_404_MARKERS = (
        "soft 404", "product not found", "no longer available",
        "discontinued", "not a product page",
    )

    ns = {
        "logging": logging,
        "logger": logging.getLogger("t.f15"),
        "output_filter_fields": output_filter_fields,
        "has_substantive_field": has_substantive_field,
        "ScrapeState": dict,
        "DEAD_STATUS_CODES": DEAD_STATUS_CODES,
        "SOFT_404_MARKERS": SOFT_404_MARKERS,
        "__name__": "t_f15",
    }
    exec(fn_src, ns)
    return ns["_scraper_has_real_items"]


class TestScraperHasRealItems:
    def setup_method(self):
        self.fn = _load_fn()

    def _state(self, items, slug=None):
        st = {
            "content_type_config": {"content_type": "product"},
            "page_type": "product",
            "site_slug": slug or "",
            "test_report": {"sample_products": items},
        }
        return st

    def test_brand_only_rows_do_not_count(self):
        # job 337 shape: 36 rows, only brand populated
        rows = [{"title": f"Dynamite tee {i}", "brand": "Dynamite",
                 "price": "", "availability": "", "original_price": ""} for i in range(36)]
        assert self.fn(self._state(rows), min_count=3) is False

    def test_priced_rows_count(self):
        rows = [{"title": f"tee {i}", "price": "$20.00", "availability": "In Stock"}
                for i in range(5)]
        assert self.fn(self._state(rows), min_count=3) is True

    def test_availability_only_counts(self):
        # any() semantics: one core field is enough (320-class safety)
        rows = [{"title": f"tee {i}", "availability": "In Stock"} for i in range(5)]
        assert self.fn(self._state(rows), min_count=3) is True

    def test_title_only_rows_do_not_count(self):
        # has_substantive_field escape must NOT fire when filter fields exist
        rows = [{"title": f"tee {i}"} for i in range(10)]
        assert self.fn(self._state(rows), min_count=3) is False

    def test_unknown_content_type_keeps_old_behavior(self):
        # No filter fields → has_substantive_field fallback. NOTE: the REAL
        # has_substantive_field deliberately excludes title from substantive
        # fields (soft-404 pages still carry titles — see its docstring), so
        # title-only rows do NOT count here either; a brand-only row does.
        # (The earlier "title counts" expectation only held under the
        # degraded-WSL runner where the src import fell back to a title lambda.)
        rows = [{"title": f"item {i}", "brand": "Acme"} for i in range(5)]
        st = self._state(rows)
        st["content_type_config"] = {"content_type": ""}  # unknown → no fields
        assert self.fn(st, min_count=3) is True
        title_only = [{"title": f"tee {i}"} for i in range(5)]
        st2 = self._state(title_only)
        st2["content_type_config"] = {"content_type": ""}
        assert self.fn(st2, min_count=3) is False


class TestOutputAsTruthFallback:
    """The OUTPUT-AS-TRUTH file rescue must apply the same core-field rule."""

    def setup_method(self):
        self.fn = _load_fn()

    def test_brand_only_output_file_does_not_rescue(self):
        # Single bad file ONLY — with a good file also present, rescuing from
        # the good one is correct behavior (not the 337 scenario).
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            ws = os.path.join(td, "workspace", "x-com")
            os.makedirs(ws)
            rows = [{"title": f"t{i}", "brand": "Dynamite", "price": ""} for i in range(10)]
            json.dump({"products": rows}, open(os.path.join(ws, "output_1.json"), "w"))
            st = {
                "content_type_config": {"content_type": "product"},
                "page_type": "product",
                "site_slug": "x-com",
                "test_report": {"sample_products": []},  # tester saw nothing
            }
            import unittest.mock as mk
            with mk.patch.dict(os.environ, {"PROJECT_ROOT": td}):
                assert self.fn(st, min_count=3) is False

    def test_priced_output_file_rescues(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            ws = os.path.join(td, "workspace", "x-com")
            os.makedirs(ws)
            rows = [{"title": f"t{i}", "price": "$9.99"} for i in range(5)]
            json.dump({"products": rows}, open(os.path.join(ws, "output_1.json"), "w"))
            st = {
                "content_type_config": {"content_type": "product"},
                "page_type": "product",
                "site_slug": "x-com",
                "test_report": {"sample_products": []},
            }
            import unittest.mock as mk
            with mk.patch.dict(os.environ, {"PROJECT_ROOT": td}):
                assert self.fn(st, min_count=3) is True



class TestOverrideGateSite:
    """Static assertions on the route_after_testing override condition."""

    def test_override_condition_includes_missing_core(self):
        src = open(os.path.join(ROOT, "webapp/agents/nodes/route_after_testing.py")).read()
        assert "if not _cov_reason and not missing_core and _scraper_has_real_items(state, min_count=3):" in src
