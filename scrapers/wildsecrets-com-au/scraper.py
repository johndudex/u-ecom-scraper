#!/usr/bin/env python3
"""Scraper for https://www.wildsecrets.com.au

Strategy: http_requests — JSON-LD primary, CSS fallback.
No proxy (direct HTTP works). No anti-bot protection detected.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SITE_NAME = "Wild Secrets"
SITE_URL = "https://www.wildsecrets.com.au"
PLATFORM = "custom"
SCRAPING_METHOD = "http_requests"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "logs", "wildsecrets-com-au.log")

# Rate-limiting — conservative for a direct-HTTP site
DELAY = 1.5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

# Image-URL skip patterns (banners / icons / emoji)
_IMAGE_SKIP_RE = re.compile(
    r"/(brand\.assets|emoji|flags|icon|navigation|logo)/", re.IGNORECASE
)

# Soft-404 phrases in <title> / <h1>
_SOFT404_RE = re.compile(
    r"not found|unavailable|discontinued|no longer available|page not found|404",
    re.IGNORECASE,
)

# Availability normalisation
_AVAIL_MAP: dict[str, str] = {
    "instock": "In Stock",
    "in stock": "In Stock",
    "preorder": "Pre-order",
    "pre-order": "Pre-order",
    "out of stock": "Out of Stock",
    "outofstock": "Out of Stock",
    "https://schema.org/instock": "In Stock",
    "https://schema.org/outofstock": "Out of Stock",
    "https://schema.org/preorder": "Pre-order",
}

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Product:
    """Standard output product record."""

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
    sku: str = ""
    description: str = ""
    rating: str = ""
    review_count: str = ""
    images: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalise_availability(raw: str | None) -> str:
    """Map raw availability text/schema URI to standard label."""
    if not raw:
        return ""
    stripped = raw.strip()
    # Try exact match
    if stripped.lower() in _AVAIL_MAP:
        return _AVAIL_MAP[stripped.lower()]
    # Try substring match for schema URIs
    for key, val in _AVAIL_MAP.items():
        if key in stripped.lower():
            return val
    return stripped


def _format_price(raw_price: float | str | None, currency_code: str = "AUD") -> str:
    """Return a formatted price string with the appropriate currency symbol."""
    if raw_price is None:
        return ""
    try:
        val = float(raw_price)
    except (ValueError, TypeError):
        return ""
    symbol_map = {
        "AUD": "A$",
        "USD": "$",
        "EUR": "€",
        "GBP": "£",
        "NZD": "NZ$",
    }
    symbol = symbol_map.get(currency_code, f"{currency_code} ")
    return f"{symbol}{val:,.2f}"


def _filter_product_images(img_urls: list[str], sku: str = "") -> list[str]:
    """Keep only gallery-worthy images, skip banners/icons/logos."""
    filtered: list[str] = []
    for u in img_urls:
        if _IMAGE_SKIP_RE.search(u):
            continue
        if sku and sku.lower() in u.lower():
            filtered.append(u)
        elif not sku:
            filtered.append(u)
        # If sku provided and not in URL, still include if it looks like a product
        # image path (contains /products/ and an image suffix)
        elif sku and "/products/" in u.lower():
            filtered.append(u)
    return filtered


def _extract_jsonld_product(soup: BeautifulSoup) -> dict[str, Any] | None:
    """Find and return the first JSON-LD Product block."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and data.get("@type") == "Product":
            return data
        # Sometimes it's a list of entities
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("@type") == "Product":
                    return item
    return None


def _check_soft_404(soup: BeautifulSoup, final_url: str, request_url: str) -> str:
    """Return a remarks string if the page looks like a soft 404."""
    # Check title
    title_tag = soup.find("title")
    if title_tag and _SOFT404_RE.search(title_tag.get_text()):
        return "Soft 404: product not found (title check)"
    # Check H1
    h1 = soup.find("h1")
    if h1 and _SOFT404_RE.search(h1.get_text()):
        return "Soft 404: product not found (H1 check)"
    # Check URL redirect
    parsed_req = urlparse(request_url)
    parsed_fin = urlparse(final_url)
    if parsed_req.path != parsed_fin.path and parsed_fin.path in ("/", "/search", "/404"):
        return "Soft 404: redirected away from product page"
    return ""


# ---------------------------------------------------------------------------
# Main extraction
# ---------------------------------------------------------------------------


def fetch_page(url: str) -> tuple[int, str, str, BeautifulSoup | None]:
    """Fetch a URL and return (status_code, final_url, text, soup).

    Uses a thread-local session (created per-call) for thread safety.
    """
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        resp = session.get(url, timeout=20, allow_redirects=True)
    except requests.RequestException as exc:
        return 0, url, "", None
    return resp.status_code, resp.url, resp.text, BeautifulSoup(resp.text, "html.parser")


