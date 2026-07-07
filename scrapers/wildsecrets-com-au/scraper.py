#!/usr/bin/env python3
"""Wild Secrets Australia product scraper.

Extracts product data from wildsecrets.com.au product pages using
direct HTTP requests. Primary extraction from JSON-LD structured data
with CSS fallback selectors. Fully server-rendered pages — no JS needed.

Usage:
    python3 scraper_draft.py                     # Full extraction
    python3 scraper_draft.py --sample            # First 5 products only
    python3 scraper_draft.py --limit 20           # Max 20 products
    python3 scraper_draft.py --input urls.json   # Custom input file
    python3 scraper_draft.py --urls <URL> ...     # CLI URLs
    python3 scraper_draft.py --no-proxy           # Explicitly no proxy (default)
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SITE_NAME = "Wild Secrets Australia"
SITE_URL = "https://www.wildsecrets.com.au"
PLATFORM = "custom"
SCRAPING_METHOD = "http_requests"
DOMAIN = "https://www.wildsecrets.com.au"
DELAY = 1.0  # seconds between requests
MAX_WORKERS = 8
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

# Schema.org availability mapping
AVAILABILITY_MAP = {
    "https://schema.org/instock": "In Stock",
    "https://schema.org/limitedavailability": "Limited Stock",
    "https://schema.org/outofstock": "Out of Stock",
    "https://schema.org/preorder": "Pre-Order",
    "https://schema.org/discontinued": "Discontinued",
}

# Soft-404 patterns
SOFT_404_PATTERNS = re.compile(
    r"page\s+not\s+found|product\s+not\s+found|item\s+not\s+found|"
    r"no\s+longer\s+available|unavailable|discontinued|"
    r"404\s+not\s+found|oops|error\s+occurred",
    re.IGNORECASE,
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "logs", "wildsecrets-com-au.log")

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class Product:
    """Standardised product record."""

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
    # Extra fields (not in standard output but kept for richness)
    brand: str = ""
    sku: str = ""
    description: str = ""
    images: list[str] = field(default_factory=list)
    condition: str = ""

    def to_dict(self) -> dict:
        """Serialise to output dict (standard fields only)."""
        return {
            "id": self.id,
            "title": self.title,
            "price": self.price,
            "availability": self.availability,
            "original_price": self.original_price,
            "currency": self.currency,
            "url": self.url,
            "src_url": self.src_url,
            "location": self.location,
            "status_code": self.status_code,
            "scraped_at": self.scraped_at,
            "remarks": self.remarks,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_logging() -> logging.Logger:
    """Configure file + console logging."""
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    return logging.getLogger(__name__)


logger = _setup_logging()


def _format_price(raw_price: str | float | None, currency: str = "AUD") -> str:
    """Convert a raw price value to formatted string with currency symbol.

    Args:
        raw_price: Price as string (e.g. '92.9900') or float.
        currency: ISO 4217 code.

    Returns:
        Formatted price like '$92.99' or empty string on failure.
    """
    if raw_price is None or raw_price == "":
        return ""
    try:
        val = float(raw_price)
    except (ValueError, TypeError):
        return ""
    symbol = {"AUD": "$", "USD": "$", "EUR": "€", "GBP": "£"}.get(currency, "$")
    return f"{symbol}{val:,.2f}"


def _parse_availability_schema(schema_url: str) -> str:
    """Map schema.org availability URL to human-readable text."""
    if not schema_url:
        return ""
    key = schema_url.strip().lower()
    return AVAILABILITY_MAP.get(key, schema_url)


def _normalise_availability(text: str) -> str:
    """Normalise availability text to standard values."""
    if not text:
        return ""
    lower = text.strip().lower()
    if "in stock" in lower or "available" in lower:
        return "In Stock"
    if "out of stock" in lower or "unavailable" in lower:
        return "Out of Stock"
    if "preorder" in lower or "pre-order" in lower:
        return "Pre-Order"
    if "discontinued" in lower:
        return "Discontinued"
    if "limited" in lower:
        return "Limited Stock"
    return text.strip()


def _fix_image_url(url: str) -> str:
    """Prepend https: to protocol-relative image URLs."""
    if url and url.startswith("//"):
        return "https:" + url
    return url


def _detect_soft_404(soup: BeautifulSoup, requested_url: str, final_url: str) -> str:
    """Detect soft-404 pages.

    Returns:
        Description string if soft-404 detected, else empty string.
    """
    # Check URL redirect
    if final_url and final_url != requested_url:
        parsed_final = final_url.rstrip("/")
        parsed_requested = requested_url.rstrip("/")
        if not parsed_final.endswith(parsed_requested.split("/")[-1]):
            return "Soft 404: redirected away from product page"

    # Check page title / H1
    for selector in ["title", "h1"]:
        el = soup.select_one(selector)
        if el and el.get_text(strip=True):
            text = el.get_text(strip=True)
            if SOFT_404_PATTERNS.search(text):
                return f"Soft 404: '{text}'"

    # Check for lack of product container or JSON-LD
    product_container = soup.select_one(".main-product-container")
    if not product_container:
        return "Soft 404: no product container found on page"

    # Check for "no product" JSON-LD absence (only if page loaded OK)
    jsonld = _extract_jsonld(soup)
    if not jsonld:
        # Could be a valid page without JSON-LD, so only flag if container
        # is also empty
        h1 = soup.select_one("h1.title")
        if h1 and SOFT_404_PATTERNS.search(h1.get_text(strip=True)):
            return "Soft 404: product not found in content"

    return ""


# ---------------------------------------------------------------------------
# JSON-LD extraction
# ---------------------------------------------------------------------------


def _extract_jsonld(soup: BeautifulSoup) -> Optional[dict]:
    """Extract the first Product-type JSON-LD block from the page."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and data.get("@type") == "Product":
            return data
        # Handle @graph arrays
        if isinstance(data, dict) and "@graph" in data:
            for item in data["@graph"]:
                if isinstance(item, dict) and item.get("@type") == "Product":
                    return item
    return None


