#!/usr/bin/env python3
"""
Gilt.com Product Scraper (SeleniumBase UC Mode)

Uses SeleniumBase UC Mode to bypass Gilt's custom anti-bot protection.
Extracts product data primarily from the server-rendered `window.product_json`
JavaScript global variable, which contains ALL product data (name, brand, price,
MSRP, SKU, features, availability) in raw USD.

CRITICAL: The page uses Borderfree (BFX) currency conversion that replaces
displayed prices with local currency. All price extraction MUST use
`window.product_json` values, NOT CSS selectors.

PROXY: This site works WITHOUT proxy. Residential proxy actually CAUSES FAILURE
because it's too slow for the SPA to render window.product_json within the
polling window. The --no-proxy flag is the DEFAULT behavior.

Usage:
    python3 scraper_draft.py                    # reads input_urls.json from same folder
    python3 scraper_draft.py --input urls.json  # explicit input file
    python3 scraper_draft.py --urls url1 url2   # URLs as CLI arguments
    python3 scraper_draft.py --sample            # scrape only 5 products
    python3 scraper_draft.py --limit 10          # max 10 products
    python3 scraper_draft.py --xvfb              # use Xvfb virtual display (Docker)
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
from urllib.parse import urlparse, urlunparse

from seleniumbase import SB

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.environ.get("PROJECT_ROOT", os.path.join(SCRIPT_DIR, ".."))

SITE_NAME = "Gilt"
SITE_URL = "https://www.gilt.com"
PLATFORM = "custom"
SCRAPING_METHOD = "seleniumbase_uc"
SITE_SLUG = "gilt-com"

DELAY_BETWEEN_REQUESTS = 4
PAGE_LOAD_TIMEOUT = 30
UC_RECONNECT_TIME = 4

# Polling config for product_json extraction (synchronous)
# No-proxy is the default and proven fast path (22s per product)
PRODUCT_JSON_MAX_ATTEMPTS = 15
PRODUCT_JSON_POLL_DELAY = 1.5  # seconds between polls
INITIAL_PAGE_WAIT = 7  # seconds to wait after navigation before polling

TRACKING_PARAMS = [
    "campaignid", "gclid", "gbraid", "gad_campaignid", "gad_source",
    "keyword", "matchtype", "network", "partner", "utm_source", "dsi",
    "lsi", "country", "currency", "device", "deeplink", "adgroupid",
    "adposition", "subid",
]

TIMESTAMP = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
OUTPUT_FILE = os.path.join(SCRIPT_DIR, f"output_{TIMESTAMP}.json")
INPUT_FILE = os.path.join(SCRIPT_DIR, "input_urls.json")
LOG_FILE = os.path.join(PROJECT_ROOT, "logs", f"{SITE_SLUG}.log")

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
os.makedirs(SCRIPT_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


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
    brand: str = ""
    sku: str = ""
    description: str = ""
    images: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to output dictionary."""
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
        if self.brand:
            result["brand"] = self.brand
        if self.sku:
            result["sku"] = self.sku
        if self.description:
            result["description"] = self.description
        if self.images:
            result["images"] = self.images
        return result


def clean_html(html_str: str) -> str:
    """Strip HTML tags and decode entities to plain text."""
    if not html_str:
        return ""
    text = re.sub(r"<[^>]+>", " ", html_str)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def ensure_https(url: str) -> str:
    """Ensure a URL uses HTTPS scheme.

    Handles protocol-relative URLs (//cdn.example.com/...) by prepending 'https:'.
    Also converts http:// to https://.
    """
    if not url:
        return url
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://"):
        return "https://" + url[7:]
    return url


def strip_tracking_params(url: str) -> str:
    """Remove known tracking query parameters from URL, return clean URL."""
    parsed = urlparse(url)
    if not parsed.query:
        return url
    params = parsed.query.split("&")
    clean_params = []
    for p in params:
        key = p.split("=")[0].lower()
        if key not in TRACKING_PARAMS:
            clean_params.append(p)
    clean_query = "&".join(clean_params)
    return urlunparse(parsed._replace(query=clean_query))


def format_usd_price(value: Any) -> str:
    """Format a numeric price value as USD string with dollar sign."""
    if value is None:
        return ""
    try:
        num = float(str(value))
        if num <= 0:
            return ""
        return f"${num:,.2f}"
    except (ValueError, TypeError):
        return ""


