#!/usr/bin/env python3
"""American Eagle Australia — Product Scraper.

Extracts product data from americaneagle.com.au via the Localised Inc.
Product REST API (/api/product/s/{product_code}).  No browser, no proxy,
no session required.

Usage:
    python3 scraper_draft.py
    python3 scraper_draft.py --input custom_urls.json
    python3 scraper_draft.py --urls "https://americaneagle.com.au/en-au/product/..."
    python3 scraper_draft.py --sample
    python3 scraper_draft.py --limit 10
"""

import argparse
import datetime
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SITE_NAME = "American Eagle Australia"
SITE_URL = "https://americaneagle.com.au"
PLATFORM = "localised"
SCRAPING_METHOD = "http_requests"
CURRENCY_CODE = "AUD"
CURRENCY_SYMBOL = "A$"

PRODUCT_CODE_REGEX = re.compile(r"/en-au/product/[^/]+/(\d+_\d+_\d+)")
API_BASE_URL = "https://americaneagle.com.au/api/product/s"
API_QUERY_PARAMS = "?lang=en&siteTag=AE_AU"
BASIC_AUTH = ("test", "test")  # Optional, API works without it

DELAY = 1.0  # seconds between requests
REQUEST_TIMEOUT = 15  # seconds

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-AU,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Referer": "https://americaneagle.com.au/",
}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "..", "..", "logs", "americaneagle-com-au.log")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Product:
    """Standard product output record."""
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


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def extract_product_code(url: str) -> Optional[str]:
    """Extract the product code (e.g. '1165_5323_131') from a product URL."""
    match = PRODUCT_CODE_REGEX.search(url)
    return match.group(1) if match else None


def extract_color_from_url(url: str) -> Optional[str]:
    """Extract the color parameter from a product URL query string."""
    parsed = urlparse(url)
    color = parse_qs(parsed.query).get("color", [None])
    return color[0] if color[0] else None


def strip_html_tags(html_str: str) -> str:
    """Remove HTML tags and return plain text."""
    if not html_str:
        return ""
    soup = BeautifulSoup(html_str, "html.parser")
    return soup.get_text(separator=" ", strip=True)


def determine_availability(data: dict) -> str:
    """Aggregate availability across ALL size variants in ALL color options.

    Rules:
      - If ANY variant is AVAILABLE → 'In Stock'
      - If no AVAILABLE but some LOWSTOCK → 'Low Stock'
      - If ALL variants are OUTOFSTOCK → 'Out of Stock'
    """
    has_available = False
    has_low_stock = False

    options = data.get("options", [])
    if not options:
        # No variant data — check top-level availability
        if data.get("availableForPurchase"):
            return "In Stock"
        return "Out of Stock"

    for color_option in options:
        for size_option in color_option.get("options", []):
            avail = size_option.get("availability", "").upper()
            if avail == "AVAILABLE":
                has_available = True
            elif avail == "LOWSTOCK":
                has_low_stock = True

    if has_available:
        return "In Stock"
    if has_low_stock:
        return "Low Stock"
    return "Out of Stock"


def format_price(price_value: float) -> str:
    """Format a numeric price as a currency string."""
    return f"{CURRENCY_SYMBOL}{price_value:.2f}"


# ---------------------------------------------------------------------------
# API fetch
# ---------------------------------------------------------------------------

def fetch_product_data(product_code: str, session: requests.Session) -> dict:
    """Fetch product JSON from the Localised Product REST API.

    Returns the parsed JSON dict on success, or an empty dict on failure.
    """
    url = f"{API_BASE_URL}/{product_code}{API_QUERY_PARAMS}"
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        status = resp.status_code

        if status != 200:
            logger.warning(
                "API returned HTTP %d for product code %s", status, product_code
            )
            return {}

        data = resp.json()

        # NOT_FOUND is returned with HTTP 200 — must check the body
        if isinstance(data, dict) and data.get("result") == "NOT_FOUND":
            logger.warning("Product not found (NOT_FOUND) for code %s", product_code)
            return {}

        return data

    except requests.Timeout:
        logger.error("Timeout fetching product %s", product_code)
    except requests.ConnectionError as e:
        logger.error("Connection error fetching product %s: %s", product_code, e)
    except requests.RequestException as e:
        logger.error("Request error fetching product %s: %s", product_code, e)
    except json.JSONDecodeError as e:
        logger.error("JSON decode error for product %s: %s", product_code, e)

    return {}


# ---------------------------------------------------------------------------
# Product extraction
# ---------------------------------------------------------------------------

