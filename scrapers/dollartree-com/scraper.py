#!/usr/bin/env python3
"""DollarTree.com product scraper using HTTP requests.

Extracts product data from DollarTree product pages via server-rendered
JSON-LD structured data and meta tags. No JavaScript rendering needed.

Strategy: http_requests (direct HTTP, no proxy)
Platform: Oracle Commerce Cloud (OCC)
Extraction: Hybrid — JSON-LD primary, meta tags for price, CSS fallbacks

JSON-LD structure on DollarTree product pages:
  - Block 0: { "@type": "Product", ... } — has offers, sku, category, etc.
  - Block 1: { "aggregateRating": { "@type": "AggregateRating", ... } }
  - Block 2: { "review": { "@type": "Review", ... } }

IMPORTANT: JSON-LD offers.price is BULK/CASE pricing (e.g., $15 for 12 units).
The actual per-item price comes from meta[property='product:price:amount'].
"""

import argparse
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SITE_NAME = "DollarTree"
SITE_URL = "https://www.dollartree.com"
PLATFORM = "custom"  # Oracle Commerce Cloud (OCC)
SCRAPING_METHOD = "http_requests"
DELAY = 2.0  # Rate limiting delay in seconds (Akamai CDN present)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "logs", "dollartree-com.log")
INPUT_URLS_FILE = os.path.join(SCRIPT_DIR, "input_urls.json")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) "
    "Gecko/20100101 Firefox/127.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.2 Safari/605.1.15",
]

# Image URL patterns to skip (navigation, icons, brands, etc.)
IMAGE_SKIP_PATTERNS = [
    "/brand.assets/", "/emoji/", "/flags/", "/icon/", "/navigation/",
    "/logo/", "/badge/",
]

# Soft 404 detection markers
SOFT_404_MARKERS = [
    "not found", "page not found", "product not found",
    "unavailable", "discontinued", "no longer available",
    "item not found", "we couldn't find",
]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

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
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class Product:
    """Standard product data structure."""

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
    # Extended fields (not in base schema but extractable)
    sku: str = ""
    brand: str = ""
    category: str = ""
    description: str = ""
    image: str = ""
    rating: str = ""
    review_count: str = ""


@dataclass
class JSONLDData:
    """Parsed JSON-LD blocks from a product page."""

    product: Optional[dict[str, Any]] = None
    aggregate_rating: Optional[dict[str, Any]] = None
    review: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------


def create_session() -> requests.Session:
    """Create a requests session with appropriate headers."""
    session = requests.Session()
    session.headers.update({
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "User-Agent": random.choice(USER_AGENTS),
    })
    return session


# ---------------------------------------------------------------------------
# JSON-LD extraction
# ---------------------------------------------------------------------------


def parse_all_jsonld(soup: BeautifulSoup) -> JSONLDData:
    """Parse all JSON-LD blocks from the page.

    DollarTree product pages have 3 JSON-LD blocks:
      - Block 0: { "@type": "Product", "name": ..., "offers": {...}, ... }
      - Block 1: { "aggregateRating": { "@type": "AggregateRating", ... } }
        NOTE: NO top-level @type — use wrapper key 'aggregateRating'
      - Block 2: { "review": { "@type": "Review", ... } }
        NOTE: NO top-level @type — use wrapper key 'review'
    """
    data = JSONLDData()
    scripts = soup.find_all("script", type="application/ld+json")

    for idx, script in enumerate(scripts):
        if not script.string:
            continue
        try:
            parsed = json.loads(script.string)
        except (json.JSONDecodeError, TypeError):
            continue

        if not isinstance(parsed, dict):
            continue

        atype = parsed.get("@type", "")

        # Block 0: Product (has @type)
        if atype == "Product" and data.product is None:
            data.product = parsed
            logger.debug("JSON-LD block %d: Product found", idx)
            continue

        # Block 1: AggregateRating (NO @type, has 'aggregateRating' key)
        if data.aggregate_rating is None and "aggregateRating" in parsed:
            agg = parsed["aggregateRating"]
            if isinstance(agg, dict):
                data.aggregate_rating = agg
                logger.debug(
                    "JSON-LD block %d: AggregateRating found via wrapper key", idx
                )
            continue

        # Block 2: Review (NO @type, has 'review' key)
        if data.review is None and "review" in parsed:
            rev = parsed["review"]
            if isinstance(rev, dict):
                data.review = rev
                logger.debug(
                    "JSON-LD block %d: Review found via wrapper key", idx
                )
            continue

        # Also handle case where @type IS set on blocks 1/2
        if atype == "AggregateRating" and data.aggregate_rating is None:
            data.aggregate_rating = parsed
        elif atype == "Review" and data.review is None:
            data.review = parsed

    return data


