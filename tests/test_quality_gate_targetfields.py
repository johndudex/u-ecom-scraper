"""F9 quality gate vs user-requested field sets (job-9 Priceline case).

Prod job 9 (priceline.com.au): user requested {previous_price, current_price,
description, ratings} on a product page. Scraper honored it perfectly —
3616/3616 records carry all four. But the gate judged against the DEFAULT
core list (price/availability/...), where `current_price` doesn't match
`price` and nothing else overlaps → 0 good, 3616 core-less → FAILED with
"extraction quality gate". A perfect extraction was failed.

Locks:
- gate with target_fields: items carrying >=1 REQUESTED field are good
- alias names count (current_price ≈ price family) even without target_fields
- default behavior unchanged when no target_fields (regression guard)
- the priceline shape verbatim: 3616 records, requested-only fields → PASS
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

from agents.nodes.run_execution import _extraction_quality_gate  # noqa: E402


def _write_output(tmp_path, records, failed=0):
    p = tmp_path / "output_x.json"
    p.write_text(json.dumps({
        "site": "t", "products": records,
        "metadata": {"failed_products": failed},
    }))
    return str(p)


PRICELINE_RECORD = {
    "id": 1,
    "url": "https://x.example/p/1",
    "src_url": "https://x.example/c",
    "status_code": 200,
    "scraped_at": "2026-08-26T00:00:00+00:00",
    "remarks": "",
    "current_price": "$15.99",
    "previous_price": "",
    "description": "A balanced blend of things.",
    "ratings": "0.0",
}


class TestGateWithTargetFields:
    def test_priceline_shape_passes_with_requested_fields(self, tmp_path):
        p = _write_output(tmp_path, [dict(PRICELINE_RECORD, id=i) for i in range(10)])
        msg = _extraction_quality_gate(
            p, "navigation", 10,
            target_fields=["previous_price", "current_price", "description", "ratings"],
        )
        assert msg == "", f"perfect extraction failed: {msg}"

    def test_requested_fields_missing_still_fails(self, tmp_path):
        # records with NONE of the requested fields → still core-less → gate fires
        bad = [{k: v for k, v in PRICELINE_RECORD.items()
                if k in ("id", "url", "src_url", "status_code", "scraped_at", "remarks")}
               for i in range(10)]
        p = _write_output(tmp_path, bad)
        msg = _extraction_quality_gate(
            p, "navigation", 0,
            target_fields=["previous_price", "current_price", "description", "ratings"],
        )
        assert "quality gate" in msg

    def test_default_path_regression_unaffected(self, tmp_path):
        # no target_fields → registry/alias core list still judges. Records
        # with NO core-ish field (prices stripped; description+ratings are
        # not core) must still count core-less → gate fires. (The legacy
        # expectation that current_price didn't count is the job-9 bug.)
        stripped = [
            {k: v for k, v in PRICELINE_RECORD.items()
             if k not in ("current_price", "previous_price")}
            for i in range(10)
        ]
        p = _write_output(tmp_path, stripped)
        msg = _extraction_quality_gate(p, "navigation", 0)
        assert "quality gate" in msg


class TestAliasCoreNames:
    def test_current_price_counts_as_price_family(self, tmp_path):
        # even WITHOUT target_fields, price-alias names should count as good
        # (current_price/previous_price/list_price/ sale_price)
        p = _write_output(tmp_path, [dict(PRICELINE_RECORD, id=i) for i in range(10)])
        msg = _extraction_quality_gate(p, "navigation", 0)
        assert msg == "", (
            "price-alias fields (current_price) should satisfy the core "
            f"predicate: {msg}"
        )
