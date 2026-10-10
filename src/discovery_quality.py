"""[wave-50 T5e] Generic nav-tree junk bar.

A recurring false-success class shipped a listing's NAVIGATION TREE as
products: the "discovered URLs" were sibling section links sitting shallower
in the tree than the listing itself (listing ``/<section>/kids/age/0-3``,
"items" ``/<section>/new-in``). Detection must be structural and
site-agnostic — no domain lists, no site names. Two cheap structural
signals:

1. the URL shares the listing's FIRST path segment (the junk lives in the
   same section of the tree the listing sits in), AND
2. the URL is strictly shallower than the listing (real item pages hang
   BELOW their listing; section links hang beside or above it).

Fired ⇒ phase 1 returned the nav tree, not item pages. Known-safe shapes,
checked in tests: ``/products/<slug>`` (different first segment), deep PDPs
under a shared prefix (never shallower than their listing).

[wave-56 F5] One more safe shape: a LEAF item namespace under the listing's
first segment — books-toscrape PDPs are ``/catalogue/<slug>_N/index.html``
(3 segments, no nav-directory token) under a depth-5
``/catalogue/category/books/travel_2/index.html`` listing; the depth rule
alone read that flat PDP namespace as a nav tree and vetoed 1000 real
product rows (job 578). A row-shaped URL carrying no nav-directory segment
is exempt from the suspicious set; genuine section links keep their
``/category|/collections|...`` segments and still fire.

[wave-55 C3] ``row_url_is_item_shaped`` also carries a PDP-shape tier: a row
URL that IS a navigation/listing position (``/collections/<slug>``,
``/search?q=``, ``/new-in``) fails the shape. Audit C3's job 1068 shipped one
title across ``/en-us/watches/collections/tank/`` collection URLs; every
ground-truth counter read them as data because the floor accepted any real
path. The tier is calibrated against the promoted ``scrapers/*``
``input_urls.json`` corpus (11,116 real item URLs): every token in the nav
sets fails NONE of them, so the floor cannot unseat a working scraper. It
stays out of the one fight a URL shape cannot settle — a deep all-slug
category path (job 1200's ``/<section>/<cat>/<subcat>``) is byte-shape
identical to 935 real 4-segment PDPs in that corpus, so content-level bars
own it.

False-positive cost is one honest FAILED — the gate fails the run, it never
edits rows — which is the correct side to err on: junk-as-products is
exactly the failure this system has repeatedly shipped as COMPLETED.
"""

import re
from urllib.parse import urlsplit

# [wave-55 C3] Segment tokens that mark a PRODUCT-DETAIL path on the
# mainstream platforms: ``/products/<slug>``, ``/product/<slug>``, ``/prod/``,
# ``/items/``, ``/pdp/``, ``/sku/``, Spree/Solidus ``/p/<slug>``, marketplace
# ``/dp/<asin>`` and ``/gp/product/<asin>``. Tested against EVERY segment.
_PDP_SEGMENT_TOKENS = frozenset({
    "products", "product", "prod", "items", "item", "pdp", "p", "dp", "gp",
    "sku",
})

# Navigation positions that stay a listing position even mid-path: a path
# PREFIX (``/collections/<slug>``, ``/c/<cat>``, ``/browse/dept/1``,
# ``/search?q=``). Zero-collateral per the corpus calibration above — ``shop``
# and ``new`` are excluded because both sit inside real item paths.
_NAV_DIRECTORY_TOKENS = frozenset({
    "c", "cat", "category", "categories", "collections", "browse", "dept",
    "department", "departments", "section", "search",
})

# Terminal navigation pages: a listing POSITION only as the LAST segment
# (``/new-in``, ``/clearance``, ``/bestsellers``) — mid-path they are plain
# directory names on sites whose PDPs hang under them.
_NAV_TERMINAL_TOKENS = frozenset({
    "new-in", "new-arrivals", "newarrivals", "sale", "clearance",
    "bestsellers", "best-sellers", "gifts", "shop-all", "all-products",
    "range", "ranges",
})

# An ID-shaped or file-suffixed last segment is a PDP signal on its own
# (``/p/12345``, ``/alyshia-black-2``, ``/item/foo-112697.aspx``).
_DIGIT_RE = re.compile(r"\d")
_SUFFIX_RE = re.compile(r"\.[a-z0-9]{2,5}$")