# ---------------------------------------------------------------------------
# Field extractors
# ---------------------------------------------------------------------------


def extract_from_jsonld(jsonld: dict) -> dict:
    """Extract product fields from JSON-LD data.

    Returns a dict of extracted field values (raw, unformatted).
    """
    result: dict = {}
    try:
        result["title"] = jsonld.get("name", "")
    except Exception:
        result["title"] = ""

    try:
        offers = jsonld.get("offers", {})
        if isinstance(offers, dict):
            result["price"] = offers.get("price", "")
            result["currency"] = offers.get("priceCurrency", "")
            result["availability"] = offers.get("availability", "")
            result["condition"] = offers.get("itemCondition", "")
            result["url"] = offers.get("url", "")
        elif isinstance(offers, list) and offers:
            result["price"] = offers[0].get("price", "")
            result["currency"] = offers[0].get("priceCurrency", "")
            result["availability"] = offers[0].get("availability", "")
            result["condition"] = offers[0].get("itemCondition", "")
            result["url"] = offers[0].get("url", "")
    except Exception:
        pass

    try:
        result["sku"] = jsonld.get("sku", "")
    except Exception:
        result["sku"] = ""

    try:
        brand = jsonld.get("brand")
        if isinstance(brand, dict):
            result["brand"] = brand.get("name", "")
        elif isinstance(brand, str):
            result["brand"] = brand
        else:
            result["brand"] = ""
    except Exception:
        result["brand"] = ""

    try:
        images = jsonld.get("image", [])
        if isinstance(images, str):
            images = [images]
        result["images"] = [_fix_image_url(img) for img in images]
    except Exception:
        result["images"] = []

    try:
        result["description"] = jsonld.get("description", "")
    except Exception:
        result["description"] = ""

    return result