def get_offers(product_block: dict[str, Any]) -> dict[str, Any]:
    """Extract the offers dict from a JSON-LD Product block.

    Handles cases where offers is a dict, a list of dicts, or nested.
    """
    offers_raw = product_block.get("offers")
    if offers_raw is None:
        return {}

    if isinstance(offers_raw, dict):
        return offers_raw

    if isinstance(offers_raw, list):
        # Return the first dict in the list
        for item in offers_raw:
            if isinstance(item, dict):
                return item
        return {}

    return {}


# ---------------------------------------------------------------------------
# Soft 404 detection
# ---------------------------------------------------------------------------


def detect_soft_404(
    soup: BeautifulSoup,
    status_code: int,
    requested_url: str,
    final_url: str,
    jsonld: JSONLDData,
) -> tuple[bool, str]:
    """Detect soft 404 pages where the product no longer exists.

    Returns (is_soft_404, reason_string).
    """
    # Real HTTP 404
    if status_code == 404:
        return True, "Soft 404: HTTP 404 returned"

    # Check if final URL differs significantly from requested URL (redirect)
    if final_url and requested_url:
        req_path = urlparse(requested_url).path.rstrip("/")
        fin_path = urlparse(final_url).path.rstrip("/")
        if fin_path != req_path and fin_path != "":
            return True, f"Soft 404: redirected to {final_url}"

    # No JSON-LD Product block means likely not a product page
    if jsonld.product is None:
        # Check page title and H1 for 404 markers
        title_text = ""
        h1_el = soup.find("h1")
        if h1_el:
            title_text = h1_el.get_text(strip=True).lower()

        for marker in SOFT_404_MARKERS:
            if marker in title_text:
                return True, f"Soft 404: H1 contains '{marker}'"

        # Also check <title> tag
        page_title = soup.find("title")
        if page_title:
            pt_text = page_title.get_text(strip=True).lower()
            for marker in SOFT_404_MARKERS:
                if marker in pt_text:
                    return True, f"Soft 404: page title contains '{marker}'"

        return True, "Soft 404: no JSON-LD Product found on page"

    # Check JSON-LD product name for 404 markers
    product_name = jsonld.product.get("name", "")
    if product_name:
        name_lower = str(product_name).lower()
        for marker in SOFT_404_MARKERS:
            if marker in name_lower:
                return True, f"Soft 404: product name contains '{marker}'"

    return False, ""


# ---------------------------------------------------------------------------
# Field extraction helpers
# ---------------------------------------------------------------------------


