#!/usr/bin/env python3
"""
HTTP Requests Scraper — westelm.com.au (NetSuite SuiteCommerce Advanced)

Adapted by Code Writer from templates/requests_scraper.py for the SCA Items API.

SITE MECHANICS (measured, see workspace/westelm-com-au/probe_transport.py):
  * Every HTML page (listing AND PDP) is a ~5.8KB JS shell (`<title>Shopping</title>`,
    empty #main, robots noindex). Plain-HTTP soup crawling yields ZERO product
    anchors on ANY page — the shared soup discovery cannot work here.
  * The site is NetSuite SuiteCommerce Advanced: its open, unauthenticated Items
    REST service returns complete product JSON over plain HTTP:
      - discovery:  GET /api/items?fieldset=search&q={term}&limit=100&offset={n}
      - extraction: GET /api/items?fieldset=details&url={urlcomponent}
    Both verified live (HTTP 200, no anti-bot, no proxy).
  * Phase 1 therefore runs the search API with offset pagination (until the API's
    reported `total` is reached / a page returns no new items). The template's
    shared soup-discovery call site is kept VERBATIM as the fallback for the day
    the site serves real HTML listings again; zero-yield self-heals through it.
  * Phase 2 fetches the details fieldset per item (sequential — ~70 catalogue
    items make ThreadPoolExecutor pointless and would trip the phase-2
    instant-fail floor detector).

Fields (USER SCHEMA ONLY — no standard-table fields):
  product name, price (currency symbol included, e.g. "$42.90"), currency (ISO
  code from locale.currency, e.g. "AUD"), size (group member specs from
  custitem_group_product_information), availability ("in_stock"/"out_of_stock").

Usage:
    python3 scraper_draft.py                    # Phase 1 discovery + full extraction
    python3 scraper_draft.py --input urls.json  # explicit input file (Phase 2 only)
    python3 scraper_draft.py --sample           # 5 products
    python3 scraper_draft.py --listing-url https://www.westelm.com.au/bath
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from html import unescape
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests  # noqa: F401 -- drafts' Phase-2 helpers commonly need it
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

SITE_NAME = "West Elm Australia"
SITE_URL = "https://www.westelm.com.au"
PLATFORM = "NetSuite SuiteCommerce Advanced (SCA) 2018.2"
SCRAPING_METHOD = "http_requests"
SITE_SLUG = "westelm-com-au"

# Code Writer adapted: promoted listing URL from navigation_analysis (job spec).
PRODUCT_LISTING_URL = "https://www.westelm.com.au/bath"
SRC_URL = PRODUCT_LISTING_URL
# Navigation analysis reports load_more (browser-side); the SCA API paginates by
# offset, so the soup fallback keeps the generic "?page=N" construction.
PAGE_PARAM_NAME = "page"
if PAGE_PARAM_NAME.startswith("{") and PAGE_PARAM_NAME.endswith("}"):
    PAGE_PARAM_NAME = "page"
PRODUCT_LISTING_URLS = [PRODUCT_LISTING_URL]
PAGE_SIZE = None
OFFSET_MODE = False
EXTRA_PAGE_PARAMS: dict = {}
DELAY_BETWEEN_REQUESTS = 0.5
MAX_PAGES = None  # Full extraction: no arbitrary cap. Deadline + API `total` bound it.
DISCOVERY_DEADLINE_SECONDS = 300
EMPTY_DISCOVERY_RETRY_DELAY_S = 45

# ── SCA Items API (the site's data plane; HTML is a JS shell) ────────────────
# Code Writer adapted: these constants replace soup-based listing crawling.
API_ITEMS_URL = SITE_URL + "/api/items"
SEARCH_FIELDSET = "search"
DETAILS_FIELDSET = "details"
SEARCH_PAGE_SIZE = 100  # measured: limit=100 honoured (total 70 came back in one page)
DEFAULT_QUERY = "bath"  # promoted listing /bath → search term
# Product urlcomponents on this site end in a -bNNN / -dNNN suffix
# (e.g. fluted-marble-bath-accessories-b281, caspian-metal-bath-accessories-d11450).
PRODUCT_URL_RE = re.compile(r"/[a-z0-9-]+-[bd]\d+", re.IGNORECASE)
NOT_FOUND_MARKERS = ("not found", "no longer available", "discontinued", "unavailable")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
# Code Writer adapted: JSON Accept header for the Items API fetches.
JSON_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": HEADERS["Accept-Language"],
    "Referer": SITE_URL + "/",
}

# ── HTTP FETCH — SHARED MODULE, NOT YOURS TO REIMPLEMENT (CRITICAL) ──────────
# Kept verbatim per template. The ladder defaults to tier "none" — exactly the
# access recipe measured for this site (direct HTTP, no proxy, no stealth).
# --no-proxy is accepted and is the default behavior (tier "none" first).
from src.http_fetch import create_fetch_json, create_fetch_page
from src.page_analysis import phase2_instant_fail

fetch_page = create_fetch_page(delay_s=DELAY_BETWEEN_REQUESTS, headers=HEADERS)
# Code Writer adapted: API-family per-item fetches ride the SAME shared ladder
# (create_fetch_json is src.http_fetch's sanctioned API closure — same session,
# same proxy contract; the LLM cannot strip what it never sees).
fetch_json = create_fetch_json(delay_s=DELAY_BETWEEN_REQUESTS, headers=JSON_HEADERS)

# ── LISTING DISCOVERY — SHARED MODULE, NOT YOURS TO REIMPLEMENT (CRITICAL) ───
# Kept verbatim per template. The soup crawl is this draft's FALLBACK path only
# (the HTML listing is a JS shell today); the PRIMARY Phase 1 is the SCA search
# API loop in discover_urls_via_api below.
from src.listing_discovery import discover_listing_urls_with_retry

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TIMESTAMP = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S_%f")
OUTPUT_FILE = os.path.join(SCRIPT_DIR, f"output_{TIMESTAMP}_{os.getpid()}.json")
INPUT_FILE = os.path.join(SCRIPT_DIR, "input_urls.json")
LOG_FILE = os.path.join(os.path.dirname(SCRIPT_DIR), "logs", f"{SITE_SLUG}.log")

# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════════════

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def clean_html(html_str: str) -> str:
    if not html_str:
        return ""
    text = re.sub(r"<[^>]+>", " ", html_str)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def make_absolute_url(url: str, base: str = SITE_URL) -> str:
    if not url:
        return ""
    if url.startswith("http"):
        return url
    if url.startswith("//"):
        return f"https:{url}"
    return urljoin(base, url)


def is_valid_site_url(url: str) -> bool:
    """URL hygiene (protocol-relative trap guard): absolute http(s) on our host."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and parsed.netloc.endswith("westelm.com.au")