def extract_from_css(soup: BeautifulSoup) -> dict:
    """Extract product fields from CSS selectors (fallback).

    Returns a dict of extracted field values.
    """
    result: dict = {}

    # Title
    try:
        el = soup.select_one("h1.title")
        if el:
            result["title"] = el.get_text(strip=True)
    except Exception:
        pass

    # Price (combine .amount + .cents)
    try:
        amount_el = soup.select_one(".price-container .amount")
        cents_el = soup.select_one(".price-container .cents")
        if amount_el:
            amount_text = amount_el.get_text(strip=True)
            cents_text = cents_el.get_text(strip=True) if cents_el else ""
            result["price_display"] = amount_text + cents_text
    except Exception:
        pass

    # Original price ("Don't pay $XX.XX")
    try:
        el = soup.select_one(".dont-pay span")
        if el:
            result["original_price"] = el.get_text(strip=True)
    except Exception:
        pass

    # Availability from add-to-cart container
    try:
        atc = soup.select_one("#add-to-cart-container")
        if atc:
            atc_text = atc.get_text()
            match = re.search(r"Item\s*Code:\s*\S+\s*-\s*(.*)", atc_text, re.IGNORECASE)
            if match:
                result["availability"] = match.group(1).strip()
    except Exception:
        pass

    # SKU from add-to-cart container
    try:
        atc = soup.select_one("#add-to-cart-container")
        if atc:
            atc_text = atc.get_text()
            match = re.search(r"Item\s*Code:\s*(\S+)", atc_text, re.IGNORECASE)
            if match:
                result["sku"] = match.group(1).strip()
    except Exception:
        pass

    # Description
    try:
        el = soup.select_one(".product-description")
        if el:
            result["description"] = el.get_text(strip=True)
    except Exception:
        pass

    # Canonical URL
    try:
        el = soup.select_one("link[rel='canonical']")
        if el:
            result["canonical_url"] = el.get("href", "")
    except Exception:
        pass

    return result


# ---------------------------------------------------------------------------
# Main extraction per product
# ---------------------------------------------------------------------------


def extract_product(url: str, src_url: str, product_id: int, session: requests.Session) -> Product:
    """Fetch and extract data from a single product page.

    Args:
        url: Product page URL.
        src_url: Source listing URL (same as url for direct input).
        product_id: Sequential product ID.
        session: Thread-local requests.Session.

    Returns:
        Product dataclass with extracted data.
    """
    product = Product(
        id=product_id,
        url=url,
        src_url=src_url,
        scraped_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        status_code=0,
    )

    try:
        resp = session.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
        product.status_code = resp.status_code
        resp.raise_for_status()
    except requests.RequestException as e:
        product.remarks = f"Request failed: {e}"
        logger.error(f"[{product_id}] Failed to fetch {url}: {e}")
        return product

    # Check for redirect away from product page
    final_url = resp.url
    soup = BeautifulSoup(resp.text, "lxml")

    # Soft-404 detection
    soft_404 = _detect_soft_404(soup, url, final_url)
    if soft_404:
        product.remarks = soft_404
        product.status_code = resp.status_code
        logger.warning(f"[{product_id}] {soft_404}: {url}")
        return product

    # --- JSON-LD primary extraction ---
    jsonld = _extract_jsonld(soup)
    jsonld_data = extract_from_jsonld(jsonld) if jsonld else {}

    # --- CSS fallback extraction ---
    css_data = extract_from_css(soup)

    # --- Title ---
    product.title = jsonld_data.get("title") or css_data.get("title") or ""

    # --- Price (from JSON-LD, formatted with currency symbol) ---
    raw_price = jsonld_data.get("price", "")
    currency = jsonld_data.get("currency", "AUD")
    if raw_price:
        product.price = _format_price(raw_price, currency)
        product.currency = currency
    elif "price_display" in css_data:
        product.price = css_data["price_display"]
        product.currency = "AUD"

    # --- Availability (prefer CSS for granular text, fall back to JSON-LD) ---
    if css_data.get("availability"):
        product.availability = _normalise_availability(css_data["availability"])
    elif jsonld_data.get("availability"):
        product.availability = _normalise_availability(
            _parse_availability_schema(jsonld_data["availability"])
        )

    # --- Original price (CSS only — "Don't pay" element) ---
    product.original_price = css_data.get("original_price", "")

    # --- URL ---
    canonical = css_data.get("canonical_url", "")
    if canonical:
        product.url = canonical
    elif jsonld_data.get("url"):
        product.url = urljoin(DOMAIN, jsonld_data["url"])

    # --- Store extra fields ---
    product.brand = jsonld_data.get("brand", "")
    product.sku = jsonld_data.get("sku") or css_data.get("sku", "")

    # Description: prefer CSS (full marketing copy) over JSON-LD (may be just name)
    css_desc = css_data.get("description", "")
    jsonld_desc = jsonld_data.get("description", "")
    if css_desc and len(css_desc) > len(jsonld_desc or ""):
        product.description = css_desc
    else:
        product.description = jsonld_desc

    product.images = jsonld_data.get("images", [])

    # Condition
    cond = jsonld_data.get("condition", "")
    if cond:
        product.condition = cond.split("/")[-1] if "/" in cond else cond

    return product


# ---------------------------------------------------------------------------
# Thread worker
# ---------------------------------------------------------------------------


