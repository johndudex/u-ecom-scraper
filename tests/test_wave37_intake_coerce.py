"""[wave-37 W37-NEW-C] PDP-shaped URLs submitted as listing pages coerce to
PDP mode at intake. Prod 626-class (7 jobs): /products/, /p/, /items/ URLs
with nav_method=listing → the traverse treats the PDP as a category page,
harvests off-domain recommendation carousels, and the cross-domain guard
kills the job twice over. Intake knows the URL shape cheapest.
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

from src.intake_coerce import coerce_pdp_intake  # noqa: E402


def test_pdp_path_coerces_listing_to_pdp():
    mode, note = coerce_pdp_intake(
        "https://www.childrensplace.com/us/p/Girls-Active-Short-Sleeve-Top", "listing"
    )
    assert mode == "pdp"
    assert note and "/p/" in note


def test_pdp_path_coerces_search_too():
    mode, _ = coerce_pdp_intake(
        "https://example.com/products/blue-shirt-12345", "search"
    )
    assert mode == "pdp"


def test_category_path_untouched():
    mode, note = coerce_pdp_intake("https://example.com/collections/all", "listing")
    assert mode == "listing"
    assert note is None


def test_homepage_untouched():
    mode, note = coerce_pdp_intake("https://example.com/", "listing")
    assert mode == "listing"
    assert note is None


def test_url_list_nav_untouched():
    mode, note = coerce_pdp_intake("https://example.com/p/x", "list")
    assert (mode, note) == ("list", None)


def test_multi_segment_pdp_paths():
    for path in ("/shop/items/girls-top-88231", "/store/products/widget"):
        mode, _ = coerce_pdp_intake(f"https://example.com{path}", "listing")
        assert mode == "pdp", path


def test_shop_category_listing_not_coerced():
    # "shop"/"store" are category-page prefixes, not PDP markers —
    # a genuine listing under /shop/ must keep its mode.
    for path in ("/shop/all", "/store/collections/kitchen"):
        mode, note = coerce_pdp_intake(f"https://example.com{path}", "listing")
        assert mode == "listing", path
        assert note is None


def test_generic_deep_path_is_not_pdp():
    # deep non-merch paths (articles, help pages) must not coerce
    mode, note = coerce_pdp_intake("https://example.com/blog/how-to-choose", "listing")
    assert mode == "listing"
    assert note is None


def test_single_merch_segment_without_slug_untouched():
    # bare "/products" with no item slug is a category root, not a PDP
    mode, note = coerce_pdp_intake("https://example.com/products", "listing")
    assert mode == "listing"
    assert note is None
