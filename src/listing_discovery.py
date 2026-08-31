"""Shared Phase-1 listing discovery for requests-based scrapers. NOT site-adaptable code.

Why this module exists (job-65 citybeach, after job-58/62 birkenstock): the
discovery loop lived inline in ``templates/requests_scraper.py`` and
code_writer rewrote it. Job 65's draft kept the ``soft_block_escalations``
counter — so the output LOOKED instrumented — but deleted the entire
zero-links escalation branch that could ever increment it, replacing the
guard with a bare ``if new_on_page == 0: no_new_items``. At execution the
site soft-blocked (200-but-zero-links; it had served 1,317 URLs to the
tester minutes earlier), the ladder had no trigger, the 45s retry re-hit the
same IP, and the job finalized 0 items. Same structural lesson as
``src/http_fetch.py``: machinery the run depends on cannot live in the
LLM-editable file.

The writer adapts exactly one callback — ``extract_fn(soup) -> list[str]``
(the site's anchors, regex and absolutization) — plus data constants.
Pagination, the soft-block proxy ladder, the tier floor lock-in, the JSON-LD
``ItemList`` fallback, the zero-URL reclassification, and the one-shot retry
all live here, out of reach.

Two "200 but no product links" signatures are disambiguated per page:

- HIDDEN SSR: the listing's product URLs exist only inside a JSON-LD
  ``ItemList`` block (the visible grid hydrates client-side). Before
  declaring a page empty, the ItemList is parsed — citybeach served 48
  product URLs that way while selecting zero anchors. No extra request.
- SOFT BLOCK: an anti-bot challenge served with HTTP 200 selects zero item
  links AND carries no ItemList. Invisible to status-code ban checks, so the
  DISCOVERY LOOP escalates the proxy tier and refetches the SAME page
  (page 1 only; an empty page >= 2 is a genuine catalog end).

Diagnostics: every listing page logs THREE counts — anchors served, usable
product links extracted, new URLs added. Job 65's postmortem was blocked
because the draft logged only the post-filter count, making "served 0
anchors" and "served 300 anchors, none matching" indistinguishable.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from urllib.parse import urlencode, urljoin

from src.discovery import pdp_candidates
from src.http_fetch import SoftBlock

logger = logging.getLogger(__name__)

# Stop reasons that mean "ran out of listing" rather than "could not see the
# listing". Only these reclassify to empty_first_page when ZERO urls were
# found across every listing — a real catalog-end requires having seen items.
_EXHAUSTION_REASONS = ("short_page", "no_next_link", "no_new_items")

# [job-85 supercheapauto] Gates used to key on ``discovered_urls == 0``, but a
# job URL that is an ITEM page yields exactly 1 junk link (itself / related
# products) — which reads as success while the usable yield is 0. A real
# listing page serves dozens of cards; 1-2 links is a detail page, not a
# catalog.
ZERO_YIELD_JUNK_LINKS = 2


def listing_yield_failure(cov: dict | None) -> bool:
    """True when discovery exited cleanly but produced NO usable item URLs.

    [job-85 supercheapauto] One shared definition of "this listing cannot be
    crawled", consumed by RC1's execution listing fallback and the graph's
    Phase-1 probe gates (they previously keyed on ``discovered_urls == 0``,
    which a PDP-as-listing's single junk link defeats: ``discovered_urls: 1,
    found: 0, stop_reason: "short_page"`` armed nothing and the run shipped
    0 items as SUCCESS).

    Failure shapes:
    - ``empty_first_page`` — the job-58 200-but-blocked signature;
    - zero usable yield (``found`` when the run reports it, else the raw
      link count) with an exhaustion-flavored stop reason — the crawl gave
      up before ever seeing a catalog;
    - a tiny raw yield (≤ ``ZERO_YIELD_JUNK_LINKS`` links) with no usable
      yield.

    NEVER a failure: ``max_pages_hit`` (genuine catalog end), hard errors
    (``navigate_error`` / ``navigate_throttled`` — access problems owned by
    the strategy ladder, not the listing choice), and any missing or
    unknowable signal (gates stay no-op on missing data).
    """
    if not isinstance(cov, dict):
        return False
    sr = str(cov.get("stop_reason") or "")
    if sr == "empty_first_page":
        return True
    if sr in ("max_pages_hit", "navigate_error", "navigate_throttled"):
        return False
    disc = cov.get("discovered_urls")
    if isinstance(disc, (list, tuple)):
        disc = len(disc)
    try:
        disc_n = int(disc or 0)
    except (TypeError, ValueError):
        disc_n = 0
    if disc_n == 0:
        # Raw zero (the legacy RC1 contract): nothing was ever seen, so any
        # non-hard stop reason counts as a dead listing.
        return True
    found = cov.get("found")
    if found is None and isinstance(cov.get("coverage"), dict):
        # Probe-yield shape: the full coverage dict rides along nested.
        # NOTE: ``found`` only means something on a run that EXTRACTED — a
        # --discover-only probe's found is 0 by construction, so the probe
        # caller (graph._probe_yield_dead) nulls it before calling this
        # predicate. A literal nested 0 from any other caller keeps the old
        # zero-usable-yield verdict.
        found = cov["coverage"].get("found")
    try:
        found_n = int(found) if found is not None else None
    except (TypeError, ValueError):
        found_n = None
    if found_n is None:
        # No post-filter yield reported (the probe path reports raw links
        # only) — the raw count IS the yield, and ≤2 links is a detail
        # page's self/related links, not a catalog.
        return disc_n <= ZERO_YIELD_JUNK_LINKS
    if found_n != 0:
        return False
    # Zero usable yield alongside nonzero raw links: exhaustion-flavored
    # stop = the crawl never saw a catalog; tiny raw yield = the "listing"
    # was an item page.
    return sr in _EXHAUSTION_REASONS or disc_n <= ZERO_YIELD_JUNK_LINKS


def build_page_url(
    listing_url: str,
    page: int,
    page_param: str = "page",
    page_size: int | None = None,
    offset_mode: bool = False,
    extra_page_params: dict | None = None,
) -> str:
    """Build the listing URL for ``page`` (1-based).

    Numbered params: ``?page=2``. Offset-style platforms (SFCC ``start``,
    Algolia-style offsets): ``offset_mode=True`` + ``page_size`` turns page 2
    into ``?start=48&sz=48`` (with ``extra_page_params={"sz": 48}``).
    """
    if offset_mode:
        if not page_size:
            raise ValueError(
                "offset_mode=True requires page_size (the param carries a "
                "byte/row offset, not a page number)"
            )
        param_value = (page - 1) * page_size
    else:
        param_value = page

    sep = "&" if "?" in listing_url else "?"
    url = f"{listing_url}{sep}{page_param}={param_value}"
    if extra_page_params:
        url += "&" + urlencode(extra_page_params)
    return url


def _iter_jsonld_blocks(soup) -> list:
    """Every parseable ``application/ld+json`` payload on the page."""
    payloads = []
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or (script.get_text() if hasattr(script, "get_text") else None)
        if not raw:
            continue
        try:
            payloads.append(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            continue
    return payloads


def _types_of(node: dict) -> set:
    """``@type`` as a set — it is a string, a list, or absent."""
    t = node.get("@type")
    if isinstance(t, str):
        return {t}
    if isinstance(t, list):
        return {x for x in t if isinstance(x, str)}
    return set()


def _urls_from_item_list(node: dict) -> list[str]:
    """Pull candidate item URLs out of one schema.org ``ItemList`` node."""
    urls: list[str] = []
    for element in node.get("itemListElement") or []:
        if isinstance(element, str):
            urls.append(element)
        elif isinstance(element, dict):
            item = element.get("item")
            if isinstance(item, str):
                urls.append(item)
            elif isinstance(item, dict):
                urls.append(item.get("url") or item.get("@id") or "")
            else:
                urls.append(element.get("url") or element.get("@id") or "")
    return [u for u in urls if isinstance(u, str) and u]


def jsonld_item_urls(soup, base_url: str) -> list[str]:
    """Product URLs from JSON-LD ``ItemList`` blocks, absolutized against
    ``base_url``. ``BreadcrumbList`` and other node types are ignored —
    ItemList is the schema type listing pages embed for their item set."""
    urls: list[str] = []
    for payload in _iter_jsonld_blocks(soup):
        nodes = payload if isinstance(payload, list) else [payload]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            graph = node.get("@graph") or [node]
            for sub in graph:
                if isinstance(sub, dict) and "ItemList" in _types_of(sub):
                    urls.extend(_urls_from_item_list(sub))
    return [urljoin(base_url, u) for u in urls]


def discover_listing_urls(
    fetch_page: Callable,
    listing_urls: list,
    extract_fn: Callable,
    *,
    page_param: str = "page",
    page_size: int | None = None,
    offset_mode: bool = False,
    extra_page_params: dict | None = None,
    url_filter=None,
    max_pages: int | None = None,
    deadline_s: float = 300,
) -> tuple[list[str], dict]:
    """Phase 1: discover item URLs by paginating every listing page.

    ``fetch_page`` is the ``src.http_fetch`` closure — called as
    ``fetch_page(url, min_tier)``; its ``min_tier_floor`` / ``tiers_total``
    attributes drive the soft-block ladder. ``extract_fn(soup)`` returns the
    page's usable product URLs (absolute, site-filtered) — the ONLY
    site-adaptable part. ``url_filter`` (optional regex) additionally gates
    JSON-LD ItemList candidates so hidden-SSR discovery respects the same
    product-URL shape the anchors do — and the anchor results themselves (a
    permissive selector must not ship nav/category links the pattern excludes).
    With NO ``url_filter`` the generic PDP-likeness partition
    (``src.discovery.pdp_candidates``) is the only shape signal applied, and it
    runs instead of — never alongside — ``url_filter``.

    Returns ``(urls, discovery_meta)``; ``discovery_meta`` carries the
    signals the discovery-coverage gate reads (contract §1/§2):
    ``stop_reason`` (``navigate_error`` is sticky across listings), raw
    ``discovered_urls``, friction counters, and JSON-LD fallback usage.
    """
    all_urls: list[str] = []
    seen: set = set()
    # Default per contract §2: a loop that completes without an explicit break
    # is treated as "ran off the end of pagination" (no next link).
    stop_reason = "no_next_link"
    saw_navigate_error = False
    deadline_start = time.monotonic()
    # [job-62 birkenstock] Soft-block escalation state: the proxy tier index
    # the next fetch starts from, and how many times a 200-but-zero-links
    # page forced an escalation (surfaced in discovery_coverage).
    min_tier = 0
    soft_block_escalations = 0
    pages_fetched = 0
    jsonld_fallback_pages = 0

    floor = int(getattr(fetch_page, "min_tier_floor", 0))
    tiers_total = int(getattr(fetch_page, "tiers_total", 1))

    # [job-316 citybeach] Probe page cap: the test-time discovery probe only
    # needs to know whether this listing yields item URLs, not walk a deep
    # catalogue (citybeach: 29 pages ≈ 8 min — the probe's 180s bound could
    # never reach a verdict, so the Phase-2 zero-yield gate ran blind exactly
    # on the biggest catalogues). The probe sets SCRAPER_DISCOVERY_MAX_PAGES;
    # template callers pass explicit max_pages (or none) and are unaffected.
    if max_pages is None:
        try:
            max_pages = int(os.environ.get("SCRAPER_DISCOVERY_MAX_PAGES") or 0) or None
        except ValueError:
            max_pages = None

    # Enumerate every listing/category URL, paginating each until exhausted.
    # Dedupe across listings via `seen`. For a single-listing site this is
    # identical to a plain ?page=N loop.
    for listing_url in listing_urls:
        page = 1
        while True:
            # Fail-fast: a blocked/slow discovery must not run unbounded.
            if time.monotonic() - deadline_start > deadline_s:
                logger.warning(
                    "Discovery exceeded %ss deadline — stopping (navigate_error). "
                    "A blocked strategy should fail fast so the gate switches.",
                    deadline_s,
                )
                saw_navigate_error = True
                stop_reason = "navigate_error"
                break
            paginated_url = build_page_url(
                listing_url, page, page_param, page_size, offset_mode,
                extra_page_params,
            )
            logger.info(f"Fetching listing page {page}: {paginated_url}")

            result = fetch_page(paginated_url, min_tier)
            # [job-58 birkenstock] A 200-wrapped challenge is NOT a navigation
            # failure: the fetch succeeded, the body is just not the listing.
            # fetch_page reports the challenge shape (see
            # src.http_fetch.detect_soft_block) and THIS loop escalates — a
            # soft block is handled exactly like a page that yielded zero
            # links, so the existing escalation branch below stays the single
            # recovery owner.
            soft_blocked = isinstance(result, SoftBlock)
            if not result and not soft_blocked:
                # HTTP error / 429-502-503 / connection failure / rate-limit bail.
                # This is NOT exhaustion — the loop gave up (H4). navigate_error
                # is sticky: if any listing failed, the gate must FAIL.
                saw_navigate_error = True
                stop_reason = "navigate_error"
                break
            pages_fetched += 1

            if soft_blocked:
                soup = None
                page_urls = []
                anchors_served = 0
                extraction_mode = "soft_block"
            else:
                soup, _ = result
                page_urls = list(extract_fn(soup))
                anchors_served = len(soup.find_all("a"))
                extraction_mode = "anchors"
                # Link quality: the site's own product-URL pattern gates BOTH
                # sources of candidates — the anchors extract_fn returns and
                # the hidden-SSR ItemList fallback below. Anchors used to be
                # trusted unconditionally, so a permissive selector shipped
                # nav/category links the regex was written to exclude.
                if url_filter is not None:
                    page_urls = [u for u in page_urls if url_filter.search(u)]
                elif page_urls:
                    # No site pattern known → the generic PDP-likeness
                    # partition is the only remaining shape signal. NEVER both:
                    # with a url_filter present this partition does not run.
                    page_urls = pdp_candidates(page_urls)

            # Hidden-SSR fallback: a 200 page whose grid hydrates client-side
            # still embeds its item set as a JSON-LD ItemList. Check it BEFORE
            # declaring the page empty (and before burning a proxy tier on
            # what is not a block at all).
            if soup is not None and not page_urls:
                candidates = jsonld_item_urls(soup, paginated_url)
                if url_filter is not None:
                    candidates = [u for u in candidates if url_filter.search(u)]
                elif candidates:
                    candidates = pdp_candidates(candidates)
                if candidates:
                    page_urls = candidates
                    extraction_mode = "jsonld_itemlist"
                    jsonld_fallback_pages += 1
                    logger.info(
                        "Anchor extraction found 0 usable product links on %s "
                        "but the JSON-LD ItemList carried %s — using it",
                        listing_url, len(candidates),
                    )

            new_on_page = 0
            for absolute_url in page_urls:
                if absolute_url and absolute_url not in seen:
                    seen.add(absolute_url)
                    all_urls.append(absolute_url)
                    new_on_page += 1

            logger.info(
                "Listing %s page %s [%s]: page served %s anchors, %s usable "
                "product links, %s new (total: %s)",
                listing_url, page, extraction_mode, anchors_served,
                len(page_urls), new_on_page, len(all_urls),
            )

            # [job-62 birkenstock] A tier that unblocks a listing becomes the
            # floor for every later fetch (subsequent pages AND Phase-2 item
            # pages) — without this, extraction would restart unproxied and
            # re-enter the block.
            if page_urls and min_tier > floor:
                floor = min_tier
                fetch_page.min_tier_floor = min_tier
                logger.info(
                    "Proxy tier index %s unblocked %s — locking it in as the "
                    "floor for all later fetches", min_tier, listing_url,
                )

            # Short page: fewer items than expected → genuine end of results.
            # BUT check for a SOFT BLOCK first [job-62 birkenstock]: HTTP 200
            # whose body yields zero item links (anchors AND ItemList) is the
            # anti-bot challenge signature — is_banned() cannot see it (there
            # is no 403/503 status to test). Escalate one proxy tier and
            # re-fetch the SAME page. Only page 1 escalates: an empty page >= 2
            # is a genuine end-of-catalog, and the ladder length bounds it.
            # [job-65 citybeach] The zero-check runs on extract_fn's OUTPUT,
            # inside this module — a draft can no longer filter links inline
            # and bypass the escalation branch.
            if len(page_urls) == 0:
                if page == 1 and min_tier < tiers_total - 1:
                    min_tier += 1
                    soft_block_escalations += 1
                    logger.warning(
                        "Page 1 of %s returned 200 with ZERO product links "
                        "(soft-block signature) — escalating to proxy tier "
                        "index %s and refetching", listing_url, min_tier,
                    )
                    continue
                stop_reason = "short_page"
                break

            # Dedup worked: page returned items but none were new → exhausted.
            if new_on_page == 0:
                stop_reason = "no_new_items"
                break

            # MAX_PAGES safety cap (only fires when max_pages is non-None).
            if max_pages is not None and page >= max_pages:
                stop_reason = "max_pages_hit"
                logger.info(f"Hit MAX_PAGES cap ({max_pages}) at {listing_url}, stopping")
                break

            page += 1

    # navigate_error takes priority over later successes so the classifier sees FAIL.
    if saw_navigate_error:
        stop_reason = "navigate_error"
    elif not all_urls and stop_reason in _EXHAUSTION_REASONS:
        # [job-58 birkenstock] Ending every listing with ZERO discovered URLs
        # and no hard error is the "200-but-blocked" signature: an anti-bot
        # challenge or consent wall served with HTTP 200 selects zero product
        # links, which "short_page" would misreport as a genuine end-of-catalog.
        # A real catalog-end requires having seen items. Reclassify so the
        # discovery-coverage gate FAILs the run instead of declaring exhaustion.
        stop_reason = "empty_first_page"

    discovery_meta = {
        "stop_reason": stop_reason,
        "max_pages_hit": stop_reason == "max_pages_hit",
        "page_cap": max_pages,
        "discovered_urls": len(all_urls),
        "soft_block_escalations": soft_block_escalations,
        "pages_fetched": pages_fetched,
        "jsonld_fallback_pages": jsonld_fallback_pages,
    }
    return all_urls, discovery_meta


def discover_listing_urls_with_retry(
    fetch_page: Callable,
    listing_urls: list,
    extract_fn: Callable,
    *,
    retry_delay_s: float = 45,
    **cfg,
) -> tuple[list[str], dict]:
    """[job-58 birkenstock] One-shot retry around Phase 1 discovery.

    A zero-URL discovery with no hard navigate_error is the "200-but-blocked"
    signature (see the reclassification in ``discover_listing_urls``): the
    listing fetch SUCCEEDS but yields zero product links. Back off once and
    re-enumerate — the block window is often shorter than the gap between the
    test phase's runs (birkenstock's tester run discovered 15 URLs 90s before
    the blocked execution run). Healthy runs never enter the retry path.
    """
    urls, meta = discover_listing_urls(fetch_page, listing_urls, extract_fn, **cfg)
    if urls or not retry_delay_s:
        return urls, meta
    if meta.get("stop_reason") in ("navigate_error", "navigate_throttled"):
        return urls, meta
    logger.warning(
        "Phase 1 discovered 0 URLs (stop_reason=%s, likely 200-but-blocked) — "
        "retrying once after %ss", meta.get("stop_reason"), retry_delay_s,
    )
    time.sleep(retry_delay_s)
    urls, meta = discover_listing_urls(fetch_page, listing_urls, extract_fn, **cfg)
    meta["retried_empty_discovery"] = True
    if not urls and meta.get("stop_reason") not in ("navigate_error", "navigate_throttled"):
        meta["stop_reason"] = "empty_first_page"
    return urls, meta
