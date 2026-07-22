#!/usr/bin/env python3
"""American Eagle Outfitters (ae.com) — Playwright Scraper.

Single-phase URL-list scraper: reads product URLs from input_urls.json,
navigates each with Playwright, extracts product data from JSON-LD
(script[data-testid='pdp-schema-org']) + CSS selectors.

Usage:
    python3 scraper.py                                    # default: all URLs from input_urls.json
    python3 scraper.py --sample                           # first 5 products only
    python3 scraper.py --limit 50                         # cap at 50 products
    python3 scraper.py --input custom_urls.json           # use a different input file
    python3 scraper.py --urls "https://www.ae.com/us/en/p/..." "https://..."
    python3 scraper.py --no-proxy                         # force no proxy (default for this site)
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, Page

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

SITE_NAME = "American Eagle Outfitters"
SITE_URL = "https://www.ae.com"
PLATFORM = "custom"
SITE_SLUG = "ae-com"
SCRAPING_METHOD = "playwright"
PROXY_TIER = "none"

# ── Rate limiting ──────────────────────────────────────────────────────────
DELAY_BETWEEN_REQUESTS = 2.0  # seconds between requests (per site analysis)

# ── Page settings ─────────────────────────────────────────────────────────
PAGE_LOAD_TIMEOUT = 35000
PAGE_SETTLE_MS = 3000  # wait after domcontentloaded for Ember.js render

# ── AE.com-specific selectors ─────────────────────────────────────────────
JSONLD_SELECTOR = 'script[data-testid="pdp-schema-org"]'
TITLE_SELECTOR = 'h1[data-testid="product-name"]'
SALE_PRICE_SELECTOR = '[data-testid="sale-price"]'
LIST_PRICE_SELECTOR = '[data-testid="list-price"]'
IMAGE_GALLERY_SELECTOR = '[data-testid="images-container"]'
COLOR_SELECTOR = 'span[class*="product-color"]'
CURRENCY = "USD"

# Content-type-aware output filter: products need title + (price OR availability)
CORE_FILTER_FIELDS = ["price", "availability"]

# ═══════════════════════════════════════════════════════════════════════════════
# PATHS & LOGGING
# ═══════════════════════════════════════════════════════════════════════════════

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, f"{SITE_SLUG}.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(SITE_SLUG)


# ═══════════════════════════════════════════════════════════════════════════════
# BROWSER HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _get_browser(pw, headless: bool = True):
    """Launch Chromium — connects to remote CDP if available."""
    cdp_endpoint = os.environ.get("BROWSER_CDP_ENDPOINT", "")
    if cdp_endpoint:
        logger.info("Connecting to remote browser via CDP: %s", cdp_endpoint)
        return pw.chromium.connect_over_cdp(cdp_endpoint)

    launch_args = {
        "headless": headless,
        "args": [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--no-first-run",
        ],
    }
    return pw.chromium.launch(**launch_args)


def _get_status_code(page: Page, url: str) -> int:
    """Get the HTTP status code for the current page via response interception."""
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
        return resp.status if resp else 0
    except Exception:
        return 0


# ═══════════════════════════════════════════════════════════════════════════════
# SOFT 404 DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

_SOFT_404_PATTERNS = [
    "page not found",
    "product not found",
    "no longer available",
    "unavailable",
    "discontinued",
    "we couldn't find",
    "this item is no longer",
    "oops",
    "error 404",
    "not found",
]


def _detect_soft_404(page: Page, requested_url: str) -> Optional[str]:
    """Check for soft 404 via page title, h1, and JSON-LD presence."""
    try:
        final_url = page.url
        req_path = urlparse(requested_url).path.strip("/").lower()
        fin_path = urlparse(final_url).path.strip("/").lower()
        if fin_path and req_path and not fin_path.startswith(req_path.split("/")[0]):
            if "/us/en/p/" not in fin_path:
                return "Soft 404: redirected to non-product page"

        title_text = page.title().lower() if page.title() else ""
        for pattern in _SOFT_404_PATTERNS:
            if pattern in title_text:
                return f"Soft 404: page title contains '{pattern}'"

        h1_text = page.evaluate(
            "() => document.querySelector('h1')?.textContent?.toLowerCase() || ''"
        )
        for pattern in _SOFT_404_PATTERNS:
            if pattern in h1_text:
                return f"Soft 404: h1 contains '{pattern}'"

        # Check for JSON-LD Product type
        has_product = page.evaluate("""() => {
            const scripts = document.querySelectorAll('script[type="application/ld+json"]');
            for (const s of scripts) {
                try {
                    const d = JSON.parse(s.textContent);
                    if (Array.isArray(d)) {
                        if (d.some(i => i['@type'] === 'Product')) return true;
                    } else if (d['@type'] === 'Product') return true;
                } catch(e) {}
            }
            // Also check AE-specific data-testid
            const ae_ld = document.querySelector('script[data-testid="pdp-schema-org"]');
            if (ae_ld) {
                try {
                    const d = JSON.parse(ae_ld.textContent);
                    if (d['@type'] === 'Product') return true;
                } catch(e) {}
            }
            return false;
        }""")
        if not has_product:
            return "Soft 404: no JSON-LD Product type found"

    except Exception as exc:
        logger.debug("Soft 404 check error: %s", exc)

    return None


# ═══════════════════════════════════════════════════════════════════════════════
# EXTRACTION HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _normalize_price(price_raw: str, currency: str = "USD") -> str:
    """Normalize a raw price string to include the currency symbol."""
    if not price_raw:
        return ""
    cleaned = re.sub(r"\s*Now\s*", "", price_raw, flags=re.IGNORECASE).strip()
    if cleaned and cleaned[0] in "$€£¥":
        return cleaned
    symbol = {"USD": "$", "EUR": "€", "GBP": "£", "CAD": "C$", "AUD": "A$"}.get(currency, "$")
    return f"{symbol}{cleaned}"


def _normalize_availability(avail_str: str) -> str:
    """Normalize availability URL/text to 'In Stock' or 'Out of Stock'."""
    if not avail_str:
        return ""
    lower = avail_str.lower()
    if "instock" in lower or "in stock" in lower or "available" in lower:
        return "In Stock"
    if "outofstock" in lower or "out of stock" in lower or "soldout" in lower or "unavailable" in lower:
        return "Out of Stock"
    return "In Stock"


def _extract_category_from_url(url: str) -> str:
    """Extract category path from AE.com product URL.

    URL pattern: /us/en/p/{gender}/{top-cat}/{sub-cat}/{slug}/{sku}
    Category segments are everything between 'p' and the last two path segments.
    """
    try:
        path = urlparse(url).path.strip("/")
        parts = path.split("/")
        if "p" in parts:
            idx = parts.index("p")
            # Everything between 'p' and the last 2 segments (slug + sku)
            cat_parts = parts[idx + 1:-2]
            if cat_parts:
                return " > ".join(cat_parts)
    except Exception:
        pass
    return ""


def _extract_images(page: Page) -> list[str]:
    """Extract product gallery images from [data-testid='images-container'].

    Scoped to the gallery container, filtered to Scene7 product images.
    """
    return page.evaluate("""() => {
        const gallery = document.querySelector('[data-testid="images-container"]');
        const container = gallery || document;
        const imgs = container.querySelectorAll('img[src*="$pdp"]');
        const seen = new Set();
        const results = [];
        for (const img of imgs) {
            let src = img.getAttribute('src') || '';
            if (!src) continue;
            if (!src.includes('scene7') && !src.includes('/is/image/aeo/')) continue;
            const skip = ['/brand.assets/', '/emoji/', '/flags/', '/icon/', '/navigation/'];
            if (skip.some(p => src.toLowerCase().includes(p))) continue;
            if (src.startsWith('//')) src = 'https:' + src;
            if (!seen.has(src)) {
                seen.add(src);
                results.push(src);
            }
        }
        return results;
    }""")


# ═══════════════════════════════════════════════════════════════════════════════
# PRODUCT EXTRACTION JS — runs in browser context
# ═══════════════════════════════════════════════════════════════════════════════

EXTRACT_PRODUCT_JS = """
(src_url) => {
    const item = {
        title: '',
        price: '',
        availability: '',
        original_price: '',
        currency: '',
        url: window.location.href,
        src_url: src_url || '',
        location: '',
        remarks: '',
        brand: '',
        sku: '',
        description: '',
        image: '',
        color: '',
        material: '',
    };

    // ── JSON-LD extraction via AE-specific selector ───────────────────────
    let jsonld = null;

    // Try AE's custom data-testid first (most reliable)
    const aeScript = document.querySelector('script[data-testid="pdp-schema-org"]');
    if (aeScript) {
        try {
            const data = JSON.parse(aeScript.textContent);
            if (data && data['@type'] === 'Product') jsonld = data;
        } catch(e) {}
    }

    // Fallback: standard JSON-LD
    if (!jsonld) {
        const ldScripts = document.querySelectorAll('script[type="application/ld+json"]');
        for (const s of ldScripts) {
            try {
                const data = JSON.parse(s.textContent);
                if (data['@type'] === 'Product') { jsonld = data; break; }
                if (Array.isArray(data)) {
                    for (const item of data) {
                        if (item['@type'] === 'Product') { jsonld = item; break; }
                    }
                    if (jsonld) break;
                }
            } catch(e) {}
        }
    }

    if (jsonld) {
        item.title = jsonld.name || '';
        item.sku = jsonld.sku || '';
        item.description = jsonld.description || '';
        item.color = jsonld.color || '';
        item.material = jsonld.material || '';

        // Brand (can be string or object)
        const brand = jsonld.brand;
        if (typeof brand === 'string') item.brand = brand;
        else if (brand && brand.name) item.brand = brand.name;

        // Image
        const img = jsonld.image;
        if (typeof img === 'string') {
            item.image = img.startsWith('//') ? 'https:' + img : img;
        } else if (Array.isArray(img) && img.length > 0 && typeof img[0] === 'string') {
            item.image = img[0].startsWith('//') ? 'https:' + img[0] : img[0];
        }

        // Offers — extract price AND availability separately
        const offers = jsonld.offers;
        if (offers) {
            const offerList = Array.isArray(offers) ? offers : [offers];
            for (const offer of offerList) {
                if (!offer) continue;

                // Price
                if (!item.price && offer.price) {
                    item.price = String(offer.price);
                    item.currency = offer.priceCurrency || 'USD';
                }

                // Availability — extract INDEPENDENTLY of price
                if (!item.availability && offer.availability) {
                    const avail = offer.availability;
                    if (avail.includes('InStock') || avail.includes('instock')) {
                        item.availability = 'In Stock';
                    } else if (avail.includes('OutOfStock') || avail.includes('outofstock')) {
                        item.availability = 'Out of Stock';
                    } else if (avail.includes('PreOrder') || avail.includes('preorder')) {
                        item.availability = 'In Stock';
                    } else if (avail.includes('LimitedAvailability')) {
                        item.availability = 'In Stock';
                    }
                }
            }
        }
    }

    // ── CSS fallbacks ──────────────────────────────────────────────────────
    if (!item.title) {
        const h1 = document.querySelector('h1[data-testid="product-name"]');
        if (h1) item.title = h1.textContent.trim();
    }

    // Original price (list price) — only from DOM, not in JSON-LD
    const listPriceEl = document.querySelector('[data-testid="list-price"]');
    if (listPriceEl) {
        const lpText = listPriceEl.textContent.trim();
        item.original_price = lpText.startsWith('$') ? lpText : '$' + lpText;
    }

    // Sale price fallback (if JSON-LD didn't have it)
    if (!item.price) {
        const saleEl = document.querySelector('[data-testid="sale-price"]');
        if (saleEl) {
            const st = saleEl.textContent.replace(/Now\\s*/i, '').trim();
            item.price = st.startsWith('$') ? st.substring(1) : st;
            item.currency = 'USD';
        }
    }

    // Availability fallback from DOM
    if (!item.availability) {
        const availEl = document.querySelector('div[class*="availability"]');
        if (availEl) {
            const at = availEl.textContent.toLowerCase();
            if (at.includes('in stock') || at.includes('available')) {
                item.availability = 'In Stock';
            } else if (at.includes('out of stock') || at.includes('sold out')) {
                item.availability = 'Out of Stock';
            }
        }
    }

    // Color fallback
    if (!item.color) {
        const colorEl = document.querySelector('span[class*="product-color"]');
        if (colorEl) item.color = colorEl.textContent.trim();
    }

    return item;
}
"""


# ═══════════════════════════════════════════════════════════════════════════════
# SCRAPE PRODUCT
# ═══════════════════════════════════════════════════════════════════════════════

def _error_item(url: str, src_url: str, error: str, status_code: int = 0) -> dict:
    """Create an error item dict."""
    return {
        "id": 0,
        "title": "",
        "price": "",
        "availability": "",
        "original_price": "",
        "currency": "",
        "url": url,
        "src_url": src_url,
        "location": "",
        "status_code": status_code,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": f"Error: {error[:200]}",
    }


def scrape_product(page: Page, url: str, src_url: str, index: int) -> dict:
    """Navigate to a product URL and extract all product data."""
    # Navigate
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
        status_code = response.status if response else 0
    except Exception as exc:
        logger.error("Navigation failed for %s: %s", url[:80], exc)
        return _error_item(url, src_url, f"navigation failed: {exc}", 0)

    # Wait for Ember.js to render the product data
    page.wait_for_timeout(PAGE_SETTLE_MS)

    # Handle HTTP-level 404
    if status_code == 404:
        return _error_item(url, src_url, "404 not found", 404)

    # Soft 404 detection
    soft_404 = _detect_soft_404(page, url)
    if soft_404:
        return _error_item(url, src_url, soft_404, status_code)

    # Extract product data via JS evaluation
    try:
        data = page.evaluate(EXTRACT_PRODUCT_JS, src_url)
    except Exception as exc:
        logger.error("JS evaluation failed for %s: %s", url[:80], exc)
        return _error_item(url, src_url, f"JS evaluation failed: {exc}", status_code)

    # Build the output item
    price_raw = data.get("price", "")
    currency = data.get("currency", CURRENCY) or CURRENCY

    item = {
        "id": index,
        "title": data.get("title", ""),
        "price": _normalize_price(price_raw, currency),
        "availability": data.get("availability", ""),
        "original_price": data.get("original_price", ""),
        "currency": currency,
        "url": url,
        "src_url": src_url,
        "location": data.get("location", ""),
        "status_code": status_code,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": data.get("remarks", ""),
    }

    # Extra fields (not in standard output but useful)
    if data.get("brand"):
        item["brand"] = data["brand"]
    if data.get("sku"):
        item["sku"] = data["sku"]
    if data.get("description"):
        item["description"] = data["description"]
    if data.get("color"):
        item["color"] = data["color"]
    if data.get("material"):
        item["material"] = data["material"]

    # Image (main)
    image = data.get("image", "")
    if image:
        item["image"] = image

    # Images (gallery)
    try:
        images = _extract_images(page)
        if images:
            item["images"] = images
    except Exception as exc:
        logger.debug("Image extraction failed for %s: %s", url[:60], exc)

    # Category from URL
    category = _extract_category_from_url(url)
    if category:
        item["category"] = category

    # If no title, it might be a non-product page that slipped through
    if not item["title"]:
        item["remarks"] = "No product title found — may not be a valid product page"

    return item


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description=f"{SITE_NAME} Product Scraper")
    parser.add_argument("--input", type=str, help="Path to input URLs JSON file")
    parser.add_argument("--urls", nargs="+", help="Product URLs as CLI arguments")
    parser.add_argument("--sample", action="store_true", help="Scrape first 5 products only")
    parser.add_argument("--limit", type=int, default=None, help="Max products to scrape")
    parser.add_argument(
        "--headless", action="store_true", default=True,
        help="Run in headless mode (default: True)",
    )
    parser.add_argument(
        "--no-proxy", action="store_true",
        help="Disable proxy (default for this site)",
    )
    args = parser.parse_args()

    # ── Determine product URLs ─────────────────────────────────────────────
    product_urls: list[str] = []

    # --input takes precedence over everything
    if args.input:
        input_path = args.input
    else:
        input_path = os.path.join(SCRIPT_DIR, "input_urls.json")

    if args.urls:
        product_urls = list(args.urls)
    else:
        try:
            with open(input_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            product_urls = data.get("urls", [])
        except FileNotFoundError:
            logger.error("Input file not found: %s", input_path)
            sys.exit(1)
        except json.JSONDecodeError as exc:
            logger.error("Invalid JSON in %s: %s", input_path, exc)
            sys.exit(1)

    if not product_urls:
        logger.error("No product URLs to scrape")
        sys.exit(1)

    # Apply limits
    if args.sample:
        product_urls = product_urls[:5]
        logger.info("Sample mode: scraping first 5 products")
    elif args.limit:
        product_urls = product_urls[:args.limit]

    # Deduplicate while preserving order
    product_urls = list(dict.fromkeys(product_urls))

    # ── Logging ──────────────────────────────────────────────────────────
    logger.info("=" * 80)
    logger.info(f"Starting scraper for {SITE_NAME}")
    logger.info(f"Total products: {len(product_urls)}")
    logger.info(f"Scraping method: {SCRAPING_METHOD}")
    logger.info(f"Proxy tier: {PROXY_TIER}")
    logger.info("=" * 80)

    # ── Extraction ────────────────────────────────────────────────────────
    start_time = time.time()
    results: list[dict] = []
    success_count = 0
    failed_count = 0

    with sync_playwright() as p:
        browser = _get_browser(p, headless=args.headless)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
        )
        page = context.new_page()

        # Warm up session on the homepage
        logger.info("Warming up session on %s ...", SITE_URL)
        try:
            page.goto(SITE_URL, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
            page.wait_for_timeout(5000)
            logger.info("Session warm-up complete")
        except Exception as exc:
            logger.warning("Warm-up failed (non-fatal): %s", exc)

        for i, url in enumerate(product_urls, start=1):
            try:
                item = scrape_product(page, url, url, i)
                results.append(item)

                if item.get("title"):
                    success_count += 1
                else:
                    failed_count += 1
                    logger.warning("No title extracted from: %s", url[:80])

            except Exception as exc:
                logger.error("Error processing %s: %s", url[:80], exc)
                results.append(_error_item(url, url, str(exc)))
                failed_count += 1

            # Progress logging
            if i % 25 == 0:
                percent = (i / len(product_urls)) * 100
                logger.info(
                    "Progress: [%d/%d] (%.1f%%) — success: %d, failed: %d",
                    i, len(product_urls), percent, success_count, failed_count,
                )

            # Rate limiting
            if i < len(product_urls):
                time.sleep(DELAY_BETWEEN_REQUESTS)

        browser.close()

    # ── Output filter: keep only items with title + at least one core field ──
    extra = [f for f in CORE_FILTER_FIELDS if f and f != "title"]
    before_count = len(results)
    filtered_results = [
        it for it in results
        if it.get("title") and (not extra or any(it.get(f) for f in extra))
    ]
    if len(filtered_results) != before_count:
        logger.info(
            "Output filter: %d → %d items (dropped %d without core fields)",
            before_count, len(filtered_results), before_count - len(filtered_results),
        )

    # Assign sequential IDs
    for idx, item in enumerate(filtered_results, start=1):
        item["id"] = idx

    # ── Write output ──────────────────────────────────────────────────────
    output = {
        "site": {
            "name": SITE_NAME,
            "url": SITE_URL,
            "platform": PLATFORM,
            "scraping_method": SCRAPING_METHOD,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        },
        "products": filtered_results,
        "metadata": {
            "scraping_duration_seconds": round(time.time() - start_time, 2),
            "failed_products": failed_count,
            "rate_limit_delay": DELAY_BETWEEN_REQUESTS,
        },
    }

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    output_filename = os.path.join(SCRIPT_DIR, f"output_{timestamp}.json")
    with open(output_filename, "w", encoding="utf-8") as f:
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


        json.dump(output, f, indent=2, ensure_ascii=False, default=str)

    logger.info("=" * 80)
    logger.info("EXTRACTION COMPLETE")
    logger.info(
        "Total: %d, Success: %d, Failed: %d, Duration: %.1fs",
        len(filtered_results), success_count, failed_count,
        time.time() - start_time,
    )
    logger.info("Output: %s", output_filename)
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
