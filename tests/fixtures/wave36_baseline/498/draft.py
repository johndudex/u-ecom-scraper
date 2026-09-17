#!/usr/bin/env python3
"""
HTTP Requests Scraper — Briscoes NZ (briscoes.co.nz)

Adapted from templates/requests_scraper.py by Code Writer Agent.
Site: Magento PWA Studio (Venia-based React SPA).

TRANSPORT FACTS (measured by probe, 2026-09):
- The <head> of every page is server-side rendered (react-helmet): og:* meta
  tags + JSON-LD are present in the RAW HTTP response. The <body> is a React
  shell ("Oops! JavaScript is disabled") — body CSS selectors are useless.
- PDP head carries og:price:amount (SALE price), og:price:currency,
  og:availability, og:title, og:description, plus a JSON-LD Product block
  whose offers.priceSpecification holds the ListPrice (was-price).
- The CATEGORY LISTING page (/kitchen/kitchen-storage/) is a pure JS shell:
  0 anchors, 0 inline product URLs, no JSON-LD ItemList, and ?page=N is
  ignored — the shared HTML discovery module cannot see items there.
- DISCOVERY RUNG (measured 200): the store's Magento GraphQL endpoint
  (POST /graphql) is open and returns full product payloads with
  products(filter: {category_id}, pageSize, currentPage) pagination.
  This is the site's own listing mechanism (the PWA storefront fetches
  exactly this), so Phase 1 uses it, scoped to the promoted listing's
  category. Fallback: sitemap.xml → product URLs. Last resort: the shared
  HTML listing-discovery module (honest zero on this site).

Proxy: NONE — the probe verified direct HTTP reaches this site with no
anti-bot challenge (scraper_analysis: proxy tier "none", "Do NOT use any
proxy"). --no-proxy is accepted for contract compliance (no-op: this
scraper never proxies).

Usage:
    python3 scraper_draft.py                    # full run: discover + extract
    python3 scraper_draft.py --sample           # 5 products from seed file
    python3 scraper_draft.py --input urls.json  # explicit input file
    python3 scraper_draft.py --discover-only    # Phase 1 only
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
from urllib.parse import urljoin

import requests  # noqa: F401 -- kept for template parity (PDP transport)
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

SITE_NAME = "Briscoes NZ"
SITE_URL = "https://www.briscoes.co.nz"
PLATFORM = "magento_pwa_studio"
SCRAPING_METHOD = "http_requests"
SITE_SLUG = "briscoes-co-nz"

PRODUCT_LISTING_URL = "https://www.briscoes.co.nz/kitchen/kitchen-storage/"
SRC_URL = PRODUCT_LISTING_URL
GRAPHQL_URL = "https://www.briscoes.co.nz/graphql"
# search_term fallback (nav analysis: search supported). Empty = use listing.
DEFAULT_QUERY = ""

PAGE_PARAM_NAME = "page"  # unused by GraphQL pagination; kept for the shared
# HTML-discovery fallback call (the site ignores it — harmless).
PRODUCT_LISTING_URLS = [PRODUCT_LISTING_URL]
PAGE_SIZE = None
OFFSET_MODE = False
EXTRA_PAGE_PARAMS: dict = {}
DELAY_BETWEEN_REQUESTS = 1.5
MAX_PAGES = None  # full extraction mandate — GraphQL total_pages bounds it
DISCOVERY_DEADLINE_SECONDS = 420
EMPTY_DISCOVERY_RETRY_DELAY_S = 45

# GraphQL listing page size: the source's products query accepts large pages;
# 200 keeps request count low while staying inside Magento's default cap.
GQL_PAGE_SIZE = 200

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

GQL_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Accept-Language": HEADERS["Accept-Language"],
    "Origin": SITE_URL,
    "Referer": PRODUCT_LISTING_URL,
}

# Product URL shape (both discovered items and Phase-2 filter):
#   /product/<sku>/<url-key>/
PRODUCT_URL_RE = re.compile(r"/product/\d+/[a-z0-9\-]+/?", re.I)

# ── HTTP FETCH — SHARED MODULE, NOT YOURS TO REIMPLEMENT (CRITICAL) ──────────
from src.http_fetch import create_fetch_page, create_fetch_text
from src.page_analysis import phase2_instant_fail

fetch_page = create_fetch_page(delay_s=DELAY_BETWEEN_REQUESTS, headers=HEADERS)
fetch_text = create_fetch_text(delay_s=DELAY_BETWEEN_REQUESTS, headers=HEADERS)

# ── LISTING DISCOVERY — SHARED MODULE, NOT YOURS TO REIMPLEMENT (CRITICAL) ───
# Kept verbatim per template contract: it is this site's LAST-RESORT discovery
# rung (the HTML listing is a JS shell here, so GraphQL/sitemap run first —
# but if all measured rungs fail, the shared module still owns the attempt
# with its ladder escalation and honest empty_first_page classification).
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


# ── FIELD NORMALIZERS — inline on purpose, NOT a src import ──────────────────

def _norm_price(value) -> Optional[float]:
    """Strip currency symbols/whitespace from a price. Returns a float or None.

    "£1,234.56" → 1234.56, "1.234,56 €" → 1234.56, 16.49 → 16.49. None when
    no digits — an unparseable price is EMPTY, never zero.
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


