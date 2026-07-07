#!/usr/bin/env python3
"""
Dollar Tree Navigation Scraper - Two-Phase Architecture

Phase 1: Discover product URLs from sitemap XML (primary) or listing page navigation
Phase 2: Scrape each product page using Playwright (Knockout.js SPA requires JS rendering)

Oracle Commerce Cloud site using Knockout.js — server-side HTML is an empty shell.
All product data is rendered client-side. Playwright is required for Phase 2 extraction.

CRITICAL: JSON-LD offers.price is the CASE/BULK price (e.g., $18 for 12 units).
Per-unit price MUST come from CSS .list-sale-price or meta[property='product:price:amount'].

Usage:
    python3 scraper.py                                # Default: sitemap discovery
    python3 scraper.py --query "furniture"             # Listing page discovery
    python3 scraper.py --listing-url URL               # Browse a specific listing page
    python3 scraper.py --sitemap                       # Explicit sitemap discovery
    python3 scraper.py --input urls.json                # Read URLs from JSON file
    python3 scraper.py --urls URL1 URL2 URL3           # Pass URLs directly via CLI
    python3 scraper.py --sample                        # Scrape first 5 products only
    python3 scraper.py --limit 50                      # Max 50 products
    python3 scraper.py --no-proxy                      # Explicitly disable proxy (default)
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
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import requests as http_requests
from playwright.sync_api import sync_playwright, Page

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

SITE_NAME = "Dollar Tree"
SITE_URL = "https://www.dollartree.com"
PLATFORM = "oracle_commerce_cloud"
SITE_SLUG = "dollartree-com"
CURRENCY = "USD"
DEFAULT_QUERY = "furniture"
OUTPUT_KEY = "products"
SCRAPING_METHOD = "playwright"
DELAY_BETWEEN_REQUESTS = 2.0
PAGE_LOAD_TIMEOUT = 45000
MAX_SCROLL_ATTEMPTS = 15
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(SCRIPT_DIR, "..", "logs")

# Phase 1: Sitemap Configuration (PRIMARY discovery method)
SITEMAP_URLS = [
    "https://www.dollartree.com/productSitemap.xml",
    "https://www.dollartree.com/productSitemap-2.xml",
]

# Phase 1: Listing/Search Configuration (secondary — /products only shows categories)
LISTING_URL = "https://www.dollartree.com/products"
SEARCH_URL_TEMPLATE = "https://www.dollartree.com/products"

# Product URL pattern: /{slug}/{numeric-id} — MUST end with digits as LAST path segment
# CRITICAL FIX: Added $ anchor so re.fullmatch() rejects category URLs like
# /picture-frames/3x2-picture-frames (where \d+ would match the leading '3')
PRODUCT_URL_REGEX = re.compile(
    r"https?://(?:www\.)?dollartree\.com/[A-Za-z0-9][A-Za-z0-9\-]*/\d+$"
)

# Phase 2: Wait selectors (Knockout.js rendering)
WAIT_SELECTOR = "h1.product-name"
PRICE_SELECTOR = ".list-sale-price"
AVAILABILITY_SELECTOR = ".stock-availability"

# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════════════

os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, f"{SITE_SLUG}.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(SITE_SLUG)


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 1: URL DISCOVERY
# ═══════════════════════════════════════════════════════════════════════════════


def discover_urls_from_sitemap(limit: Optional[int] = None) -> list[str]:
    """Discover product URLs from the product sitemap XML files.

    Dollar Tree (OCC) exposes product sitemaps at /productSitemap.xml and
    /productSitemap-2.xml. These are static XML files accessible via HTTP.
    This is the PRIMARY discovery method because the /products listing page
    only contains category links, not product links.
    """
    logger.info("Phase 1: Discovering product URLs from sitemaps...")
    all_urls: list[str] = []

    for sitemap_url in SITEMAP_URLS:
        try:
            logger.info("  Fetching sitemap: %s", sitemap_url)
            resp = http_requests.get(
                sitemap_url,
                timeout=15,
                headers={"User-Agent": "Mozilla/5.0 (compatible; Bot/1.0)"},
            )
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            if "xml" not in content_type and not resp.text.strip().startswith("<"):
                logger.warning(
                    "  Sitemap %s returned non-XML content, skipping", sitemap_url
                )
                continue

            root = ElementTree.fromstring(resp.text)
            ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

            # Sitemap index: recurse into child sitemaps
            sitemap_entries = root.findall(".//sm:sitemap/sm:loc", ns)
            if sitemap_entries:
                logger.info(
                    "  Sitemap index with %d child sitemaps", len(sitemap_entries)
                )
                for entry in sitemap_entries:
                    child_url = entry.text.strip()
                    if child_url:
                        child_urls = _fetch_sitemap_urls(child_url)
                        all_urls.extend(child_urls)
                continue

            # Regular sitemap: extract URLs directly
            urls = _parse_sitemap_product_urls(root, ns)
            logger.info(
                "  Sitemap %s: %d product URLs found", sitemap_url, len(urls)
            )
            all_urls.extend(urls)

        except http_requests.RequestException as e:
            logger.warning("  Failed to fetch sitemap %s: %s", sitemap_url, e)
        except ElementTree.ParseError as e:
            logger.warning("  Failed to parse sitemap %s: %s", sitemap_url, e)

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique_urls: list[str] = []
    for url in all_urls:
        if url not in seen:
            seen.add(url)
            unique_urls.append(url)

    if limit:
        unique_urls = unique_urls[:limit]

    logger.info("Phase 1 (sitemap): Discovered %d unique product URLs", len(unique_urls))
    return unique_urls


def _fetch_sitemap_urls(sitemap_url: str) -> list[str]:
    """Fetch and parse a child sitemap URL."""
    try:
        resp = http_requests.get(
            sitemap_url,
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (compatible; Bot/1.0)"},
        )
        resp.raise_for_status()
        root = ElementTree.fromstring(resp.text)
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        return _parse_sitemap_product_urls(root, ns)
    except Exception as e:
        logger.warning("  Failed to fetch child sitemap %s: %s", sitemap_url, e)
        return []


def _parse_sitemap_product_urls(
    root: ElementTree.Element, ns: dict
) -> list[str]:
    """Extract product URLs from a sitemap XML, filtering by the product URL pattern.

    CRITICAL FIX: Uses re.fullmatch() instead of re.match() to ensure the
    numeric product ID is the LAST path segment. re.match() only checks the
    beginning and would accept category URLs like /picture-frames/3x2-picture-frames.
    """
    urls: list[str] = []
    for loc in root.findall(".//sm:url/sm:loc", ns):
        url = loc.text.strip() if loc.text else ""
        if url and PRODUCT_URL_REGEX.fullmatch(url):
            urls.append(url)
    return urls


def discover_urls_from_listing(
    page: Page,
    url: str,
    limit: Optional[int] = None,
) -> list[str]:
    """Phase 1b: Discover product URLs by navigating a listing/search page.

    Dollar Tree is a Knockout.js SPA — product cards render after JS execution.
    The /products page shows CATEGORIES, not products. This function navigates
    into category subpages to find actual product links.

    Strategy:
    1. Load the listing page, wait for Knockout.js rendering
    2. Extract all links, filter for category URLs (non-product pattern)
    3. Navigate into each category page to find product links
    4. Scroll within each category to trigger lazy loading
    """
    logger.info("Phase 1: Navigating listing page → %s", url)
    try:
        page.goto(url, timeout=PAGE_LOAD_TIMEOUT, wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
    except Exception as exc:
        logger.error("  Failed to load listing page: %s", exc)
        return []

    # First try to extract product links directly from the listing page
    all_urls: list[str] = []
    seen: set[str] = set()

    initial_links = _extract_product_links_from_page(page)
    for u in initial_links:
        if u not in seen:
            seen.add(u)
            all_urls.append(u)

    logger.info("  Direct product links from listing: %d", len(all_urls))

    if all_urls:
        # Found some product links directly, good!
        _scroll_and_collect(page, all_urls, seen, limit)
        unique_urls = list(dict.fromkeys(all_urls))
        if limit:
            unique_urls = unique_urls[:limit]
        logger.info(
            "Phase 1 (listing): Discovered %d unique product URLs from %s",
            len(unique_urls),
            url,
        )
        return unique_urls

    # No product links found — navigate into category pages to find products
    logger.info("  No direct product links. Navigating into category pages...")

    category_urls = _extract_category_links_from_page(page)
    logger.info("  Found %d category links to explore", len(category_urls))

    cat_limit = 10  # Explore up to 10 categories to find products
    for cat_url in category_urls[:cat_limit]:
        if limit and len(all_urls) >= limit:
            break

        logger.info("  Exploring category: %s", cat_url)
        try:
            page.goto(
                cat_url, timeout=PAGE_LOAD_TIMEOUT, wait_until="domcontentloaded"
            )
            page.wait_for_timeout(4000)
        except Exception as exc:
            logger.warning("  Failed to load category page %s: %s", cat_url, exc)
            continue

        # Scroll and collect product links within this category
        _scroll_and_collect(page, all_urls, seen, limit)

    unique_urls = list(dict.fromkeys(all_urls))
    if limit:
        unique_urls = unique_urls[:limit]

    logger.info(
        "Phase 1 (listing): Discovered %d unique product URLs from %s",
        len(unique_urls),
        url,
    )
    return unique_urls


def _scroll_and_collect(
    page: Page,
    all_urls: list[str],
    seen: set[str],
    limit: Optional[int] = None,
) -> None:
    """Scroll a page and collect product links, appending to all_urls in-place."""
    for attempt in range(MAX_SCROLL_ATTEMPTS):
        current_urls = _extract_product_links_from_page(page)
        new_count = 0
        for u in current_urls:
            if u not in seen:
                seen.add(u)
                all_urls.append(u)
                new_count += 1

        logger.info(
            "    Scroll %d/%d: %d links (%d new, %d total)",
            attempt + 1,
            MAX_SCROLL_ATTEMPTS,
            len(current_urls),
            new_count,
            len(all_urls),
        )

        if limit and len(all_urls) >= limit:
            break

        # Try "Load More" button
        load_more_clicked = _try_click_load_more(page)
        if load_more_clicked:
            page.wait_for_timeout(3000)
            continue

        # Scroll down
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(2000)

        # Check for pagination
        next_page = _try_get_next_page_link(page)
        if next_page:
            logger.info("    Following pagination → %s", next_page)
            try:
                page.goto(
                    next_page, timeout=PAGE_LOAD_TIMEOUT, wait_until="domcontentloaded"
                )
                page.wait_for_timeout(4000)
                continue
            except Exception as exc:
                logger.warning("    Failed to follow pagination: %s", exc)
                break

        # No new links after sufficient attempts
        if new_count == 0 and attempt >= 2:
            logger.info("    No new links after scroll, stopping")
            break


def _extract_product_links_from_page(page: Page) -> list[str]:
    """Extract all product page URLs from the current page DOM.

    CRITICAL FIX: Uses re.fullmatch() instead of re.match(). re.match() only
    checks the beginning of the string, so it would accept category URLs like
    /picture-frames/3x2-picture-frames (where \\d+ matches the leading '3').
    fullmatch() requires the ENTIRE string to match the pattern.
    """
    links: list[str] = []
    try:
        hrefs = page.evaluate(
            """() => {
                const links = document.querySelectorAll('a[href]');
                return Array.from(links).map(a => a.getAttribute('href'));
            }"""
        )
        for href in hrefs or []:
            if not href:
                continue
            # Resolve relative URLs
            full_url = urljoin(SITE_URL, href) if href.startswith("/") else href
            # CRITICAL: Use fullmatch() to reject category URLs
            if PRODUCT_URL_REGEX.fullmatch(full_url):
                links.append(full_url)
    except Exception as exc:
        logger.warning("  Error extracting product links: %s", exc)
    return links


def _extract_category_links_from_page(page: Page) -> list[str]:
    """Extract category listing page URLs from a page.

    Category URLs are on dollartree.com but don't match the product URL pattern
    (they don't end with /{numeric-id}). We collect URLs that look like category
    pages: have multiple path segments but don't end with a pure numeric ID.
    """
    links: list[str] = []
    seen: set[str] = set()
    try:
        hrefs = page.evaluate(
            """() => {
                const links = document.querySelectorAll('a[href]');
                return Array.from(links).map(a => a.getAttribute('href'));
            }"""
        )
        for href in hrefs or []:
            if not href:
                continue
            full_url = urljoin(SITE_URL, href) if href.startswith("/") else href
            # Skip if it's a product URL (we only want categories)
            if PRODUCT_URL_REGEX.fullmatch(full_url):
                continue
            # Must be on dollartree.com domain
            if "dollartree.com" not in full_url:
                continue
            # Must have a path with multiple segments (at least 2 after domain)
            path = urlparse(full_url).path.strip("/")
            if not path:
                continue
            segments = [s for s in path.split("/") if s]
            if len(segments) < 2:
                continue
            # Skip common non-category paths
            skip_prefixes = ("/products", "/search", "/cart", "/checkout", "/account")
            if any(path.startswith(p) for p in skip_prefixes):
                continue
            # Last segment should NOT be purely numeric (that's a product)
            if segments[-1].isdigit():
                continue
            # Looks like a category
            if full_url not in seen:
                seen.add(full_url)
                links.append(full_url)
    except Exception as exc:
        logger.warning("  Error extracting category links: %s", exc)
    return links


def _try_click_load_more(page: Page) -> bool:
    """Try to find and click a 'Load More' / 'Show More' / 'View All' button."""
    selectors = [
        'button:has-text("Load More")',
        'button:has-text("Show More")',
        'a:has-text("Load More")',
        'a:has-text("Show More")',
        'a:has-text("View All")',
        '[class*="load-more"]',
        '[class*="show-more"]',
        '[data-action="load-more"]',
        '[data-action="show-more"]',
    ]
    for sel in selectors:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click()
                return True
        except Exception:
            continue
    return False


def _try_get_next_page_link(page: Page) -> Optional[str]:
    """Try to find a 'Next Page' pagination link."""
    selectors = [
        'a:has-text("Next")',
        'a:has-text(">")',
        'a[rel="next"]',
        '[class*="pagination"] a:has-text(">")',
        '[class*="pager"] a:has-text("Next")',
        'a[href*="page="]:has-text("Next")',
    ]
    for sel in selectors:
        try:
            link = page.query_selector(sel)
            if link and link.is_visible():
                href = link.get_attribute("href") or ""
                if href:
                    return (
                        urljoin(SITE_URL, href) if href.startswith("/") else href
                    )
        except Exception:
            continue
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 2: PRODUCT EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════════


def extract_product_data(page: Page, url: str, src_url: str) -> dict:
    """Phase 2: Extract structured product data from a single product page.

    Uses hybrid extraction:
    - JSON-LD for: title, description, image, brand, sku, category, rating,
                  review_count, currency
    - CSS for: price (per-unit, NOT case price), availability
    - Meta tags for: price fallback, currency fallback

    IMPORTANT: JSON-LD offers.price is the CASE/BULK price (e.g., $18 for 12×$1.50).
    Per-unit price is extracted from .list-sale-price CSS or
    meta[property='product:price:amount'].

    Wait strategy: Multi-step approach for Knockout.js SPA rendering:
    1. page.goto() with domcontentloaded
    2. waitForSelector('h1.product-name', {timeout: 15000})
    3. waitForSelector('.list-sale-price', {timeout: 10000})
    4. If step 2 fails, wait for JSON-LD as fallback
    5. If extraction yields empty title, wait 3s and retry once
    """
    try:
        page.goto(url, timeout=PAGE_LOAD_TIMEOUT, wait_until="domcontentloaded")
        # Multi-step wait for Knockout.js rendering
        try:
            page.wait_for_selector(WAIT_SELECTOR, timeout=15000)
            # Also wait for price element
            try:
                page.wait_for_selector(PRICE_SELECTOR, timeout=10000)
            except Exception:
                pass
        except Exception:
            # Fallback: wait for JSON-LD scripts to be injected by Knockout
            try:
                page.wait_for_selector(
                    'script[type="application/ld+json"]', timeout=10000
                )
            except Exception:
                # Last resort: just wait
                page.wait_for_timeout(5000)
    except Exception as exc:
        logger.error("  Failed to load product page %s: %s", url[:80], exc)
        return _make_error_item(url, src_url, f"Page load failed: {exc}")

    product: dict = _extract_fields_from_page(page, url, src_url)

    # Retry logic: if title is empty, wait and retry once
    if not product.get("title"):
        logger.warning(
            "  Title empty on first extraction attempt, waiting and retrying..."
        )
        page.wait_for_timeout(3000)
        product = _extract_fields_from_page(page, url, src_url)

    return product


def _extract_fields_from_page(page: Page, url: str, src_url: str) -> dict:
    """Extract all product fields from the current page state.

    Separated from navigate logic to support retry extraction.
    """
    product: dict = {
        "id": 0,
        "title": "",
        "price": "",
        "availability": "",
        "original_price": "",
        "currency": "",
        "url": url,
        "src_url": src_url,
        "location": "",
        "status_code": 200,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": "",
    }

    # ── Soft 404 Detection ────────────────────────────────────────────
    try:
        final_url = page.url
        if final_url and final_url.rstrip("/") != url.rstrip("/"):
            path = urlparse(final_url).path
            if path in ("/", "/products", "/search") or not PRODUCT_URL_REGEX.fullmatch(
                final_url
            ):
                product["remarks"] = f"Soft 404: redirected to {final_url}"
                product["status_code"] = 301
                return product
    except Exception:
        pass

    # Check page title/H1 for soft 404 indicators
    try:
        page_text = page.evaluate(
            """() => {
                const h1 = document.querySelector('h1');
                const title = document.title || '';
                const body = (document.body?.innerText || '').substring(0, 500);
                return JSON.stringify({
                    h1: h1?.textContent || '',
                    title: title,
                    body: body
                });
            }"""
        )
        if page_text:
            page_info = json.loads(page_text)
            text_to_check = (
                (page_info.get("h1") or "")
                + " "
                + (page_info.get("title") or "")
                + " "
                + (page_info.get("body") or "")
            ).lower()
            soft_404_phrases = [
                "page not found",
                "product not found",
                "no longer available",
                "discontinued",
                "item not found",
                "page could not be found",
                "oops",
            ]
            for phrase in soft_404_phrases:
                h1_lower = (page_info.get("h1") or "").lower()
                title_lower = (page_info.get("title") or "").lower()
                if phrase in h1_lower or phrase in title_lower:
                    product["remarks"] = (
                        f"Soft 404: '{phrase}' found in page heading/title"
                    )
                    return product
                if phrase == "page not found" and phrase in text_to_check:
                    product["remarks"] = f"Soft 404: '{phrase}' detected on page"
                    return product
    except Exception:
        pass

    # ── Extract JSON-LD Data ──────────────────────────────────────────
    json_ld_data: Optional[dict] = None
    aggregate_rating: Optional[dict] = None

    try:
        ld_blocks = page.evaluate(
            """() => {
                const scripts = document.querySelectorAll('script[type="application/ld+json"]');
                return Array.from(scripts).map(s => {
                    try { return JSON.parse(s.textContent); }
                    catch(e) { return null; }
                }).filter(Boolean);
            }"""
        )

        for block in (ld_blocks or []):
            block_type = block.get("@type", "")
            if isinstance(block_type, str) and block_type == "Product":
                json_ld_data = block
            elif isinstance(block_type, str) and block_type == "AggregateRating":
                aggregate_rating = block

            if isinstance(block_type, list):
                if "Product" in block_type:
                    json_ld_data = block
                if "AggregateRating" in block_type:
                    aggregate_rating = block

    except Exception as exc:
        logger.warning("  JSON-LD extraction failed: %s", exc)

    if not json_ld_data:
        product["remarks"] = (
            "No JSON-LD Product block found — may not be a valid product page"
        )

    # ── Title ──────────────────────────────────────────────────────────
    try:
        if json_ld_data:
            product["title"] = json_ld_data.get("name", "")
        if not product["title"]:
            product["title"] = page.evaluate(
                """() => {
                    const h1 = document.querySelector('h1.product-name');
                    return h1 ? h1.textContent.trim() : '';
                }"""
            ) or ""
        if not product["title"]:
            product["title"] = page.evaluate(
                """() => {
                    const h1 = document.querySelector('h1');
                    return h1 ? h1.textContent.trim() : '';
                }"""
            ) or ""
    except Exception as exc:
        logger.warning("  Title extraction failed: %s", exc)

    # ── Price (CRITICAL: use CSS, NOT JSON-LD) ─────────────────────────
    # JSON-LD offers.price is the CASE/BULK price (12 units × unit price).
    # Per-unit price MUST come from .list-sale-price or meta tag.
    try:
        price = page.evaluate(
            """() => {
                // Primary: .list-sale-price CSS element
                const p = document.querySelector('.list-sale-price');
                if (p) {
                    const dollar = p.querySelector('.list-price-text-dollar');
                    const cent = p.querySelector('.list-price-text-cent');
                    const dollarText = dollar ? dollar.textContent.trim().replace('.', '') : '';
                    const centText = cent ? cent.textContent.trim() : '00';
                    if (dollarText) return '$' + dollarText + '.' + centText;
                }
                // Fallback: meta tag
                const meta = document.querySelector('meta[property="product:price:amount"]');
                if (meta && meta.content) return '$' + meta.content;
                // Last resort: try any visible price element
                const priceEl = document.querySelector('[data-auto-id="product-price"]');
                if (priceEl) return priceEl.textContent.trim();
                return '';
            }"""
        ) or ""
        product["price"] = price
    except Exception as exc:
        logger.warning("  Price extraction failed: %s", exc)

    # ── Availability ──────────────────────────────────────────────────
    try:
        avail = page.evaluate(
            """() => {
                const el = document.querySelector('.stock-availability');
                if (!el) return '';
                const text = el.textContent.trim().toLowerCase();
                const classes = el.className || '';
                if (classes.includes('out-of-stock') || text.includes('unavail')) {
                    return 'Out of Stock';
                }
                if (text.includes('available') || text.includes('in stock')) {
                    return 'In Stock';
                }
                return text;
            }"""
        ) or ""
        if avail:
            if "out of stock" in avail.lower() or "unavail" in avail.lower():
                product["availability"] = "Out of Stock"
            else:
                product["availability"] = "In Stock"
        elif json_ld_data:
            offers = json_ld_data.get("offers", {})
            if isinstance(offers, dict):
                avail_url = offers.get("availability", "")
                if "OutOfStock" in avail_url or "SoldOut" in avail_url:
                    product["availability"] = "Out of Stock"
                elif "InStock" in avail_url or "LimitedAvailability" in avail_url:
                    product["availability"] = "In Stock"
    except Exception as exc:
        logger.warning("  Availability extraction failed: %s", exc)

    # ── Currency ──────────────────────────────────────────────────────
    try:
        currency = page.evaluate(
            """() => {
                const meta = document.querySelector('meta[property="product:price:currency"]');
                if (meta && meta.content) return meta.content;
                return '';
            }"""
        ) or ""
        if not currency and json_ld_data:
            offers = json_ld_data.get("offers", {})
            if isinstance(offers, dict):
                currency = offers.get("priceCurrency", "")
        product["currency"] = currency or CURRENCY
    except Exception:
        product["currency"] = CURRENCY

    # ── Brand ──────────────────────────────────────────────────────────
    brand: str = ""
    try:
        if json_ld_data:
            brand_obj = json_ld_data.get("brand")
            if isinstance(brand_obj, dict):
                brand = brand_obj.get("name", "")
        if not brand:
            brand = page.evaluate(
                """() => {
                    const els = document.querySelectorAll('.text-bold-content');
                    for (const el of els) {
                        if (el.textContent.includes('Brand:')) {
                            const next = el.nextElementSibling;
                            if (next) return next.textContent.trim();
                            return el.textContent.replace('Brand:', '').trim();
                        }
                    }
                    return '';
                }"""
            ) or ""
    except Exception:
        pass

    # ── SKU ────────────────────────────────────────────────────────────
    sku: str = ""
    try:
        sku = page.evaluate(
            """() => {
                const el = document.querySelector('.occ-sku');
                if (el) return el.textContent.replace('SKU:', '').trim();
                return '';
            }"""
        ) or ""
        if not sku and json_ld_data:
            sku = str(json_ld_data.get("sku", ""))
    except Exception:
        pass

    # ── Category ─────────────────────────────────────────────────────
    category: str = ""
    try:
        if json_ld_data:
            category = json_ld_data.get("product_category", "")
            if not category:
                cats = json_ld_data.get("category", [])
                if isinstance(cats, list) and cats:
                    category = list(dict.fromkeys(cats))[-1] if cats else ""
                elif isinstance(cats, str):
                    category = cats
    except Exception:
        pass

    # ── Description ───────────────────────────────────────────────────
    description: str = ""
    try:
        description = page.evaluate(
            """() => {
                const ps = document.querySelectorAll('main p');
                for (const p of ps) {
                    const parent = p.parentElement;
                    if (parent && parent.className && parent.className.includes('text-regular-content')) {
                        const text = p.textContent.trim();
                        if (text.length > 30) return text;
                    }
                }
                return '';
            }"""
        ) or ""
        if not description and json_ld_data:
            description = json_ld_data.get("description", "")
    except Exception:
        if json_ld_data:
            description = json_ld_data.get("description", "")

    # ── Image ──────────────────────────────────────────────────────────
    image: str = ""
    try:
        if json_ld_data:
            img = json_ld_data.get("image", "")
            if isinstance(img, list) and img:
                image = img[0]
            elif isinstance(img, str):
                image = img
        if not image:
            image = page.evaluate(
                """() => {
                    const img = document.querySelector('img[src*="height=475"]');
                    return img ? img.src : '';
                }"""
            ) or ""
    except Exception:
        pass

    # Filter out non-product images
    if image:
        skip_patterns = [
            "/brand.assets/", "/emoji/", "/flags/", "/icon/", "/navigation/"
        ]
        if any(pat in image for pat in skip_patterns):
            image = ""

    # ── Rating & Review Count ──────────────────────────────────────────
    rating: str = ""
    review_count: str = ""
    try:
        if aggregate_rating:
            rating = str(aggregate_rating.get("ratingValue", ""))
            review_count = str(aggregate_rating.get("reviewCount", ""))
        elif json_ld_data and json_ld_data.get("aggregateRating"):
            ag = json_ld_data["aggregateRating"]
            if isinstance(ag, dict):
                rating = str(ag.get("ratingValue", ""))
                review_count = str(ag.get("reviewCount", ""))
    except Exception:
        pass

    # ── Assemble extra fields into remarks ─────────────────────────────
    extra_parts: list[str] = []
    if brand:
        extra_parts.append(f"Brand: {brand}")
    if sku:
        extra_parts.append(f"SKU: {sku}")
    if category:
        extra_parts.append(f"Category: {category}")
    if rating:
        extra_parts.append(f"Rating: {rating}/5")
    if review_count:
        extra_parts.append(f"Reviews: {review_count}")
    if description:
        desc_preview = description[:150] + ("..." if len(description) > 150 else "")
        extra_parts.append(f"Desc: {desc_preview}")
    if image:
        extra_parts.append(f"Image: {image}")

    if extra_parts:
        if product["remarks"]:
            product["remarks"] += " | " + " | ".join(extra_parts)
        else:
            product["remarks"] = " | ".join(extra_parts)

    return product


def _make_error_item(url: str, src_url: str, error: str) -> dict:
    """Create an error product item."""
    return {
        "id": 0,
        "title": "",
        "price": "",
        "availability": "",
        "original_price": "",
        "currency": "",
        "url": url,
        "src_url": src_url,
        "location": "",
        "status_code": 0,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": f"Error: {error[:200]}",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(
        description=f"{SITE_NAME} Navigation Scraper (Two-Phase)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--query", type=str, default=None,
        help="Search query for listing page discovery",
    )
    parser.add_argument(
        "--listing-url", type=str, default=None,
        help="Specific listing/category URL to browse for product links",
    )
    parser.add_argument(
        "--sitemap", action="store_true", default=False,
        help="Use sitemap XML for Phase 1 discovery",
    )
    parser.add_argument(
        "--input", type=str, default=None,
        help="Path to input URLs JSON file",
    )
    parser.add_argument(
        "--urls", type=str, nargs="+", default=None,
        help="Product URLs as CLI arguments",
    )
    parser.add_argument(
        "--sample", action="store_true",
        help="Scrape first 5 products only",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Max products to scrape",
    )
    parser.add_argument(
        "--no-proxy", action="store_true", default=True,
        help="Disable proxy (default: no proxy)",
    )
    parser.add_argument(
        "--headless", action="store_true", default=True,
        help="Run browser in headless mode",
    )
    parser.add_argument("--xvfb", action="store_true", default=False, help=argparse.SUPPRESS)
    args = parser.parse_args()

    limit = 5 if args.sample else args.limit

    start_time = time.time()
    discovered_urls: list[str] = []
    src_url_base: str = SITE_URL

    logger.info("=" * 80)
    logger.info("Starting %s Navigation Scraper", SITE_NAME)
    logger.info("Platform: %s | Method: %s", PLATFORM, SCRAPING_METHOD)
    logger.info(
        "Rate limit delay: %.1fs | Limit: %s",
        DELAY_BETWEEN_REQUESTS,
        limit or "none",
    )
    logger.info("=" * 80)

    # ── Determine URL source ──────────────────────────────────────────
    if args.input:
        input_path = args.input
        if not os.path.isabs(input_path):
            input_path = os.path.join(SCRIPT_DIR, input_path)
        logger.info("Reading URLs from file: %s", input_path)
        try:
            with open(input_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            discovered_urls = data.get("urls", [])
            src_url_base = "input_file"
            logger.info("Loaded %d URLs from file", len(discovered_urls))
        except Exception as exc:
            logger.error("Failed to read input file: %s", exc)
            sys.exit(1)

    elif args.urls:
        discovered_urls = list(args.urls)
        src_url_base = "cli_args"
        logger.info("Loaded %d URLs from CLI arguments", len(discovered_urls))

    elif args.listing_url:
        # User explicitly wants listing page discovery
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=args.headless, args=[])
            context = browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()
            discovered_urls = discover_urls_from_listing(page, args.listing_url, limit)
            src_url_base = args.listing_url
            browser.close()

    elif args.query or args.sitemap:
        # Explicit sitemap or query mode
        if args.sitemap:
            discovered_urls = discover_urls_from_sitemap(limit)
            src_url_base = "sitemap"
        else:
            # Query mode: try listing page first, fall back to sitemap
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=args.headless, args=[])
                context = browser.new_context(
                    viewport={"width": 1920, "height": 1080},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
                    ),
                )
                page = context.new_page()
                discovered_urls = discover_urls_from_listing(page, LISTING_URL, limit)
                src_url_base = LISTING_URL
                browser.close()

            # If listing page found no products, fall back to sitemap
            if not discovered_urls:
                logger.info(
                    "Listing page found no product URLs, falling back to sitemap..."
                )
                discovered_urls = discover_urls_from_sitemap(limit)
                src_url_base = "sitemap_fallback"

    else:
        # DEFAULT: Phase 1 sitemap discovery (most reliable for this SPA)
        # The /products listing page only shows category links, not products.
        # Sitemap XML directly contains all product URLs.
        logger.info(
            "Default mode: Phase 1 sitemap discovery (query: '%s')", DEFAULT_QUERY
        )
        discovered_urls = discover_urls_from_sitemap(limit)
        src_url_base = "sitemap"

    if not discovered_urls:
        logger.warning("No product URLs discovered. Exiting.")
        sys.exit(0)

    if limit:
        discovered_urls = discovered_urls[:limit]

    logger.info("=" * 80)
    logger.info("Phase 2: Extracting data from %d products", len(discovered_urls))
    logger.info("Source: %s", src_url_base)
    logger.info("=" * 80)

    # ── Phase 2: Scrape each product ──────────────────────────────────
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless, args=[])
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        results: list[dict] = []
        success_count = 0
        failed_count = 0
        total = len(discovered_urls)

        for i, url in enumerate(discovered_urls, 1):
            if i % 25 == 0 or i == 1:
                percent = (i / total) * 100
                logger.info(
                    "Progress: [%d/%d] (%.1f%%) — Success: %d, Failed: %d",
                    i,
                    total,
                    percent,
                    success_count,
                    failed_count,
                )

            logger.info("  [%d/%d] Scraping: %s", i, total, url[:100])

            try:
                product = extract_product_data(page, url, src_url_base)
                product["id"] = i
                results.append(product)

                if product.get("title"):
                    success_count += 1
                elif product.get("remarks") and "Error" in product.get("remarks", ""):
                    failed_count += 1
                else:
                    failed_count += 1

            except Exception as exc:
                logger.error("  Failed to extract %s: %s", url[:80], exc)
                error_item = _make_error_item(url, src_url_base, str(exc))
                error_item["id"] = i
                results.append(error_item)
                failed_count += 1

            # Rate limiting
            if i < total:
                time.sleep(DELAY_BETWEEN_REQUESTS)

        browser.close()

    # ── Write Output ──────────────────────────────────────────────────
    output = {
        "site": {
            "name": SITE_NAME,
            "url": SITE_URL,
            "platform": PLATFORM,
            "scraping_method": "playwright_navigation",
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        },
        OUTPUT_KEY: results,
        "metadata": {
            "scraping_duration_seconds": round(time.time() - start_time, 2),
            "discovered_urls": len(discovered_urls),
            "extracted_items": len(results),
            "failed_products": failed_count,
            "rate_limit_delay": DELAY_BETWEEN_REQUESTS,
        },
    }

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    output_filename = os.path.join(SCRIPT_DIR, f"output_{timestamp}.json")

    with open(output_filename, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False, default=str)

    logger.info("=" * 80)
    logger.info("EXTRACTION COMPLETE")
    logger.info(
        "Total: %d | Success: %d | Failed: %d",
        len(results),
        success_count,
        failed_count,
    )
    logger.info("Duration: %.1fs", time.time() - start_time)
    logger.info("Output: %s", output_filename)
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
