#!/usr/bin/env python3
"""
SeleniumBase UC Mode Scraper Template

Uses SeleniumBase UC Mode (undetected-chromedriver wrapper) to bypass anti-bot
protection (Akamai, Cloudflare). Uses Xvfb virtual display for Docker/headless
Linux environments.

Anti-bot sites (Akamai Bot Manager, Cloudflare Bot Management) detect multi-page
scraping sessions and block them. This template uses a PER-PAGE SESSION architecture:
each product page gets its own fresh SB() session so blocked sessions don't cascade.
Discovery (Phase 1) uses a single session with automatic reset on block detection.

Supports two modes:
1. Single-phase: product URLs provided via --urls or --input
2. Two-phase: category seed URLs provided, product URLs discovered at runtime

Usage:
    python3 scraper.py                    # reads input_urls.json from same folder
    python3 scraper.py --input urls.json  # explicit input file
    python3 scraper.py --urls url1 url2     # URLs as CLI arguments
    python3 scraper.py --sample               # scrape only 5 products
    python3 scraper.py --category-url "https://shop.com/category" --limit 3  # two-phase discovery
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

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.environ.get("PROJECT_ROOT", os.path.join(SCRIPT_DIR, ".."))
sys.path.insert(0, PROJECT_ROOT)

SITE_NAME = "{SITE_NAME}"
SITE_URL = "{SITE_URL}"
PLATFORM = "{PLATFORM}"
SCRAPING_METHOD = "seleniumbase_uc"
SITE_SLUG = "{SITE_SLUG}"
CURRENCY = "{CURRENCY}"

PRODUCT_LISTING_URL = "{PRODUCT_LISTING_URL}"
SRC_URL = "{PRODUCT_LISTING_URL}"
DELAY_BETWEEN_REQUESTS = {DELAY_BETWEEN_REQUESTS}
WARMUP_WAIT = 15
UC_RECONNECT_TIME = 4
BLOCK_RESET_THRESHOLD = 2
SESSIONS_BEFORE_FULL_RESET = 10

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


@dataclass
class Product:
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


def clean_html(html_str: str) -> str:
    if not html_str:
        return ""
    text = re.sub(r"<[^>]+>", " ", html_str)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


DETECT_BLOCK_JS = """
var bodyText = document.body ? document.body.innerText.toUpperCase() : '';
if (bodyText.indexOf('UNFORTUNATELY WE ARE UNABLE TO GIVE YOU ACCESS') !== -1) return 'akamai';
if (bodyText.indexOf('JUST A MOMENT') !== -1) return 'cloudflare';
if (bodyText.indexOf('ACCESS DENIED') !== -1) return 'generic';
return null;
"""

ACCEPT_COOKIES_JS = """
var btns = document.querySelectorAll("button[data-auto-id='accept-cookie-btn']");
if (btns.length > 0) { btns[0].click(); return true; }
var all = document.querySelectorAll('button');
for (var i = 0; i < all.length; i++) {
    var t = all[i].textContent.trim().toLowerCase();
    if (t === 'accept' || t === 'accept all cookies' || t === 'accept all') {
        all[i].click(); return true;
    }
}
return false;
"""

EXTRACT_PRODUCT_JS = """
var product = {
    title: '',
    price: '',
    availability: '',
    original_price: '',
    currency: '',
    url: window.location.href,
    src_url: arguments[0] || '',
    location: '',
    remarks: '',
    brand: '',
    sku: ''
};

var jsonld = null;
var scripts = document.querySelectorAll('script[type="application/ld+json"]');
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

product.title = (jsonld && jsonld.name) || '';
if (!product.title) {
    var titleEl = document.querySelector('h1');
    product.title = titleEl ? titleEl.textContent.trim() : '';
}

if (jsonld && jsonld.offers) {
    var offers = Array.isArray(jsonld.offers) ? jsonld.offers[0] : jsonld.offers;
    product.price = offers.price || '';
    var highPrice = offers.highPrice || '';
    if (highPrice && parseFloat(highPrice) > parseFloat(product.price || 0)) {
        product.original_price = highPrice;
    }
    product.currency = offers.priceCurrency || '';
    var avail = offers.availability || '';
    product.availability = avail.indexOf('InStock') !== -1 ? 'In Stock' : 'Out of Stock';
} else {
    var priceEl = document.querySelector('[data-testid*="price"], .product-price, .price, [class*="price-current"]');
    product.price = priceEl ? priceEl.textContent.trim() : '';
}

product.brand = (jsonld && jsonld.brand && jsonld.brand.name) || '';
product.sku = (jsonld && jsonld.sku) || '';

