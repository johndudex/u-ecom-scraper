#!/usr/bin/env python3
"""
Armani.com Product Scraper

Uses requests + BeautifulSoup to extract product data from armani.com
product pages via JSON-LD structured data (@graph format).

JSON-LD fields extracted:
  - title, description, brand from @graph[Product]
  - price, currency, availability, original_price from @graph[Product].offers
  - images from @graph[Product].image
  - category from @graph[BreadcrumbList].itemListElement (positions >= 3, excluding last)
  - color from CSS selector span.text-light-6
  - SKU from URL regex

Usage:
    python3 scraper_draft.py                          # reads input_urls.json
    python3 scraper_draft.py --input custom_urls.json # explicit input file
    python3 scraper_draft.py --urls <url1> <url2>     # URLs as CLI args
    python3 scraper_draft.py --sample                 # scrape first 5 only
    python3 scraper_draft.py --limit 10               # max N products
    python3 scraper_draft.py --no-proxy               # force no proxy (default)
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

import requests
from bs4 import BeautifulSoup

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

SITE_NAME = "Armani"
SITE_URL = "https://www.armani.com"
PLATFORM = "custom"
SCRAPING_METHOD = "http_requests"
SITE_SLUG = "armani-com"

DELAY_BETWEEN_REQUESTS = 2.0
MAX_RETRIES = 3
REQUEST_TIMEOUT = 15

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INPUT_FILE = os.path.join(SCRIPT_DIR, "input_urls.json")
LOG_FILE = os.path.join(os.path.dirname(SCRIPT_DIR), "logs", f"{SITE_SLUG}.log")

# Regex to extract product code from URL (e.g., GW003665-AF24627-U4153)
# NOTE: No leading '/' — 'cod-' appears mid-segment in the URL path
PRODUCT_CODE_RE = re.compile(r"cod-([A-Z0-9_-]+)", re.IGNORECASE)

# Schema.org availability mapping
AVAILABILITY_MAP = {
    "instock": "In Stock",
    "outofstock": "Out of Stock",
    "onorder": "On Order",
    "preorder": "Pre-Order",
    "discontinued": "Discontinued",
}

# Currency symbol map
CURRENCY_SYMBOLS = {
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "JPY": "¥",
    "CAD": "C$",
    "AUD": "A$",
    "CHF": "CHF",
    "CNY": "¥",
}

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

def extract_product_code_from_url(url: str) -> str:
    """Extract the product code (SKU) from the URL using regex."""
    match = PRODUCT_CODE_RE.search(url)
    return match.group(1) if match else ""


def format_price(value: float | int | str, currency: str = "USD") -> str:
    """Format a numeric price with the appropriate currency symbol."""
    if not value:
        return ""
    try:
        num = float(value)
    except (ValueError, TypeError):
        return str(value)
    symbol = CURRENCY_SYMBOLS.get(currency, f"{currency} ")
    return f"{symbol}{num:,.2f}"


def map_availability(schema_url: str) -> str:
    """Map a schema.org availability URL to a human-readable string."""
    if not schema_url:
        return ""
    lower = schema_url.lower()
    for key, label in AVAILABILITY_MAP.items():
        if key in lower:
            return label
    # Fallback: try to extract the last segment of the URL
    segments = lower.split("/")
    last = segments[-1] if segments else ""
    for key, label in AVAILABILITY_MAP.items():
        if key in last:
            return label
    return schema_url.split("/")[-1].replace("schema.org", "").strip()


def load_urls_from_file(filepath: str) -> list[str]:
    """Load product URLs from a JSON file."""
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("urls", [])


def save_urls_to_file(filepath: str, urls: list[str]) -> None:
    """Save product URLs to a JSON file."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump({"urls": urls}, f, indent=2, ensure_ascii=False)
    logger.info(f"Saved {len(urls)} URLs to {filepath}")


