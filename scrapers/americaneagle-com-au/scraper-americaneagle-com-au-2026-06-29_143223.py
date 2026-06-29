#!/usr/bin/env python3
"""
American Eagle Australia - Navigation Scraper (Two-Phase Architecture)

Phase 1: Discover product URLs via search/category pages (HTTP requests + BeautifulSoup)
Phase 2: Extract product data from each URL via Shopify JSON API

Platform: Shopify (confirmed via cdn.shopify.com CDN, theme paths, shopify tags)
Extraction: /products/{handle}.json Shopify public API
Anti-bot: None detected - direct HTTP works without proxy
Currency: AUD

Usage:
    python3 scraper_draft.py                                    # default: search "pants"
    python3 scraper_draft.py --query "jeans"                    # custom search query
    python3 scraper_draft.py --category-url "https://americaneagle.com.au/collections/american-eagle-sale"
    python3 scraper_draft.py --sample                           # scrape first 5 products
    python3 scraper_draft.py --limit 50                         # max 50 products
    python3 scraper_draft.py --urls URL1 URL2 ...               # scrape specific product URLs
    python3 scraper_draft.py --input custom_urls.json            # read product URLs from file
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse, parse_qs, urlencode

import requests
from bs4 import BeautifulSoup

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

SITE_NAME = "American Eagle Australia"
SITE_URL = "https://americaneagle.com.au"
PLATFORM = "shopify"
SITE_SLUG = "americaneagle-com-au"
SCRAPING_METHOD = "http_requests"
CURRENCY_CODE = "AUD"
CURRENCY_SYMBOL = "$"

# Rate limiting
DELAY = 1.0  # seconds between requests (Shopify API rate limit caution)

# HTTP settings
REQUEST_TIMEOUT = 15
MAX_RETRIES = 3
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en-US;q=0.9,en;q=0.8",
}

# Phase 1: Navigation Configuration
DEFAULT_QUERY = "pants"
SEARCH_URL_PATTERN = f"{SITE_URL}/search?q={{query}}&options%5Bprefix%5D=last"
SEARCH_WORKING_URL = f"{SITE_URL}/search?q=pants&options%5Bprefix%5D=last"
CATEGORY_URLS = [
    "https://americaneagle.com.au/collections/american-eagle-sale",
    "https://americaneagle.com.au/collections/american-eagle-new-arrivals",
    "https://americaneagle.com.au/collections/american-eagle-men",
    "https://americaneagle.com.au/collections/american-eagle-men-jeans",
    "https://americaneagle.com.au/collections/american-eagle-men-bottoms",
    "https://americaneagle.com.au/collections/american-eagle-men-bottoms-pants",
    "https://americaneagle.com.au/collections/american-eagle-men-tops",
    "https://americaneagle.com.au/collections/aerie-new-arrivals",
    "https://americaneagle.com.au/collections/off-campus",
    "https://americaneagle.com.au/collections/american-eagle-new-arrivals-women",
]

# Phase 1: Pagination
PAGINATION_TYPE = "next_button"
NEXT_BUTTON_SELECTOR = 'a[rel="next"]'
PAGE_PARAM_NAME = "page"
MAX_PAGES = None  # no limit

# Phase 1: Item Link Extraction
ITEM_CONTAINER_SELECTOR = "[data-product-id]"
ITEM_LINK_SELECTOR = 'a[href*="/products/"]'
ITEM_URL_PATTERN = re.compile(r"/products/([A-Za-z0-9\-_]+)")

# Paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(SCRIPT_DIR, "..", "logs")
LOG_FILE = os.path.join(LOG_DIR, f"{SITE_SLUG}.log")

# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════════════

os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(SITE_SLUG)


# ═══════════════════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Product:
    """Standard product output structure."""
    id: int = 0
    title: str = ""
    price: str = ""
    availability: str = ""
    original_price: str = ""
    currency: str = ""
    url: str = ""
    src_url: str = ""
    location: str = ""
    status_code: int = 0
    scraped_at: str = ""
    remarks: str = ""
    # Extended fields
    brand: str = ""
    description: str = ""
    images: list = field(default_factory=list)
    sku: str = ""
    category: str = ""
    tags: str = ""
    variant_id: str = ""
    variant_name: str = ""
    variant_size: str = ""
    variant_color: str = ""


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

def _http_get(url: str, session: requests.Session) -> requests.Response:
    """Make a rate-limited HTTP GET request with retries."""
    time.sleep(DELAY)
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            wait = DELAY * (2 ** attempt)
            logger.warning("Retry %d/%d for %s: %s (wait %.1fs)", attempt + 1, MAX_RETRIES, url[:80], exc, wait)
            time.sleep(wait)
    logger.error("Failed after %d retries for %s: %s", MAX_RETRIES, url[:80], last_exc)
    raise last_exc  # type: ignore


def _normalize_url(href: str) -> str:
    """Normalize a URL: make absolute, strip trailing slashes, remove fragments."""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    elif href.startswith("/"):
        href = SITE_URL.rstrip("/") + href
    # Remove fragment
    href = href.split("#")[0]
    # Normalize query string order
    parsed = urlparse(href)
    if parsed.query:
        params = parse_qs(parsed.query, keep_blank_values=True)
        # Keep variant param but sort
        normalized_query = urlencode(params, doseq=True)
        href = f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{normalized_query}"
    else:
        href = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return href


def _extract_handle_from_url(url: str) -> Optional[str]:
    """Extract product handle from a /products/{handle} URL."""
    parsed = urlparse(url)
    match = ITEM_URL_PATTERN.search(parsed.path)
    if match:
        return match.group(1)
    return None


def _strip_html(html: str) -> str:
    """Strip HTML tags and decode entities from a string."""
    if not html:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", html)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()

    # Remove duplicated trailing text (common in Shopify body_html where
    # the intro paragraph appears in both the description and detail sections)
    lines = text.split("\n")
    # Check if the first non-empty line appears again near the end
    first_line = ""
    for line in lines:
        stripped = line.strip()
        if stripped:
            first_line = stripped
            break
    if first_line and len(first_line) > 20 and len(lines) > 4:
        # Check last 3 lines for the first_line content
        tail = "\n".join(l.strip() for l in lines[-3:]).strip()
        if tail.startswith(first_line):
            # Remove the duplicated portion from the end
            deduped = "\n".join(lines[:-1]).strip()
            # Remove trailing whitespace/newlines again
            deduped = re.sub(r"\n{3,}", "\n\n", deduped).strip()
            if len(deduped) > len(first_line):
                text = deduped

    return text


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 1: URL DISCOVERY (HTTP Requests + BeautifulSoup)
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_item_links_from_html(html: str) -> list[str]:
    """Extract product URLs from listing page HTML using BeautifulSoup."""
    urls: list[str] = []
    try:
        soup = BeautifulSoup(html, "html.parser")

        # Primary: extract links from within [data-product-id] containers
        containers = soup.select(ITEM_CONTAINER_SELECTOR)
        if containers:
            for container in containers:
                links = container.select(ITEM_LINK_SELECTOR)
                for link in links:
                    href = link.get("href", "")
                    if href and "/products/" in href:
                        urls.append(_normalize_url(href))
        else:
            # Fallback: extract all product links from the page
            links = soup.select(ITEM_LINK_SELECTOR)
            for link in links:
                href = link.get("href", "")
                if href and "/products/" in href:
                    urls.append(_normalize_url(href))
    except Exception as exc:
        logger.warning("Error extracting item links from HTML: %s", exc)
    return urls


def _get_next_page_url_from_html(html: str, current_url: str, next_page_num: int) -> Optional[str]:
    """Determine the next page URL from listing page HTML."""
    try:
        soup = BeautifulSoup(html, "html.parser")

        if PAGINATION_TYPE == "next_button":
            next_btn = soup.select_one(NEXT_BUTTON_SELECTOR)
            if next_btn:
                href = next_btn.get("href", "")
                if href:
                    return _normalize_url(href)
                # Check if it's a link with text content but no href (SPA-style) - skip for HTTP
        elif PAGINATION_TYPE == "page_param":
            separator = "&" if "?" in current_url else "?"
            return f"{current_url}{separator}{PAGE_PARAM_NAME}={next_page_num}"
    except Exception as exc:
        logger.warning("Error finding next page: %s", exc)
    return None


def _discover_urls_via_search(
    session: requests.Session,
    query: str,
    max_pages: Optional[int] = None,
    limit: Optional[int] = None,
) -> tuple[list[str], str]:
    """Phase 1a: Discover product URLs by searching the site.

    Returns:
        Tuple of (list of product URLs, the search URL used as src_url).
    """
    search_url = SEARCH_URL_PATTERN.replace("{query}", query)
    logger.info("Phase 1: Searching for '%s' → %s", query, search_url)

    try:
        resp = _http_get(search_url, session)
        resp.raise_for_status()
    except Exception as exc:
        logger.error("Phase 1: Failed to fetch search page: %s", exc)
        return [], search_url

    all_urls: list[str] = _extract_item_links_from_html(resp.text)
    logger.info("Phase 1: Page 1 → %d product links found", len(all_urls))

    current_page = 1
    current_url = search_url

    while True:
        if max_pages and current_page >= max_pages:
            logger.info("Phase 1: Reached max_pages=%d", max_pages)
            break
        if limit and len(all_urls) >= limit:
            logger.info("Phase 1: Reached limit=%d product URLs", limit)
            break

        next_url = _get_next_page_url_from_html(resp.text, current_url, current_page + 1)
        if not next_url:
            logger.info("Phase 1: No more pages (page %d)", current_page)
            break

        logger.info("Phase 1: Navigating to page %d → %s", current_page + 1, next_url[:100])
        try:
            resp = _http_get(next_url, session)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("Phase 1: Failed to fetch page %d: %s", current_page + 1, exc)
            break

        current_url = next_url
        new_urls = _extract_item_links_from_html(resp.text)
        new_count = len(set(new_urls) - set(all_urls))
        logger.info(
            "Phase 1: Page %d → %d items (%d new)",
            current_page + 1,
            len(new_urls),
            new_count,
        )

        if new_count == 0:
            logger.info("Phase 1: No new items on page %d, stopping", current_page + 1)
            break

        all_urls.extend(new_urls)
        current_page += 1

    # Deduplicate by product handle (strip query params to avoid
    # duplicates from different tracking/variant query strings)
    seen_handles: set[str] = set()
    unique_urls: list[str] = []
    for u in all_urls:
        handle = _extract_handle_from_url(u)
        if handle and handle not in seen_handles:
            seen_handles.add(handle)
            unique_urls.append(u)
    if limit:
        unique_urls = unique_urls[:limit]

    logger.info("Phase 1: Discovered %d unique product URLs", len(unique_urls))
    return unique_urls, search_url


def _discover_urls_via_category(
    session: requests.Session,
    category_url: str,
    max_pages: Optional[int] = None,
    limit: Optional[int] = None,
) -> tuple[list[str], str]:
    """Phase 1b: Discover product URLs from a category/collection page.

    Returns:
        Tuple of (list of product URLs, the category URL used as src_url).
    """
    logger.info("Phase 1: Browsing category → %s", category_url)

    try:
        resp = _http_get(category_url, session)
        resp.raise_for_status()
    except Exception as exc:
        logger.error("Phase 1: Failed to fetch category page: %s", exc)
        return [], category_url

    all_urls: list[str] = _extract_item_links_from_html(resp.text)
    logger.info("Phase 1: Category page 1 → %d product links", len(all_urls))

    current_page = 1
    current_url = category_url

    while True:
        if max_pages and current_page >= max_pages:
            break
        if limit and len(all_urls) >= limit:
            break

        next_url = _get_next_page_url_from_html(resp.text, current_url, current_page + 1)
        if not next_url:
            break

        logger.info("Phase 1: Category page %d → %s", current_page + 1, next_url[:100])
        try:
            resp = _http_get(next_url, session)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("Phase 1: Failed to fetch category page %d: %s", current_page + 1, exc)
            break

        current_url = next_url
        new_urls = _extract_item_links_from_html(resp.text)
        new_count = len(set(new_urls) - set(all_urls))

        if new_count == 0:
            break

        all_urls.extend(new_urls)
        current_page += 1

    seen_handles: set[str] = set()
    unique_urls: list[str] = []
    for u in all_urls:
        handle = _extract_handle_from_url(u)
        if handle and handle not in seen_handles:
            seen_handles.add(handle)
            unique_urls.append(u)
    if limit:
        unique_urls = unique_urls[:limit]

    logger.info("Phase 1: Discovered %d unique product URLs from category", len(unique_urls))
    return unique_urls, category_url


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 2: PRODUCT EXTRACTION (Shopify JSON API)
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_variant_id_from_url(url: str) -> Optional[int]:
    """Extract variant ID from URL query parameter ?variant=XXXXX."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    variant_ids = params.get("variant", [])
    if variant_ids:
        try:
            return int(variant_ids[0])
        except (ValueError, IndexError):
            pass
    return None


