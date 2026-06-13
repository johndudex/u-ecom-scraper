#!/usr/bin/env python3
"""
Adidas Ireland Product Scraper (SeleniumBase UC Mode)

Uses SeleniumBase UC Mode (undetected-chromedriver wrapper) to bypass Akamai
Bot Manager protection. adidas.ie uses Next.js with __NEXT_DATA__ for product
state, and JSON-LD for structured product metadata.

Extraction strategy:
  - JSON-LD (script[type="application/ld+json"]) for: title, description, brand,
    sku (productGroupID), rating, reviews
  - __NEXT_DATA__ (script#__NEXT_DATA__) for: price, original_price, availability,
    images, sizes, colors (JSON-LD offers are empty on adidas.ie)
  - CSS fallbacks for any fields not found in structured data

Warmup: visit homepage → accept cookie wall → wait → scrape product pages.

Usage:
    python3 scraper_draft.py
    python3 scraper_draft.py --input custom_urls.json
    python3 scraper_draft.py --urls "https://www.adidas.ie/product/XYZ.html"
    python3 scraper_draft.py --sample
    python3 scraper_draft.py --limit 10
    python3 scraper_draft.py --xvfb
    python3 scraper_draft.py --no-proxy
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

from seleniumbase import SB

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.environ.get("PROJECT_ROOT", os.path.join(SCRIPT_DIR, ".."))
sys.path.insert(0, PROJECT_ROOT)

SITE_NAME = "Adidas Ireland"
SITE_URL = "https://www.adidas.ie"
PLATFORM = "custom"
SCRAPING_METHOD = "seleniumbase_uc"
SITE_SLUG = "adidas-ie"

DELAY_BETWEEN_REQUESTS = 4.0
PAGE_LOAD_TIMEOUT = 30
WARMUP_WAIT = 18
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
# Data model
# ---------------------------------------------------------------------------

BLOCKED_URL_PATTERNS = [
    "/brand.assets/",
    "/emoji/",
    "/flags/",
    "/icon/",
    "/navigation/",
]


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
    description: str = ""
    sku: str = ""
    brand: str = ""
    color: str = ""
    size: str = ""
    rating_value: str = ""
    review_count: str = ""
    images: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
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
        optional_fields = [
            "description", "sku", "brand", "color", "size",
            "rating_value", "review_count", "images",
        ]
        for f in optional_fields:
            val = getattr(self, f)
            if val:
                result[f] = val
        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def clean_html(html_str: str) -> str:
    """Strip HTML tags and HTML entities from a string."""
    if not html_str:
        return ""
    text = re.sub(r"<[^>]+>", " ", html_str)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def format_price_eur(price_value: Any) -> str:
    """Convert a raw price value to a formatted string with € symbol."""
    if price_value is None:
        return ""
    if isinstance(price_value, str):
        price_value = price_value.strip()
        if price_value:
            return price_value
        return ""
    try:
        p = float(price_value)
        return f"\u20ac{p:,.2f}"
    except (ValueError, TypeError):
        return ""


def is_product_image(src: str, product_code: str = "") -> bool:
    """Return True if the image src appears to be a real product gallery image."""
    if not src:
        return False
    for pat in BLOCKED_URL_PATTERNS:
        if pat in src:
            return False
    # Very small images are usually thumbnails/icons
    if src.endswith(".svg"):
        return False
    if "data:" in src:
        return False
    if product_code and product_code.lower() not in src.lower():
        # Allow images that don't contain the product code only if they
        # look like product images (have /images/ or similar path)
        if "/images/" not in src and "/assets/" not in src:
            return False
    return True


def normalize_availability(raw: str) -> str:
    """Normalise availability text to 'In Stock' or 'Out of Stock'."""
    if not raw:
        return ""
    lower = raw.lower().strip()
    in_stock_keywords = [
        "in stock", "available", "add to bag", "add to cart",
        "instore", "online", "buy now", "shop now",
    ]
    out_of_stock_keywords = [
        "out of stock", "sold out", "unavailable", "not available",
        "coming soon", "notify me",
    ]
    for kw in in_stock_keywords:
        if kw in lower:
            return "In Stock"
    for kw in out_of_stock_keywords:
        if kw in lower:
            return "Out of Stock"
    return raw.strip()


# ---------------------------------------------------------------------------
# JavaScript extraction snippets  (var-based — no arrow IIFEs!)
# ---------------------------------------------------------------------------

EXTRACT_JSONLD_JS = """
var scripts = document.querySelectorAll('script[type="application/ld+json"]');
var results = [];
for (var i = 0; i < scripts.length; i++) {
    try {
        var data = JSON.parse(scripts[i].textContent);
        var items = Array.isArray(data) ? data : [data];
        for (var j = 0; j < items.length; j++) {
            var type = items[j]['@type'];
            if (type === 'ProductGroup' || type === 'Product') {
                results.push(items[j]);
            }
        }
    } catch(e) {}
}
return results;
"""

EXTRACT_NEXT_DATA_JS = """
try {
    var el = document.getElementById('__NEXT_DATA__');
    if (el) {
        return JSON.parse(el.textContent);
    }
} catch(e) {}
return null;
"""

EXTRACT_PRICE_CSS_JS = """
var priceEl = document.querySelector('[data-auto-id="product-price"]') ||
              document.querySelector('[data-testid="product-price"]') ||
              document.querySelector('.gl-price__value');
