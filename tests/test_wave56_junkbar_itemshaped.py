"""[wave-56 F5] The nav-tree junk bar must not flag item-shaped URLs.

Job 578 (books-toscrape list_page, 2026-10-09): the run's terminal verdict
was the wave-50 ``_shallow_section_gate`` — "1000/1000 discovered URLs are
shallower section links under the listing's own first path segment
'/catalogue'". But books' REAL product URLs are ``/catalogue/<slug>_N/
index.html``: fewer path segments than the category listing
(``/catalogue/category/books/travel_2/index.html``, depth 5), same first
segment — the depth heuristic reads a flat PDP namespace as a navigation
tree and vetoes 1000 genuine product rows.

The predicate's own comment (run_execution.py:2436) names this exact class —
"real PDPs sit in a flat namespace under a shared first segment" — but the
exemption only covers navigation/search_term anchors, not list_page. And the
detector module already HAS the item-shape oracle: ``row_url_is_item_shaped``
(the same one ``_nonitem_row_gate`` trusts for output rows).

Fix: ``shallow_section_links`` exempts a URL from the suspicious set when it
is item-shaped AND carries no nav-directory position. The second conjunct
matters on ``.html``-suffixed sites (books): ``_SUFFIX_RE`` makes EVERY url
row-shaped there, but a genuine section link still carries a nav-directory
segment (``/catalogue/category/...``) — only leaf-slug namespaces (the PDP
shape) are exempt. URLs the shape floor rejects (``/collections/...``,
``/new-in``) keep firing the bar.
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

from src.discovery_quality import shallow_section_links

LISTING = "https://books-toscrape.com/catalogue/category/books/travel_2/index.html"


class TestItemShapedExemption:
    def test_books_pdp_namespace_not_flagged(self):
        # The exact job-578 shape: every discovered URL is a real books PDP —
        # flat under /catalogue, shallower than the category listing, no nav
        # directory segment anywhere in the path.
        pdps = [
            f"https://books-toscrape.com/catalogue/a-light-in-the-attic_{i}/index.html"
            for i in range(10)
        ]
        res = shallow_section_links(LISTING, pdps)
        assert res["fired"] is False, (
            "flat PDP namespaces are items, not section links "
            f"(share={res['share']})"
        )

    def test_genuine_shallow_section_links_still_fire(self):
        # Sibling category pages: shallower than the listing AND carrying the
        # /category/ nav-directory segment — the real nav-tree junk shape.
        sections = [
            f"https://books-toscrape.com/catalogue/category/genre_{i}/index.html"
            for i in range(4)
        ]
        res = shallow_section_links(LISTING, sections)
        assert res["fired"] is True

    def test_mixed_set_not_vetoed(self):
        pdps = [
            f"https://books-toscrape.com/catalogue/some-book_{i}/index.html"
            for i in range(8)
        ]
        sections = [
            "https://books-toscrape.com/catalogue/category/poetry_1/index.html",
        ]
        res = shallow_section_links(LISTING, pdps + sections)
        assert res["fired"] is False

    def test_shape_floor_rejections_still_suspicious(self):
        # Nav positions the item-shape floor rejects (/collections/...) stay
        # in the suspicious set even when item-shaped sites blur the depth
        # rule — the original job-1116/488 junk shape still fires.
        shells = [
            "https://books-toscrape.com/catalogue/collections/sale-room",
        ] * 8
        res = shallow_section_links(LISTING, shells)
        assert res["fired"] is True
