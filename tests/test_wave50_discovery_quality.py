"""[wave-50 T5e] Generic nav-tree junk bar — the structural detector and its
safe shapes. No site names in the detector; the wonderbly shape appears here
only as one instance of the generic "sibling section links shallower than
the listing" pattern.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture
def detect():
    from src.discovery_quality import shallow_section_links

    return shallow_section_links


LISTING = "https://books.example/personalized-books/kids/age/0-3"


class TestFires:
    def test_section_siblings_shallower_than_listing(self, detect):
        """The 1116/488 shape, generic: every 'item' is a sibling section of
        the listing's own tree segment, at depth 2 vs the listing's 4."""
        junk = [
            "https://books.example/personalized-books/new-in",
            "https://books.example/personalized-books/bestsellers",
            "https://books.example/personalized-books/gifts",
            "https://books.example/personalized-books/baby",
            "https://books.example/personalized-books/toddler",
        ]
        res = detect(LISTING, junk)
        assert res["fired"] is True
        assert res["share"] == 1.0
        assert res["suspicious"] == 5
        assert res["segment"] == "personalized-books"

    def test_fires_at_threshold_share(self, detect):
        urls = [
            # real PDP: deeper than the listing (5 vs 4), same segment
            "https://s.example/shop/cat/sub/age/item-1",
        ] + [
            f"https://s.example/shop/sec{i}" for i in range(9)
        ]
        res = detect("https://s.example/shop/cat/sub/age", urls)
        assert res["fired"] is True  # 9/10 >= 0.8
        assert res["share"] == 0.9

    def test_query_and_trailing_slash_normalized(self, detect):
        junk = [
            "https://books.example/personalized-books/new-in/?sort=asc",
            "https://books.example/personalized-books/bestsellers#top",
            "https://books.example/personalized-books/gifts/",
        ]
        res = detect(LISTING, junk)
        assert res["fired"] is True


class TestSafeShapes:
    def test_real_item_pages_below_the_listing(self, detect):
        """Items live DEEPER under the same segment — the healthy tree."""
        real = [
            f"https://books.example/personalized-books/kids/age/0-3/item-{i}"
            for i in range(5)
        ]
        assert detect(LISTING, real)["fired"] is False

    def test_products_beside_collections(self, detect):
        """The classic shop shape: listing /collections/all, items
        /products/<slug> — different first segment, never fires."""
        urls = [f"https://s.example/products/sku-{i}" for i in range(10)]
        res = detect("https://s.example/collections/all", urls)
        assert res["fired"] is False
        assert res["suspicious"] == 0

    def test_shallow_pdp_prefix_site(self, detect):
        """/c/<cat> listing with /p/<slug> items — different segment."""
        urls = [f"https://s.example/p/sweater-{i}" for i in range(8)]
        assert detect("https://s.example/c/womens/sweaters", urls)["fired"] is False

    def test_mixed_set_below_threshold(self, detect):
        """1 junk section link among real items — dilution, not a verdict."""
        urls = ["https://books.example/personalized-books/new-in"] + [
            f"https://books.example/personalized-products/slug-{i}"
            for i in range(9)
        ]
        res = detect(LISTING, urls)
        assert res["fired"] is False
        assert res["suspicious"] == 1

    def test_root_listing_never_judged(self, detect):
        """Listing at site root: 'shallower' is impossible — no verdict."""
        urls = [f"https://s.example/sec{i}" for i in range(5)]
        res = detect("https://s.example/", urls)
        assert res["fired"] is False

    def test_too_few_urls_never_fires(self, detect):
        junk = [
            "https://books.example/personalized-books/new-in",
            "https://books.example/personalized-books/bestsellers",
        ]
        assert detect(LISTING, junk)["fired"] is False

    def test_cross_host_urls_excluded(self, detect):
        junk = [
            "https://other.example/personalized-books/new-in",
            "https://other.example/personalized-books/bestsellers",
            "https://other.example/personalized-books/gifts",
        ]
        res = detect(LISTING, junk)
        assert res["fired"] is False
        assert res["checked"] == 0

    def test_relative_urls_counted_as_same_host(self, detect):
        junk = [
            "/personalized-books/new-in",
            "/personalized-books/bestsellers",
            "/personalized-books/gifts",
        ]
        assert detect(LISTING, junk)["fired"] is True

    def test_junk_url_equal_to_listing_is_not_shallower(self, detect):
        junk = [LISTING, LISTING + "/", LISTING + "?x=1"]
        res = detect(LISTING, junk)
        assert res["fired"] is False
        assert res["checked"] == 3 and res["suspicious"] == 0

    def test_empty_inputs(self, detect):
        assert detect(LISTING, [])["fired"] is False
        assert detect("", ["/a/b", "/a/c", "/a/d"])["fired"] is False
