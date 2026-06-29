#!/usr/bin/env python3
"""
Calvin Klein UK Product Scraper

Two-phase architecture using SeleniumBase UC Mode:
  Phase 1: Discover product URLs via search (/search?searchTerm=watches)
  Phase 2: Scrape each discovered product page (per-page fresh session)

Strategy: seleniumbase_uc (no proxy)
Platform: Salesforce Commerce Cloud (SFCC)
Currency: GBP (£)

Usage:
    python3 scraper.py                          # search & scrape "watches"
    python3 scraper.py --search-term "jeans"    # custom search term
    python3 scraper.py --input urls.json        # skip discovery, scrape provided URLs
    python3 scraper.py --urls url1 url2         # scrape specific product URLs
    python3 scraper.py --sample                 # scrape only 5 products
    python3 scraper.py --limit 10               # max 10 products
    python3 scraper.py --no-proxy               # explicit no-proxy flag
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
PROJECT_ROOT = os.environ.get("PROJECT_ROOT", os.path.join(SCRIPT_DIR, ".."))

SITE_NAME = "Calvin Klein United Kingdom Official Store"
SITE_URL = "https://www.calvinklein.co.uk"
PLATFORM = "sfcc"
SCRAPING_METHOD = "seleniumbase_uc"
SITE_SLUG = "calvinklein-co-uk"
CURRENCY = "GBP"

SEARCH_TERM = "watches"
SEARCH_URL = f"{SITE_URL}/search?searchTerm={SEARCH_TERM}"
DELAY_BETWEEN_REQUESTS = 3.0
UC_RECONNECT_TIME = 4
MAX_DISCOVERY_PAGES = 20

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

# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------


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
    brand: str = ""
    sku: str = ""
    description: str = ""
    image: str = ""
    color: str = ""
    mpn: str = ""
    category: str = ""
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
            "brand", "sku", "description", "image", "color", "mpn", "category", "images"
        ]
        for fld in optional_fields:
            val = getattr(self, fld)
            if val:
                result[fld] = val
        return result


# ---------------------------------------------------------------------------
# JavaScript Snippets
# ---------------------------------------------------------------------------

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

DETECT_BLOCK_JS = """
var bodyText = document.body ? document.body.innerText.toUpperCase() : '';
if (bodyText.indexOf('UNFORTUNATELY WE ARE UNABLE TO GIVE YOU ACCESS') !== -1) return 'akamai';
if (bodyText.indexOf('JUST A MOMENT') !== -1) return 'cloudflare';
if (bodyText.indexOf('ACCESS DENIED') !== -1) return 'generic';
if (bodyText.indexOf('BLOCKED') !== -1) return 'generic';
return null;
"""

SOFT_404_CHECK_JS = """
var checks = {
    title: document.title || '',
    bodyText: document.body ? document.body.innerText : '',
    url: window.location.href
};
var bodyUpper = checks.bodyText.toUpperCase();
if (bodyUpper.indexOf('PRODUCT NOT FOUND') !== -1) return 'Soft 404: product not found';
if (bodyUpper.indexOf('NO LONGER AVAILABLE') !== -1) return 'Soft 404: no longer available';
if (bodyUpper.indexOf('PAGE NOT FOUND') !== -1) return 'Soft 404: page not found';
if (bodyUpper.indexOf('UNAVAILABLE') !== -1 && bodyUpper.indexOf('PRODUCT') !== -1) return 'Soft 404: product unavailable';
var titleUpper = checks.title.toUpperCase();
if (titleUpper.indexOf('NOT FOUND') !== -1) return 'Soft 404: not found in title';
if (titleUpper.indexOf('UNAVAILABLE') !== -1) return 'Soft 404: unavailable in title';
return null;
"""

# Phase 1: Extract product links from a search/listing page
EXTRACT_PRODUCT_LINKS_JS = """
var links = document.querySelectorAll(
    'a[href*="/watch"], a[href*="-wf"], a[href*="-wm"], a[href*="-wu"]'
);
var seen = {};
var results = [];
var origin = window.location.origin;
for (var i = 0; i < links.length; i++) {
    var href = links[i].getAttribute('href');
    if (href && !seen[href]) {
        seen[href] = true;
        if (href.indexOf('http') !== 0) {
            href = origin + href;
        }
        // Filter: must look like a product URL (contains slug-w<sku>)
        var match = href.match(/[A-Za-z0-9-]+-w[A-Za-z0-9]+$/);
        if (match) {
            results.push(href);
        }
    }
}
return results;
"""

# Phase 1: Click next page button, returns true if clicked
CLICK_NEXT_PAGE_JS = """
var nextBtns = document.querySelectorAll(
    'a[aria-label="Next"], a[aria-label="next"], button[aria-label="Next"], ' +
    'a.next, button.next, a[data-auto-id="next-page"], ' +
    '.pagination a:last-child, .pager a:last-child, ' +
    'a[href*="page="], a[href*="start="], ' +
    'li.next a, li.next button'
);
for (var i = 0; i < nextBtns.length; i++) {
    var el = nextBtns[i];
    // Skip if disabled
    if (el.classList.contains('disabled') || el.classList.contains('is-disabled')) continue;
    if (el.getAttribute('disabled')) continue;
    if (el.getAttribute('aria-disabled') === 'true') continue;
    // Check if it looks like a real next button
    var text = (el.textContent || '').trim().toLowerCase();
    if (text.indexOf('next') !== -1 || text.indexOf('>') !== -1 || text.indexOf('\\u203a') !== -1) {
        el.click();
        return true;
    }
}
return false;
"""

# Phase 2: Extract product data via JSON-LD + DOM fallbacks
EXTRACT_PRODUCT_DATA_JS = """
var srcUrl = arguments[0] || '';
var product = {
    title: '',
    price: '',
    availability: '',
    original_price: '',
    currency: '',
    url: window.location.href,
    src_url: srcUrl,
    remarks: '',
    brand: '',
    sku: '',
    description: '',
    image: '',
    color: '',
    mpn: '',
    category: '',
    images: []
};

