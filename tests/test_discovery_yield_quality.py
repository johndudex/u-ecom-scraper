"""Yield-quality tests for src/discovery.py — job-318 (arkswimwear) class.

Job 318 ran ``--limit 10`` against a listing whose extractor OR'd a permissive
``a[href*="/intl/"]`` catch-all into the card selectors: discovery accumulated
5641 URLs in DOM order (header nav first, product grid last), Phase-2 sliced
the HEAD, and 9 of the 10 processed URLs were non-product pages (the one good
item was the gift-card page). Two engine defences now exist:

1. ``target_urls`` — a limit-capped run stops the crawl once the head of the
   listing has yielded enough candidates (stop_reason ``target_met``) instead
   of walking to ``max_pages`` exhaustion (job-318: 200 pages / ~44 min).
2. ``rank_pdp`` (default on via ``pdp_rank``) — the returned list is ordered
   most-PDP-like first, stable, drop-free. The recalibrated ``_pdp_score``
   fixes the inversion the old scorer had on Magento-style PDPs
   (``/intl/shop/<category>/<product-slug>`` scored −2 under the old
   any-segment "shop" penalty — BELOW the utility pages it exists to demote).

The template half (playwright_scraper.py) is asserted STATICALLY on the source
text — templates are not importable (bs4/playwright) and are copied verbatim
by code_writer, so the contract has to hold on the source.
"""
from __future__ import annotations

import os
import sys
from urllib.parse import parse_qsl, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.discovery import (
    DiscoveryConfig,
    StopReason,
    _pdp_score,
    discover_item_urls,
    pdp_candidates,
    rank_pdp,
)

TEMPLATES = os.path.join(ROOT, "templates")

# ── job-318 URL shapes (arkswimwear, Magento /intl/ prefix) ──
ARK_PDP = "https://www.arkswimwear.com/intl/shop/womens-bikini-tops/black-triangle-bikini-top"
ARK_CATEGORY = "https://www.arkswimwear.com/intl/shop/womens-bikini-tops"
ARK_GIFT_CARD = "https://www.arkswimwear.com/intl/gift-card/"
ARK_SHIPPING = "https://www.arkswimwear.com/intl/customer-service/shipping"
ARK_CART = "https://www.arkswimwear.com/intl/cart"
ARK_SHOP_ROOT = "https://www.arkswimwear.com/intl/shop"


# ═══════════════════════════════════════════════════════════════════════════════
# Part 1 — the recalibrated scorer
# ═══════════════════════════════════════════════════════════════════════════════

class TestPdpScore:
    def test_job318_inversion_fixed(self):
        """A mid-path ``shop`` segment must not sink a real PDP below junk."""
        assert _pdp_score(ARK_PDP) > 0

    def test_pdp_outranks_category_outranks_utility(self):
        assert _pdp_score(ARK_PDP) > _pdp_score(ARK_CATEGORY)
        assert _pdp_score(ARK_CATEGORY) > _pdp_score(ARK_GIFT_CARD)
        assert _pdp_score(ARK_GIFT_CARD) >= 0  # kept, just demoted

    def test_last_segment_negative_sinks(self):
        """The page IS the utility page when the negative word is the leaf."""
        assert _pdp_score(ARK_CART) < 0
        assert _pdp_score(ARK_SHOP_ROOT) < 0

    def test_midpath_negative_no_penalty(self):
        """``/cart/checkout`` uses cart as a namespace — not a cart page."""
        assert _pdp_score("https://x.test/cart/checkout/complete") >= 0

    def test_platform_pdp_shapes_still_score(self):
        assert _pdp_score("https://x.test/products/foo") >= 2
        assert _pdp_score("https://x.test/dp/B09X") >= 2
        assert _pdp_score("https://x.test/p/nma-mens-quick-dry-t") >= 4

    def test_unparseable_zero(self):
        assert _pdp_score("not a url") == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Part 2 — rank_pdp: drop-free, PDPs-first, stable
# ═══════════════════════════════════════════════════════════════════════════════