def _detect_soft_404(product_data: dict, url: str, handle: str) -> Optional[str]:
    """Detect soft 404: product not found, redirected, or unavailable.

    Returns:
        A remark string if soft 404 detected, None otherwise.
    """
    title = (product_data.get("title") or "").lower()
    handle_lower = handle.lower()

    # Check title for not-found indicators
    bad_patterns = [
        "page not found",
        "product not found",
        "404",
        "not available",
        "no longer available",
        "unavailable",
        "discontinued",
        "oops",
        "error",
    ]
    for pattern in bad_patterns:
        if pattern in title:
            return f"Soft 404: product not found ('{pattern}' in title)"

    # Check if product title matches handle (very short/empty product data)
    if not title or len(title) < 2:
        return "Soft 404: product data empty or missing title"

    return None


def _scrape_product_via_api(
    session: requests.Session,
    url: str,
    src_url: str,
) -> Product:
    """Phase 2: Extract product data using the Shopify JSON API.

    Fetches /products/{handle}.json and maps all fields.
    """
    product = Product()
    product.url = url
    product.src_url = src_url
    product.scraped_at = datetime.now(timezone.utc).isoformat()
    product.currency = CURRENCY_CODE

    # Extract handle from URL
    handle = _extract_handle_from_url(url)
    if not handle:
        product.remarks = "Could not extract product handle from URL"
        product.status_code = 0
        logger.warning("Cannot extract handle from %s", url)
        return product

    # Extract variant ID from URL (if present)
    url_variant_id = _extract_variant_id_from_url(url)

    # Build Shopify JSON API URL
    api_url = f"{SITE_URL}/products/{handle}.json"

    try:
        resp = _http_get(api_url, session)
        product.status_code = resp.status_code
        resp.raise_for_status()

        data = resp.json()
        product_data = data.get("product", {})

        if not product_data:
            product.remarks = "Soft 404: no product data in JSON response"
            return product

        # Soft 404 detection
        soft_404 = _detect_soft_404(product_data, url, handle)
        if soft_404:
            product.remarks = soft_404
            return product

        # ── Title ──────────────────────────────────────────────────
        product.title = (product_data.get("title") or "").strip()

        # ── Vendor / Brand ─────────────────────────────────────────
        product.brand = (product_data.get("vendor") or "").strip()

        # ── Description ─────────────────────────────────────────────
        body_html = product_data.get("body_html") or ""
        product.description = _strip_html(body_html)

        # ── Tags / Category ─────────────────────────────────────────
        # Shopify API may return tags as a string or a list depending on the store
        tags = product_data.get("tags", []) or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        product.tags = ", ".join(tags) if tags else ""

        # Extract category from cat: prefixed tags
        cat_tags = [t for t in tags if isinstance(t, str) and t.startswith("cat:")]
        if cat_tags:
            # Use the most specific (longest) category path
            product.category = max(cat_tags, key=len).replace("cat:", "")

        # ── Images ──────────────────────────────────────────────────
        images = product_data.get("images", []) or []
        product.images = [img.get("src", "") for img in images if img.get("src")]

        # ── Variants ────────────────────────────────────────────────
        variants = product_data.get("variants", []) or []
        if variants:
            # Select the matching variant or the first one
            matched_variant = variants[0]
            if url_variant_id is not None:
                for v in variants:
                    if v.get("id") == url_variant_id:
                        matched_variant = v
                        break

            product.variant_id = str(matched_variant.get("id", ""))
            product.variant_name = str(matched_variant.get("title") or "").strip()
            product.variant_size = str(matched_variant.get("option1") or "").strip()
            product.variant_color = str(matched_variant.get("option2") or "").strip()
            product.sku = str(matched_variant.get("sku") or "").strip()

            # ── Price (with currency symbol per formatting rules) ────
            raw_price = matched_variant.get("price") or ""
            if raw_price:
                try:
                    price_float = float(raw_price)
                    product.price = f"{CURRENCY_SYMBOL}{price_float:,.2f}"
                except (ValueError, TypeError):
                    product.price = f"{CURRENCY_SYMBOL}{raw_price}"

            # ── Original Price (compare_at_price) ───────────────────
            compare_price = matched_variant.get("compare_at_price") or ""
            if compare_price:
                try:
                    compare_float = float(compare_price)
                    product.original_price = f"{CURRENCY_SYMBOL}{compare_float:,.2f}"
                except (ValueError, TypeError):
                    product.original_price = f"{CURRENCY_SYMBOL}{compare_price}"

            # ── Currency from variant data if available ──────────────
            variant_currency = matched_variant.get("price_currency") or ""
            if variant_currency:
                product.currency = variant_currency

            # ── Availability ──────────────────────────────────────────
            inventory_mgmt = matched_variant.get("inventory_management") or ""
            available = matched_variant.get("available")
            if available is False:
                product.availability = "Out of Stock"
            elif inventory_mgmt:
                product.availability = "In Stock"
            else:
                product.availability = "In Stock"  # Default for Shopify

        # ── Construct canonical product URL ─────────────────────────
        product_handle = product_data.get("handle") or handle
        product.url = f"{SITE_URL}/products/{product_handle}"

    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            product.remarks = "Soft 404: product not found (404 from JSON API)"
            product.status_code = exc.response.status_code
        else:
            product.remarks = f"HTTP error: {exc}"
            product.status_code = getattr(exc.response, "status_code", 0)
        logger.error("Phase 2: HTTP error for %s: %s", url[:80], exc)
    except requests.RequestException as exc:
        product.remarks = f"Request error: {exc}"
        product.status_code = 0
        logger.error("Phase 2: Request error for %s: %s", url[:80], exc)
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        product.remarks = f"Parse error: {exc}"
        logger.error("Phase 2: Parse error for %s: %s", url[:80], exc)

    return product