def _norm_availability(value) -> Optional[str]:
    """Normalize availability to ``in_stock`` / ``out_of_stock``."""
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


def _meta_content(soup: BeautifulSoup, attr: str, key: str) -> str:
    """First matching <meta> content value (og:/product:/name attributes)."""
    tag = soup.find("meta", attrs={attr: key})
    if tag is None:
        return ""
    return (tag.get("content") or "").strip()


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 1 — DISCOVERY (GraphQL listing API → sitemap → shared HTML module)
# ═══════════════════════════════════════════════════════════════════════════════

# Code Writer adapted: the HTML category listing is a JS shell on this site
# (measured: 0 anchors, 0 inline /product/ URLs, ?page=N ignored), so the
# site's own listing mechanism is its GraphQL storefront API. The shared
# ladder still carries every GET; the single POST rung below rides the
# measured transport (curl_cffi chrome-TLS impersonation, NO proxy —
# scraper_analysis ACCESS RECIPE: fingerprint_chrome_none) with its own
# retry/backoff. ProxyConfig is deliberately NOT imported: this job is
# proxy=none and the task forbids the proxy module.

try:  # measured listing transport — absence degrades to plain requests
    from curl_cffi import requests as _curl_requests

    _GQL_SESSION = _curl_requests.Session(impersonate="chrome")
except Exception:  # pragma: no cover - slim images
    _GQL_SESSION = None

_GQL_PRODUCTS_QUERY = """
query CategoryProducts($categoryId: String!, $pageSize: Int!, $currentPage: Int!) {
  products(filter: {category_id: {eq: $categoryId}}, pageSize: $pageSize, currentPage: $currentPage) {
    total_count
    items { sku name url_key url_suffix __typename }
    page_info { page_size current_page total_pages }
  }
}
"""

_GQL_CATEGORY_QUERY = """
query ResolveCategory($path: String!) {
  categories(filters: {url_path: {eq: $path}}) {
    items { id name url_path product_count }
  }
}
"""


def _gql_post(payload: dict) -> Optional[dict]:
    """POST one GraphQL payload via the measured no-proxy transport."""
    last_err = ""
    for attempt in range(2):
        try:
            if _GQL_SESSION is not None:
                resp = _GQL_SESSION.post(
                    GRAPHQL_URL, data=json.dumps(payload).encode("utf-8"),
                    headers=GQL_HEADERS, timeout=30,
                )
            else:
                resp = requests.post(
                    GRAPHQL_URL, data=json.dumps(payload).encode("utf-8"),
                    headers=GQL_HEADERS, timeout=30,
                )
            if resp.status_code == 200:
                return resp.json()
            last_err = f"HTTP {resp.status_code}: {str(resp.text)[:120]}"
        except Exception as exc:  # transport-level — retry once, then give up
            last_err = f"{type(exc).__name__}: {exc}"
        time.sleep(2 * (attempt + 1))
    logger.warning("GraphQL POST failed after retries: %s", last_err)
    return None