def extract_product(product_url: str, src_url: str, product_id: int,
                    session: requests.Session) -> Optional[Product]:
    """Extract product data for a single URL using the Localised Product API."""
    product_code = extract_product_code(product_url)
    if not product_code:
        logger.error("Could not extract product code from URL: %s", product_url)
        product = Product(
            id=product_id, url=product_url, src_url=src_url,
            status_code=0, remarks="Failed to extract product code from URL",
        )
        product.scraped_at = datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat()
        return product

    logger.info("Fetching product code: %s", product_code)

    # Rate-limit
    time.sleep(DELAY)

    data = fetch_product_data(product_code, session)
    if not data:
        product = Product(
            id=product_id, url=product_url, src_url=src_url,
            status_code=200,
            remarks="API returned empty or NOT_FOUND response",
        )
        product.scraped_at = datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat()
        return product

    # --- Map fields ---
    product = Product(id=product_id, url=product_url, src_url=src_url)
    product.scraped_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    product.status_code = 200
    product.currency = CURRENCY_CODE

    # Title
    product.title = data.get("name", "")

    # Price — use sale price if on sale, otherwise priceMin
    sale_status = data.get("sale", "NOSALE")
    price_range = data.get("priceRange", {})
    if sale_status == "ONSALE" and price_range.get("saleMin") is not None:
        product.price = format_price(price_range["saleMin"])
    elif data.get("priceMin") is not None:
        product.price = format_price(data["priceMin"])
    elif price_range.get("saleMin") is not None:
        product.price = format_price(price_range["saleMin"])
    else:
        product.price = ""

    # Original price — only when actually on sale with a discount
    if (sale_status == "ONSALE"
            and price_range.get("listMin") is not None
            and price_range.get("saleMin") is not None
            and price_range["listMin"] > price_range["saleMin"]):
        product.original_price = format_price(price_range["listMin"])
    else:
        product.original_price = ""

    # Availability
    product.availability = determine_availability(data)

    # Remarks — optional enrichment
    remarks_parts = []
    color_param = extract_color_from_url(product_url)
    if color_param:
        remarks_parts.append(f"Color: {color_param}")

    brand = data.get("brand", {})
    if brand.get("name"):
        remarks_parts.append(f"Brand: {brand['name']}")

    category_ids = data.get("categoryPageIds", [])
    if category_ids:
        category_str = category_ids[0] if len(category_ids) == 1 else " > ".join(category_ids)
        remarks_parts.append(f"Category: {category_str}")

    product.remarks = " | ".join(remarks_parts)

    return product


# ---------------------------------------------------------------------------
# URL loading
# ---------------------------------------------------------------------------

def load_urls(input_file: Optional[str] = None,
              cli_urls: Optional[list[str]] = None) -> list[str]:
    """Load product URLs from a JSON file or CLI arguments."""
    if cli_urls:
        return cli_urls

    if input_file:
        path = input_file
    else:
        path = os.path.join(SCRIPT_DIR, "input_urls.json")

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        urls = data.get("urls", [])
        if not urls:
            logger.error("No URLs found in %s", path)
            sys.exit(1)
        return urls
    except FileNotFoundError:
        logger.error("Input file not found: %s", path)
        sys.exit(1)
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON in input file: %s", e)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the scraper."""
    parser = argparse.ArgumentParser(
        description="American Eagle Australia Product Scraper"
    )
    parser.add_argument(
        "--input", type=str, default=None,
        help="Path to input URLs JSON file",
    )
    parser.add_argument(
        "--urls", nargs="+", default=None,
        help="Product URL(s) as CLI arguments",
    )
    parser.add_argument(
        "--sample", action="store_true",
        help="Scrape only the first 5 products",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Max number of products to scrape",
    )
    parser.add_argument(
        "--no-proxy", action="store_true", default=True,
        help="Disable proxy (default for this site)",
    )
    args = parser.parse_args()

    # Load URLs
    product_urls = load_urls(args.input, args.urls)

    # Apply limits
    if args.sample:
        product_urls = product_urls[:5]
    if args.limit:
        product_urls = product_urls[: args.limit]

    total = len(product_urls)
    logger.info("=" * 80)
    logger.info("Starting scraper for %s", SITE_NAME)
    logger.info("Strategy: HTTP requests → Localised Product REST API")
    logger.info("Total products: %d", total)
    logger.info("=" * 80)

    # HTTP session (no proxy)
    session = requests.Session()
    session.headers.update(HEADERS)
    session.auth = BASIC_AUTH

    start_time = time.time()
    results: list[Product] = []
    failed_count = 0

    for idx, url in enumerate(product_urls, start=1):
        logger.info(
            "[%d/%d] Scraping: %s", idx, total, url
        )

        try:
            product = extract_product(
                product_url=url,
                src_url=url,
                product_id=idx,
                session=session,
            )
            if product:
                results.append(product)
                logger.info(
                    "  → Title: %s | Price: %s | Availability: %s",
                    product.title[:60] if product.title else "(empty)",
                    product.price or "(empty)",
                    product.availability or "(empty)",
                )
            else:
                failed_count += 1
                logger.warning("  → Failed to extract product data")

        except Exception as e:
            failed_count += 1
            logger.error("  → Unexpected error: %s", e)

        # Progress reporting every 25 products
        if len(results) % 25 == 0 and len(results) > 0:
            percent = (len(results) / total) * 100
            logger.info(
                "Progress: [%d/%d] (%.1f%%)", len(results), total, percent
            )

    duration = round(time.time() - start_time, 2)

    # Build output
    output = {
        "site": {
            "name": SITE_NAME,
            "url": SITE_URL,
            "platform": PLATFORM,
            "scraping_method": SCRAPING_METHOD,
            "scraped_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        },
        "products": [
            {
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
            for p in results
        ],
        "metadata": {
            "scraping_duration_seconds": duration,
            "failed_products": failed_count,
            "rate_limit_delay": DELAY,
        },
    }

    # Write output
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%d_%H%M%S"
    )
    output_file = os.path.join(SCRIPT_DIR, f"output_{timestamp}.json")
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    logger.info("=" * 80)
    logger.info("EXTRACTION COMPLETE")
    logger.info("Total: %d, Success: %d, Failed: %d",
                total, len(results), failed_count)
    logger.info("Output saved to: %s", output_file)
    logger.info("Duration: %.2f seconds", duration)
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