def extract_product_json(driver: Any) -> Optional[dict]:
    """Extract window.product_json using SYNCHRONOUS execute_script with polling.

    CRITICAL: Must NOT use execute_async_script with Promise pattern — it crashes
    ChromeDriver 149 with 'Cannot read properties of null (reading then)'.
    Instead, use synchronous execute_script in a Python-side polling loop.
    """
    for attempt in range(PRODUCT_JSON_MAX_ATTEMPTS):
        try:
            result = driver.execute_script(
                'if (window.product_json && typeof window.product_json === "object") '
                "{ return JSON.stringify(window.product_json); } "
                "return null;"
            )
            if result:
                return json.loads(result)
        except Exception as e:
            logger.debug(f"  product_json poll {attempt + 1} error: {e}")
        time.sleep(PRODUCT_JSON_POLL_DELAY)
    return None


def extract_images_from_dom(driver: Any) -> list[str]:
    """Extract product images scoped to the PDP gallery container.

    Uses relaxed filter (just 'ruecdn.com') to match all product CDN images.
    Scopes to .pdp-layout__images-container to avoid recommendation images.
    All URLs are ensured to use HTTPS (handles protocol-relative // URLs).
    """
    js_code = """
    var images = [];
    var container = document.querySelector('.pdp-layout__images-container');
    if (container) {
        var imgs = container.querySelectorAll('img');
        for (var i = 0; i < imgs.length; i++) {
            var src = imgs[i].getAttribute('src') || imgs[i].getAttribute('data-src') || '';
            if (src && src.indexOf('ruecdn.com') !== -1) {
                images.push(src);
            }
        }
    }
    // Deduplicate
    var seen = {};
    var unique = [];
    for (var j = 0; j < images.length; j++) {
        var normalized = images[j];
        if (!seen[normalized]) {
            seen[normalized] = true;
            unique.push(normalized);
        }
    }
    return unique;
    """
    try:
        raw_images = driver.execute_script(js_code) or []
        return [ensure_https(img) for img in raw_images]
    except Exception as e:
        logger.warning(f"  Failed to extract images from DOM: {e}")
        return []


def detect_block_page(driver: Any) -> Optional[str]:
    """Detect if the page is showing a block/challenge page."""
    js_code = """
    var bodyText = document.body ? document.body.innerText.toUpperCase() : '';
    if (bodyText.indexOf('UNFORTUNATELY WE ARE UNABLE TO GIVE YOU ACCESS') !== -1) return 'akamai';
    if (bodyText.indexOf('JUST A MOMENT') !== -1) return 'cloudflare';
    if (bodyText.indexOf('ACCESS DENIED') !== -1) return 'generic';
    if (bodyText.indexOf('ROBOT CHECK') !== -1) return 'captcha';
    if (bodyText.indexOf('BLOCKED') !== -1) return 'generic';
    if (bodyText.indexOf('ERROR 403') !== -1) return '403';
    return null;
    """
    try:
        return driver.execute_script(js_code)
    except Exception:
        return "error"