def extract_price_from_meta(soup: BeautifulSoup, raw_html: str) -> tuple[str, str]:
    """Extract price and currency from meta tags.

    Returns (price_string, currency_string).
    The meta product:price:amount has the per-item price (e.g., '1.25'),
    NOT the JSON-LD bulk price ($15).

    Uses multiple strategies:
      1. BeautifulSoup with property attribute
      2. BeautifulSoup with name attribute
      3. Regex on raw HTML (ultimate fallback)
    """
    price = ""
    currency = ""

    # Strategy 1: property attribute (standard)
    meta_price = soup.find(
        "meta",
        attrs={"property": "product:price:amount"},
    )
    if not meta_price:
        # Strategy 2: name attribute (some sites use name= instead of property=)
        meta_price = soup.find(
            "meta",
            attrs={"name": "product:price:amount"},
        )
    if not meta_price:
        # Strategy 3: regex on raw HTML
        m = re.search(
            r'<meta[^>]+(?:property|name)\s*=\s*["\']product:price:amount["\'][^>]+content\s*=\s*["\']([\d.]+)["\']',
            raw_html,
            re.IGNORECASE,
        )
        if not m:
            # Try reversed attribute order
            m = re.search(
                r'<meta[^>]+content\s*=\s*["\']([\d.]+)["\'][^>]+(?:property|name)\s*=\s*["\']product:price:amount["\']',
                raw_html,
                re.IGNORECASE,
            )
        if m:
            price = m.group(1).strip()

    if meta_price and meta_price.get("content"):
        raw = meta_price["content"].strip()
        try:
            val = float(raw)
            if 0 < val < 15:  # Sanity check — bulk price would be ~15
                price = f"${val:.2f}"
        except (ValueError, TypeError):
            price = f"${raw}"

    # Currency meta tag
    meta_currency = soup.find(
        "meta",
        attrs={"property": "product:price:currency"},
    )
    if not meta_currency:
        meta_currency = soup.find(
            "meta",
            attrs={"name": "product:price:currency"},
        )
    if not meta_currency:
        m = re.search(
            r'<meta[^>]+(?:property|name)\s*=\s*["\']product:price:currency["\'][^>]+content\s*=\s*["\']([A-Z]{3})["\']',
            raw_html,
            re.IGNORECASE,
        )
        if not m:
            m = re.search(
                r'<meta[^>]+content\s*=\s*["\']([A-Z]{3})["\'][^>]+(?:property|name)\s*=\s*["\']product:price:currency["\']',
                raw_html,
                re.IGNORECASE,
            )
        if m:
            currency = m.group(1).strip()

    if meta_currency and meta_currency.get("content"):
        currency = meta_currency["content"].strip()

    return price, currency


def extract_price_from_css(soup: BeautifulSoup) -> str:
    """Extract price from CSS fallback selector .list-sale-price."""
    el = soup.select_one(".list-sale-price")
    if el:
        text = el.get_text(strip=True)
        # The text is like '$1.25 each' or similar
        match = re.search(r"\$?\d+\.?\d*", text)
        if match:
            return match.group().replace("$", "")
    return ""


def format_price(raw_value: str) -> str:
    """Ensure price string has a currency symbol.

    Args:
        raw_value: A price string, e.g. '1.25' or '$1.25'.

    Returns:
        Formatted price with $ prefix, e.g. '$1.25'.
    """
    if not raw_value:
        return ""
    raw_value = raw_value.strip()
    if raw_value.startswith("$"):
        return raw_value
    try:
        val = float(raw_value)
        return f"${val:.2f}"
    except (ValueError, TypeError):
        return f"${raw_value}"


def normalize_availability(raw: str) -> str:
    """Normalize schema.org availability to standard strings."""
    if not raw:
        return ""
    # Handle full URLs like 'http://schema.org/InStock'
    segment = raw.split("/")[-1] if "/" in raw else raw
    mapping = {
        "InStock": "In Stock",
        "InStoreOnly": "In Stock",
        "LimitedAvailability": "In Stock",
        "OnlineOnly": "In Stock",
        "OutOfStock": "Out of Stock",
        "Discontinued": "Out of Stock",
        "PreOrder": "In Stock",
    }
    return mapping.get(segment, segment.replace("Stock", " Stock"))


