"""[wave-56b F4b] The zero-yield verdict must key on ROW COUNT, not on URL
harvestability.

Deep-audit proof (2026-10-09): ``_output_row_urls`` returns [] both for
zero rows AND for rows carrying none of ``_ROW_URL_KEYS`` — so a url_list
run that SHIPPED real rows (title/price only, or an ``href`` key the
harvest does not know) is FAILED with "0 of N submitted seeds produced
rows", a false assertion on a delivering run. Rows without a harvestable
key make the C2 SCOPE check unverifiable, not the run empty.
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

SEEDS = [
    "https://books-toscrape.com/catalogue/a-light-in-the-attic_1000/index.html",
    "https://books-toscrape.com/catalogue/tipping-the-velvet_999/index.html",
]


def _write(tmp_path, payload):
    p = tmp_path / "output_test.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return str(p)


class TestRowsWithoutUrlKeys:
    def test_urlless_rows_are_not_zero_yield(self, tmp_path):
        from agents.nodes.run_execution import _url_list_scope_gate

        out = _write(tmp_path, {
            "products": [
                {"title": "A Light in the Attic", "price": "51.77"},
                {"title": "Tipping the Velvet", "price": "53.74"},
            ],
        })
        assert _url_list_scope_gate(out, SEEDS, "url_list") == "", (
            "rows shipped but unharvestable for scope: not a zero-yield lie"
        )

    def test_zero_rows_still_fire_the_verdict(self, tmp_path):
        from agents.nodes.run_execution import _url_list_scope_gate

        out = _write(tmp_path, {"products": [], "failed_products": 11})
        diag = _url_list_scope_gate(out, SEEDS, "url_list")
        assert diag and "0 of 2" in diag

    def test_off_seed_strays_still_fire(self, tmp_path):
        from agents.nodes.run_execution import _url_list_scope_gate

        out = _write(tmp_path, {"products": [
            {"url": "https://books-toscrape.com/catalogue/stray_1/index.html",
             "title": "Stray"},
        ]})
        diag = _url_list_scope_gate(out, SEEDS, "url_list")
        assert diag and "scope violation" in diag

    def test_on_seed_rows_still_pass(self, tmp_path):
        from agents.nodes.run_execution import _url_list_scope_gate

        out = _write(tmp_path, {"products": [
            {"url": SEEDS[0], "title": "A"},
            {"url": SEEDS[1], "title": "B"},
        ]})
        assert _url_list_scope_gate(out, SEEDS, "url_list") == ""