var priceText = priceEl ? priceEl.textContent.trim() : '';

var origEl = document.querySelector('[data-auto-id="product-price-previous"]') ||
             document.querySelector('.gl-price__not-reduced') ||
             document.querySelector('[class*="previous-price"]');
var origText = origEl ? origEl.textContent.trim() : '';

return { price: priceText, original_price: origText };
"""

EXTRACT_AVAILABILITY_CSS_JS = """
var labelEl = document.querySelector('[data-auto-id="product-availability-label"]');
var labelText = labelEl ? labelEl.textContent.trim() : '';

var addBtn = document.querySelector('[data-auto-id="add-to-bag-button"]');
var inStock = addBtn ? true : false;

return { label: labelText, has_add_button: inStock };
"""

EXTRACT_COLOR_JS = """
var colorEl = document.querySelector('[data-auto-id="color-variation-label"]') ||
              document.querySelector('[data-auto-id="product-color"]');
return colorEl ? colorEl.textContent.trim() : '';
"""

EXTRACT_IMAGES_CSS_JS = """
var imgs = document.querySelectorAll('[data-auto-id="product-image"] img, .product-image___image');
var srcs = [];
for (var i = 0; i < imgs.length; i++) {
    var src = imgs[i].getAttribute('src') || imgs[i].getAttribute('data-src') || '';
    if (src && src.indexOf('data:') !== 0) {
        srcs.push(src);
    }
}
return srcs;
"""

DETECT_BLOCK_JS = """
var bodyText = document.body ? document.body.innerText.toUpperCase() : '';
if (bodyText.indexOf('UNFORTUNATELY WE ARE UNABLE TO GIVE YOU ACCESS') !== -1) return 'akamai';
if (bodyText.indexOf('JUST A MOMENT') !== -1) return 'cloudflare';
if (bodyText.indexOf('ACCESS DENIED') !== -1) return 'generic';
if (bodyText.indexOf('BLOCKED') !== -1 && bodyText.indexOf('REQUEST') !== -1) return 'generic';
return null;
"""


# ---------------------------------------------------------------------------
# Core extraction
# ---------------------------------------------------------------------------

def extract_product_data(driver: Any, url: str, src_url: str, index: int) -> Product:
    """Navigate to product URL and extract all available fields."""
    product = Product(
        id=index,
        url=url,
        src_url=src_url,
        currency="EUR",
        status_code=200,
        scraped_at=datetime.now(timezone.utc).isoformat(),
    )

    # Navigate with UC reconnect
    driver.uc_open_with_reconnect(url, reconnect_time=UC_RECONNECT_TIME)
    time.sleep(4)

    # Detect block page
    block_type = driver.execute_script(DETECT_BLOCK_JS)
    if block_type:
        product.status_code = 403
        product.remarks = f"{block_type.upper()} BLOCKED"
        logger.warning(f"  [{index}] {block_type.upper()} block detected on {url}")
        return product

    # ---- JSON-LD extraction ----
    jsonld_blocks = driver.execute_script(EXTRACT_JSONLD_JS) or []
    jsonld = None
    for block in jsonld_blocks:
        if block.get("@type") == "ProductGroup":
            jsonld = block
            break
    if not jsonld and jsonld_blocks:
        jsonld = jsonld_blocks[0]

    if jsonld:
        product.title = jsonld.get("name", "")
        product.description = clean_html(jsonld.get("description", ""))
        brand_obj = jsonld.get("brand")
        if isinstance(brand_obj, dict):
            product.brand = brand_obj.get("name", "adidas")
        elif isinstance(brand_obj, str):
            product.brand = brand_obj
        else:
            product.brand = "adidas"
        product.sku = jsonld.get("productGroupID", "")
        rating = jsonld.get("aggregateRating")
        if isinstance(rating, dict):
            product.rating_value = str(rating.get("ratingValue", ""))
            product.review_count = str(rating.get("reviewCount", ""))

        # JSON-LD offers are EMPTY on adidas.ie — skip them for price

    # ---- __NEXT_DATA__ extraction (primary for price/availability/images) ----
    next_data = driver.execute_script(EXTRACT_NEXT_DATA_JS)
    if next_data:
        try:
            product_store = (
                next_data.get("props", {})
                .get("pageProps", {})
                .get("initialState", {})
                .get("productStore", {})
            )
            products_dict = product_store.get("products", {})
            if products_dict:
                # Get first product entry
                product_key = next(iter(products_dict))
                product_data = products_dict[product_key]
                if isinstance(product_data, dict):
                    data = product_data.get("data", product_data)

                    # Title fallback from __NEXT_DATA__
                    if not product.title:
                        product.title = data.get("name", "")

                    # Price
                    price_info = data.get("priceInfo", {})
                    if price_info:
                        current = price_info.get("currentPrice", {})
                        if isinstance(current, dict):
                            raw_price = current.get("value", "")
                            product.price = format_price_eur(raw_price)
                        elif current:
                            product.price = format_price_eur(current)

                        previous = price_info.get("previousPrice", {})
                        if isinstance(previous, dict):
                            raw_prev = previous.get("value", "")
                            if raw_prev:
                                product.original_price = format_price_eur(raw_prev)
                        elif previous:
                            product.original_price = format_price_eur(previous)

                        # Also check strikethrough / sale price fields
                        if not product.original_price:
                            strikethrough = price_info.get("strikethroughPrice", {})
                            if isinstance(strikethrough, dict):
                                raw_strike = strikethrough.get("value", "")
                                if raw_strike:
                                    product.original_price = format_price_eur(raw_strike)

                    # Availability
                    availability_info = data.get("availability", {})
                    if isinstance(availability_info, dict):
                        avail_status = availability_info.get("status", "")
                        avail_label = availability_info.get("label", "")
                        product.availability = normalize_availability(
                            avail_label or avail_status
                        )

                    # Images
                    media = data.get("media", {})
                    if isinstance(media, dict):
                        all_images = []
                        # Typical structure: media.images or media.standard
                        for media_key in ("images", "standard", "gallery"):
                            media_list = media.get(media_key, [])
                            if isinstance(media_list, list):
                                for img_entry in media_list:
                                    img_url = ""
                                    if isinstance(img_entry, dict):
                                        img_url = img_entry.get("url", "") or img_entry.get(
                                            "src", ""
                                        )
                                        # Try different sizes, prefer large
                                        if not img_url:
                                            for sz in (
                                                "XXL",
                                                "XL",
                                                "LARGE",
                                                "L",
                                                "MEDIUM",
                                                "M",
                                                "SMALL",
                                            ):
                                                sz_obj = img_entry.get(sz, {})
                                                if isinstance(sz_obj, dict):
                                                    img_url = sz_obj.get("url", "")
                                                    if img_url:
                                                        break
                                    elif isinstance(img_entry, str):
                                        img_url = img_entry
                                    if img_url and img_url not in all_images:
                                        all_images.append(img_url)

                        # Also check variants for images
                        has_variants = jsonld.get("hasVariant", []) if jsonld else []
                        if has_variants and not all_images:
                            for v in has_variants:
                                if isinstance(v, dict):
                                    v_img = v.get("image", "")
                                    if isinstance(v_img, str) and v_img not in all_images:
                                        all_images.append(v_img)
                                    elif isinstance(v_img, list):
                                        for vi in v_img:
                                            if (
                                                isinstance(vi, str)
                                                and vi not in all_images
                                            ):
                                                all_images.append(vi)

                        product.images = all_images

                    # Color
                    product_colors = data.get("colorInfo", []) or data.get("colors", [])
                    if isinstance(product_colors, list) and product_colors:
                        color_names = []
                        for c in product_colors:
                            if isinstance(c, dict):
                                cn = c.get("name", "") or c.get("label", "")
                                if cn:
                                    color_names.append(cn)
                            elif isinstance(c, str):
                                color_names.append(c)
                        if color_names:
                            product.color = ", ".join(color_names[:5])

                    # Size from URL param or variants
                    if "forceSelSize" in url:
                        from urllib.parse import urlparse, parse_qs
                        qs = parse_qs(urlparse(url).query)
                        sizes = qs.get("forceSelSize", [])
                        if sizes:
                            product.size = sizes[0]

                    # SKU fallback
                    if not product.sku:
                        product.sku = data.get("id", "") or data.get("modelNumber", "")
        except Exception as e:
            logger.warning(f"  [{index}] Error parsing __NEXT_DATA__: {e}")
            product.remarks = f"__NEXT_DATA__ parse error: {e}"

    # ---- CSS fallbacks ----
    # Price
    if not product.price:
        css_price = driver.execute_script(EXTRACT_PRICE_CSS_JS) or {}
        product.price = css_price.get("price", "")
        product.original_price = css_price.get("original_price", "")

    # Availability
    if not product.availability:
        css_avail = driver.execute_script(EXTRACT_AVAILABILITY_CSS_JS) or {}
        if css_avail.get("label"):
            product.availability = normalize_availability(css_avail["label"])
        elif css_avail.get("has_add_button"):
            product.availability = "In Stock"

    # Title fallback
    if not product.title:
        product.title = driver.execute_script(
            "var el = document.querySelector('[data-auto-id=\"product-title\"]');"
            "return el ? el.textContent.trim() : '';"
        )

    # Color fallback
    if not product.color:
        product.color = driver.execute_script(EXTRACT_COLOR_JS) or ""

    # Images fallback
    if not product.images:
        css_images = driver.execute_script(EXTRACT_IMAGES_CSS_JS) or []
        product.images = css_images

    # Filter images — scope to product gallery only
    product_code = product.sku or ""
    filtered_images = []
    seen = set()
    for img_src in product.images:
        if img_src in seen:
            continue
        if is_product_image(img_src, product_code):
            filtered_images.append(img_src)
            seen.add(img_src)
    product.images = filtered_images[:15]  # cap at 15

    # Determine if still available based on multiple signals
    if not product.availability:
        has_add_btn = driver.execute_script(
            "return !!document.querySelector('[data-auto-id=\"add-to-bag-button\"]');"
        )
        product.availability = "In Stock" if has_add_btn else "Out of Stock"

    return product


# ---------------------------------------------------------------------------
# Warmup & cookie consent
# ---------------------------------------------------------------------------

def warmup_session(driver: Any) -> bool:
    """Visit homepage, wait for Akamai, accept cookie wall."""
    logger.info(f"Warming up session: visiting {SITE_URL}")
    driver.uc_open_with_reconnect(SITE_URL, reconnect_time=UC_RECONNECT_TIME)
    logger.info(f"Waiting {WARMUP_WAIT}s for Akamai sensor data collection...")
    time.sleep(WARMUP_WAIT)

    # Detect block
    block_type = driver.execute_script(DETECT_BLOCK_JS)
    if block_type:
        logger.error(f"{block_type.upper()} BLOCK DETECTED during warm-up")
        return False

    # Accept cookie consent (full-page overlay blocks rendering)
    clicked = driver.execute_script("""
    var btns = document.querySelectorAll("button[data-auto-id='accept-cookie-btn']");
    if (btns.length > 0) { btns[0].click(); return true; }
    var all = document.querySelectorAll('button');
    for (var i = 0; i < all.length; i++) {
        var t = all[i].textContent.trim().toLowerCase();
        if (t === 'accept' || t === 'accept all cookies' || t === 'accept all' ||
            t.indexOf('accept') !== -1) {
            all[i].click(); return true;
        }
    }
    return false;
    """)
    if clicked:
        logger.info("Accepted cookie consent")
        time.sleep(3)
    else:
        logger.warning("No cookie consent button found — may already be accepted")

    # Wait for Next.js hydration
    time.sleep(2)

    logger.info("Warm-up complete")
    return True


# ---------------------------------------------------------------------------
# URL loading
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=f"Scraper for {SITE_NAME} (SeleniumBase UC Mode)"
    )
    parser.add_argument("--sample", action="store_true", help="Scrape only 5 products")
    parser.add_argument(
        "--limit", type=int, default=None, help="Max products to scrape"
    )
    parser.add_argument(
        "--input", type=str, default=None, help="Path to input URLs JSON file"
    )
    parser.add_argument(
        "--urls", nargs="+", default=None, help="Product URLs as arguments"
    )
    parser.add_argument(
        "--no-proxy", action="store_true", default=True, help="Skip proxy (default)"
    )
    parser.add_argument(
        "--xvfb", action="store_true", help="Use Xvfb virtual display (Docker)"
    )
    args = parser.parse_args()

    start_time = time.time()

    logger.info("=" * 80)
    logger.info(f"Starting SeleniumBase UC Mode scraper for {SITE_NAME}")
    logger.info(f"Site: {SITE_URL}")
    logger.info(f"Output: {OUTPUT_FILE}")
    logger.info(f"Proxy: disabled (direct connection)")
    logger.info(f"Xvfb: {args.xvfb}")
    logger.info("=" * 80)

    # Load URLs
    product_urls: list[str] = []
    if args.urls:
        product_urls = list(args.urls)
    elif args.input:
        product_urls = load_urls_from_file(args.input)
    elif os.path.exists(INPUT_FILE):
        product_urls = load_urls_from_file(INPUT_FILE)
    else:
        logger.error("No input URLs found. Provide --urls, --input, or input_urls.json")
        sys.exit(1)

    if args.sample:
        product_urls = product_urls[:5]
    if args.limit:
        product_urls = product_urls[: args.limit]

    logger.info(f"Total products to scrape: {len(product_urls)}")

    # SeleniumBase configuration — no proxy
    sb_kwargs: dict[str, Any] = {
        "uc": True,
        "xvfb": args.xvfb,
        "locale_code": "en",
    }

    results: list[dict[str, Any]] = []
    failed = 0

    with SB(**sb_kwargs) as sb:
        driver = sb.driver

        try:
            # Warmup
            if not warmup_session(driver):
                logger.error("Warm-up failed — aborting")
                sys.exit(1)

            # Scrape each product
            for i, url in enumerate(product_urls):
                src_url = url  # src_url equals the product URL when from input
                try:
                    product = extract_product_data(driver, url, src_url, i + 1)
                    if product.title:
                        results.append(product.to_dict())
                        logger.info(
                            f"  [{i + 1}/{len(product_urls)}] "
                            f"{product.title[:60]} \u2014 {product.price}"
                        )
                    else:
                        logger.warning(f"  [{i + 1}/{len(product_urls)}] No title: {url}")
                        failed += 1
                        results.append(product.to_dict())

                except Exception as e:
                    logger.error(f"  [{i + 1}/{len(product_urls)}] Error: {url}: {e}")
                    failed += 1
                    err_product = Product(
                        id=i + 1,
                        url=url,
                        src_url=src_url,
                        currency="EUR",
                        status_code=0,
                        scraped_at=datetime.now(timezone.utc).isoformat(),
                        remarks=f"Extraction error: {e}",
                    )
                    results.append(err_product.to_dict())

                # Progress logging
                if (i + 1) % 25 == 0 or (i + 1) == len(product_urls):
                    pct = ((i + 1) / len(product_urls)) * 100
                    logger.info(
                        f"Progress: [{i + 1}/{len(product_urls)}] ({pct:.1f}%)"
                    )

                # Rate limit between products
                if i < len(product_urls) - 1:
                    time.sleep(DELAY_BETWEEN_REQUESTS)

        except Exception as exc:
            logger.error(f"Fatal error during scraping: {exc}")

    # Write output
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

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    logger.info("=" * 80)
    logger.info("EXTRACTION COMPLETE")
    logger.info(f"Total: {len(results)}, Success: {len(results) - failed}, Failed: {failed}")
    logger.info(f"Duration: {round(time.time() - start_time, 2)}s")
    logger.info(f"Output: {OUTPUT_FILE}")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