// --- JSON-LD extraction ---
var jsonld = null;
var breadcrumb = null;
var scripts = document.querySelectorAll('script[type="application/ld+json"]');
for (var i = 0; i < scripts.length; i++) {
    try {
        var data = JSON.parse(scripts[i].textContent);
        var items = Array.isArray(data) ? data : [data];
        for (var j = 0; j < items.length; j++) {
            if (!jsonld && (items[j]['@type'] === 'Product' || items[j]['@type'] === 'ProductGroup')) {
                jsonld = items[j];
            }
            if (!breadcrumb && items[j]['@type'] === 'BreadcrumbList') {
                breadcrumb = items[j];
            }
        }
    } catch(e) {}
}

// Extract JSON-LD Product fields
if (jsonld) {
    product.title = jsonld.name || '';
    var rawBrand = (jsonld.brand && (jsonld.brand.name || jsonld.brand)) || '';
    // Title-case the brand: "CALVIN KLEIN" -> "Calvin Klein"
    if (rawBrand) {
        product.brand = rawBrand.toLowerCase().replace(/\\b\\w/g, function(c) { return c.toUpperCase(); });
    }
    product.sku = jsonld.sku || '';
    product.mpn = jsonld.mpn || '';
    product.color = jsonld.color || '';

    // Description - clean HTML
    if (jsonld.description) {
        var tmp = document.createElement('div');
        tmp.innerHTML = jsonld.description;
        product.description = tmp.textContent.trim();
    }

    // Image
    if (jsonld.image) {
        if (Array.isArray(jsonld.image)) {
            product.image = jsonld.image[0] || '';
            product.images = jsonld.image.filter(function(img) {
                return img && img.indexOf('data:') !== 0;
            });
        } else {
            product.image = jsonld.image;
            product.images = [jsonld.image];
        }
    }

    // Offers (known to be EMPTY on this site, but try anyway)
    if (jsonld.offers && jsonld.offers.price) {
        var offers = Array.isArray(jsonld.offers) ? jsonld.offers[0] : jsonld.offers;
        product.price = offers.price || '';
        var highPrice = offers.highPrice || '';
        if (highPrice && parseFloat(highPrice) > parseFloat(product.price || '0')) {
            product.original_price = highPrice;
        }
        product.currency = offers.priceCurrency || '';
        var avail = offers.availability || '';
        product.availability = avail.indexOf('InStock') !== -1 ? 'In Stock' : 'Out of Stock';
    }
}

