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
checked in tests: ``/collections/all`` → ``/products/<slug>`` (different
first segment), ``/c/<cat>`` → ``/p/<slug>`` (different first segment), and
deep PDPs under a shared prefix (never shallower than their listing).

False-positive cost is one honest FAILED — the gate fails the run, it never
edits rows — which is the correct side to err on: junk-as-products is
exactly the failure this system has repeatedly shipped as COMPLETED.
"""

from urllib.parse import urlsplit


def row_url_is_item_shaped(url: str) -> bool:
    """Structural floor for an extracted ROW: its URL must point at a page.

    A recurring false-success class shipped fragment-only (``/#main``) and
    bare-origin URLs as ``products`` — a URL with no path is never an item
    detail page on any site (there is nothing to extract but the shell).
    Deliberately narrow: anything with a real path passes (``/new-in/?sort=``
    is a deeper junk shape other gates own); this floor only rejects the
    can-never-be-an-item class, so it is safe inside ground-truth counting.
    """
    if not url:
        return False
    try:
        path = (urlsplit(str(url)).path or "").strip("/")
    except Exception:
        return False
    return bool(path)


def _path_segments(url: str) -> tuple[str, list[str]]:
    parts = urlsplit(str(url or ""))
    segs = [s for s in parts.path.split("/") if s]
    return (parts.netloc.lower(), segs)


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
        if segs[0] == first_seg and len(segs) < depth:
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