# ═══════════════════════════════════════════════════════════════════════════════
# OUTPUT HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _product_to_dict(p: Product) -> dict:
    """Convert Product dataclass to output dictionary."""
    d = {
        "id": p.id,
        "title": p.title,
        "price": p.price,
        "availability": p.availability,
        "original_price": p.original_price,
        "currency": p.currency,
        "url": p.url,
        "src_url": p.src_url,
        "location": p.location,
        "status_code": p.status_code,
        "scraped_at": p.scraped_at,
        "remarks": p.remarks,
    }
    # Include extended fields only if they have values
    if p.brand:
        d["brand"] = p.brand
    if p.description:
        d["description"] = p.description
    if p.images:
        d["images"] = p.images
    if p.sku:
        d["sku"] = p.sku
    if p.category:
        d["category"] = p.category
    if p.tags:
        d["tags"] = p.tags
    if p.variant_id:
        d["variant_id"] = p.variant_id
    if p.variant_name:
        d["variant_name"] = p.variant_name
    if p.variant_size:
        d["variant_size"] = p.variant_size
    if p.variant_color:
        d["variant_color"] = p.variant_color
    return d


def _write_output(results: list[dict], start_time: float, failed_count: int) -> str:
    """Write results to timestamped JSON output file."""
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
            "failed_products": failed_count,
            "rate_limit_delay": DELAY,
        },
    }

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    output_filename = os.path.join(SCRIPT_DIR, f"output_{timestamp}.json")

    os.makedirs(SCRIPT_DIR, exist_ok=True)
    with open(output_filename, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False, default=str)

    return output_filename


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    """Main entry point for the two-phase navigation scraper."""
    parser = argparse.ArgumentParser(
        description=f"{SITE_NAME} Navigation Scraper (Two-Phase)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 scraper_draft.py                           # default: search 'pants'\n"
            "  python3 scraper_draft.py --query 'jeans'           # search for jeans\n"
            "  python3 scraper_draft.py --sample                 # scrape first 5 only\n"
            "  python3 scraper_draft.py --limit 50               # max 50 products\n"
            "  python3 scraper_draft.py --urls URL1 URL2         # specific URLs\n"
            "  python3 scraper_draft.py --input urls.json        # from file\n"
        ),
    )
    parser.add_argument("--query", type=str, help="Search query for Phase 1 discovery")
    parser.add_argument(
        "--category-url",
        type=str,
        help="Category/collection URL to crawl for Phase 1",
    )
    parser.add_argument(
        "--listing-url",
        type=str,
        help="Generic listing page URL to crawl for Phase 1",
    )
    parser.add_argument("--input", type=str, help="Path to input product URLs JSON file")
    parser.add_argument(
        "--urls",
        type=str,
        nargs="+",
        help="Product URLs to scrape directly (skip Phase 1)",
    )
    parser.add_argument(
        "--sample",
        action="store_true",
        help="Scrape only the first 5 products discovered",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max number of products to scrape",
    )
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        default=True,
        help="Disable proxy (default: no proxy for this site)",
    )
    args = parser.parse_args()

    # Determine effective limit
    limit = 5 if args.sample else args.limit

    logger.info("=" * 80)
    logger.info("Starting scraper for %s", SITE_NAME)
    logger.info("Platform: %s | Method: %s", PLATFORM, SCRAPING_METHOD)
    logger.info("=" * 80)

    session = requests.Session()
    start_time = time.time()
    discovered_urls: list[str] = []
    src_url_base: str = SITE_URL

    # ── Determine product URL source ──────────────────────────────────
    if args.urls:
        # Direct URL mode: skip Phase 1
        logger.info("Mode: Direct URLs (%d provided)", len(args.urls))
        discovered_urls = args.urls
        src_url_base = ""  # Each URL is its own source
    elif args.input:
        # Input file mode: skip Phase 1
        logger.info("Mode: Input file → %s", args.input)
        try:
            with open(args.input, "r", encoding="utf-8") as f:
                data = json.load(f)
            discovered_urls = data.get("urls", [])
            logger.info("Loaded %d URLs from input file", len(discovered_urls))
        except Exception as exc:
            logger.error("Failed to read input file: %s", exc)
            sys.exit(1)
    elif args.category_url:
        # Phase 1: Category discovery
        urls, src_url_base = _discover_urls_via_category(
            session, args.category_url, MAX_PAGES, limit
        )
        discovered_urls = urls
    elif args.listing_url:
        # Phase 1: Generic listing page discovery
        urls, src_url_base = _discover_urls_via_category(
            session, args.listing_url, MAX_PAGES, limit
        )
        discovered_urls = urls
    elif args.query:
        # Phase 1: Search discovery
        urls, src_url_base = _discover_urls_via_search(
            session, args.query, MAX_PAGES, limit
        )
        discovered_urls = urls
    else:
        # Default: Phase 1 search with DEFAULT_QUERY
        logger.info("Mode: Default search (query='%s')", DEFAULT_QUERY)
        urls, src_url_base = _discover_urls_via_search(
            session, DEFAULT_QUERY, MAX_PAGES, limit
        )
        discovered_urls = urls

    if not discovered_urls:
        logger.warning("No product URLs discovered. Exiting.")
        _write_output([], start_time, 0)
        sys.exit(0)

    # ── Phase 2: Extract product data ──────────────────────────────────
    logger.info("=" * 80)
    logger.info("Phase 2: Extracting data from %d products", len(discovered_urls))
    logger.info("=" * 80)

    results: list[dict] = []
    failed_count = 0
    total = len(discovered_urls)

    for i, url in enumerate(discovered_urls, 1):
        # Determine src_url: use the listing page if available, else the URL itself
        if src_url_base:
            item_src_url = src_url_base
        else:
            item_src_url = url

        # Progress reporting
        if i == 1 or i % 25 == 0 or i == total:
            percent = (i / total) * 100
            logger.info(
                "Progress: [%d/%d] (%.1f%%)", i, total, percent
            )
        logger.info("Scraping: %s", url[:120])

        try:
            product = _scrape_product_via_api(session, url, item_src_url)
            product.id = i
            result_dict = _product_to_dict(product)
            results.append(result_dict)

            if product.remarks and ("error" in product.remarks.lower() or "404" in product.remarks.lower()):
                failed_count += 1

            status = "OK" if product.title else "SKIP"
            logger.info(
                "  → %s | %s | %s",
                status,
                product.title[:60] if product.title else "NO TITLE",
                product.price,
            )
        except Exception as exc:
            logger.error("Failed to scrape %s: %s", url[:80], exc)
            failed_count += 1
            error_product = Product(
                id=i, url=url, src_url=item_src_url,
                status_code=0,
                scraped_at=datetime.now(timezone.utc).isoformat(),
                remarks=f"Error: {str(exc)[:200]}",
            )
            results.append(_product_to_dict(error_product))

    # ── Write output ───────────────────────────────────────────────────
    output_filename = _write_output(results, start_time, failed_count)
    success_count = len(results) - failed_count

    duration = round(time.time() - start_time, 2)
    logger.info("=" * 80)
    logger.info("EXTRACTION COMPLETE")
    logger.info(
        "Total: %d, Success: %d, Failed: %d",
        len(results),
        success_count,
        failed_count,
    )
    logger.info("Duration: %.1f seconds", duration)
    logger.info("Output: %s", output_filename)
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