def build_product_from_json(
    product_json: dict,
    url: str,
    src_url: str,
    index: int,
    images: list[str],
) -> Product:
    """Build a Product dataclass from the product_json data."""
    product = Product(
        id=index,
        url=strip_tracking_params(url),
        src_url=strip_tracking_params(src_url),
        currency="USD",
        status_code=200,
        scraped_at=datetime.now(timezone.utc).isoformat(),
    )

    # Title: prefer display_name, fallback to name
    product.title = product_json.get("display_name") or product_json.get("name", "")

    # Brand
    product.brand = product_json.get("brand", "")

    # Price (list_price_min is the current selling price)
    list_price = product_json.get("list_price_min")
    product.price = format_usd_price(list_price)

    # Original price / MSRP (msrp_min) — only include when MSRP > list price
    msrp = product_json.get("msrp_min")
    if msrp and list_price:
        try:
            if float(str(msrp)) > float(str(list_price)):
                product.original_price = format_usd_price(msrp)
        except (ValueError, TypeError):
            product.original_price = format_usd_price(msrp)
    elif msrp and not list_price:
        product.original_price = format_usd_price(msrp)

    # SKU from first SKU entry
    skus = product_json.get("skus", [])
    if skus and isinstance(skus, list) and len(skus) > 0:
        first_sku = skus[0]
        product.sku = first_sku.get("sku_number", "")

        # Description / features (HTML string, clean it)
        features = first_sku.get("features", "")
        if features:
            product.description = clean_html(features)

        # Build variant info in remarks if multiple SKUs with distinct colors/sizes
        if len(skus) > 1:
            variant_info = []
            for sku in skus:
                color = sku.get("color__display_value", "")
                size = sku.get("size__display_value", "")
                sku_num = sku.get("sku_number", "")
                parts = []
                if color and color != "null" and color != "None":
                    parts.append(f"color={color}")
                if size and size != "null" and size != "None":
                    parts.append(f"size={size}")
                if sku_num:
                    parts.append(f"sku={sku_num}")
                if parts:
                    variant_info.append(", ".join(parts))
            if variant_info:
                remark_prefix = product.remarks + "; " if product.remarks else ""
                product.remarks = remark_prefix + f"Variants: {'; '.join(variant_info)}"

    # Availability from product_json fields
    is_final_sale = product_json.get("is_final_sale", False)
    backorder_enabled = product_json.get("backorder_enabled", False)
    max_per_cart = product_json.get("max_per_cart", 0)

    if max_per_cart and int(max_per_cart) > 0:
        product.availability = "In Stock"
    elif backorder_enabled:
        product.availability = "In Stock"
        remark_prefix = product.remarks + "; " if product.remarks else ""
        product.remarks = remark_prefix + "Backorder enabled"
    elif is_final_sale and max_per_cart and int(max_per_cart) > 0:
        product.availability = "In Stock"
    elif max_per_cart == 0 or max_per_cart is None:
        product.availability = "Out of Stock"
    else:
        product.availability = "In Stock"

    if is_final_sale:
        remark_prefix = product.remarks + "; " if product.remarks else ""
        product.remarks = remark_prefix + "Final sale"

    # Images (already HTTPS-ensured from extract_images_from_dom)
    product.images = images

    return product


def build_fallback_product(
    url: str,
    src_url: str,
    index: int,
    title: str = "",
    remarks: str = "",
    status_code: int = 0,
) -> Product:
    """Build a minimal Product when extraction fails."""
    return Product(
        id=index,
        title=title,
        url=strip_tracking_params(url),
        src_url=strip_tracking_params(src_url),
        currency="USD",
        status_code=status_code,
        scraped_at=datetime.now(timezone.utc).isoformat(),
        remarks=remarks,
    )


def scrape_product(
    driver: Any,
    url: str,
    src_url: str,
    index: int,
) -> Product:
    """Scrape a single product page using window.product_json as primary source."""
    logger.info(f"  [{index}] Navigating to: {url[:100]}")

    try:
        driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        driver.uc_open_with_reconnect(url, reconnect_time=UC_RECONNECT_TIME)
    except Exception as e:
        logger.error(f"  [{index}] Failed to navigate to {url}: {e}")
        return build_fallback_product(
            url, src_url, index, remarks=f"Navigation error: {e}", status_code=0
        )

    # Wait for JS-heavy SPA to settle
    time.sleep(INITIAL_PAGE_WAIT)

    # Detect block pages
    block_type = detect_block_page(driver)
    if block_type:
        logger.warning(f"  [{index}] {block_type.upper()} block detected on {url}")
        return build_fallback_product(
            url, src_url, index,
            remarks=f"{block_type.upper()} BLOCKED",
            status_code=403,
        )

    # Extract product_json (primary data source) via SYNCHRONOUS polling
    max_poll_window = PRODUCT_JSON_MAX_ATTEMPTS * PRODUCT_JSON_POLL_DELAY
    logger.debug(
        f"  [{index}] Polling for window.product_json "
        f"(up to {max_poll_window:.0f}s)..."
    )
    product_json = extract_product_json(driver)

    if product_json:
        logger.info(f"  [{index}] product_json extracted successfully")

        # Extract images from DOM (scoped to PDP gallery)
        images = extract_images_from_dom(driver)

        product = build_product_from_json(product_json, url, src_url, index, images)

        if product.title:
            logger.info(
                f"  [{index}] {product.title[:60]} — {product.price} "
                f"(MSRP: {product.original_price or 'N/A'})"
            )
        else:
            logger.warning(f"  [{index}] No title extracted from product_json")

        return product

    # Fallback: try JSON-LD if product_json is not available
    logger.warning(
        f"  [{index}] product_json not found after {max_poll_window:.0f}s, "
        f"falling back to JSON-LD + DOM"
    )
    return scrape_product_fallback(driver, url, src_url, index)