def extract_image_url(
    soup: BeautifulSoup,
    jsonld: JSONLDData,
) -> str:
    """Extract product image URL, scoped to product gallery."""
    # Primary: JSON-LD Product image
    if jsonld.product:
        img = jsonld.product.get("image")
        if isinstance(img, str) and img.startswith("http"):
            return img
        if isinstance(img, list) and img:
            for item in img:
                if isinstance(item, str) and item.startswith("http"):
                    return item

    # Fallback: meta og:image
    meta_img = soup.find("meta", attrs={"property": "og:image"})
    if meta_img and meta_img.get("content"):
        return meta_img["content"].strip()

    # Fallback: CSS selector scoped to product details
    container = soup.select_one(".oc3-product-details")
    if container:
        imgs = container.select("img.img-responsive")
        for img_el in imgs:
            src = img_el.get("src") or img_el.get("data-src", "")
            if src and _is_valid_product_image(src):
                if not src.startswith("http"):
                    src = f"https://www.dollartree.com{src}"
                return src

    return ""


def _is_valid_product_image(url: str) -> bool:
    """Check if an image URL looks like a product image."""
    url_lower = url.lower()
    for pattern in IMAGE_SKIP_PATTERNS:
        if pattern in url_lower:
            return False
    # Should have some product indicator
    return "/products/" in url_lower or "/product" in url_lower or "ccstore" in url_lower


def extract_category(product_block: dict[str, Any]) -> str:
    """Extract category from JSON-LD Product block (may be an array)."""
    cat = product_block.get("category")
    if not cat:
        return ""
    if isinstance(cat, list):
        return " > ".join(str(c) for c in cat if c)
    return str(cat)


def extract_brand(product_block: dict[str, Any]) -> str:
    """Extract brand from JSON-LD Product block."""
    brand = product_block.get("brand")
    if isinstance(brand, dict):
        name = brand.get("name")
        return str(name) if name and str(name).lower() not in ("null", "none") else ""
    if isinstance(brand, str) and brand.lower() not in ("null", "none", ""):
        return brand
    return ""


def extract_rating(jsonld: JSONLDData) -> str:
    """Extract rating from JSON-LD AggregateRating block."""
    if not jsonld.aggregate_rating:
        return ""
    val = jsonld.aggregate_rating.get("ratingValue")
    if val is not None:
        return str(val)
    return ""


def extract_review_count(jsonld: JSONLDData) -> str:
    """Extract review count from JSON-LD AggregateRating block."""
    if not jsonld.aggregate_rating:
        return ""
    count = jsonld.aggregate_rating.get("reviewCount")
    if count is not None:
        return str(count)
    return ""


def extract_rating_css_fallback(soup: BeautifulSoup) -> tuple[str, str]:
    """Extract rating and review count from BazaarVoice CSS fallback.

    aria-label example: 'average rating value is 5.0 of 5.'
    """
    rating = ""
    review_count = ""
    el = soup.select_one(".bv_main_rating_button[aria-label*='rating']")
    if el:
        aria = el.get("aria-label", "")
        # Extract rating value: "average rating value is 5.0 of 5."
        m = re.search(r"is\s+([\d.]+)\s+of", aria)
        if m:
            rating = m.group(1)

    return rating, review_count


# ---------------------------------------------------------------------------
# Main product extraction
# ---------------------------------------------------------------------------


