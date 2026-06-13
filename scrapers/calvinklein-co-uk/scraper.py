#!/usr/bin/env python3
"""
Calvin Klein UK Product Scraper — SeleniumBase UC Mode

Uses SeleniumBase UC Mode (undetected-chromedriver wrapper) to bypass fingerprint-level
anti-bot detection (Cloudflare or similar WAF). The site is a headless React/Next.js
frontend backed by Salesforce Commerce Cloud. Direct HTTP requests time out and standard
Playwright is blocked. Only UC Chrome loads the full page.

Extraction strategy: HYBRID
- JSON-LD structured data for: title, sku, brand, description, image, color, mpn, category
- Rendered DOM (JS evaluation) for: price, original_price, availability
- Static: currency = GBP

Usage:
    python3 scraper.py                                # reads input_urls.json from same folder
    python3 scraper.py --input urls.json               # explicit input file
    python3 scraper.py --urls url1 url2                # URLs as CLI arguments
    python3 scraper.py --sample                       # scrape only 5 products
    python3 scraper.py --limit 10                     # max 10 products
    python3 scraper.py --xvfb                         # use Xvfb virtual display (Docker)
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
# Configuration
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SITE_NAME = "Calvin Klein UK"
SITE_URL = "https://www.calvinklein.co.uk"
PLATFORM = "custom (React/Next.js + SFCC)"
SCRAPING_METHOD = "seleniumbase_uc"
SITE_SLUG = "calvinklein-co-uk"

DELAY_BETWEEN_REQUESTS = 3.0
PAGE_LOAD_TIMEOUT = 20
UC_RECONNECT_TIME = 4

TIMESTAMP = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
OUTPUT_FILE = os.path.join(SCRIPT_DIR, f"output_{TIMESTAMP}.json")
INPUT_FILE = os.path.join(SCRIPT_DIR, "input_urls.json")
LOG_FILE = os.path.join(SCRIPT_DIR, f"{SITE_SLUG}.log")

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


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class Product:
    """Represents a single scraped product."""

    id: int = 0
    title: str = ""
    price: str = ""
    availability: str = ""
    original_price: str = ""
    currency: str = "GBP"
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
    mpn: str = ""
    category: str = ""
    images: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert product to output dictionary."""
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
        # Optional enriched fields
        optional_fields = [
            ("description", self.description),
            ("sku", self.sku),
            ("brand", self.brand),
            ("color", self.color),
            ("mpn", self.mpn),
            ("category", self.category),
            ("images", self.images),
        ]
        for key, value in optional_fields:
            if value:
                result[key] = value
        return result


# ---------------------------------------------------------------------------
# JavaScript extraction scripts (var-based for Selenium compatibility)
# ---------------------------------------------------------------------------

# Detect anti-bot block pages
DETECT_BLOCK_JS = """
var bodyText = document.body ? document.body.innerText.toUpperCase() : '';
if (bodyText.indexOf('UNFORTUNATELY WE ARE UNABLE TO GIVE YOU ACCESS') !== -1) return 'akamai';
if (bodyText.indexOf('JUST A MOMENT') !== -1) return 'cloudflare';
if (bodyText.indexOf('ACCESS DENIED') !== -1) return 'generic';
if (bodyText.indexOf('BLOCKED') !== -1) return 'generic';
return null;
"""

# Extract JSON-LD Product and BreadcrumbList data
EXTRACT_JSONLD_JS = """
var result = { product: null, breadcrumbs: null };
var scripts = document.querySelectorAll('script[type="application/ld+json"]');
for (var i = 0; i < scripts.length; i++) {
    try {
        var data = JSON.parse(scripts[i].textContent);
        var items = Array.isArray(data) ? data : [data];
        for (var j = 0; j < items.length; j++) {
            if (items[j]['@type'] === 'Product' && !result.product) {
                result.product = items[j];
            }
            if (items[j]['@type'] === 'BreadcrumbList' && !result.breadcrumbs) {
                result.breadcrumbs = items[j];
            }
        }
    } catch(e) {}
}
return result;
"""