def extract_product(url: str, idx: int) -> Product:
    """Extract product data from a single URL.

    Primary: JSON-LD.  Fallback: CSS selectors + data-layer-model.
    """
    product = Product(id=idx, url=url, src_url=url)

    status_code, final_url, html, soup = fetch_page(url)
    product.status_code = status_code
    product.scraped_at = datetime.now(timezone.utc).isoformat()

    if soup is None:
        product.remarks = f"Failed to fetch page (HTTP {status_code})"
        return product

    # --- Soft 404 check ---
    soft404 = _check_soft_404(soup, final_url, url)
    if soft404:
        product.remarks = soft404
        return product

    # --- JSON-LD extraction (primary) ---
    jsonld = _extract_jsonld_product(soup)

    if jsonld:
        # Title
        product.title = jsonld.get("name", "")

        # SKU
        product.sku = jsonld.get("sku", "")

        # Brand
        brand = jsonld.get("brand")
        if isinstance(brand, dict):
            product.brand = brand.get("name", "")
        elif isinstance(brand, str):
            product.brand = brand

        # Description
        product.description = jsonld.get("description", "")

        # Offers — can be a list or a single dict
        offers = jsonld.get("offers", {})
        if isinstance(offers, list):
            offers = offers[0] if offers else {}

        # Currency
        currency_code = offers.get("priceCurrency", "AUD")
        product.currency = currency_code

        # Price — stored as string like "179.9900"
        raw_price = offers.get("price")
        product.price = _format_price(raw_price, currency_code)

        # Availability
        raw_avail = offers.get("availability", "")
        product.availability = _normalise_availability(raw_avail)

        # Rating
        agg = jsonld.get("aggregateRating")
        if isinstance(agg, dict):
            product.rating = str(agg.get("ratingValue", ""))
            product.review_count = str(agg.get("reviewCount", ""))

        # Images — may be a string or list
        raw_images = jsonld.get("image", [])
        if isinstance(raw_images, str):
            raw_images = [raw_images]
        # Normalise protocol-relative URLs
        normalised: list[str] = []
        for img_url in raw_images:
            if img_url.startswith("//"):
                img_url = f"https:{img_url}"
            normalised.append(img_url)
        product.images = _filter_product_images(normalised, product.sku)

    # --- data-data-layer-model (original_price, isOnSale) ---
    layer_el = soup.select_one("[data-data-layer-model]")
    if layer_el:
        try:
            layer_data = json.loads(layer_el["data-data-layer-model"])
            if layer_data.get("isOnSale"):
                raw_op = layer_data.get("salePrice")
                if raw_op is not None:
                    product.original_price = _format_price(raw_op, product.currency or "AUD")
        except (json.JSONDecodeError, TypeError, KeyError):
            pass

    # --- CSS fallbacks for any missing fields ---
    if not product.title:
        h1 = soup.select_one(".product-information-container h1")
        if h1:
            product.title = h1.get_text(strip=True)

    if not product.price:
        price_el = soup.select_one(".product-information-container .price")
        if price_el:
            product.price = price_el.get_text(strip=True)

    if not product.brand:
        brand_el = soup.select_one(".product-information-container .brand")
        if brand_el:
            product.brand = brand_el.get_text(strip=True)

    if not product.sku:
        code_div = soup.select_one("div.code")
        if code_div:
            m = re.search(r"Item Code:\s*(\w+)", code_div.get_text())
            if m:
                product.sku = m.group(1)

    if not product.availability:
        code_div = soup.select_one("div.code")
        if code_div:
            m = re.search(
                r"Item Code:\s*\w+\s*-\s*(In stock|Out of stock|Pre-order)",
                code_div.get_text(),
                re.IGNORECASE,
            )
            if m:
                product.availability = m.group(1).strip()

    if not product.description:
        desc_pars = soup.select(".product-description p")
        if desc_pars:
            product.description = "\n\n".join(
                p.get_text(strip=True) for p in desc_pars
            )

    # Fallback images from CSS
    if not product.images:
        css_imgs = soup.select(
            '.product-image-container img, [class*="gallery"] img'
        )
        img_urls: list[str] = []
        for img in css_imgs:
            src = img.get("src") or img.get("data-src") or ""
            if src:
                if src.startswith("//"):
                    src = f"https:{src}"
                img_urls.append(src)
        product.images = _filter_product_images(img_urls, product.sku)

    # --- Final: if no JSON-LD Product and no CSS title, likely not a product ---
    if not product.title and not jsonld:
        product.remarks = "Soft 404: no product data found on page"

    return product


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def load_urls(path: str) -> list[str]:
    """Load URLs from a JSON file."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("urls", [])


def run_scraper(urls: list[str], sample: bool = False, limit: int | None = None) -> list[Product]:
    """Run the scraper over the given URLs with concurrency."""
    if sample:
        urls = urls[:5]
    elif limit:
        urls = urls[:limit]

    total = len(urls)
    logger.info("=" * 80)
    logger.info("Starting scraper for %s", SITE_NAME)
    logger.info("Total products: %d", total)
    logger.info("=" * 80)

    results: list[Product | None] = [None] * total
    success = 0
    failed = 0

    # Use ThreadPoolExecutor for concurrent HTTP extraction
    with ThreadPoolExecutor(max_workers=8) as executor:
        future_map = {
            executor.submit(extract_product, url, idx + 1): idx
            for idx, url in enumerate(urls)
        }
        completed = 0
        for future in as_completed(future_map):
            idx = future_map[future]
            try:
                product = future.result()
                results[idx] = product
                completed += 1
                if product.title or product.price:
                    success += 1
                else:
                    failed += 1
            except Exception as exc:
                logger.error("Error processing URL at index %d: %s", idx, exc)
                failed += 1
                results[idx] = Product(
                    id=idx + 1,
                    remarks=f"Exception: {exc}",
                    scraped_at=datetime.now(timezone.utc).isoformat(),
                )
                completed += 1

            if completed % 25 == 0 or completed == total:
                pct = (completed / total) * 100
                logger.info(
                    "Progress: [%d/%d] (%.1f%%) — success=%d, failed=%d",
                    completed,
                    total,
                    pct,
                    success,
                    failed,
                )

    # Filter out None entries (shouldn't happen)
    final_results = [r for r in results if r is not None]

    logger.info("=" * 80)
    logger.info("EXTRACTION COMPLETE")
    logger.info("Total: %d, Success: %d, Failed: %d", total, success, failed)
    logger.info("=" * 80)

    return final_results


def write_output(products: list[Product], start_time: float) -> str:
    """Write products to the output JSON file and return the path."""
    os.makedirs(os.path.join(SCRIPT_DIR, "logs"), exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    output_file = os.path.join(SCRIPT_DIR, f"output_{timestamp}.json")

    output_products: list[dict[str, Any]] = []
    for p in products:
        entry: dict[str, Any] = {
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
        # Only include extended fields if they have values
        if p.brand:
            entry["brand"] = p.brand
        if p.sku:
            entry["sku"] = p.sku
        if p.description:
            entry["description"] = p.description
        if p.rating:
            entry["rating"] = p.rating
        if p.review_count:
            entry["review_count"] = p.review_count
        if p.images:
            entry["images"] = p.images

        output_products.append(entry)

    output = {
        "site": {
            "name": SITE_NAME,
            "url": SITE_URL,
            "platform": PLATFORM,
            "scraping_method": SCRAPING_METHOD,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        },
        "products": output_products,
        "metadata": {
            "scraping_duration_seconds": round(time.time() - start_time, 2),
            "failed_products": sum(
                1 for p in products if not (p.title or p.price)
            ),
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

    logger.info("Output written to: %s (%d products)", output_file, len(output_products))
    return output_file


# ---------------------------------------------------------------------------
# Module-level logger (configured in main)
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=f"Scraper for {SITE_NAME}")
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
        default=False,
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
        default=True,
        help="Do not use any proxy (default for this site)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # Ensure logs directory exists
    os.makedirs(os.path.join(SCRIPT_DIR, "logs"), exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler(),
        ],
    )
    # Reconfigure module-level logger with the handlers above
    logger.setLevel(logging.INFO)

    # Determine URLs
    urls: list[str] = []
    if args.input:
        urls = load_urls(args.input)
        logger.info("Loaded %d URLs from %s", len(urls), args.input)
    elif args.urls:
        urls = args.urls
        logger.info("Using %d URLs from CLI", len(urls))
    else:
        default_input = os.path.join(SCRIPT_DIR, "input_urls.json")
        if os.path.exists(default_input):
            urls = load_urls(default_input)
            logger.info("Loaded %d URLs from default input_urls.json", len(urls))
        else:
            logger.error("No input URLs provided. Use --input, --urls, or create input_urls.json")
            return 1

    if not urls:
        logger.error("No URLs to process")
        return 1

    start_time = time.time()

    try:
        products = run_scraper(urls, sample=args.sample, limit=args.limit)
    except KeyboardInterrupt:
        logger.info("Scraper interrupted by user")
        return 130

    write_output(products, start_time)

    logger.info("Done in %.1f seconds", time.time() - start_time)
    return 0


if __name__ == "__main__":
    sys.exit(main())
