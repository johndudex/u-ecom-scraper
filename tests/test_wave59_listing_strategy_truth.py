"""[wave-59 C6] Listing-shaped url_list seeds walk — including http_requests.

The listing-seed arm (wave-58 1c) rewrote api/internal_api picks to
http_navigation so the scraper WALKS the listing pages the user provided.
R2 showed the missed class: a bare ``http_requests`` pick on listing seeds
single-phase-fetches the LISTING page — its env-gated two-phase branch never
fired in prod, and item discovery off a category page needs the two-phase
machinery http_navigation carries. The widen is LOCAL (API_STRATEGIES stays
``{"api", "internal_api"}`` — template-family flag maps and the escalation
ladder must not see http_requests reclassified globally) and stamped
(``listing_seed_reason``) so the artifact says WHY it walks.

Item-shaped seed sets — the url_list contract — are never touched.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

from agents.graph import _apply_listing_seed_strategy  # noqa: E402

SEEDS = [
    "https://www.jjshouse.com/all/bridesmaid-dresses",
    "https://www.jjshouse.com/collection/girls-dresses",
    "https://www.jjshouse.com/shop/accessories-shoes",
]
ITEMS = [
    {"url": "https://www.jjshouse.com/product-123", "title": "A dress"},
]
LISTING_STATE = {
    "input_mode": "url_list",
    "input_urls": SEEDS,
    "url": "https://www.jjshouse.com/x",
}


class TestHttpRequestsWalks:
    def test_http_requests_listing_seed_walks(self):
        out = _apply_listing_seed_strategy(
            {"strategy": "http_requests"}, LISTING_STATE
        )
        assert out["strategy"] == "http_navigation"
        assert out.get("listing_seed_discovery") is True

    def test_reason_stamp_records_the_origin(self):
        out = _apply_listing_seed_strategy(
            {"strategy": "http_requests"}, LISTING_STATE
        )
        reason = out.get("listing_seed_reason") or ""
        assert "http_requests" in reason
        assert "http_navigation" in reason
        assert "listing" in reason.lower()

    def test_api_pick_reason_stamp_too(self):
        out = _apply_listing_seed_strategy(
            {"strategy": "internal_api"}, LISTING_STATE
        )
        assert out["strategy"] == "http_navigation"
        assert "internal_api" in (out.get("listing_seed_reason") or "")

    def test_alias_fields_widened_for_http_requests(self):
        out = _apply_listing_seed_strategy({
            "strategy": "http_requests",
            "scraping_method": "http_requests",
            "recommended_strategy": "http_requests",
        }, LISTING_STATE)
        assert out["scraping_method"] == "http_navigation"
        assert out["recommended_strategy"] == "http_navigation"

    def test_api_strategies_constant_untouched(self):
        from agents.constants import API_STRATEGIES

        assert API_STRATEGIES == frozenset({"api", "internal_api"}), (
            "the widen is local to the listing-seed arm — a global "
            "reclassification would move template-family flags and the "
            "escalation ladder"
        )


class TestUntouchedShapes:
    def test_http_requests_outside_listing_seeds(self):
        for mode in ("search_term", "navigation", "list_page"):
            out = _apply_listing_seed_strategy(
                {"strategy": "http_requests"}, {**LISTING_STATE, "input_mode": mode}
            )
            assert out["strategy"] == "http_requests", mode

    def test_item_shaped_seeds_never_touched(self):
        for strategy in ("http_requests", "internal_api"):
            out = _apply_listing_seed_strategy(
                {"strategy": strategy},
                {**LISTING_STATE, "input_urls": ITEMS},
            )
            assert out["strategy"] == strategy, strategy

    def test_browser_lane_untouched(self):
        out = _apply_listing_seed_strategy(
            {"strategy": "playwright"}, LISTING_STATE
        )
        assert out["strategy"] == "playwright"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