# Extract price from rendered DOM using pattern matching
EXTRACT_PRICE_JS = """
var prices = [];
var elements = document.querySelectorAll('span, div, p');
for (var i = 0; i < elements.length; i++) {
    var el = elements[i];
    var text = el.textContent.trim();
    // Only match elements where the text is a price or starts with a price
    var match = text.match(/^£\\s*([0-9]+[.,]?[0-9]{0,2})/);
    if (match) {
        prices.push({
            price: match[0],
            context: text.substring(0, 100),
            tag: el.tagName,
            className: el.className ? el.className.substring(0, 80) : '',
            childCount: el.children.length
        });
    }
}
return prices;
"""

# Extract original / was price from strikethrough / compare elements
EXTRACT_ORIGINAL_PRICE_JS = """
var origPrices = [];
var elements = document.querySelectorAll('s, del, [class*="compare"], [class*="was"], [class*="original"], [class*="strike"]');
for (var i = 0; i < elements.length; i++) {
    var el = elements[i];
    var text = el.textContent.trim();
    var match = text.match(/£\\s*([0-9]+[.,]?[0-9]{0,2})/);
    if (match) {
        origPrices.push({ price: match[0], context: text.substring(0, 80), tag: el.tagName });
    }
}
return origPrices;
"""

# Extract availability status from rendered DOM
EXTRACT_AVAILABILITY_JS = """
var bodyText = document.body ? document.body.innerText : '';
if (/out\\s+of\\s+stock/i.test(bodyText)) return 'Out of Stock';
if (/in\\s+stock/i.test(bodyText)) return 'In Stock';
// Check Add to Bag button state
var addBtn = null;
var buttons = document.querySelectorAll('button');
for (var i = 0; i < buttons.length; i++) {
    var btnText = buttons[i].textContent.toLowerCase();
    if (btnText.indexOf('add to bag') !== -1 || btnText.indexOf('add to cart') !== -1 || btnText.indexOf('add to shopping bag') !== -1) {
        addBtn = buttons[i];
        break;
    }
}
if (addBtn && (addBtn.disabled || addBtn.getAttribute('aria-disabled') === 'true' || addBtn.getAttribute('disabled') !== null)) {
    return 'Out of Stock';
}
if (addBtn) return 'In Stock';
return 'Unknown';
"""

# Accept cookie consent banner
ACCEPT_COOKIES_JS = """
var clicked = false;
// Try data-auto-id accept button
var btns = document.querySelectorAll('button[data-auto-id="accept-cookie-btn"]');
if (btns.length > 0) { btns[0].click(); return true; }
// Try common cookie consent button patterns
var allBtns = document.querySelectorAll('button');
for (var i = 0; i < allBtns.length; i++) {
    var t = allBtns[i].textContent.trim().toLowerCase();
    if (t === 'accept' || t === 'accept all cookies' || t === 'accept all' || t === 'i agree' || t === 'got it' || t === 'ok') {
        allBtns[i].click();
        return true;
    }
}
// Try links and other clickable elements
var links = document.querySelectorAll('a, [role="button"]');
for (var i = 0; i < links.length; i++) {
    var t = links[i].textContent.trim().toLowerCase();
    if (t === 'accept' || t === 'accept all cookies' || t === 'accept all') {
        links[i].click();
        return true;
    }
}
return false;
"""


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def normalize_url(url: str) -> str:
    """Normalize double-slash and ensure consistent URL format."""
    url = url.strip()
    # Fix double-slash in path (not protocol)
    url = re.sub(r"(https?://[^/]+)//+", r"\1/", url)
    # Remove trailing slash
    url = url.rstrip("/")
    return url


def clean_html(html_str: str) -> str:
    """Strip HTML tags and decode entities."""
    if not html_str:
        return ""
    text = re.sub(r"<[^>]+>", " ", html_str)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def pick_best_price(price_candidates: list[dict[str, str]]) -> str:
    """
    Pick the most likely product price from candidates.

    Heuristic: prefer short context, no shipping/delivery/footer keywords,
    no large child counts (which indicate footer/nav containers).
    """
    if not price_candidates:
        return ""

    # Filter out non-product price contexts
    filtered = []
    for candidate in price_candidates:
        context = candidate.get("context", "").lower()
        tag = candidate.get("tag", "")
        child_count = int(candidate.get("childCount", 0))

        # Exclude shipping, delivery, returns, footer, gift card, promo contexts
        if any(kw in context for kw in [
            "shipp", "deliver", "return", "footer", "gift",
            "promo", "voucher", "newsletter", "subscribe",
            "cookie", "sign up", "register",
        ]):
            continue

        # Prefer elements with few children (leaf nodes more likely to be price displays)
        if child_count > 20:
            continue

        filtered.append(candidate)

    if not filtered:
        # Fallback: return first candidate if all were filtered out
        if price_candidates:
            return price_candidates[0].get("price", "")
        return ""

    # Sort by context length — shorter context is more likely a pure price element
    filtered.sort(key=lambda x: len(x.get("context", "")))

    return filtered[0].get("price", "")


