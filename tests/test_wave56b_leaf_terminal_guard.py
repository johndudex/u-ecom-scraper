"""[wave-56b F5b] The leaf exemption must not waive SECTION-shaped URLs.

Deep-audit proof (2026-10-09): wave-56's suffix arm waives any row-shaped
URL with a file suffix and no nav-directory token — so a nav tree whose
section links carry ``.html`` (``/us/sale.html``, ``/catalogue/new-in/…``)
flips the junk bar from a 100% veto to silence; 1,780 of 11,027 promoted
corpus URLs (16%) now sit in the exempt family. The suffix arm keeps the
books fix (``/index.html`` PDPs) but must refuse TERMINAL-NAV tokens
(``sale``, ``new-in``, ``bestsellers``…) in any segment, and the standalone
digit arm goes entirely (``/catalogue/fiction/page-2``-style paginated
sections must return to suspicious; the books fix never needed it).
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


def _links(base, paths):
    return [f"{base}{p}" for p in paths]


class TestSuffixSectionsStillFire:
    BASE = "https://shop.example/us/women/dresses"

    def test_terminal_token_suffix_sections_fire(self):
        urls = _links("https://shop.example/us/", [
            "sale.html", "new-arrivals.html", "clearance.html", "gifts.html",
        ])
        out = shallow_section_links(self.BASE, urls)
        assert out["fired"], out
        assert out["suspicious"] == 4

    # Listings must sit DEEPER than the sections (real books-toscrape shape:
    # depth-5 category listing) — the wave-50 bar only depth-judges URLs
    # strictly shallower than the listing, so equal/deeper listings would
    # test the depth rule, not the exemption this file targets.
    DEEP_LISTING = "https://books.example/catalogue/category/books/travel_2/index.html"

    def test_terminal_token_mid_path_with_index_suffix_fires(self):
        urls = _links("https://books.example/catalogue/", [
            "new-in/index.html", "bestsellers/index.html",
            "gifts/index.html", "clearance/index.html",
        ])
        out = shallow_section_links(self.DEEP_LISTING, urls)
        assert out["fired"], out

    def test_digit_marked_sections_fire_again(self):
        """[wave-56b corrected] Pagination stems are listing counters, not
        item identifiers: ``page-N`` stays depth-judged even though the
        digit arm is restored for PDP identifiers
        (``/catalog/item/12345``, ``/alyshia-black-2``)."""
        urls = _links("https://books.example/catalogue/", [
            "fiction/page-2", "fiction/page-3", "fiction/page-4",
        ])
        out = shallow_section_links(self.DEEP_LISTING, urls)
        assert out["fired"], out

    def test_directory_token_index_sections_still_fire(self):
        urls = _links("https://books.example/catalogue/", [
            "category/fiction_1/index.html", "category/travel_2/index.html",
            "category/history_3/index.html",
        ])
        out = shallow_section_links(self.DEEP_LISTING, urls)
        assert out["fired"], out


class TestBooksFixSurvives:
    LISTING = "https://books.toscrape.com/catalogue/category/books/travel_2/index.html"

    def test_flat_index_html_pdp_namespace_still_exempt(self):
        pdps = _links("https://books.toscrape.com/catalogue/", [
            f"some-novel_{n}/index.html" for n in range(1, 11)
        ])
        out = shallow_section_links(self.LISTING, pdps)
        assert not out["fired"], out
        assert out["suspicious"] == 0

    def test_real_item_suffix_slugs_stay_exempt(self):
        """citybeach shape ``/us/<slug>/<id>-NN.html``: real items must not
        be vetoed (a false veto = a missed pass)."""
        urls = _links("https://shop.example/us/", [
            "foil-legging/53839062-041.html", "vinyasa-scarf/53839063-100.html",
            "mesh-bra/53839064-001.html",
        ])
        out = shallow_section_links("https://shop.example/us/women/dresses", urls)
        assert not out["fired"], out
