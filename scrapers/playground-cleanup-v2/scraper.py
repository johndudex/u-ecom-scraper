#!/usr/bin/env python3
"""
Calvin Klein UK — SeleniumBase UC Mode Scraper (Per-Page Session Architecture)

Calvin Klein UK uses session-cookie gating: the homepage must be visited first
to establish session cookies. Without them, all product/category pages return
"oops!" error pages. The site also uses anti-bot session detection.

This scraper uses a PER-PAGE SESSION architecture: each product page gets its
own fresh SB() session with homepage warmup, preventing anti-bot session
detection from cascading across products.

Extraction: JSON-LD primary, DOM CSS fallback.

Usage:
    python3 scraper.py                           # reads input_urls.json
    python3 scraper.py --input urls.json         # explicit input file
    python3 scraper.py --urls "https://..." "https://..."  # CLI URLs
    python3 scraper.py --sample                  # scrape only 5 products
    python3 scraper.py --limit 20                # max products to scrape
    python3 scraper.py --no-proxy                # skip proxy (default for this site)
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
from typing import Any

from seleniumbase import SB

# ─── Paths & Constants ─────────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.environ.get("PROJECT_ROOT", os.path.join(SCRIPT_DIR, ".."))
sys.path.insert(0, PROJECT_ROOT)

SITE_NAME = "Calvin Klein UK"
SITE_URL = "https://www.calvinklein.co.uk"
PLATFORM = "sfcc"
SCRAPING_METHOD = "seleniumbase_uc"
SITE_SLUG = "calvinklein-co-uk"
CURRENCY = "GBP"

DELAY_BETWEEN_REQUESTS = 3.0
WARMUP_WAIT = 20
UC_RECONNECT_TIME = 4
MAX_CONSECUTIVE_ERRORS = 3
PRODUCT_ID_RE = re.compile(r"/p/(\w+)")

TIMESTAMP = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
OUTPUT_FILE = os.path.join(SCRIPT_DIR, f"output_{TIMESTAMP}.json")
INPUT_FILE = os.path.join(SCRIPT_DIR, "input_urls.json")
LOG_FILE = os.path.join(os.path.dirname(SCRIPT_DIR.rstrip("/")), "logs", f"{SITE_SLUG}.log")

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

# ─── Currency formatting ───────────────────────────────────────────────────────

CURRENCY_SYMBOLS = {
    "GBP": "£",
    "USD": "$",
    "EUR": "€",
    "CAD": "C$",
    "AUD": "A$",
    "NZD": "NZ$",
}


def format_price(raw_price: str, currency_code: str) -> str:
    """Format a clean numeric price string with currency symbol."""
    if not raw_price:
        return ""
    prefix = CURRENCY_SYMBOLS.get(currency_code, f"{currency_code} ")
    try:
        price_num = float(raw_price)
        return f"{prefix}{price_num:,.2f}"
    except (ValueError, TypeError):
        return f"{prefix}{raw_price}"


# ─── Data class ───────────────────────────────────────────────────────────────

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
    description: str = ""
    images: list[str] = field(default_factory=list)
    brand: str = ""
    sku: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to output-compatible dict."""
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
        if self.description:
            result["description"] = self.description
        if self.images:
            result["images"] = self.images
        if self.brand:
            result["brand"] = self.brand
        if self.sku:
            result["sku"] = self.sku
        return result


# ─── JavaScript extraction snippets ─────────────────────────────────────────────

DETECT_OOPS_JS = """
var title = document.title || '';
var body = document.body ? document.body.innerText.substring(0, 500).toUpperCase() : '';
if (title.toLowerCase().indexOf('oops!') !== -1) return 'oops_page';
if (body.indexOf('PRODUCT NOT FOUND') !== -1) return 'not_found';
if (body.indexOf('NO LONGER AVAILABLE') !== -1) return 'no_longer_available';
if (body.indexOf('UNAVAILABLE') !== -1) return 'unavailable';
return null;
"""