def format_price_gbp(price_str: str) -> str:
    """Ensure price string has £ prefix and is cleanly formatted."""
    if not price_str:
        return ""
    price_str = price_str.strip()
    if not price_str.startswith("£"):
        price_str = "£" + price_str
    # Clean up whitespace between symbol and digits
    price_str = re.sub(r"£\s+", "£", price_str)
    return price_str


# ---------------------------------------------------------------------------
# Cookie consent handling
# ---------------------------------------------------------------------------

def accept_cookies(driver: Any) -> bool:
    """Try to accept cookie consent banner. Returns True if clicked."""
    try:
        # First check if a cookie banner is visible
        banner_check = driver.execute_script("""
        var banners = document.querySelectorAll('[class*="cookie"], [class*="consent"], [class*="banner"]');
        for (var i = 0; i < banners.length; i++) {
            if (banners[i].offsetParent !== null || banners[i].getBoundingClientRect().height > 0) {
                return true;
            }
        }
        return false;
        """)
        if banner_check:
            clicked = driver.execute_script(ACCEPT_COOKIES_JS)
            if clicked:
                logger.info("Accepted cookies consent banner")
                time.sleep(2)
                return True
    except Exception as e:
        logger.debug(f"Cookie acceptance check error (non-fatal): {e}")
    return False


# ---------------------------------------------------------------------------
# Product scraping
# ---------------------------------------------------------------------------