# ═══════════════════════════════════════════════════════════════════════════════
# NETWORK
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_page(url: str) -> Optional[tuple[BeautifulSoup, int]]:
    """Fetch a page via HTTP GET and return (soup, status_code) or None on failure."""
    for attempt in range(MAX_RETRIES):
        try:
            time.sleep(DELAY_BETWEEN_REQUESTS)
            response = requests.get(
                url,
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                return soup, response.status_code

            logger.warning(
                f"HTTP {response.status_code} for {url} (attempt {attempt + 1}/{MAX_RETRIES})"
            )
            if response.status_code in (403, 503, 429):
                # Rate limited or blocked — wait longer before retry
                time.sleep(DELAY_BETWEEN_REQUESTS * 3)
                continue

            # Other non-200 status codes — return the soup anyway for partial extraction
            if response.text:
                soup = BeautifulSoup(response.text, "html.parser")
                return soup, response.status_code
            return None, response.status_code

        except requests.RequestException as e:
            logger.error(
                f"Failed to fetch {url} (attempt {attempt + 1}/{MAX_RETRIES}): {e}"
            )
            if attempt < MAX_RETRIES - 1:
                time.sleep(DELAY_BETWEEN_REQUESTS * 2)

    return None


# ═══════════════════════════════════════════════════════════════════════════════
# JSON-LD EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════════

def extract_jsonld_graph(soup: BeautifulSoup) -> dict[str, dict]:
    """
    Extract all @graph entries from JSON-LD scripts, keyed by @type.

    Armani.com uses @graph structure where Product and BreadcrumbList are
    separate entries inside a @graph array, not root-level keys.

    Returns a dict like: {"Product": {...}, "BreadcrumbList": {...}}
    """
    graph: dict[str, dict] = {}

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
        except (json.JSONDecodeError, TypeError):
            continue

        # Handle @graph format
        if isinstance(data, dict):
            entries = data.get("@graph", [])
            if not entries and data.get("@type"):
                entries = [data]
            if isinstance(entries, list):
                for item in entries:
                    if isinstance(item, dict) and item.get("@type"):
                        entry_type = item["@type"]
                        # Keep the first entry of each type (some pages may have duplicates)
                        if entry_type not in graph:
                            graph[entry_type] = item

    return graph


def extract_title(product_ld: dict) -> str:
    """Extract product title from JSON-LD Product entry."""
    name = product_ld.get("name", "")
    return str(name).strip() if name else ""


def extract_description(product_ld: dict) -> str:
    """Extract product description from JSON-LD Product entry."""
    desc = product_ld.get("description", "")
    return str(desc).strip() if desc else ""


def extract_price_and_currency(product_ld: dict) -> tuple[str, str, str]:
    """
    Extract price, currency, and original_price from JSON-LD offers.

    Returns (formatted_price, formatted_original_price, currency_code).
    """
    offers = product_ld.get("offers", {})
    if isinstance(offers, list):
        offers = offers[0] if offers else {}

    price_value = offers.get("price", "")
    high_price_value = offers.get("highPrice", "")
    currency = offers.get("priceCurrency", "USD")

    # Format current price
    price_str = format_price(price_value, currency) if price_value else ""

    # Format original price only if highPrice exists and is greater than price
    original_price_str = ""
    if high_price_value:
        try:
            if float(high_price_value) > float(price_value or 0):
                original_price_str = format_price(high_price_value, currency)
        except (ValueError, TypeError):
            original_price_str = ""

    return price_str, original_price_str, str(currency)


def extract_availability(product_ld: dict) -> str:
    """Extract and normalize availability from JSON-LD offers."""
    offers = product_ld.get("offers", {})
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    avail_url = offers.get("availability", "")
    return map_availability(avail_url)


def extract_brand(product_ld: dict) -> str:
    """Extract brand name from JSON-LD Product entry."""
    brand = product_ld.get("brand", {})
    if isinstance(brand, dict):
        return str(brand.get("name", "")).strip()
    return str(brand).strip() if brand else ""


def extract_images(product_ld: dict, url: str) -> list[str]:
    """
    Extract product images from JSON-LD Product entry.

    Filters to only include images from 'assets-cf.armani.com' domain
    and ensures they contain the product SKU/code in the URL path.
    """
    images_raw = product_ld.get("image", [])
    if isinstance(images_raw, str):
        images_raw = [images_raw]

    product_code = extract_product_code_from_url(url)
    # SKU in image URLs uses underscores instead of hyphens
    sku_normalized = product_code.replace("-", "_") if product_code else ""

    filtered = []
    for img in images_raw:
        img_str = str(img).strip()
        if not img_str:
            continue
        # Filter by armani assets domain
        if "assets-cf.armani.com" not in img_str:
            continue
        # Skip non-product images (banners, icons, logos, etc.)
        skip_patterns = ["/brand.assets/", "/emoji/", "/flags/", "/icon/", "/navigation/"]
        if any(p in img_str for p in skip_patterns):
            continue
        # Prefer images containing the product SKU
        if sku_normalized and sku_normalized in img_str:
            filtered.append(img_str)
        elif not sku_normalized:
            # No SKU available — include all armani assets images
            filtered.append(img_str)

    return filtered


def extract_category(breadcrumbs_ld: dict) -> str:
    """
    Extract category breadcrumb from JSON-LD BreadcrumbList entry.

    BreadcrumbList positions:
      1: Home
      2: GA US (locale)
      3: Woman (or Man, etc.)
      4: Clothing
      5: Shirts and Tops
      6: [Product Name] ← EXCLUDE (not a category)

    We use positions >= 3, excluding the LAST item (which is the product name).
    """
    if not breadcrumbs_ld:
        return ""

    items = breadcrumbs_ld.get("itemListElement", [])
    if not items:
        return ""

    # EXCLUDE the last breadcrumb item — it's the product name, not a category
    items = items[:-1]

    categories = []
    for item in items:
        if not isinstance(item, dict):
            continue
        position = item.get("position", 0)
        name = item.get("name", "")
        if position >= 3 and name:
            categories.append(str(name).strip())

    return " > ".join(categories)


def extract_color(soup: BeautifulSoup) -> str:
    """Extract color from CSS selector span.text-light-6 (first match)."""
    color_el = soup.select_one("span.text-light-6")
    if color_el:
        text = color_el.get_text(strip=True)
        return text if text else ""
    return ""


# ═══════════════════════════════════════════════════════════════════════════════
# PRODUCT EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════════

def extract_product_from_page(
    soup: BeautifulSoup, url: str, status_code: int, src_url: str
) -> dict:
    """
    Extract all product fields from a parsed page.

    Uses JSON-LD @graph as primary source for most fields,
    with CSS selectors for color (available in static HTML).
    """
    graph = extract_jsonld_graph(soup)

    product_ld = graph.get("Product", {})
    breadcrumbs_ld = graph.get("BreadcrumbList", {})

    # Extract price-related fields together
    price_str, original_price_str, currency = extract_price_and_currency(product_ld)

    product: dict = {
        "id": 0,
        "title": extract_title(product_ld),
        "price": price_str,
        "availability": extract_availability(product_ld),
        "original_price": original_price_str,
        "currency": currency,
        "url": url,
        "src_url": src_url,
        "location": "",
        "status_code": status_code,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": "",
        "description": extract_description(product_ld),
        "brand": extract_brand(product_ld),
        "sku": extract_product_code_from_url(url),
        "images": extract_images(product_ld, url),
        "category": extract_category(breadcrumbs_ld),
        "color": extract_color(soup),
    }

    # Add warnings to remarks if any key fields are missing
    missing_fields = []
    if not product["title"]:
        missing_fields.append("title")
    if not product["price"]:
        missing_fields.append("price")
    if not product["sku"]:
        missing_fields.append("sku")
    if missing_fields:
        product["remarks"] = f"Missing fields: {', '.join(missing_fields)}"

    return product


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description=f"HTTP scraper for {SITE_NAME}")
    parser.add_argument("--input", type=str, default=None, help="Path to input URLs JSON file")
    parser.add_argument("--urls", nargs="+", default=None, help="Product URLs as arguments")
    parser.add_argument("--sample", action="store_true", help="Scrape only 5 products")
    parser.add_argument("--limit", type=int, default=None, help="Max products to scrape")
    parser.add_argument("--no-proxy", action="store_true", help="Force no proxy (default for this site)")
    args = parser.parse_args()

    start_time = time.time()

    logger.info("=" * 80)
    logger.info(f"Starting scraper for {SITE_NAME}")
    logger.info(f"Site: {SITE_URL}")
    logger.info(f"Platform: {PLATFORM}")
    logger.info(f"Method: {SCRAPING_METHOD}")
    logger.info("=" * 80)

    # ── Load product URLs ────────────────────────────────────────────────────
    product_urls: list[str] = []

    if args.urls:
        product_urls = list(args.urls)
        logger.info(f"Loaded {len(product_urls)} URLs from CLI --urls")
    elif args.input:
        product_urls = load_urls_from_file(args.input)
        logger.info(f"Loaded {len(product_urls)} URLs from --input {args.input}")
    elif os.path.exists(INPUT_FILE):
        product_urls = load_urls_from_file(INPUT_FILE)
        logger.info(f"Loaded {len(product_urls)} URLs from {INPUT_FILE}")
    else:
        logger.error(f"No input URLs found. Place URLs in {INPUT_FILE} or use --input/--urls")
        sys.exit(1)

    # ── Apply limits ─────────────────────────────────────────────────────────
    if args.sample:
        product_urls = product_urls[:5]
        logger.info("Sample mode: limiting to 5 products")
    if args.limit:
        product_urls = product_urls[: args.limit]
        logger.info(f"Limit mode: max {args.limit} products")

    total = len(product_urls)
    if total == 0:
        logger.warning("No product URLs to process. Exiting.")
        sys.exit(0)

    logger.info(f"Total products to scrape: {total}")
    logger.info("=" * 80)

    # ── Scrape each product ──────────────────────────────────────────────────
    results: list[dict] = []
    failed = 0

    for i, url in enumerate(product_urls):
        logger.info(f"[{i + 1}/{total}] Scraping: {url}")

        result = fetch_page(url)
        if result is None:
            logger.error(f"  ✗ Failed to fetch page after {MAX_RETRIES} retries")
            failed += 1
            # Create a minimal failed-product entry
            results.append({
                "id": i + 1,
                "title": "",
                "price": "",
                "availability": "",
                "original_price": "",
                "currency": "",
                "url": url,
                "src_url": url,
                "location": "",
                "status_code": 0,
                "scraped_at": datetime.now(timezone.utc).isoformat(),
                "remarks": f"Failed to fetch page after {MAX_RETRIES} retries",
            })
            continue

        soup, status_code = result
        product = extract_product_from_page(soup, url, status_code, url)
        product["id"] = i + 1
        results.append(product)

        status = "✓" if product["title"] and product["price"] else "⚠"
        logger.info(
            f"  {status} [{product.get('sku', 'N/A')}] "
            f"{product['title'][:60]} — {product['price']} "
            f"({product['availability']})"
        )

        # Progress logging every 25 products
        if (i + 1) % 25 == 0 or (i + 1) == total:
            percent = ((i + 1) / total) * 100
            logger.info(f"Progress: [{i + 1}/{total}] ({percent:.1f}%)")

    # ── Write output ────────────────────────────────────────────────────────
    elapsed = round(time.time() - start_time, 2)

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
            "scraping_duration_seconds": elapsed,
            "failed_products": failed,
            "rate_limit_delay": DELAY_BETWEEN_REQUESTS,
        },
    }

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    output_file = os.path.join(SCRIPT_DIR, f"output_{timestamp}.json")

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    success = len(results) - failed
    logger.info("=" * 80)
    logger.info("EXTRACTION COMPLETE")
    logger.info(f"Total: {len(results)}, Success: {success}, Failed: {failed}")
    logger.info(f"Duration: {elapsed}s")
    logger.info(f"Output: {output_file}")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
