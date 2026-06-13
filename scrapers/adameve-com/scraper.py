#!/usr/bin/env python3
"""
Adam & Eve Product Scraper
===========================
Scrapes product data from https://www.adameve.com using HTTP requests.
No anti-bot protection detected. Pages are fully server-rendered.

Primary data source: JSON-LD with non-standard capitalized property names.
Secondary sources: OG meta tags, CSS selectors, serverSideEvents JS variable.

Usage:
    python3 scraper_draft.py
    python3 scraper_draft.py --sample
    python3 scraper_draft.py --limit 20
    python3 scraper_draft.py --urls "https://www.adameve.com/..." "https://www.adameve.com/..."
    python3 scraper_draft.py --input custom_urls.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SITE_NAME = "Adam & Eve"
SITE_URL = "https://www.adameve.com"
PLATFORM = "custom"
SCRAPING_METHOD = "http_requests"
DELAY = 2.0  # seconds between requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(SCRIPT_DIR, "..", "..", "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "adameve-com.log")

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
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class Product:
    """Represents a single product's extracted data."""

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
    sku: str = ""
    image_url: str = ""
    images: list[str] = field(default_factory=list)
    rating: str = ""
    review_count: str = ""
    category: str = ""
    breadcrumbs: list[str] = field(default_factory=list)
    variants: list[dict[str, Any]] = field(default_factory=list)
    color_variants: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
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
            "brand": self.brand,
            "description": self.description,
            "sku": self.sku,
            "image_url": self.image_url,
            "images": self.images,
            "rating": self.rating,
            "review_count": self.review_count,
            "category": self.category,
            "breadcrumbs": self.breadcrumbs,
            "variants": self.variants,
            "color_variants": self.color_variants,
        }


# ---------------------------------------------------------------------------
# Logging Setup
# ---------------------------------------------------------------------------

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
# JSON-LD Extraction
# ---------------------------------------------------------------------------