def _gql_category_id(listing_url: str) -> Optional[str]:
    """Resolve the listing URL's category id via its url_path."""
    path = re.sub(r"^https?://[^/]+", "", listing_url).strip("/")
    path = path.split("?")[0].split("#")[0]
    if not path:
        return None
    data = _gql_post({"query": _GQL_CATEGORY_QUERY, "variables": {"path": path}})
    if not data or data.get("errors"):
        return None
    items = ((data.get("data") or {}).get("categories") or {}).get("items") or []
    for cat in items:
        if cat.get("id"):
            logger.info(
                "GraphQL resolved listing %s → category %s (%s, product_count=%s)",
                listing_url, cat.get("id"), cat.get("name"), cat.get("product_count"),
            )
            return str(cat["id"])
    return None


def _gql_item_url(item: dict) -> str:
    """Build the absolute PDP URL from a GraphQL product item.

    Magento PWA url_key here already carries the 'product/<sku>/<slug>' shape
    (measured: url_key='product/1121835/poppiseed-jawsome-shark-rug',
    url_suffix='/'); older shapes carry a bare slug — build that form then.
    Paths are JOINED against the site root, never concatenated onto a host.
    """
    url_key = (item.get("url_key") or "").strip().strip("/")
    suffix = item.get("url_suffix") or "/"
    sku = (item.get("sku") or "").strip()
    if url_key.startswith("product/"):
        path = f"/{url_key}"
    else:
        path = f"/product/{sku}/{url_key}"
    return make_absolute_url(path + (suffix if suffix.startswith("/") else f"/{suffix}"))


def _discover_via_graphql(listing_url: str, search_term: str = "") -> list[str]:
    """Paginate the category's (or a search term's) products via GraphQL."""
    variables: dict = {"pageSize": GQL_PAGE_SIZE, "currentPage": 1}
    if search_term:
        query = _GQL_PRODUCTS_QUERY.replace(
            "(filter: {category_id: {eq: $categoryId}}", "(search: $search"
        ).replace("$categoryId: String!,", "$search: String!,")
        variables["search"] = search_term
    else:
        category_id = _gql_category_id(listing_url)
        if not category_id:
            return []
        variables["categoryId"] = category_id
        query = _GQL_PRODUCTS_QUERY

    urls: list[str] = []
    seen: set = set()
    page = 1
    total_pages: Optional[int] = None
    while True:
        variables["currentPage"] = page
        data = _gql_post({"query": query, "variables": variables})
        products = ((data or {}).get("data") or {}).get("products")
        if not products:
            logger.warning("GraphQL products query returned no data on page %s", page)
            break
        items = products.get("items") or []
        if not items:
            break
        new_on_page = 0
        for item in items:
            url = _gql_item_url(item)
            if url and PRODUCT_URL_RE.search(url) and url not in seen:
                seen.add(url)
                urls.append(url)
                new_on_page += 1
        info = products.get("page_info") or {}
        total_pages = info.get("total_pages") or total_pages
        logger.info(
            "GraphQL listing page %s/%s: %s items, %s new (total %s; total_count=%s)",
            page, total_pages, len(items), new_on_page, len(urls),
            products.get("total_count"),
        )
        if total_pages is not None and page >= int(total_pages):
            break
        if new_on_page == 0:
            break
        page += 1
    return urls


def _discover_via_sitemap() -> list[str]:
    """Fallback rung: product URLs from sitemap.xml (index → children)."""
    result = fetch_text(SITE_URL + "/sitemap.xml")
    if not result:
        return []
    index_text, _status = result
    children = re.findall(r"<loc>([^<]+)</loc>", index_text)
    urls: list[str] = []
    seen: set = set()
    for child in children[:8]:
        child_result = fetch_text(child)
        if not child_result:
            continue
        child_text, _status = child_result
        for loc in re.findall(r"<loc>([^<]+)</loc>", child_text):
            if PRODUCT_URL_RE.search(loc) and loc not in seen:
                seen.add(loc)
                urls.append(loc)
    logger.info("Sitemap discovery: %s product URLs from %s sitemap files",
                len(urls), min(len(children), 8))
    return urls