ACCEPT_COOKIES_JS = """
var btns = document.querySelectorAll("button[data-auto-id='accept-cookie-btn']");
if (btns.length > 0) { btns[0].click(); return true; }
var all = document.querySelectorAll('button');
for (var i = 0; i < all.length; i++) {
    var t = all[i].textContent.trim().toLowerCase();
    if (t === 'accept' || t === 'accept all cookies' || t === 'accept all'
        || t === 'accept cookies' || t === 'agree') {
        all[i].click(); return true;
    }
}
var links = document.querySelectorAll('a');
for (var i = 0; i < links.length; i++) {
    var t = links[i].textContent.trim().toLowerCase();
    if (t === 'accept all cookies' || t === 'accept cookies') {
        links[i].click(); return true;
    }
}
return false;
"""

EXTRACT_JSONLD_JS = """
var scripts = document.querySelectorAll('script[type="application/ld+json"]');
var allBlocks = [];
for (var i = 0; i < scripts.length; i++) {
    try {
        var data = JSON.parse(scripts[i].textContent);
        if (Array.isArray(data)) {
            for (var j = 0; j < data.length; j++) {
                allBlocks.push(data[j]);
            }
        } else {
            allBlocks.push(data);
        }
    } catch(e) {}
}
return allBlocks;
"""

EXTRACT_PRODUCT_FROM_JSONLD_JS = """
var blocks = arguments[0];
var product = null;
var productWithOffers = null;
var productWithRatings = null;

for (var i = 0; i < blocks.length; i++) {
    var b = blocks[i];
    if (b['@type'] === 'Product' || b['@type'] === 'ProductGroup') {
        product = b;
        if (b.offers) {
            productWithOffers = b;
        }
        if (b.aggregateRating) {
            productWithRatings = b;
        }
    }
}

var result = {
    title: '',
    price: '',
    original_price: '',
    currency: '',
    availability: '',
    description: '',
    sku: '',
    brand: '',
    images: [],
    rating: '',
    review_count: '',
    category: ''
};

var src = productWithOffers || product;
if (!src) return result;

result.title = src.name || '';
result.description = src.description || '';
result.sku = src.sku || src.mpn || '';

var brandName = '';
if (src.brand) {
    brandName = (typeof src.brand === 'object') ? (src.brand.name || '') : src.brand;
}
result.brand = brandName;

if (src.image) {
    if (typeof src.image === 'string') {
        result.images = [src.image];
    } else if (Array.isArray(src.image)) {
        result.images = src.image;
    }
}

if (src.offers) {
    var offers = Array.isArray(src.offers) ? src.offers[0] : src.offers;
    result.price = offers.price || '';
    var highPrice = offers.highPrice || '';
    if (highPrice && result.price && parseFloat(highPrice) > parseFloat(result.price)) {
        result.original_price = highPrice;
    }
    result.currency = offers.priceCurrency || '';
    var avail = offers.availability || '';
    if (avail.indexOf('InStock') !== -1) {
        result.availability = 'In Stock';
    } else if (avail.indexOf('OutOfStock') !== -1) {
        result.availability = 'Out of Stock';
    } else if (avail) {
        result.availability = avail.split('/').pop();
    }
}

if (productWithRatings && productWithRatings !== productWithOffers) {
    var agg = productWithRatings.aggregateRating;
    if (agg) {
        result.rating = agg.ratingValue || '';
        result.review_count = agg.reviewCount || '';
    }
}

return result;
"""

