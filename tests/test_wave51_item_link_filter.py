"""[wave-51 T7] A configured item URL pattern gates EVERY discovery tier.

Job 499 (briscoes, search_term "air fryer"): the writer legally filled
``ITEM_LINK_SELECTOR = "a[href]"`` — the templates present that as the
default selector shape — and page-wide tier 2 then handed Phase 2 every
anchor on the rendered page (``/#main``, ``/``, ``/new-in/?sort=…``,
``/cart``, ``/register…``). The pattern-filtered tier 3 never fired: a
rendered page always has >20 anchors, so the ``len(links) < 20`` guard
starves it. ``--limit 5`` then kept the first 5 DOM-order anchors — all
nav junk — and the graph shipped 1 shell row as COMPLETED (the tester's
FAIL verdict was overridden by stale-seed ground truth; see T8/T9).

When the site's item URL shape is known and configured, no tier may
return a URL the pattern rejects. Without a configured pattern the
legacy behavior is byte-identical.
"""

import ast
import logging
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

HTTP_NAV = ROOT / "templates" / "http_navigation_scraper.py"
NAV = ROOT / "templates" / "navigation_scraper.py"

PATTERN = r"/product/\d+/[^/?#]+"
SITE_URL = "https://www.briscoes.co.nz/"

LISTING_HTML = """<html><body><div id="main">
<a href="#main">Top</a>
<a href="/">Home</a>
<a href="/new-in/?sort=NEW_ARRIVAL_DESC">New in</a>
<a href="/cart">Cart</a>
<a href="/register?ReturnUrl=%2Fsearch%2F">Register</a>
<a href="/store-finder/">Stores</a>
<a href="/product/1136060/russell-hobbs-satisfry-air-fryer-8-3-litre-rhaf2763/">Satisfry Air Fryer 8.3L</a>
<a href="/product/1129308/brabantia-digital-air-fryer-6-litre-1650w/">Brabantia Digital Air Fryer</a>
</div></body></html>"""

PRODUCT_URLS = [
    SITE_URL + "product/1136060/russell-hobbs-satisfry-air-fryer-8-3-litre-rhaf2763/",
    SITE_URL + "product/1129308/brabantia-digital-air-fryer-6-litre-1650w/",
]


def _load_extract(constants):
    """Exec the template's OWN link-extraction functions with these constants."""
    src = HTTP_NAV.read_text(encoding="utf-8")
    tree = ast.parse(src)
    ns = {
        "re": re,
        "BeautifulSoup": BeautifulSoup,
        "logger": logging.getLogger("wave51-test"),
        "SITE_URL": SITE_URL,
        "urlparse": urlparse,
    }
    ns.update(constants)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in (
            "_extract_item_links",
            "_make_absolute",
            "_is_product_url",
        ):
            exec(  # noqa: S102 - test harness, same pattern as test_discovery_ladder
                compile(ast.get_source_segment(src, node), "<tpl>", "exec"), ns
            )
    return ns["_extract_item_links"]


