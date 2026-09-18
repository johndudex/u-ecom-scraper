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
    job row explains the mode change.
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
                f"'{nav_method}' to PDP mode [wave-37 W37-NEW-C]."
            )
    return nav_method, None