def extract_jsonld_blocks(html: str) -> list[dict[str, Any]]:
    """Extract all JSON-LD blocks from HTML.

    Adam & Eve has 2 blocks: BreadcrumbList (index 0) and Product (index 1).
    The Product JSON-LD may have concatenated JSON after the main object,
    so we parse from first '{' to matching last '}'.
    """
    blocks: list[dict[str, Any]] = []
    pattern = re.compile(
        r'<script\s+type=["\']application/ld\+json["\']\s*>(.*?)</script>',
        re.DOTALL | re.IGNORECASE,
    )
    for match in pattern.finditer(html):
        raw = match.group(1).strip()
        if not raw:
            continue
        # Find first '{' and match to its closing '}'
        start = raw.find("{")
        if start == -1:
            continue
        # Simple brace matching for the outermost object
        depth = 0
        end = -1
        for i in range(start, len(raw)):
            if raw[i] == "{":
                depth += 1
            elif raw[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end == -1:
            continue
        try:
            data = json.loads(raw[start:end])
            blocks.append(data)
        except json.JSONDecodeError:
            logger.debug(f"Failed to parse JSON-LD block starting at position {start}")
            continue
    return blocks


def find_product_jsonld(blocks: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Find the Product JSON-LD block from extracted blocks."""
    for block in blocks:
        if block.get("@type") == "Product":
            return block
    return None


def find_breadcrumb_jsonld(blocks: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Find the BreadcrumbList JSON-LD block from extracted blocks."""
    for block in blocks:
        if block.get("@type") == "BreadcrumbList":
            return block
    return None


def safe_get(data: dict[str, Any], *keys: str, default: str = "") -> str:
    """Safely navigate nested dict with capitalized keys. Returns string."""
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    if isinstance(current, str):
        return current
    if isinstance(current, (int, float)):
        return str(current)
    return default


# ---------------------------------------------------------------------------
# serverSideEvents Extraction
# ---------------------------------------------------------------------------

def extract_server_side_events(html: str) -> Optional[dict[str, Any]]:
    """Parse serverSideEvents variable from inline JavaScript.

    Pattern: var serverSideEvents = [{...}];
    Returns the first event object or None.
    """
    pattern = re.compile(
        r"var\s+serverSideEvents\s*=\s*\[(\{.*?\})\]\s*;",
        re.DOTALL,
    )
    match = pattern.search(html)
    if not match:
        return None
    try:
        events = json.loads(f"[{match.group(1)}]")
        if events and isinstance(events, list) and len(events) > 0:
            return events[0]
    except (json.JSONDecodeError, IndexError):
        logger.debug("Failed to parse serverSideEvents")
    return None


# ---------------------------------------------------------------------------
# Page Validation
# ---------------------------------------------------------------------------

def is_product_page(html: str, soup: BeautifulSoup) -> bool:
    """Validate that the fetched page is actually a product page.

    Some product URLs redirect to homepage (200 OK but homepage content).
    Checks:
    1. JSON-LD Product block exists
    2. phe.page.pageType is "Product"
    3. Canonical URL contains '.aspx'
    4. OG type is 'product'
    """
    # Check 1: JSON-LD Product block
    jsonld_blocks = extract_jsonld_blocks(html)
    if find_product_jsonld(jsonld_blocks):
        return True

    # Check 2: phe.page.pageType in inline JS
    if re.search(r'pageType\s*:\s*"Product"', html):
        return True

    # Check 3: canonical URL contains .aspx
    canonical_el = soup.select_one("link[rel='canonical']")
    if canonical_el and canonical_el.get("href", "").find(".aspx") != -1:
        return True

    # Check 4: og:type is 'product'
    og_type = soup.select_one("meta[property='og:type']")
    if og_type and og_type.get("content", "").strip().lower() == "product":
        return True

    return False


# ---------------------------------------------------------------------------
# Availability Normalization
# ---------------------------------------------------------------------------

def normalize_availability(raw: str) -> str:
    """Normalize availability values to standard 'In Stock' or 'Out of Stock'."""
    if not raw:
        return ""
    lower = raw.lower().strip()
    # Handle full URL like 'http://schema.org/InStock'
    if "instock" in lower and "out" not in lower:
        return "In Stock"
    if "outofstock" in lower or "out of stock" in lower:
        return "Out of Stock"
    if "preorder" in lower:
        return "Pre-Order"
    if "limited" in lower:
        return "Limited Availability"
    if lower in ("instock", "in stock", "in_stock"):
        return "In Stock"
    return raw


# ---------------------------------------------------------------------------
# Price Formatting
# ---------------------------------------------------------------------------

def format_price(value: Any, currency_symbol: str = "$") -> str:
    """Format a numeric price value as a string with currency symbol."""
    if not value:
        return ""
    # Strip any non-numeric characters first (handles corrupted text)
    cleaned = re.sub(r"[^\d.]", "", str(value).strip())
    if not cleaned:
        return str(value)
    try:
        num = float(cleaned)
        return f"{currency_symbol}{num:,.2f}"
    except (ValueError, TypeError):
        return str(value)


# ---------------------------------------------------------------------------
# Image URL Filtering
# ---------------------------------------------------------------------------

def is_valid_product_image(src: str) -> bool:
    """Check if an image URL is a valid product image (not brand/emoji/flag/icon)."""
    skip_patterns = [
        "/brand.assets/",
        "/emoji/",
        "/flags/",
        "/icon/",
        "/navigation/",
    ]
    lower = src.lower()
    if not src or src.startswith("data:"):
        return False
    for pattern in skip_patterns:
        if pattern in lower:
            return False
    # Must have some path segments and contain an identifier
    parts = urllib.parse.urlparse(src).path.split("/")
    if len(parts) < 4:
        return False
    return True


# ---------------------------------------------------------------------------
# Breadcrumb Extraction
# ---------------------------------------------------------------------------

def extract_breadcrumbs_from_jsonld(
    breadcrumb_data: Optional[dict[str, Any]],
) -> list[str]:
    """Extract breadcrumb names from BreadcrumbList JSON-LD."""
    if not breadcrumb_data:
        return []
    items = breadcrumb_data.get("itemListElement", [])
    names = []
    for item in items:
        name = item.get("name", "")
        if name:
            names.append(name)
    return names


def extract_breadcrumbs_from_css(soup: BeautifulSoup) -> list[str]:
    """Extract breadcrumb text from CSS selectors as fallback."""
    crumbs: list[str] = []
    elements = soup.select(".breadcrumb a, .breadcrumb span")
    for el in elements:
        text = el.get_text(strip=True)
        if text:
            crumbs.append(text)
    return crumbs


# ---------------------------------------------------------------------------
# Variant Extraction
# ---------------------------------------------------------------------------

def extract_variants(soup: BeautifulSoup) -> list[dict[str, str]]:
    """Extract product variants from the variant select dropdown.

    Format: {Color}/{Size} (${Price})
    First option 'Please make a selection' (empty value) is skipped.
    """
    variants: list[dict[str, str]] = []
    select = soup.select_one("select.product-variant-select")
    if not select:
        return variants
    for option in select.select("option"):
        value = option.get("value", "").strip()
        if not value:
            continue  # Skip 'Please make a selection'
        text = option.get_text(strip=True)
        # Parse price from text
        price_match = re.search(r"\$[\d,.]+", text)
        price = price_match.group(0) if price_match else ""
        # Parse color/size from text (remove price portion)
        label = re.sub(r"\s*\([^)]*\)\s*$", "", text).strip()
        label = re.sub(r"\s*\(\$[\d,.]+\)\s*$", "", label).strip()
        # Split color/size by last '/'
        parts = label.rsplit("/", 1)
        color = parts[0].strip() if len(parts) > 1 else ""
        size = parts[1].strip() if len(parts) > 1 else label
        variants.append({
            "value": value,
            "text": text,
            "color": color,
            "size": size,
            "price": price,
        })
    return variants


def extract_color_variants(
    sse: Optional[dict[str, Any]],
) -> list[str]:
    """Extract all possible colors from serverSideEvents."""
    if not sse:
        return []
    colors = sse.get("Colors", [])
    if isinstance(colors, list):
        return [str(c) for c in colors if c]
    return []


# ---------------------------------------------------------------------------
# Category Extraction
# ---------------------------------------------------------------------------

def extract_category_from_breadcrumbs(breadcrumbs: list[str]) -> str:
    """Build category path from breadcrumbs, skipping 'Home'."""
    parts = [b for b in breadcrumbs if b.lower() != "home"]
    return "/".join(parts)


def extract_category_from_sse(sse: Optional[dict[str, Any]]) -> str:
    """Extract category path from serverSideEvents."""
    if not sse:
        return ""
    try:
        category = sse["ecommerce"]["detail"]["products"][0].get("category", "")
        return str(category) if category else ""
    except (KeyError, IndexError, TypeError):
        return ""


# ---------------------------------------------------------------------------
# Image Gallery Extraction
# ---------------------------------------------------------------------------

def extract_gallery_images(soup: BeautifulSoup, html: str) -> list[str]:
    """Extract product gallery images using multiple selector strategies.

    The site has 'productPageRedesigned_new: true' which may affect the
    DOM structure. Try selectors in order of specificity.

    Deduplicates by URL and filters out data: URIs and non-product images.
    """
    images: list[str] = []

    # Strategy 1: Most specific - main carousel only (avoids thumbnail duplicates)
    selectors = [
        ".ae-main-product-carousel .glider-track .glider-slide img",
        ".ae-product-carousel .glider-track > .glider-slide img",
        ".ae-product-carousel .glider-slide img",
        # Broader fallbacks
        ".product-gallery img",
        "[data-auto-id='product-image'] img",
        "[data-testid*='gallery'] img",
    ]

    for selector in selectors:
        imgs = soup.select(selector)
        if imgs:
            for img in imgs:
                src = img.get("src") or img.get("data-src") or ""
                if src.startswith("//"):
                    src = "https:" + src
                if src and not src.startswith("data:") and is_valid_product_image(src):
                    images.append(src)
            if images:
                break  # Use the first selector that matches

    # Strategy 2: If still empty, try to extract from serverSideEvents
    if not images:
        sse = extract_server_side_events(html)
        if sse:
            sse_images = sse.get("Images", [])
            if isinstance(sse_images, list):
                for img_url in sse_images:
                    if isinstance(img_url, str) and is_valid_product_image(img_url):
                        images.append(img_url)

    # Strategy 3: Fallback to JSON-LD Image if still empty
    if not images:
        jsonld_blocks = extract_jsonld_blocks(html)
        product_jsonld = find_product_jsonld(jsonld_blocks)
        if product_jsonld:
            jsonld_img = safe_get(product_jsonld, "Image")
            if jsonld_img and is_valid_product_image(jsonld_img):
                images.append(jsonld_img)

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique_images: list[str] = []
    for img_url in images:
        # Normalize URL for dedup (remove size suffixes, trailing slashes)
        norm = img_url.rstrip("/")
        if norm not in seen:
            seen.add(norm)
            unique_images.append(img_url)

    return unique_images


# ---------------------------------------------------------------------------
# Main Extraction Logic
# ---------------------------------------------------------------------------

def fetch_page(url: str) -> tuple[Optional[str], int]:
    """Fetch a product page via HTTP GET. Returns (html, status_code)."""
    try:
        # Strip query parameters that might cause issues (tracking params)
        clean_url = url.split("?")[0] if "?" in url else url
        logger.debug(f"Fetching: {clean_url}")
        response = requests.get(
            clean_url,
            headers=HEADERS,
            timeout=20,
            allow_redirects=True,
        )
        return response.text, response.status_code
    except requests.RequestException as e:
        logger.error(f"Failed to fetch {url}: {e}")
        return None, 0


def extract_product(
    url: str,
    src_url: str,
    product_id: int,
) -> Product:
    """Extract all product data from a single product page URL."""

    product = Product(id=product_id, url=url, src_url=src_url)
    now = datetime.now(timezone.utc)
    product.scraped_at = now.isoformat()

    html, status_code = fetch_page(url)
    product.status_code = status_code

    if not html or status_code != 200:
        product.remarks = f"Failed to fetch page (status {status_code})"
        logger.warning(f"Failed to fetch {url} — status {status_code}")
        return product

    # Parse HTML
    soup = BeautifulSoup(html, "html.parser")

    # --- PAGE VALIDATION ---
    # Verify this is actually a product page, not a homepage redirect
    if not is_product_page(html, soup):
        product.remarks = (
            "Received non-product page (possibly discontinued/redirected). "
            "URL may be invalid or product removed."
        )
        product.title = ""
        product.price = ""
        logger.warning(
            f"  ⚠ Non-product page received for {url} "
            f"(status {status_code}, possibly discontinued)"
        )
        return product

    # --- Canonical URL ---
    # Only update url from canonical if it looks like a product URL (.aspx)
    canonical_el = soup.select_one("link[rel='canonical']")
    if canonical_el and canonical_el.get("href"):
        canonical_href = canonical_el["href"].strip()
        if canonical_href and ".aspx" in canonical_href:
            product.url = canonical_href
        # Otherwise keep original input URL (prevents homepage URL overwrite)

    # --- JSON-LD blocks ---
    jsonld_blocks = extract_jsonld_blocks(html)
    product_jsonld = find_product_jsonld(jsonld_blocks)
    breadcrumb_jsonld = find_breadcrumb_jsonld(jsonld_blocks)

    # --- serverSideEvents ---
    sse = extract_server_side_events(html)

    # --- Title ---
    # Primary: JSON-LD Product.Name (capital N)
    title = safe_get(product_jsonld, "Name") if product_jsonld else ""
    if not title:
        # Fallback: h1.item_title
        h1 = soup.select_one("h1.item_title")
        if h1:
            title = h1.get_text(strip=True)
    if not title:
        # Fallback: OG title (strip site suffix)
        og_title = soup.select_one("meta[property='og:title']")
        if og_title and og_title.get("content"):
            title = og_title["content"].strip()
            # Strip common suffixes
            for suffix in [" | Adam & Eve", " | Adam&Eve"]:
                if title.endswith(suffix):
                    title = title[: -len(suffix)].strip()
                    break
    product.title = title

    # --- Price ---
    # Primary: JSON-LD Product.Offers.Price (numeric)
    raw_price = safe_get(product_jsonld, "Offers", "Price") if product_jsonld else ""
    if not raw_price:
        # Fallback: OG meta
        og_price = soup.select_one("meta[property='og:price:amount']")
        if og_price and og_price.get("content"):
            raw_price = og_price["content"].strip()
    if not raw_price:
        # Fallback: CSS .ae-normal-price .ae-price--normal
        price_el = soup.select_one(".ae-normal-price .ae-price--normal")
        if price_el:
            raw_price = price_el.get_text(strip=True).replace("$", "").replace(",", "").strip()
    product.price = format_price(raw_price)

    # --- Original Price ---
    # Only if .ae-sale-price does NOT have class 'hide'
    sale_container = soup.select_one(".ae-sale-price")
    original_price = ""
    if sale_container and "hide" not in sale_container.get("class", []):
        was_el = sale_container.select_one(".ae-price--was")
        if was_el:
            was_text = was_el.get_text(strip=True)
            # CRITICAL FIX: Extract only the numeric price from potentially
            # corrupted text like '89.99Excluded from promotion.Save 58.49 (65%)'
            price_match = re.search(r"\d+\.?\d*", was_text)
            if price_match:
                original_price = format_price(price_match.group(0))
        # If sale is active, the "now" price should be the actual price
        now_el = sale_container.select_one(".ae-price--now")
        if now_el:
            now_text = now_el.get_text(strip=True)
            now_match = re.search(r"\d+\.?\d*", now_text)
            if now_match:
                product.price = format_price(now_match.group(0))

    # Fallback: og:price:standard_amount
    if not original_price:
        og_std_price = soup.select_one("meta[property='og:price:standard_amount']")
        if og_std_price and og_std_price.get("content"):
            std_price = og_std_price["content"].strip()
            # Only use as original_price if it differs from current price
            try:
                std_num = float(std_price)
                cur_num = float(product.price.replace("$", "").replace(",", "").strip())
                if std_num > cur_num > 0:
                    original_price = format_price(std_price)
            except (ValueError, TypeError):
                pass

    product.original_price = original_price

    # --- Currency ---
    currency = safe_get(product_jsonld, "Offers", "PriceCurrency") if product_jsonld else ""
    if not currency:
        og_currency = soup.select_one("meta[property='og:price:currency']")
        if og_currency and og_currency.get("content"):
            currency = og_currency["content"].strip()
    product.currency = currency

    # --- Availability ---
    raw_avail = safe_get(product_jsonld, "Offers", "Availability") if product_jsonld else ""
    if not raw_avail:
        og_avail = soup.select_one("meta[property='og:availability']")
        if og_avail and og_avail.get("content"):
            raw_avail = og_avail["content"].strip()
    if not raw_avail and sse:
        raw_avail = sse.get("dimension11", "")
    product.availability = normalize_availability(raw_avail)

    # --- Brand ---
    brand = safe_get(product_jsonld, "Brand", "Name") if product_jsonld else ""
    if not brand:
        og_brand = soup.select_one("meta[property='og:brand']")
        if og_brand and og_brand.get("content"):
            brand = og_brand["content"].strip()
    if not brand and sse:
        brand = str(sse.get("Brand", ""))
    product.brand = brand

    # --- Description ---
    description = safe_get(product_jsonld, "Description") if product_jsonld else ""
    if not description:
        desc_els = soup.select(".ae-accordion__content.ae-product-description")
        if desc_els:
            description = " ".join(el.get_text(strip=True) for el in desc_els)
    product.description = description

    # --- SKU ---
    sku = safe_get(product_jsonld, "Mpn") if product_jsonld else ""
    if not sku and sse:
        sku = str(sse.get("Sku", ""))
    product.sku = sku

    # --- Image URL (primary single image) ---
    # Use OG image (full resolution) over JSON-LD (250x250)
    image_url = ""
    og_image = soup.select_one("meta[property='og:image']")
    if og_image and og_image.get("content"):
        image_url = og_image["content"].strip()
    if not image_url:
        image_url = safe_get(product_jsonld, "Image") if product_jsonld else ""
    product.image_url = image_url

    # --- Images (gallery) ---
    product.images = extract_gallery_images(soup, html)

    # --- Rating ---
    # Prefer serverSideEvents (more precise, e.g., 4.25 vs 4.2)
    # CRITICAL FIX: Round to 1 decimal place to avoid floating-point errors
    rating = ""
    if sse:
        sse_rating = sse.get("dimension13")
        if sse_rating is not None:
            try:
                rounded = round(float(sse_rating), 1)
                rating = f"{rounded:.1f}"
            except (ValueError, TypeError):
                rating = str(sse_rating)
    if not rating:
        rating = safe_get(
            product_jsonld, "AggregateRating", "RatingValue"
        ) if product_jsonld else ""
    product.rating = rating

    # --- Review Count ---
    review_count = safe_get(
        product_jsonld, "AggregateRating", "RatingCount"
    ) if product_jsonld else ""
    if not review_count and sse:
        rc = sse.get("metric4")
        if rc is not None:
            review_count = str(int(rc))
    product.review_count = review_count

    # --- Breadcrumbs ---
    breadcrumbs = extract_breadcrumbs_from_jsonld(breadcrumb_jsonld)
    if not breadcrumbs:
        breadcrumbs = extract_breadcrumbs_from_css(soup)
    product.breadcrumbs = breadcrumbs

    # --- Category ---
    category = extract_category_from_sse(sse)
    if not category:
        category = extract_category_from_breadcrumbs(breadcrumbs)
    product.category = category

    # --- Variants ---
    product.variants = extract_variants(soup)
    product.color_variants = extract_color_variants(sse)

    return product


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_output(results: list[Product], start_time: float) -> str:
    """Write results to timestamped JSON file. Returns output path."""
    os.makedirs(SCRIPT_DIR, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    output_file = os.path.join(SCRIPT_DIR, f"output_{timestamp}.json")

    failed_count = sum(1 for p in results if not p.title and p.status_code != 200)

    output = {
        "site": {
            "name": SITE_NAME,
            "url": SITE_URL,
            "platform": PLATFORM,
            "scraping_method": SCRAPING_METHOD,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        },
        "products": [p.to_dict() for p in results],
        "metadata": {
            "scraping_duration_seconds": round(time.time() - start_time, 2),
            "failed_products": failed_count,
            "rate_limit_delay": DELAY,
        },
    }

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    logger.info(f"Output written to: {output_file}")
    return output_file


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=f"Scraper for {SITE_NAME} ({SITE_URL})",
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
        default=False,
        help="Scrape only the first 5 products",
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
        help="Disable proxy usage (default for this site)",
    )
    return parser.parse_args()


def load_urls(args: argparse.Namespace) -> list[str]:
    """Load product URLs from CLI args or input_urls.json."""
    if args.urls:
        return args.urls

    input_path = args.input or os.path.join(SCRIPT_DIR, "input_urls.json")
    if not os.path.exists(input_path):
        logger.error(f"Input file not found: {input_path}")
        sys.exit(1)

    try:
        with open(input_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        urls = data.get("urls", [])
        if not urls:
            logger.error(f"No URLs found in {input_path}")
            sys.exit(1)
        return urls
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Failed to read {input_path}: {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """Main entry point for the scraper."""
    args = parse_args()
    product_urls = load_urls(args)

    # Apply --sample or --limit
    if args.sample:
        product_urls = product_urls[:5]
        logger.info("Sample mode: scraping first 5 products")
    elif args.limit is not None:
        product_urls = product_urls[: args.limit]
        logger.info(f"Limit mode: scraping up to {args.limit} products")

    # Deduplicate URLs while preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for u in product_urls:
        normalized = u.split("?")[0]  # Strip query params for dedup
        if normalized not in seen:
            seen.add(normalized)
            deduped.append(u)
    product_urls = deduped

    logger.info("=" * 80)
    logger.info(f"Starting scraper for {SITE_NAME}")
    logger.info(f"Total products: {len(product_urls)}")
    logger.info(f"Delay: {DELAY}s between requests")
    logger.info("=" * 80)

    start_time = time.time()
    results: list[Product] = []
    success_count = 0
    failed_count = 0

    for idx, url in enumerate(product_urls, start=1):
        product_num = len(results) + 1
        logger.info(f"[{product_num}/{len(product_urls)}] Processing: {url}")

        try:
            product = extract_product(url, src_url=url, product_id=product_num)
            results.append(product)

            if product.title:
                success_count += 1
                logger.info(
                    f"  ✓ {product.title[:60]} | {product.price} | {product.availability}"
                )
            else:
                failed_count += 1
                remark = product.remarks if product.remarks else "No title extracted"
                logger.warning(
                    f"  ✗ {remark} (status {product.status_code})"
                )
        except Exception as e:
            failed_count += 1
            logger.error(f"  ✗ Exception processing {url}: {e}")
            # Create a placeholder product
            product = Product(
                id=product_num,
                url=url,
                src_url=url,
                status_code=0,
                scraped_at=datetime.now(timezone.utc).isoformat(),
                remarks=f"Exception: {str(e)}",
            )
            results.append(product)

        # Rate limiting
        if idx < len(product_urls):
            time.sleep(DELAY)

        # Progress reporting every 25 products
        if len(results) % 25 == 0:
            percent = (len(results) / len(product_urls)) * 100
            logger.info(
                f"Progress: [{len(results)}/{len(product_urls)}] ({percent:.1f}%)"
            )

    # Write output
    output_file = write_output(results, start_time)

    duration = round(time.time() - start_time, 2)
    logger.info("=" * 80)
    logger.info("EXTRACTION COMPLETE")
    logger.info(f"Total: {len(results)}, Success: {success_count}, Failed: {failed_count}")
    logger.info(f"Duration: {duration}s")
    logger.info(f"Output: {output_file}")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
