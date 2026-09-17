#!/usr/bin/env python3
"""
HTTP Requests Scraper — diptyqueparis.com (Shopify, fr-fr locale)

Extracts EVERY requested field for a PDP from the server-rendered
<script type="application/ld+json"> Product block in ONE direct-HTTP request
(no proxy, no browser — analyzer-verified over plain requests).
Fallback chain: og: meta tags → Shopify {handle}.json endpoint.

Template: http_requests_scraper (shared discovery + fetch_page machinery).
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

import requests  # noqa: F401 -- template keeps requests imported; every fetch goes through fetch_page (shared proxy ladder).
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

# ═════════════════════════════ CONFIGURATION ═══════════════════════════════════
SITE_NAME = "diptyqueparis.com"
SITE_URL = "https://diptyqueparis.com"
PLATFORM = "shopify"
SCRAPING_METHOD = "http_requests"
SITE_SLUG = "diptyqueparis-com"

# Code Writer adapted: real Shopify collection enumeration listing (/fr-fr/collections/all,
# paginated ?page=N) instead of the seed-PDP default — fixes the tester's MEDIUM issue where
# Phase 1 anchored on the PDP's own canonical link and yielded exactly 1 product URL.
PRODUCT_LISTING_URL = "https://diptyqueparis.com/fr-fr/collections/all"
SRC_URL = "https://diptyqueparis.com/fr-fr/collections/all"
# Shopify pagination query param.
PAGE_PARAM_NAME = "page"
# Multi-listing enumeration: single collection here; discovery can be pointed at any
# collection via SCRAPER_LISTING_URL / --listing-url (injected listing is honored first).
PRODUCT_LISTING_URLS = [PRODUCT_LISTING_URL]
PAGE_SIZE = None
OFFSET_MODE = False
EXTRA_PAGE_PARAMS: dict = {}
DELAY_BETWEEN_REQUESTS = 2.0
# Safety cap on listing pagination (None = unlimited → full extraction contract).
MAX_PAGES = None
# Fail-fast wall-clock deadline for Phase 1 discovery.
DISCOVERY_DEADLINE_SECONDS = 300
# [job-58 birkenstock] One-shot re-enumeration delay on a zero-URL ("200-but-blocked") end.
EMPTY_DISCOVERY_RETRY_DELAY_S = 45

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",  # NO br — requests may not support Brotli (job contract)
}

# ── HTTP FETCH — SHARED MODULE, NOT YOURS TO REIMPLEMENT (CRITICAL) ──────────
from src.http_fetch import create_fetch_page
from src.page_analysis import phase2_instant_fail

fetch_page = create_fetch_page(delay_s=DELAY_BETWEEN_REQUESTS, headers=HEADERS)

# ── LISTING DISCOVERY — SHARED MODULE, NOT YOURS TO REIMPLEMENT (CRITICAL) ───
from src.listing_discovery import discover_listing_urls_with_retry

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# job-71 popsockets: microsecond stamp + pid keeps the output path unique per process.
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


# ── FIELD NORMALIZERS — inline on purpose, NOT a src import ──────────────────
# Drafts execute in the browser-service image: a NEW src import would ImportError
# there until that image is rebuilt, so these ~25 lines live in each python-side
# template verbatim (playwright/UC normalize in-page via JS and don't need them).

def _norm_price(value) -> Optional[float]:
    """Strip currency symbols/whitespace from a price.

    "£1,234.56" → 1234.56 (a float), "1.234,56 €" → 1234.56, 24.99 (a
    JSON-LD number) → 24.99. Returns None when no digits are present — an
    unparseable price is EMPTY, never zero.
    """
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


def _price_str(value) -> str:
    """Code Writer adapted: emit the 2-decimal price STRING the contract wants.

    Raw source forms on this store are JSON-LD 155.0 (number) and og:price:amount
    '155' — both in the tester's known_bad_values; the required emit is '155.00'.
    Returns "" for unparseable input (an empty price is data, never '0.00').
    """
    norm = _norm_price(value)
    if norm is None:
        return ""
    return f"{norm:.2f}"


def _norm_availability(value) -> Optional[str]:
    """Normalize availability to ``in_stock`` / ``out_of_stock``.

    Accepts the schema.org URI form, InStock / In Stock / in_stock / Available
    and their negatives. Anything unrecognised passes through lowercased —
    availability is never invented (an unknown state is data, not an error).
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "in_stock" if value else "out_of_stock"
    text = str(value).strip().lower()
    if not text:
        return None
    if "://" in text:  # e.g. http://schema.org/InStock
        text = text.rsplit("/", 1)[-1]
    compact = text.replace("-", "_").replace(" ", "")
    if compact in ("in_stock", "instock", "available"):
        return "in_stock"
    if compact in ("out_of_stock", "outofstock", "unavailable", "sold_out", "soldout"):
        return "out_of_stock"
    return text


