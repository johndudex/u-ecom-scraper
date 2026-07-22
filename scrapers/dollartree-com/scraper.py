#!/usr/bin/env python3
"""Dollar Tree product scraper.

Extracts product data from dollartree.com product pages using HTTP requests.
Primary data sources: JSON-LD structured data + HTML meta tags (server-rendered
in Oracle Commerce Cloud), with CSS fallbacks where applicable.

IMPORTANT: JSON-LD offers.price is the CASE price (e.g. $42 for 24 units),
NOT the per-unit price. Unit price comes from meta[property='product:price:amount'].
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SITE_NAME = "Dollar Tree"
SITE_URL = "https://www.dollartree.com"
PLATFORM = "oracle_commerce_cloud"
SCRAPING_METHOD = "http_requests"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "logs", "dollartree-com.log")

# Create logs directory if needed
os.makedirs(os.path.join(SCRIPT_DIR, "logs"), exist_ok=True)

DELAY = 2.0  # seconds between requests
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class Product:
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
    brand: str = ""
    category: str = ""
    description: str = ""
    sku: str = ""
    image: str = ""
    rating: str = ""
    review_count: str = ""
    remarks: str = ""


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def extract_jsonld(html: str) -> Optional[dict]:
    """Extract the first JSON-LD Product block from page HTML."""
    soup = BeautifulSoup(html, "html.parser")
    scripts = soup.find_all("script", type="application/ld+json")
    for script in scripts:
        try:
            data = json.loads(script.string)
            if isinstance(data, dict) and data.get("@type") == "Product":
                return data
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def extract_meta_content(soup: BeautifulSoup, property_name: str) -> Optional[str]:
    """Extract content from a <meta> tag by property attribute."""
    tag = soup.find("meta", attrs={"property": property_name})
    if tag and tag.get("content"):
        return tag.get("content", "").strip()
    return None


def normalize_availability(raw: str) -> str:
    """Normalize schema.org availability to In Stock / Out of Stock."""
    if not raw:
        return ""
    mapping = {
        "InStock": "In Stock",
        "LimitedAvailability": "In Stock",
        "OnlineOnly": "In Stock",
        "PreOrder": "In Stock",
        "SoldOut": "Out of Stock",
        "Discontinued": "Out of Stock",
        "OutOfStock": "Out of Stock",
    }
    # Strip the schema.org prefix if present
    clean = raw.replace("http://schema.org/", "").replace("https://schema.org/", "")
    return mapping.get(clean, clean)


def detect_soft_404(soup: BeautifulSoup, final_url: str, requested_url: str) -> Optional[str]:
    """Detect soft 404 / product-not-found pages."""
    # Check for redirect to a non-product page
    if final_url and final_url != requested_url:
        path = final_url.rstrip("/")
        if "/category/" in path or "/search?" in path:
            return "Soft 404: redirected to category/search page"

    # Check page title or H1 for not-found indicators
    title_tag = soup.find("title")
    h1_tag = soup.find("h1")
    for text_source in [title_tag, h1_tag]:
        if text_source and text_source.string:
            lower = text_source.string.lower()
            for indicator in [
                "not found",
                "page not found",
                "product not found",
                "no longer available",
                "unavailable",
                "discontinued",
                "item not found",
                "we couldn't find",
                "error 404",
            ]:
                if indicator in lower:
                    return f"Soft 404: '{text_source.string.strip()}'"

    return None


def extract_sku_from_url(url: str) -> Optional[str]:
    """Extract product SKU/code from Dollar Tree URL pattern /product-name/SKU."""
    match = re.search(r"/([A-Za-z0-9]{3,})(?:\?|$|/)", url)
    if match:
        return match.group(1)
    # Also try ending pattern
    match = re.search(r"/([^/]+)$", url.rstrip("/"))
    if match:
        candidate = match.group(1)
        if candidate.isdigit() or len(candidate) >= 3:
            return candidate
    return None


# ---------------------------------------------------------------------------
# Product extraction
# ---------------------------------------------------------------------------
def extract_product(url: str, index: int) -> Product:
    """Fetch and extract product data from a single Dollar Tree product URL."""
    product = Product(id=index, url=url, src_url=url)
    product.scraped_at = datetime.now(timezone.utc).isoformat()

    try:
        time.sleep(DELAY)
        resp = requests.get(url, headers=HEADERS, timeout=20)
        product.status_code = resp.status_code
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error("Failed to fetch %s: %s", url, e)
        product.remarks = f"Request failed: {e}"
        return product

    # Check for redirect
    final_url = resp.url
    if final_url and final_url.rstrip("/") != url.rstrip("/"):
        logger.info("Redirected: %s -> %s", url, final_url)

    html = resp.text
    soup = BeautifulSoup(html, "html.parser")

    # --- Soft 404 detection ---
    soft_404 = detect_soft_404(soup, final_url, url)
    if soft_404:
        logger.warning("Soft 404 detected for %s: %s", url, soft_404)
        product.remarks = soft_404
        return product

    # --- JSON-LD extraction ---
    jsonld = extract_jsonld(html)
    if not jsonld:
        # No JSON-LD Product block found — likely not a product page
        logger.warning("No JSON-LD Product found for %s", url)
        product.remarks = "No JSON-LD Product block found on page"
        # Still try meta tags below before giving up

    # --- Meta tag extraction (primary for price) ---
    meta_price = extract_meta_content(soup, "product:price:amount")
    meta_currency = extract_meta_content(soup, "product:price:currency")

    # --- Extract fields ---
    # Title: CSS first (h1.product-name may be server-rendered), then JSON-LD
    title = ""
    h1_tag = soup.select_one("h1.product-name")
    if h1_tag:
        title = h1_tag.get_text(strip=True)
    if not title and jsonld:
        title = jsonld.get("name", "")
    product.title = title

    # Price: meta tag (unit price) — NOT JSON-LD offers.price (case price)
    if meta_price:
        product.price = f"${meta_price}"
    elif jsonld:
        # Fallback to JSON-LD but mark as case price
        offers = jsonld.get("offers", {})
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        if isinstance(offers, dict):
            case_price = offers.get("price", "")
            if case_price:
                product.price = f"${case_price}"
                product.remarks = "Price from JSON-LD (may be case price, not unit price)"

    # Currency
    if meta_currency:
        product.currency = meta_currency
    elif jsonld:
        offers_cur = jsonld.get("offers", {})
        if isinstance(offers_cur, list):
            offers_cur = offers_cur[0] if offers_cur else {}
        if isinstance(offers_cur, dict):
            product.currency = offers_cur.get("priceCurrency", "")

    # Availability
    if jsonld:
        offers_avail = jsonld.get("offers", {})
        if isinstance(offers_avail, list):
            offers_avail = offers_avail[0] if offers_avail else {}
        if isinstance(offers_avail, dict):
            raw_avail = offers_avail.get("availability", "")
            product.availability = normalize_availability(raw_avail)

    # Brand
    if jsonld:
        brand_data = jsonld.get("brand")
        if isinstance(brand_data, dict):
            product.brand = brand_data.get("name", "") or ""
        elif isinstance(brand_data, str):
            product.brand = brand_data

    # Category
    if jsonld:
        cat = jsonld.get("category", "")
        if isinstance(cat, list):
            product.category = " > ".join(str(c) for c in cat)
        else:
            product.category = str(cat) if cat else ""

    # Description
    if jsonld:
        product.description = jsonld.get("description", "")

    # SKU
    if jsonld:
        product.sku = str(jsonld.get("sku", ""))
    if not product.sku:
        sku_from_url = extract_sku_from_url(url)
        if sku_from_url:
            product.sku = sku_from_url

    # Image: JSON-LD primary, scoped to product
    if jsonld:
        img = jsonld.get("image", "")
        if isinstance(img, list) and img:
            product.image = img[0] if isinstance(img[0], str) else str(img[0])
        elif isinstance(img, str):
            product.image = img
    if not product.image:
        img_tag = soup.select_one("img.ccz-small")
        if img_tag and img_tag.get("src"):
            src = img_tag["src"]
            # Filter out non-product images
            skip_patterns = ["/brand.assets/", "/emoji/", "/flags/", "/icon/", "/navigation/"]
            if not any(p in src for p in skip_patterns):
                product.image = src

    # Rating (from Bazaarvoice)
    rating_el = soup.select_one("[class*='bv-rating'] button[aria-label]")
    if rating_el:
        aria = rating_el.get("aria-label", "")
        m = re.search(r"(\d+(?:\.\d+)?) out of 5", aria)
        if m:
            product.rating = m.group(1)

    # Review count
    bv_rating_el = soup.select_one("[class*='bv-rating']")
    if bv_rating_el:
        text = bv_rating_el.get_text()
        m = re.search(r"\((\d+)\)", text)
        if m:
            product.review_count = m.group(1)

    # --- Content-type filter: must have title + at least price ---
    if not product.title:
        product.remarks = (product.remarks + "; No title found").lstrip("; ")

    return product


# ---------------------------------------------------------------------------
# URL loading
# ---------------------------------------------------------------------------
def load_urls(path: str) -> list[str]:
    """Load product URLs from a JSON file."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    urls = data.get("urls", [])
    if not urls:
        logger.error("No URLs found in %s", path)
        sys.exit(1)
    return urls


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description=f"{SITE_NAME} product scraper")
    parser.add_argument("--input", type=str, help="Path to input URLs JSON file")
    parser.add_argument("--urls", nargs="+", help="Product URLs as CLI arguments")
    parser.add_argument("--sample", action="store_true", help="Scrape only 5 products")
    parser.add_argument("--limit", type=int, default=0, help="Max products to scrape")
    parser.add_argument("--no-proxy", action="store_true", help="Explicitly disable proxy (default for this site)")
    args = parser.parse_args()

    # Determine URLs
    if args.urls:
        urls = args.urls
    elif args.input:
        urls = load_urls(args.input)
    else:
        default_input = os.path.join(SCRIPT_DIR, "input_urls.json")
        if os.path.exists(default_input):
            urls = load_urls(default_input)
        else:
            logger.error("No input URLs provided. Use --input, --urls, or place input_urls.json in %s", SCRIPT_DIR)
            sys.exit(1)

    # Apply limits
    if args.sample:
        urls = urls[:5]
        logger.info("Sample mode: limiting to 5 products")
    elif args.limit > 0:
        urls = urls[: args.limit]

    logger.info("=" * 80)
    logger.info("Starting scraper for %s", SITE_NAME)
    logger.info("Total products: %d", len(urls))
    logger.info("Scraping method: %s", SCRAPING_METHOD)
    logger.info("=" * 80)

    start_time = time.time()
    results: list[dict] = []
    success = 0
    failed = 0

    for i, url in enumerate(urls, start=1):
        logger.info("[%d/%d] Scraping: %s", i, len(urls), url)
        product = extract_product(url, i)

        # Convert to dict for JSON output
        p_dict = {
            "id": product.id,
            "title": product.title,
            "price": product.price,
            "availability": product.availability,
            "original_price": product.original_price,
            "currency": product.currency,
            "url": product.url,
            "src_url": product.src_url,
            "location": product.location,
            "status_code": product.status_code,
            "scraped_at": product.scraped_at,
            "brand": product.brand,
            "category": product.category,
            "description": product.description,
            "sku": product.sku,
            "image": product.image,
            "rating": product.rating,
            "review_count": product.review_count,
            "remarks": product.remarks,
        }

        # Content-type filter: keep items with title AND at least one core field
        has_title = bool(product.title)
        has_core = bool(product.price) or bool(product.availability)
        is_soft_404 = "soft 404" in (product.remarks or "").lower() or "no json-ld" in (product.remarks or "").lower()

        if has_title and has_core:
            results.append(p_dict)
            success += 1
        elif is_soft_404 or not has_title:
            # Include as error item for visibility
            results.append(p_dict)
            failed += 1
        else:
            # Has title but no price/availability — still include but note
            p_dict["remarks"] = (p_dict.get("remarks", "") + "; Incomplete product data").lstrip("; ")
            results.append(p_dict)
            success += 1

        # Progress logging
        if i % 25 == 0 or i == len(urls):
            pct = (i / len(urls)) * 100
            logger.info("Progress: [%d/%d] (%.1f%%)", i, len(urls), pct)

    duration = round(time.time() - start_time, 2)

    # --- Write output ---
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    output_file = os.path.join(SCRIPT_DIR, f"output_{timestamp}.json")

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
            "scraping_duration_seconds": duration,
            "failed_products": failed,
            "rate_limit_delay": DELAY,
        },
    }

    with open(output_file, "w", encoding="utf-8") as f:
        # _OUTPUT_FILTER_APPLIED — drop non-item pages (content-type aware)
        try:
            _before = len(output.get("products", []))
            output["products"] = [p for p in output.get("products", []) if p.get('title') and (p.get('price') or p.get('availability') or p.get('currency'))]
            _after = len(output["products"])
            if _before != _after:
                logger.info('output filter: %d → %d items (removed %d without title+price,availability,currency)',
                             _before, _after, _before - _after)
        except Exception:
            pass


        json.dump(output, f, indent=2, ensure_ascii=False)

    logger.info("=" * 80)
    logger.info("Output written to: %s", output_file)
    logger.info("Products in output: %d", len(output["products"]))
    logger.info("EXTRACTION COMPLETE")
    logger.info("Total: %d, Success: %d, Failed: %d", len(results), success, failed)
    logger.info("Duration: %.2fs", duration)
    logger.info("Output: %s", output_file)
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