def _thread_worker(
    index: int,
    url: str,
    src_url: str,
    product_id: int,
) -> tuple[int, Product]:
    """Worker for ThreadPoolExecutor — creates its own Session."""
    session = requests.Session()
    session.headers.update(HEADERS)
    # Respect rate limiting: stagger by index
    time.sleep(index * (DELAY / MAX_WORKERS))
    product = extract_product(url, src_url, product_id, session)
    session.close()
    return (index, product)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def load_urls(path: str) -> list[str]:
    """Load URLs from a JSON file."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("urls", [])


def filter_products(products: list[Product]) -> list[Product]:
    """Remove non-product entries (soft-404s, empty items).

    Keep items that have a title AND at least one of: price, availability.
    """
    filtered = []
    for p in products:
        if p.remarks and ("soft 404" in p.remarks.lower() or "redirect" in p.remarks.lower()):
            # Keep soft-404 items but with empty fields — they are in output
            # as indicators of missing products
            filtered.append(p)
            continue
        if p.title and (p.price or p.availability):
            filtered.append(p)
        elif p.title:
            # Has title but no price/availability — still keep it
            filtered.append(p)
    return filtered


def main() -> None:
    """Entry point."""
    parser = argparse.ArgumentParser(description="Wild Secrets Australia product scraper")
    parser.add_argument("--input", type=str, default=None, help="Path to input URLs JSON file")
    parser.add_argument("--urls", nargs="+", default=None, help="Product URLs as CLI arguments")
    parser.add_argument("--sample", action="store_true", help="Scrape only first 5 products")
    parser.add_argument("--limit", type=int, default=0, help="Max products to scrape")
    parser.add_argument("--no-proxy", action="store_true", default=True, help="No proxy (default)")
    args = parser.parse_args()

    # Determine URL source
    if args.urls:
        urls = args.urls
    elif args.input:
        urls = load_urls(args.input)
    else:
        default_input = os.path.join(SCRIPT_DIR, "input_urls.json")
        if os.path.exists(default_input):
            urls = load_urls(default_input)
        else:
            logger.error(f"No input_urls.json found at {default_input}")
            sys.exit(1)

    # Apply --sample / --limit
    if args.sample:
        urls = urls[:5]
    elif args.limit > 0:
        urls = urls[: args.limit]

    logger.info("=" * 80)
    logger.info(f"Starting scraper for {SITE_NAME}")
    logger.info(f"Total products: {len(urls)}")
    logger.info(f"Scraping method: {SCRAPING_METHOD}")
    logger.info(f"Rate limit delay: {DELAY}s")
    logger.info(f"Concurrency: {MAX_WORKERS} workers")
    logger.info("=" * 80)

    start_time = time.time()
    results: dict[int, Product] = {}
    success = 0
    failed = 0

    # Concurrent extraction with thread-local sessions
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(_thread_worker, idx, url, url, idx + 1): idx
            for idx, url in enumerate(urls)
        }

        for future in as_completed(futures):
            idx, product = future.result()
            results[idx] = product
            count = len(results)
            if product.title and product.price:
                success += 1
            elif product.remarks:
                failed += 1
            else:
                failed += 1

            if count % 10 == 0 or count == len(urls):
                pct = (count / len(urls)) * 100
                logger.info(f"Progress: [{count}/{len(urls)}] ({pct:.1f}%)")

    # Reconstruct ordered results
    ordered = [results[i] for i in sorted(results.keys())]
    ordered = filter_products(ordered)

    # Re-index after filtering
    for i, p in enumerate(ordered, 1):
        p.id = i

    # Write output
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    output_file = os.path.join(SCRIPT_DIR, f"output_{timestamp}.json")

    output = {
        "site": {
            "name": SITE_NAME,
            "url": SITE_URL,
            "platform": PLATFORM,
            "scraping_method": SCRAPING_METHOD,
            "scraped_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        },
        "products": [p.to_dict() for p in ordered],
        "metadata": {
            "scraping_duration_seconds": round(time.time() - start_time, 2),
            "failed_products": failed,
            "rate_limit_delay": DELAY,
        },
    }

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
    logger.info(f"EXTRACTION COMPLETE")
    logger.info(f"Total: {len(ordered)}, Success: {success}, Failed: {failed}")
    logger.info(f"Output: {output_file}")
    logger.info(f"Duration: {round(time.time() - start_time, 2)}s")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
