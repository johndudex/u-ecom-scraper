#!/usr/bin/env python3
"""
Calvin Klein UK Navigation Scraper — Two-Phase Architecture

Phase 1: Discover product URLs via backend search API (pvh.cloud).
         If the API is blocked, fall back to Playwright browser discovery.
Phase 2: Extract product data from each product page using JSON-LD + DOM
         selectors via Playwright with CloakBrowser stealth.

Usage:
    python3 scraper_draft.py                          # search for "watches"
    python3 scraper_draft.py --query "shirts"          # custom search term
    python3 scraper_draft.py --sample                  # first 5 items only
    python3 scraper_draft.py --limit 50                # max 50 items
    python3 scraper_draft.py --urls <url1> <url2> ...  # scrape specific URLs
    python3 scraper_draft.py --input urls.json         # scrape from file
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import requests
from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from src.proxy import ProxyConfig, build_proxy_url, should_warn_residential, warn_residential_usage

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

SITE_NAME = "Calvin Klein United Kingdom"
SITE_URL = "https://www.calvinklein.co.uk"
PLATFORM = "custom"
SITE_SLUG = "calvinklein-co-uk"
SCRAPING_METHOD = "playwright"
CONTENT_TYPE = "product"
OUTPUT_KEY = "products"
CURRENCY = "GBP"
CURRENCY_SYMBOL = "£"

# Phase 1: API discovery
API_BASE = "https://live.ck.prd.b2c-api.eu.pvh.cloud/products"
SEARCH_URL_WORKING = "https://www.calvinklein.co.uk/search?searchTerm=watches"
DEFAULT_QUERY = "watches"

# Phase 1: Navigation (browser fallback)
SEARCH_URL_PATTERN = "https://www.calvinklein.co.uk/search?searchTerm={query}"
ITEM_CONTAINER_SELECTOR = '[data-testid*="GridItem"]'
ITEM_LINK_SELECTOR = "a"
PRODUCT_URL_PATTERN = re.compile(r"/[a-z0-9]+-[a-z0-9]+-[a-z0-9]+-[a-z0-9]+-[a-z0-9]+$", re.IGNORECASE)

# Phase 1: Pagination
PAGINATION_TYPE = "unknown"
NEXT_BUTTON_SELECTOR = ""
PAGE_PARAM_NAME = "offset"
MAX_PAGES = 50

# Phase 2: Extraction
DELAY_BETWEEN_REQUESTS = 3.0
ROTATE_EVERY = 25
PAGE_LOAD_TIMEOUT = 30000

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(os.path.dirname(SCRIPT_DIR), "logs", f"{SITE_SLUG}.log")

API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Origin": "https://www.calvinklein.co.uk",
    "Referer": "https://www.calvinklein.co.uk/",
}

# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════════════

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(SITE_SLUG)
proxy_config = ProxyConfig.get_instance()

# Content-type-aware output filter
_CONTENT_FILTER_FIELDS = {
    "product": ["price", "availability"],
    "article": ["author", "publish_date"],
    "job_posting": ["company", "location"],
}
CORE_FILTER_FIELDS = _CONTENT_FILTER_FIELDS.get(CONTENT_TYPE, [])


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _is_product_url(href: str) -> bool:
    """Calvin Klein product URL detector.

    Product URLs look like: /ck-pulse-gold-plated-steel-bracelet-watch-wf25100136000
    They have a long slug with a product code suffix (alphanumeric 10+ chars).
    """
    if not href:
        return False
    parsed = urlparse(href)
    host = parsed.hostname or ""
    site_host = urlparse(SITE_URL).hostname or ""
    if site_host not in host and host:
        return False
    path = parsed.path.strip("/")
    if not path or len(path) < 10:
        return False
    segs = path.split("/")
    # Skip locale prefixes like /en-uk/
    if len(segs) >= 2 and segs[0] in ("en-uk", "en-gb"):
        segs = segs[1:]
    if len(segs) == 1:
        slug = segs[0]
        # Product URLs have a long slug ending with a product code
        if len(slug) < 15:
            return False
        # Must contain at least some digits (product code like wf25100136000)
        if not any(c.isdigit() for c in slug):
            return False
        return True
    return False


def _normalize_url(href: str) -> str:
    """Ensure href is a full absolute URL."""
    if not href:
        return ""
    if href.startswith("/"):
        return SITE_URL.rstrip("/") + href
    if href.startswith("http"):
        return href
    return ""


def _format_price(raw_price: str, currency_symbol: str = CURRENCY_SYMBOL) -> str:
    """Format price string with currency symbol."""
    if not raw_price:
        return ""
    raw_price = raw_price.strip()
    # If already has a symbol, return as-is
    if re.match(r"^[£$€¥]", raw_price):
        return raw_price
    return f"{currency_symbol}{raw_price}"


def _map_availability(raw: str) -> str:
    """Normalize availability text to standard values."""
    if not raw:
        return ""
    raw_lower = raw.lower()
    if any(kw in raw_lower for kw in ("in stock", "available", "add to bag", "add to cart")):
        return "In Stock"
    if any(kw in raw_lower for kw in ("out of stock", "sold out", "unavailable", "not available")):
        return "Out of Stock"
    return raw.strip()


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 1A: API DISCOVERY (preferred — direct HTTP to pvh.cloud)
# ═══════════════════════════════════════════════════════════════════════════════

def _test_api_connection() -> bool:
    """Test if the backend API is accessible without anti-bot blocking."""
    test_params = {
        "filter[search]": DEFAULT_QUERY,
        "page[offset]": 0,
        "page[limit]": 2,
    }
    try:
        resp = requests.get(
            API_BASE, params=test_params, headers=API_HEADERS,
            timeout=15, proxies=None, verify=True,
        )
        if resp.status_code == 200:
            data = resp.json()
            # Check if we actually got items back
            items = _extract_api_items(data)
            if items:
                logger.info("API test PASSED — %d items returned", len(items))
                return True
            else:
                logger.warning("API returned 200 but no items — response keys: %s", list(data.keys())[:10])
                return False
        else:
            logger.warning("API test FAILED — status %d", resp.status_code)
            return False
    except Exception as exc:
        logger.warning("API test FAILED — %s", exc)
        return False


def _extract_api_items(data: dict | list) -> list[dict]:
    """Extract product items from the API response (flexible key matching)."""
    if isinstance(data, list):
        return data
    # Try common key names
    for key in ("products", "items", "data", "results", "hits", "records", "entries"):
        if key in data and isinstance(data[key], list):
            return data[key]
    # Try nested structures
    if isinstance(data, dict):
        for key, val in data.items():
            if isinstance(val, list) and val and isinstance(val[0], dict):
                # Heuristic: if items have a name/title field, they're products
                sample = val[0]
                if any(k in sample for k in ("name", "title", "slug", "url", "productId")):
                    return val
    return []


def _extract_api_total(data: dict | list) -> int:
    """Extract total item count from the API response."""
    if isinstance(data, list):
        return len(data)
    for key in ("totalCount", "total_count", "total", "totalResults", "totalProducts",
                "count", "numFound", "totalItems", "nbHits", "numberOfResults"):
        val = data.get(key)
        if isinstance(val, int):
            return val
    return 0


def _discover_urls_via_api(query: str, limit: Optional[int] = None) -> tuple[list[str], list[dict]]:
    """Phase 1 (primary): Discover product URLs via backend JSON API.

    Returns a tuple of (product_urls, raw_api_items).
    """
    all_items: list[dict] = []
    offset = 0
    page_size = 100  # Use large page size to minimize calls
    page_num = 1
    seen_slugs: set[str] = set()

    while True:
        params = {
            "filter[search]": query,
            "page[offset]": offset,
            "page[limit]": page_size,
        }
        logger.info("API page %d: offset=%d, limit=%d", page_num, offset, page_size)

        try:
            time.sleep(1.0)
            resp = requests.get(
                API_BASE, params=params, headers=API_HEADERS,
                timeout=15, proxies=None, verify=True,
            )
            if resp.status_code != 200:
                logger.error("API returned status %d — stopping", resp.status_code)
                break
            data = resp.json()
        except Exception as exc:
            logger.error("API request failed: %s", exc)
            break

        items = _extract_api_items(data)
        total = _extract_api_total(data)
        if total:
            logger.info("API: got %d items (total reported: %d)", len(items), total)

        new_count = 0
        for item in items:
            slug = item.get("slug") or item.get("url") or item.get("id") or ""
            if slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            all_items.append(item)
            new_count += 1

        logger.info("API page %d: %d items (%d new, cumulative: %d)", page_num, len(items), new_count, len(all_items))

        if limit and len(all_items) >= limit:
            break
        if len(items) < page_size:
            break
        if total and len(all_items) >= total:
            break
        if page_num >= MAX_PAGES:
            break

        offset += page_size
        page_num += 1

    # Build product URLs from API items
    product_urls = []
    for item in all_items:
        url = _build_product_url_from_api(item)
        if url:
            product_urls.append(url)

    if limit:
        all_items = all_items[:limit]
        product_urls = product_urls[:limit]

    logger.info("API discovery: %d products, %d URLs built", len(all_items), len(product_urls))
    return product_urls, all_items


def _build_product_url_from_api(item: dict) -> str:
    """Build a product page URL from an API item."""
    url = item.get("url") or ""
    slug = item.get("slug") or ""
    product_code = item.get("sku") or item.get("productId") or item.get("id") or ""

    # If URL is already a full product page URL
    if url and url.startswith("http"):
        if "calvinklein" in url:
            return url
        # Relative URL on the ck site
        if url.startswith("/"):
            return SITE_URL.rstrip("/") + url

    # If we have a slug, build the URL
    if slug:
        return f"{SITE_URL.rstrip('/')}/{slug}"

    return ""


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 1B: BROWSER DISCOVERY (fallback)
# ═══════════════════════════════════════════════════════════════════════════════

def _discover_urls_via_browser(page, query: str, limit: Optional[int] = None) -> list[str]:
    """Phase 1 (fallback): Discover product URLs using Playwright browser."""
    search_url = SEARCH_URL_PATTERN.replace("{query}", query)
    logger.info("Browser discovery: searching '%s' → %s", query, search_url)

    try:
        page.goto(search_url, timeout=PAGE_LOAD_TIMEOUT)
        page.wait_for_load_state("domcontentloaded")
        time.sleep(8)
    except Exception as exc:
        logger.error("Failed to load search page: %s", exc)
        return []

    all_urls: list[str] = _extract_item_links(page, search_url)
    logger.info("Page 1: discovered %d URLs", len(all_urls))

    current_page = 1
    while True:
        if limit and len(all_urls) >= limit:
            break
        if current_page >= MAX_PAGES:
            break

        next_url = _get_next_page_url(page, current_page + 1)
        if not next_url:
            logger.info("No more pages (page %d)", current_page)
            break

        logger.info("Navigating to page %d: %s", current_page + 1, next_url[:80])
        try:
            page.goto(next_url, timeout=PAGE_LOAD_TIMEOUT)
            page.wait_for_load_state("domcontentloaded")
            time.sleep(8)
        except Exception as exc:
            logger.error("Failed to load page %d: %s", current_page + 1, exc)
            break

        new_urls = _extract_item_links(page, search_url)
        new_count = len(set(new_urls) - set(all_urls))
        logger.info("Page %d: %d items (%d new, cumulative: %d)",
                     current_page + 1, len(new_urls), new_count, len(set(all_urls)))

        if new_count == 0:
            break

        all_urls.extend(new_urls)
        current_page += 1

    unique_urls = list(dict.fromkeys(all_urls))
    if limit:
        unique_urls = unique_urls[:limit]
    logger.info("Browser discovery: %d total unique URLs", len(unique_urls))
    return unique_urls


def _extract_item_links(page, src_url: str) -> list[str]:
    """Extract product page URLs from a listing page."""
    links: list[str] = []
    seen: set[str] = set()

    # Primary: GridItem containers → links
    try:
        containers = page.query_selector_all(ITEM_CONTAINER_SELECTOR)
        for container in containers:
            link_els = container.query_selector_all(ITEM_LINK_SELECTOR)
            for link_el in link_els:
                href = link_el.get_attribute("href") or ""
                full_url = _normalize_url(href)
                if full_url and full_url not in seen and _is_product_url(full_url):
                    links.append(full_url)
                    seen.add(full_url)
    except Exception as exc:
        logger.warning("GridItem extraction error: %s", exc)

    # Fallback: same-domain product-looking URLs
    if len(links) < 20:
        try:
            all_hrefs = page.eval_on_selector_all(
                "a[href]", "els => els.map(e => e.href)"
            )
            for h in all_hrefs:
                if h in seen:
                    continue
                if _is_product_url(h):
                    links.append(h)
                    seen.add(h)
        except Exception as exc:
            logger.warning("Fallback link extraction failed: %s", exc)

    return links


def _get_next_page_url(page, next_page_num: int) -> Optional[str]:
    """Determine URL for next page — tries multiple strategies."""
    # Strategy 1: next-button selectors
    for sel in (
        'a[rel="next"]', 'a.next', 'li.next a', '[aria-label*="next" i]',
        'a:has-text("Next")', 'button:has-text("Next")', 'a:has-text(">")',
        'a:has-text("NEXT")', '[class*="next"]', 'a[data-testid*="next"]',
    ):
        try:
            btn = page.query_selector(sel)
            if btn:
                href = btn.get_attribute("href") or ""
                if href:
                    return _normalize_url(href)
                # SPA-style: click and get new URL
                try:
                    btn.click()
                    page.wait_for_load_state("domcontentloaded")
                    time.sleep(5)
                    if page.url:
                        return page.url
                except Exception:
                    pass
        except Exception:
            pass

    # Strategy 2: ?page=N
    current = page.url
    sep = "&" if "?" in current else "?"
    return f"{current}{sep}page={next_page_num}"


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 2: PRODUCT EXTRACTION (Playwright with JSON-LD + DOM)
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_product_data(page, url: str, src_url: str) -> dict:
    """Phase 2: Extract product data from a product detail page.

    Uses JSON-LD for title/description/sku/brand/image/color/mpn.
    Falls back to DOM selectors for price/availability/original_price
    (since JSON-LD offers is empty on this site).
    """
    try:
        page.goto(url, timeout=PAGE_LOAD_TIMEOUT)
        page.wait_for_load_state("domcontentloaded")
        time.sleep(5)
    except Exception as exc:
        logger.error("Failed to load product page %s: %s", url[:80], exc)
        return _error_item(url, src_url, f"Load failed: {exc}")

    product: dict = {
        "id": 0,
        "title": "",
        "price": "",
        "availability": "",
        "original_price": "",
        "currency": CURRENCY,
        "url": url,
        "src_url": src_url,
        "location": "",
        "status_code": 200,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": "",
    }

    # ── Soft 404 detection ─────────────────────────────────────────────
    try:
        final_url = page.url
        if final_url and url not in final_url and "calvinklein" in final_url:
            # Redirected to a different page — check if it's a product page
            if not _is_product_url(final_url):
                product["remarks"] = f"Soft 404: redirected to {final_url}"
                product["status_code"] = 200
                return product
    except Exception:
        pass

    # ── JSON-LD extraction ──────────────────────────────────────────────
    try:
        json_ld_blocks = page.evaluate(
            "() => { "
            "  const scripts = document.querySelectorAll('script[type=\"application/ld+json\"]'); "
            "  return Array.from(scripts).map(s => { "
            "    try { return JSON.parse(s.textContent); } "
            "    catch(e) { return null; } "
            "  }).filter(Boolean); "
            "}"
        )

        for block in json_ld_blocks:
            if block.get("@type") == "Product":
                product["title"] = block.get("name", "")
                product["description"] = block.get("description", "")
                product["sku"] = block.get("sku", "")
                product["color"] = block.get("color", "")
                product["mpn"] = block.get("mpn", "")

                # Brand
                brand = block.get("brand", {})
                if isinstance(brand, dict):
                    product["brand"] = brand.get("name", "Calvin Klein")
                else:
                    product["brand"] = str(brand) if brand else "Calvin Klein"

                # Image — scope to product gallery only
                images = block.get("image", [])
                if isinstance(images, str):
                    images = [images]
                product["images"] = _filter_product_images(images)

                # Offers (empty on CK, but try anyway)
                offers = block.get("offers", {})
                if isinstance(offers, dict) and offers:
                    price = str(offers.get("price", ""))
                    if price and price != "0":
                        product["price"] = _format_price(price)
                    product["currency"] = offers.get("priceCurrency", CURRENCY)
                    avail_raw = offers.get("availability", "")
                    product["availability"] = _map_availability(avail_raw)
                elif isinstance(offers, list) and offers:
                    for offer in offers:
                        if isinstance(offer, dict):
                            price = str(offer.get("price", ""))
                            if price and price != "0":
                                product["price"] = _format_price(price)
                            product["currency"] = offer.get("priceCurrency", CURRENCY)
                            avail_raw = offer.get("availability", "")
                            product["availability"] = _map_availability(avail_raw)
                            break

                # Category from BreadcrumbList
                for breadcrumb_block in json_ld_blocks:
                    if breadcrumb_block.get("@type") == "BreadcrumbList":
                        product["category"] = _extract_breadcrumb_category(breadcrumb_block)
                        break

                break  # Found the Product block

    except Exception as exc:
        logger.warning("JSON-LD extraction failed for %s: %s", url[:60], exc)

    # ── DOM price extraction (primary — JSON-LD offers is empty) ────────
    if not product["price"]:
        product["price"] = _extract_price_from_dom(page)

    if not product["availability"]:
        product["availability"] = _extract_availability_from_dom(page)

    if not product["original_price"]:
        product["original_price"] = _extract_original_price_from_dom(page)

    # ── Fallback title from H1 ─────────────────────────────────────────
    if not product["title"]:
        try:
            h1 = page.query_selector("h1")
            if h1:
                raw_title = (h1.text_content() or "").strip()
                # Clean: remove brand suffix and product code
                raw_title = re.sub(r"\s*Calvin\s*Klein[®™]?\s*\|.*$", "", raw_title, flags=re.IGNORECASE)
                product["title"] = raw_title.strip()
        except Exception:
            pass

    # ── Soft 404: check title for not-found signals ────────────────────
    if product["title"]:
        title_lower = product["title"].lower()
        if any(kw in title_lower for kw in ("page not found", "not available", "404", "error")):
            product["remarks"] = f"Soft 404: title contains '{product['title'][:50]}'"
            product["title"] = ""
            product["price"] = ""
            product["availability"] = ""

    # ── Ensure price has currency symbol ───────────────────────────────
    if product["price"] and not re.match(r"^[£$€¥]", product["price"]):
        product["price"] = _format_price(product["price"])

    if product["original_price"] and not re.match(r"^[£$€¥]", product["original_price"]):
        product["original_price"] = _format_price(product["original_price"])

    return product


def _extract_price_from_dom(page) -> str:
    """Extract current price from DOM selectors."""
    selectors = [
        '[data-testid="product-price"]',
        '[data-testid="productPrice"]',
        '[class*="price"][class*="current"]',
        '[class*="price"][class*="main"]',
        '[class*="price"][class*="selling"]',
        '.product-price',
        '.price-value',
        '[class*="product-price"]',
    ]
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el:
                text = (el.text_content() or "").strip()
                if re.search(r"[\d.,]+", text):
                    return text
        except Exception:
            pass

    # Generic fallback: find any element with price-like content
    try:
        prices = page.evaluate(
            "(function() {"
            "  const els = document.querySelectorAll('[class*=\"price\"], [class*=\"Price\"]'); "
            "  for (const el of els) { "
            "    const t = el.textContent.trim(); "
            "    if (t.match(/[\\d.,]+/) && t.length < 30) return t; "
            "  } "
            "  return null; "
            "})()"
        )
        if prices:
            return str(prices).strip()
    except Exception:
        pass

    return ""


def _extract_availability_from_dom(page) -> str:
    """Extract availability status from DOM selectors."""
    selectors = [
        '[data-testid="product-availability"]',
        '[class*="availability"]',
        '[class*="stock"]',
        '[class*="inventory"]',
        '[aria-label*="stock" i]',
        '[aria-label*="available" i]',
    ]
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el:
                text = (el.text_content() or "").strip()
                if text:
                    return _map_availability(text)
        except Exception:
            pass

    # Check add-to-cart button availability
    try:
        atc = page.query_selector('button:has-text("Add to Bag"), button:has-text("Add to bag"), '
                                  'button:has-text("Add to Cart"), button:has-text("Add to cart")')
        if atc:
            return "In Stock"
        oos = page.query_selector('button:has-text("Out of Stock"), button:has-text("Sold Out"), '
                                  'button:has-text("Out of stock")')
        if oos:
            return "Out of Stock"
    except Exception:
        pass

    return ""


def _extract_original_price_from_dom(page) -> str:
    """Extract original/strikethrough price (only when on sale)."""
    selectors = [
        '[class*="price"][class*="original"]',
        '[class*="price"][class*="compare"]',
        '[class*="price"][class*="was"]',
        '[class*="price"][class*="old"]',
        '[class*="price"][class*="strike"]',
        's', 'del', '.compare-at-price',
    ]
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el:
                text = (el.text_content() or "").strip()
                if text and re.search(r"[\d.,]+", text):
                    return _format_price(text)
        except Exception:
            pass
    return ""


def _filter_product_images(images: list[str]) -> list[str]:
    """Filter images to only product gallery images (exclude nav/brand/badge)."""
    filtered = []
    skip_patterns = ["/brand.assets/", "/emoji/", "/flags/", "/icon/", "/navigation/",
                     "/logo/", "/badge/", "/banner/"]
    for img_url in images:
        if not isinstance(img_url, str):
            continue
        if not img_url.startswith("http"):
            continue
        if any(pat in img_url.lower() for pat in skip_patterns):
            continue
        filtered.append(img_url)
    return filtered[:15]  # Cap at 15 images per product


def _extract_breadcrumb_category(block: dict) -> str:
    """Extract category from BreadcrumbList JSON-LD."""
    try:
        elements = block.get("itemListElement", [])
        categories = []
        for elem in elements:
            name = elem.get("name", "")
            if name and name.lower() not in ("home", "homepage", "calvin klein"):
                categories.append(name)
        return " > ".join(categories) if categories else ""
    except Exception:
        return ""


def _extract_price_from_api_item(item: dict) -> str:
    """Try to extract price from an API item (various field names)."""
    for key in ("price", "finalPrice", "salePrice", "currentPrice", "unitPrice"):
        val = item.get(key)
        if val is not None:
            return _format_price(str(val))
    # Check nested price objects
    pricing = item.get("pricing", {}) or item.get("priceData", {})
    if isinstance(pricing, dict):
        for key in ("price", "finalPrice", "salePrice", "currentPrice"):
            val = pricing.get(key)
            if val is not None:
                return _format_price(str(val))
    return ""


def _extract_availability_from_api_item(item: dict) -> str:
    """Try to extract availability from an API item."""
    for key in ("availability", "stock", "inventoryStatus", "inStock", "stockStatus"):
        val = item.get(key)
        if val is not None:
            return _map_availability(str(val))
    # Boolean availability
    avail = item.get("available")
    if isinstance(avail, bool):
        return "In Stock" if avail else "Out of Stock"
    return ""


def _transform_api_item(item: dict, index: int, src_url: str) -> dict:
    """Transform an API item to standard product output format."""
    product: dict = {
        "id": index,
        "title": item.get("name", "") or item.get("title", "") or item.get("productName", ""),
        "price": _extract_price_from_api_item(item),
        "availability": _extract_availability_from_api_item(item),
        "original_price": "",
        "currency": CURRENCY,
        "url": _build_product_url_from_api(item),
        "src_url": src_url,
        "location": "",
        "status_code": 200,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": "",
    }

    # Add optional fields from API
    if item.get("sku"):
        product["sku"] = item["sku"]
    if item.get("brand"):
        product["brand"] = item["brand"] if isinstance(item["brand"], str) else "Calvin Klein"
    if item.get("description"):
        product["description"] = item["description"]
    if item.get("image") or item.get("imageUrl"):
        images = item.get("image", []) or item.get("imageUrl", [])
        if isinstance(images, str):
            images = [images]
        product["images"] = _filter_product_images(images)
    if item.get("color"):
        product["color"] = item["color"]
    if item.get("slug"):
        product["slug"] = item["slug"]

    # Try to find original price
    for key in ("originalPrice", "compareAtPrice", "listPrice", "wasPrice", "highPrice",
                "priceBeforeDiscount", "msrp"):
        val = item.get(key)
        if val is not None:
            try:
                float_val = float(val)
                if float_val > 0:
                    current = float(product["price"].replace("£", "").replace(",", "")) if product["price"] else 0
                    if float_val > current > 0:
                        product["original_price"] = _format_price(str(val))
            except (ValueError, TypeError):
                pass

    return product


def _error_item(url: str, src_url: str, error: str) -> dict:
    return {
        "id": 0,
        "title": "",
        "price": "",
        "availability": "",
        "original_price": "",
        "currency": CURRENCY,
        "url": url,
        "src_url": src_url,
        "location": "",
        "status_code": 0,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": f"Error: {error[:200]}",
    }


def _browser_alive(page) -> bool:
    """Probe if the browser is still responsive."""
    try:
        page.evaluate("1")
        return True
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description=f"{SITE_NAME} Scraper")
    parser.add_argument("--query", type=str, default=None, help="Search query term")
    parser.add_argument("--sample", action="store_true", help="Scrape first 5 items only")
    parser.add_argument("--limit", type=int, default=None, help="Max items to scrape")
    parser.add_argument("--input", type=str, default=None, help="Path to input URLs JSON file")
    parser.add_argument("--urls", nargs="+", default=None, help="Product URLs as CLI arguments")
    parser.add_argument("--no-proxy", action="store_true", help="Disable proxy")
    parser.add_argument("--headless", action="store_true", default=True, help="Headless mode")
    args = parser.parse_args()

    limit = 5 if args.sample else args.limit
    query = args.query or DEFAULT_QUERY
    start_time = time.time()

    logger.info("=" * 80)
    logger.info("Starting scraper for %s", SITE_NAME)
    logger.info("Site: %s", SITE_URL)
    logger.info("Query: %s", query)
    if limit:
        logger.info("Limit: %d", limit)
    logger.info("=" * 80)

    results: list[dict] = []
    failed_count = 0
    discovered_urls: list[str] = []
    src_url_base = SEARCH_URL_PATTERN.replace("{query}", query)

    # ── MODE: Direct URL input (--urls or --input) ────────────────────
    direct_url_mode = False
    if args.urls:
        discovered_urls = args.urls
        direct_url_mode = True
        logger.info("Mode: direct URLs (%d)", len(discovered_urls))
    elif args.input:
        try:
            with open(args.input, "r", encoding="utf-8") as f:
                data = json.load(f)
            discovered_urls = data.get("urls", [])
            direct_url_mode = True
            logger.info("Mode: input file (%d URLs)", len(discovered_urls))
        except Exception as exc:
            logger.error("Failed to read input file: %s", exc)
            sys.exit(1)
    else:
        # ── MODE: Discovery (Phase 1) ────────────────────────────────
        # First, try the backend API directly (no proxy, direct HTTP)
        api_works = _test_api_connection()
        raw_api_items: list[dict] = []

        if api_works:
            logger.info("Using backend API for discovery (Phase 1A)")
            discovered_urls, raw_api_items = _discover_urls_via_api(query, limit)

            # Check if API items have complete data (price, etc.)
            if raw_api_items and len(raw_api_items) > 0:
                sample = raw_api_items[0]
                has_price = bool(sample.get("price") or sample.get("finalPrice")
                                 or sample.get("pricing", {}).get("price"))
                if has_price:
                    logger.info("API items have price data — using API transform directly")
                    # Transform API items to output format
                    for i, item in enumerate(raw_api_items):
                        try:
                            product = _transform_api_item(item, i + 1, src_url_base)
                            results.append(product)
                        except Exception as exc:
                            logger.error("Error transforming API item %d: %s", i + 1, exc)
                            failed_count += 1
                            results.append(_error_item(
                                discovered_urls[i] if i < len(discovered_urls) else "",
                                src_url_base, str(exc)
                            ))

                    if limit:
                        results = results[:limit]

                    # Progress reporting
                    if len(results) % 25 == 0:
                        pct = (len(results) / max(len(discovered_urls), 1)) * 100
                        logger.info("Progress: [%d/%d] (%.1f%%)", len(results), len(discovered_urls), pct)

                    # Write output and exit
                    _write_output(results, start_time, failed_count, len(discovered_urls))
                    return

                logger.info("API items lack price data — need browser for Phase 2")
            else:
                logger.info("API discovery returned 0 items — falling back to browser")
        else:
            logger.info("Backend API blocked — using browser discovery (Phase 1B)")

        # If API didn't give us URLs or data, use browser
        if not discovered_urls and not api_works:
            # Browser-based discovery
            proxy_url = None
            if not args.no_proxy:
                proxy_url = build_proxy_url("datacenter")

            with sync_playwright() as p:
                browser, context, page = _launch_browser(p, proxy_url, args.headless)
                try:
                    discovered_urls = _discover_urls_via_browser(page, query, limit)
                finally:
                    try:
                        browser.close()
                    except Exception:
                        pass

    if not discovered_urls:
        logger.warning("No product URLs discovered")
        _write_output([], start_time, 0, 0)
        return

    # Apply limit to URLs
    if limit:
        discovered_urls = discovered_urls[:limit]

    # For direct URL mode, set src_url_base to empty string — each URL
    # serves as its own src_url (handled in the extraction loop below).
    if direct_url_mode:
        src_url_base = ""

    logger.info("Phase 2: Extracting data from %d items", len(discovered_urls))

    # ── PHASE 2: Browser-based extraction ───────────────────────────────
    proxy_url = None
    if not args.no_proxy:
        proxy_url = build_proxy_url("datacenter")

    with sync_playwright() as p:
        browser, context, page = _launch_browser(p, proxy_url, args.headless)

        total = len(discovered_urls)
        for i, url in enumerate(discovered_urls, 1):
            # Rotate browser periodically to prevent stealth Chromium crashes
            if (not _browser_alive(page)) or (i > 1 and (i - 1) % ROTATE_EVERY == 0):
                if i > 1:
                    logger.warning("Relaunching browser at item %d (alive=%s)", i, _browser_alive(page))
                    try:
                        browser.close()
                    except Exception:
                        pass
                    browser, context, page = _launch_browser(p, proxy_url, args.headless)

            if i % 25 == 0:
                pct = (i / total) * 100
                logger.info("Progress: [%d/%d] (%.1f%%)", i, total, pct)

            logger.info("[%d/%d] %s", i, total, url[:100])

            # Determine src_url: for direct URLs, src_url = url itself;
            # for discovery mode, src_url = the search/listing page.
            item_src_url = url if direct_url_mode else src_url_base

            try:
                product = _extract_product_data(page, url, item_src_url)
                product["id"] = i
                results.append(product)
                if product.get("remarks") and "Error" in product.get("remarks", ""):
                    failed_count += 1
            except Exception as exc:
                logger.error("Failed to extract %s: %s", url[:80], exc)
                results.append(_error_item(url, item_src_url, str(exc)))
                failed_count += 1

            if i < total:
                time.sleep(DELAY_BETWEEN_REQUESTS)

        try:
            browser.close()
        except Exception:
            pass

    # ── Output filter ──────────────────────────────────────────────────
    _extra = [f for f in CORE_FILTER_FIELDS if f and f != "title"]
    before = len(results)
    results = [
        it for it in results
        if it.get("title") and (not _extra or any(it.get(f) for f in _extra))
    ]
    if len(results) != before:
        logger.info(
            "Output filter: %d → %d items (dropped %d without core fields)",
            before, len(results), before - len(results),
        )

    _write_output(results, start_time, failed_count, len(discovered_urls))


def _launch_browser(p, proxy_url: Optional[str], headless: bool = True):
    """Launch Playwright browser with optional proxy and stealth."""
    browser_args = []
    if proxy_url:
        browser_args.append(f"--proxy-server={proxy_url}")
        logger.info("Using datacenter proxy")

    browser = p.chromium.launch(headless=headless, args=browser_args)
    context = browser.new_context(
        viewport={"width": 1920, "height": 1080},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    )
    page = context.new_page()
    return browser, context, page


def _write_output(results: list[dict], start_time: float, failed: int, discovered: int) -> None:
    """Write output JSON file."""
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
            "scraping_duration_seconds": round(time.time() - start_time, 2),
            "failed_products": failed,
            "rate_limit_delay": DELAY_BETWEEN_REQUESTS,
            "discovered_urls": discovered,
        },
    }

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    output_file = os.path.join(SCRIPT_DIR, f"output_{timestamp}.json")

    with open(output_file, "w", encoding="utf-8") as f:
        # _OUTPUT_FILTER_APPLIED — drop non-item pages (content-type aware)
        _FILTER_FIELDS = ['price', 'availability', 'currency']
        try:
            _before = len(output.get(OUTPUT_KEY, []))
            output[OUTPUT_KEY] = [p for p in output.get(OUTPUT_KEY, []) if p.get('title') and (p.get('price') or p.get('availability') or p.get('currency'))]
            _after = len(output[OUTPUT_KEY])
            if _before != _after:
                logger.info('output filter: %d → %d items (removed %d without title+price,availability,currency)',
                             _before, _after, _before - _after)
        except Exception:
            pass


        json.dump(output, f, indent=2, ensure_ascii=False)

    logger.info("=" * 80)
    logger.info("EXTRACTION COMPLETE")
    logger.info("Total: %d, Failed: %d, Discovered: %d", len(results), failed, discovered)
    logger.info("Duration: %.1fs", time.time() - start_time)
    logger.info("Output: %s", output_file)
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