def _extract_listing_links(soup: BeautifulSoup) -> list[str]:
    """Site-adaptable callback for the SHARED HTML discovery module.

    The rendered category grid is client-side here, so raw-HTML anchors are
    normally empty — the module's ItemList fallback / soft-block escalation
    then classify the page honestly. Contract: ABSOLUTE urls, [] when none,
    never raise.
    """
    urls = []
    for link in soup.select("a[href]"):
        absolute_url = make_absolute_url(link.get("href", ""))
        if not absolute_url:
            continue
        if not PRODUCT_URL_RE.search(absolute_url):
            continue
        urls.append(absolute_url)
    return urls


def run_phase1_discovery(listing_urls: list[str], search_term: str = "") -> tuple[list[str], dict]:
    """Phase 1: measured rungs in order, zero-yield self-heals down the chain.

    1. GraphQL category/search listing (the site's own storefront API).
    2. Sitemap product URLs (full catalogue, raw HTTP).
    3. Shared HTML listing-discovery module (verbatim call — honest zero).
    A rung that yields 0 falls through loudly to the next one.
    """
    urls: list[str] = []
    meta: dict = {"source": None, "stop_reason": "no_next_link",
                  "max_pages_hit": False, "soft_block_escalations": 0,
                  "retried_empty_discovery": False, "jsonld_fallback_pages": 0}

    for listing_url in listing_urls:
        urls = _discover_via_graphql(listing_url, search_term)
        if urls:
            meta["source"] = "graphql_category" if not search_term else "graphql_search"
            break
    if not urls and search_term:
        # search-term listing failed → fall back to the promoted category.
        urls = _discover_via_graphql(PRODUCT_LISTING_URL)
        if urls:
            meta["source"] = "graphql_category"
    if not urls:
        logger.warning("Phase 1: GraphQL rung yielded 0 URLs — falling back to sitemap")
        urls = _discover_via_sitemap()
        if urls:
            meta["source"] = "sitemap"
    if not urls:
        logger.warning(
            "Phase 1: sitemap rung yielded 0 URLs — falling back to shared HTML "
            "listing discovery (soft-block ladder owns this attempt)"
        )
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
        urls, meta = discover_listing_urls_with_retry(
            fetch_page, listing_urls, _extract_listing_links,
            **_discovery_cfg,
        )
        meta["source"] = "html_listing"
        return urls, meta

    logger.info("Phase 1 complete via %s: %s product URLs", meta["source"], len(urls))
    return urls, meta


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 2 — EXTRACTION (SSR <head>: og: meta tags + JSON-LD Product)
# ═══════════════════════════════════════════════════════════════════════════════

# Code Writer adapted: field map = product name, price, currency, description,
# availability. Sources (all server-rendered in <head>): og:title/JSON-LD name,
# og:price:amount / JSON-LD offers.price (SALE price — measured 16.49),
# og:price:currency / JSON-LD priceCurrency (NZD), JSON-LD description →
# og:description, og:availability / JSON-LD offers.availability (instock →
# in_stock). was-price: JSON-LD offers.priceSpecification ListPrice (29.99),
# kept only when greater than the current price (value-oriented).

SOFT404_TITLE_MARKERS = (
    "not found", "page not found", "no longer available", "unavailable",
    "discontinued", "404", "whoops",
)


def extract_jsonld_products(soup: BeautifulSoup) -> list[dict]:
    """Every schema.org Product node across the page's JSON-LD blocks."""
    products: list[dict] = []
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or (script.get_text() if hasattr(script, "get_text") else None)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            graph = node.get("@graph") or [node]
            for sub in graph:
                if isinstance(sub, dict) and sub.get("@type") == "Product":
                    products.append(sub)
    return products


