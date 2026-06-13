#!/usr/bin/env python3
"""
Adairs New Zealand Product Scraper

Extracts product data from Adairs NZ (adairs.co.nz) product pages using
direct HTTP requests and JSON-LD structured data parsing.

Platform: Episerver CMS (custom)
Scraping method: http_requests (no browser, no proxy needed)
Extraction: JSON-LD only (Product schema + BreadcrumbList schema)

Usage:
    python3 scraper.py                              # reads input_urls.json
    python3 scraper.py --input custom_urls.json      # explicit input file
    python3 scraper.py --urls "https://..."          # CLI URLs
    python3 scraper.py --sample                     # scrape only 5 products
    python3 scraper.py --limit 20                   # max 20 products
    python3 scraper.py --no-proxy                   # explicit no-proxy (default)
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests
from bs4 import BeautifulSoup

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

SITE_NAME = "Adairs New Zealand"
SITE_URL = "https://www.adairs.co.nz/"
PLATFORM = "custom"
SCRAPING_METHOD = "http_requests"
SITE_SLUG = "adairs-co-nz"

DELAY_BETWEEN_REQUESTS = 1.0
MAX_RETRIES = 3
REQUEST_TIMEOUT = 15

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-NZ,en;q=0.9,en-US;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TIMESTAMP = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
OUTPUT_FILE = os.path.join(SCRIPT_DIR, f"output_{TIMESTAMP}.json")
INPUT_FILE = os.path.join(SCRIPT_DIR, "input_urls.json")
LOG_FILE = os.path.join(os.path.dirname(SCRIPT_DIR), "logs", f"{SITE_SLUG}.log")

# Currency symbols for price formatting
CURRENCY_SYMBOLS: dict[str, str] = {
    "NZD": "$",
    "AUD": "$",
    "USD": "$",
    "GBP": "\u00a3",
    "EUR": "\u20ac",
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
# DATA STRUCTURES
# ═══════════════════════════════════════════════════════════════════════════════


def create_product(url: str, src_url: str) -> dict[str, Any]:
    """Create an empty product dict with defaults."""
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
        "remarks": "",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# NETWORK - Direct HTTP (no proxy)
# ═══════════════════════════════════════════════════════════════════════════════


def fetch_page(url: str) -> Optional[tuple[BeautifulSoup, int]]:
    """Fetch a page via direct HTTP. No proxy used."""
    for attempt in range(MAX_RETRIES):
        try:
            time.sleep(DELAY_BETWEEN_REQUESTS)
            logger.debug(f"Fetching {url} (attempt {attempt + 1}/{MAX_RETRIES})")
            response = requests.get(
                url,
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
            )
            status_code = response.status_code

            if status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                return soup, status_code

            logger.warning(
                f"HTTP {status_code} for {url} (attempt {attempt + 1}/{MAX_RETRIES})"
            )
            if status_code in (403, 429, 503):
                logger.warning(
                    f"Possible block detected (HTTP {status_code}), retrying..."
                )
                time.sleep(DELAY_BETWEEN_REQUESTS * 3)
                continue

            # Non-retryable status codes
            return None

        except requests.RequestException as e:
            logger.error(
                f"Request failed for {url} (attempt {attempt + 1}/{MAX_RETRIES}): {e}"
            )
            if attempt < MAX_RETRIES - 1:
                time.sleep(DELAY_BETWEEN_REQUESTS * 2)

    logger.error(f"All retries exhausted for {url}")
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# JSON-LD EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════════


def extract_jsonld_blocks(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Extract all JSON-LD blocks from the page."""
    blocks: list[dict[str, Any]] = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
            if isinstance(data, dict):
                blocks.append(data)
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        blocks.append(item)
        except (json.JSONDecodeError, TypeError, AttributeError):
            continue
    return blocks