def scrape_product(
    driver: Any,
    url: str,
    src_url: str,
    index: int,
) -> dict[str, Any]:
    """
    Scrape a single product page.

    Uses hybrid extraction: JSON-LD for structured fields + DOM JS for price/availability.
    """
    url = normalize_url(url)
    logger.info(f"  [{index}] Navigating to: {url}")

    # Navigate using UC reconnect
    driver.uc_open_with_reconnect(url, reconnect_time=UC_RECONNECT_TIME)
    time.sleep(3)

    # Wait for page to fully render (React/Next.js hydration)
    try:
        driver.execute_script("""
        return new Promise(function(resolve) {
            var checkCount = 0;
            var interval = setInterval(function() {
                checkCount++;
                // Check if page has meaningful product content
                var scripts = document.querySelectorAll('script[type="application/ld+json"]');
                if (scripts.length > 0) {
                    clearInterval(interval);
                    resolve(true);
                }
                if (checkCount > 20) {
                    clearInterval(interval);
                    resolve(true);
                }
            }, 500);
        });
        """)
    except Exception:
        time.sleep(3)

    # Detect anti-bot block
    try:
        block_type = driver.execute_script(DETECT_BLOCK_JS)
        if block_type:
            logger.warning(f"  [{index}] {block_type.upper()} block detected on {url}")
            return {
                "id": index,
                "title": "",
                "price": "",
                "availability": "",
                "original_price": "",
                "currency": "GBP",
                "url": url,
                "src_url": src_url,
                "location": "UK",
                "status_code": 403,
                "scraped_at": datetime.now(timezone.utc).isoformat(),
                "remarks": f"{block_type.upper()} BLOCKED — page did not load",
            }
    except Exception as e:
        logger.warning(f"  [{index}] Block detection error (non-fatal): {e}")

    # Accept cookies if banner appears on product page
    accept_cookies(driver)

    # --- JSON-LD extraction ---
    product_data: dict[str, Any] = {
        "id": index,
        "url": url,
        "src_url": src_url,
        "currency": "GBP",
        "location": "UK",
        "status_code": 200,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }

    jsonld = None
    breadcrumbs = None
    try:
        ld_result = driver.execute_script(EXTRACT_JSONLD_JS)
        if ld_result:
            jsonld = ld_result.get("product")
            breadcrumbs = ld_result.get("breadcrumbs")
    except Exception as e:
        logger.warning(f"  [{index}] JSON-LD extraction error: {e}")

    # Extract JSON-LD fields
    if jsonld:
        product_data["title"] = jsonld.get("name", "")
        product_data["sku"] = jsonld.get("sku", "")
        product_data["description"] = clean_html(jsonld.get("description", ""))
        brand_obj = jsonld.get("brand")
        if isinstance(brand_obj, dict):
            product_data["brand"] = brand_obj.get("name", "")
        else:
            product_data["brand"] = str(brand_obj) if brand_obj else ""

        # Image: may be string or array
        img = jsonld.get("image", "")
        if isinstance(img, list):
            product_data["images"] = [i for i in img if i]
        elif img:
            product_data["images"] = [img]

        product_data["color"] = jsonld.get("color", "")
        product_data["mpn"] = jsonld.get("mpn", "")

    # Fallback title from page <title>
    if not product_data.get("title"):
        try:
            page_title = driver.execute_script("return document.title;")
            if page_title:
                # Pattern: "Product Name Calvin Klein® | SKU"
                fallback_title = page_title.split("Calvin Klein")[0].strip()
                if not fallback_title:
                    fallback_title = page_title.split("|")[0].strip()
                product_data["title"] = fallback_title
        except Exception:
            pass

    # Category from BreadcrumbList
    if breadcrumbs:
        try:
            items = breadcrumbs.get("itemListElement", [])
            category_parts = []
            for item in items:
                name = item.get("item", {}).get("name", "")
                if name and name.lower() != "home" and name.lower() != "calvin klein":
                    category_parts.append(name)
            product_data["category"] = " > ".join(category_parts)
        except Exception:
            pass

    # --- DOM extraction for price ---
    try:
        price_candidates = driver.execute_script(EXTRACT_PRICE_JS) or []
        if price_candidates:
            # Log all candidates for debugging
            for pc in price_candidates[:5]:
                logger.debug(
                    f"  [{index}] Price candidate: '{pc.get('price')}' "
                    f"context='{pc.get('context', '')[:60]}' "
                    f"tag={pc.get('tag')} children={pc.get('childCount')}"
                )
            best_price = pick_best_price(price_candidates)
            product_data["price"] = format_price_gbp(best_price)
        else:
            logger.warning(f"  [{index}] No price found via pattern matching")
    except Exception as e:
        logger.warning(f"  [{index}] Price extraction error: {e}")

    # --- DOM extraction for original/was price ---
    try:
        orig_candidates = driver.execute_script(EXTRACT_ORIGINAL_PRICE_JS) or []
        if orig_candidates:
            product_data["original_price"] = format_price_gbp(
                orig_candidates[0].get("price", "")
            )
    except Exception as e:
        logger.debug(f"  [{index}] Original price extraction error (non-fatal): {e}")

    # --- DOM extraction for availability ---
    try:
        availability = driver.execute_script(EXTRACT_AVAILABILITY_JS)
        if availability:
            product_data["availability"] = availability
        else:
            product_data["availability"] = "Unknown"
    except Exception as e:
        logger.debug(f"  [{index}] Availability extraction error (non-fatal): {e}")
        product_data["availability"] = "Unknown"

    # Build remarks if price came from DOM fallback
    remarks_parts = []
    if product_data.get("price"):
        remarks_parts.append("Price extracted from DOM (JSON-LD offers empty)")
    if product_data.get("availability") == "Unknown":
        remarks_parts.append("Availability unconfirmed")
    product_data["remarks"] = "; ".join(remarks_parts)

    return product_data


# ---------------------------------------------------------------------------
# URL loading
# ---------------------------------------------------------------------------

