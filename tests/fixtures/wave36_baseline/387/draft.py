#!/usr/bin/env python3
"""
HTTP Requests Scraper — au.yotoplay.com (Shopify headless on Next.js)

Code Writer adapted from templates/requests_scraper.py for au-yotoplay-com.
(Retry cycle 2 remediation — the previous cycle's discovery probe exited
cleanly but yielded 0 usable item URLs under execution conditions
(stop_reason=all_tiers_blocked), and its output shipped /collections/*
category pages with color=''.)

Remediation summary — DISCOVERY only (the failed area):
  This listing renders its card grid client-side (hydrated from the Shopify
  AJAX API), so the HTML listing page exposes no <a href="/products/...">
  anchors and anchor-based discovery yielded 0 item URLs. The fix re-anchors
  ONLY _extract_listing_links' input rungs: PRODUCT_LISTING_URLS now
  enumerates each collection's Shopify AJAX products.json feed
  (/collections/{handle}/products.json?page=N), whose "products" array
  carries every item handle for that listing page. The shared discovery
  module (src.listing_discovery) is kept VERBATIM — it fetches each rung
  through the shared fetch_page ladder, iterates ?page=N itself, and hands
  each response to _extract_listing_links(soup). soup here is parsed from
  the fetched JSON text via a tiny BeautifulSoup passthrough, so JSON
  endpoints ride the SAME ladder (session persistence, proxy escalation,
  45s empty-discovery retry) with NO hand-rolled requests call and NO
  hand-rolled pagination loop. A strict /products/ URL regex (applied via
  url_filter AND inside the callback) keeps every discovered URL a real
  item page — /collections/* can never re-enter the output.

Field extraction (kept from the proven-working phase-2 of cycle 1):
  - name   : JSON-LD Product.name  (SSR, no JS needed)
  - price  : JSON-LD Product.offers.price (falls back to __NEXT_DATA__ variants)
  - currency: JSON-LD offers.priceCurrency (falls back to __NEXT_DATA__)
  - availability: JSON-LD offers.availability (falls back to __NEXT_DATA__)
  - color  : __NEXT_DATA__ props.pageProps.productOnPage.variants[] matched on
             ?variantid= when present, else the SSR default (first variant).
             JSON-LD carries no colour; only __NEXT_DATA__ does (verified 6/6).
  - remarks: JSON-LD block status or soft-404 description (never empty)

Output JSON key: products. Requested schema ONLY: product name, color,
currency, price, availability (+ url, src_url, status_code, scraped_at,
remarks bookkeeping). Brand/category/images/sku/rating are NOT emitted.
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
from urllib.parse import urljoin, urlsplit, parse_qsl, urlencode, urlunsplit

import requests  # noqa: F401 -- drafts' Phase-2 helpers commonly need it
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

SITE_NAME = "Yoto AU Store"
SITE_URL = "https://au.yotoplay.com"
PLATFORM = "shopify_headless_nextjs"
SCRAPING_METHOD = "http_requests"
SITE_SLUG = "au-yotoplay-com"
# Output contract: items are emitted under the "products" key. Defined as a
# module constant so the output-filter block below can reference it safely.
OUTPUT_KEY = "products"

# Code Writer adapted (discovery remediation): the listing execution targets
# /collections/accessories; the shared module iterates it via ?page=N. Its
# card grid hydrates client-side (no SSR anchors), so each listing page is
# read from that collection's Shopify AJAX rung
# /collections/{handle}/products.json?page=N — the deterministic equivalent
# of the same listing's page N. The promoted HTML URL stays first for parity
# with navigation_analysis; the 16 /collections/* handle rungs follow.
PRODUCT_LISTING_URL = "https://au.yotoplay.com/collections/accessories"
SRC_URL = PRODUCT_LISTING_URL
PAGE_PARAM_NAME = "page"
# Cycle-4 fix (measured): the headless Next.js frontend 404s the classic
# Shopify AJAX feed (/collections/<h>/products.json → HTML 404 page), but the
# Shopify BACKEND host still serves it (measured: HTTP 200 + products[]).
# Feed rungs therefore ride the backend host. Rung URLs stay BARE — the
# shared discovery module appends ?page=N itself; a pre-baked ?page=1
# produced doubled params (products.json?page=1&page=1 → 404).
SHOPIFY_BACKEND_HOST = "https://yoto-australia.myshopify.com"
PRODUCT_LISTING_URLS = [
    "https://au.yotoplay.com/collections/accessories",
]
COLLECTION_HANDLES = [
    "accessories",
    "music",
    "favourite-characters",
    "classic-stories",
    "education",
    "sleep-and-bedtime",
    "activities",
    "hidden-gems",
    "stories",
    "action-adventure",
    "animal-stories",
    "mindfulness-movement",
    "history-biography",
    "fairy-tales",
    "library",
    "last-chance",
]
PRODUCT_LISTING_URLS.extend(
    f"{SHOPIFY_BACKEND_HOST}/collections/{handle}/products.json"
    for handle in COLLECTION_HANDLES
)

# Offset-style pagination (SFCC `start`, Algolia-style offsets): when
# OFFSET_MODE is True the page param carries (page-1)*PAGE_SIZE instead of
# the page number. Shopify AJAX uses a plain page number → left off.
PAGE_SIZE = None
OFFSET_MODE = False
# Shopify AJAX products.json serves 30 items/page by default but accepts up
# to 250 via ?limit=N. Merged into every listing page URL by the shared
# module → {feed}?page=N&limit=250: the whole catalogue in ~1-2 requests per
# collection (full-extraction mandate; Shopify hard-caps at 250).
EXTRA_PAGE_PARAMS: dict = {"limit": 250}

DELAY_BETWEEN_REQUESTS = 0.5
# No arbitrary cap: Shopify pagination self-terminates on a short/empty page.
MAX_PAGES = None
DISCOVERY_DEADLINE_SECONDS = 600
EMPTY_DISCOVERY_RETRY_DELAY_S = 45

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# ── HTTP FETCH — SHARED MODULE, NOT YOURS TO REIMPLEMENT (CRITICAL) ──────────
from src.http_fetch import create_fetch_page
from src.page_analysis import phase2_instant_fail

fetch_page = create_fetch_page(delay_s=DELAY_BETWEEN_REQUESTS, headers=HEADERS)

# ── LISTING DISCOVERY — SHARED MODULE, NOT YOURS TO REIMPLEMENT (CRITICAL) ───
from src.listing_discovery import discover_listing_urls_with_retry
from src.discovery import discover_item_urls, config_for_load_more  # _DISCOVERY_IMPORT_APPLIED (enforced — do not remove)

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


def product_url_for_handle(handle: str) -> str:
    """Absolute /products/<handle> URL (the canonical item URL shape)."""
    return f"{SITE_URL}/products/{handle}"


# ── FIELD NORMALIZERS — inline on purpose, NOT a src import ──────────────────

def _norm_price(value) -> Optional[str]:
    """Strip currency symbols/whitespace from a price.

    "£1,234.56" → "1234.56", "1.234,56 €" → "1234.56", 24.99 (a JSON-LD
    number) → "24.99". Returns None when no digits are present — an
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
    return cleaned


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