def extract_product(
    url: str,
    session: requests.Session,
    product_id: int,
) -> Product:
    """Extract product data from a single DollarTree product page.

    Args:
        url: The product page URL.
        session: A requests.Session instance.
        product_id: Sequential ID for this product.

    Returns:
        A Product dataclass with extracted data.
    """
    product = Product(
        id=product_id,
        url=url,
        src_url=url,
        scraped_at=datetime.now(timezone.utc).isoformat(),
    )

    try:
        time.sleep(DELAY)
        response = session.get(url, timeout=20)
        product.status_code = response.status_code

        if response.status_code != 200:
            product.remarks = f"HTTP {response.status_code}"
            logger.warning("HTTP %d for %s", response.status_code, url)
            return product

        # Check if redirected to a different URL
        final_url = response.url
        response.encoding = response.apparent_encoding or "utf-8"
        html = response.text
        raw_html = html  # Keep for regex fallbacks

        soup = BeautifulSoup(html, "html.parser")
        jsonld = parse_all_jsonld(soup)

        # Debug logging for JSON-LD parsing
        if jsonld.product:
            logger.debug(
                "JSON-LD Product keys: %s",
                list(jsonld.product.keys()),
            )
            offers = jsonld.product.get("offers")
            logger.debug("JSON-LD offers raw type: %s, value: %s", type(offers), str(offers)[:200] if offers else "None")
        else:
            logger.warning("No JSON-LD Product block found for %s", url)

        # Soft 404 detection
        is_404, reason = detect_soft_404(
            soup, response.status_code, url, final_url, jsonld
        )
        if is_404:
            product.remarks = reason
            logger.info("Soft 404 detected for %s: %s", url, reason)
            return product

        # --- Extract fields ---
        warnings: list[str] = []

        # Title — from JSON-LD Product.name
        if jsonld.product:
            product.title = str(jsonld.product.get("name", "") or "")
        if not product.title:
            h1 = soup.find("h1")
            if h1:
                product.title = h1.get_text(strip=True)
        if not product.title:
            warnings.append("title missing")

        # SKU — from JSON-LD Product.sku (direct child)
        if jsonld.product:
            sku_val = jsonld.product.get("sku")
            if sku_val is not None:
                product.sku = str(sku_val)
        # Fallback: extract numeric ID from URL path
        if not product.sku:
            path_parts = urlparse(url).path.rstrip("/").split("/")
            if len(path_parts) >= 2:
                product.sku = path_parts[-1]

        # Description — from JSON-LD Product.description
        if jsonld.product:
            product.description = str(jsonld.product.get("description", "") or "")

        # Price — PRIMARY from meta tag (per-item price, e.g., 1.25)
        meta_price, meta_currency = extract_price_from_meta(soup, raw_html)
        product.price = format_price(meta_price)
        product.currency = meta_currency

        # Price fallback: CSS
        if not product.price:
            css_price = extract_price_from_css(soup)
            product.price = format_price(css_price)
            if css_price:
                warnings.append("price from CSS fallback")

        # Currency fallback: JSON-LD offers.priceCurrency
        if not product.currency and jsonld.product:
            offers = get_offers(jsonld.product)
            ld_currency = offers.get("priceCurrency")
            if ld_currency:
                product.currency = str(ld_currency)

        # Availability — from JSON-LD offers.availability
        if jsonld.product:
            offers = get_offers(jsonld.product)
            raw_avail = offers.get("availability")
            if raw_avail:
                product.availability = normalize_availability(str(raw_avail))
            else:
                # Debug: log available offers keys
                logger.debug(
                    "Availability missing. Offers keys: %s",
                    list(offers.keys()) if offers else "empty",
                )

        # If availability still empty, default to In Stock
        if not product.availability:
            product.availability = "In Stock"
            warnings.append("availability defaulted to In Stock")

        # Brand
        if jsonld.product:
            product.brand = extract_brand(jsonld.product)

        # Category
        if jsonld.product:
            product.category = extract_category(jsonld.product)

        # Image
        product.image = extract_image_url(soup, jsonld)

        # Rating — from JSON-LD AggregateRating (block with 'aggregateRating' key)
        product.rating = extract_rating(jsonld)
        if not product.rating:
            # CSS fallback from BazaarVoice
            css_rating, _ = extract_rating_css_fallback(soup)
            product.rating = css_rating
            if css_rating:
                warnings.append("rating from CSS fallback")

        # Review count — from JSON-LD AggregateRating
        product.review_count = extract_review_count(jsonld)

        # Original price (typically empty for DollarTree)
        product.original_price = ""

        # Currency default fallback
        if not product.currency:
            product.currency = "USD"
            warnings.append("currency defaulted to USD")

        # Warnings
        if warnings:
            existing = product.remarks
            product.remarks = f"{existing}; {'; '.join(warnings)}" if existing else "; ".join(warnings)

        logger.info(
            "Extracted: [%d] %s — %s (avail: %s, currency: %s)",
            product_id,
            product.title[:60],
            product.price,
            product.availability,
            product.currency,
        )

    except requests.Timeout:
        product.remarks = "Request timeout"
        logger.error("Timeout fetching %s", url)
    except requests.ConnectionError:
        product.remarks = "Connection error"
        logger.error("Connection error fetching %s", url)
    except Exception as e:
        product.remarks = f"Extraction error: {e}"
        logger.error("Error extracting %s: %s", url, e, exc_info=True)

    return product