def load_urls_from_file(filepath: str) -> list[str]:
    """Load product URLs from a JSON file."""
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("urls", [])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description=f"Scraper for {SITE_NAME} (SeleniumBase UC Mode)"
    )
    parser.add_argument(
        "--input", type=str, default=None, help="Path to input URLs JSON file"
    )
    parser.add_argument(
        "--urls", nargs="+", default=None, help="Product URLs as arguments"
    )
    parser.add_argument(
        "--sample", action="store_true", help="Scrape only 5 products"
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Max products to scrape"
    )
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        default=True,
        help="Connect directly without proxy (default: no proxy)",
    )
    parser.add_argument(
        "--xvfb", action="store_true", help="Use Xvfb virtual display (Docker)"
    )
    args = parser.parse_args()

    start_time = time.time()

    logger.info("=" * 80)
    logger.info(f"Starting SeleniumBase UC Mode scraper for {SITE_NAME}")
    logger.info(f"Site: {SITE_URL}")
    logger.info(f"Strategy: {SCRAPING_METHOD} (no proxy)")
    logger.info(f"Output: {OUTPUT_FILE}")
    logger.info(f"Xvfb: {args.xvfb}")
    logger.info("=" * 80)

    # --- Load URLs ---
    product_urls: list[str] = []

    if args.urls:
        product_urls = [normalize_url(u) for u in args.urls]
    elif args.input:
        product_urls = load_urls_from_file(args.input)
    elif os.path.exists(INPUT_FILE):
        product_urls = load_urls_from_file(INPUT_FILE)

    if args.sample:
        product_urls = product_urls[:5]
    if args.limit:
        product_urls = product_urls[: args.limit]

    if not product_urls:
        logger.error("No product URLs found. Exiting.")
        logger.error("Provide URLs via --urls, --input, or input_urls.json")
        sys.exit(1)

    logger.info(f"Total products to scrape: {len(product_urls)}")
    logger.info("-" * 80)

    # --- Launch browser ---
    sb_kwargs: dict[str, Any] = {
        "uc": True,
        "xvfb": args.xvfb,
        "headless": True,
        "locale_code": "en-GB",
    }

    results: list[dict[str, Any]] = []
    failed = 0

    with SB(**sb_kwargs) as sb:
        driver = sb.driver

        try:
            # --- Warm-up: visit homepage, wait, accept cookies ---
            logger.info(f"Warming up: visiting {SITE_URL}")
            driver.uc_open_with_reconnect(
                SITE_URL, reconnect_time=UC_RECONNECT_TIME
            )
            time.sleep(5)

            # Check for block on homepage
            try:
                block_type = driver.execute_script(DETECT_BLOCK_JS)
                if block_type:
                    logger.error(
                        f"{block_type.upper()} BLOCK DETECTED during warm-up"
                    )
                    logger.error("Aborting scrape.")
                    sys.exit(1)
            except Exception:
                pass

            # Accept cookies
            accept_cookies(driver)
            logger.info("Warm-up complete")

            # --- Scrape each product ---
            for i, url in enumerate(product_urls):
                url = normalize_url(url)
                src_url = url  # src_url equals the product URL for direct input

                try:
                    product = scrape_product(driver, url, src_url, i + 1)

                    if product.get("title"):
                        results.append(product)
                        logger.info(
                            f"  [{i + 1}/{len(product_urls)}] "
                            f"{product['title'][:60]} — {product.get('price', 'N/A')}"
                        )
                    else:
                        logger.warning(
                            f"  [{i + 1}/{len(product_urls)}] "
                            f"No title extracted: {url}"
                        )
                        failed += 1
                        results.append(product)

                except Exception as e:
                    logger.error(
                        f"  [{i + 1}/{len(product_urls)}] Error scraping {url}: {e}"
                    )
                    failed += 1
                    results.append({
                        "id": i + 1,
                        "title": "",
                        "price": "",
                        "availability": "",
                        "original_price": "",
                        "currency": "GBP",
                        "url": url,
                        "src_url": src_url,
                        "location": "UK",
                        "status_code": 0,
                        "scraped_at": datetime.now(timezone.utc).isoformat(),
                        "remarks": f"Scrape error: {str(e)[:200]}",
                    })

                # Progress reporting
                if (i + 1) % 25 == 0 or (i + 1) == len(product_urls):
                    percent = ((i + 1) / len(product_urls)) * 100
                    logger.info(
                        f"Progress: [{i + 1}/{len(product_urls)}] ({percent:.1f}%)"
                    )

                # Rate limiting delay between requests
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

    logger.info("=" * 80)
    logger.info("EXTRACTION COMPLETE")
    logger.info(f"Total: {len(results)}, Success: {len(results) - failed}, Failed: {failed}")
    logger.info(f"Duration: {round(time.time() - start_time, 2)}s")
    logger.info(f"Output: {OUTPUT_FILE}")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
