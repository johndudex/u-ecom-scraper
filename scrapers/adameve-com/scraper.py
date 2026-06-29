#!/usr/bin/env python3
"""
Adam & Eve Navigation Scraper - Two-Phase Architecture

Phase 1: Discover product URLs via search/category pages (HTTP requests + BeautifulSoup)
Phase 2: Extract product data from each discovered page (HTTP requests)

Usage:
    python3 scraper.py                                # default: search "lingerie"
    python3 scraper.py --query "vibrators"             # search with custom query
    python3 scraper.py --category-url "https://..."    # crawl a specific category
    python3 scraper.py --input custom_urls.json        # scrape URLs from file
    python3 scraper.py --urls "https://..." "..."     # scrape specific URLs
    python3 scraper.py --sample                        # scrape first 5 items only
    python3 scraper.py --limit 50                      # max 50 items
    python3 scraper.py --no-proxy                       # disable proxy (default)
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
from urllib.parse import urljoin, urlparse, parse_qs, urlencode

import requests
from bs4 import BeautifulSoup

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

SITE_NAME = "Adam & Eve"
SITE_URL = "https://www.adameve.com"
PLATFORM = "custom"
SITE_SLUG = "adameve-com"
SCRAPING_METHOD = "http_requests"
DELAY = 2.0
MAX_PAGES = 5
DEFAULT_QUERY = "lingerie"
OUTPUT_KEY = "products"

# Phase 1: Navigation configuration (from navigation_analysis.json)
SEARCH_WORKING_URL = "https://www.adameve.com/lingerie-ch-951.aspx?st=lingerie"
CATEGORY_URLS = [
    "https://www.adameve.com/adult-sex-toys/womens-sex-toys-ch-955.aspx",
    "https://www.adameve.com/adult-sex-toys/vibrators-ch-1011.aspx",
    "https://www.adameve.com/adult-sex-toys/dildo-sex-toys-ch-1012.aspx",
    "https://www.adameve.com/lingerie-ch-951.aspx",
    "https://www.adameve.com/adult-sex-toys/anal-sex-toys-ch-1002.aspx",
    "https://www.adameve.com/adult-sex-toys/nipple-toys-c-1016.aspx",
    "https://www.adameve.com/adult-sex-toys/kinky-bondage-ch-1007.aspx",
    "https://www.adameve.com/adult-sex-toys-ch-1503.aspx",
]

ITEM_CONTAINER_SELECTOR = '[data-cy="product-grid-item"]'
ITEM_LINK_SELECTOR = '[data-cy="product-grid-item"] a[href]'
PAGE_PARAM_NAME = "pnum"

# Product URL filter — matches /sp-{slug}-{id}.aspx
PRODUCT_URL_REGEX = re.compile(r"/sp-[A-Za-z0-9\-]+-\d+\.aspx")

# serverSideEvents regex for dataLayer extraction
SERVER_SIDE_EVENTS_REGEX = re.compile(
    r"var\s+serverSideEvents\s*=\s*(\[[\s\S]*?\]);", re.DOTALL
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Connection": "keep-alive",
}

# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════════════

LOG_DIR = os.path.join(SCRIPT_DIR, "..", "logs")
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
# HTTP UTILITY
# ═══════════════════════════════════════════════════════════════════════════════


def fetch_page(url: str, timeout: int = 15) -> requests.Response:
    """Fetch a page with rate limiting and error handling.

    Returns the raw requests.Response object.
    Raises on network failures.
    """
    time.sleep(DELAY)
    try:
        response = requests.get(
            url, headers=HEADERS, timeout=timeout, allow_redirects=True
        )
        response.raise_for_status()
        return response
    except requests.RequestException as exc:
        logger.error("Failed to fetch %s: %s", url[:100], exc)
        raise


def fetch_soup(url: str, timeout: int = 15) -> tuple[BeautifulSoup, requests.Response]:
    """Fetch a URL and return (BeautifulSoup, Response)."""
    response = fetch_page(url, timeout)
    soup = BeautifulSoup(response.text, "html.parser")
    return soup, response


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 1: URL DISCOVERY
# ═══════════════════════════════════════════════════════════════════════════════


def _extract_product_links_from_html(soup: BeautifulSoup) -> list[str]:
    """Extract product page URLs from a listing page HTML.

    Uses verified selectors from scraper_analysis:
      - container: [data-cy="product-grid-item"]
      - link: [data-cy="product-grid-item"] a[href]
    Filters to keep only URLs matching the product URL pattern.
    """
    links: list[str] = []

    # Primary: extract from product grid containers
    try:
        containers = soup.select(ITEM_CONTAINER_SELECTOR)
        for container in containers:
            for link_el in container.select("a[href]"):
                href = link_el.get("href", "")
                if href:
                    full_url = urljoin(SITE_URL, href)
                    if PRODUCT_URL_REGEX.search(full_url):
                        links.append(full_url)
    except Exception as exc:
        logger.warning("Error extracting item links from containers: %s", exc)

    # Fallback: direct link selector
    if not links:
        try:
            for link_el in soup.select(ITEM_LINK_SELECTOR):
                href = link_el.get("href", "")
                if href:
                    full_url = urljoin(SITE_URL, href)
                    if PRODUCT_URL_REGEX.search(full_url):
                        links.append(full_url)
        except Exception as exc:
            logger.warning("Fallback link extraction failed: %s", exc)

    return links


def _build_paged_url(base_url: str, page_num: int) -> str:
    """Build a paginated URL by appending the pnum parameter."""
    parsed = urlparse(base_url)
    params = parse_qs(parsed.query)
    params["pnum"] = [str(page_num)]
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{urlencode(params, doseq=True)}"


def discover_urls_via_search(
    query: str,
    max_pages: Optional[int] = None,
    limit: Optional[int] = None,
) -> list[str]:
    """Phase 1a: Discover product URLs by searching the site.

    Uses the verified working search URL pattern from navigation_analysis.
    """
    if query == "lingerie":
        search_url = SEARCH_WORKING_URL
    else:
        # Construct search URL using the known pattern
        search_url = f"{SITE_URL}/search?st={query}"

    logger.info("Phase 1: Searching for '%s' -> %s", query, search_url)
    all_urls: list[str] = []

    # Page 1
    try:
        soup, _resp = fetch_soup(search_url)
        page_urls = _extract_product_links_from_html(soup)
        new_urls = [u for u in page_urls if u not in all_urls]
        all_urls.extend(new_urls)
        logger.info(
            "Phase 1: Page 1 -> %d items (%d new)", len(page_urls), len(new_urls)
        )
    except Exception as exc:
        logger.error("Phase 1: Failed to fetch search page: %s", exc)
        return all_urls[:limit] if limit else all_urls

    # Paginate via pnum parameter
    current_page = 1
    while True:
        if max_pages and current_page >= max_pages:
            logger.info("Phase 1: Reached max_pages=%d", max_pages)
            break
        if limit and len(all_urls) >= limit:
            logger.info("Phase 1: Reached limit=%d", limit)
            break

        current_page += 1
        next_url = _build_paged_url(search_url, current_page)

        try:
            soup, _resp = fetch_soup(next_url)
            page_urls = _extract_product_links_from_html(soup)
            new_urls = [u for u in page_urls if u not in all_urls]

            if not new_urls:
                logger.info(
                    "Phase 1: No new items on page %d, stopping", current_page
                )
                break

            all_urls.extend(new_urls)
            logger.info(
                "Phase 1: Page %d -> %d items (%d new)",
                current_page,
                len(page_urls),
                len(new_urls),
            )
        except Exception as exc:
            logger.warning("Phase 1: Failed to fetch page %d: %s", current_page, exc)
            break

    unique_urls = list(dict.fromkeys(all_urls))
    if limit:
        unique_urls = unique_urls[:limit]
    logger.info("Phase 1: Discovered %d total product URLs via search", len(unique_urls))
    return unique_urls


def discover_urls_via_category(
    category_url: str,
    max_pages: Optional[int] = None,
    limit: Optional[int] = None,
) -> list[str]:
    """Phase 1b: Discover product URLs from a category page.

    Uses pnum pagination parameter.
    """
    logger.info("Phase 1: Browsing category -> %s", category_url)
    all_urls: list[str] = []

    try:
        soup, _resp = fetch_soup(category_url)
        page_urls = _extract_product_links_from_html(soup)
        new_urls = [u for u in page_urls if u not in all_urls]
        all_urls.extend(new_urls)
        logger.info(
            "Phase 1: Page 1 -> %d items (%d new)", len(page_urls), len(new_urls)
        )
    except Exception as exc:
        logger.error("Phase 1: Failed to fetch category page: %s", exc)
        return all_urls[:limit] if limit else all_urls

    current_page = 1
    while True:
        if max_pages and current_page >= max_pages:
            break
        if limit and len(all_urls) >= limit:
            break

        current_page += 1
        next_url = _build_paged_url(category_url, current_page)

        try:
            soup, _resp = fetch_soup(next_url)
            page_urls = _extract_product_links_from_html(soup)
            new_urls = [u for u in page_urls if u not in all_urls]

            if not new_urls:
                logger.info(
                    "Phase 1: No new items on category page %d, stopping", current_page
                )
                break

            all_urls.extend(new_urls)
            logger.info(
                "Phase 1: Category page %d -> %d items (%d new)",
                current_page,
                len(page_urls),
                len(new_urls),
            )
        except Exception:
            break

    unique_urls = list(dict.fromkeys(all_urls))
    if limit:
        unique_urls = unique_urls[:limit]
    logger.info(
        "Phase 1: Discovered %d product URLs from category", len(unique_urls)
    )
    return unique_urls


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 2: PRODUCT EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════════


def _get_meta(soup: BeautifulSoup, property_name: str) -> Optional[str]:
    """Get content attribute of a meta tag by its property name."""
    meta = soup.find("meta", attrs={"property": property_name})
    if meta:
        return (meta.get("content") or "").strip()
    return None


def _extract_server_side_events(html: str) -> Optional[dict]:
    """Extract and parse the serverSideEvents JS variable from inline <script>.

    The dataLayer contains: ProductName, Price, Brand, Sku, Colors, Sizes,
    dimension11 (availability), dimension12 (review_count), dimension13 (rating),
    ecommerce.detail.products[0].category, etc.
    """
    try:
        match = SERVER_SIDE_EVENTS_REGEX.search(html)
        if match:
            data = json.loads(match.group(1))
            if data and isinstance(data, list) and len(data) > 0:
                return data[0]
    except (json.JSONDecodeError, IndexError) as exc:
        logger.warning("Failed to parse serverSideEvents: %s", exc)
    return None


def _detect_soft_404(
    soup: BeautifulSoup, requested_url: str, final_url: str
) -> Optional[str]:
    """Detect soft 404 pages and return a description, or None if valid.

    Checks:
    1. Final URL after redirects differs from product URL pattern
    2. Page title / H1 contains not-found indicators
    3. og:type is not 'product'
    """
    # Check redirect to non-product page
    if final_url != requested_url and not PRODUCT_URL_REGEX.search(final_url):
        return f"Soft 404: redirected to non-product page {final_url}"

    # Check H1 text
    h1 = soup.select_one("h1")
    if h1:
        h1_text = h1.get_text(strip=True).lower()
        indicators = [
            "not found",
            "unavailable",
            "discontinued",
            "no longer available",
            "page not found",
            "404",
            "product not found",
            "item not found",
        ]
        for indicator in indicators:
            if indicator in h1_text:
                return f"Soft 404: H1 contains '{indicator}'"

    # Check title tag
    title_tag = soup.find("title")
    if title_tag:
        title_text = title_tag.get_text(strip=True).lower()
        for indicator in ["not found", "404", "page not found"]:
            if indicator in title_text:
                return "Soft 404: page title indicates missing product"

    # Check og:type
    og_type = _get_meta(soup, "og:type")
    if og_type and og_type.strip().lower() != "product":
        return f"Soft 404: og:type is '{og_type}' not 'product'"

    return None


def _extract_gallery_images(soup: BeautifulSoup, og_image: Optional[str]) -> list[str]:
    """Extract product gallery images scoped to product containers.

    Rules:
    - Scope to product gallery containers only
    - Skip brand assets, emoji, flags, icons, navigation, logos
    - Cap at 15 images
    """
    images: list[str] = []
    if og_image:
        full = urljoin(SITE_URL, og_image) if og_image.startswith("/") else og_image
        images.append(full)

    skip_patterns = [
        "/brand.assets/",
        "/emoji/",
        "/flags/",
        "/icon/",
        "/navigation/",
        "/logo",
    ]

    # Try known product gallery selectors
    gallery_selectors = [
        '[data-auto-id="product-image"] img',
        ".product-gallery img",
        "#pdp-gallery img",
        '[data-testid*="gallery"] img',
        ".main-image img",
        ".pdp-image img",
        ".product-detail img",
    ]

    for selector in gallery_selectors:
        try:
            img_els = soup.select(selector)
            for img_el in img_els:
                src = (
                    img_el.get("src")
                    or img_el.get("data-src")
                    or img_el.get("data-lazy-src")
                    or ""
                )
                if not src:
                    continue
                full_src = urljoin(SITE_URL, src) if src.startswith("/") else src

                # Skip non-product images
                src_lower = src.lower()
                if any(p in src_lower for p in skip_patterns):
                    continue
                # Skip tiny images (likely icons/badges)
                if "1x1" in src or "blank.gif" in src:
                    continue

                if full_src not in images:
                    images.append(full_src)
        except Exception:
            continue

        if len(images) > 2:
            break

    return images[:15]


def extract_product_data(url: str, src_url: str) -> dict:
    """Phase 2: Extract structured product data from a single product page.

    Primary data source: OG meta tags
    Secondary data source: serverSideEvents dataLayer variable
    Clean title: h1 element
    """
    try:
        response = fetch_page(url, timeout=15)
    except Exception as exc:
        return _error_product(url, src_url, str(exc))

    status_code = response.status_code
    final_url = response.url
    html = response.text
    soup = BeautifulSoup(html, "html.parser")

    product: dict = {
        "id": 0,
        "title": "",
        "price": "",
        "availability": "",
        "original_price": "",
        "currency": "",
        "url": final_url,
        "src_url": src_url,
        "location": "",
        "status_code": status_code,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": "",
        "brand": "",
        "sku": "",
        "rating": "",
        "review_count": "",
        "category": "",
        "description": "",
        "images": [],
        "variants_colors": [],
        "variants_sizes": [],
    }

    # ── Soft 404 detection ──────────────────────────────────────────────
    soft_404 = _detect_soft_404(soup, url, final_url)
    if soft_404:
        product["remarks"] = soft_404
        logger.warning("Soft 404 detected: %s -> %s", url[:80], soft_404)
        return product

    # ── OG Meta Tags (primary data source) ──────────────────────────────
    og_title = _get_meta(soup, "og:title")
    og_url = _get_meta(soup, "og:url")
    og_desc = _get_meta(soup, "og:description")
    og_image = _get_meta(soup, "og:image")
    og_price_amount = _get_meta(soup, "og:price:amount")
    og_price_currency = _get_meta(soup, "og:price:currency")
    og_brand = _get_meta(soup, "og:brand")
    og_availability = _get_meta(soup, "og:availability")

    # ── serverSideEvents dataLayer (secondary) ──────────────────────────
    sse = _extract_server_side_events(html)

    # ── Title: prefer h1 (clean name) over OG title (includes branding) ──
    h1 = soup.select_one("h1")
    if h1:
        product["title"] = h1.get_text(strip=True)
    elif og_title:
        product["title"] = og_title

    # ── URL ──────────────────────────────────────────────────────────────
    if og_url:
        product["url"] = og_url

    # ── Price ────────────────────────────────────────────────────────────
    if og_price_amount:
        try:
            price_val = float(og_price_amount)
            currency = og_price_currency or "USD"
            product["currency"] = currency
            product["price"] = f"${price_val:,.2f}"
        except ValueError:
            product["price"] = og_price_amount
            product["currency"] = og_price_currency or "USD"
    elif sse and sse.get("Price"):
        try:
            price_val = float(str(sse["Price"]))
            product["price"] = f"${price_val:,.2f}"
        except ValueError:
            product["price"] = str(sse["Price"])
        product["currency"] = "USD"

    # ── Original Price: empty unless a clear "was" price is visible ──────
    # OG price:amount IS the regular/list price. Look for a visible sale price
    # in the HTML that doesn't require a promo code.
    # For simplicity, leave original_price empty (price is the current price).
    product["original_price"] = ""

    # ── Availability ────────────────────────────────────────────────────
    if og_availability:
        avail_lower = og_availability.lower()
        if avail_lower == "instock" or "in" in avail_lower:
            product["availability"] = "In Stock"
        elif "out" in avail_lower or "oos" in avail_lower or "discontinu" in avail_lower:
            product["availability"] = "Out of Stock"
        else:
            product["availability"] = "In Stock"
    elif sse:
        dim11 = str(sse.get("dimension11", ""))
        if "In Stock" in dim11:
            product["availability"] = "In Stock"
        elif "Out of Stock" in dim11 or "Out" in dim11:
            product["availability"] = "Out of Stock"
        elif dim11:
            product["availability"] = dim11
        else:
            product["availability"] = "In Stock"
    else:
        product["availability"] = "In Stock"

    # ── Currency ─────────────────────────────────────────────────────────
    if not product["currency"]:
        product["currency"] = og_price_currency or "USD"

    # ── Brand ──────────────────────────────────────────────────────────
    if og_brand:
        product["brand"] = og_brand
    elif sse and sse.get("Brand"):
        product["brand"] = str(sse["Brand"])

    # ── SKU ─────────────────────────────────────────────────────────────
    if sse and sse.get("Sku"):
        product["sku"] = str(sse["Sku"])

    # ── Rating (round to 1 decimal) ────────────────────────────────────
    if sse and sse.get("dimension13") is not None:
        try:
            rating = float(sse["dimension13"])
            product["rating"] = f"{rating:.1f}"
        except (ValueError, TypeError):
            product["rating"] = str(sse["dimension13"])

    # ── Review Count ───────────────────────────────────────────────────
    if sse and sse.get("dimension12") is not None:
        try:
            rc = int(float(str(sse["dimension12"])))
            product["review_count"] = str(rc)
        except (ValueError, TypeError):
            product["review_count"] = str(sse["dimension12"])

    # ── Category ─────────────────────────────────────────────────────────
    if sse:
        try:
            ecommerce = sse.get("ecommerce", {})
            detail = ecommerce.get("detail", {})
            prods = detail.get("products", [])
            if isinstance(prods, list) and prods:
                cat = prods[0].get("category", "")
                if cat:
                    product["category"] = cat
        except (AttributeError, IndexError, TypeError):
            pass

    # ── Description ──────────────────────────────────────────────────────
    if og_desc:
        product["description"] = og_desc
    else:
        # Fallback: look for a description section
        try:
            desc_el = soup.select_one("meta[name='description']")
            if desc_el:
                product["description"] = desc_el.get("content", "")
        except Exception:
            pass

    # ── Images (scoped to product gallery) ───────────────────────────────
    product["images"] = _extract_gallery_images(soup, og_image)

    # ── Variants: Colors ────────────────────────────────────────────────
    if sse and sse.get("Colors"):
        colors = sse["Colors"]
        if isinstance(colors, list):
            product["variants_colors"] = colors
        elif isinstance(colors, str):
            product["variants_colors"] = [c.strip() for c in colors.split(",") if c.strip()]

    # ── Variants: Sizes (dataLayer often empty, CSS fallback) ───────────
    sizes: list[str] = []
    if sse and sse.get("Sizes"):
        sz = sse["Sizes"]
        if isinstance(sz, list) and sz:
            sizes = sz
        elif isinstance(sz, str) and sz.strip():
            sizes = [s.strip() for s in sz.split(",") if s.strip()]

    # CSS fallback for sizes: look for size-related selects/options
    if not sizes:
        try:
            for sel in soup.select("select"):
                prev_label = sel.find_previous("label")
                prev_span = sel.find_previous("span")
                context_text = ""
                if prev_label:
                    context_text = prev_label.get_text(strip=True).lower()
                elif prev_span:
                    context_text = prev_span.get_text(strip=True).lower()
                if "size" in context_text:
                    options = sel.select("option[value]")
                    sizes = [
                        opt.get_text(strip=True)
                        for opt in options
                        if opt.get("value") and opt.get_text(strip=True)
                    ]
                    if sizes:
                        break
        except Exception:
            pass

    product["variants_sizes"] = sizes

    return product


def _error_product(url: str, src_url: str, error: str) -> dict:
    """Create an error product record with empty defaults."""
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
        "sku": "",
        "rating": "",
        "review_count": "",
        "category": "",
        "description": "",
        "images": [],
        "variants_colors": [],
        "variants_sizes": [],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════


def main() -> None:
    parser = argparse.ArgumentParser(description=f"{SITE_NAME} Navigation Scraper")
    parser.add_argument(
        "--query", type=str, help="Search query for Phase 1 navigation"
    )
    parser.add_argument(
        "--category-url", type=str, help="Category URL for Phase 1 discovery"
    )
    parser.add_argument(
        "--input", type=str, dest="input_file", help="Path to input URLs JSON file"
    )
    parser.add_argument("--urls", nargs="+", help="Product URLs as CLI arguments")
    parser.add_argument(
        "--sample", action="store_true", help="Scrape first 5 items only"
    )
    parser.add_argument("--limit", type=int, default=None, help="Max items to scrape")
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        default=True,
        help="Disable proxy (default for this site)",
    )
    args = parser.parse_args()

    limit = 5 if args.sample else args.limit
    start_time = time.time()
    discovered_urls: list[str] = []
    src_url_base: str = ""

    logger.info("=" * 80)
    logger.info("Starting scraper for %s", SITE_NAME)
    logger.info("Strategy: %s", SCRAPING_METHOD)
    logger.info("Delay: %ss", DELAY)
    logger.info("=" * 80)

    # ── Determine URL source ────────────────────────────────────────────
    if args.urls:
        # Direct URLs from CLI
        discovered_urls = list(args.urls)
        src_url_base = SITE_URL
        logger.info("Using %d URLs from CLI arguments", len(discovered_urls))

    elif args.input_file:
        # URLs from input file
        try:
            with open(args.input_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            discovered_urls = data.get("urls", [])
            src_url_base = SITE_URL
            logger.info("Loaded %d URLs from %s", len(discovered_urls), args.input_file)
        except Exception as exc:
            logger.error("Failed to read input file: %s", exc)
            sys.exit(1)

    elif args.category_url:
        # Phase 1: Category discovery
        discovered_urls = discover_urls_via_category(
            args.category_url, MAX_PAGES, limit
        )
        src_url_base = args.category_url

    else:
        # Phase 1: Search discovery (default)
        query = args.query or DEFAULT_QUERY
        discovered_urls = discover_urls_via_search(query, MAX_PAGES, limit)
        src_url_base = SEARCH_WORKING_URL

    if not discovered_urls:
        logger.warning("No product URLs to scrape")
        sys.exit(0)

    if limit:
        discovered_urls = discovered_urls[:limit]

    total = len(discovered_urls)
    logger.info("Total products to scrape: %d", total)
    logger.info("=" * 80)

    # ── Phase 2: Extract data from each URL ────────────────────────────
    results: list[dict] = []
    success_count = 0
    failed_count = 0

    for i, url in enumerate(discovered_urls, 1):
        # Progress logging every 25 items and at boundaries
        if i == 1 or i == total or i % 25 == 0:
            percent = (i / total) * 100
            logger.info("Progress: [%d/%d] (%.1f%%)", i, total, percent)

        logger.info("Scraping: %s", url[:100])

        try:
            product = extract_product_data(url, src_url_base)
            results.append(product)

            if product.get("title"):
                success_count += 1
            elif "Soft 404" in product.get("remarks", ""):
                logger.warning("Soft 404 skipped: %s", url[:80])
                failed_count += 1
            else:
                failed_count += 1
        except Exception as exc:
            logger.error("Failed to extract %s: %s", url[:80], exc)
            results.append(_error_product(url, src_url_base, str(exc)))
            failed_count += 1

    # Assign sequential IDs
    for idx, item in enumerate(results, 1):
        item["id"] = idx

    # ── Write output ────────────────────────────────────────────────────
    output = {
        "site": {
            "name": SITE_NAME,
            "url": SITE_URL,
            "platform": PLATFORM,
            "scraping_method": f"{SCRAPING_METHOD}_navigation",
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        },
        OUTPUT_KEY: results,
        "metadata": {
            "scraping_duration_seconds": round(time.time() - start_time, 2),
            "failed_products": failed_count,
            "rate_limit_delay": DELAY,
            "discovered_urls": total,
            "extracted_items": len(results),
        },
    }

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    output_filename = os.path.join(SCRIPT_DIR, f"output_{timestamp}.json")

    with open(output_filename, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False, default=str)

    logger.info("=" * 80)
    logger.info("EXTRACTION COMPLETE")
    logger.info(
        "Total: %d, Success: %d, Failed: %d", total, success_count, failed_count
    )
    logger.info("Duration: %.1fs", time.time() - start_time)
    logger.info("Output: %s", output_filename)
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