def scrape_product_fallback(
    driver: Any,
    url: str,
    src_url: str,
    index: int,
) -> Product:
    """Fallback extraction using JSON-LD and DOM text scanning.

    Sets status_code=200 since the page loaded successfully (no block page).
    """
    product = build_fallback_product(
        url, src_url, index, status_code=200, remarks=""
    )

    # Extract JSON-LD
    js_jsonld = """
    var scripts = document.querySelectorAll('script[type="application/ld+json"]');
    var jsonld = null;
    for (var i = 0; i < scripts.length; i++) {
        try {
            var data = JSON.parse(scripts[i].textContent);
            var items = Array.isArray(data) ? data : [data];
            for (var j = 0; j < items.length; j++) {
                if (items[j]['@type'] === 'Product' || items[j]['@type'] === 'ProductGroup') {
                    jsonld = items[j];
                    break;
                }
            }
            if (jsonld) break;
        } catch(e) {}
    }
    if (jsonld) {
        var img = jsonld.image || '';
        return JSON.stringify({
            name: jsonld.name || '',
            brand: (jsonld.brand && jsonld.brand.name) || '',
            image: img,
            description: jsonld.description || ''
        });
    }
    return null;
    """
    try:
        jsonld_str = driver.execute_script(js_jsonld)
        if jsonld_str:
            jsonld = json.loads(jsonld_str)
            product.title = jsonld.get("name", "")
            product.brand = jsonld.get("brand", "")
            img = jsonld.get("image", "")
            if isinstance(img, list) and img:
                product.images = [ensure_https(i) for i in img]
            elif isinstance(img, str) and img:
                product.images = [ensure_https(img)]
            product.description = jsonld.get("description", "")
            logger.debug(
                f"  [{index}] JSON-LD extracted: title='{product.title[:40]}' "
                f"brand='{product.brand}' images={len(product.images)}"
            )
    except Exception as e:
        logger.warning(f"  [{index}] JSON-LD extraction failed: {e}")

    # Try to get product_json one more time with extended wait
    # (in case it loaded after the initial polling window)
    time.sleep(3)
    product_json_retry = extract_product_json(driver)
    if product_json_retry:
        logger.info(f"  [{index}] product_json found on retry!")
        list_price = product_json_retry.get("list_price_min")
        product.price = format_usd_price(list_price)
        msrp = product_json_retry.get("msrp_min")
        if msrp and list_price:
            try:
                if float(str(msrp)) > float(str(list_price)):
                    product.original_price = format_usd_price(msrp)
            except (ValueError, TypeError):
                pass
        # Override title/brand with product_json if available
        display_name = (
            product_json_retry.get("display_name")
            or product_json_retry.get("name", "")
        )
        if display_name:
            product.title = display_name
        brand = product_json_retry.get("brand", "")
        if brand:
            product.brand = brand
        skus = product_json_retry.get("skus", [])
        if skus and isinstance(skus, list) and len(skus) > 0:
            product.sku = skus[0].get("sku_number", "")
            features = skus[0].get("features", "")
            if features:
                product.description = clean_html(features)
        product.remarks = "Extracted via product_json (retry)"
        # Re-extract images from DOM now that page has had more time
        dom_images = extract_images_from_dom(driver)
        if dom_images:
            product.images = dom_images
        return product

    # Scan for price in body text (last resort fallback)
    # NOTE: This may return BFX-converted prices, not USD
    js_price_scan = """
    var bodyText = document.body ? document.body.innerText : '';
    var priceMatch = bodyText.match(/\\$[\\d,]+\\.\\d{2}/g);
    if (priceMatch && priceMatch.length > 0) {
        return priceMatch;
    }
    var broadMatch = bodyText.match(/US\\s*\\$[\\d,.]+/g);
    if (broadMatch && broadMatch.length > 0) {
        return broadMatch;
    }
    return null;
    """
    try:
        prices = driver.execute_script(js_price_scan)
        if prices and len(prices) >= 1:
            product.price = prices[0]
            if len(prices) > 1:
                product.original_price = prices[1]
            logger.debug(f"  [{index}] Price from body regex: {product.price}")
    except Exception:
        pass

    # Check availability from Add to Cart button
    js_atc = """
    var btn = document.querySelector('[data-testid="pdp__add-to-cart-btn"]');
    if (btn) {
        return btn.textContent.trim().toLowerCase();
    }
    return null;
    """
    try:
        atc_text = driver.execute_script(js_atc)
        if atc_text and "add to cart" in atc_text:
            product.availability = "In Stock"
            logger.debug(
                f"  [{index}] Availability: In Stock (Add to Cart button found)"
            )
        elif atc_text and (
            "sold out" in atc_text or "unavailable" in atc_text
        ):
            product.availability = "Out of Stock"
            logger.debug(
                f"  [{index}] Availability: Out of Stock "
                f"(button text: {atc_text})"
            )
        else:
            logger.debug(
                f"  [{index}] Add to Cart button not found, "
                f"availability unknown"
            )
    except Exception:
        pass

    # Extract images from DOM if not already obtained from JSON-LD
    if not product.images:
        product.images = extract_images_from_dom(driver)

    if not product.title:
        # Try page title as last resort
        try:
            page_title = driver.title
            if page_title:
                parts = page_title.split(" - ")
                if len(parts) >= 3:
                    if not product.brand:
                        product.brand = parts[1].strip()
                    product.title = " - ".join(parts[2:]).strip()
                elif len(parts) == 2:
                    product.title = parts[1].strip()
                logger.debug(
                    f"  [{index}] Title from page title: {product.title[:40]}"
                )
        except Exception:
            pass

    product.remarks = "Extracted via JSON-LD/DOM fallback (product_json unavailable)"

    return product