def _jsonld_offer(product_node: dict) -> dict:
    offers = product_node.get("offers") or {}
    if isinstance(offers, list):
        return offers[0] if offers else {}
    return offers if isinstance(offers, dict) else {}


def _jsonld_list_price(offer: dict) -> Optional[float]:
    """schema.org ListPrice from offers.priceSpecification (dict or list)."""
    spec = offer.get("priceSpecification")
    specs = spec if isinstance(spec, list) else ([spec] if isinstance(spec, dict) else [])
    for entry in specs:
        if not isinstance(entry, dict):
            continue
        price_type = str(entry.get("priceType") or entry.get("@type") or "")
        if "listprice" in price_type.lower():
            return _norm_price(entry.get("price"))
    return None


def extract_product_from_page(soup: BeautifulSoup, url: str, status_code: int, src_url: str) -> dict:
    # Code Writer adapted: head-meta + JSON-LD extraction per the field map.
    product = {
        "id": 0,
        "title": "",
        "price": "",
        "availability": "",
        "original_price": "",
        "currency": "",
        "description": "",
        "url": url,
        "src_url": src_url,
        "status_code": status_code,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": "",
    }

    jsonld_products = extract_jsonld_products(soup)
    jsonld = jsonld_products[0] if jsonld_products else None
    offer = _jsonld_offer(jsonld) if jsonld else {}

    # ── title: og:title → JSON-LD name → meta[name=title] ──
    title = _meta_content(soup, "property", "og:title")
    if not title and jsonld:
        title = str(jsonld.get("name") or "").strip()
    if not title:
        title = _meta_content(soup, "name", "title")
    product["title"] = title

    # ── price (NUMBER): og:price:amount → JSON-LD offers.price ──
    price = _norm_price(_meta_content(soup, "property", "og:price:amount"))
    if price is None:
        price = _norm_price(offer.get("price"))
    product["price"] = price

    # ── original_price: JSON-LD ListPrice, kept only when HIGHER (value-
    #    oriented: lower = current, higher = previous). Empty when absent.
    list_price = _jsonld_list_price(offer)
    if list_price is not None and price is not None and list_price > price:
        product["original_price"] = list_price

    # ── currency: og:price:currency → JSON-LD priceCurrency ──
    currency = _meta_content(soup, "property", "og:price:currency")
    if not currency:
        currency = _meta_content(soup, "property", "product:price:currency")
    if not currency:
        currency = str(offer.get("priceCurrency") or "").strip()
    product["currency"] = currency.upper()

    # ── availability: og:availability → JSON-LD offers.availability ──
    availability = _meta_content(soup, "property", "og:availability")
    if not availability:
        availability = offer.get("availability")
    product["availability"] = _norm_availability(availability)

    # ── description: JSON-LD (full sentence) → og:description → meta name ──
    description = ""
    if jsonld and jsonld.get("description"):
        description = clean_html(str(jsonld["description"]))
    if not description:
        description = _meta_content(soup, "property", "og:description")
    if not description:
        description = _meta_content(soup, "name", "description")
    product["description"] = description

    # ── Soft 404 detection ──
    page_title = ""
    title_tag = soup.find("title")
    if title_tag:
        page_title = title_tag.get_text(strip=True).lower()
    markers_hit = [m for m in SOFT404_TITLE_MARKERS if m in page_title or m in title.lower()]
    if not jsonld_products and _meta_content(soup, "property", "og:type") != "product":
        product["remarks"] = "Soft 404: no Product JSON-LD and og:type != product — not a product page"
    elif markers_hit:
        product["remarks"] = f"Soft 404: title marker(s) {markers_hit}"
    if product["remarks"]:
        logger.warning("Soft 404 on %s: %s", url, product["remarks"])
        product["title"] = ""
        product["price"] = ""
    return product


# ═══════════════════════════════════════════════════════════════════════════════
# INPUT HANDLING
# ═══════════════════════════════════════════════════════════════════════════════

def load_urls_from_file(filepath: str) -> list[str]:
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("urls", [])