EXTRACT_DOM_FALLBACK_JS = """
var result = {
    title: '',
    price: '',
    original_price: '',
    availability: '',
    description: '',
    sku: '',
    images: [],
    category: ''
};

var titleEl = document.querySelector('h1.product-name, h1[class*="product"], h1[data-product-name], h1');
if (titleEl) result.title = titleEl.textContent.trim();

var priceEl = document.querySelector('div.price span.sales span.value, [class*="price"] .sales, [data-price], .product-price .value');
if (priceEl) {
    var contentAttr = priceEl.getAttribute('content');
    result.price = contentAttr || priceEl.textContent.trim();
}

var origEl = document.querySelector('div.price span.standard span.value, [class*="price"] .standard, [class*="price"] .strike-through, .product-price .list');
if (origEl) {
    var origContent = origEl.getAttribute('content');
    result.original_price = origContent || origEl.textContent.trim();
}

var availEl = document.querySelector('[class*="availability"], [data-in-stock], .stock-status');
if (availEl) result.availability = availEl.textContent.trim();

var descEl = document.querySelector('[class*="description"], [class*="product-detail"] .detail, .product-description');
if (descEl) result.description = descEl.textContent.trim();

var pidEl = document.querySelector('[data-pid]');
if (pidEl) result.sku = pidEl.getAttribute('data-pid') || pidEl.textContent.trim();

var imgEls = document.querySelectorAll('[class*="product-image"] img, .product-primary-image img, [class*="carousel"] img');
for (var i = 0; i < imgEls.length; i++) {
    var src = imgEls[i].src || imgEls[i].getAttribute('data-src');
    if (src) result.images.push(src);
}

var crumbEls = document.querySelectorAll('[class*="breadcrumb"] a, nav[aria-label="breadcrumb"] a');
var cats = [];
for (var i = 0; i < crumbEls.length; i++) {
    var t = crumbEls[i].textContent.trim();
    if (t) cats.push(t);
}
result.category = cats.join(' > ');

return result;
"""

# ─── Session management ───────────────────────────────────────────────────────

SESSION_KWARGS = {
    "uc": True,
    "xvfb": True,
    "locale_code": "en-gb",
}


def _make_sb_kwargs(extra: dict | None = None) -> dict[str, Any]:
    """Build SB() constructor kwargs, merging any extras."""
    kwargs = dict(SESSION_KWARGS)
    if extra:
        kwargs.update(extra)
    return kwargs


def warmup_session(driver) -> bool:
    """Visit homepage to establish session cookies and accept consent banners.

    Calvin Klein UK uses session-cookie gating: the homepage sets session
    cookies required for all subsequent page access. Without visiting the
    homepage first, all product/category pages return 'oops!' error pages.

    This MUST be called at the start of each per-page session.
    """
    logger.info(f"Warming up session: visiting {SITE_URL}")
    try:
        driver.uc_open_with_reconnect(SITE_URL, reconnect_time=UC_RECONNECT_TIME)
        time.sleep(3)
    except Exception as e:
        logger.error(f"Warmup navigation failed: {e}")
        return False

    # Check for error page on homepage itself
    error_type = driver.execute_script(DETECT_OOPS_JS)
    if error_type:
        logger.error(f"Error page on homepage: {error_type}")
        return False

    logger.info(f"Waiting {WARMUP_WAIT}s for session cookies and anti-bot sensors...")
    time.sleep(WARMUP_WAIT)

    # Accept cookie consent banners
    try:
        clicked = driver.execute_script(ACCEPT_COOKIES_JS)
        if clicked:
            logger.info("Accepted cookie consent banner")
            time.sleep(2)
    except Exception as e:
        logger.debug(f"Cookie consent handling: {e}")

    logger.info("Warm-up complete — session cookies established")
    return True


# ─── Product extraction ────────────────────────────────────────────────────────

def extract_product_id_from_url(url: str) -> str:
    """Extract product ID from URL path (/p/{productID})."""
    match = PRODUCT_ID_RE.search(url)
    return match.group(1) if match else ""


