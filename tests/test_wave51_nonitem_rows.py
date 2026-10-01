"""[wave-51 T9] Root-only / fragment-only row URLs are never items.

Job 499 (briscoes, search_term "air fryer") shipped COMPLETED with one row:
``{"title": "Briscoes | Homeware & Home Accessories", "price": 399.0,
"url": "https://www.briscoes.co.nz/#main"}`` — the rendered page SHELL. The
row carries title+price, so every ground-truth counter read it as data:

  1. ``_scraper_has_real_items`` (route_after_testing) counted it (and the
     20 stale-seed rows) toward the GROUND-TRUTH OVERRIDE that overrode the
     tester's FAIL verdict;
  2. run_execution's F9 saw a row with a core field and passed it.

Two wirings, one predicate (``src.discovery_quality.row_url_is_item_shaped``):
a row whose URL has no path (``/#main``, bare origin) can never be an item
on any site. The floor is deliberately narrow — ``/new-in/?sort=…`` still
passes (deeper junk shapes are other gates' problem).
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

from src.discovery_quality import row_url_is_item_shaped  # noqa: E402
from webapp.agents.nodes.run_execution import _nonitem_row_gate  # noqa: E402

import importlib  # noqa: E402

# The nodes package re-exports a NODE FUNCTION named route_after_testing, so
# a plain `import ... as` binds the function, not the module.
_rat_mod = importlib.import_module("webapp.agents.nodes.route_after_testing")


class TestRowUrlIsItemShaped:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://www.briscoes.co.nz/#main", False),
            ("https://www.briscoes.co.nz/", False),
            ("https://www.briscoes.co.nz", False),
            ("#main", False),
            ("", False),
            (None, False),
            # Anything with a real path passes — including deeper junk
            # shapes other gates own.
            ("https://www.briscoes.co.nz/product/1136060/russell-hobbs-air-fryer/", True),
            ("https://shop.com/new-in/?sort=NEW_ARRIVAL_DESC", True),
            ("https://shop.com/search?q=air+fryer", True),
            ("https://shop.com/p/12345?variant=9", True),
        ],
    )
    def test_floor(self, url, expected):
        assert row_url_is_item_shaped(url) is expected


def _write_output(tmp_path, rows, key="products"):
    p = tmp_path / "output_test.json"
    p.write_text(json.dumps({key: rows}), encoding="utf-8")
    return str(p)


def _row(url):
    return {
        "title": "Briscoes | Homeware & Home Accessories",
        "price": 399.0,
        "url": url,
        "src_url": "https://www.briscoes.co.nz/search/?q=air fryer",
    }


class TestNonitemRowGate:
    def test_all_junk_fires(self, tmp_path):
        out = _write_output(tmp_path, [_row("https://www.briscoes.co.nz/#main")])
        msg = _nonitem_row_gate(out, "search_term")
        assert "#main" in msg or "no path" in msg.lower()

    def test_majority_junk_fires(self, tmp_path):
        out = _write_output(
            tmp_path,
            [
                _row("https://www.briscoes.co.nz/#main"),
                _row("https://www.briscoes.co.nz/"),
                {"title": "Real Fryer", "price": 199.0,
                 "url": "https://www.briscoes.co.nz/product/1/fryer/"},
            ],
        )
        assert _nonitem_row_gate(out, "search_term")

    def test_minority_junk_passes(self, tmp_path):
        """A mostly-real run with one shell row is not this gate's verdict —
        failing real yield over one junk row would punish extraction."""
        rows = [
            {"title": f"Fryer {i}", "price": 100.0 + i,
             "url": f"https://www.briscoes.co.nz/product/{i}/fryer-{i}/"}
            for i in range(4)
        ]
        rows.append(_row("https://www.briscoes.co.nz/#main"))
        out = _write_output(tmp_path, rows)
        assert _nonitem_row_gate(out, "search_term") == ""

    def test_url_list_never_fires(self, tmp_path):
        """url_list seeds are user-provided — the gate never second-guesses
        them (same scoping rule as T5e)."""
        out = _write_output(tmp_path, [_row("https://www.briscoes.co.nz/#main")])
        assert _nonitem_row_gate(out, "url_list") == ""

    def test_empty_output_noops(self, tmp_path):
        out = _write_output(tmp_path, [])
        assert _nonitem_row_gate(out, "search_term") == ""

    def test_unreadable_file_noops(self, tmp_path):
        assert _nonitem_row_gate(str(tmp_path / "missing.json"), "search_term") == ""

    def test_legacy_product_url_key_is_judged(self, tmp_path):
        """Pre-rename rows carry ``product_url`` — the gate must see them."""
        out = _write_output(
            tmp_path,
            [{"title": "Shell", "price": 1.0,
              "product_url": "https://shop.example/#main"}],
        )
        assert _nonitem_row_gate(out, "navigation")


class TestGroundTruthCountingUsesTheFloor:
    """The override counter must not count shell rows as real items."""

    def test_scraper_has_real_items_rejects_shell_rows(self, tmp_path, monkeypatch):
        state = {
            "site_slug": "briscoes-co-nz",
            "input_mode": "search_term",
            "content_type_config": {"content_type": "product",
                                    "output_key": "products"},
            "test_report": {},
        }
        # Put the rows in the report — the FIRST source ground truth checks
        # (no workspace scan involved).
        state["test_report"]["sample_products"] = [
            _row("https://www.briscoes.co.nz/#main"),
            _row("https://www.briscoes.co.nz/"),
            _row("https://www.briscoes.co.nz/"),
        ]
        assert _rat_mod._scraper_has_real_items(state, min_count=3) is False

    def test_real_rows_still_count(self):
        state = {
            "site_slug": "x",
            "input_mode": "search_term",
            "content_type_config": {"content_type": "product",
                                    "output_key": "products"},
            "test_report": {
                "sample_products": [
                    {"title": "Fryer", "price": 199.0,
                     "url": "https://shop.example/product/1/fryer/"},
                    {"title": "Shaver", "price": 49.0,
                     "url": "https://shop.example/product/2/shaver/"},
                    {"title": "Vacuum", "price": 299.0,
                     "url": "https://shop.example/product/3/vacuum/"},
                ]
            },
        }
        assert _rat_mod._scraper_has_real_items(state, min_count=3) is True

    def test_run_execution_wires_the_gate_after_t5e(self):
        src = (
            ROOT / "webapp" / "agents" / "nodes" / "run_execution.py"
        ).read_text(encoding="utf-8")
        t5e_at = src.index("_sh = _shallow_section_gate(")
        gate_at = src.index("_ni = _nonitem_row_gate(")
        assert t5e_at < gate_at