def save_urls_to_file(filepath: str, urls: list[str]) -> None:
    # A 0-URL discovery must not DESTROY an existing seed file.
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
# --fresh-discovery / --listing-url / --discover-only / --query / --input /
# --sample / --limit / --urls: the pipeline launches with EXACTLY these names.
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
        help="Re-run Phase 1 discovery even when a seed file exists",
    )
    parser.add_argument(
        "--listing-url",
        type=str,
        default=None,
        help="Listing/category page URL to discover from (overrides the default)",
    )
    parser.add_argument(
        "--query",
        type=str,
        default=None,
        help="Search term to discover products for (site search)",
    )
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        help="Disable proxies (default for this site — direct HTTP verified; "
        "the proxy module is never imported)",
    )
    args = parser.parse_args()

    if args.no_proxy:
        logger.info("--no-proxy set: direct HTTP only (matches this site's measured recipe)")

    if args.input:
        # Relative seed-file values resolve against the scraper's own dir.
        args.input = os.path.join(SCRIPT_DIR, args.input)

    start_time = time.time()

    logger.info("=" * 80)
    logger.info(f"Starting scraper for {SITE_NAME}")
    logger.info(f"Site: {SITE_URL}")
    logger.info(f"Listing URL: {PRODUCT_LISTING_URL}")
    logger.info(f"Output: {OUTPUT_FILE}")
    logger.info("=" * 80)

    # Shared-module discovery config (data, not code — see the import banner).
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

    # F6 DETERMINISTIC DISCOVERY GATE (env-var): run_execution injects
    # SCRAPER_LISTING_URL — when set, Phase 1 MUST run on it.
    _env_listing = os.environ.get("SCRAPER_LISTING_URL", "").strip()
    if _env_listing:
        PRODUCT_LISTING_URLS[:] = [_env_listing]
        logger.info("Env gate: SCRAPER_LISTING_URL set — forcing Phase 1 discovery on %s",
                    _env_listing[:80])
    elif args.listing_url:
        PRODUCT_LISTING_URLS[:] = [args.listing_url]
        logger.info("--listing-url set — forcing Phase 1 discovery on %s",
                    args.listing_url[:80])

    _force_discovery = bool(_env_listing or args.listing_url or args.fresh_discovery
                            or args.query)
    if args.fresh_discovery:
        logger.info("--fresh-discovery set: Phase 1 will re-discover (seed file "
                    "is rewritten from the fresh listing crawl)")

    product_urls = []
    discovered_urls_raw = 0
    ran_phase1 = False
    skipped_reason: Optional[str] = None
    discovery_meta: dict = {"stop_reason": "skipped", "max_pages_hit": False}

    search_term = (args.query or DEFAULT_QUERY or "").strip()

    # Phase 1: URL discovery (or load from input).
    if args.discover_only or _force_discovery:
        logger.info("Discovery mode (%s): running Phase 1, skipping Phase 2 extraction",
                    "env-gate" if _env_listing else "flag-gate")
        product_urls, discovery_meta = run_phase1_discovery(
            PRODUCT_LISTING_URLS, search_term
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
        product_urls, discovery_meta = run_phase1_discovery(
            PRODUCT_LISTING_URLS, search_term
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
        # Phase 2: extract fields from each discovered item page.
        phase2_start = time.monotonic()
        for i, url in enumerate(product_urls):
            result = fetch_page(url)
            if result:
                soup, status_code = result
                # src_url contract: the listing URL for discovered items, the
                # item URL itself when the run was seeded from a URL list.
                src_for_item = SRC_URL if ran_phase1 else url
                product = extract_product_from_page(soup, url, status_code, src_for_item)
                product["id"] = i + 1
                # Emission filter: a row with NO substantive field beyond the
                # boilerplate keys is a page that rendered nothing.
                _BK = {"url", "src_url", "scraped_at", "status_code", "remarks",
                       "id", "title"}
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

    # Mechanical "fetch actually happened" detector.
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
        # Site-specific: which Phase-1 rung produced the URL set.
        "discovery_source": discovery_meta.get("source"),
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