def scrape_product_per_session(url: str, src_url: str, index: int) -> dict[str, Any]:
    """Scrape a single product page using a FRESH SB() session.

    Per seleniumbase-uc-patterns skill: anti-bot systems detect multi-page
    scraping sessions and block them. Each product gets its own fresh browser
    session with homepage warmup to establish session cookies.

    Architecture per product:
        1. Create fresh SB(uc=True, xvfb=True, locale_code='en-gb')
        2. Visit homepage → wait 20s for session cookies
        3. Accept cookie consent banners
        4. Navigate to product page → wait 3s for render
        5. Check for 'oops!' error page
        6. Extract product data (JSON-LD + DOM fallback)
        7. Close SB() session

    Returns a product dict.
    """
    # Default error product in case everything fails
    error_product = {
        "id": index,
        "title": "",
        "price": "",
        "availability": "",
        "original_price": "",
        "currency": CURRENCY,
        "url": url,
        "src_url": src_url,
        "location": "",
        "status_code": 0,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": "",
    }

    try:
        with SB(**_make_sb_kwargs()) as sb:
            driver = sb.driver

            # Step 1: Warmup — visit homepage to get session cookies
            if not warmup_session(driver):
                error_product["remarks"] = "Warmup failed — could not establish session"
                return error_product

            # Step 2: Navigate to product page
            logger.info(f"  [{index}] Navigating to {url}")
            try:
                driver.uc_open_with_reconnect(url, reconnect_time=UC_RECONNECT_TIME)
                time.sleep(3)
            except Exception as e:
                logger.error(f"  [{index}] Navigation failed: {e}")
                error_product["remarks"] = f"Navigation failed: {str(e)[:200]}"
                return error_product

            # Step 3: Check for error pages (oops!, not found, etc.)
            error_type = driver.execute_script(DETECT_OOPS_JS)
            if error_type:
                logger.warning(f"  [{index}] Error page detected: {error_type} on {url}")
                error_product["remarks"] = f"Soft 404: {error_type} on product page"
                return error_product

            # Step 4: Build product object
            product = Product(
                id=index,
                url=url,
                src_url=src_url,
                currency=CURRENCY,
                brand="Calvin Klein",
                sku=extract_product_id_from_url(url),
                scraped_at=datetime.now(timezone.utc).isoformat(),
                status_code=200,
            )

            # Step 5: Check for soft 404 via JSON-LD type
            try:
                jsonld_blocks = driver.execute_script(EXTRACT_JSONLD_JS) or []
                has_product_jsonld = any(
                    b.get("@type") in ("Product", "ProductGroup")
                    for b in jsonld_blocks
                    if isinstance(b, dict)
                )
                if not has_product_jsonld:
                    # No Product JSON-LD — might be a soft 404
                    page_title = driver.execute_script("return document.title || ''")
                    h1_text = driver.execute_script(
                        "var h1 = document.querySelector('h1'); return h1 ? h1.textContent.trim() : '';"
                    )
                    title_lower = (page_title + h1_text).lower()
                    if any(
                        kw in title_lower
                        for kw in ("not found", "unavailable", "no longer")
                    ):
                        product.remarks = "Soft 404: product not found"
                        product.status_code = 0
                        return product.to_dict()
                    logger.debug(f"No Product JSON-LD on {url}, using DOM fallback")
            except Exception as e:
                logger.debug(f"JSON-LD check error: {e}")
                jsonld_blocks = []

            # Step 6: Primary — Extract from JSON-LD
            jsonld_data: dict[str, Any] = {}
            if jsonld_blocks:
                try:
                    jsonld_data = driver.execute_script(
                        EXTRACT_PRODUCT_FROM_JSONLD_JS, jsonld_blocks
                    ) or {}
                except Exception as e:
                    logger.debug(f"JSON-LD extraction error: {e}")

            # Step 7: Fallback — Extract from DOM
            dom_data: dict[str, Any] = {}
            try:
                dom_data = driver.execute_script(EXTRACT_DOM_FALLBACK_JS) or {}
            except Exception as e:
                logger.debug(f"DOM fallback extraction error: {e}")

            # ── Map fields: JSON-LD primary, DOM fallback ──
            currency_code = jsonld_data.get("currency", CURRENCY)

            product.title = jsonld_data.get("title") or dom_data.get("title", "")
            product.description = jsonld_data.get("description") or dom_data.get(
                "description", ""
            )
            product.availability = jsonld_data.get("availability") or dom_data.get(
                "availability", ""
            )
            product.brand = jsonld_data.get("brand") or "Calvin Klein"
            product.sku = (
                jsonld_data.get("sku")
                or dom_data.get("sku")
                or extract_product_id_from_url(url)
            )

            # Price — use JSON-LD clean numeric value, then DOM fallback
            raw_price = jsonld_data.get("price") or dom_data.get("price", "")
            raw_original_price = jsonld_data.get("original_price") or dom_data.get(
                "original_price", ""
            )

            product.price = format_price(raw_price, currency_code)
            if raw_original_price:
                formatted_orig = format_price(raw_original_price, currency_code)
                # Only set original_price if it differs from current price
                if formatted_orig and formatted_orig != product.price:
                    product.original_price = formatted_orig

            product.currency = currency_code

            # Images — scope to product gallery, filter out non-product images
            images = jsonld_data.get("images") or dom_data.get("images") or []
            product_images: list[str] = []
            skip_patterns = [
                "/brand.assets/",
                "/emoji/",
                "/flags/",
                "/icon/",
                "/navigation/",
                "/logo",
            ]
            for img_url in images:
                if isinstance(img_url, str):
                    img_lower = img_url.lower()
                    if not any(p in img_lower for p in skip_patterns):
                        product_images.append(img_url)
            product.images = product_images[:15]  # Cap at 15 images per product

            # Check if current URL redirected (soft 404 via redirect)
            try:
                final_url = driver.execute_script("return window.location.href")
                if final_url and final_url != url:
                    if "/p/" not in final_url:
                        product.remarks = (
                            f"Soft 404: redirected to non-product page ({final_url})"
                        )
                        product.status_code = 0
                        product.title = ""
                        product.price = ""
                        return product.to_dict()
                    else:
                        product.url = final_url
            except Exception:
                pass

            # Build remarks from supplementary data
            remarks_parts: list[str] = []
            rating = jsonld_data.get("rating")
            review_count = jsonld_data.get("review_count")
            category = jsonld_data.get("category") or dom_data.get("category")
            if rating:
                remarks_parts.append(f"Rating: {rating}/5")
            if review_count:
                remarks_parts.append(f"Reviews: {review_count}")
            if category:
                remarks_parts.append(f"Category: {category}")
            product.remarks = " | ".join(remarks_parts)

            if not product.title:
                product.status_code = 0
                product.remarks = (
                    "No title extracted — page may not be a valid product page"
                )

            return product.to_dict()

    except Exception as e:
        logger.error(f"  [{index}] Session failed for {url}: {e}")
        error_product["remarks"] = f"Session error: {str(e)[:200]}"
        return error_product