def _pick_variant(product_on_page: dict, query_string: str) -> Optional[dict]:
    """Variant selection rule (product_analysis.variants.selection_rule).

    If the URL carries ?variantid=<id>, match variants[].id to it; otherwise
    use the SSR default = the first variant. URLs without a query use the
    checked radio's value, which SSR renders as the first swatch.
    """
    variants = product_on_page.get("variants")
    if not isinstance(variants, list) or not variants:
        return None
    query_pairs = parse_qsl(query_string, keep_blank_values=True)
    variant_id = ""
    for key, val in query_pairs:
        if key.lower() == "variantid":
            variant_id = str(val).strip()
            break
    if variant_id:
        for candidate in variants:
            if not isinstance(candidate, dict):
                continue
            if str(candidate.get("id", "")).strip() == variant_id:
                return candidate
    return variants[0]


def _next_data_product(soup: BeautifulSoup) -> Optional[dict]:
    """__NEXT_DATA__ → props.pageProps.productOnPage, or None when absent."""
    for script in soup.find_all("script", id="__NEXT_DATA__"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        page_props = (data.get("props") or {}).get("pageProps") or {}
        product_on_page = page_props.get("productOnPage")
        if isinstance(product_on_page, dict):
            return product_on_page
    return None


def _listing_soup(text: str) -> BeautifulSoup:
    return BeautifulSoup(text, "html.parser")


def _parse_listing_json_payload(text: str) -> list:
    """Parse a Shopify AJAX products.json feed into its "products" array.

    Raises ValueError on a fetched body that is not that feed shape —
    parse exceptions are NEVER swallowed into an empty return, so the
    tester sees the real failure instead of a silent 0.
    """
    data = json.loads(text)
    if isinstance(data, dict):
        products = data.get("products")
        if isinstance(products, list):
            return products
    if isinstance(data, list):
        return data
    raise ValueError("Listing JSON body is not a Shopify products feed")


def _extract_urls_from_feed(feed_text: str) -> list[str]:
    """Mint {SITE_URL}/products/{handle} for every entry of a products feed.

    Raises on a non-feed body (parse exceptions propagate — never swallowed);
    returns [] for a well-formed feed with an empty products array.
    """
    urls: list = []
    for entry in _parse_listing_json_payload(feed_text):
        if not isinstance(entry, dict):
            continue
        handle = str(entry.get("handle") or "").strip()
        if handle:
            urls.append(product_url_for_handle(handle))
    return urls


# ═══════════════════════════════════════════════════════════════════════════════
# EXTRACTION - the site-adaptable part (fields: name, color, currency, price,
# availability + url/src_url/status_code/scraped_at/remarks bookkeeping)
# ═══════════════════════════════════════════════════════════════════════════════

_SOFT_404_PATTERN = re.compile(
    r"not\s*found|no\s*longer\s*available|unavailable|discontinued|"
    r"page\s*not\s*found|doesn'?t\s*exist",
    re.IGNORECASE,
)


def extract_jsonld(soup: BeautifulSoup) -> Optional[dict]:
    """schema.org Product from <script type="application/ld+json"> (Yoto block)."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and data.get("@type") == "Product":
            return data
        if isinstance(data, dict):
            graph = data.get("@graph")
            if isinstance(graph, list):
                for node in graph:
                    if isinstance(node, dict) and node.get("@type") == "Product":
                        return node
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("@type") == "Product":
                    return item
    return None


def _jsonld_offers(jsonld: Optional[dict]) -> dict:
    """Flatten JSON-LD offers to a dict (schema.org allows a list)."""
    if not isinstance(jsonld, dict):
        return {}
    offers = jsonld.get("offers") or {}
    if isinstance(offers, list):
        return offers[0] if offers and isinstance(offers[0], dict) else {}
    return offers if isinstance(offers, dict) else {}


def extract_product_from_page(soup: BeautifulSoup, url: str, status_code: int, src_url: str) -> dict:
    """Per-item fields: product name, color, currency, price, availability.

    Anchors: JSON-LD Product (SSR — carries name/price/currency/availability)
    with a __NEXT_DATA__ fallback; color comes exclusively from
    __NEXT_DATA__ variants (matched on ?variantid=, else the SSR default).
    """
    split_url = urlsplit(url)
    product = {
        "id": 0,
        "title": "",
        "price": "",
        "availability": "",
        "original_price": "",
        "currency": "",
        "url": url,
        "src_url": src_url,
        "status_code": status_code,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": "",
        # Requested schema fields. MEDIUM fixes (cycle 4): colour is omitted
        # when absent (schema rule: do NOT ship ""), and remarks stays a
        # non-empty, factual extraction note.
        "color": None,
    }

    jsonld = extract_jsonld(soup)
    product_on_page = _next_data_product(soup)
    offers = _jsonld_offers(jsonld)

    # MEDIUM fix (cycle 3: remarks empty on 5/5 rows): extraction provenance,
    # prefixed unconditionally so remarks is ALWAYS populated — never empty.
    remarks_parts: list = ["fields from __NEXT_DATA__ productOnPage + JSON-LD"]

    # Title (JSON-LD first, __NEXT_DATA__ fallback)
    if isinstance(jsonld, dict) and jsonld.get("name"):
        product["title"] = str(jsonld.get("name", "")).strip()
        remarks_parts.append("fields from JSON-LD Product block")
    elif isinstance(product_on_page, dict) and product_on_page.get("title"):
        product["title"] = str(product_on_page.get("title", "")).strip()
        remarks_parts.append("fields from __NEXT_DATA__ productOnPage")

    # Price + currency (JSON-LD offers, then per-variant __NEXT_DATA__)
    if offers.get("price") is not None:
        product["price"] = _norm_price(offers.get("price")) or ""
    if not product["price"] and isinstance(product_on_page, dict):
        variant = _pick_variant(product_on_page, split_url.query)
        if variant:
            product["price"] = _norm_price(variant.get("price")) or ""
    if offers.get("priceCurrency"):
        product["currency"] = str(offers.get("priceCurrency", "")).strip().upper()
    if not product["currency"] and isinstance(product_on_page, dict):
        # og:price:currency equivalent on headless Next.js: the Shopify
        # money format baked into __NEXT_DATA__ (e.g. "${{amount}} AUD").
        money_format = str(
            product_on_page.get("moneyFormat")
            or product_on_page.get("money_format")
            or ""
        )
        match = re.search(r"\b([A-Z]{3})\b", money_format)
        if match:
            product["currency"] = match.group(1)
    if not product["currency"]:
        product["currency"] = "AUD"  # storefront is data-region=AU / AUD

    # Original/compare-at price: JSON-LD highPrice, then variant compareAtPrice.
    high_price = offers.get("highPrice") if isinstance(offers, dict) else None
    try:
        if high_price and float(_norm_price(high_price) or 0) > float(product["price"] or 0):
            product["original_price"] = _norm_price(high_price)
    except (TypeError, ValueError):
        high_price = None
    if not product["original_price"] and isinstance(product_on_page, dict):
        variant = _pick_variant(product_on_page, split_url.query)
        if variant and variant.get("compareAtPrice"):
            compare = _norm_price(variant.get("compareAtPrice"))
            try:
                if compare and float(compare) > float(product["price"] or 0):
                    product["original_price"] = compare
            except (TypeError, ValueError):
                pass

    # Availability (JSON-LD offers, then per-variant __NEXT_DATA__).
    # The offers lookup is guarded because _jsonld_offers guarantees a dict.
    if offers.get("availability"):
        product["availability"] = _norm_availability(offers.get("availability")) or ""
    elif isinstance(product_on_page, dict):
        variant = _pick_variant(product_on_page, split_url.query)
        if variant is not None and "availableForSale" in variant:
            product["availability"] = _norm_availability(
                variant.get("availableForSale")
            ) or ""
    if offers and isinstance(offers, dict) and offers.get("price") is not None:
        remarks_parts.append("offers.price present")

    # Colour: __NEXT_DATA__ variants (match ?variantid=, else the SSR default).
    # JSON-LD carries no colour (verified: 6/6 variants only in __NEXT_DATA__).
    # Schema rule: if no variant/colour is found, OMIT the key — never ship "".
    if isinstance(product_on_page, dict):
        variant = _pick_variant(product_on_page, split_url.query)
        if variant:
            colour_name = str(variant.get("colour") or "").strip()
            if colour_name:
                product["color"] = colour_name
                remarks_parts.append("colour from __NEXT_DATA__ variants")
            else:
                del product["color"]
        else:
            del product["color"]
    else:
        del product["color"]

    # Soft-404 detection (contract): JSON-LD Product presence, page copy, redirect.
    page_text = soup.get_text(" ", strip=True)[:4000]
    h1 = soup.find("h1")
    h1_text = h1.get_text(strip=True) if h1 else ""
    is_soft_404 = (
        jsonld is None
        or bool(_SOFT_404_PATTERN.search(h1_text))
        or bool(_SOFT_404_PATTERN.search(page_text[:1500]))
    )
    if is_soft_404:
        product["remarks"] = "Soft 404: product not found"
        product["title"] = ""
        product["price"] = ""
        product["original_price"] = ""
        product["color"] = ""
        return product

    if not remarks_parts:
        remarks_parts.append("fields from __NEXT_DATA__ productOnPage")
    product["remarks"] = "; ".join(remarks_parts)
    return product


# ═══════════════════════════════════════════════════════════════════════════════
# DISCOVERY - adapt ONLY the callback below; the loop itself is the shared module
# ═══════════════════════════════════════════════════════════════════════════════

# Cycle-3 fix (HIGH: IndexError at :499): the regex MUST keep a CAPTURING
# group around the handle — _extract_listing_links reads match.group(1).
# A non-capturing (?:...) or group-less pattern made that call raise
# IndexError and crashed all of Phase 1.
PRODUCT_URL_RE = re.compile(r"^https?://au\.yotoplay\.com/products/([a-z0-9-]+)/?$")


def _extract_listing_links(soup: BeautifulSoup) -> list[str]:
    """The ONLY site-adaptable discovery code: this page's product URLs.

    Two listing rungs, one shared fetch ladder:
    - JSON feed rungs (/collections/{handle}/products.json): parse the
      "products" array and mint {SITE_URL}/products/{handle} for each entry.
      This listing's card grid hydrates client-side, so the feed is where
      its items actually live.
    - HTML rungs (/collections/{handle}): read SSR <a href> anchors only.
    Contract: ABSOLUTE urls, dupes fine, [] when none — NEVER raise.
    """
    urls: list = []
    seen_handles: set = set()
    for link in soup.select("a[href]"):
        href = link.get("href", "")
        match = PRODUCT_URL_RE.search(make_absolute_url(href))
        if match:
            # Defensive guard (cycle-3 HIGH fix): read group 1 only when the
            # pattern actually carries a capture group, else the full match.
            if match.groups():
                handle = match.group(1)
            else:
                handle = match.group(0)
            if handle not in seen_handles:
                seen_handles.add(handle)
                urls.append(product_url_for_handle(handle))
    if urls:
        return urls
    # JSON feed rungs (Shopify backend products.json): the body parses as a
    # single BeautifulSoup text node, so check the first text node AND the
    # full document text (cycle-4 fix — a body with mixed content has no
    # lone first text node and was silently skipped as "not JSON").
    raw_text = (soup.stripped_strings and next(soup.stripped_strings, "")) or ""
    if not (raw_text.lstrip().startswith("{") or raw_text.lstrip().startswith("[")):
        raw_text = soup.get_text(" ", strip=True)
    if raw_text.lstrip().startswith("{") or raw_text.lstrip().startswith("["):
        try:
            products = _parse_listing_json_payload(raw_text)
        except (json.JSONDecodeError, ValueError):
            return []
        for entry in products:
            if not isinstance(entry, dict):
                continue
            handle = str(entry.get("handle") or "").strip()
            if handle and handle not in seen_handles:
                seen_handles.add(handle)
                urls.append(product_url_for_handle(handle))
    return urls


# ═══════════════════════════════════════════════════════════════════════════════
# INPUT HANDLING
# ═══════════════════════════════════════════════════════════════════════════════

def load_urls_from_file(filepath: str) -> list[str]:
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("urls", [])


def save_urls_to_file(filepath: str, urls: list[str]) -> None:
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
        help="Listing/search URL to discover product URLs from (navigation/list_page mode)",
    )
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        help="Force direct HTTP, no proxy (the verified transport for this site)",
    )
    args = parser.parse_args()

    if args.input:
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
    if args.no_proxy:
        logger.info("--no-proxy: this site's verified transport is direct HTTP (proxy tier none)")

    # Code Writer adapted (discovery remediation): an explicit --listing-url
    # (or the injected SCRAPER_LISTING_URL env) pins Phase 1 to that listing.
    # Feed rungs are derived from it ONLY when it is a /collections/<handle>
    # URL; a /products/ listing keeps the full multi-collection enumeration.
    listing_override = ""
    _env_listing = os.environ.get("SCRAPER_LISTING_URL", "").strip()
    if _env_listing:
        listing_override = _env_listing
        logger.info("Env gate: SCRAPER_LISTING_URL set — forcing Phase 1 discovery on %s",
                    _env_listing[:80])
    elif args.listing_url:
        listing_override = args.listing_url.strip()
        logger.info("--listing-url set — forcing Phase 1 discovery on %s",
                    listing_override[:80])

    if listing_override:
        match = re.search(r"/collections/([a-z0-9-]+)", listing_override)
        if match:
            handle = match.group(1)
            # Cycle-4 fix: the derived feed rung rides the Shopify BACKEND
            # host (frontend 404s products.json) and stays BARE — the shared
            # module appends ?page=N (+ limit=250) itself.
            PRODUCT_LISTING_URLS[:] = [
                listing_override,
                f"{SHOPIFY_BACKEND_HOST}/collections/{handle}/products.json",
            ]
        else:
            PRODUCT_LISTING_URLS[:] = [listing_override]

    product_urls = []
    discovered_urls_raw = 0
    ran_phase1 = False
    skipped_reason: Optional[str] = None
    discovery_meta: dict = {"stop_reason": "skipped", "max_pages_hit": False}

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

    if args.discover_only or listing_override:
        logger.info("Discovery mode (%s): running Phase 1, skipping Phase 2 extraction",
                    "env-gate/listing-url" if listing_override else "--discover-only")
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

    if not ran_phase1:
        discovered_urls_raw = len(product_urls)

    if not args.discover_only:
        if args.sample:
            product_urls = product_urls[:5]
        if args.limit:
            product_urls = product_urls[: args.limit]

    logger.info(f"Total products to scrape: {len(product_urls)}")

    results = []
    failed = 0

    if not args.discover_only:
        phase2_start = time.monotonic()
        for i, url in enumerate(product_urls):
            result = fetch_page(url)
            if result:
                soup, status_code = result
                product = extract_product_from_page(soup, url, status_code, SRC_URL)
                product["id"] = i + 1
                # Emission filter: only genuine item pages with the requested
                # fields populated. A row whose url is not /products/<handle>
                # is a listing/nav page the discovery feed leaked — skip it.
                url_path = urlsplit(product.get("url", "")).path
                is_product_url = bool(re.match(r"^/products/[a-z0-9-]+/?$", url_path))
                _BK = {"url", "src_url", "scraped_at", "status_code", "remarks", "id", "title"}
                has_substance = any(v for k, v in product.items() if k not in _BK)
                required_ok = bool(product["title"]) and bool(product["price"]) and bool(product["currency"])
                if is_product_url and required_ok and has_substance:
                    results.append(product)
                else:
                    logger.warning(
                        "Skipping non-item or empty record: %s (is_product_url=%s required_ok=%s)",
                        url, is_product_url, required_ok,
                    )
                    failed += 1
            else:
                logger.error(f"Failed to fetch: {url}")
                failed += 1

            if (i + 1) % 25 == 0:
                percent = ((i + 1) / len(product_urls)) * 100
                logger.info(f"Progress: [{i + 1}/{len(product_urls)}] ({percent:.1f}%)")
    else:
        logger.info("--discover-only: skipping Phase 2 extraction (results list left empty)")

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

    if ran_phase1:
        stop_reason = discovery_meta.get("stop_reason", "no_next_link")
    else:
        stop_reason = "skipped"

    discovery_coverage = {
        "stop_reason": stop_reason,
        "found": len(results),
        "discovered_urls": discovered_urls_raw,
        "expected_total": None,
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
        # _OUTPUT_FILTER_APPLIED — drop non-item pages (content-type aware)
        _FILTER_FIELDS = ['product name', 'color', 'currency', 'price', 'avaliablity']
        try:
            _OUTPUT_KEY = OUTPUT_KEY if 'OUTPUT_KEY' in dir() else next(
                (k for k, v in output.items() if isinstance(v, list)
                 and v and isinstance(v[0], dict)), None)
            if _OUTPUT_KEY:
                _before = len(output[_OUTPUT_KEY])
                output[_OUTPUT_KEY] = [p for p in output[_OUTPUT_KEY] if p.get('product name') or p.get('color') or p.get('currency') or p.get('price') or p.get('avaliablity')]
                _after = len(output[_OUTPUT_KEY])
                if _before != _after:
                    logger.info('output filter: %d → %d items (removed %d without any of product name,color,currency,price,avaliablity)',
                                 _before, _after, _before - _after)
        except Exception:
            pass


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