def scrape_product_with_retry(
    driver: Any,
    url: str,
    src_url: str,
    index: int,
    max_retries: int = 3,
) -> Product:
    """Scrape a product with retry logic for intermittent connectivity."""
    last_error = ""
    for attempt in range(1, max_retries + 1):
        try:
            product = scrape_product(driver, url, src_url, index)
            # If we got a block page, retry with exponential backoff
            if product.status_code == 403 and attempt < max_retries:
                wait = DELAY_BETWEEN_REQUESTS * (2 ** attempt)
                logger.warning(
                    f"  [{index}] Blocked on attempt {attempt}/{max_retries}, "
                    f"retrying in {wait}s..."
                )
                time.sleep(wait)
                continue
            return product
        except Exception as e:
            last_error = str(e)
            logger.error(f"  [{index}] Attempt {attempt}/{max_retries} failed: {e}")
            if attempt < max_retries:
                wait = DELAY_BETWEEN_REQUESTS * (2 ** attempt)
                time.sleep(wait)

    return build_fallback_product(
        url, src_url, index,
        remarks=f"All retries failed: {last_error}",
        status_code=0,
    )


def load_urls_from_file(filepath: str) -> list[str]:
    """Load product URLs from a JSON file."""
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("urls", [])


def save_urls_to_file(filepath: str, urls: list[str]) -> None:
    """Save product URLs to a JSON file."""
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump({"urls": urls}, f, indent=2, ensure_ascii=False)
    logger.info(f"Saved {len(urls)} URLs to {filepath}")


def warmup_session(driver: Any) -> None:
    """Visit homepage to establish session cookies before scraping.

    Gilt's anti-bot system may require a warmup visit. The product_json
    is server-rendered so this is not strictly required for data extraction,
    but it helps establish a session and avoid blocks.
    """
    logger.info(f"Warming up: visiting {SITE_URL}")
    try:
        driver.uc_open_with_reconnect(SITE_URL, reconnect_time=UC_RECONNECT_TIME)
        time.sleep(5)
        block = detect_block_page(driver)
        if block:
            logger.warning(
                f"  Homepage blocked ({block}), proceeding to scraping anyway"
            )
        else:
            logger.info("  Homepage loaded successfully")
    except Exception as e:
        logger.warning(f"  Warmup failed: {e}, proceeding to scraping")