class TestPatternGatesEveryTier:
    def test_page_wide_ahref_selector_cannot_ship_nav_anchors(self):
        """The exact job-499 shape: no container selector, ``a[href]`` page-wide,
        pattern configured — nav anchors must not reach Phase 2."""
        extract = _load_extract(
            {
                "ITEM_CONTAINER_SELECTOR": "",
                "ITEM_LINK_SELECTOR": "a[href]",
                "ITEM_URL_PATTERN": PATTERN,
            }
        )
        assert extract(LISTING_HTML) == PRODUCT_URLS

    def test_container_scoped_tier_is_gated_too(self):
        """A container selector that turns out to be the page wrapper (the
        header/footer case) gets the same gate."""
        extract = _load_extract(
            {
                "ITEM_CONTAINER_SELECTOR": "div#main",
                "ITEM_LINK_SELECTOR": "a[href]",
                "ITEM_URL_PATTERN": PATTERN,
            }
        )
        assert extract(LISTING_HTML) == PRODUCT_URLS

    def test_placeholder_pattern_keeps_legacy_behavior(self):
        """No configured pattern → byte-identical legacy (unfiltered) tiers.
        Pattern-less drafts keep today's behavior exactly."""
        extract = _load_extract(
            {
                "ITEM_CONTAINER_SELECTOR": "",
                "ITEM_LINK_SELECTOR": "a[href]",
                "ITEM_URL_PATTERN": "{ITEM_URL_PATTERN}",
            }
        )
        got = extract(LISTING_HTML)
        assert PRODUCT_URLS[0] in got
        assert SITE_URL + "new-in/?sort=NEW_ARRIVAL_DESC" in got
        assert SITE_URL + "#main" in got

    def test_broken_pattern_regex_degrades_to_legacy(self):
        """An uncompilable pattern must not zero discovery — legacy path."""
        extract = _load_extract(
            {
                "ITEM_CONTAINER_SELECTOR": "",
                "ITEM_LINK_SELECTOR": "a[href]",
                "ITEM_URL_PATTERN": "((",
            }
        )
        got = extract(LISTING_HTML)
        assert PRODUCT_URLS[0] in got
        assert SITE_URL + "cart" in got

    def test_first_seen_order_and_dedupe_survive(self):
        extract = _load_extract(
            {
                "ITEM_CONTAINER_SELECTOR": "div#main",
                "ITEM_LINK_SELECTOR": "a[href]",
                "ITEM_URL_PATTERN": PATTERN,
            }
        )
        assert extract(LISTING_HTML + LISTING_HTML) == PRODUCT_URLS


class TestHttpNavSourceWiring:
    def test_pattern_compiled_once_before_tier_1(self):
        src = HTTP_NAV.read_text(encoding="utf-8")
        fn = src[src.index("def _extract_item_links") :]
        fn = fn[: fn.index("\ndef ", 10)]
        assert fn.count("re.compile(ITEM_URL_PATTERN)") == 1
        # The compile sits BEFORE the tier-1 selector block.
        assert fn.index("re.compile(ITEM_URL_PATTERN)") < fn.index(
            "ITEM_CONTAINER_SELECTOR and"
        )

    def test_both_selector_tiers_carry_the_gate(self):
        src = HTTP_NAV.read_text(encoding="utf-8")
        fn = src[src.index("def _extract_item_links") :]
        fn = fn[: fn.index("\ndef ", 10)]
        assert fn.count("if pattern and not pattern.search(") >= 2

    def test_tier_3_reuses_the_hoisted_pattern(self):
        src = HTTP_NAV.read_text(encoding="utf-8")
        fn = src[src.index("def _extract_item_links") :]
        fn = fn[: fn.index("\ndef ", 10)]
        # The old local re-compile inside the len<20 block is gone.
        assert fn.count("ITEM_URL_PATTERN not in") == 1


class TestPlaywrightTemplateWired:
    """templates/navigation_scraper.py mirrors the same 3-tier shape on a
    Playwright page object — the same gate must guard both selector tiers."""

    def _fn_src(self) -> str:
        src = NAV.read_text(encoding="utf-8")
        fn = src[src.index("def _extract_item_links") :]
        return fn[: fn.index("\ndef ", 10)]

    def test_both_selector_tiers_carry_the_gate(self):
        assert self._fn_src().count("if pattern and not pattern.search(") >= 2

    def test_pattern_compiled_from_the_configured_constant(self):
        fn = self._fn_src()
        assert "re.compile(ITEM_URL_PATTERN)" in fn
        assert fn.count("re.compile(ITEM_URL_PATTERN)") == 1

    def test_placeholder_stays_legacy(self):
        fn = self._fn_src()
        assert 'ITEM_URL_PATTERN not in ("", "{ITEM_URL_PATTERN}")' in fn