def find_product_jsonld(blocks: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Find the JSON-LD block with @type == 'Product'."""
    for block in blocks:
        block_type = block.get("@type") or block.get("type")
        if block_type == "Product":
            return block
    return None


def find_breadcrumb_jsonld(
    blocks: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Find the JSON-LD block with @type == 'BreadcrumbList'."""
    for block in blocks:
        block_type = block.get("@type") or block.get("type")
        if block_type == "BreadcrumbList":
            return block
    return None


def extract_breadcrumb_category(
    breadcrumb: Optional[dict[str, Any]],
) -> list[str]:
    """Extract category names from BreadcrumbList JSON-LD."""
    if not breadcrumb:
        return []
    items = breadcrumb.get("itemListElement", [])
    categories: list[str] = []
    for item in items:
        name = item.get("name", "")
        if name:
            categories.append(name)
    return categories


def parse_availability(availability_url: str) -> str:
    """Parse schema.org availability URL to normalized status string."""
    if not availability_url:
        return ""
    segment = availability_url.rstrip("/").split("/")[-1]
    mapping = {
        "InStock": "In Stock",
        "OutOfStock": "Out of Stock",
        "PreOrder": "Pre-Order",
        "Discontinued": "Discontinued",
        "LimitedAvailability": "Limited Availability",
        "InStoreOnly": "In Store Only",
        "OnlineOnly": "Online Only",
    }
    return mapping.get(segment, segment.replace("InStock", "In Stock"))


def format_price(price_val: Any, currency: str = "") -> str:
    """Format a numeric price value with currency symbol."""
    if price_val is None or price_val == "":
        return ""
    try:
        price_float = float(price_val)
        symbol = CURRENCY_SYMBOLS.get(currency, currency)
        formatted = f"{price_float:,.2f}"
        return f"{symbol}{formatted}"
    except (ValueError, TypeError):
        return str(price_val)


def check_maintenance_page(soup: BeautifulSoup) -> bool:
    """Check if the page is the NZ maintenance/coming-soon page."""
    h1 = soup.find("h1")
    if h1:
        h1_text = h1.get_text(strip=True).lower()
        if "update for our new zealand customers" in h1_text:
            return True
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# PRODUCT EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════════


def extract_product(url: str, src_url: str) -> dict[str, Any]:
    """Extract product data from a single product page URL.

    Uses JSON-LD structured data (Product schema + BreadcrumbList schema)
    as the primary and only data source. No CSS selectors needed.

    Handles the NZ maintenance page gracefully by detecting its signature
    h1 text and reporting no product data.
    """
    product = create_product(url, src_url)

    result = fetch_page(url)
    if result is None:
        product["status_code"] = 0
        product["remarks"] = "Failed to fetch page after all retries"
        return product

    soup, status_code = result
    product["status_code"] = status_code

    # Check for NZ maintenance page
    is_maintenance = check_maintenance_page(soup)

    # Extract all JSON-LD blocks
    jsonld_blocks = extract_jsonld_blocks(soup)
    product_jsonld = find_product_jsonld(jsonld_blocks)
    breadcrumb_jsonld = find_breadcrumb_jsonld(jsonld_blocks)

    # Maintenance page detected — no product data available
    if is_maintenance and product_jsonld is None:
        product["availability"] = "Out of Stock"
        product["remarks"] = "NZ site is in maintenance mode — no product data available"
        logger.warning(f"Maintenance page detected for {url}")
        return product

    # No Product JSON-LD found
    if product_jsonld is None:
        product["remarks"] = "No Product JSON-LD block found on page"
        logger.warning(f"No Product JSON-LD found for {url}")
        return product

    # ── Extract fields from Product JSON-LD ──────────────────────────────────

    # Title
    product["title"] = product_jsonld.get("name", "")

    # Build remarks from extra fields not in standard output schema
    remarks_parts: list[str] = []

    # SKU
    sku = product_jsonld.get("sku", "")
    if sku:
        remarks_parts.append(f"SKU: {sku}")

    # Brand
    brand = product_jsonld.get("brand", {})
    if isinstance(brand, dict):
        brand_name = brand.get("name", "")
        if brand_name:
            remarks_parts.append(f"Brand: {brand_name}")

    # Description (first 200 chars for context)
    description = product_jsonld.get("description", "")
    if description:
        desc_preview = description[:200] + ("..." if len(description) > 200 else "")
        remarks_parts.append(f"Desc: {desc_preview}")

    # Offers (AggregateOffer)
    offers = product_jsonld.get("offers", {})
    if isinstance(offers, dict):
        currency = offers.get("priceCurrency", "")
        product["currency"] = currency

        low_price = offers.get("lowPrice")
        high_price = offers.get("highPrice")

        # Format the primary price from lowPrice (the 'from' price)
        if low_price is not None:
            product["price"] = format_price(low_price, currency)

        # Note the full price range in remarks if low != high
        # original_price stays empty — no 'was' price in JSON-LD
        if high_price is not None and low_price is not None:
            try:
                low_f = float(low_price)
                high_f = float(high_price)
                if high_f > low_f:
                    remarks_parts.append(
                        f"Price range: {format_price(low_price, currency)} "
                        f"- {format_price(high_price, currency)}"
                    )
            except (ValueError, TypeError):
                pass

        # Availability
        availability_url = offers.get("availability", "")
        product["availability"] = parse_availability(availability_url)

    # Images count
    images = product_jsonld.get("image", [])
    if isinstance(images, str):
        images = [images]
    if images:
        remarks_parts.append(f"Images: {len(images)}")

    # Reviews — prefer actual array length over aggregateRating (data discrepancy)
    reviews = product_jsonld.get("review", [])
    if isinstance(reviews, list):
        actual_review_count = len(reviews)
        if actual_review_count > 0:
            remarks_parts.append(f"Reviews: {actual_review_count}")

    # Category from BreadcrumbList
    breadcrumb_categories = extract_breadcrumb_category(breadcrumb_jsonld)
    if breadcrumb_categories:
        category_str = " > ".join(breadcrumb_categories)
        remarks_parts.append(f"Category: {category_str}")

    # Set remarks from collected parts
    product["remarks"] = "; ".join(remarks_parts)

    return product


# ═══════════════════════════════════════════════════════════════════════════════
# INPUT HANDLING
# ═══════════════════════════════════════════════════════════════════════════════


def load_urls_from_file(filepath: str) -> list[str]:
    """Load product URLs from a JSON file."""
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("urls", [])


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════


def main() -> None:
    """Main entry point for the scraper."""
    parser = argparse.ArgumentParser(
        description=f"HTTP scraper for {SITE_NAME} — JSON-LD extraction"
    )
    parser.add_argument(
        "--input", type=str, default=None, help="Path to input URLs JSON file"
    )
    parser.add_argument(
        "--urls", nargs="+", default=None, help="Product URLs as CLI arguments"
    )
    parser.add_argument(
        "--sample", action="store_true", help="Scrape only 5 products"
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Max products to scrape"
    )
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        default=True,
        help="Run without proxy (default for this site)",
    )
    args = parser.parse_args()

    start_time = time.time()

    logger.info("=" * 80)
    logger.info(f"Starting scraper for {SITE_NAME}")
    logger.info(f"Site: {SITE_URL}")
    logger.info(f"Scraping method: {SCRAPING_METHOD}")
    logger.info(f"Extraction: JSON-LD only")
    logger.info(f"No proxy: {args.no_proxy}")
    logger.info(f"Output: {OUTPUT_FILE}")
    logger.info("=" * 80)

    # Load product URLs
    product_urls: list[str] = []

    if args.urls:
        product_urls = list(args.urls)
        logger.info(f"Loaded {len(product_urls)} URLs from CLI arguments")
    elif args.input:
        product_urls = load_urls_from_file(args.input)
        logger.info(f"Loaded {len(product_urls)} URLs from {args.input}")
    elif os.path.exists(INPUT_FILE):
        product_urls = load_urls_from_file(INPUT_FILE)
        logger.info(f"Loaded {len(product_urls)} URLs from {INPUT_FILE}")
    else:
        logger.error(
            f"No input file found at {INPUT_FILE} and no URLs provided via CLI"
        )
        logger.error("Use --input or --urls to provide product URLs")
        sys.exit(1)

    # Apply limits
    if args.sample:
        product_urls = product_urls[:5]
        logger.info("Sample mode: limiting to 5 products")
    if args.limit:
        product_urls = product_urls[: args.limit]
        logger.info(f"Limit mode: max {args.limit} products")

    total = len(product_urls)
    logger.info(f"Total products to scrape: {total}")
    logger.info("=" * 80)

    # Scrape each product
    results: list[dict[str, Any]] = []
    failed = 0

    for i, url in enumerate(product_urls, start=1):
        logger.info(f"[{i}/{total}] Scraping: {url}")
        product = extract_product(url, url)  # src_url = url for input URLs
        product["id"] = i
        results.append(product)

        if product.get("remarks"):
            logger.info(f"  Remarks: {product['remarks']}")

        if product.get("status_code", 0) == 0 and not product.get("title"):
            failed += 1
            logger.warning(f"  FAILED to extract product data")
        else:
            logger.info(
                f"  OK Title: {product.get('title', 'N/A')} | "
                f"Price: {product.get('price', 'N/A')} | "
                f"Availability: {product.get('availability', 'N/A')}"
            )

        # Progress reporting every 25 products (or every product if <= 25)
        if i % 25 == 0 or (i == total and total <= 25):
            percent = (i / total) * 100
            logger.info(f"Progress: [{i}/{total}] ({percent:.1f}%)")

    # Build output
    duration = round(time.time() - start_time, 2)
    output: dict[str, Any] = {
        "site": {
            "name": SITE_NAME,
            "url": SITE_URL,
            "platform": PLATFORM,
            "scraping_method": SCRAPING_METHOD,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        },
        "products": results,
        "metadata": {
            "scraping_duration_seconds": duration,
            "failed_products": failed,
            "rate_limit_delay": DELAY_BETWEEN_REQUESTS,
        },
    }

    # Write output file
    os.makedirs(SCRIPT_DIR, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    logger.info("=" * 80)
    logger.info("EXTRACTION COMPLETE")
    success = len(results) - failed
    logger.info(f"Total: {len(results)}, Success: {success}, Failed: {failed}")
    logger.info(f"Duration: {duration}s")
    logger.info(f"Output: {OUTPUT_FILE}")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