def row_url_is_item_shaped(url: str) -> bool:
    """Structural floor for an extracted ROW: its URL must point at an item.

    Two tiers, oldest first:

    1. [wave-51 T9] a URL with no path (``/#main``, bare origin) is a page
       POSITION (nav anchor / rendered shell), never an item detail page on
       any site — nothing to extract but the chrome.
    2. [wave-55 C3] a URL that IS a navigation/listing position — a
       nav-directory prefix or a terminal nav page — is a listing, and a
       listing is not an item even when a shallow crawl hangs a defaulted
       price on it.

    Deliberately narrow in the pass direction: a path that carries no PDP
    signal and no nav token still passes, because real PDPs exist as a single
    root slug (256 of them in the promoted corpus) and as 4 plain-slug
    segments (935 of them). This floor only rejects the can-never-be-an-item
    class, so it stays safe inside ground-truth counting.
    """
    if not url:
        return False
    try:
        parts = urlsplit(str(url))
        segs = [s for s in (parts.path or "").split("/") if s]
    except Exception:
        return False
    if not segs:
        return False
    low = [s.lower() for s in segs]
    last = low[-1]
    # A bare index (/products, /collections, /search) is a listing root, not
    # a detail page — even when the lone token is a PDP keyword.
    if len(low) == 1:
        return not (
            last in _PDP_SEGMENT_TOKENS
            or last in _NAV_DIRECTORY_TOKENS
            or last in _NAV_TERMINAL_TOKENS
        )
    # Tier-2 passes first: /collections/<c>/products/<slug> is an item.
    if any(s in _PDP_SEGMENT_TOKENS for s in low):
        return True
    if _DIGIT_RE.search(last) or _SUFFIX_RE.search(last):
        return True
    if last in _NAV_TERMINAL_TOKENS:
        return False
    return not any(s in _NAV_DIRECTORY_TOKENS for s in low)


def _exempt_leaf_item(url: str, segs: list[str]) -> bool:
    """[wave-56 F5] Is this URL a leaf item page rather than a section link?

    Requires a file-suffixed last segment (books' ``/index.html``, the
    ``<slug>_<id>/index.html`` PDP namespace). The shape floor deliberately
    passes bare keyword slugs — and the wave-50 junk shapes (``/baby``,
    ``/sec3``) are exactly that class, so they stay depth-judged. The
    nav-directory conjunct keeps genuine section links
    (``/catalogue/category/...``) suspicious even on ``.html``-suffixed
    sites where ``_SUFFIX_RE`` makes every URL row-shaped.

    [wave-56b F5b, corrected per critique] A suffix is a LEAF MARKER, not
    semantics: strip it before the terminal-token check so ``/us/sale.html``
    and ``/new-in/index.html`` stop hiding behind their file extensions.
    The terminal check judges the STRIPPED LAST SEGMENT ONLY — mid-path
    terminal tokens are plain directory names on sites whose PDPs hang
    under them (the token set's own contract), so an any-segment check
    flipped real PDPs like ``/gifts/for-him/socks.html`` into the
    suspicious set. The digit arm is RESTORED (16% of the promoted corpus
    exempted through it — ``/catalog/item/12345``-class PDPs carry no
    suffix); the junk shapes it can hide stay content-gated.
    ``/catalogue/<slug>_981/index.html`` still exempts: its stem is the
    slug, not a nav token.
    """
    try:
        if not row_url_is_item_shaped(url):
            return False
    except Exception:
        return False
    if any(s.lower() in _NAV_DIRECTORY_TOKENS for s in segs):
        return False
    last = segs[-1].lower()
    _stem = _SUFFIX_RE.sub("", last)
    # An index FILE's stem is meaningless ("index") — the named token sits
    # one level up: ``/new-in/index.html`` must judge "new-in", not "index".
    if _stem in ("index", "default") and len(segs) >= 2:
        _stem = _SUFFIX_RE.sub("", segs[-2].lower())
    if _stem in _NAV_TERMINAL_TOKENS:
        return False
    if _SUFFIX_RE.search(last):
        return True
    if _DIGIT_RE.search(last) and len(segs) >= 3:
        # Digit arm (restored — /catalog/item/12345 and /alyshia-black-2
        # class PDPs carry no suffix; 16% of the promoted corpus exempted
        # through it) with one carve-out: a PAGINATION stem before the
        # digits is a listing counter, not an item identifier
        # (``/fiction/page-2`` stays depth-judged).
        _alpha = re.split(r"\d", last, maxsplit=1)[0].strip("-")
        if _alpha not in ("page", "pages", "p"):
            return True
    return False


def _path_segments(url: str) -> tuple[str, list[str]]:
    parts = urlsplit(str(url or ""))
    segs = [s for s in parts.path.split("/") if s]
    return (parts.netloc.lower(), segs)


# [wave-57 class 1] Seed-side listing signals. A url_list job whose seeds
# are LISTING positions (job #566: 19 category/collection urls) was judged
# by a gate whose premise — every seed is an item page — was false, and the
# C2 scope gate vetoed a 913-row full-catalog success. The seed namespace
# set is measured ZERO-collateral on the promoted scrapers/*/input_urls.json
# corpus (20,892 urls): ``shop`` is deliberately EXCLUDED (928 real PDPs —
# renttherunway /shop/designers/<x>/<slug> 900, jayjays 18, woolworths 9 —
# classify listing-shaped under it; discovery_quality's own _NAV_DIRECTORY_
# TOKENS docstring already recorded why shop stays out), and there is NO
# query arm: ``/slug?sort=new`` must not lift an ambiguous plain slug.
_SEED_LISTING_NAMESPACE = frozenset({
    "all", "collection", "catalog", "catalogue",
})