// Breadcrumb category - handle multiple possible structures
if (breadcrumb && breadcrumb.itemListElement) {
    var cats = [];
    var elements = breadcrumb.itemListElement;
    for (var k = 0; k < elements.length; k++) {
        var name = '';
        // Standard: { item: { name: "..." } }
        if (elements[k].item && elements[k].item.name) {
            name = elements[k].item.name;
        // Alternative: { name: "..." } directly
        } else if (elements[k].name) {
            name = elements[k].name;
        // Alternative: { item: { @id: "...", name: "..." } } via @graph
        } else if (elements[k].item && typeof elements[k].item === 'string') {
            name = elements[k].item;
        }
        if (name && name !== 'Home' && name !== 'Home page') {
            cats.push(name);
        }
    }
    product.category = cats.join(' > ');
}

// --- Title fallback: parse page title ---
if (!product.title) {
    var titleText = document.title || '';
    var idx = titleText.indexOf(' Calvin Klein');
    if (idx !== -1) {
        product.title = titleText.substring(0, idx).trim();
    } else {
        product.title = titleText.split('|')[0].split('\\u2014')[0].trim();
    }
}

// --- Price extraction (DOM - required because JSON-LD offers is empty) ---
// We collect raw price text and parse it to handle dual-price sale items
var rawPriceText = '';
if (!product.price) {
    var priceSelectors = [
        '[data-price]',
        '[data-testid*="price"]',
        '[class*="price-value"]',
        '[class*="Price"]',
        '[class*="sales"] [class*="value"]',
        '.product-price .price-sales',
        '[itemprop="price"]',
        'span.price',
        '.product-detail .price'
    ];
    for (var p = 0; p < priceSelectors.length; p++) {
        var el = document.querySelector(priceSelectors[p]);
        if (el) {
            var text = el.textContent.trim();
            if (text && text.indexOf('\\u00a3') !== -1) {
                rawPriceText = text;
                break;
            }
        }
    }
}

// Price regex fallback: find pound-prefixed prices in visible text
if (!rawPriceText) {
    var allEls = document.querySelectorAll('[class*="price"], [class*="Price"], span, div');
    var priceRe = /^\\u00a3[\\d,]+(\\.\\d{2})?$/;
    for (var q = 0; q < allEls.length; q++) {
        var t = allEls[q].textContent.trim();
        var children = allEls[q].children.length;
        if (children === 0 && priceRe.test(t)) {
            rawPriceText = t;
            break;
        }
    }
    // If still not found, search for pound pattern in body text
    if (!rawPriceText) {
        var bodyText = document.body ? document.body.innerText : '';
        var poundMatch = bodyText.match(/\\u00a3[\\d,]+(?:\\.\\d{2})/);
        if (poundMatch) {
            rawPriceText = poundMatch[0];
        }
    }
}

// Parse price text: extract all pound-prefixed monetary values
// For sale items, there will be TWO prices (e.g. "\\u00a3139.00 \\u00a397.00")
// The FIRST is the original price, the LAST is the current/sale price
if (rawPriceText) {
    var allPrices = rawPriceText.match(/\\u00a3[\\d,]+(?:\\.\\d{2})/g);
    if (allPrices && allPrices.length >= 2) {
        // Multiple prices: first = original, last = current/sale
        product.original_price = allPrices[0];
        product.price = allPrices[allPrices.length - 1];
    } else if (allPrices && allPrices.length === 1) {
        product.price = allPrices[0];
    } else {
        product.price = rawPriceText.trim();
    }
}

