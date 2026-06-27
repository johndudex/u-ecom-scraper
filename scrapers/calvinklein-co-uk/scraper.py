#!/usr/bin/env python3
"""
Calvin Klein UK Product Scraper

Two-phase SeleniumBase UC Mode scraper for calvinklein.co.uk.
Phase 1: Discover product URLs from search results (handles infinite scroll).
Phase 2: Extract product data from each page using JSON-LD + CSS fallbacks.

Uses per-page sessions to avoid Cloudflare Bot Management blocking.

Usage:
    python3 scraper.py                        # reads input_urls.json from same folder
    python3 scraper.py --discover             # Phase 1 only — discover and save URLs
    python3 scraper.py --scrape              # Phase 2 only — scrape saved URLs
    python3 scraper.py --input urls.json      # explicit input file
    python3 scraper.py --urls url1 url2        # URLs as CLI arguments
    python3 scraper.py --sample                 # scrape only 5 products
    python3 scraper.py --limit 10              # scrape max 10 products
    python3 scraper.py --search watches         # override search criteria
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

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.environ.get("PROJECT_ROOT", os.path.join(SCRIPT_DIR, ".."))

SITE_NAME = "Calvin Klein"
SITE_URL = "https://www.calvinklein.co.uk"
PLATFORM = "sfcc"
SCRAPING_METHOD = "seleniumbase_uc"
SITE_SLUG = "calvinklein-co-uk"

SEARCH_CRITERIA = "watches"
SEARCH_URL = f"{SITE_URL}/search?searchTerm={SEARCH_CRITERIA}"

DELAY_BETWEEN_REQUESTS = 3.0
PAGE_LOAD_TIMEOUT = 30
WARMUP_WAIT = 20
UC_RECONNECT_TIME = 4
MAX_SCROLL_PASSES = 10
SCROLL_PAUSE = 2.0
DISCOVER_MAX_PRODUCTS = 200

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
    category: str = ""

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
        for optional_key in ("description", "images", "brand", "sku", "category"):
            val = getattr(self, optional_key)
            if val:
                result[optional_key] = val
        return result


def clean_html(html_str: str) -> str:
    if not html_str:
        return ""
    text = re.sub(r"<[^>]+>", " ", html_str)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_sku_from_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    parts = path.split("/")
    for part in reversed(parts):
        match = re.search(r"([A-Z]?\d{8,})", part.upper())
        if match:
            return match.group(1)
    return ""


def detect_currency_from_price(price_text: str) -> str:
    if not price_text:
        return ""
    if "$" in price_text:
        return "USD"
    if "€" in price_text:
        return "EUR"
    if "£" in price_text:
        return "GBP"
    return "GBP"


def format_price_with_symbol(raw_price: str, currency: str) -> str:
    if not raw_price:
        return ""
    raw_price = raw_price.strip()
    if re.match(r"^[\$€£¥]", raw_price):
        return raw_price
    symbol_map = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "CHF": "CHF "}
    symbol = symbol_map.get(currency, "")
    return f"{symbol}{raw_price}"


def extract_jsonld_from_html(page_source: str) -> list[dict[str, Any]]:
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


def find_jsonld_product(
    jsonld_blocks: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    for block in jsonld_blocks:
        atype = block.get("@type", "")
        if isinstance(atype, str) and atype in ("Product", "ProductGroup"):
            return block
        if isinstance(atype, list) and "Product" in atype:
            return block
    return None


def find_jsonld_breadcrumbs(
    jsonld_blocks: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    for block in jsonld_blocks:
        atype = block.get("@type", "")
        if isinstance(atype, str) and atype == "BreadcrumbList":
            return block
    return None


def extract_breadcrumb_category(breadcrumb: Optional[dict[str, Any]]) -> str:
    if not breadcrumb:
        return ""
    items = breadcrumb.get("itemListElement", [])
    categories = []
    for item in items:
        name = item.get("name", "")
        if name:
            categories.append(name)
    return " > ".join(categories)


DETECT_BLOCK_JS = """
var bodyText = document.body ? document.body.innerText.toUpperCase() : '';
if (bodyText.indexOf('UNFORTUNATELY WE ARE UNABLE TO GIVE YOU ACCESS') !== -1) return 'akamai';
if (bodyText.indexOf('JUST A MOMENT') !== -1) return 'cloudflare';
if (bodyText.indexOf('ACCESS DENIED') !== -1) return 'generic';
if (bodyText.indexOf('VERIFY YOU ARE HUMAN') !== -1) return 'captcha';
return null;
"""

ACCEPT_COOKIES_JS = """
var selectors = [
    "button[data-auto-id='accept-cookie-btn']",
    "button[data-testid='cookie-accept']",
    "#onetrust-accept-btn-handler",
    ".cookie-accept",
    "button[class*='cookie' i][class*='accept' i]"
];
for (var i = 0; i < selectors.length; i++) {
    try {
        var btn = document.querySelector(selectors[i]);
        if (btn) { btn.click(); return true; }
    } catch(e) {}
}
var allBtns = document.querySelectorAll('button, a[role="button"]');
for (var j = 0; j < allBtns.length; j++) {
    var t = allBtns[j].textContent.trim().toLowerCase();
    if (t === 'accept' || t === 'accept all cookies' || t === 'accept all' ||
        t === 'agree' || t === 'continue' || t === 'got it') {
        allBtns[j].click();
        return true;
    }
}
return false;
"""

DISCOVER_PRODUCTS_JS = """
var cards = document.querySelectorAll('.ProductGrid_ProductGridItem__VJcst, [class*="ProductGridItem"], [class*="product-card"], [class*="tile"]');
var seen = {};
var productUrls = [];
for (var i = 0; i < cards.length; i++) {
    var link = cards[i].querySelector('a[href]');
    if (!link) continue;
    var href = link.getAttribute('href');
    if (!href || seen[href]) continue;
    // Skip non-product links
    if (href.indexOf('/search') !== -1) continue;
    if (href.indexOf('/cart') !== -1) continue;
    if (href.indexOf('/account') !== -1) continue;
    if (href.indexOf('/wishlist') !== -1) continue;
    if (href.indexOf('#') === 0) continue;
    if (href.indexOf('javascript:') === 0) continue;
    seen[href] = true;
    if (href.indexOf('http') !== 0) {
        href = window.location.origin + href;
    }
    productUrls.push(href);
}
return productUrls;
"""

CLICK_LOAD_MORE_JS = """
var selectors = [
    'button[class*="load-more" i]',
    'a[class*="load-more" i]',
    'button[class*="show-more" i]',
    'a[class*="show-more" i]',
    '[data-testid="load-more"]',
    '[class*="LoadMore"]',
    'button[class*="more" i]'
];
for (var i = 0; i < selectors.length; i++) {
    var btn = document.querySelector(selectors[i]);
    if (btn) {
        btn.click();
        return true;
    }
}
return false;
"""


def warmup_session(driver: Any) -> bool:
    logger.info(f"Warming up session: visiting {SITE_URL}")
    driver.uc_open_with_reconnect(SITE_URL, reconnect_time=UC_RECONNECT_TIME)
    logger.info(f"Waiting {WARMUP_WAIT}s for anti-bot sensors...")
    time.sleep(WARMUP_WAIT)

    block_type = driver.execute_script(DETECT_BLOCK_JS)
    if block_type:
        logger.error(f"{block_type.upper()} BLOCK DETECTED during warm-up")
        return False

    try:
        clicked = driver.execute_script(ACCEPT_COOKIES_JS)
        if clicked:
            logger.info("Accepted cookies")
            time.sleep(2)
    except Exception as e:
        logger.warning(f"Cookie accept error (non-fatal): {e}")

    logger.info("Warm-up complete")
    return True


def is_404_page(page_title: str, page_source: str) -> bool:
    title_upper = page_title.upper()
    if "CANNOT BE FOUND" in title_upper or "PAGE NOT FOUND" in title_upper:
        return True
    body_match = re.search(
        r"<body[^>]*>(.*?)</body>", page_source, re.DOTALL | re.IGNORECASE
    )
    if body_match:
        body_html = body_match.group(1)
        body_html_no_scripts = re.sub(
            r"<script[^>]*>.*?</script>", "", body_html, flags=re.DOTALL | re.IGNORECASE
        )
        body_text = re.sub(r"<[^>]+>", " ", body_html_no_scripts).upper()
        for phrase in (
            "CANNOT BE FOUND",
            "PAGE NOT FOUND",
            "NO LONGER AVAILABLE",
            "DISCONTINUED",
            "PRODUCT NOT FOUND",
            "ITEM NOT FOUND",
        ):
            if phrase in body_text:
                return True
    return False


# ---------------------------------------------------------------------------
# Phase 1: Discover product URLs from search results
# ---------------------------------------------------------------------------
def discover_product_urls(search_criteria: Optional[str] = None) -> list[str]:
    """Navigate search pages and extract all product URLs via page-based pagination."""
    criteria = search_criteria or SEARCH_CRITERIA
    base_url = f"{SITE_URL}/search?searchTerm={criteria}"
    logger.info(f"Phase 1: Discovering product URLs from {base_url}")

    with SB(uc=True, xvfb=True) as sb:
        driver = sb.driver
        driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)

        if not warmup_session(driver):
            logger.error("Warm-up failed. Cannot discover products.")
            return []

        all_urls: list[str] = []

        for page_num in range(1, 20):
            sep = "&" if "?" in base_url else "?"
            page_url = f"{base_url}{sep}page={page_num}" if page_num > 1 else base_url
            logger.info(f"  Loading page {page_num}: {page_url}")

            driver.uc_open_with_reconnect(page_url, reconnect_time=UC_RECONNECT_TIME)
            time.sleep(5)

            block_type = driver.execute_script(DETECT_BLOCK_JS)
            if block_type:
                logger.error(f"{block_type.upper()} BLOCK on search page {page_num}")
                break

            urls = driver.execute_script(DISCOVER_PRODUCTS_JS) or []
            new_urls = [u for u in urls if u not in all_urls]

            if new_urls:
                all_urls.extend(new_urls)
                logger.info(
                    f"  Page {page_num}: found {len(new_urls)} new URLs (total: {len(all_urls)})"
                )
            else:
                logger.info(f"  Page {page_num}: no new URLs, stopping")
                break

            if len(all_urls) >= DISCOVER_MAX_PRODUCTS:
                logger.info(f"  Reached max product limit ({DISCOVER_MAX_PRODUCTS})")
                break

            time.sleep(DELAY_BETWEEN_REQUESTS)

        all_unique = list(dict.fromkeys(all_urls))
        logger.info(f"Discovery complete: {len(all_unique)} unique product URLs")
        return all_unique


# ---------------------------------------------------------------------------
# Phase 2: Extract product data from each URL
# ---------------------------------------------------------------------------
def extract_product_from_url(url: str, src_url: str, index: int) -> Product:
    """Open a fresh UC session per product to avoid Cloudflare blocking."""
    product = Product(
        id=index,
        url=url,
        src_url=src_url,
        scraped_at=datetime.now(timezone.utc).isoformat(),
        brand=SITE_NAME,
        currency="GBP",
    )

    with SB(uc=True, xvfb=True) as sb:
        driver = sb.driver
        driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)

        try:
            driver.uc_open_with_reconnect(url, reconnect_time=UC_RECONNECT_TIME)
        except Exception as e:
            logger.error(f"  [{index}] Navigation failed for {url}: {e}")
            product.remarks = f"Navigation error: {e}"
            product.status_code = 0
            return product

        time.sleep(5)

        try:
            block_type = driver.execute_script(DETECT_BLOCK_JS)
            if block_type:
                logger.warning(f"  [{index}] {block_type.upper()} block on {url}")
                product.status_code = 403
                product.remarks = f"{block_type.upper()} BLOCKED"
                return product
        except Exception:
            pass

        try:
            final_url = driver.current_url
            product.url = final_url
        except Exception:
            final_url = url

        try:
            page_source = driver.page_source
        except Exception as e:
            logger.error(f"  [{index}] Failed to get page source: {e}")
            product.remarks = f"Failed to get page source: {e}"
            product.status_code = 200
            return product

        page_title = ""
        try:
            page_title = driver.execute_script("return document.title || '';")
        except Exception:
            pass

        if is_404_page(page_title, page_source):
            logger.warning(f"  [{index}] Soft 404: {url}")
            product.remarks = f"Soft 404: product not found (title: {page_title})"
            product.status_code = 200
            return product

        product.status_code = 200

        jsonld_blocks = extract_jsonld_from_html(page_source)
        jsonld_product = find_jsonld_product(jsonld_blocks)
        jsonld_breadcrumb = find_jsonld_breadcrumbs(jsonld_blocks)

        if jsonld_product:
            product.title = jsonld_product.get("name", "") or product.title

            brand_data = jsonld_product.get("brand", {})
            if isinstance(brand_data, dict) and brand_data.get("name"):
                product.brand = brand_data["name"]
            else:
                product.brand = SITE_NAME

            product.sku = jsonld_product.get("sku", "") or product.sku
            product.description = clean_html(jsonld_product.get("description", ""))

            img_data = jsonld_product.get("image", [])
            if isinstance(img_data, str):
                img_data = [img_data]
            if img_data and not product.images:
                product.images = [img for img in img_data if isinstance(img, str)]

            offers = None
            for key in jsonld_product:
                if key.lower() == "offers":
                    offers = jsonld_product[key]
                    break
            if offers:
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                if isinstance(offers, dict):
                    offer_price = offers.get("price", "")
                    if offer_price and offer_price != "{}":
                        try:
                            price_num = float(offer_price)
                            product.currency = (
                                offers.get("priceCurrency", "GBP") or "GBP"
                            )
                            product.price = format_price_with_symbol(
                                f"{price_num:,.2f}", product.currency
                            )
                        except (ValueError, TypeError):
                            product.price = offer_price

                    if not product.currency:
                        product.currency = offers.get("priceCurrency", "GBP") or "GBP"

                    availability = offers.get("availability", "")
                    if isinstance(availability, str):
                        if "InStock" in availability:
                            product.availability = "In Stock"
                        elif "OutOfStock" in availability or "SoldOut" in availability:
                            product.availability = "Out of Stock"
                        elif "PreOrder" in availability:
                            product.availability = "In Stock"
                        elif "LimitedAvailability" in availability:
                            product.availability = "Low Stock"
                        else:
                            product.availability = availability

                    high_price = offers.get("highPrice", "")
                    if high_price:
                        try:
                            product.original_price = format_price_with_symbol(
                                f"{float(high_price):,.2f}", product.currency or "GBP"
                            )
                        except (ValueError, TypeError):
                            pass
                    elif offers.get("lowPrice") and offers.get("highPrice"):
                        try:
                            low = float(offers["lowPrice"])
                            high = float(offers["highPrice"])
                            if high > low:
                                product.original_price = format_price_with_symbol(
                                    f"{high:,.2f}", product.currency or "GBP"
                                )
                        except (ValueError, TypeError):
                            pass

        if not product.category:
            product.category = extract_breadcrumb_category(jsonld_breadcrumb)

        try:
            if not product.original_price and product.price:
                was_price = driver.execute_script("""
                var wasEl = document.querySelector('[class*="wasPrice"], [class*="was-price"], [class*="originalPrice"], s, del');
                if (wasEl) {
                    var t = wasEl.textContent.trim();
                    if (t.match(/[\\d]/)) return t;
                }
                return '';
                """)
                if was_price:
                    was_clean = was_price.strip().split()[0]
                    try:
                        orig_num = float(re.sub(r"[^\d.]", "", was_clean))
                        sale_num = float(re.sub(r"[^\d.]", "", product.price))
                        if orig_num > sale_num:
                            product.original_price = format_price_with_symbol(
                                f"{orig_num:,.2f}", product.currency
                            )
                    except (ValueError, TypeError):
                        pass

            if not product.title:
                title_el = driver.execute_script("""
                var selectors = [
                    'h1.product-name',
                    'h1',
                    '[data-product-id] .product-name',
                    '[class*="product-title"]',
                    'meta[property="og:title"]'
                ];
                for (var i = 0; i < selectors.length; i++) {
                    var el = document.querySelector(selectors[i]);
                    if (el) {
                        var t = el.getAttribute('content') || el.textContent.trim();
                        if (t) return t;
                    }
                }
                return '';
                """)
                if title_el:
                    product.title = title_el

            if not product.price:
                price_text = driver.execute_script("""
                var selectors = [
                    '.product-price .value',
                    '[data-product-id] .price .sales',
                    '.price .sales',
                    '.product-price',
                    '[class*="price"]',
                    '[class*="Price"]'
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

            if not product.original_price:
                orig_text = driver.execute_script("""
                var selectors = [
                    '.product-price .standard',
                    '[data-product-id] .price .standard',
                    '.price .standard',
                    '[class*="compare"]',
                    '[class*="strike"]',
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
                        product.currency = detect_currency_from_price(
                            product.original_price
                        )

            if not product.availability:
                avail_text = driver.execute_script("""
                var selectors = [
                    '.product-availability',
                    '[data-product-in-stock]',
                    '[class*="availability"]',
                    '[class*="stock"]',
                    '[class*="add-to-cart"]',
                    '[class*="add-to-bag"]'
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
                    elif any(
                        kw in avail_text.lower()
                        for kw in ("add to", "available", "shop now")
                    ):
                        product.availability = "In Stock"
                    elif any(
                        kw in avail_text.lower()
                        for kw in ("out of", "sold out", "unavailable")
                    ):
                        product.availability = "Out of Stock"
                    else:
                        product.availability = avail_text

            if not product.description:
                desc_text = driver.execute_script("""
                var selectors = [
                    '.product-description .value',
                    '[data-product-id] .product-detail-description',
                    '.product-detail-content',
                    '.product-description',
                    '[class*="description"]'
                ];
                for (var i = 0; i < selectors.length; i++) {
                    var el = document.querySelector(selectors[i]);
                    if (el && el.textContent.trim().length > 10) return el.textContent.trim();
                }
                return '';
                """)
                if desc_text:
                    product.description = desc_text

            if not product.sku:
                sku_text = driver.execute_script("""
                var selectors = [
                    '[data-pid]',
                    '[data-product-id]',
                    'input[name="pid"]',
                    '[class*="sku"]',
                    '[class*="product-code"]'
                ];
                for (var i = 0; i < selectors.length; i++) {
                    var el = document.querySelector(selectors[i]);
                    if (el) {
                        var t = el.getAttribute('data-pid') || el.getAttribute('data-product-id') || el.getAttribute('value') || el.textContent.trim();
                        if (t) return t;
                    }
                }
                return '';
                """)
                if sku_text:
                    product.sku = sku_text

            if not product.category:
                cat_text = driver.execute_script("""
                var selectors = [
                    '.breadcrumb',
                    'nav[aria-label="breadcrumb"]',
                    '[class*="breadcrumb"]'
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

            if not product.images:
                dom_images = driver.execute_script("""
                var gallerySelectors = [
                    '.product-images img',
                    '[data-product-id] img',
                    '.primary-image img',
                    '[class*="gallery"] img',
                    '[class*="carousel"] img',
                    '[class*="product-image"] img'
                ];
                var seen = {};
                var imgs = [];
                for (var s = 0; s < gallerySelectors.length; s++) {
                    var els = document.querySelectorAll(gallerySelectors[s]);
                    for (var i = 0; i < els.length; i++) {
                        var src = els[i].getAttribute('src') || els[i].getAttribute('data-src') || '';
                        if (src && !seen[src]) {
                            if (src.indexOf('data:image') === 0) continue;
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

    if not product.sku:
        product.sku = extract_sku_from_url(final_url)
    if not product.currency:
        product.currency = "GBP"
    if product.price:
        product.price = format_price_with_symbol(product.price, product.currency)
    if product.original_price:
        product.original_price = format_price_with_symbol(
            product.original_price, product.currency
        )

    return product


def load_urls_from_file(filepath: str) -> list[str]:
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("urls", [])


def save_urls_to_file(urls: list[str], filepath: str) -> None:
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump({"urls": urls}, f, indent=2, ensure_ascii=False)
    logger.info(f"Saved {len(urls)} URLs to {filepath}")


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
        "--discover", action="store_true", help="Phase 1 only: discover product URLs"
    )
    parser.add_argument(
        "--scrape", action="store_true", help="Phase 2 only: scrape saved URLs"
    )
    parser.add_argument(
        "--search", type=str, default=None, help="Search criteria (default: watches)"
    )
    args = parser.parse_args()

    start_time = time.time()

    logger.info("=" * 80)
    logger.info(f"Starting scraper for {SITE_NAME}")
    logger.info(f"Site: {SITE_URL}")
    logger.info(f"Platform: {PLATFORM}")
    logger.info(f"Scraping method: {SCRAPING_METHOD}")
    logger.info(f"Output: {OUTPUT_FILE}")
    logger.info("=" * 80)

    if args.discover:
        urls = discover_product_urls(search_criteria=args.search)
        if urls:
            save_urls_to_file(urls, INPUT_FILE)
        return

    product_urls: list[str] = []

    if args.urls:
        product_urls = args.urls
    elif args.input:
        product_urls = load_urls_from_file(args.input)
    elif os.path.exists(INPUT_FILE):
        product_urls = load_urls_from_file(INPUT_FILE)
    else:
        logger.info("No input_urls.json found. Running Phase 1: discovery...")
        urls = discover_product_urls(search_criteria=args.search)
        if not urls:
            logger.error("No product URLs discovered.")
            sys.exit(1)
        save_urls_to_file(urls, INPUT_FILE)
        product_urls = urls

    if args.sample:
        product_urls = product_urls[:5]
    if args.limit:
        product_urls = product_urls[: args.limit]

    logger.info(f"Phase 2: Scraping {len(product_urls)} products")

    results: list[dict[str, Any]] = []
    failed = 0

    for i, url in enumerate(product_urls):
        logger.info(f"  [{i + 1}/{len(product_urls)}] Scraping: {url}")
        try:
            product = extract_product_from_url(url, url, i + 1)

            if product.title:
                results.append(product.to_dict())
                logger.info(
                    f"  [{i + 1}/{len(product_urls)}] OK {product.title[:60]} "
                    f"-- {product.price} | {product.availability or 'N/A'}"
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

        if (i + 1) % 10 == 0:
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