class TestRankPdp:
    def test_junk_head_sinks(self):
        """DOM order (nav first, products last) → PDPs first after ranking."""
        urls = [ARK_GIFT_CARD, ARK_CATEGORY, ARK_PDP, ARK_SHIPPING, ARK_CART]
        ranked = rank_pdp(urls)
        assert ranked[0] == ARK_PDP
        assert ARK_CART == ranked[-1]  # negative score sinks below 0-score junk

    def test_drop_free(self):
        urls = [ARK_GIFT_CARD, ARK_CATEGORY, ARK_PDP, ARK_SHIPPING, ARK_CART]
        assert sorted(rank_pdp(urls)) == sorted(urls)

    def test_stable_ties(self):
        """Equal scores keep discovery order (reverse-stable sort)."""
        a = "https://x.test/intl/shop/shirts/shirt-a"
        b = "https://x.test/intl/shop/shirts/shirt-b"
        assert rank_pdp([a, b]) == [a, b]
        assert rank_pdp([b, a]) == [b, a]

    def test_empty(self):
        assert rank_pdp([]) == []


# ═══════════════════════════════════════════════════════════════════════════════
# Part 3 — engine wiring: target_met stop + rank applied to the result
# ═══════════════════════════════════════════════════════════════════════════════

PAGE_SIZE = 36
TOTAL = 97


def _items_for_page(page_num: int) -> list[str]:
    start = (page_num - 1) * PAGE_SIZE
    end = min(start + PAGE_SIZE, TOTAL)
    return [f"https://x.test/item/{i:04d}" for i in range(start, end)]


class _Resp:
    def __init__(self, status: int):
        self.status = status


class _FakePage:
    """PageLike double serving ``?page=N`` items (1-indexed, page 1 = no param)."""

    def __init__(self):
        self._url = "https://x.test/list"

    @property
    def url(self) -> str:
        return self._url

    def goto(self, url: str, timeout: int = 0) -> _Resp:
        self._url = url
        return _Resp(200)

    def evaluate(self, js, *args):
        return []

    def query_selector(self, selector):
        return None

    def wait_for_timeout(self, ms):
        pass

    def wait_for_load_state(self, state):
        pass


def _extract(page) -> list[str]:
    qs = dict(parse_qsl(urlparse(page.url).query, keep_blank_values=True))
    page_num = int(qs.get("page") or 1)
    return _items_for_page(page_num)


def _cfg(**overrides) -> DiscoveryConfig:
    base = {
        "strategies": ("page_param",),
        "page_param_name": "page",
        "items_per_page": PAGE_SIZE,
        "page_settle_after_nav_s": 0.0,
        "max_pages": 25,
        "min_initial_links": 1,
    }
    base.update(overrides)
    return DiscoveryConfig(**base)


class TestTargetMetStop:
    def test_stops_once_target_reached(self):
        """40-target: initial page (36) short → one page_param round (72) → stop."""
        result = discover_item_urls(_FakePage(), "https://x.test/list", _extract,
                                    _cfg(target_urls=40))
        assert result.stop_reason == StopReason.TARGET_MET.value
        assert len(result.urls) >= 40
        assert len(result.urls) < TOTAL  # did NOT crawl to exhaustion
        assert result.max_pages_hit is False

    def test_initial_page_alone_can_meet_target(self):
        result = discover_item_urls(_FakePage(), "https://x.test/list", _extract,
                                    _cfg(target_urls=30))
        assert result.stop_reason == StopReason.TARGET_MET.value
        assert len(result.urls) == PAGE_SIZE

    def test_none_target_crawls_to_exhaustion(self):
        """No target → pre-job-318 behaviour (exhaustion / max_pages)."""
        result = discover_item_urls(_FakePage(), "https://x.test/list", _extract,
                                    _cfg(target_urls=None))
        assert result.stop_reason != StopReason.TARGET_MET.value
        assert len(result.urls) == TOTAL