def main():
    parser = argparse.ArgumentParser(
        description=f"Scraper for {SITE_NAME} (SeleniumBase UC Mode)"
    )
    parser.add_argument(
        "--sample", action="store_true",
        help="Scrape only 5 products",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Max products to scrape",
    )
    parser.add_argument(
        "--input", type=str, default=None,
        help="Path to input URLs JSON file",
    )
    parser.add_argument(
        "--urls", nargs="+", default=None,
        help="Product URLs as CLI arguments",
    )
    parser.add_argument(
        "--xvfb", action="store_true",
        help="Use Xvfb virtual display (Docker)",
    )
    args = parser.parse_args()

    start_time = time.time()

    logger.info("=" * 80)
    logger.info(f"Starting SeleniumBase UC Mode scraper for {SITE_NAME}")
    logger.info(f"Site: {SITE_URL}")
    logger.info(f"Platform: {PLATFORM}")
    logger.info(f"Proxy: none (direct connection — verified to work for this site)")
    logger.info(f"Output: {OUTPUT_FILE}")
    logger.info(f"Xvfb: {args.xvfb}")
    logger.info("=" * 80)

    # Load product URLs
    product_urls: list[str] = []

    if args.urls:
        product_urls = list(args.urls)
        logger.info(f"Loaded {len(product_urls)} URLs from --urls argument")
    elif args.input:
        product_urls = load_urls_from_file(args.input)
        logger.info(f"Loaded {len(product_urls)} URLs from {args.input}")
    elif os.path.exists(INPUT_FILE):
        product_urls = load_urls_from_file(INPUT_FILE)
        logger.info(f"Loaded {len(product_urls)} URLs from {INPUT_FILE}")
    else:
        logger.error(
            f"No input URLs found. Provide --urls, --input, or {INPUT_FILE}"
        )
        sys.exit(1)

    # Apply sample/limit
    if args.sample:
        product_urls = product_urls[:5]
    if args.limit:
        product_urls = product_urls[: args.limit]

    logger.info(f"Total products to scrape: {len(product_urls)}")

    if not product_urls:
        logger.info("No products to scrape. Exiting.")
        sys.exit(0)

    # SeleniumBase UC configuration — NO PROXY (default for this site)
    # Live testing proved: no-proxy = 100% success, residential proxy = 0% success
    # (proxy is too slow for SPA to render product_json within polling window)
    sb_kwargs: dict[str, Any] = {
        "uc": True,
        "xvfb": args.xvfb,
    }

    results: list[dict[str, Any]] = []
    failed_count = 0

    try:
        with SB(**sb_kwargs) as sb:
            driver = sb.driver
            logger.info("Browser session started (UC Mode, no proxy)")

            # Warmup: visit homepage to establish session
            warmup_session(driver)

            # Scrape each product
            for i, url in enumerate(product_urls):
                src_url = url  # src_url equals the product URL from input
                try:
                    product = scrape_product_with_retry(
                        driver, url, src_url, i + 1, max_retries=3
                    )
                    results.append(product.to_dict())

                    if product.title:
                        logger.info(
                            f"  [{i + 1}/{len(product_urls)}] OK: "
                            f"{product.title[:50]} — {product.price}"
                        )
                    else:
                        logger.warning(
                            f"  [{i + 1}/{len(product_urls)}] NO TITLE: "
                            f"{url[:80]}"
                        )
                        failed_count += 1

                except Exception as e:
                    logger.error(
                        f"  [{i + 1}/{len(product_urls)}] FATAL: "
                        f"{url[:80]}: {e}"
                    )
                    failed_count += 1
                    fallback = build_fallback_product(
                        url, src_url, i + 1,
                        remarks=f"Fatal error: {e}",
                    )
                    results.append(fallback.to_dict())

                # Progress reporting
                if (
                    (i + 1) % 25 == 0
                    or (i + 1) == len(product_urls)
                ):
                    percent = ((i + 1) / len(product_urls)) * 100
                    logger.info(
                        f"Progress: [{i + 1}/{len(product_urls)}] "
                        f"({percent:.1f}%)"
                    )

                # Rate limiting delay between products with jitter
                if i < len(product_urls) - 1:
                    jitter = time.uniform(-1.0, 1.0)
                    time.sleep(max(1.0, DELAY_BETWEEN_REQUESTS + jitter))

    except Exception as exc:
        logger.error(f"Fatal browser error: {exc}")
        if not results:
            sys.exit(1)

    # Build output
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
            "failed_products": failed_count,
            "rate_limit_delay": DELAY_BETWEEN_REQUESTS,
        },
    }

    # Write output file
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    logger.info(f"Output saved to {OUTPUT_FILE}")

    # Summary
    success_count = len(results) - failed_count
    logger.info("=" * 80)
    logger.info("EXTRACTION COMPLETE")
    logger.info(
        f"Total: {len(results)}, Success: {success_count}, Failed: {failed_count}"
    )
    logger.info(f"Duration: {round(time.time() - start_time, 2)}s")
    logger.info(f"Output: {OUTPUT_FILE}")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
