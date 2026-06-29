#!/usr/bin/env python3
"""
American Eagle Australia - Navigation Scraper (Two-Phase)

Phase 1: Discover product URLs via Shopify Collection JSON API
Phase 2: Extract product data via Shopify /products/{handle}.json API

The search page (americaneagle.com.au/search?searchTerm=...) is client-side rendered,
so HTML scraping of search results does NOT work. All discovery uses the Shopify
Collection JSON API which returns structured product data via direct HTTP.

Usage:
    python3 scraper.py                             # discover all products via /collections/all
    python3 scraper.py --query "jeans"             # search via collection API
    python3 scraper.py --category-url "https://americaneagle.com.au/collections/american-eagle-men"
    python3 scraper.py --input urls.json           # scrape from URL list
    python3 scraper.py --urls URL1 URL2            # scrape specific product URLs
    python3 scraper.py --sample                    # scrape first 5 products only
    python3 scraper.py --limit 50                  # max 50 products
    python3 scraper.py --no-proxy                  # explicitly disable proxy (default)
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Optional
from urllib.parse import urlparse

import requests

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

SITE_NAME = "American Eagle Australia"
SITE_URL = "https://americaneagle.com.au"
PLATFORM = "shopify"
SITE_SLUG = "americaneagle-com-au"
OUTPUT_KEY = "products"
CURRENCY_SYMBOL = "$"
CURRENCY_CODE = "AUD"

# Phase 1: Discovery via Shopify Collection JSON API
DEFAULT_QUERY = "pants"
ALL_COLLECTIONS_API = "https://americaneagle.com.au/collections/all/products.json?limit=250&page={page}"

# Category URLs and handles discovered during site analysis
CATEGORY_URLS: list[str] = [
    "https://americaneagle.com.au/collections/american-eagle-sale",
    "https://americaneagle.com.au/collections/american-eagle-new-arrivals",
    "https://americaneagle.com.au/collections/american-eagle-men",
    "https://americaneagle.com.au/collections/american-eagle-men-jeans",
    "https://americaneagle.com.au/collections/american-eagle-men-tops",
    "https://americaneagle.com.au/collections/american-eagle-men-bottoms",
    "https://americaneagle.com.au/collections/american-eagle-men-bottoms-pants",
    "https://americaneagle.com.au/collections/american-eagle-men-bottoms-shorts",
    "https://americaneagle.com.au/collections/american-eagle-men-underwear",
    "https://americaneagle.com.au/collections/american-eagle-men-activewear",
    "https://americaneagle.com.au/collections/aerie-new-arrivals",
    "https://americaneagle.com.au/collections/off-campus",
]

COLLECTION_HANDLES: list[str] = [
    "american-eagle-sale",
    "american-eagle-new-arrivals",
    "american-eagle-men",
    "american-eagle-men-jeans",
    "american-eagle-men-tops",
    "american-eagle-men-bottoms",
    "american-eagle-men-bottoms-pants",
    "american-eagle-men-bottoms-shorts",
    "american-eagle-men-underwear",
    "american-eagle-men-activewear",
    "aerie-new-arrivals",
    "off-campus",
]

# Query-to-collection keyword mapping for --query mode
QUERY_COLLECTION_MAP: dict[str, list[str]] = {
    "pants": ["american-eagle-men-bottoms-pants", "american-eagle-men-bottoms"],
    "jeans": ["american-eagle-men-jeans", "american-eagle-men"],
    "shorts": ["american-eagle-men-bottoms-shorts", "american-eagle-men-bottoms"],
    "tops": ["american-eagle-men-tops", "american-eagle-men"],
    "sale": ["american-eagle-sale"],
    "new": ["american-eagle-new-arrivals"],
    "underwear": ["american-eagle-men-underwear"],
    "activewear": ["american-eagle-men-activewear"],
    "aerie": ["aerie-new-arrivals"],
}

# Phase 1: Pagination
MAX_PAGES = 50
PAGE_SIZE = 250

# Phase 2: Shopify API extraction
SHOPIFY_PRODUCT_API = "https://americaneagle.com.au/products/{handle}.json"

# Rate limiting
DELAY_SECONDS = 1.0
REQUEST_TIMEOUT = 15
MAX_RETRIES = 3

# HTTP headers
HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════════════

LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
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


class _HTMLTextExtractor(HTMLParser):
    """Strip HTML tags and return plain text."""

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def get_text(self) -> str:
        return " ".join(self._parts).strip()


def strip_html(html: str) -> str:
    """Remove HTML tags and return plain text."""
    if not html:
        return ""
    extractor = _HTMLTextExtractor()
    extractor.feed(html)
    text = extractor.get_text()
    return re.sub(r"\s+", " ", text).strip()


def format_price(raw_price: Any, currency_symbol: str = CURRENCY_SYMBOL) -> str:
    """Format a price string with currency symbol.

    Args:
        raw_price: Numeric string like '58.95' or already formatted '$58.95'.
        currency_symbol: Currency symbol to prepend.

    Returns:
        Formatted price like '$58.95'.
    """
    if not raw_price:
        return ""
    raw_price = str(raw_price).strip()
    if raw_price.startswith(("$", "€", "£")):
        return raw_price
    try:
        price_float = float(raw_price)
        return f"{currency_symbol}{price_float:,.2f}"
    except (ValueError, TypeError):
        return f"{currency_symbol}{raw_price}"


def normalize_availability(available: bool) -> str:
    """Map Shopify availability boolean to standard text."""
    return "In Stock" if available else "Out of Stock"


def extract_style_id(body_html: str) -> str:
    """Extract style ID from product body_html .api-id section."""
    if not body_html:
        return ""
    match = re.search(r"Style ID:\s*([\w\-]+)", body_html)
    if match:
        return match.group(1)
    return ""


def extract_category(tags: list[str]) -> str:
    """Extract most specific category from Shopify tags with 'cat:' prefix."""
    cat_tags = [t for t in tags if t.startswith("cat:")]
    if not cat_tags:
        return ""
    return max(cat_tags, key=len)


def extract_handle_from_url(url: str) -> str:
    """Extract product handle from a Shopify product URL."""
    parsed = urlparse(url)
    path = parsed.path
    match = re.match(r".*/products/([A-Za-z0-9][\w\-]*)", path)
    if match:
        return match.group(1)
    return ""


def is_soft_404(product_data: Optional[dict]) -> bool:
    """Detect soft 404 from Shopify API response."""
    if not product_data:
        return True
    product = product_data.get("product", {})
    title = product.get("title", "")
    if not title:
        return True
    title_lower = title.lower()
    if any(
        kw in title_lower
        for kw in [
            "not found",
            "unavailable",
            "discontinued",
            "no longer available",
            "page not found",
            "404",
        ]
    ):
        return True
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP REQUEST UTILITY
# ═══════════════════════════════════════════════════════════════════════════════

_http_session: Optional[requests.Session] = None


def get_http_session() -> requests.Session:
    """Get or create a shared HTTP session."""
    global _http_session
    if _http_session is None:
        _http_session = requests.Session()
        _http_session.headers.update(HEADERS)
    return _http_session


def safe_get(url: str, timeout: int = REQUEST_TIMEOUT) -> Optional[requests.Response]:
    """Make an HTTP GET request with retry logic."""
    session = get_http_session()
    last_error: Optional[Exception] = None

    for attempt in range(MAX_RETRIES):
        try:
            resp = session.get(url, timeout=timeout, allow_redirects=True)
            return resp
        except requests.RequestException as e:
            last_error = e
            wait = DELAY_SECONDS * (attempt + 1)
            logger.warning(
                "Request failed (attempt %d/%d) for %s: %s",
                attempt + 1,
                MAX_RETRIES,
                url[:80],
                e,
            )
            if attempt < MAX_RETRIES - 1:
                time.sleep(wait)

    logger.error("All %d attempts failed for %s: %s", MAX_RETRIES, url[:80], last_error)
    return None


def safe_get_json(url: str, timeout: int = REQUEST_TIMEOUT) -> Optional[dict]:
    """Make an HTTP GET request and parse JSON response."""
    resp = safe_get(url, timeout)
    if resp is None:
        return None
    try:
        resp.raise_for_status()
        return resp.json()
    except (ValueError, requests.HTTPError) as e:
        logger.error("Failed to parse JSON from %s: %s", url[:80], e)
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 1: URL DISCOVERY (Shopify Collection JSON API)
# ═══════════════════════════════════════════════════════════════════════════════


def discover_urls_from_all_collections(
    max_pages: Optional[int] = None,
    limit: Optional[int] = None,
) -> tuple[list[str], str]:
    """Discover ALL product URLs via Shopify /collections/all/products.json API.

    This is the PRIMARY discovery method. The search page is client-side rendered
    and does NOT return product links in static HTML.

    Args:
        max_pages: Maximum pagination pages to follow.
        limit: Maximum number of product URLs to return.

    Returns:
        Tuple of (list of product URLs, source URL used).
    """
    logger.info(
        "Phase 1: Discovering products via /collections/all/products.json"
    )

    urls: list[str] = []
    seen_handles: set[str] = set()
    page_num = 1
    max_pg = max_pages or MAX_PAGES

    while page_num <= max_pg:
        if limit and len(urls) >= limit:
            logger.info("Phase 1: Reached limit=%d at page %d", limit, page_num)
            break

        api_url = ALL_COLLECTIONS_API.replace("{page}", str(page_num))
        logger.info(
            "Phase 1: Fetching /collections/all page %d ...", page_num
        )

        data = safe_get_json(api_url)
        if not data or not data.get("products"):
            logger.info(
                "Phase 1: No products returned on page %d, stopping", page_num
            )
            break

        products = data["products"]
        new_count = 0
        for product in products:
            handle = product.get("handle", "")
            if handle and handle not in seen_handles:
                seen_handles.add(handle)
                product_url = f"{SITE_URL}/products/{handle}"
                urls.append(product_url)
                new_count += 1

        logger.info(
            "Phase 1: Page %d → %d products (%d new, %d total)",
            page_num,
            len(products),
            new_count,
            len(urls),
        )

        if len(products) < PAGE_SIZE:
            logger.info(
                "Phase 1: Received %d products (< %d page size), "
                "last page reached",
                len(products),
                PAGE_SIZE,
            )
            break

        page_num += 1
        time.sleep(DELAY_SECONDS)

    if limit:
        urls = urls[:limit]

    logger.info("Phase 1: Discovered %d total product URLs", len(urls))
    return urls, f"{SITE_URL}/collections/all"


def discover_urls_from_collection(
    collection_handle: str,
    max_pages: Optional[int] = None,
    limit: Optional[int] = None,
) -> tuple[list[str], str]:
    """Discover product URLs from a specific collection via JSON API.

    Args:
        collection_handle: Shopify collection handle (e.g. 'american-eagle-men').
        max_pages: Maximum pagination pages.
        limit: Maximum product URLs.

    Returns:
        Tuple of (product URLs list, source URL).
    """
    logger.info(
        "Phase 1: Discovering products from collection '%s'", collection_handle
    )

    urls: list[str] = []
    seen_handles: set[str] = set()
    page_num = 1
    max_pg = max_pages or MAX_PAGES

    while page_num <= max_pg:
        if limit and len(urls) >= limit:
            break

        api_url = (
            f"{SITE_URL}/collections/{collection_handle}"
            f"/products.json?limit=250&page={page_num}"
        )
        logger.info(
            "Phase 1: Fetching collection '%s' page %d ...",
            collection_handle,
            page_num,
        )

        data = safe_get_json(api_url)
        if not data or not data.get("products"):
            break

        products = data["products"]
        new_count = 0
        for product in products:
            handle = product.get("handle", "")
            if handle and handle not in seen_handles:
                seen_handles.add(handle)
                product_url = f"{SITE_URL}/products/{handle}"
                urls.append(product_url)
                new_count += 1

        logger.info(
            "Phase 1: Collection '%s' page %d → %d products (%d new)",
            collection_handle,
            page_num,
            len(products),
            new_count,
        )

        if len(products) < PAGE_SIZE:
            break

        page_num += 1
        time.sleep(DELAY_SECONDS)

    if limit:
        urls = urls[:limit]

    logger.info(
        "Phase 1: Discovered %d products from collection '%s'",
        len(urls),
        collection_handle,
    )
    return urls, f"{SITE_URL}/collections/{collection_handle}"


def discover_urls_from_query(
    query: str,
    max_pages: Optional[int] = None,
    limit: Optional[int] = None,
) -> tuple[list[str], str]:
    """Discover product URLs based on a search query.

    Maps the query to relevant collection handles using QUERY_COLLECTION_MAP.
    Falls back to /collections/all if no mapping found.

    Args:
        query: Search term.
        max_pages: Maximum pagination pages.
        limit: Maximum product URLs.

    Returns:
        Tuple of (product URLs list, source description).
    """
    query_lower = query.lower().strip()

    # Try to find matching collections from the keyword map
    matched_handles: list[str] = []
    for keyword, handles in QUERY_COLLECTION_MAP.items():
        if keyword in query_lower:
            matched_handles.extend(handles)

    # Deduplicate while preserving order
    matched_handles = list(dict.fromkeys(matched_handles))

    if matched_handles:
        logger.info(
            "Phase 1: Query '%s' matched collections: %s",
            query,
            matched_handles,
        )

        all_urls: list[str] = []
        all_src_parts: list[str] = []

        for handle in matched_handles:
            if limit and len(all_urls) >= limit:
                break
            remaining = (limit - len(all_urls)) if limit else None
            urls, src = discover_urls_from_collection(
                handle, max_pages, remaining
            )
            for u in urls:
                if u not in all_urls:
                    all_urls.append(u)
            all_src_parts.append(handle)

        src_url = (
            f"collections:{','.join(all_src_parts)}"
            if all_src_parts
            else f"{SITE_URL}/collections/all"
        )
        return all_urls, src_url

    # Fallback: use /collections/all
    logger.info(
        "Phase 1: No collection match for query '%s', "
        "falling back to /collections/all",
        query,
    )
    return discover_urls_from_all_collections(max_pages, limit)


def discover_urls_from_category_url(
    category_url: str,
    max_pages: Optional[int] = None,
    limit: Optional[int] = None,
) -> tuple[list[str], str]:
    """Discover product URLs from a category/collection URL.

    Args:
        category_url: Full category/collection URL.
        max_pages: Maximum pagination pages.
        limit: Maximum product URLs.

    Returns:
        Tuple of (product URLs list, source URL).
    """
    parsed = urlparse(category_url)
    path_parts = parsed.path.strip("/").split("/")

    if "collections" in path_parts:
        idx = path_parts.index("collections")
        if idx + 1 < len(path_parts):
            collection_handle = path_parts[idx + 1]
            return discover_urls_from_collection(
                collection_handle, max_pages, limit
            )

    logger.warning("Could not extract collection handle from %s", category_url)
    return [], category_url


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 2: PRODUCT DATA EXTRACTION (Shopify API)
# ═══════════════════════════════════════════════════════════════════════════════


def extract_product_via_api(
    url: str, src_url: str
) -> dict[str, Any]:
    """Extract product data using Shopify /products/{handle}.json API.

    Args:
        url: Full product page URL.
        src_url: Source listing/search URL where the product was discovered.

    Returns:
        Dictionary with extracted product fields.
    """
    handle = extract_handle_from_url(url)
    if not handle:
        return _make_error_product(
            url, src_url, "Could not extract product handle from URL"
        )

    api_url = SHOPIFY_PRODUCT_API.replace("{handle}", handle)
    data = safe_get_json(api_url)

    status_code = 200
    if data is None:
        return _make_error_product(
            url, src_url, f"Failed to fetch product JSON from {api_url}"
        )

    # Check for soft 404
    if is_soft_404(data):
        return _make_error_product(
            url, src_url, "Soft 404: product not found", status_code
        )

    product = data.get("product", {})
    title = product.get("title", "").strip()

    if not title:
        return _make_error_product(
            url, src_url, "Soft 404: product has no title", status_code
        )

    # Match variant (via ?variant= param if present, otherwise first variant)
    variant_id = _extract_variant_id_from_url(url)
    variants = product.get("variants", [])

    matched_variant = None
    if variant_id:
        for v in variants:
            if str(v.get("id")) == str(variant_id):
                matched_variant = v
                break
    if matched_variant is None and variants:
        matched_variant = variants[0]

    # Build product record
    now = datetime.now(timezone.utc).isoformat()
    record: dict[str, Any] = {
        "id": 0,
        "url": url,
        "src_url": src_url,
        "status_code": status_code,
        "scraped_at": now,
        "remarks": "",
    }

    # Core fields
    record["title"] = title
    record["sku"] = ""
    record["brand"] = product.get("vendor", "")
    record["description"] = _extract_description(product.get("body_html", ""))
    record["variant_name"] = ""
    record["location"] = ""

    # Variant-specific fields
    if matched_variant:
        raw_price = matched_variant.get("price", "")
        record["price"] = format_price(raw_price)

        raw_compare = matched_variant.get("compare_at_price")
        record["original_price"] = (
            format_price(raw_compare) if raw_compare else ""
        )

        available = matched_variant.get("available", True)
        record["availability"] = normalize_availability(available)

        record["currency"] = matched_variant.get("price_currency", CURRENCY_CODE)
        record["sku"] = matched_variant.get("sku", "") or ""
        record["variant_name"] = matched_variant.get("title", "")
    else:
        record["price"] = ""
        record["original_price"] = ""
        record["availability"] = "Out of Stock"
        record["currency"] = CURRENCY_CODE

    # Extra fields
    record["style_id"] = extract_style_id(product.get("body_html", ""))

    tags = product.get("tags", [])
    record["category"] = extract_category(tags if isinstance(tags, list) else [])

    # Images - product gallery only (scoped to product.images)
    images = product.get("images", [])
    image_urls: list[str] = []
    for img in images:
        src = img.get("src", "")
        if src and src not in image_urls:
            # Skip non-product images (logo, icons, navigation)
            skip_patterns = [
                "/brand.assets/",
                "/emoji/",
                "/flags/",
                "/icon/",
                "/navigation/",
            ]
            if any(p in src for p in skip_patterns):
                continue
            image_urls.append(src)
    record["images"] = ",".join(image_urls[:20]) if image_urls else ""

    return record


def _extract_variant_id_from_url(url: str) -> Optional[str]:
    """Extract variant ID from URL query parameter."""
    parsed = urlparse(url)
    params = parsed.query
    match = re.search(r"variant=(\d+)", params)
    if match:
        return match.group(1)
    return None


def _extract_description(body_html: str) -> str:
    """Extract clean description from Shopify body_html."""
    if not body_html:
        return ""

    cleaned = re.sub(
        r"<(script|style)[^>]*>.*?</\1>",
        "",
        body_html,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # Try to get main description from api-description section
    api_desc_match = re.search(
        r'class="api-description"[^>]*>(.*?)</div>',
        cleaned,
        re.DOTALL,
    )
    if api_desc_match:
        text = strip_html(api_desc_match.group(1))
        if text:
            return text

    # Fallback: strip HTML and take first 500 chars
    text = strip_html(cleaned)
    if len(text) > 500:
        text = text[:500].rstrip() + "..."
    return text


def _make_error_product(
    url: str,
    src_url: str,
    error: str,
    status_code: int = 0,
) -> dict[str, Any]:
    """Create an error product record."""
    return {
        "id": 0,
        "url": url,
        "src_url": src_url,
        "status_code": status_code,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": f"Error: {error[:200]}",
        "title": "",
        "price": "",
        "availability": "",
        "original_price": "",
        "currency": "",
        "sku": "",
        "brand": "",
        "description": "",
        "variant_name": "",
        "style_id": "",
        "category": "",
        "images": "",
        "location": "",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description=f"{SITE_NAME} Navigation Scraper"
    )
    parser.add_argument(
        "--query",
        type=str,
        default=None,
        help="Search query (default: 'pants')",
    )
    parser.add_argument(
        "--category-url",
        type=str,
        default=None,
        help="Category/collection URL to crawl",
    )
    parser.add_argument(
        "--input",
        type=str,
        default=None,
        help="Path to input URLs JSON file",
    )
    parser.add_argument(
        "--urls",
        nargs="+",
        default=None,
        help="Product URLs as CLI arguments",
    )
    parser.add_argument(
        "--sample",
        action="store_true",
        help="Scrape only 5 products",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max products to scrape",
    )
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        help="Disable proxy (default for this site)",
    )
    args = parser.parse_args()

    limit = 5 if args.sample else args.limit

    logger.info("=" * 80)
    logger.info("Starting scraper for %s", SITE_NAME)
    logger.info("Platform: %s | Method: http_requests (Shopify API)", PLATFORM)
    logger.info("Limit: %s | Sample: %s", limit or "unlimited", args.sample)
    logger.info("=" * 80)

    start_time = time.time()
    discovered_urls: list[str] = []
    src_url = ""

    # ── URL Resolution ──────────────────────────────────────────────────

    if args.urls:
        # Direct product URLs from CLI
        discovered_urls = args.urls
        src_url = "cli_input"
        logger.info(
            "Using %d product URLs from CLI arguments", len(discovered_urls)
        )

    elif args.input:
        # Product URLs from input file
        try:
            with open(args.input, "r", encoding="utf-8") as f:
                data = json.load(f)
            discovered_urls = data.get("urls", [])
            src_url = args.input
            logger.info(
                "Loaded %d product URLs from %s",
                len(discovered_urls),
                args.input,
            )
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.error("Failed to read input file '%s': %s", args.input, e)
            sys.exit(1)

    elif args.category_url:
        # Phase 1: Discover from specific category/collection URL
        discovered_urls, src_url = discover_urls_from_category_url(
            args.category_url, MAX_PAGES, limit
        )

    else:
        # Default: Phase 1 discovery
        # Use query if provided, otherwise use DEFAULT_QUERY
        query = args.query if args.query else DEFAULT_QUERY
        logger.info(
            "Phase 1: Default discovery mode with query '%s'", query
        )

        # Map query to relevant collection(s) via Collection JSON API
        # Falls back to /collections/all if no mapping found
        discovered_urls, src_url = discover_urls_from_query(
            query, MAX_PAGES, limit
        )

    if not discovered_urls:
        logger.warning("No product URLs discovered. Exiting.")
        sys.exit(0)

    # ── Phase 2: Extract Data ───────────────────────────────────────────

    total = len(discovered_urls)
    logger.info("=" * 80)
    logger.info("Phase 2: Extracting data from %d products", total)
    logger.info("=" * 80)

    results: list[dict[str, Any]] = []
    success_count = 0
    fail_count = 0

    for i, url in enumerate(discovered_urls, 1):
        product_url = url.strip()
        if not product_url:
            continue

        # Progress logging
        if i % 25 == 0 or i == 1 or i == total:
            percent = (i / total) * 100
            logger.info(
                "Progress: [%d/%d] (%.1f%%)", i, total, percent
            )
        logger.info("Scraping: %s", product_url[:100])

        try:
            product = extract_product_via_api(product_url, src_url)
            results.append(product)

            if product.get("title"):
                success_count += 1
            else:
                fail_count += 1
                logger.warning(
                    "No title extracted for %s", product_url[:80]
                )
        except Exception as exc:
            fail_count += 1
            logger.error("Failed to extract %s: %s", product_url[:80], exc)
            results.append(_make_error_product(product_url, src_url, str(exc)))

        # Rate limiting between requests
        if i < total:
            time.sleep(DELAY_SECONDS)

    # Assign sequential IDs
    for idx, product in enumerate(results, 1):
        product["id"] = idx

    # ── Write Output ────────────────────────────────────────────────────

    output = {
        "site": {
            "name": SITE_NAME,
            "url": SITE_URL,
            "platform": PLATFORM,
            "scraping_method": "http_requests_shopify_api",
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        },
        OUTPUT_KEY: results,
        "metadata": {
            "scraping_duration_seconds": round(time.time() - start_time, 2),
            "discovered_urls": len(discovered_urls),
            "extracted_items": len(results),
            "failed_products": fail_count,
            "rate_limit_delay": DELAY_SECONDS,
        },
    }

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    output_filename = os.path.join(SCRIPT_DIR, f"output_{timestamp}.json")

    os.makedirs(SCRIPT_DIR, exist_ok=True)
    with open(output_filename, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False, default=str)

    # ── Summary ─────────────────────────────────────────────────────────
    duration = round(time.time() - start_time, 2)
    logger.info("=" * 80)
    logger.info("EXTRACTION COMPLETE")
    logger.info(
        "Total: %d | Success: %d | Failed: %d",
        len(results),
        success_count,
        fail_count,
    )
    logger.info("Duration: %.1f seconds", duration)
    logger.info("Output: %s", output_filename)
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