class TestEngineRanking:
    def test_pdp_rank_on_by_default(self):
        """Mixed DOM order (junk first, PDPs last) comes back PDPs-first."""
        page = _FakePage()
        items = _extract(page)
        mixed = [ARK_GIFT_CARD, ARK_CART, ARK_SHIPPING] + items

        def _mixed_extract(p):
            return mixed

        result = discover_item_urls(page, "https://x.test/list", _mixed_extract,
                                    _cfg(target_urls=30))
        ranked = result.urls
        # Item URLs (+4: "item" segment AND "/item/" substring) occupy the head
        # in stable order; junk (+2/0/−2) is sunk behind them — DOM order had
        # gift-card FIRST (the job-318 head).
        assert ranked[:len(items)] == items
        assert ranked[0] != mixed[0]
        assert set(ranked) == set(mixed)  # drop-free

    def test_pdp_rank_off_preserves_order(self):
        """pdp_rank=False keeps raw crawl order even when ranking would flip it."""
        page = _FakePage()

        def _cart_first(p):
            return [ARK_CART, ARK_PDP]

        result = discover_item_urls(page, "https://x.test/list", _cart_first,
                                    _cfg(pdp_rank=False, pdp_filter=False,
                                         target_urls=2))
        assert result.urls == [ARK_CART, ARK_PDP]

    def test_pdp_filter_opt_in_drops_negatives(self):
        page = _FakePage()

        def _with_junk(p):
            return _extract(p) + [ARK_CART, ARK_SHOP_ROOT]

        result = discover_item_urls(page, "https://x.test/list", _with_junk,
                                    _cfg(pdp_filter=True, pdp_rank=False,
                                         target_urls=1000))
        assert ARK_CART not in result.urls
        assert ARK_SHOP_ROOT not in result.urls
        assert "https://x.test/item/0000" in result.urls

    def test_result_fields_survive_postprocessing(self):
        """replace() must not lose stop_reason/pages_visited/param_used."""
        result = discover_item_urls(_FakePage(), "https://x.test/list", _extract,
                                    _cfg(target_urls=40))
        assert result.pages_visited == 2
        assert result.stop_reason == StopReason.TARGET_MET.value


# ═══════════════════════════════════════════════════════════════════════════════
# Part 4 — template contract (static source asserts; templates aren't importable)
# ═══════════════════════════════════════════════════════════════════════════════

class TestPlaywrightTemplateContract:
    def _src(self) -> str:
        with open(os.path.join(TEMPLATES, "playwright_scraper.py")) as fh:
            return fh.read()

    def test_target_wiring_present(self):
        src = self._src()
        assert "_cfg.target_urls = DISCOVERY_TARGET_URLS" in src
        assert "global DISCOVERY_TARGET_URLS" in src
        assert "_item_cap * 4 if _item_cap else None" in src

    def test_strict_selector_rule_documented(self):
        """The job-318 rule must be where code_writer reads it — in the template."""
        src = self._src()
        assert "STRICT product-card selectors" in src
        assert 'a[href*="/intl/"]' in src  # the anti-example stays visible

    def test_slice_still_after_discovery(self):
        """Phase-2 keeps slicing the (now ranked) head — selection unchanged."""
        src = self._src()
        assert "product_urls[: args.limit]" in src
        idx_target = src.index("DISCOVERY_TARGET_URLS = _item_cap")
        idx_slice = src.index("product_urls[: args.limit]")
        assert idx_target < idx_slice


# ═══════════════════════════════════════════════════════════════════════════════
# Part 5 — pdp_candidates keep-all fallback still holds (shared with
# src/listing_discovery.py; a filter that empties a yield must never ship)
# ═══════════════════════════════════════════════════════════════════════════════

class TestPdpCandidatesFallback:
    def test_keep_all_when_nothing_scores(self):
        urls = ["https://x.test/a", "https://y.test/b"]
        assert pdp_candidates(urls, min_score=5) == urls

    def test_urlparse_roundtrip_on_ranked_urls(self):
        """Ranking must not mangle URLs (opaque ids, query strings)."""
        urls = [
            "https://x.test/p/1234?q=1",
            "https://x.test/products/widget?colour=blue",
        ]
        for u in rank_pdp(urls):
            assert urlparse(u).scheme == "https"