// Normalize price: ensure pound prefix
if (product.price) {
    product.price = product.price.trim();
    if (product.price.indexOf('\\u00a3') !== 0 && !isNaN(parseFloat(product.price.replace(/,/g, '')))) {
        product.price = '\\u00a3' + product.price;
    }
}

// --- Original price (sale / compare-at) --- only if not already set from dual-price parsing
if (!product.original_price) {
    var origSelectors = [
        '[class*="compare"]',
        '[class*="was"]',
        '[class*="strike"]',
        '[class*="original"]',
        '.price--crossed',
        's',
        'div.price span.standard span.value'
    ];
    for (var r = 0; r < origSelectors.length; r++) {
        var origEl = document.querySelector(origSelectors[r]);
        if (origEl) {
            var origText = origEl.textContent.trim();
            var origMatch = origText.match(/\\u00a3[\\d,]+(?:\\.\\d{2})/);
            if (origMatch) {
                product.original_price = origMatch[0];
                break;
            }
        }
    }
}
if (product.original_price) {
    product.original_price = product.original_price.trim();
    if (product.original_price.indexOf('\\u00a3') !== 0) {
        product.original_price = '\\u00a3' + product.original_price;
    }
}

// --- Availability ---
if (!product.availability) {
    var availSelectors = [
        '[class*="availability"]',
        '[class*="stock"]',
        '[class*="inventory"]',
        '[data-available]',
        '[data-in-stock]'
    ];
    for (var a = 0; a < availSelectors.length; a++) {
        var availEl = document.querySelector(availSelectors[a]);
        if (availEl) {
            var availText = availEl.textContent.trim().toLowerCase();
            if (availText) {
                if (availText.indexOf('in stock') !== -1 || availText.indexOf('available') !== -1) {
                    product.availability = 'In Stock';
                } else if (availText.indexOf('out of stock') !== -1 || availText.indexOf('unavailable') !== -1) {
                    product.availability = 'Out of Stock';
                } else {
                    product.availability = availText.charAt(0).toUpperCase() + availText.slice(1);
                }
                break;
            }
        }
    }
}
if (!product.availability) {
    // Default to In Stock if no selector found and page has price
    product.availability = product.price ? 'In Stock' : 'Out of Stock';
}

product.currency = 'GBP';

return product;
"""

# Extract gallery images scoped to product gallery containers
EXTRACT_GALLERY_IMAGES_JS = """
var images = [];
var gallerySelectors = [
    '[data-auto-id="product-image"]',
    '.product-gallery',
    '#pdp-gallery',
    '[data-testid*="gallery"]',
    '[data-auto-id*="gallery"]',
    '[class*="product-image"]',
    '[class*="ProductImage"]',
    '[class*="pdp-gallery"]',
    '[class*="carousel"]'
];

var galleries = [];
for (var g = 0; g < gallerySelectors.length; g++) {
    var els = document.querySelectorAll(gallerySelectors[g]);
    for (var h = 0; h < els.length; h++) {
        galleries.push(els[h]);
    }
}

