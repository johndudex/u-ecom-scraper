"""[wave-37 W37-NEW-C] Intake-mode coercion for PDP-shaped URLs.

Prod evidence 09-15→09-18: 7 jobs (626/625/622/620/614/602/598) submitted
item URLs as listing pages; every one died on the cross-domain guard after
the traverse mined the PDP's recommendation carousels. The URL shape is
decidable at intake for free.
"""
from __future__ import annotations

from urllib.parse import urlparse

# Merch-path evidence from the prod failures + the platform defaults
# (Shopify /products/, SFCC /p/, Magento /items|product/). "shop"/"store"
# are deliberately EXCLUDED — they are common CATEGORY-page prefixes
# (/shop/all) and would coerce genuine listings. Matched against ANY
# non-final path segment: prod 626's PDP is /us/p/Girls-Active-… — a
# locale prefix sits before the merch segment.
_PDP_SEGMENTS = frozenset({
    "products", "product", "p", "items", "item", "dp",
})


def coerce_pdp_intake(url: str, nav_method: str) -> tuple[str, str | None]:
    """Return the effective nav method, coercing PDP-shaped listing/search
    submissions to ``pdp`` (the pipeline's url_list single-item mode).

    ``note`` is None when no coercion happened; callers log/persist it so the
    job row explains the mode change. [wave-65] the note is also surfaced on
    the create response's ``warning`` slot — prod 1417 flipped a listing job
    to single-item mode and the user only found out from the mode badge.
    """
    if nav_method not in ("listing", "search"):
        return nav_method, None
    path = (urlparse(url).path or "").strip("/")
    segs = [s.lower() for s in path.split("/") if s]
    # len >= 2: a bare merch segment with no slug (/products) is a category
    # root, not an item page — leave it alone.
    if len(segs) >= 2:
        hit = next((s for s in segs[:-1] if s in _PDP_SEGMENTS), None)
        if hit:
            return "pdp", (
                f"URL looks like an item page ('/{hit}/…') — coerced from "
                f"'{nav_method}' to PDP mode [wave-37 W37-NEW-C]. "
                f"Expecting many products? Submit the listing/catalog page "
                f"URL instead."
            )
    return nav_method, None


def looks_like_search_seed(url: str) -> bool:
    """[wave-65] Is this exact-list seed a SEARCH/RESULTS page?

    Prod 1418: an amazon ``/s?rh=…`` URL sailed through exact-list mode as
    a product-URL seed and burned 2.5h to an honest pc=0. Narrow by design —
    category/collection seeds are legitimate (wave-57's warn-stamp contract;
    job 566 shipped 913 rows behind them), and ``/s/<slug>/<digits>`` PDPs
    (nordstrom prod seeds) must never flag.

    Composition, not duplication: multi-segment shapes delegate to
    ``discovery_quality.seed_url_is_listing_shaped`` (corpus-measured
    zero-collateral on 20,892 production seed URLs; the item-shaped veto
    spares nordstrom ``/s/<slug>/<digits>``). Single-segment ``/s`` is NOT
    delegated — the row floor correctly treats root-slug PDPs as items, so
    ``/s`` needs its own rule: search pages are ``/s`` + query (amazon
    family), i.e. exactly ONE path segment and a non-empty query string.
    """
    try:
        segs = [s.lower() for s in (urlparse(url).path or "").split("/") if s]
    except Exception:
        return False
    if not segs:
        return False
    if segs[0] == "s":
        return len(segs) == 1 and bool(urlparse(url).query)
    if segs[0] == "search":
        from src.discovery_quality import seed_url_is_listing_shaped

        return seed_url_is_listing_shaped(url)
    return False