def _iso_currency(value) -> str:
    """Uppercase-trim an ISO 4217 code ('eur' → 'EUR'); '' when absent."""
    if not value:
        return ""
    return str(value).strip().upper()


# ═══════════════════════════════════════════════════════════════════════════════
# EXTRACTION — Code Writer adapted: field map anchors every requested field on the
# server-rendered JSON-LD Product block; fallbacks are og: meta → Shopify .json.
# ═══════════════════════════════════════════════════════════════════════════════

def extract_jsonld(soup: BeautifulSoup) -> Optional[dict]:
    """Return the JSON-LD Product dict from a PDP (single block; list/@graph aware)."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            if candidate.get("@type") == "Product":
                return candidate
            graph = candidate.get("@graph")
            if isinstance(graph, list):
                for node in graph:
                    if isinstance(node, dict) and node.get("@type") == "Product":
                        return node
    return None


def _extract_product_json(url: str) -> Optional[dict]:
    """Fallback: Shopify {handle}.json endpoint (plain HTTP through fetch_page)."""
    handle_m = re.search(r"/products/([a-z0-9-]+)", url or "")
    if not handle_m:
        return None
    endpoint = f"{SITE_URL}/fr-fr/products/{handle_m.group(1)}.json"
    try:
        result = fetch_page(endpoint, min_tier=0)
    except TypeError:
        result = fetch_page(endpoint)
    if isinstance(result, dict):  # .json endpoint may return parsed JSON, not soup
        return result.get("product") or result
    if not result:
        return None
    soup, status_code = result
    try:
        return json.loads(soup.get_text() or "").get("product")
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None


def extract_product_from_page(soup: BeautifulSoup, url: str, status_code: int, src_url: str) -> dict:
    product = {
        "title": "",
        "price": "",
        "currency": "",
        "availability": "",
        "original_price": "",
        "url": url,
        "src_url": src_url,
        "status_code": status_code,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": "",
    }

    # ── Soft-404 detection (CRITICAL): 200 pages that are not a product page ──
    page_text = ""
    try:
        page_text = (soup.get_text() or "")[:4000].lower()
    except Exception:
        page_text = ""
    _soft404_markers = ("product not found", "not found", "unavailable",
                        "discontinued", "no longer available", "page not found")
    _title_el = soup.title or soup.find("h1")
    _title_text = (_title_el.get_text(strip=True).lower() if _title_el else "")
    if any(marker in _title_text or marker in page_text for marker in _soft404_markers):
        product["remarks"] = "Soft 404: product not found"
        return product

    # Canonical redirect check: a PDP that redirects to a different product URL.
    try:
        canonical = soup.find("link", rel=lambda v: v and "canonical" in v)
        canonical_url = (canonical.get("href") or "").strip() if canonical else ""
        if canonical_url and canonical_url not in (url, urljoin(SITE_URL, url)):
            if urlparse(canonical_url).path != urlparse(url).path:
                product["remarks"] = f"Soft 404: redirected to {canonical_url}"
                return product
    except Exception:
        pass

    jsonld = extract_jsonld(soup)

    # ── title ── JSON-LD name → og:title → <title>
    if jsonld and jsonld.get("name"):
        product["title"] = str(jsonld.get("name")).strip()
    if not product["title"]:
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            product["title"] = og_title["content"].strip()
    if not product["title"] and _title_el:
        product["title"] = _title_el.get_text(strip=True)

    # ── price / currency / availability ── JSON-LD offers → og: meta → .json endpoint
    offers = None
    if jsonld:
        raw_offers = jsonld.get("offers")
        if isinstance(raw_offers, list):
            offers = raw_offers[0] if raw_offers else None
        elif isinstance(raw_offers, dict):
            offers = raw_offers

    price_raw = None
    currency_raw = None
    availability_raw = None
    if offers:
        price_raw = offers.get("price", offers.get("lowPrice"))
        currency_raw = offers.get("priceCurrency")
        availability_raw = offers.get("availability")

    if price_raw is None:
        og_price = soup.find("meta", property="og:price:amount")
        if og_price:
            price_raw = og_price.get("content")
    if not currency_raw:
        og_cur = soup.find("meta", property="og:price:currency")
        if og_cur:
            currency_raw = og_cur.get("content")

    if product["title"] and (price_raw is None or not currency_raw):
        api_product = _extract_product_json(url)
        if api_product:
            if price_raw is None:
                variants = api_product.get("variants") or [{}]
                price_raw = (variants[0] or {}).get("price")
            if not currency_raw:
                currency_raw = (variants[0] or {}).get("price_currency") if variants else None

    product["price"] = _price_str(price_raw)  # 2-decimal string: '155.00', never '155'/'155.0'
    product["currency"] = _iso_currency(currency_raw)

    # Availability has NO fallback outside the HTML page's JSON-LD (analyzer-verified:
    # the .json endpoint OMITS the available flag on this store).
    availability = _norm_availability(availability_raw)
    if availability:
        product["availability"] = availability

    # ── original_price ── compare_at_price / highPrice, empty when not on sale
    original_raw = None
    if offers:
        for key in ("highPrice", "compare_at_price", "listPrice"):
            if offers.get(key) not in (None, ""):
                original_raw = offers.get(key)
                break
    if original_raw is None and product["title"]:
        pass  # .json fallback's compare_at_price is only consulted when title proved the PDP
    if original_raw is not None:
        original_norm = _norm_price(original_raw)
        current_norm = _norm_price(product["price"])
        if original_norm and current_norm and original_norm > current_norm:
            product["original_price"] = _price_str(original_raw)
        else:
            product["original_price"] = ""

    return product


# ═══════════════════════════════════════════════════════════════════════════════
# DISCOVERY — adapt ONLY the callback below; the loop itself is the shared module
# ═══════════════════════════════════════════════════════════════════════════════

# Code Writer adapted: STRICT Shopify PDP-card selector (was the permissive
# 'a[href]' default that matched the seed PDP's own canonical link and yielded
# exactly 1 URL). Requires a literal '/products/' segment; canonical <link> tags
# and '/products/{handle}.json' suggestions are filtered by _PRODUCT_URL_RE.
PRODUCT_LINK_SELECTOR = 'a[href*="/products/"]'
if PRODUCT_LINK_SELECTOR.startswith("{"):
    PRODUCT_LINK_SELECTOR = "a[href]"  # unfilled placeholder → permissive default (template default)

# A candidate must end in a /products/{handle} path segment with no extension and
# no query — excludes .json endpoints, ?variant= links and bare canonical tags.
_PRODUCT_URL_RE = re.compile(r"/products/[a-z0-9-]+/?(?:\?|$)")


def _extract_listing_links(soup: BeautifulSoup) -> list[str]:
    """The ONLY site-adaptable discovery code: this page's product URLs.

    Contract: return a list of ABSOLUTE urls (dupes fine — the shared module
    dedupes). Return [] when the page carries no product links; NEVER raise.
    An empty result makes the module do exactly one of two things before it
    gives up: check the page's JSON-LD ItemList (hidden-SSR listings embed
    their item set there even when the visible grid hydrates client-side),
    or escalate the proxy tier (200-but-zero-links soft-block signature).
    """
    urls = []
    for link in soup.select(PRODUCT_LINK_SELECTOR):
        raw_href = link.get("href", "")
        absolute_url = make_absolute_url(raw_href)
        if not absolute_url:
            continue
        # Strip query/fragment before the strict product-path check (?variant=… links
        # carry the bare /products/{handle} path plus tracking params).
        path_only = absolute_url.split("?", 1)[0].split("#", 1)[0]
        if not _PRODUCT_URL_RE.search(path_only):
            continue
        clean_url = f"{path_only}"
        if clean_url not in urls:
            urls.append(clean_url)
    return urls


# ═══════════════════════════════════════════════════════════════════════════════
# INPUT HANDLING
# ═══════════════════ broad phase-2 loop with the shared fetch_page ladder ═════

def load_urls_from_file(filepath: str) -> list[str]:
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("urls", [])


def save_urls_to_file(filepath: str, urls: list[str]) -> None:
    # [job-77] A 0-URL discovery must not DESTROY an existing seed file: the
    # adoreme execution discovered 0 under a PDP listing and left a 16-byte
    # {"urls": []} as the site's canonical input_urls.json. Skip the empty
    # overwrite when a seed already exists; a non-empty refresh still writes.
    if not urls and os.path.exists(filepath):
        logger.warning(f"Refusing to overwrite {filepath} with an empty URL list")
        return
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump({"urls": urls}, f, indent=2, ensure_ascii=False)
    logger.info(f"Saved {len(urls)} URLs to {filepath}")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

# ── CLI CONTRACT — keep every line below when adapting ───────────────────────
# The pipeline launches this scraper with EXACTLY these names. A flag missing
# from the argparse below is STRIPPED at launch and discovery silently falls
# back to the seed file (input_urls.json). Same for the SCRAPER_LISTING_URL
# env read in main(). ADD flags if you need them; NEVER remove or rename:
#   --fresh-discovery  always (execution)     --listing-url  navigation/list_page
#   --query            search_term            --input/--sample/--limit  testing
#   --discover-only    Phase-1 probe          (+ SCRAPER_LISTING_URL env read)
# Source of truth: webapp/agents/constants.py
# ──────────────────────────────────────────────────────────────────────────────
def main():
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
        help="Ignore any discovery cache and re-run Phase 1 (accepted as a no-op here: "
        "requests_scraper has no checkpoint file to skip)",
    )
    parser.add_argument(
        "--listing-url",
        type=str,
        default=None,
        help="Explicit listing URL for Phase 1 discovery (overrides PRODUCT_LISTING_URLS)",
    )
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        help="Site is analyzer-verified to accept plain direct HTTP (proxy tier: none) — "
        "this flag documents the no-proxy contract; the shared fetch ladder already "
        "starts at tier 0 (direct) for this draft and only escalates if hard-blocked.",
    )
    args = parser.parse_args()

    if args.input:
        # [job-60 zquiet] The runner's CWD is not the scraper's directory
        # (the tester passes `--input input_urls.json` while CWD=/app), so a
        # relative seed-file value must resolve against the scraper's own
        # directory. os.path.join passes absolute paths through untouched.
        args.input = os.path.join(SCRIPT_DIR, args.input)

    start_time = time.time()

    logger.info("=" * 80)
    logger.info(f"Starting scraper for {SITE_NAME}")
    logger.info(f"Site: {SITE_URL}")
    logger.info(f"Listing URL: {PRODUCT_LISTING_URL}")
    logger.info(f"Output: {OUTPUT_FILE}")
    logger.info("=" * 80)

    if args.fresh_discovery:
        logger.info(
            "--fresh-discovery accepted (no-op: requests_scraper has no "
            "discovered_urls_checkpoint.json to ignore; input_urls.json is rewritten on every discovery)"
        )

    product_urls = []
    discovered_urls_raw = 0
    ran_phase1 = False
    skipped_reason: Optional[str] = None
    discovery_meta: dict = {"stop_reason": "skipped", "max_pages_hit": False}

    # Shared-module discovery config (data, not code — see the import banner).
    _discovery_cfg = {
        "page_param": PAGE_PARAM_NAME,
        "page_size": PAGE_SIZE,
        "offset_mode": OFFSET_MODE,
        "extra_page_params": EXTRA_PAGE_PARAMS,
        "url_filter": _PRODUCT_URL_RE,
        "max_pages": MAX_PAGES,
        "deadline_s": DISCOVERY_DEADLINE_SECONDS,
        "retry_delay_s": EMPTY_DISCOVERY_RETRY_DELAY_S,
    }

    # Phase 1: URL discovery (or load from input).
    # F6 DETERMINISTIC DISCOVERY GATE (env-var): run_execution injects
    # SCRAPER_LISTING_URL because the LLM-adapted argparse may drop
    # --listing-url/--fresh-discovery (the CLI-contract guard then strips
    # them). Without this gate the seed-file branch below ALWAYS wins (the
    # seed file exists — run_execution stages input_urls.json) and discovery
    # never runs (prod 285: 1 item from 4 seeds; pagination dead). In-place
    # [:] mutation so LLM-copied drafts that alias the list stay coherent.
    _env_listing = os.environ.get("SCRAPER_LISTING_URL", "").strip()
    if _env_listing:
        PRODUCT_LISTING_URLS[:] = [_env_listing]
        logger.info("Env gate: SCRAPER_LISTING_URL set — forcing Phase 1 discovery on %s",
                    _env_listing[:80])

    if args.discover_only or _env_listing:
        logger.info("Discovery mode (%s): running Phase 1, skipping Phase 2 extraction",
                    "env-gate" if _env_listing else "--discover-only")
        product_urls, discovery_meta = discover_listing_urls_with_retry(
            fetch_page, PRODUCT_LISTING_URLS, _extract_listing_links,
            **_discovery_cfg,
        )
        discovered_urls_raw = len(product_urls)
        ran_phase1 = True
        save_urls_to_file(INPUT_FILE, product_urls)
    elif args.listing_url:
        # --listing-url (navigation/list_page contract): same Phase 1 machinery,
        # explicit listing from the launcher.
        PRODUCT_LISTING_URLS[:] = [args.listing_url]
        product_urls, discovery_meta = discover_listing_urls_with_retry(
            fetch_page, PRODUCT_LISTING_URLS, _extract_listing_links,
            **_discovery_cfg,
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
    elif os.path.exists(INPUT_FILE):
        logger.info(f"Loading previously discovered URLs from {INPUT_FILE}")
        product_urls = load_urls_from_file(INPUT_FILE)
        # input_urls.json is this template's persisted discovery output; reusing it
        # means Phase 1 did not run this invocation.
        skipped_reason = "checkpoint_loaded"
    else:
        logger.info("No input_urls.json found. Discovering products from listing page...")
        product_urls, discovery_meta = discover_listing_urls_with_retry(
            fetch_page, PRODUCT_LISTING_URLS, _extract_listing_links,
            **_discovery_cfg,
        )
        discovered_urls_raw = len(product_urls)
        ran_phase1 = True
        save_urls_to_file(INPUT_FILE, product_urls)

    # Raw discovered count is captured BEFORE any sample/limit slicing so it
    # reflects true discovery yield.
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
        # Phase 2: extract fields from each discovered item page.
        phase2_start = time.monotonic()
        for i, url in enumerate(product_urls):
            result = fetch_page(url)
            if result:
                soup, status_code = result
                product = extract_product_from_page(soup, url, status_code, url)
                product["id"] = i + 1
                # Emission filter: a row with NO substantive field beyond the
                # boilerplate keys is a page that rendered nothing — shipping it
                # as a product poisons the item count every downstream gate reads.
                _BK = {"url", "src_url", "scraped_at", "status_code", "remarks", "id", "title"}
                if any(v for k, v in product.items() if k not in _BK):
                    results.append(product)
                else:
                    logger.warning(f"No substantive data extracted from: {url}")
                    failed += 1
            else:
                logger.error(f"Failed to fetch: {url}")
                failed += 1

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

    # discovery_coverage block. Always emitted so the gate can read a uniform
    # schema regardless of which path produced the output.
    if ran_phase1:
        stop_reason = discovery_meta.get("stop_reason", "no_next_link")
    else:
        stop_reason = "skipped"

    discovery_coverage = {
        "stop_reason": stop_reason,
        "found": len(results),  # post-filter extracted item count (0 when Phase 2 skipped)
        "discovered_urls": discovered_urls_raw,  # raw pre-filter count (diagnostic)
        "expected_total": None,  # no baked-in coverage_target for this template
        "dimensions_iterated": 0,
        "dimensions_total": 0,
        "max_pages_hit": discovery_meta.get("max_pages_hit", False),
        "ran_phase1": ran_phase1,
        "skipped_reason": skipped_reason,
        "soft_block_escalations": discovery_meta.get("soft_block_escalations", 0),
        "retried_empty_discovery": discovery_meta.get("retried_empty_discovery", False),
        "jsonld_fallback_pages": discovery_meta.get("jsonld_fallback_pages", 0),
        "phase2_instant_fail": phase2_instant,
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