var imgSrcs = {};
if (galleries.length > 0) {
    for (var i = 0; i < galleries.length; i++) {
        var imgs = galleries[i].querySelectorAll('img');
        for (var j = 0; j < imgs.length; j++) {
            var src = imgs[j].getAttribute('src') || imgs[j].getAttribute('data-src') || '';
            if (src && src.indexOf('data:') !== 0 && !imgSrcs[src]) {
                imgSrcs[src] = true;
                images.push(src);
            }
        }
    }
} else {
    // Fallback: all images with product SKU pattern in URL
    var allImgs = document.querySelectorAll('img[src]');
    for (var k = 0; k < allImgs.length; k++) {
        var s = allImgs[k].getAttribute('src');
        if (s && s.indexOf('data:') === 0) continue;
        if (s.indexOf('/brand.assets/') !== -1) continue;
        if (s.indexOf('/emoji/') !== -1) continue;
        if (s.indexOf('/flags/') !== -1) continue;
        if (s.indexOf('/icon/') !== -1) continue;
        if (s.indexOf('/navigation/') !== -1) continue;
        if (s.indexOf('logo') !== -1) continue;
        if (s.indexOf('sprite') !== -1) continue;
        if (s.indexOf('pixel') !== -1) continue;
        if (!imgSrcs[s]) {
            imgSrcs[s] = true;
            images.push(s);
        }
    }
}
return images;
"""


# ---------------------------------------------------------------------------
# Utility Functions
# ---------------------------------------------------------------------------


def is_valid_product_url(url: str) -> bool:
    """Check if a URL matches the product URL pattern."""
    pattern = r"/[A-Za-z0-9-]+-w[A-Za-z0-9]+$"
    return bool(re.search(pattern, url))


def normalize_brand(brand: str) -> str:
    """Normalize brand to title-case: 'CALVIN KLEIN' -> 'Calvin Klein'."""
    if not brand:
        return ""
    return brand.strip().title()


def load_urls_from_file(filepath: str) -> list[str]:
    """Load URLs from a JSON file."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("urls", [])
    except Exception as e:
        logger.error(f"Failed to load URLs from {filepath}: {e}")
        return []