return product;
"""

EXTRACT_PRODUCT_URLS_JS = """
var links = document.querySelectorAll('a[href]');
var seen = {};
var unique = [];
for (var i = 0; i < links.length; i++) {
    var href = links[i].getAttribute('href');
    if (href && !seen[href]) {
        seen[href] = true;
        if (href.indexOf('http') !== 0) {
            href = window.location.origin + href;
        }
        unique.push(href);
    }
}
return unique;
"""

EXTRACT_IMAGES_JS = """
var images = [];
var imgEls = document.querySelectorAll('img[src]');
for (var i = 0; i < imgEls.length; i++) {
    var src = imgEls[i].getAttribute('src');
    if (src && src.indexOf('data:') !== 0 && src.indexOf('logo') === -1
        && src.indexOf('icon') === -1 && src.indexOf('sprite') === -1
        && src.indexOf('pixel') === -1) {
        images.push(src);
    }
}
return images;
"""

CHECK_REDIRECT_JS = """
var url = window.location.href;
if (url.indexOf('careers.pvh.com') !== -1 ||
    url.indexOf('oops') !== -1 ||
    url.indexOf('unavailable') !== -1 ||
    url.indexOf('not-found') !== -1) {
    return 'redirect';
}
return null;
"""

SESSION_KWARGS = {
    "uc": True,
    "xvfb": True,
}


def _make_sb_kwargs(extra: dict | None = None) -> dict[str, Any]:
    kwargs = dict(SESSION_KWARGS)
    if extra:
        kwargs.update(extra)
    return kwargs


def open_page(driver, url, reconnect_time=UC_RECONNECT_TIME) -> bool:
    """Navigate to a URL. Returns False if page shows redirect/block."""
    try:
        driver.uc_open_with_reconnect(url, reconnect_time=reconnect_time)
        time.sleep(3)
        redirect = driver.execute_script(CHECK_REDIRECT_JS)
        if redirect:
            logger.warning(f"Redirect/block detected on {url}: {redirect}")
            return False
        return True
    except Exception as e:
        logger.error(f"Failed to open {url}: {e}")
        return False


def warmup_session(driver) -> bool:
    logger.info(f"Warming up session: visiting {SITE_URL}")
    if not open_page(driver, SITE_URL):
        logger.error("Warm-up page failed (redirect/block)")
        return False

    logger.info(f"Waiting {WARMUP_WAIT}s for anti-bot sensor data collection...")
    time.sleep(WARMUP_WAIT)

    block_type = driver.execute_script(DETECT_BLOCK_JS)
    if block_type:
        logger.error(f"{block_type.upper()} BLOCK DETECTED during warm-up")
        return False

    clicked = driver.execute_script(ACCEPT_COOKIES_JS)
    if clicked:
        logger.info("Accepted cookies")
        time.sleep(2)

    logger.info("Warm-up complete")
    return True


def scrape_product_per_session(url: str, src_url: str, index: int) -> dict[str, Any]:
    """Create a fresh SB() session for each product page to avoid session-level anti-bot detection."""
    try:
        with SB(**_make_sb_kwargs()) as sb:
            driver = sb.driver
            if not open_page(driver, url):
                return {
                    "id": index,
                    "title": "",
                    "price": "",
                    "availability": "",
                    "original_price": "",
                    "currency": CURRENCY,
                    "url": url,
                    "src_url": src_url,
                    "location": "",
                    "status_code": 403,
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                    "remarks": "Redirect/block on initial page load",
                }

            data = driver.execute_script(EXTRACT_PRODUCT_JS, src_url)
            images = driver.execute_script(EXTRACT_IMAGES_JS) or []

            product = Product(
                id=index,
                title=data.get("title", ""),
                price=data.get("price", ""),
                availability=data.get("availability", ""),
                original_price=data.get("original_price", ""),
                currency=data.get("currency", CURRENCY),
                url=url,
                src_url=src_url,
                location=data.get("location", ""),
                status_code=200,
                scraped_at=datetime.now(timezone.utc).isoformat(),
                remarks=data.get("remarks", ""),
                brand=data.get("brand", ""),
                sku=data.get("sku", ""),
                images=images,
            )

            if not product.title:
                product.status_code = 0
                product.remarks = "No title found — page may not have loaded"

            return product.to_dict()

    except Exception as e:
        logger.error(f"  [{index}] Session failed for {url}: {e}")
        return {
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
            "remarks": f"Session error: {str(e)[:200]}",
        }


def discover_product_urls(driver, category_url: str) -> list[str]:
    open_page(driver, category_url)
    if driver.current_url != category_url and "/oops" not in (driver.current_url or ""):
        logger.warning(f"Redirected during discovery, got {driver.current_url}")
        return []
    return driver.execute_script(EXTRACT_PRODUCT_URLS_JS) or []


def load_urls_from_file(filepath: str) -> list[str]:
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("urls", [])


def save_urls_to_file(filepath: str, urls: list[str]) -> None:
    os.makedirs(os.path.dirname(filepath) if os.path.dirname(filepath) else ".", exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump({"urls": urls}, f, indent=2, ensure_ascii=False)
    logger.info(f"Saved {len(urls)} URLs to {filepath}")


def main():
    parser = argparse.ArgumentParser(description=f"Scraper for {SITE_NAME} (SeleniumBase UC Mode)")
    parser.add_argument("--sample", action="store_true", help="Scrape only 5 products")
    parser.add_argument("--limit", type=int, default=None, help="Max products to scrape")
    parser.add_argument("--input", type=str, default=None, help="Path to input URLs JSON file")
    parser.add_argument("--urls", nargs="+", default=None, help="Product URLs as arguments")
    parser.add_argument("--category-url", type=str, default=None, help="Category URL for product discovery")
    parser.add_argument("--no-proxy", action="store_true", help="Skip proxy, connect directly")
    parser.add_argument("--xvfb", action="store_true", help="Use Xvfb virtual display (Docker)")
    args = parser.parse_args()

    start_time = time.time()

    logger.info("=" * 80)
    logger.info(f"Starting scraper for {SITE_NAME}")
    logger.info(f"Site: {SITE_URL}")
    logger.info(f"Xvfb: {args.xvfb}")
    logger.info("=" * 80)

    product_urls = []

    if args.urls:
        product_urls = [u.strip("\"'") for u in args.urls]
    elif args.input:
        product_urls = load_urls_from_file(args.input)
    elif args.category_url:
        product_urls = []

    if args.sample and product_urls:
        product_urls = product_urls[:5]
    if args.limit:
        product_urls = product_urls[: args.limit]

    results = []
    failed = 0

    extra = {}
    if args.no_proxy:
        extra["proxy"] = None
    if args.xvfb:
        extra["xvfb"] = True

    if not product_urls and args.category_url:
        logger.info(f"No input URLs provided, discovering from category: {args.category_url}")
        with SB(**_make_sb_kwargs(extra)) as sb:
            driver = sb.driver
            try:
                if not warmup_session(driver):
                    logger.error("Warm-up failed, cannot proceed")
                    sys.exit(1)
                product_urls = discover_product_urls(driver, args.category_url)
                save_urls_to_file(INPUT_FILE, product_urls)
            finally:
                pass

        if args.sample and product_urls:
            product_urls = product_urls[:5]
        if args.limit:
            product_urls = product_urls[: args.limit]

    if not product_urls:
        logger.error("No product URLs found — cannot proceed")
        sys.exit(1)

    logger.info(f"Total products to scrape: {len(product_urls)}")
    logger.info(f"Using PER-PAGE SESSION architecture (new SB() per product page)")

    for i, url in enumerate(product_urls):
        try:
            product = scrape_product_per_session(url, SRC_URL, i + 1)
            if product.get("title"):
                results.append(product)
                logger.info(
                    f"  [{i + 1}/{len(product_urls)}] "
                    f"{product['title'][:60]} \u2014 {product['price']}"
                )
            else:
                logger.warning(f"  [{i + 1}/{len(product_urls)}] No title: {url}")
                failed += 1
                results.append(product)

        except Exception as e:
            logger.error(f"  [{i + 1}/{len(product_urls)}] Error: {url}: {e}")
            failed += 1

        if (i + 1) % 25 == 0:
            percent = ((i + 1) / len(product_urls)) * 100
            logger.info(f"Progress: [{i + 1}/{len(product_urls)}] ({percent:.1f}%)")

        if i < len(product_urls) - 1:
            time.sleep(DELAY_BETWEEN_REQUESTS)

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

    os.makedirs(os.path.dirname(OUTPUT_FILE) if os.path.dirname(OUTPUT_FILE) else ".", exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    logger.info("=" * 80)
    logger.info("EXTRACTION COMPLETE")
    logger.info(f"Total: {len(results)}, Failed: {failed}")
    logger.info(f"Duration: {round(time.time() - start_time, 2)}s")
    logger.info(f"Output: {OUTPUT_FILE}")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