def _seed_path_segments(url: str) -> list[str]:
    try:
        parts = urlsplit(str(url or ""))
        return [s for s in (parts.path or "").split("/") if s]
    except Exception:
        return []


def _seed_strongly_item_shaped(url: str) -> bool:
    """Does this seed carry a POSITIVE item signal (Tier-2 arms of the row
    floor: a PDP segment token, a digit or a file suffix in the last
    segment)? Strong item evidence outranks every listing guess — a set
    containing one such seed never loosens the scope gate."""
    segs = _seed_path_segments(url)
    if not segs:
        return False
    low = [s.lower() for s in segs]
    if any(s in _PDP_SEGMENT_TOKENS for s in low):
        return True
    last = low[-1]
    return bool(_DIGIT_RE.search(last) or _SUFFIX_RE.search(last))


def seed_url_is_listing_shaped(url: str) -> bool:
    """Is this SUBMITTED SEED a listing position rather than an item page?

    Positive listing signal AND zero positive item signal:
      - first path segment in ``_SEED_LISTING_NAMESPACE``
        (``/all/..., /collection/..., /catalog/...``);
      - OR the last segment is a terminal nav page (``/new-in``);
      - OR the URL already fails ``row_url_is_item_shaped`` — the C3
        nav-position class (``/collections/x``, ``/c/gifts``, ``/search``),
        which carries the plural/c/department/browse vocabulary.
    Ambiguous plain-slug paths (``/the-name-of-the-book``, with or without a
    ``?sort=`` query) return False — the classifier never loosens a gate on
    a guess.
    """
    segs = _seed_path_segments(url)
    if not segs:
        return False
    low = [s.lower() for s in segs]
    if _seed_strongly_item_shaped(url):
        return False
    if low[0] in _SEED_LISTING_NAMESPACE:
        return True
    if low[-1] in _NAV_TERMINAL_TOKENS:
        return True
    try:
        return not row_url_is_item_shaped(url)
    except Exception:
        return False


def seeds_are_listing_shaped(urls) -> bool:
    """Does this seed SET read as listings (scope-extending), job-#566?

    GUARDIAN-corrected set rule: at least one seed positively
    listing-shaped AND no seed strongly item-shaped. Weak/ambiguous plain
    slugs are inert — they neither block the loosening nor cause it. (An
    earlier all()-seeds-listing rule kept the #566 set itself strict: its
    ``/shop/accessories-shoes`` member is ambiguous, and the fix must fix
    its own regression target.) All-ambiguous sets — the renttherunway
    PDP-paste shape — stay strict.
    """
    cleaned = [str(u).strip() for u in (urls or []) if str(u or "").strip()]
    if not cleaned:
        return False
    if not any(seed_url_is_listing_shaped(u) for u in cleaned):
        return False
    return not any(_seed_strongly_item_shaped(u) for u in cleaned)


def shallow_section_links(
    listing_url: str,
    urls,
    *,
    min_urls: int = 3,
    share_threshold: float = 0.8,
) -> dict:
    """Structural nav-junk detector over a discovered-URL set.

    Returns ``{"fired", "checked", "suspicious", "share", "examples",
    "segment"}``. ``fired`` requires ≥ ``min_urls`` same-host URLs of which
    ≥ ``share_threshold`` share the listing's first path segment AND sit
    strictly shallower than it.
    """
    result = {
        "fired": False,
        "checked": 0,
        "suspicious": 0,
        "share": 0.0,
        "examples": [],
        "segment": "",
    }
    listing_host, listing_segs = _path_segments(listing_url)
    if not listing_segs:
        # Listing at the site root — there is no shallower; the signal is
        # structurally meaningless here. (Depth, not hostname, is the bar.)
        return result
    first_seg = listing_segs[0]
    depth = len(listing_segs)
    checked = 0
    suspicious: list[str] = []
    for u in urls or []:
        raw = str(u or "").strip()
        if not raw:
            continue
        host, segs = _path_segments(raw)
        if not segs:
            continue
        if listing_host and host and host != listing_host:
            continue
        checked += 1
        if (
            segs[0] == first_seg
            and len(segs) < depth
            and not _exempt_leaf_item(raw, segs)
        ):
            suspicious.append(raw)
    result["checked"] = checked
    result["suspicious"] = len(suspicious)
    if checked < min_urls:
        return result
    share = len(suspicious) / checked
    result["share"] = round(share, 3)
    if share >= share_threshold:
        result["fired"] = True
        result["segment"] = first_seg
        result["examples"] = suspicious[:3]
    return result