def save_urls_to_file(filepath: str, urls: list[str]) -> None:
    """Save discovered URLs to a JSON file."""
    os.makedirs(os.path.dirname(filepath) if os.path.dirname(filepath) else ".", exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump({"urls": urls}, f, indent=2, ensure_ascii=False)
    logger.info(f"Saved {len(urls)} URLs to {filepath}")


# ---------------------------------------------------------------------------
# Session Management
# ---------------------------------------------------------------------------


def make_sb_kwargs(xvfb: bool = False) -> dict[str, Any]:
    """Build SeleniumBase UC Mode kwargs (no proxy for this site)."""
    kwargs: dict[str, Any] = {
        "uc": True,
        "locale_code": "en-gb",
    }
    if xvfb:
        kwargs["xvfb"] = True
    return kwargs


def open_page(driver, url: str, reconnect_time: int = UC_RECONNECT_TIME) -> bool:
    """Navigate to a URL using UC Mode's reconnect logic.

    Returns True if the page loaded successfully, False if blocked/redirected.
    """
    try:
        driver.uc_open_with_reconnect(url, reconnect_time=reconnect_time)
        time.sleep(3)

        # Check for anti-bot blocks
        block_type = driver.execute_script(DETECT_BLOCK_JS)
        if block_type:
            logger.warning(f"Block detected on {url}: {block_type}")
            return False

        return True
    except Exception as e:
        logger.error(f"Failed to open {url}: {e}")
        return False


def accept_cookies(driver) -> None:
    """Try to accept cookie consent if a banner is present."""
    try:
        clicked = driver.execute_script(ACCEPT_COOKIES_JS)
        if clicked:
            logger.info("Accepted cookies")
            time.sleep(2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Phase 1: Product URL Discovery
# ---------------------------------------------------------------------------


def discover_product_urls(
    search_url: str,
    xvfb: bool = False,
    max_pages: int = MAX_DISCOVERY_PAGES,
) -> list[str]:
    """Discover product URLs from search results with pagination.

    Uses a single SB session for the discovery phase.
    """
    logger.info("=" * 80)
    logger.info(f"PHASE 1: Discovering product URLs from: {search_url}")
    logger.info("=" * 80)

    all_product_urls: list[str] = []

    try:
        with SB(**make_sb_kwargs(xvfb=xvfb)) as sb:
            driver = sb.driver

            # Warmup: visit homepage first
            logger.info(f"Warming up: visiting {SITE_URL}")
            if not open_page(driver, SITE_URL):
                logger.error("Warm-up failed — homepage blocked")
                return []

            time.sleep(5)
            accept_cookies(driver)

            # Navigate to search page
            logger.info(f"Navigating to search: {search_url}")
            if not open_page(driver, search_url):
                logger.error("Search page failed to load")
                return []

            accept_cookies(driver)
            time.sleep(2)

            for page_num in range(1, max_pages + 1):
                logger.info(f"Discovering page {page_num}...")

                # Extract product links from current page
                try:
                    page_urls = driver.execute_script(EXTRACT_PRODUCT_LINKS_JS) or []
                except Exception as e:
                    logger.error(f"JS error extracting links on page {page_num}: {e}")
                    page_urls = []

                # Filter to valid product URLs only
                valid_urls = [u for u in page_urls if is_valid_product_url(u)]

                new_count = 0
                for url in valid_urls:
                    if url not in all_product_urls:
                        all_product_urls.append(url)
                        new_count += 1

                logger.info(
                    f"  Page {page_num}: found {len(page_urls)} links, "
                    f"{new_count} new products (total: {len(all_product_urls)})"
                )

                # Try to click next page
                if page_num < max_pages:
                    try:
                        time.sleep(2)
                        driver.execute_script(
                            "window.scrollTo(0, document.body.scrollHeight); return true;"
                        )
                        time.sleep(1)

                        clicked = driver.execute_script(CLICK_NEXT_PAGE_JS)
                        if not clicked:
                            logger.info("No next page button found or end of results")
                            break

                        # Wait for new page to load
                        time.sleep(3)
                    except Exception as e:
                        logger.info(f"Pagination ended or error: {e}")
                        break

    except Exception as e:
        logger.error(f"Discovery session error: {e}")

    logger.info(f"Discovery complete: {len(all_product_urls)} product URLs found")
    return all_product_urls


# ---------------------------------------------------------------------------
# Phase 2: Product Data Extraction
# ---------------------------------------------------------------------------


def scrape_product_page(
    url: str,
    src_url: str,
    index: int,
    xvfb: bool = False,
) -> dict[str, Any]:
    """Scrape a single product page using a fresh SB session.

    Per-page session architecture avoids anti-bot cascading blocks.
    """
    try:
        with SB(**make_sb_kwargs(xvfb=xvfb)) as sb:
            driver = sb.driver

            if not open_page(driver, url):
                return Product(
                    id=index,
                    url=url,
                    src_url=src_url,
                    currency=CURRENCY,
                    status_code=403,
                    scraped_at=datetime.now(timezone.utc).isoformat(),
                    remarks="Redirect/block on initial page load",
                ).to_dict()

            accept_cookies(driver)
            time.sleep(2)

            # Soft 404 detection
            soft_404 = driver.execute_script(SOFT_404_CHECK_JS)
            if soft_404:
                logger.warning(f"  [{index}] Soft 404 detected: {soft_404}")
                return Product(
                    id=index,
                    url=url,
                    src_url=src_url,
                    currency=CURRENCY,
                    status_code=200,
                    scraped_at=datetime.now(timezone.utc).isoformat(),
                    remarks=soft_404,
                ).to_dict()

            # Check if JSON-LD has a Product type (if not, likely not a product page)
            has_product_jsonld = driver.execute_script("""
                var scripts = document.querySelectorAll('script[type="application/ld+json"]');
                for (var i = 0; i < scripts.length; i++) {
                    try {
                        var data = JSON.parse(scripts[i].textContent);
                        var items = Array.isArray(data) ? data : [data];
                        for (var j = 0; j < items.length; j++) {
                            if (items[j]['@type'] === 'Product' || items[j]['@type'] === 'ProductGroup') {
                                return true;
                            }
                        }
                    } catch(e) {}
                }
                return false;
            """)

            if not has_product_jsonld:
                return Product(
                    id=index,
                    url=url,
                    src_url=src_url,
                    currency=CURRENCY,
                    status_code=200,
                    scraped_at=datetime.now(timezone.utc).isoformat(),
                    remarks="Soft 404: no JSON-LD Product found on page",
                ).to_dict()

            # Check if final URL matches requested URL
            final_url = driver.execute_script("return window.location.href;")
            if final_url and final_url != url:
                if not is_valid_product_url(final_url):
                    return Product(
                        id=index,
                        url=url,
                        src_url=src_url,
                        currency=CURRENCY,
                        status_code=200,
                        scraped_at=datetime.now(timezone.utc).isoformat(),
                        remarks=f"Soft 404: redirected to non-product URL {final_url}",
                    ).to_dict()

            # Extract product data via hybrid JS
            data = driver.execute_script(EXTRACT_PRODUCT_DATA_JS, src_url) or {}

            # Extract gallery images
            gallery_images = driver.execute_script(EXTRACT_GALLERY_IMAGES_JS) or []

            # Post-processing: normalize brand to title-case
            brand_raw = data.get("brand", "") or ""
            brand_normalized = normalize_brand(brand_raw)

            # Post-processing: validate price format
            price_raw = data.get("price", "") or ""

            product = Product(
                id=index,
                title=data.get("title", "") or "",
                price=price_raw,
                availability=data.get("availability", "") or "",
                original_price=data.get("original_price", "") or "",
                currency=data.get("currency", CURRENCY),
                url=data.get("url", url) or url,
                src_url=data.get("src_url", src_url) or src_url,
                status_code=200,
                scraped_at=datetime.now(timezone.utc).isoformat(),
                remarks=data.get("remarks", "") or "",
                brand=brand_normalized,
                sku=data.get("sku", "") or "",
                description=data.get("description", "") or "",
                image=data.get("image", "") or "",
                color=data.get("color", "") or "",
                mpn=data.get("mpn", "") or "",
                category=data.get("category", "") or "",
                images=gallery_images if gallery_images else (data.get("images", []) or []),
            )

            # Validate extracted data
            if not product.title:
                product.remarks = (
                    product.remarks + "; no title found — page may not have loaded"
                ).lstrip("; ")
                product.status_code = 0

            return product.to_dict()

    except Exception as e:
        logger.error(f"  [{index}] Session failed for {url}: {e}")
        return Product(
            id=index,
            url=url,
            src_url=src_url,
            currency=CURRENCY,
            status_code=0,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            remarks=f"Session error: {str(e)[:200]}",
        ).to_dict()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Calvin Klein UK Product Scraper (SeleniumBase UC Mode)"
    )
    parser.add_argument("--sample", action="store_true", help="Scrape only 5 products")
    parser.add_argument("--limit", type=int, default=None, help="Max products to scrape")
    parser.add_argument("--input", type=str, default=None, help="Path to input URLs JSON file")
    parser.add_argument(
        "--urls", nargs="+", default=None, help="Product URLs as CLI arguments"
    )
    parser.add_argument(
        "--search-term", type=str, default=None, help="Custom search term for discovery"
    )
    parser.add_argument(
        "--no-proxy", action="store_true", help="Skip proxy (default for this site)"
    )
    parser.add_argument(
        "--xvfb", action="store_true", help="Use Xvfb virtual display (Docker)"
    )
    args = parser.parse_args()

    start_time = time.time()

    logger.info("=" * 80)
    logger.info(f"Starting scraper for {SITE_NAME}")
    logger.info(f"Site: {SITE_URL}")
    logger.info(f"Platform: {PLATFORM}")
    logger.info(f"Strategy: {SCRAPING_METHOD}")
    logger.info(f"Xvfb: {args.xvfb}")
    logger.info("=" * 80)

    product_urls: list[str] = []
    src_url_for_products = ""

    # Determine the source of product URLs
    if args.urls:
        # Direct product URLs via CLI
        product_urls = [u.strip("\"'") for u in args.urls]
        src_url_for_products = ""
        logger.info(f"Using {len(product_urls)} URLs from CLI arguments")
    elif args.input:
        # Direct product URLs from file
        product_urls = load_urls_from_file(args.input)
        src_url_for_products = ""
        logger.info(f"Using {len(product_urls)} URLs from {args.input}")
    else:
        # Two-phase: discover via search
        search_term = args.search_term or SEARCH_TERM
        search_url = f"{SITE_URL}/search?searchTerm={search_term}"
        product_urls = discover_product_urls(
            search_url=search_url,
            xvfb=args.xvfb,
        )
        src_url_for_products = search_url

        # Save discovered URLs
        save_urls_to_file(INPUT_FILE, product_urls)

    if not product_urls:
        logger.error("No product URLs found — cannot proceed")
        sys.exit(1)

    # Apply limits
    if args.sample and len(product_urls) > 5:
        product_urls = product_urls[:5]
        logger.info("Sample mode: limiting to 5 products")
    if args.limit:
        product_urls = product_urls[: args.limit]

    # Set src_url for products (discovery URL or individual product URL)
    if not src_url_for_products:
        src_url_for_products = product_urls[0]

    logger.info("=" * 80)
    logger.info(f"PHASE 2: Scraping {len(product_urls)} product pages")
    logger.info(f"Architecture: PER-PAGE SESSION (fresh SB() per product)")
    logger.info(f"Rate limit delay: {DELAY_BETWEEN_REQUESTS}s")
    logger.info("=" * 80)

    results: list[dict[str, Any]] = []
    failed_count = 0

    for i, url in enumerate(product_urls):
        try:
            product = scrape_product_page(
                url=url,
                src_url=src_url_for_products,
                index=i + 1,
                xvfb=args.xvfb,
            )

            if product.get("title"):
                results.append(product)
                logger.info(
                    f"  [{i + 1}/{len(product_urls)}] "
                    f"{product.get('title', '')[:60]} — {product.get('price', '')}"
                )
            else:
                failed_count += 1
                results.append(product)
                logger.warning(
                    f"  [{i + 1}/{len(product_urls)}] No title extracted: {url}"
                )

        except Exception as e:
            failed_count += 1
            logger.error(f"  [{i + 1}/{len(product_urls)}] Error scraping {url}: {e}")
            results.append(
                Product(
                    id=i + 1,
                    url=url,
                    src_url=src_url_for_products,
                    currency=CURRENCY,
                    status_code=0,
                    scraped_at=datetime.now(timezone.utc).isoformat(),
                    remarks=f"Unhandled error: {str(e)[:200]}",
                ).to_dict()
            )

        # Progress reporting
        if (i + 1) % 25 == 0 or (i + 1) == len(product_urls):
            percent = ((i + 1) / len(product_urls)) * 100
            logger.info(f"Progress: [{i + 1}/{len(product_urls)}] ({percent:.1f}%)")

        # Rate limiting between products
        if i < len(product_urls) - 1:
            time.sleep(DELAY_BETWEEN_REQUESTS)

    # ---------------------------------------------------------------------------
    # Output
    # ---------------------------------------------------------------------------

    success_count = len([r for r in results if r.get("title")])
    duration = round(time.time() - start_time, 2)

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
            "rate_limit_delay": DELAY_BETWEEN_REQUESTS,
        },
    }

    os.makedirs(
        os.path.dirname(OUTPUT_FILE) if os.path.dirname(OUTPUT_FILE) else ".", exist_ok=True
    )
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    logger.info("=" * 80)
    logger.info("EXTRACTION COMPLETE")
    logger.info(f"Total: {len(results)}, Success: {success_count}, Failed: {failed_count}")
    logger.info(f"Duration: {duration}s")
    logger.info(f"Output: {OUTPUT_FILE}")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