# ── FIELD NORMALIZERS — inline on purpose, NOT a src import ──────────────────

def _norm_price(value) -> Optional[float]:
    """Strip currency symbols/whitespace from a price → float (None if no digits)."""
    if value is None:
        return None
    cleaned = re.sub(r"[^\d.,-]", "", str(value).strip())
    if not re.search(r"\d", cleaned):
        return None
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = re.sub(r",(?=\d{3}(?:\D|$))", "", cleaned).replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _norm_availability(value) -> Optional[str]:
    """Normalize availability to ``in_stock`` / ``out_of_stock``."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "in_stock" if value else "out_of_stock"
    text = str(value).strip().lower()
    if not text:
        return None
    if "://" in text:
        text = text.rsplit("/", 1)[-1]
    compact = text.replace("-", "_").replace(" ", "")
    if compact in ("in_stock", "instock", "available"):
        return "in_stock"
    if compact in ("out_of_stock", "outofstock", "unavailable", "sold_out", "soldout"):
        return "out_of_stock"
    return text


# ═══════════════════════════════════════════════════════════════════════════════
# EXTRACTION — SCA Items API (details fieldset)
# ═══════════════════════════════════════════════════════════════════════════════
# Code Writer adapted: the DOM/JSON-LD extractors are replaced by API-path
# extraction per the product_analysis field map. Zero JSON-LD exists on this
# site (verified) — do not attempt DOM extraction.

def _is_soft_404(item: Optional[dict]) -> bool:
    """No item object, or a not-found-flavoured name, means a dead product page."""
    if not isinstance(item, dict):
        return True
    name_blob = " ".join(
        str(item.get(key) or "")
        for key in ("displayname", "storedisplayname2", "pagetitle")
    ).lower()
    return any(marker in name_blob for marker in NOT_FOUND_MARKERS)


def extract_size(item: dict) -> str:
    """`size` for this site = the group member spec lines (verified field map):
    custitem_group_product_information is HTML like
    '<ul><li>Soap Pump: 7 cm diam. x 18 cm h</li>...</ul>'.
    Regular (non-group) items: defensive peek at itemoptions_detail Size labels.
    """
    raw = item.get("custitem_group_product_information") or ""
    if raw:
        try:
            spec_soup = BeautifulSoup(str(raw), "html.parser")
            lines = [li.get_text(" ", strip=True) for li in spec_soup.find_all("li")]
            lines = [line for line in lines if line]
            if lines:
                return "; ".join(lines)
        except Exception:  # malformed HTML fragment — fall through to plain text
            pass
        return clean_html(str(raw))
    options_detail = item.get("itemoptions_detail")
    if isinstance(options_detail, dict):
        try:
            for option in options_detail.get("options") or []:
                if not isinstance(option, dict):
                    continue
                label = str(option.get("label") or "")
                if "size" in label.lower():
                    values = option.get("values")
                    if isinstance(values, list) and values:
                        names = [
                            v.get("label", "") if isinstance(v, dict) else str(v)
                            for v in values
                        ]
                        return ", ".join(n for n in names if n)
        except Exception:
            pass
    return ""


def extract_product_from_api(
    item: Optional[dict], payload: Optional[dict], url: str, status_code: int, src_url: str
) -> dict:
    """Map one details-fieldset item to the USER SCHEMA (name/price/currency/size/
    availability). Price keeps its currency symbol (job formatting contract:
    "$42.90", the API's own formatted primary source); currency comes from the
    response's top-level locale (sibling of items), falling back to the
    documented $→AUD site inference."""
    product = {
        "id": 0,
        "title": "",
        "price": "",
        "currency": "",
        "size": "",
        "availability": "",
        "url": url,
        "src_url": src_url,
        "status_code": status_code,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": "",
    }

    if _is_soft_404(item):
        # Soft 404 contract: dead/redirected PDPs (API returns no items, or a
        # not-found name) carry remarks and NO extracted data.
        product["remarks"] = "Soft 404: product not found"
        return product

    product["title"] = str(item.get("displayname") or item.get("storedisplayname2") or "").strip()

    # Price — PRIMARY: onlinecustomerprice_detail.onlinecustomerprice_formatted
    # (e.g. "$42.90"). WARNING per field map: pricelevel1 / pricelevel1_formatted
    # are placeholder junk ($1.10) on this site and are NEVER read.
    price_detail = item.get("onlinecustomerprice_detail")
    if not isinstance(price_detail, dict):
        price_detail = {}
    price = str(price_detail.get("onlinecustomerprice_formatted")
                or item.get("onlinecustomerprice_formatted") or "").strip()
    if not price:
        number = _norm_price(price_detail.get("onlinecustomerprice")
                             or item.get("onlinecustomerprice"))
        if number is not None:
            price = f"${number:,.2f}"
    product["price"] = price

    # Currency — TOP-LEVEL locale key (sibling of items), not inside items[0].
    locale = payload.get("locale") if isinstance(payload, dict) else None
    currency = str((locale or {}).get("currency") or "").strip()
    if not currency and price.startswith("$"):
        currency = "AUD"  # documented site inference: '$' on westelm.com.au is AUD
    product["currency"] = currency

    # Size — group member specification lines.
    product["size"] = extract_size(item)

    # Availability — isinstock is a real boolean in the details response.
    product["availability"] = _norm_availability(item.get("isinstock")) or ""

    return product


# ═══════════════════════════════════════════════════════════════════════════════
# DISCOVERY
# ═══════════════════════════════════════════════════════════════════════════════

PRODUCT_LINK_SELECTOR = "a[href]"  # soup fallback only (HTML listing is a JS shell)


def _extract_listing_links(soup: BeautifulSoup) -> list[str]:
    """Site-adaptable soup-fallback callback: this page's product URLs.

    Contract: ABSOLUTE urls, [] when none, NEVER raises. On today's shell HTML
    this returns [] (2 nav anchors, no products) — which correctly drives the
    shared module's ItemList check → ladder escalation → honest empty signal.
    """
    urls = []
    for link in soup.select(PRODUCT_LINK_SELECTOR):
        absolute_url = make_absolute_url(link.get("href", ""))
        if not absolute_url:
            continue
        if PRODUCT_URL_RE is not None and not PRODUCT_URL_RE.search(absolute_url):
            continue
        urls.append(absolute_url)
    return urls


def derive_search_term(listing_url: str) -> str:
    """`/bath` → "bath"; `/fluted-marble-bath-accessories-b281` →
    "fluted marble bath accessories" (item suffix stripped)."""
    path = urlparse(listing_url).path.strip("/")
    last = path.split("/")[-1] if path else ""
    last = re.sub(r"-[bd]\d+$", "", last, flags=re.IGNORECASE)
    return last.replace("-", " ").strip()


def _search_api_collect(query: str, started: float) -> tuple[list[str], dict]:
    """Paginate the SCA search API by offset until the reported total is reached
    or a page yields nothing new. Returns (absolute_urls, meta)."""
    collected: list[str] = []
    seen: set[str] = set()
    offset = 0
    total = None
    stop_reason = "no_next_link"

    while True:
        if time.monotonic() - started > DISCOVERY_DEADLINE_SECONDS:
            logger.warning("API discovery exceeded %ss deadline — stopping", DISCOVERY_DEADLINE_SECONDS)
            stop_reason = "navigate_error"
            break
        result = fetch_json(
            API_ITEMS_URL,
            params={
                "fieldset": SEARCH_FIELDSET,
                "q": query,
                "limit": SEARCH_PAGE_SIZE,
                "offset": offset,
            },
        )
        # Code Writer adapted [self-test fix]: create_fetch_json returns a
        # (payload, status) tuple on success, but a falsy SoftBlock (a 200
        # body under the armed SCRAPER_SOFT_BLOCK_MIN_BYTES floor) or None
        # otherwise — never unpack it blindly. The SCA API's legitimate
        # end-of-catalogue page IS a tiny body (`{"items":[]}`, ~215-318b),
        # so after a successful page this is a catalogue END, not a challenge:
        # this site has no anti-bot (measured) and escalating the ladder here
        # would only burn tiers. Only a failure before ANY page succeeded is
        # a real access error (→ soup fallback / navigate_error).
        payload = result[0] if isinstance(result, tuple) else None
        status = result[1] if isinstance(result, tuple) else None
        if not isinstance(payload, dict):
            if total is None:
                logger.error("API search fetch failed before any page (offset=%s, status=%s)",
                             offset, status)
                stop_reason = "navigate_error"
            else:
                logger.info(
                    "API search returned no/soft-blocked payload at offset=%s "
                    "after %s collected — treating as end of results", offset, len(collected))
                stop_reason = "short_page"
            break
        items = payload.get("items") or []
        if total is None:
            total = int(payload.get("total") or 0)
            logger.info("SCA search q=%r: API reports total=%s", query, total)
        added = 0
        dropped = 0
        for entry in items:
            component = str(entry.get("urlcomponent") or "").strip() if isinstance(entry, dict) else ""
            if not component:
                continue
            absolute_url = make_absolute_url(component, SITE_URL + "/")
            # Code Writer adapted [self-test fix]: the API items service returns
            # catalogue ITEMS by construction — no nav/category anchors to
            # over-match (that trap is HTML-anchor-specific, and the strict
            # `-[bd]NNN` slug shape it motivates DROPPED 9 real items on the
            # sample page: 70 served → 61 kept). The URL check here is hygiene
            # only: absolute, on-host, single path segment. The strict slug
            # regex stays on the soup fallback, where anchors DO include nav.
            try:
                parsed_path = urlparse(absolute_url).path
            except ValueError:
                dropped += 1
                continue
            if (
                not is_valid_site_url(absolute_url)
                or "//" in absolute_url[len(SITE_URL):]
                or parsed_path.count("/") > 1
                or parsed_path in ("/", "")
            ):
                dropped += 1
                if dropped <= 5:
                    logger.info("DROPPED urlcomponent %r (failed URL hygiene)", component)
                continue
            if absolute_url not in seen:
                seen.add(absolute_url)
                collected.append(absolute_url)
                added += 1
        logger.info(
            "SCA search q=%r offset=%s: served=%s items, %s kept, %s dropped by "
            "hygiene, %s new (total collected: %s)",
            query, offset, len(items), len(items) - dropped, dropped, added,
            len(collected),
        )
        if not items:
            stop_reason = "short_page"
            break
        if added == 0 and offset > 0:
            stop_reason = "no_new_items"
            break
        offset += SEARCH_PAGE_SIZE
        if total is not None and len(collected) >= total:
            break  # API's reported total reached — full extraction, no cap
    meta = {
        "stop_reason": stop_reason,
        "max_pages_hit": False,
        "discovered_urls": len(collected),
        "api_query": query,
        "api_total": total,
        "soft_block_escalations": 0,
        "retried_empty_discovery": False,
        "jsonld_fallback_pages": 0,
    }
    return collected, meta


def discover_urls_via_api(listing_url: str, started: float) -> tuple[list[str], dict]:
    """Phase 1 primary: SCA Items API. Derives the search term from the listing
    URL, tries term variants on zero yield, and unions the listing URL itself
    when it IS an item page (the seed group page case)."""
    query = derive_search_term(listing_url)
    attempts = [query] if query else []
    if query and " " in query:
        attempts.append(query.split()[-1])  # last word (e.g. "accessories")
    attempts.append(DEFAULT_QUERY)
    seen_attempts: list[str] = []
    for attempt in attempts:
        if attempt in seen_attempts:
            continue
        seen_attempts.append(attempt)
        urls, meta = _search_api_collect(attempt, started)
        if urls:
            return urls, meta
        logger.warning("API discovery yielded 0 URLs for q=%r (stop_reason=%s) — "
                       "trying next term variant", attempt, meta["stop_reason"])
    # The listing may itself be an item page (seed group URL) — union it in.
    if PRODUCT_URL_RE.search(listing_url) and is_valid_site_url(listing_url):
        logger.info("Listing URL is itself an item page — adding %s", listing_url)
        return [listing_url], {
            "stop_reason": "no_next_link",
            "max_pages_hit": False,
            "discovered_urls": 1,
            "api_query": query,
            "api_total": 0,
            "soft_block_escalations": 0,
            "retried_empty_discovery": False,
            "jsonld_fallback_pages": 0,
        }
    return [], {
        "stop_reason": "empty_first_page",
        "max_pages_hit": False,
        "discovered_urls": 0,
        "api_query": query,
        "api_total": 0,
        "soft_block_escalations": 0,
        "retried_empty_discovery": False,
        "jsonld_fallback_pages": 0,
    }


def run_phase1_discovery(listing_urls: list[str], soup_cfg: dict) -> tuple[list[str], dict]:
    """API-first discovery with the template's shared soup crawl as the
    zero-yield self-heal (hard rule 5: never succeed emptily while a known
    listing shape is available)."""
    started = time.monotonic()
    listing_url = listing_urls[0] if listing_urls else PRODUCT_LISTING_URL
    urls, meta = discover_urls_via_api(listing_url, started)
    if urls:
        meta["method"] = "sca_items_api"
        return urls, meta
    logger.warning("Phase 1 API discovery found 0 URLs — falling back to shared "
                   "soup listing discovery on %s", listing_urls)
    fallback_urls, fallback_meta = discover_listing_urls_with_retry(
        fetch_page, listing_urls, _extract_listing_links, **soup_cfg,
    )
    if fallback_urls:
        fallback_meta["method"] = "soup_listing_fallback"
        return fallback_urls, fallback_meta
    meta["retried_empty_discovery"] = bool(fallback_meta.get("retried_empty_discovery"))
    meta["method"] = "api_then_soup_both_empty"
    logger.warning("Both API and soup discovery returned 0 URLs (stop_reason=%s)",
                   meta.get("stop_reason"))
    return urls, meta


# ═══════════════════════════════════════════════════════════════════════════════
# INPUT HANDLING
# ═══════════════════════════════════════════════════════════════════════════════

def load_urls_from_file(filepath: str) -> list[str]:
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("urls", [])


def save_urls_to_file(filepath: str, urls: list[str]) -> None:
    # [job-77] A 0-URL discovery must not DESTROY an existing seed file.
    if not urls and os.path.exists(filepath):
        logger.warning(f"Refusing to overwrite {filepath} with an empty URL list")
        return
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump({"urls": urls}, f, indent=2, ensure_ascii=False)
    logger.info(f"Saved {len(urls)} URLs to {filepath}")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

# ── CLI CONTRACT — keep every template flag; ADD never replace ───────────────
def main():
    global SRC_URL

    parser = argparse.ArgumentParser(description=f"HTTP scraper for {SITE_NAME}")
    parser.add_argument("--sample", action="store_true", help="Scrape only 5 products")
    parser.add_argument("--limit", type=int, default=None, help="Max products to scrape")
    parser.add_argument("--input", type=str, default=None, help="Path to input URLs JSON file")
    parser.add_argument("--urls", nargs="+", default=None, help="Product URLs as arguments")
    parser.add_argument(
        "--discover-only",
        action="store_true",
        help="Run Phase 1 discovery only; emit discovery_coverage, skip Phase 2 extraction",
    )
    parser.add_argument(
        "--fresh-discovery",
        action="store_true",
        help="Ignore any discovery cache and re-run Phase 1 (the SCA API loop has "
        "no checkpoint file; input_urls.json is rewritten on every discovery)",
    )
    # Code Writer adapted (list_page job contract): --listing-url overrides the
    # default listing; --no-proxy is accepted (direct HTTP is the measured
    # transport — the shared ladder already starts at tier "none").
    parser.add_argument(
        "--listing-url", type=str, default=None,
        help="Listing/search page URL to discover product URLs from (overrides the default)",
    )
    parser.add_argument(
        "--no-proxy", action="store_true",
        help="Force direct HTTP with no proxy (already the default for this site; "
        "the proxy ladder stays available only for hard-block escalation)",
    )
    args = parser.parse_args()

    if args.input:
        # [job-60 zquiet] Resolve relative seed-file values against the scraper's
        # own directory (the runner's CWD is not the scraper's directory).
        args.input = os.path.join(SCRIPT_DIR, args.input)

    if args.no_proxy:
        logger.info("--no-proxy accepted: direct HTTP is the measured transport for "
                    "%s (ladder starts at tier 'none')", SITE_URL)

    start_time = time.time()

    logger.info("=" * 80)
    logger.info(f"Starting scraper for {SITE_NAME}")
    logger.info(f"Site: {SITE_URL}")
    logger.info(f"Listing URL: {PRODUCT_LISTING_URL}")
    logger.info(f"Output: {OUTPUT_FILE}")
    logger.info("=" * 80)

    if args.fresh_discovery:
        logger.info(
            "--fresh-discovery: forcing Phase 1 discovery (the SCA API loop keeps "
            "no checkpoint; input_urls.json is rewritten on every discovery)"
        )

    product_urls = []
    discovered_urls_raw = 0
    ran_phase1 = False
    skipped_reason: Optional[str] = None
    discovery_meta: dict = {"stop_reason": "skipped", "max_pages_hit": False}

    # Shared-module discovery config (data, not code — soup fallback path).
    _discovery_cfg = {
        "page_param": PAGE_PARAM_NAME,
        "page_size": PAGE_SIZE,
        "offset_mode": OFFSET_MODE,
        "extra_page_params": EXTRA_PAGE_PARAMS,
        "url_filter": PRODUCT_URL_RE,
        "max_pages": MAX_PAGES,
        "deadline_s": DISCOVERY_DEADLINE_SECONDS,
        "retry_delay_s": EMPTY_DISCOVERY_RETRY_DELAY_S,
    }

    # Phase 1: URL discovery (or load from input).
    # F6 DETERMINISTIC DISCOVERY GATE (env-var) — VERBATIM template gate.
    _env_listing = os.environ.get("SCRAPER_LISTING_URL", "").strip()
    if _env_listing:
        PRODUCT_LISTING_URLS[:] = [_env_listing]
        SRC_URL = _env_listing
        logger.info("Env gate: SCRAPER_LISTING_URL set — forcing Phase 1 discovery on %s",
                    _env_listing[:80])

    # Code Writer adapted: --listing-url feeds the SAME discovery branch (it is
    # this list_page job's navigation source, per the CLI hard contract).
    if args.listing_url:
        PRODUCT_LISTING_URLS[:] = [args.listing_url]
        SRC_URL = args.listing_url
        logger.info("--listing-url set — Phase 1 discovery on %s", args.listing_url[:80])

    if args.discover_only or _env_listing or args.listing_url or args.fresh_discovery:
        logger.info("Discovery mode (%s): running Phase 1, skipping Phase 2 extraction",
                    "env-gate" if _env_listing else "flag")
        product_urls, discovery_meta = run_phase1_discovery(
            PRODUCT_LISTING_URLS, _discovery_cfg
        )
        discovered_urls_raw = len(product_urls)
        ran_phase1 = True
        save_urls_to_file(INPUT_FILE, product_urls)
    elif args.urls:
        product_urls = args.urls
        skipped_reason = "url_list_mode"
    elif args.input:
        product_urls = load_urls_from_file(args.input)
        skipped_reason = "url_list_mode"
    else:
        # Code Writer adapted (navigation-scraper contract): the default no-args
        # run is Phase 1 discovery — never a silent input_urls.json fallback.
        logger.info("No input given. Discovering products from the listing page...")
        product_urls, discovery_meta = run_phase1_discovery(
            PRODUCT_LISTING_URLS, _discovery_cfg
        )
        discovered_urls_raw = len(product_urls)
        ran_phase1 = True
        save_urls_to_file(INPUT_FILE, product_urls)

    # Raw discovered count is captured BEFORE any sample/limit slicing.
    if not ran_phase1:
        discovered_urls_raw = len(product_urls)

    # Sample/limit only apply when Phase 2 extraction will run.
    if not args.discover_only:
        if args.sample:
            product_urls = product_urls[:5]
        if args.limit:
            product_urls = product_urls[: args.limit]

    logger.info(f"Total products to scrape: {len(product_urls)}")

    results = []
    failed = 0

    if not args.discover_only:
        # Phase 2: extract fields from each discovered item page via the SCA
        # details API. Sequential by design: the catalogue here is ~70 items, and
        # concurrency would defeat the phase2_instant_fail wall-clock floor.
        # Code Writer adapted: fetch_json (shared ladder) replaces fetch_page.
        phase2_start = time.monotonic()
        for i, url in enumerate(product_urls):
            slug = url.rstrip("/").rsplit("/", 1)[-1]
            result = fetch_json(
                API_ITEMS_URL, params={"fieldset": DETAILS_FIELDSET, "url": slug}
            )
            # Code Writer adapted [self-test fix]: never unpack a falsy result
            # — create_fetch_json returns (payload, status) on success but a
            # falsy SoftBlock/None otherwise (a tiny 200 body under the armed
            # min-bytes floor, or a network failure).
            if isinstance(result, tuple):
                payload, status_code = result
                items = (payload.get("items") or []) if isinstance(payload, dict) else []
                item = items[0] if items else None
                product = extract_product_from_api(item, payload, url, status_code, SRC_URL)
                product["id"] = i + 1
                # Emission filter (template verbatim): a row with NO substantive
                # field beyond the boilerplate keys is a page that rendered
                # nothing — soft-404s land here via their remarks-only row.
                _BK = {"url", "src_url", "scraped_at", "status_code", "remarks", "id", "title"}
                if any(v for k, v in product.items() if k not in _BK):
                    results.append(product)
                else:
                    logger.warning(f"No substantive data extracted from: {url} "
                                   f"({product.get('remarks') or 'empty page'})")
                    failed += 1
            else:
                # Code Writer adapted: a 200-but-empty details payload (the
                # dead-PDP signature for this API) or a failed fetch — emit the
                # soft-404 row with remarks and NO extracted data; count as failed.
                logger.error(f"Failed to fetch: {url}")
                failed += 1
                soft404 = {
                    "id": i + 1,
                    "title": "",
                    "price": "",
                    "currency": "",
                    "size": "",
                    "availability": "",
                    "url": url,
                    "src_url": SRC_URL,
                    "status_code": None,
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                    "remarks": "Soft 404: product not found",
                }
                results.append(soft404)

            if (i + 1) % 25 == 0:
                percent = ((i + 1) / len(product_urls)) * 100
                logger.info(f"Progress: [{i + 1}/{len(product_urls)}] ({percent:.1f}%)")
    else:
        logger.info("--discover-only: skipping Phase 2 extraction (results list left empty)")

    # [T3.13c/job-76 myhouse] Mechanical "fetch actually happened" detector.
    phase2_instant = False
    if not args.discover_only and product_urls:
        phase2_elapsed = time.monotonic() - phase2_start
        phase2_instant = phase2_instant_fail(
            phase2_elapsed, len(product_urls), DELAY_BETWEEN_REQUESTS
        )
        if phase2_instant:
            logger.warning(
                "PHASE2 INSTANT FAIL: %s items in %.2fs (< %.2fs floor at "
                "delay=%ss) — fetches never actually happened",
                len(product_urls), phase2_elapsed,
                len(product_urls) * DELAY_BETWEEN_REQUESTS * 0.5,
                DELAY_BETWEEN_REQUESTS,
            )

    # discovery_coverage block — contract §1.
    if ran_phase1:
        stop_reason = discovery_meta.get("stop_reason", "no_next_link")
    else:
        stop_reason = "skipped"

    discovery_coverage = {
        "stop_reason": stop_reason,
        "found": len(results),
        "discovered_urls": discovered_urls_raw,
        "expected_total": discovery_meta.get("api_total"),
        "dimensions_iterated": 0,
        "dimensions_total": 0,
        "max_pages_hit": discovery_meta.get("max_pages_hit", False),
        "ran_phase1": ran_phase1,
        "skipped_reason": skipped_reason,
        "soft_block_escalations": discovery_meta.get("soft_block_escalations", 0),
        "retried_empty_discovery": discovery_meta.get("retried_empty_discovery", False),
        "jsonld_fallback_pages": discovery_meta.get("jsonld_fallback_pages", 0),
        "phase2_instant_fail": phase2_instant,
        # Code Writer adapted: which engine produced the URL list.
        "method": discovery_meta.get("method", "sca_items_api"),
        "api_query": discovery_meta.get("api_query"),
    }

    output = {
        "site": {
            "name": SITE_NAME,
            "url": SITE_URL,
            "platform": PLATFORM,
            "scraping_method": SCRAPING_METHOD,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        },
        "products": results,
        "metadata": {
            "phase": "discovery" if args.discover_only else "extraction",
            "scraping_duration_seconds": round(time.time() - start_time, 2),
            "failed_products": failed,
            "rate_limit_delay": DELAY_BETWEEN_REQUESTS,
            "discovered_urls": discovered_urls_raw,
            "discovery_coverage": discovery_coverage,
        },
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    logger.info("=" * 80)
    logger.info("DISCOVERY COMPLETE (--discover-only)" if args.discover_only else "EXTRACTION COMPLETE")
    logger.info(f"Total: {len(results)}, Failed: {failed}")
    logger.info(
        f"Discovery coverage: stop_reason={stop_reason}, found={len(results)}, "
        f"discovered_urls={discovered_urls_raw}, ran_phase1={ran_phase1}, "
        f"skipped_reason={skipped_reason}"
    )
    logger.info(f"Duration: {round(time.time() - start_time, 2)}s")
    logger.info(f"Output: {OUTPUT_FILE}")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