# ─── URL loading ───────────────────────────────────────────────────────────────

def load_urls_from_file(filepath: str) -> list[str]:
    """Load product URLs from a JSON file."""
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("urls", [])


# ─── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=f"Scraper for {SITE_NAME} (SeleniumBase UC Mode)"
    )
    parser.add_argument(
        "--sample", action="store_true", help="Scrape only 5 products"
    )
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
        "--no-proxy",
        action="store_true",
        default=True,
        help="Skip proxy, connect directly (default)",
    )
    # Required: browser-service injects --xvfb when dispatching SeleniumBase scrapers.
    # Without this, argparse crashes with 'unrecognized arguments: --xvfb'.
    parser.add_argument(
        "--xvfb",
        action="store_true",
        default=True,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    start_time = time.time()

    # ── Load URLs ──
    product_urls: list[str] = []

    if args.urls:
        product_urls = [u.strip("\"'") for u in args.urls]
    elif args.input:
        product_urls = load_urls_from_file(args.input)
    else:
        # Default: read from input_urls.json in script directory
        if os.path.exists(INPUT_FILE):
            product_urls = load_urls_from_file(INPUT_FILE)
        else:
            logger.error(
                f"No input URLs file found at {INPUT_FILE} "
                f"and no --urls or --input provided"
            )
            sys.exit(1)

    if args.sample and product_urls:
        product_urls = product_urls[:5]
    if args.limit is not None:
        product_urls = product_urls[: args.limit]

    if not product_urls:
        logger.error("No product URLs found — cannot proceed")
        sys.exit(1)

    # ── Logging header ──
    logger.info("=" * 80)
    logger.info(f"Starting scraper for {SITE_NAME}")
    logger.info(f"Site: {SITE_URL}")
    logger.info(f"Platform: {PLATFORM} | Method: {SCRAPING_METHOD}")
    logger.info(f"Total products: {len(product_urls)}")
    logger.info(
        f"Architecture: PER-PAGE SESSION (fresh SB() + homepage warmup per product)"
    )
    logger.info(f"Delay between requests: {DELAY_BETWEEN_REQUESTS}s")
    logger.info(f"Warmup wait: {WARMUP_WAIT}s")
    logger.info("=" * 80)

    results: list[dict[str, Any]] = []
    failed = 0
    consecutive_errors = 0

    # ── Scrape each product with its own fresh SB() session ──
    for i, url in enumerate(product_urls):
        try:
            product_dict = scrape_product_per_session(url, url, i + 1)
            remarks = product_dict.get("remarks", "")

            if product_dict.get("title"):
                results.append(product_dict)
                consecutive_errors = 0
                logger.info(
                    f"  [{i + 1}/{len(product_urls)}] "
                    f"{product_dict['title'][:60]} \u2014 {product_dict['price']}"
                )
            else:
                logger.warning(
                    f"  [{i + 1}/{len(product_urls)}] No title: {url}"
                    f" {'(' + remarks + ')' if remarks else ''}"
                )
                failed += 1
                consecutive_errors += 1
                results.append(product_dict)

                # Abort if too many consecutive errors (likely session/site issue)
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    logger.error(
                        f"Aborting: {consecutive_errors} consecutive errors. "
                        f"Site may be blocking or unreachable."
                    )
                    break

        except Exception as e:
            logger.error(
                f"  [{i + 1}/{len(product_urls)}] Error scraping {url}: {e}"
            )
            failed += 1
            consecutive_errors += 1
            results.append(
                {
                    "id": i + 1,
                    "title": "",
                    "price": "",
                    "availability": "",
                    "original_price": "",
                    "currency": CURRENCY,
                    "url": url,
                    "src_url": url,
                    "location": "",
                    "status_code": 0,
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                    "remarks": f"Extraction error: {str(e)[:200]}",
                }
            )

        # Progress reporting
        if (i + 1) % 25 == 0 and (i + 1) < len(product_urls):
            percent = ((i + 1) / len(product_urls)) * 100
            logger.info(f"Progress: [{i + 1}/{len(product_urls)}] ({percent:.1f}%)")

        # Rate limiting between requests
        if i < len(product_urls) - 1:
            time.sleep(DELAY_BETWEEN_REQUESTS)

    # ── Write output ──
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

    os.makedirs(
        os.path.dirname(OUTPUT_FILE) if os.path.dirname(OUTPUT_FILE) else ".",
        exist_ok=True,
    )
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    # ── Summary ──
    logger.info("=" * 80)
    logger.info("EXTRACTION COMPLETE")
    success_count = len(results) - failed
    logger.info(f"Total: {len(results)}, Success: {success_count}, Failed: {failed}")
    logger.info(f"Duration: {round(time.time() - start_time, 2)}s")
    logger.info(f"Output: {OUTPUT_FILE}")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
