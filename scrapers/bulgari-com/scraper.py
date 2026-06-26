#!/usr/bin/env python3
"""
Bulgari.com Product Scraper

Uses SeleniumBase UC Mode (undetected-chromedriver wrapper) to bypass browser
fingerprinting/TLS detection on Bulgari.com. No proxy is needed — direct
connection works with UC Chrome.

The scraper reads input_urls.json from its own directory by default.
Can also accept URLs via --urls or --input flags.

Usage:
    python3 scraper.py                        # reads input_urls.json from same folder
    python3 scraper.py --input urls.json      # explicit input file
    python3 scraper.py --urls url1 url2        # URLs as CLI arguments
    python3 scraper.py --sample                 # scrape only 5 products
    python3 scraper.py --limit 10              # scrape max 10 products
    python3 scraper.py --xvfb                  # use Xvfb virtual display (Docker)
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

from seleniumbase import SB
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.environ.get("PROJECT_ROOT", os.path.join(SCRIPT_DIR, ".."))

SITE_NAME = "Bulgari"
SITE_URL = "https://www.bulgari.com"
PLATFORM = "custom"
SCRAPING_METHOD = "seleniumbase_uc"
SITE_SLUG = "bulgari-com"

DELAY_BETWEEN_REQUESTS = 3.0
PAGE_LOAD_TIMEOUT = 30
WARMUP_WAIT = 15
UC_RECONNECT_TIME = 4

TIMESTAMP = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
OUTPUT_FILE = os.path.join(SCRIPT_DIR, f"output_{TIMESTAMP}.json")
INPUT_FILE = os.path.join(SCRIPT_DIR, "input_urls.json")
LOG_FILE = os.path.join(PROJECT_ROOT, "logs", f"{SITE_SLUG}.log")

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


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------
@dataclass
class Product:
    """Represents a single product extracted from a Bulgari product page."""

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
    description: str = ""
    images: list[str] = field(default_factory=list)
    brand: str = ""
    sku: str = ""
    category: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert product to dictionary, omitting empty optional fields."""
        result: dict[str, Any] = {
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
        for optional_key in ("description", "images", "brand", "sku", "category"):
            val = getattr(self, optional_key)
            if val:
                result[optional_key] = val
        return result


# ---------------------------------------------------------------------------
# Utility Functions
# ---------------------------------------------------------------------------
def clean_html(html_str: str) -> str:
    """Remove HTML tags and decode entities from a string."""
    if not html_str:
        return ""
    text = re.sub(r"<[^>]+>", " ", html_str)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_sku_from_url(url: str) -> str:
    """Extract numeric product code from URL as SKU fallback."""
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    parts = path.split("/")
    for part in reversed(parts):
        if part.isdigit():
            return part
    # Look for last segment dash-separated numeric portion
    for part in reversed(parts):
        match = re.search(r"(\d+)$", part)
        if match:
            return match.group(1)
    return ""


def detect_currency_from_price(price_text: str) -> str:
    """Infer currency code from price text symbols."""
    if not price_text:
        return ""
    if "$" in price_text:
        return "USD"
    if "€" in price_text:
        return "EUR"
    if "£" in price_text:
        return "GBP"
    if "¥" in price_text:
        return "JPY"
    if "CHF" in price_text:
        return "CHF"
    if "CNY" in price_text or "¥" in price_text:
        return "CNY"
    return "USD"  # default for en-us locale


def format_price_with_symbol(raw_price: str, currency: str) -> str:
    """Ensure price includes appropriate currency symbol."""
    if not raw_price:
        return ""
    raw_price = raw_price.strip()
    # Already has a symbol
    if re.match(r"^[\$€£¥]", raw_price):
        return raw_price
    # Add symbol based on currency
    symbol_map = {
        "USD": "$",
        "EUR": "€",
        "GBP": "£",
        "JPY": "¥",
        "CHF": "CHF ",
        "CNY": "¥",
    }
    symbol = symbol_map.get(currency, "")
    return f"{symbol}{raw_price}"


# ---------------------------------------------------------------------------
# JSON-LD Extraction from HTML Source
# ---------------------------------------------------------------------------
def extract_jsonld_from_html(page_source: str) -> list[dict[str, Any]]:
    """
    Parse page source HTML to find all JSON-LD script blocks.
    Uses regex + json.loads instead of driver.execute_script to avoid
    SyntaxError issues with CDP evaluation on Bulgari.com.
    """
    jsonld_blocks: list[dict[str, Any]] = []
    pattern = r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>'
    matches = re.findall(pattern, page_source, re.DOTALL | re.IGNORECASE)
    for match in matches:
        try:
            data = json.loads(match.strip())
            if isinstance(data, list):
                jsonld_blocks.extend(data)
            else:
                jsonld_blocks.append(data)
        except (json.JSONDecodeError, TypeError):
            continue
    return jsonld_blocks


def find_jsonld_product(jsonld_blocks: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Find the Product or ProductGroup type from JSON-LD blocks."""
    for block in jsonld_blocks:
        atype = block.get("@type", "")
        if isinstance(atype, str) and atype in ("Product", "ProductGroup"):
            return block
        if isinstance(atype, list) and "Product" in atype:
            return block
    return None


def find_jsonld_breadcrumbs(jsonld_blocks: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Find BreadcrumbList from JSON-LD blocks."""
    for block in jsonld_blocks:
        atype = block.get("@type", "")
        if isinstance(atype, str) and atype == "BreadcrumbList":
            return block
    return None


def extract_breadcrumb_category(breadcrumb: Optional[dict[str, Any]]) -> str:
    """Extract category path from BreadcrumbList JSON-LD."""
    if not breadcrumb:
        return ""
    items = breadcrumb.get("itemListElement", [])
    categories = []
    for item in items:
        name = item.get("name", "")
        if name:
            categories.append(name)
    return " > ".join(categories)


# ---------------------------------------------------------------------------
# Soft 404 Detection
# ---------------------------------------------------------------------------
def is_404_page(page_title: str, page_source: str) -> bool:
    """Detect soft 404 pages by checking title and body text."""
    title_upper = page_title.upper()
    if "CANNOT BE FOUND" in title_upper or "PAGE NOT FOUND" in title_upper:
        return True
    if "NO LONGER AVAILABLE" in title_upper or "UNAVAILABLE" in title_upper:
        return True
    # Check body text for common 404 indicators
    body_match = re.search(r"<body[^>]*>(.*?)</body>", page_source, re.DOTALL | re.IGNORECASE)
    if body_match:
        body_text = re.sub(r"<[^>]+>", " ", body_match.group(1)).upper()
        for phrase in ("CANNOT BE FOUND", "PAGE NOT FOUND", "NO LONGER AVAILABLE",
                        "DISCONTINUED", "PRODUCT NOT FOUND", "ITEM NOT FOUND"):
            if phrase in body_text:
                return True
    return False


# ---------------------------------------------------------------------------
# Block Detection
# ---------------------------------------------------------------------------
DETECT_BLOCK_JS = """
var bodyText = document.body ? document.body.innerText.toUpperCase() : '';
if (bodyText.indexOf('UNFORTUNATELY WE ARE UNABLE TO GIVE YOU ACCESS') !== -1) return 'akamai';
if (bodyText.indexOf('JUST A MOMENT') !== -1) return 'cloudflare';
if (bodyText.indexOf('ACCESS DENIED') !== -1) return 'generic';
if (bodyText.indexOf('VERIFY YOU ARE HUMAN') !== -1) return 'captcha';
return null;
"""


# ---------------------------------------------------------------------------
# Cookie Consent
# ---------------------------------------------------------------------------
ACCEPT_COOKIES_JS = """
// Try known cookie consent button selectors for LVMH/Bulgari sites
var selectors = [
    "button[data-auto-id='accept-cookie-btn']",
    "button[data-testid='cookie-accept']",
    "#onetrust-accept-btn-handler",
    ".cookie-accept",
    "button[class*='cookie' i][class*='accept' i]",
    "button[class*='consent' i][class*='accept' i]"
];
for (var i = 0; i < selectors.length; i++) {
    try {
        var btn = document.querySelector(selectors[i]);
        if (btn) { btn.click(); return true; }
    } catch(e) {}
}
// Fallback: look for any button with accept text
var allBtns = document.querySelectorAll('button, a[role="button"]');
for (var j = 0; j < allBtns.length; j++) {
    var t = allBtns[j].textContent.trim().toLowerCase();
    if (t === 'accept' || t === 'accept all cookies' || t === 'accept all' ||
        t === 'agree' || t === 'agree all' || t === 'continue' ||
        t === 'ok' || t === 'got it') {
        allBtns[j].click();
        return true;
    }
}
return false;
"""


# ---------------------------------------------------------------------------
# Wait for Product Page Load
# ---------------------------------------------------------------------------
WAIT_FOR_PRODUCT_JS = """
// Wait for product page to load - check for h1 or product data-testid elements
var titleEl = document.querySelector('h1');
var testIdEl = document.querySelector('[data-testid*="product"]');
var jsonldEl = document.querySelector('script[type="application/ld+json"]');
return {
    has_h1: !!titleEl,
    has_testid: !!testIdEl,
    has_jsonld: !!jsonldEl,
    title_text: titleEl ? titleEl.textContent.trim() : '',
    body_length: document.body ? document.body.innerText.length : 0
};
"""


# ---------------------------------------------------------------------------
# Warmup
# ---------------------------------------------------------------------------
def warmup_session(driver: Any) -> bool:
    """
    Warm up the browser session by visiting the homepage first and accepting
    any cookie consent banner. This establishes session cookies and lets
    anti-bot sensors collect baseline data.
    """
    logger.info(f"Warming up session: visiting {SITE_URL}")
    driver.uc_open_with_reconnect(SITE_URL, reconnect_time=UC_RECONNECT_TIME)
    logger.info(f"Waiting {WARMUP_WAIT}s for anti-bot sensors...")
    time.sleep(WARMUP_WAIT)

    # Check for block page
    block_type = driver.execute_script(DETECT_BLOCK_JS)
    if block_type:
        logger.error(f"{block_type.upper()} BLOCK DETECTED during warm-up")
        return False

    # Accept cookie consent
    try:
        clicked = driver.execute_script(ACCEPT_COOKIES_JS)
        if clicked:
            logger.info("Accepted cookies")
            time.sleep(2)
    except Exception as e:
        logger.warning(f"Cookie accept error (non-fatal): {e}")

    # Verify page loaded
    page_state = driver.execute_script(WAIT_FOR_PRODUCT_JS)
    logger.info(
        f"Warm-up page state: h1={page_state.get('has_h1')}, "
        f"testid={page_state.get('has_testid')}, "
        f"jsonld={page_state.get('has_jsonld')}, "
        f"body_len={page_state.get('body_length', 0)}"
    )

    logger.info("Warm-up complete")
    return True


# ---------------------------------------------------------------------------
# Extract Product Data from Page
# ---------------------------------------------------------------------------
def extract_product_from_page(
    driver: Any,
    url: str,
    src_url: str,
    index: int,
) -> Product:
    """
    Navigate to a product URL and extract all available data.
    Uses a hybrid approach: JSON-LD from page source + CSS selectors from DOM.
    """
    product = Product(
        id=index,
        url=url,
        src_url=src_url,
        scraped_at=datetime.now(timezone.utc).isoformat(),
        brand="BULGARI",
    )

    # Navigate
    try:
        driver.uc_open_with_reconnect(url, reconnect_time=UC_RECONNECT_TIME)
    except Exception as e:
        logger.error(f"  [{index}] Navigation failed for {url}: {e}")
        product.remarks = f"Navigation error: {e}"
        product.status_code = 0
        return product

    # Wait for page to render
    time.sleep(5)

    # Check for block page
    try:
        block_type = driver.execute_script(DETECT_BLOCK_JS)
        if block_type:
            logger.warning(f"  [{index}] {block_type.upper()} block detected on {url}")
            product.status_code = 403
            product.remarks = f"{block_type.upper()} BLOCKED"
            return product
    except Exception:
        pass

    # Get final URL after redirects
    try:
        final_url = driver.current_url
        product.url = final_url
    except Exception:
        final_url = url

    # Get page source for JSON-LD parsing
    try:
        page_source = driver.page_source
    except Exception as e:
        logger.error(f"  [{index}] Failed to get page source: {e}")
        product.remarks = f"Failed to get page source: {e}"
        product.status_code = 200
        return product

    # Get page title
    page_title = ""
    try:
        page_title = driver.execute_script("return document.title || '';")
    except Exception:
        pass

    # Soft 404 check
    if is_404_page(page_title, page_source):
        logger.warning(f"  [{index}] Soft 404 detected: {url} (title: {page_title})")
        product.remarks = f"Soft 404: product not found (title: {page_title})"
        product.status_code = 200
        return product

    # URL redirect check
    if final_url != url:
        # Check if redirected to homepage or search page
        parsed_final = urlparse(final_url)
        parsed_orig = urlparse(url)
        if parsed_final.path != parsed_orig.path:
            # Significant redirect — might be a 404 redirect
            if parsed_final.path in ("/", "/en-us", "/en-us/"):
                logger.warning(
                    f"  [{index}] Redirected to homepage — likely 404: {url} -> {final_url}"
                )
                product.remarks = f"Soft 404: redirected to homepage from {url}"
                product.status_code = 200
                return product

    product.status_code = 200

    # --- JSON-LD Extraction (from page source, not execute_script) ---
    jsonld_blocks = extract_jsonld_from_html(page_source)
    jsonld_product = find_jsonld_product(jsonld_blocks)
    jsonld_breadcrumb = find_jsonld_breadcrumbs(jsonld_blocks)

    if jsonld_product:
        logger.info(f"  [{index}] JSON-LD Product found")
        # Title
        if not product.title:
            product.title = jsonld_product.get("name", "")

        # Brand
        brand_data = jsonld_product.get("brand", {})
        if isinstance(brand_data, dict):
            product.brand = brand_data.get("name", "BULGARI") or "BULGARI"

        # SKU
        if not product.sku:
            product.sku = jsonld_product.get("sku", "")

        # Description
        if not product.description:
            product.description = jsonld_product.get("description", "")

        # Images
        img_data = jsonld_product.get("image", [])
        if isinstance(img_data, str):
            img_data = [img_data]
        if img_data and not product.images:
            product.images = [img for img in img_data if isinstance(img, str)]

        # Offers (price, availability, currency)
        offers = jsonld_product.get("offers")
        if offers:
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if isinstance(offers, dict):
                # Price
                offer_price = offers.get("price", "")
                if offer_price and offer_price != "{}":
                    try:
                        price_num = float(offer_price)
                        product.currency = offers.get("priceCurrency", "USD") or "USD"
                        product.price = format_price_with_symbol(
                            f"{price_num:,.2f}", product.currency
                        )
                    except (ValueError, TypeError):
                        product.price = offer_price

                # Currency fallback
                if not product.currency:
                    product.currency = offers.get("priceCurrency", "")

                # Availability
                availability = offers.get("availability", "")
                if isinstance(availability, str):
                    if "InStock" in availability:
                        product.availability = "In Stock"
                    elif "OutOfStock" in availability or "SoldOut" in availability:
                        product.availability = "Out of Stock"
                    elif "PreOrder" in availability:
                        product.availability = "In Stock"
                    else:
                        product.availability = availability

                # High price for original_price
                high_price = offers.get("highPrice", "")
                if high_price:
                    try:
                        product.original_price = format_price_with_symbol(
                            f"{float(high_price):,.2f}", product.currency or "USD"
                        )
                    except (ValueError, TypeError):
                        pass
    else:
        logger.info(f"  [{index}] No JSON-LD Product found, relying on CSS selectors")

    # --- Category from breadcrumbs ---
    if not product.category:
        product.category = extract_breadcrumb_category(jsonld_breadcrumb)

    # --- DOM CSS Selector Extraction (fallback / primary for price) ---
    try:
        # Title fallback
        if not product.title:
            title_el = driver.execute_script("""
            var selectors = [
                'h1',
                '[data-testid*="product-name"]',
                '[data-testid*="productName"]',
                '[class*="product-title"]',
                '.product-detail h1',
                '.product-name',
                '[class*="productName"]'
            ];
            for (var i = 0; i < selectors.length; i++) {
                var el = document.querySelector(selectors[i]);
                if (el && el.textContent.trim()) return el.textContent.trim();
            }
            return '';
            """)
            if title_el:
                product.title = title_el

        # Price — primary from DOM (luxury brands often have empty JSON-LD offers)
        if not product.price:
            price_text = driver.execute_script("""
            var selectors = [
                '[data-testid*="price"]',
                '.product-price',
                '.price-value',
                '[class*="price"]',
                '[class*="Price"]',
                '[class*="product-price"]',
                '[data-auto-id="price"]'
            ];
            for (var i = 0; i < selectors.length; i++) {
                var els = document.querySelectorAll(selectors[i]);
                for (var j = 0; j < els.length; j++) {
                    var t = els[j].textContent.trim();
                    if (t && t.match(/[\\d]/)) return t;
                }
            }
            return '';
            """)
            if price_text:
                product.price = price_text.strip()
                if not product.currency:
                    product.currency = detect_currency_from_price(product.price)

        # Original price
        if not product.original_price:
            orig_text = driver.execute_script("""
            var selectors = [
                '[class*="compare"]',
                '[class*="original"]',
                '.was-price',
                '[class*="strike"]',
                '[class*="Strikethrough"]',
                '[data-testid*="original-price"]',
                's', 'del'
            ];
            for (var i = 0; i < selectors.length; i++) {
                var els = document.querySelectorAll(selectors[i]);
                for (var j = 0; j < els.length; j++) {
                    var t = els[j].textContent.trim();
                    if (t && t.match(/[\\d]/)) return t;
                }
            }
            return '';
            """)
            if orig_text:
                product.original_price = orig_text.strip()
                if not product.currency:
                    product.currency = detect_currency_from_price(product.original_price)

        # Availability from DOM
        if not product.availability:
            avail_text = driver.execute_script("""
            var selectors = [
                '[data-testid*="availability"]',
                '[class*="availability"]',
                '.stock-status',
                '[class*="stock"]',
                '[class*="add-to-bag"]',
                '[class*="addToBag"]',
                '[class*="add-to-cart"]',
                '[data-auto-id="add-to-bag"]'
            ];
            for (var i = 0; i < selectors.length; i++) {
                var el = document.querySelector(selectors[i]);
                if (el) {
                    var t = el.textContent.trim().toLowerCase();
                    if (t.indexOf('add to') !== -1 || t.indexOf('available') !== -1 ||
                        t.indexOf('in stock') !== -1 || t.indexOf('shop now') !== -1 ||
                        t.indexOf('buy now') !== -1) return 'In Stock';
                    if (t.indexOf('out of stock') !== -1 || t.indexOf('sold out') !== -1 ||
                        t.indexOf('unavailable') !== -1) return 'Out of Stock';
                    if (t) return t;
                }
            }
            return '';
            """)
            if avail_text:
                if avail_text in ("In Stock", "Out of Stock"):
                    product.availability = avail_text
                else:
                    avail_lower = avail_text.lower()
                    if any(kw in avail_lower for kw in ("add to", "available", "shop now", "buy now")):
                        product.availability = "In Stock"
                    elif any(kw in avail_lower for kw in ("out of", "sold out", "unavailable")):
                        product.availability = "Out of Stock"
                    else:
                        product.availability = avail_text

        # Description from DOM
        if not product.description:
            desc_text = driver.execute_script("""
            var selectors = [
                '[data-testid*="description"]',
                '.product-description',
                '[class*="description"]',
                '[class*="product-details"]',
                '[class*="productDescription"]'
            ];
            for (var i = 0; i < selectors.length; i++) {
                var el = document.querySelector(selectors[i]);
                if (el && el.textContent.trim().length > 10) return el.textContent.trim();
            }
            return '';
            """)
            if desc_text:
                product.description = desc_text

        # SKU from DOM
        if not product.sku:
            sku_text = driver.execute_script("""
            var selectors = [
                '[data-testid*="sku"]',
                '.sku',
                '[class*="sku"]',
                '[class*="product-code"]',
                '[class*="productCode"]',
                '[data-auto-id="sku"]'
            ];
            for (var i = 0; i < selectors.length; i++) {
                var el = document.querySelector(selectors[i]);
                if (el) {
                    var t = el.textContent.trim();
                    if (t) return t;
                }
            }
            return '';
            """)
            if sku_text:
                product.sku = sku_text

        # Category from DOM breadcrumbs
        if not product.category:
            cat_text = driver.execute_script("""
            var selectors = [
                '.breadcrumb',
                'nav[aria-label="breadcrumb"]',
                '[class*="breadcrumb"]',
                '[data-auto-id="breadcrumb"]'
            ];
            for (var i = 0; i < selectors.length; i++) {
                var el = document.querySelector(selectors[i]);
                if (el) {
                    var items = el.querySelectorAll('a, span, li');
                    var parts = [];
                    for (var j = 0; j < items.length; j++) {
                        var t = items[j].textContent.trim();
                        if (t) parts.push(t);
                    }
                    return parts.join(' > ');
                }
            }
            return '';
            """)
            if cat_text:
                product.category = cat_text

        # Images from DOM (scoped to product gallery only)
        if not product.images:
            dom_images = driver.execute_script("""
            var gallerySelectors = [
                '[data-testid*="gallery"] img',
                '[data-testid*="image"] img',
                '[class*="gallery"] img',
                '[class*="carousel"] img',
                '[class*="product-image"] img',
                '[class*="productImage"] img',
                '.product-image img',
                'img[class*="product"]'
            ];
            var seen = {};
            var imgs = [];
            for (var s = 0; s < gallerySelectors.length; s++) {
                var els = document.querySelectorAll(gallerySelectors[s]);
                for (var i = 0; i < els.length; i++) {
                    var src = els[i].getAttribute('src') || els[i].getAttribute('data-src') || '';
                    if (src && !seen[src]) {
                        // Skip non-product images
                        if (src.indexOf('/brand.assets/') !== -1) continue;
                        if (src.indexOf('/emoji/') !== -1) continue;
                        if (src.indexOf('/flags/') !== -1) continue;
                        if (src.indexOf('/icon/') !== -1) continue;
                        if (src.indexOf('/navigation/') !== -1) continue;
                        if (src.indexOf('data:image') === 0) continue;
                        // Skip tiny images (icons, badges)
                        var w = els[i].naturalWidth || 0;
                        var h = els[i].naturalHeight || 0;
                        if (w > 0 && w < 50) continue;
                        if (h > 0 && h < 50) continue;
                        seen[src] = true;
                        imgs.push(src);
                    }
                }
                if (imgs.length >= 3) break;
            }
            return imgs;
            """)
            if dom_images:
                product.images = dom_images

    except Exception as e:
        logger.warning(f"  [{index}] DOM extraction error: {e}")

    # --- SKU fallback from URL ---
    if not product.sku:
        product.sku = extract_sku_from_url(final_url)

    # --- Currency fallback ---
    if not product.currency:
        product.currency = "USD"  # en-us locale default

    # --- Ensure price has currency symbol ---
    if product.price:
        product.price = format_price_with_symbol(product.price, product.currency)
    if product.original_price:
        product.original_price = format_price_with_symbol(product.original_price, product.currency)

    # --- Availability fallback ---
    if not product.availability:
        product.availability = ""  # leave empty if truly unknown

    return product


# ---------------------------------------------------------------------------
# URL Loading
# ---------------------------------------------------------------------------
def load_urls_from_file(filepath: str) -> list[str]:
    """Load URLs from a JSON file with 'urls' key."""
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("urls", [])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description=f"Scraper for {SITE_NAME} (SeleniumBase UC Mode)")
    parser.add_argument("--sample", action="store_true", help="Scrape only 5 products")
    parser.add_argument("--limit", type=int, default=None, help="Max products to scrape")
    parser.add_argument("--input", type=str, default=None, help="Path to input URLs JSON file")
    parser.add_argument("--urls", nargs="+", default=None, help="Product URLs as arguments")
    parser.add_argument("--no-proxy", action="store_true", default=True, help="Skip proxy (default)")
    parser.add_argument("--xvfb", action="store_true", help="Use Xvfb virtual display (Docker)")
    args = parser.parse_args()

    start_time = time.time()

    logger.info("=" * 80)
    logger.info(f"Starting scraper for {SITE_NAME}")
    logger.info(f"Site: {SITE_URL}")
    logger.info(f"Platform: {PLATFORM}")
    logger.info(f"Scraping method: {SCRAPING_METHOD}")
    logger.info(f"Output: {OUTPUT_FILE}")
    logger.info(f"Xvfb: {args.xvfb}")
    logger.info("=" * 80)

    # --- Load URLs ---
    product_urls: list[str] = []

    if args.urls:
        product_urls = args.urls
    elif args.input:
        product_urls = load_urls_from_file(args.input)
    elif os.path.exists(INPUT_FILE):
        product_urls = load_urls_from_file(INPUT_FILE)
    else:
        logger.error("No input URLs found. Use --urls, --input, or place input_urls.json in script directory.")
        sys.exit(1)

    if args.sample:
        product_urls = product_urls[:5]
    if args.limit:
        product_urls = product_urls[: args.limit]

    logger.info(f"Total products to scrape: {len(product_urls)}")

    if not product_urls:
        logger.error("No URLs to scrape after applying filters.")
        sys.exit(1)

    # --- SeleniumBase UC Mode ---
    sb_kwargs: dict[str, Any] = {
        "uc": True,
        "xvfb": args.xvfb,
    }

    results: list[dict[str, Any]] = []
    failed = 0

    with SB(**sb_kwargs) as sb:
        driver = sb.driver
        driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)

        try:
            # Warmup session
            if not warmup_session(driver):
                logger.error("Warm-up failed. Cannot proceed.")
                sys.exit(1)

            # --- Scrape each product URL ---
            for i, url in enumerate(product_urls):
                logger.info(f"  [{i + 1}/{len(product_urls)}] Scraping: {url}")
                try:
                    product = extract_product_from_page(driver, url, url, i + 1)

                    if product.title:
                        results.append(product.to_dict())
                        logger.info(
                            f"  [{i + 1}/{len(product_urls)}] ✓ {product.title[:60]} "
                            f"— {product.price} | {product.availability or 'N/A'}"
                        )
                    elif product.remarks and "404" in product.remarks:
                        logger.warning(f"  [{i + 1}/{len(product_urls)}] {product.remarks}")
                        results.append(product.to_dict())
                        failed += 1
                    else:
                        logger.warning(
                            f"  [{i + 1}/{len(product_urls)}] No title extracted: {url}"
                        )
                        failed += 1
                        results.append(product.to_dict())

                except Exception as e:
                    logger.error(f"  [{i + 1}/{len(product_urls)}] Error: {url}: {e}")
                    failed += 1
                    error_product = Product(
                        id=i + 1,
                        url=url,
                        src_url=url,
                        status_code=0,
                        scraped_at=datetime.now(timezone.utc).isoformat(),
                        remarks=f"Extraction error: {e}",
                    )
                    results.append(error_product.to_dict())

                # Progress reporting
                if (i + 1) % 25 == 0:
                    percent = ((i + 1) / len(product_urls)) * 100
                    logger.info(f"Progress: [{i + 1}/{len(product_urls)}] ({percent:.1f}%)")

                # Rate limiting between requests
                if i < len(product_urls) - 1:
                    time.sleep(DELAY_BETWEEN_REQUESTS)

        except Exception as exc:
            logger.error(f"Fatal error during scraping: {exc}")

    # --- Write output ---
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
            "scraping_duration_seconds": round(time.time() - start_time, 2),
            "failed_products": failed,
            "rate_limit_delay": DELAY_BETWEEN_REQUESTS,
        },
    }

    os.makedirs(os.path.dirname(OUTPUT_FILE) or ".", exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    success = len(results) - failed
    logger.info("=" * 80)
    logger.info("EXTRACTION COMPLETE")
    logger.info(f"Total: {len(results)}, Success: {success}, Failed: {failed}")
    logger.info(f"Duration: {round(time.time() - start_time, 2)}s")
    logger.info(f"Output: {OUTPUT_FILE}")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