# ---------------------------------------------------------------------------
# CLI arguments
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description=f"{SITE_NAME} product scraper (HTTP requests)"
    )
    parser.add_argument(
        "--input",
        type=str,
        default=INPUT_URLS_FILE,
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
        default=0,
        help="Max products to scrape (0 = no limit)",
    )
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        default=True,
        help="Do not use proxy (default for this site)",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Main entry point."""
    args = parse_args()
    start_time = time.time()

    # Determine input URLs
    urls: list[str] = []
    if args.urls:
        urls = list(args.urls)
        logger.info("Using %d URLs from CLI arguments", len(urls))
    else:
        input_path = args.input
        if not os.path.isabs(input_path):
            input_path = os.path.join(SCRIPT_DIR, input_path)
        try:
            with open(input_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                urls = data.get("urls", [])
            logger.info(
                "Loaded %d URLs from %s", len(urls), input_path
            )
        except FileNotFoundError:
            logger.error("Input file not found: %s", input_path)
            sys.exit(1)
        except json.JSONDecodeError as e:
            logger.error("Invalid JSON in input file: %s", e)
            sys.exit(1)

    if not urls:
        logger.error("No URLs to scrape. Exiting.")
        sys.exit(1)

    # Apply limits
    if args.sample:
        urls = urls[:5]
        logger.info("Sample mode: scraping first 5 products")
    elif args.limit > 0:
        urls = urls[: args.limit]
        logger.info("Limit mode: scraping up to %d products", args.limit)

    # --- Start scraping ---
    logger.info("=" * 80)
    logger.info("Starting scraper for %s", SITE_NAME)
    logger.info("Total products: %d", len(urls))
    logger.info("Scraping method: %s", SCRAPING_METHOD)
    logger.info("Rate limit delay: %.1fs", DELAY)
    logger.info("=" * 80)

    session = create_session()
    results: list[dict[str, Any]] = []
    failed_count = 0

    for idx, url in enumerate(urls, start=1):
        # Rotate user agent periodically
        if idx % 5 == 0:
            session.headers["User-Agent"] = random.choice(USER_AGENTS)

        product = extract_product(url, session, idx)
        results.append(product.__dict__)

        if not product.title and not product.price:
            failed_count += 1

        # Progress reporting
        if idx % 25 == 0 or idx == len(urls):
            pct = (idx / len(urls)) * 100
            logger.info(
                "Progress: [%d/%d] (%.1f%%)", idx, len(urls), pct
            )

    # --- Write output ---
    duration = round(time.time() - start_time, 2)
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
            "failed_products": failed_count,
            "rate_limit_delay": DELAY,
        },
    }

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    # --- Summary ---
    success = len(results) - failed_count
    logger.info("=" * 80)
    logger.info("EXTRACTION COMPLETE")
    logger.info("Total: %d, Success: %d, Failed: %d", len(results), success, failed_count)
    logger.info("Duration: %.2f seconds", duration)
    logger.info("Output: %s", output_file)
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
