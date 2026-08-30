"""Unit tests for the A3 price normalization and A4 seed-URL filter.

A3 (``normalize_output_prices``): generated scrapers legitimately emit
formatted price strings ("$17.00"); the deterministic checker parses them but
the tester's LLM condemns the same string as a WRONG_VALUE. Normalizing at
persist time removes the disagreement class. Only a field that is NOTHING BUT
a price (currency symbols/codes/separators around the number) is rewritten —
"From $10" and "Free" stay strings.

A4 (``_filter_seed_urls``): job 45 burned ~17 minutes of test cycles on a
seed list polluted with pagination/nav/off-domain links. The filter drops
them, with counts, before they reach the scraper.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webapp.agents.nodes.run_execution import normalize_output_prices
from webapp.agents.nodes.setup_workspace import _filter_seed_urls


def _write(tmp_path, data, name="output_1.json"):
    p = str(tmp_path / name)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return p


class TestNormalizeOutputPrices:
    def test_formatted_strings_become_numerics(self, tmp_path):
        p = _write(tmp_path, {"products": [
            {"title": "a", "price": "$17.00"},
            {"title": "b", "price": "£1,299.00"},
            {"title": "c", "current_price": "AUD 24.99"},
        ]})
        assert normalize_output_prices(p) == 3
        data = json.load(open(p))
        rows = data["products"]
        assert rows[0]["price"] == 17.0
        assert rows[1]["price"] == 1299.0
        assert rows[2]["current_price"] == 24.99
        assert all(isinstance(r["price"], float) for r in rows[:2])

    def test_prose_and_non_prices_stay_strings(self, tmp_path):
        p = _write(tmp_path, {"products": [
            {"title": "a", "price": "From $10"},
            {"title": "b", "price": "Free"},
            {"title": "$5 Special", "price": ""},
            {"title": "c", "price": 12.5},
        ]})
        assert normalize_output_prices(p) == 0
        rows = json.load(open(p))["products"]
        assert rows[0]["price"] == "From $10"
        assert rows[1]["price"] == "Free"
        assert rows[2]["price"] == ""
        assert rows[2]["title"] == "$5 Special"  # non-alias fields untouched
        assert rows[3]["price"] == 12.5  # already numeric

    def test_only_first_populated_item_key_is_scanned(self, tmp_path):
        p = _write(tmp_path, {
            "products": [{"price": "$1.00"}],
            "jobs": [{"price": "$2.00"}],
        })
        assert normalize_output_prices(p) == 1
        data = json.load(open(p))
        assert data["products"][0]["price"] == 1.0
        assert data["jobs"][0]["price"] == "$2.00"

    def test_idempotent(self, tmp_path):
        p = _write(tmp_path, {"products": [{"price": "$9.99"}]})
        assert normalize_output_prices(p) == 1
        assert normalize_output_prices(p) == 0

    def test_missing_or_invalid_file_returns_zero(self, tmp_path):
        assert normalize_output_prices(str(tmp_path / "nope.json")) == 0
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        assert normalize_output_prices(str(bad)) == 0


class TestFilterSeedUrls:
    JOB = "https://www.shop-com-au.com/"

    def test_keeps_same_domain_item_urls(self):
        urls = [
            "https://www.shop-com-au.com/p/1",
            "https://shop-com-au.com/p/2",  # bare host, same registrable
        ]
        assert _filter_seed_urls(urls, self.JOB) == urls

    def test_drops_non_http_and_pathless(self):
        urls = [
            "ftp://www.shop-com-au.com/p/1",
            "javascript:void(0)",
            "https://www.shop-com-au.com",
            "https://www.shop-com-au.com/",
        ]
        assert _filter_seed_urls(urls, self.JOB) == []

    def test_drops_off_domain(self):
        urls = [
            "https://evil.example.com/p/1",
            "https://cdn.partner.net/img/p/2",
        ]
        assert _filter_seed_urls(urls, self.JOB) == []

    def test_dedupes_first_wins(self):
        urls = [
            "https://www.shop-com-au.com/p/1",
            "https://www.shop-com-au.com/p/1",
            "https://www.shop-com-au.com/p/1?x=1",
        ]
        out = _filter_seed_urls(urls, self.JOB)
        assert out == [urls[0], urls[2]]  # ?x=1 is a distinct URL

    def test_two_part_tld_domains(self):
        job = "https://www.priceline.com.au/"
        keep = "https://www.priceline.com.au/p/1"
        drop = "https://other.com.au/p/2"
        assert _filter_seed_urls([keep, drop], job) == [keep]

    def test_blank_job_url_keeps_http_urls(self):
        urls = ["https://a.com/p/1", "https://b.com/p/2"]
        assert _filter_seed_urls(urls, "") == urls

    def test_blanks_and_garbage_skipped(self):
        urls = ["", "   ", None, "https://www.shop-com-au.com/p/1"]
        assert _filter_seed_urls(urls, self.JOB) == ["https://www.shop-com-au.com/p/1"]


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
