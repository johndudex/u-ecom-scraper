#!/usr/bin/env python3
"""
Adam & Eve Navigation Scraper - Two-Phase Architecture

Phase 1: Navigate the site to discover product URLs via search with load-more pagination
Phase 2: Scrape each discovered product page for structured data

Usage:
    python3 scraper.py                                 # search "lingerie" by default
    python3 scraper.py --query "vibrators"             # custom search query
    python3 scraper.py --input input_urls.json         # scrape from URL list
    python3 scraper.py --urls "https://..." "https://..."  # scrape specific URLs
    python3 scraper.py --sample                         # scrape first 5 items
    python3 scraper.py --limit 50                       # max 50 items
    python3 scraper.py --no-proxy                       # no proxy (default)
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
from urllib.parse import urlparse, urljoin

from playwright.sync_api import sync_playwright

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

SITE_NAME = "Adam & Eve"
SITE_URL = "https://www.adameve.com"
PLATFORM = "custom_aspnet"
SITE_SLUG = "adameve-com"

# Phase 1: Navigation / Discovery
DEFAULT_QUERY = "lingerie"
SEARCH_URL_BASE = "https://www.adameve.com/lingerie-ch-951.aspx?st=lingerie"

# Item link extraction (from navigation_analysis.json)
ITEM_CONTAINER_SELECTOR = '[data-cy="product-grid-item"]'
ITEM_LINK_SELECTOR = 'a[href*="/sp-"]'
ITEM_URL_PATTERN = re.compile(r"/sp-[A-Za-z0-9\-]+-\d+\.aspx")

# Pagination
PAGINATION_TYPE = "load_more"
LOAD_MORE_SELECTOR = "#load-more-component, .ae-plp__button a"
MAX_PAGES = 5

# Phase 2: Extraction
SCRAPING_METHOD = "playwright"
DELAY_BETWEEN_REQUESTS = 2.0
PAGE_LOAD_TIMEOUT = 30000
OUTPUT_KEY = "products"

# Soft 404 detection patterns
SOFT_404_PATTERNS = [
    "page not found",
    "product not found",
    "no longer available",
    "discontinued",
    "unavailable",
    "item not found",
    "we're sorry",
    "oops",
]

# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════════════

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(SCRIPT_DIR, "..", "..", "logs")
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
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════


def _normalize_url(href: str) -> str:
    """Normalize a URL to absolute form."""
    if not href:
        return ""
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return SITE_URL.rstrip("/") + href
    return href


def _is_valid_product_url(url: str) -> bool:
    """Check if URL matches expected product URL pattern."""
    return bool(ITEM_URL_PATTERN.search(url))


def _map_availability(availability_raw: str) -> str:
    """Map schema.org availability URI to human-readable text."""
    if not availability_raw:
        return ""
    avail_lower = availability_raw.lower()
    if "instock" in avail_lower or "instock" in availability_raw:
        return "In Stock"
    if "outofstock" in avail_lower or "soldout" in avail_lower:
        return "Out of Stock"
    if "preorder" in avail_lower:
        return "Pre-order"
    if "limitedavailability" in avail_lower:
        return "Limited Stock"
    return availability_raw


def _check_soft_404(title: str, h1_text: str, final_url: str, original_url: str) -> str:
    """Detect soft 404 pages. Returns remark string if detected, empty string otherwise."""
    # Check title / h1 for not-found patterns
    for text in [title, h1_text]:
        if text:
            text_lower = text.lower()
            for pattern in SOFT_404_PATTERNS:
                if pattern in text_lower:
                    return f"Soft 404: '{pattern}' detected in page content"

    # Check if final URL after redirects is significantly different
    if original_url and final_url:
        orig_path = urlparse(original_url).path.rstrip("/")
        final_path = urlparse(final_url).path.rstrip("/")
        # Remove query strings for comparison
        if orig_path and final_path and orig_path != final_path:
            # If redirected to a completely different page (not just added query params)
            if not final_path.startswith(orig_path):
                return f"Soft 404: redirected from {orig_path} to {final_path}"

    return ""


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 1: URL DISCOVERY
# ═══════════════════════════════════════════════════════════════════════════════


def _discover_urls_via_search(
    page,
    query: str,
    max_pages: Optional[int] = None,
    limit: Optional[int] = None,
) -> tuple[list[str], str]:
    """Phase 1: Discover product URLs by searching the site.

    Returns a tuple of (discovered_urls, search_url_used).
    """
    # Build search URL by appending ?st={query} to the lingerie category page
    search_url = f"https://www.adameve.com/lingerie-ch-951.aspx?st={query}"
    logger.info("Phase 1: Searching for '%s' → %s", query, search_url)

    try:
        page.goto(search_url, timeout=PAGE_LOAD_TIMEOUT)
        page.wait_for_load_state("networkidle")
        time.sleep(2)
    except Exception as exc:
        logger.error("Phase 1: Failed to load search page: %s", exc)
        return [], search_url

    all_urls: list[str] = _extract_item_links(page)
    logger.info("Phase 1: Initial page → %d product links found", len(all_urls))

    # Handle load-more pagination
    pages_loaded = 1
    while pages_loaded < (max_pages or MAX_PAGES):
        if limit and len(all_urls) >= limit:
            logger.info("Phase 1: Reached limit=%d", limit)
            break

        loaded_more = _click_load_more(page)
        if not loaded_more:
            logger.info("Phase 1: No more items to load (page %d)", pages_loaded)
            break

        time.sleep(2)
        new_urls = _extract_item_links(page)
        new_count = len(set(new_urls) - set(all_urls))
        logger.info(
            "Phase 1: After load-more #%d → %d total links (%d new)",
            pages_loaded,
            len(new_urls),
            new_count,
        )

        if new_count == 0:
            logger.info("Phase 1: No new items after load-more, stopping")
            break

        all_urls = list(dict.fromkeys(all_urls + new_urls))
        pages_loaded += 1

    unique_urls = list(dict.fromkeys(all_urls))
    if limit:
        unique_urls = unique_urls[:limit]

    logger.info("Phase 1: Discovered %d unique product URLs", len(unique_urls))
    return unique_urls, search_url


def _click_load_more(page) -> bool:
    """Click the load-more button. Returns True if clicked successfully."""
    try:
        btn = page.query_selector(LOAD_MORE_SELECTOR)
        if not btn:
            logger.info("Phase 1: Load-more button not found")
            return False

        # Check if button is visible and enabled
        is_visible = btn.is_visible()
        if not is_visible:
            logger.info("Phase 1: Load-more button not visible")
            return False

        # Scroll button into view and click
        btn.scroll_into_view_if_needed()
        time.sleep(0.5)
        btn.click()
        # Wait for new content to load
        page.wait_for_timeout(3000)
        return True
    except Exception as exc:
        logger.warning("Phase 1: Error clicking load-more: %s", exc)
        return False


def _extract_item_links(page) -> list[str]:
    """Extract product page URLs from a listing page."""
    links: list[str] = []
    seen: set[str] = set()

    try:
        containers = page.query_selector_all(ITEM_CONTAINER_SELECTOR)
        logger.debug("Phase 1: Found %d product grid containers", len(containers))

        for container in containers:
            link_elements = container.query_selector_all(ITEM_LINK_SELECTOR)
            for link_el in link_elements:
                href = link_el.get_attribute("href") or ""
                if href:
                    full_url = _normalize_url(href)
                    # Strip query params for dedup
                    clean_url = full_url.split("?")[0]
                    if clean_url not in seen and _is_valid_product_url(clean_url):
                        seen.add(clean_url)
                        links.append(full_url)
    except Exception as exc:
        logger.warning("Phase 1: Error extracting item links from containers: %s", exc)

    # Fallback: try without container
    if not links:
        try:
            link_elements = page.query_selector_all(ITEM_LINK_SELECTOR)
            for link_el in link_elements:
                href = link_el.get_attribute("href") or ""
                if href:
                    full_url = _normalize_url(href)
                    clean_url = full_url.split("?")[0]
                    if clean_url not in seen and _is_valid_product_url(clean_url):
                        seen.add(clean_url)
                        links.append(full_url)
        except Exception as exc:
            logger.warning("Phase 1: Fallback link extraction failed: %s", exc)

    return links


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 2: ITEM EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════════


def _extract_product_data(page, url: str, src_url: str) -> dict:
    """Phase 2: Extract structured product data from a product page."""
    original_url = url

    try:
        page.goto(url, timeout=PAGE_LOAD_TIMEOUT)
        page.wait_for_load_state("networkidle")
        time.sleep(1)
    except Exception as exc:
        logger.error("Phase 2: Failed to load %s: %s", url[:80], exc)
        return _error_product(url, src_url, str(exc))

    final_url = page.url
    status_code = 200

    product: dict = {
        "id": 0,
        "title": "",
        "price": "",
        "availability": "",
        "original_price": "",
        "currency": "",
        "url": original_url,
        "src_url": src_url,
        "location": "",
        "status_code": status_code,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": "",
        "brand": "",
        "description": "",
        "sku": "",
        "mpn": "",
        "rating": "",
        "review_count": "",
        "category": "",
        "images": [],
        "variants": [],
    }

    try:
        # ── JSON-LD Extraction ──────────────────────────────────────────
        json_ld_blocks = page.evaluate(
            "() => { "
            "  var scripts = document.querySelectorAll('script[type=\"application/ld+json\"]'); "
            "  var results = []; "
            "  for (var i = 0; i < scripts.length; i++) { "
            "    try { results.push(JSON.parse(scripts[i].textContent)); } "
            "    catch(e) { results.push(null); } "
            "  } "
            "  return results; "
            "}"
        )

        product_found = False
        breadcrumb_parts = []

        for block in json_ld_blocks:
            if not block:
                continue
            block_type = block.get("@type", "")

            # Extract category from BreadcrumbList
            if isinstance(block_type, str) and block_type == "BreadcrumbList":
                items = block.get("itemListElement", [])
                for item in items:
                    pos = item.get("position", 0)
                    name = item.get("name", "")
                    if pos >= 2:  # Skip root (position 1 = "Adam & Eve")
                        breadcrumb_parts.append(name)

            # Extract product data from Product block
            if isinstance(block_type, str) and block_type == "Product":
                product_found = True
                _extract_from_jsonld(block, product)

        if breadcrumb_parts:
            product["category"] = " > ".join(breadcrumb_parts)

        # ── Soft 404 Detection ──────────────────────────────────────────
        h1_text = ""
        try:
            h1_el = page.query_selector("h1")
            if h1_el:
                h1_text = (h1_el.text_content() or "").strip()
        except Exception:
            pass

        soft_404 = _check_soft_404(product.get("title", ""), h1_text, final_url, original_url)
        if soft_404:
            product["remarks"] = soft_404
            if not product_found:
                # No Product JSON-LD found — likely not a product page
                product["availability"] = "Out of Stock"
                return product

        # ── CSS Fallback Extraction (for fields JSON-LD misses) ────────
        _extract_from_css(page, product)

        # ── Format Fields ───────────────────────────────────────────────
        _format_product_fields(product)

    except Exception as exc:
        logger.warning("Phase 2: Extraction error for %s: %s", url[:60], exc)
        product["remarks"] = f"Partial extraction error: {str(exc)[:200]}"

    return product


def _extract_from_jsonld(block: dict, product: dict) -> None:
    """Extract fields from a JSON-LD Product block.

    Adam & Eve uses capitalized field names: Name, Mpn, Image, Description, Brand, Offers.
    """
    # Title — try Name (capitalized) then name (lowercase)
    product["title"] = block.get("Name") or block.get("name") or ""

    # SKU / MPN
    product["sku"] = block.get("Mpn") or block.get("mpn") or ""
    product["mpn"] = product["sku"]

    # Description
    product["description"] = block.get("Description") or block.get("description") or ""

    # Brand
    brand = block.get("Brand") or block.get("brand")
    if isinstance(brand, dict):
        product["brand"] = brand.get("Name") or brand.get("name") or ""
    elif isinstance(brand, str):
        product["brand"] = brand

    # Images
    images = block.get("Image") or block.get("image") or []
    if isinstance(images, str):
        images = [images]
    product["images"] = images

    # Offers — handle both capitalized and lowercase field names
    offers = block.get("Offers") or block.get("offers") or {}
    if isinstance(offers, list) and offers:
        offers = offers[0]

    if isinstance(offers, dict):
        # Price — regular list price from JSON-LD
        raw_price = offers.get("Price") or offers.get("price") or ""
        if raw_price:
            product["original_price"] = str(raw_price)

        # Currency
        currency = offers.get("PriceCurrency") or offers.get("priceCurrency") or "USD"
        product["currency"] = currency

        # Availability
        avail = offers.get("Availability") or offers.get("availability") or ""
        product["availability"] = _map_availability(avail)

    # Rating
    aggregate = block.get("AggregateRating") or block.get("aggregateRating")
    if isinstance(aggregate, dict):
        rating_val = aggregate.get("RatingValue") or aggregate.get("ratingValue")
        if rating_val is not None:
            product["rating"] = str(round(float(rating_val), 1))

        review_count = aggregate.get("ReviewCount") or aggregate.get("reviewCount")
        if review_count is not None:
            product["review_count"] = str(int(review_count))


def _extract_from_css(page, product: dict) -> None:
    """Extract fields from the DOM using CSS selectors.

    Used for promo price (not in JSON-LD), images, and variants.
    """
    # ── Promo/Sale Price (CSS primary) ──────────────────────────────────
    if not product.get("price"):
        try:
            promo_price = page.evaluate(
                "() => { "
                "  var el = document.querySelector('#product-details .jcpoffer-pricerange.v1'); "
                "  if (el) return el.textContent.trim(); "
                "  el = document.querySelector('.jcpoffer-pricerange'); "
                "  if (el) return el.textContent.trim(); "
                "  return ''; "
                "}"
            )
            if promo_price:
                product["price"] = promo_price.strip()
        except Exception:
            pass

    # ── Title fallback ───────────────────────────────────────────────────
    if not product.get("title"):
        try:
            h1 = page.query_selector("h1.item_title")
            if h1:
                product["title"] = (h1.text_content() or "").strip()
            else:
                h1 = page.query_selector("h1")
                if h1:
                    product["title"] = (h1.text_content() or "").strip()
        except Exception:
            pass

    # ── Original Price (CSS fallback) ───────────────────────────────────
    if not product.get("original_price"):
        try:
            was_price = page.evaluate(
                "() => { "
                "  var el = document.querySelector('#product-details .ae-price--normal'); "
                "  if (el) return el.textContent.trim(); "
                "  el = document.querySelector('.ae-price--was.v1'); "
                "  if (el) return el.textContent.trim(); "
                "  return ''; "
                "}"
            )
            if was_price:
                # Extract numeric value from e.g. "Reg $54.99"
                match = re.search(r"[\d,]+\.?\d*", was_price)
                if match:
                    product["original_price"] = match.group(0)
        except Exception:
            pass

    # ── Currency fallback (meta tag) ─────────────────────────────────────
    if not product.get("currency"):
        try:
            currency = page.evaluate(
                "() => { "
                "  var el = document.querySelector('meta[property=\"og:price:currency\"]'); "
                "  return el ? el.getAttribute('content') : ''; "
                "}"
            )
            if currency:
                product["currency"] = currency
        except Exception:
            pass

    # ── Images (product gallery only) ───────────────────────────────────
    if not product.get("images"):
        try:
            gallery_images = page.evaluate(
                "() => { "
                "  var imgs = document.querySelectorAll('.ae-pdp-thumb img'); "
                "  var result = []; "
                "  for (var i = 0; i < imgs.length; i++) { "
                "    var src = imgs[i].src || imgs[i].getAttribute('data-src') || ''; "
                "    if (src && src.indexOf('/cms/image/') !== -1) { "
                "      result.push(src); "
                "    } "
                "  } "
                "  return result; "
                "}"
            )
            if gallery_images:
                product["images"] = gallery_images
        except Exception:
            pass

        # Fallback to og:image if no gallery images
        if not product.get("images"):
            try:
                og_image = page.evaluate(
                    "() => { "
                    "  var el = document.querySelector('meta[property=\"og:image\"]'); "
                    "  return el ? el.getAttribute('content') : ''; "
                    "}"
                )
                if og_image:
                    product["images"] = [og_image]
            except Exception:
                pass

    # ── Variants (size options) ──────────────────────────────────────────
    try:
        variants = page.evaluate(
            "() => { "
            "  var els = document.querySelectorAll('.ae-sizes__text.size-swatch'); "
            "  var result = []; "
            "  for (var i = 0; i < els.length; i++) { "
            "    var text = els[i].textContent.trim(); "
            "    if (text) result.push(text); "
            "  } "
            "  return result; "
            "}"
        )
        if variants:
            product["variants"] = variants
    except Exception:
        pass

    # ── Description fallback ───────────────────────────────────────────
    if not product.get("description"):
        try:
            desc_el = page.query_selector(".ae-product-description")
            if desc_el:
                product["description"] = (desc_el.text_content() or "").strip()
        except Exception:
            pass


def _format_product_fields(product: dict) -> None:
    """Format and validate extracted product fields."""
    # Ensure price has currency symbol
    if product.get("price"):
        price = product["price"]
        if not re.match(r"^[\$\€\£\¥]", price):
            currency = product.get("currency", "USD")
            symbol = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency, "$")
            product["price"] = f"{symbol}{price}"

    # Ensure original_price has currency symbol
    if product.get("original_price"):
        orig = product["original_price"]
        if not re.match(r"^[\$\€\£\¥]", orig):
            currency = product.get("currency", "USD")
            symbol = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency, "$")
            product["original_price"] = f"{symbol}{orig}"

    # If no promo price found, use original_price as price
    if not product.get("price") and product.get("original_price"):
        product["price"] = product["original_price"]
        product["original_price"] = ""

    # Clean up description length
    if product.get("description") and len(product["description"]) > 2000:
        product["description"] = product["description"][:2000] + "..."


def _error_product(url: str, src_url: str, error: str) -> dict:
    """Create an error product record."""
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
        "brand": "",
        "description": "",
        "sku": "",
        "mpn": "",
        "rating": "",
        "review_count": "",
        "category": "",
        "images": [],
        "variants": [],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(description=f"{SITE_NAME} Navigation Scraper")
    parser.add_argument("--query", type=str, help="Search query for discovery mode")
    parser.add_argument("--input", type=str, help="Path to input URLs JSON file")
    parser.add_argument("--urls", nargs="+", help="Product URLs as CLI arguments")
    parser.add_argument("--sample", action="store_true", help="Scrape first 5 items only")
    parser.add_argument("--limit", type=int, default=None, help="Max items to scrape")
    parser.add_argument("--no-proxy", action="store_true", default=True, help="No proxy (default)")
    parser.add_argument("--headless", action="store_true", default=True, help="Headless mode")
    parser.add_argument("--xvfb", action="store_true", default=True, help=argparse.SUPPRESS)
    args, _ = parser.parse_known_args()

    limit = 5 if args.sample else args.limit

    start_time = time.time()
    discovered_urls: list[str] = []
    src_url_base = SITE_URL

    logger.info("=" * 80)
    logger.info("Starting scraper for %s", SITE_NAME)
    logger.info("=" * 80)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=args.headless,
            args=[],
        )
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        # ── Determine URL source ────────────────────────────────────────
        if args.input:
            # Read from input file (--input mode)
            try:
                with open(args.input, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    discovered_urls = data.get("urls", [])
                logger.info("Loaded %d URLs from %s", len(discovered_urls), args.input)
            except Exception as exc:
                logger.error("Failed to read input file: %s", exc)
                browser.close()
                sys.exit(1)
            src_url_base = ""

        elif args.urls:
            # Use URLs from CLI (--urls mode)
            discovered_urls = list(args.urls)
            logger.info("Using %d URLs from CLI arguments", len(discovered_urls))
            src_url_base = ""

        elif args.query:
            # Phase 1: Search discovery (--query mode)
            discovered_urls, src_url_base = _discover_urls_via_search(
                page, args.query, MAX_PAGES, limit
            )

        else:
            # Default: Phase 1 search discovery with DEFAULT_QUERY
            logger.info("No --query/--input/--urls provided; using default search: '%s'", DEFAULT_QUERY)
            discovered_urls, src_url_base = _discover_urls_via_search(
                page, DEFAULT_QUERY, MAX_PAGES, limit
            )

        # Apply limit
        if limit and len(discovered_urls) > limit:
            discovered_urls = discovered_urls[:limit]

        if not discovered_urls:
            logger.warning("No product URLs to scrape")
            browser.close()
            sys.exit(0)

        logger.info("Total products to scrape: %d", len(discovered_urls))
        logger.info("=" * 80)

        # ── Phase 2: Extract data from each URL ────────────────────────
        results: list[dict] = []
        total = len(discovered_urls)
        success = 0
        failed = 0

        for i, url in enumerate(discovered_urls, 1):
            # Determine src_url
            if src_url_base:
                item_src_url = src_url_base
            elif args.input:
                item_src_url = url
            else:
                item_src_url = url

            if i % 25 == 0 or i == 1:
                percent = (i / total) * 100
                logger.info(
                    "Progress: [%d/%d] (%.1f%%) — Success: %d, Failed: %d",
                    i, total, percent, success, failed,
                )
            logger.info("Scraping: %s", url[:120])

            try:
                product = _extract_product_data(page, url, item_src_url)
                product["id"] = i
                results.append(product)

                if product.get("title") and not product.get("remarks", "").startswith("Error"):
                    success += 1
                else:
                    failed += 1
            except Exception as exc:
                logger.error("Failed to extract %s: %s", url[:80], exc)
                error_prod = _error_product(url, item_src_url, str(exc))
                error_prod["id"] = i
                results.append(error_prod)
                failed += 1

            if i < total:
                time.sleep(DELAY_BETWEEN_REQUESTS)

        browser.close()

    # ── Write output ────────────────────────────────────────────────────
    duration = round(time.time() - start_time, 2)

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
            "scraping_duration_seconds": duration,
            "failed_products": failed,
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
        "Total: %d, Success: %d, Failed: %d",
        len(results), success, failed,
    )
    logger.info("Duration: %.1fs → %s", duration, output_filename)
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
