#!/usr/bin/env python3
"""
Calvin Klein Product Scraper — SeleniumBase UC Mode

Two-phase architecture:
  Phase 1: Discover product URLs from search/category pages via UC Chrome
  Phase 2: Scrape each discovered product page using per-page SB() sessions

Extraction: JSON-LD primary with CSS/JS fallbacks.
Proxy: Bright Data residential tier (escalated from datacenter — datacenter WAF blocks).
Anti-bot: UC Chrome with warmup on homepage, retry logic, and page interactions.

Usage:
    python3 scraper_draft.py                           # search "watches" (default)
    python3 scraper_draft.py --query "jeans"          # custom search query
    python3 scraper_draft.py --category-url "https://www.calvinklein.com/en-us/men-clothing/"
    python3 scraper_draft.py --sample                  # scrape only 5 products
    python3 scraper_draft.py --limit 20               # max 20 products
    python3 scraper_draft.py --input urls.json         # skip discovery, scrape URLs from file
    python3 scraper_draft.py --urls url1 url2 url3     # scrape specific product URLs
    python3 scraper_draft.py --xvfb                    # Xvfb virtual display (Docker)
"""

import argparse
import json
import logging
import os
import re
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse, urljoin

from seleniumbase import SB

# ---------------------------------------------------------------------------
# Paths & Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.environ.get("PROJECT_ROOT", os.path.join(SCRIPT_DIR, ".."))
sys.path.insert(0, PROJECT_ROOT)

from src.proxy import ProxyConfig, should_warn_residential, warn_residential_usage

SITE_NAME = "Calvin Klein"
SITE_URL = "https://www.calvinklein.com/"
PLATFORM = "sfcc"
SCRAPING_METHOD = "seleniumbase_uc"
SITE_SLUG = "calvinklein-com"
CURRENCY_DEFAULT = "USD"

DEFAULT_QUERY = "watches"
SEARCH_URL_BASE = "https://www.calvinklein.com/en-us/search"
HOMEPAGE_URL = "https://www.calvinklein.com/"
# The navigation analysis confirmed products found at this URL
PRODUCTS_URL = "https://www.calvinklein.com/products"

DELAY_BETWEEN_REQUESTS = 3.0
WARMUP_WAIT = 20
UC_RECONNECT_TIME = 12
WARMUP_MAX_RETRIES = 3
MAX_PAGES = 10
MAX_PRODUCTS = 200
# Max time (seconds) to wait for product cards to render on a listing page
DISCOVERY_RENDER_TIMEOUT = 30

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

# SFCC product URL pattern: ends with .html, contains product ID segment
# Calvin Klein product IDs are like K006912055 (letter + digits)
PRODUCT_URL_RE = re.compile(
    r"https?://(?:www\.)?calvinklein\.com/.+/", re.IGNORECASE
)

NON_PRODUCT_FRAGMENTS = [
    "/content/", "/magazine/", "/about-", "/help-", "/customer-service",
    "/stores-", "/careers", "/privacy-", "/terms-", "/gift-card",
    "/wishlist", "/cart", "/checkout", "/account", "/login",
    "/sustainability", "/brand-", "/press-", "/email-signup",
]

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
    description: str = ""
    images: list[str] = field(default_factory=list)
    brand: str = ""
    sku: str = ""
    category: str = ""
    color: str = ""
    rating: str = ""
    review_count: str = ""

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
        for optional_field in (
            "description", "images", "brand", "sku", "category",
            "color", "rating", "review_count",
        ):
            val = getattr(self, optional_field)
            if val:
                result[optional_field] = val
        return result


# ---------------------------------------------------------------------------
# Currency Formatting
# ---------------------------------------------------------------------------

CURRENCY_SYMBOLS = {
    "USD": "$", "EUR": "\u20ac", "GBP": "\u00a3", "CAD": "C$",
    "AUD": "A$", "NZD": "NZ$", "JPY": "\u00a5", "CNY": "\u00a5",
    "HKD": "HK$", "SGD": "S$", "KRW": "\u20a9",
}


def format_price(raw_price: str, currency_code: str) -> str:
    """Format a clean numeric price string with currency symbol."""
    if not raw_price:
        return ""
    symbol = CURRENCY_SYMBOLS.get(currency_code, f"{currency_code} ")
    try:
        price_num = float(raw_price)
        return f"{symbol}{price_num:,.2f}"
    except (ValueError, TypeError):
        return f"{symbol}{raw_price}"


# ---------------------------------------------------------------------------
# Availability Mapping
# ---------------------------------------------------------------------------

AVAILABILITY_MAP = {
    "InStock": "In Stock",
    "OutOfStock": "Out of Stock",
    "LimitedAvailability": "Low Stock",
    "PreOrder": "Pre-Order",
    "SoldOut": "Out of Stock",
    "OnlineOnly": "Online Only",
    "Discontinued": "Discontinued",
}


def map_availability(schema_url: str) -> str:
    if not schema_url:
        return ""
    segment = schema_url.rstrip("/").rsplit("/", 1)[-1]
    return AVAILABILITY_MAP.get(segment, "In Stock")


# ---------------------------------------------------------------------------
# Proxy & SB Helpers
# ---------------------------------------------------------------------------


def _make_proxy_auth_extension(
    proxy_host: str, proxy_port: str, proxy_user: str, proxy_pass: str
) -> str:
    """Create a Chrome extension ZIP for proxy authentication."""
    manifest_json = """
{
    "version": "1.0.0",
    "manifest_version": 2,
    "name": "Proxy Auth",
    "permissions": [
        "proxy", "tabs", "unlimitedStorage", "storage",
        "<all_urls>", "webRequest", "webRequestBlocking"
    ],
    "background": {
        "scripts": ["background.js"]
    }
}
"""
    background_js = """
var config = {
    mode: "fixed_servers",
    rules: {
        singleProxy: {
            scheme: "http",
            host: "%s",
            port: parseInt(%s)
        },
        bypassList: ["localhost"]
    }
};
chrome.proxy.settings.set({value: config, scope: "regular"}, function() {});
function callbackFn(details) {
    return {
        authCredentials: {
            username: "%s",
            password: "%s"
        }
    };
}
chrome.webRequest.onAuthRequired.addListener(
    callbackFn,
    {urls: ["<all_urls>"]},
    ['blocking']
);
""" % (proxy_host, proxy_port, proxy_user, proxy_pass)

    tmp_dir = tempfile.mkdtemp(prefix="proxy_ext_")
    ext_path = os.path.join(tmp_dir, "proxy_auth_extension.zip")
    with zipfile.ZipFile(ext_path, "w") as zf:
        zf.writestr("manifest.json", manifest_json)
        zf.writestr("background.js", background_js)
    return ext_path


def _make_sb_kwargs(
    xvfb: bool = True, no_proxy: bool = False, proxy_tier: str = "residential"
) -> dict[str, Any]:
    """Build SB() constructor kwargs. Uses residential proxy by default."""
    kwargs: dict[str, Any] = {
        "uc": True,
        "xvfb": xvfb,
        "locale_code": "en-gb",
    }

    if not no_proxy:
        _proxy_config = ProxyConfig()
        if proxy_tier == "residential":
            if should_warn_residential("residential"):
                warn_residential_usage("calvinklein.com")
            tier_cfg = _proxy_config.config.get("residential", {})
        else:
            tier_cfg = _proxy_config.config.get("datacenter", {})

        proxy_host = tier_cfg.get("host", "")
        proxy_port = tier_cfg.get("port", "")
        proxy_user = tier_cfg.get("username", "")
        proxy_pass = tier_cfg.get("password", "")

        if proxy_host and proxy_user:
            kwargs["proxy"] = f"{proxy_host}:{proxy_port}"
            kwargs["chromium_arg"] = [
                f"--proxy-server=http://{proxy_host}:{proxy_port}",
                "--disable-blink-Features=AutomationControlled",
            ]
            ext_path = _make_proxy_auth_extension(
                proxy_host, str(proxy_port), proxy_user, proxy_pass
            )
            kwargs["extension_zip"] = ext_path
            logger.info(f"Proxy configured ({proxy_tier}): {proxy_host}:{proxy_port}")
        else:
            logger.warning(f"No proxy credentials found for tier '{proxy_tier}'")

    return kwargs


# ---------------------------------------------------------------------------
# JavaScript Snippets
# ---------------------------------------------------------------------------

DETECT_BLOCK_JS = """
var bodyText = document.body ? document.body.innerText.toUpperCase() : '';
if (bodyText.indexOf('UNFORTUNATELY WE ARE UNABLE TO GIVE YOU ACCESS') !== -1) return 'akamai';
if (bodyText.indexOf('JUST A MOMENT') !== -1) return 'cloudflare';
if (bodyText.indexOf('ACCESS DENIED') !== -1) return 'generic';
if (bodyText.indexOf('ROBOT') !== -1 && bodyText.indexOf('CHECK') !== -1) return 'robot';
if (bodyText.indexOf('BLOCKED') !== -1) return 'blocked';
return null;
"""

ACCEPT_COOKIES_JS = """
var selectors = [
    "button[data-auto-id='accept-cookie-btn']",
    "button[data-testid='cookie-accept']",
    "#onetrust-accept-btn-handler",
    ".cookie-banner button",
    "button[aria-label='Accept cookies']",
    "button[aria-label='Accept all cookies']"
];
for (var s = 0; s < selectors.length; s++) {
    var btns = document.querySelectorAll(selectors[s]);
    if (btns.length > 0) { btns[0].click(); return true; }
}
var all = document.querySelectorAll('button, a, [role="button"]');
for (var i = 0; i < all.length; i++) {
    var t = all[i].textContent.trim().toLowerCase();
    if (t === 'accept' || t === 'accept all cookies' || t === 'accept all'
        || t === 'agree' || t === 'agree all' || t === 'got it'
        || t === 'i accept' || t === 'yes, i agree') {
        all[i].click(); return true;
    }
}
return false;
"""

# Check for page block/redirect — ONLY flag known anti-bot/block patterns
CHECK_REDIRECT_JS = """
var url = window.location.href.toLowerCase();
var currentUrl = window.location.href;
if (url.indexOf('access-denied') !== -1 ||
    url.indexOf('access_denied') !== -1 ||
    url.indexOf('/captcha') !== -1 ||
    url.indexOf('challenge-platform') !== -1 ||
    url.indexOf('cf-chl-bypass') !== -1 ||
    url.indexOf('robot-check') !== -1) {
    return {status: 'redirect', url: currentUrl, reason: 'block_url_pattern'};
}
var bodyText = document.body ? document.body.innerText.toUpperCase() : '';
if (bodyText.indexOf('JUST A MOMENT') !== -1 ||
    bodyText.indexOf('ACCESS DENIED') !== -1 ||
    bodyText.indexOf('VERIFY YOU ARE HUMAN') !== -1 ||
    bodyText.indexOf('ROBOT CHECK') !== -1 ||
    bodyText.indexOf('UNUSUAL TRAFFIC') !== -1 ||
    bodyText.indexOf('BLOCKED') !== -1) {
    return {status: 'block', url: currentUrl, reason: 'block_body_text'};
}
return null;
"""

# Phase 1: DEBUG — Log ALL <a> href values found on the page (before filtering)
DEBUG_LOG_ALL_LINKS_JS = """
var links = document.querySelectorAll('a[href]');
var hrefs = [];
var seen = {};
for (var i = 0; i < links.length; i++) {
    var href = links[i].getAttribute('href');
    if (!href || seen[href]) continue;
    seen[href] = true;
    hrefs.push(href);
}
return {totalLinks: hrefs.length, sample: hrefs.slice(0, 50)};
"""

# Phase 1: Extract product links from a listing/search page
# CRITICAL FIX: Much broader matching — accept any URL containing .html
# Also handle query parameters after .html, relative URLs, and CK product ID patterns
EXTRACT_LISTING_URLS_JS = """
var links = document.querySelectorAll('a[href], a[data-href]');
var seen = {};
var unique = [];
var origin = window.location.origin;

for (var i = 0; i < links.length; i++) {
    // Check both href and data-href attributes
    var href = links[i].getAttribute('data-href') || links[i].getAttribute('href');
    if (!href) continue;

    // Normalize relative URLs
    if (href.indexOf('http') !== 0) {
        if (href.indexOf('/') === 0) {
            href = origin + href;
        } else {
            // Relative to current path
            var basePath = window.location.pathname.substring(0, window.location.pathname.lastIndexOf('/') + 1);
            href = origin + basePath + href;
        }
    }

    // Skip non-product links early
    if (href.indexOf('/content/') !== -1) continue;
    if (href.indexOf('/magazine/') !== -1) continue;
    if (href.indexOf('/about-') !== -1) continue;
    if (href.indexOf('/help-') !== -1) continue;
    if (href.indexOf('/customer-service') !== -1) continue;
    if (href.indexOf('/stores-') !== -1) continue;
    if (href.indexOf('/careers') !== -1) continue;
    if (href.indexOf('/privacy-') !== -1) continue;
    if (href.indexOf('/terms-') !== -1) continue;
    if (href.indexOf('/gift-card') !== -1) continue;
    if (href.indexOf('/wishlist') !== -1) continue;
    if (href.indexOf('/cart') !== -1) continue;
    if (href.indexOf('/checkout') !== -1) continue;
    if (href.indexOf('/account') !== -1) continue;
    if (href.indexOf('/login') !== -1) continue;
    if (href.indexOf('/sustainability') !== -1) continue;
    if (href.indexOf('/brand-') !== -1) continue;
    if (href.indexOf('/press-') !== -1) continue;
    if (href.indexOf('/email-signup') !== -1) continue;
    if (href.indexOf('#') !== -1) continue;  // Skip anchor-only links
    if (href.indexOf('javascript:') !== -1) continue;

    // Strip query parameters and fragments for matching
    var cleanHref = href.split('?')[0].split('#')[0];

    // Match product URLs — multiple patterns:
    // Pattern 1: Ends with .html (standard SFCC)
    // Pattern 2: Contains .html followed by query params (SFCC with color/size selected)
    // Pattern 3: Has a product ID segment (K + digits like K006912055)
    var isProduct = false;

    if (cleanHref.indexOf('.html') !== -1) {
        // Must have a meaningful path (at least 3 segments after domain)
        var pathParts = cleanHref.replace(origin, '').split('/').filter(function(p) { return p.length > 0; });
        // SFCC: /en-us/{category}/{product-name}/{productId}.html — at least 3 segments
        if (pathParts.length >= 3 && cleanHref.endsWith('.html')) {
            isProduct = true;
        }
    }

    // Pattern 3: URL contains a CK product ID pattern (letter followed by digits)
    if (!isProduct && /[A-Za-z][0-9]{6,}/.test(cleanHref)) {
        isProduct = true;
    }

    if (isProduct && !seen[href]) {
        seen[href] = true;
        unique.push(href);
    }
}
return unique;
"""

# Check if there are more pages (pagination)
CHECK_PAGINATION_JS = """
var nextBtn = document.querySelector('a[rel="next"], .pagination a.next, [data-auto-id="pagination-next"], button.next, .next-page a');
if (nextBtn) {
    var nextUrl = nextBtn.getAttribute('href') || '';
    return {hasMore: true, nextUrl: nextUrl};
}
var items = document.querySelectorAll('a[href*=".html"]');
return {hasMore: items.length > 0, nextUrl: '', itemCount: items.length};
"""

# Phase 2: Extract product data from a product page via JSON-LD
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
    brand: 'Calvin Klein',
    sku: '',
    description: '',
    category: '',
    color: '',
    rating: '',
    review_count: '',
    has_product_jsonld: false
};

// --- SOFT 404 CHECK ---
var titleText = (document.title || '').toLowerCase();
var h1Text = '';
var h1El = document.querySelector('h1');
if (h1El) h1Text = h1El.textContent.trim().toLowerCase();
var notFoundMarkers = ['page not found', 'product not found', 'unavailable', 'discontinued',
                        'no longer available', '404', 'page error'];
for (var m = 0; m < notFoundMarkers.length; m++) {
    if (titleText.indexOf(notFoundMarkers[m]) !== -1 ||
        h1Text.indexOf(notFoundMarkers[m]) !== -1) {
        product.remarks = 'Soft 404: product not found (' + (h1Text || titleText) + ')';
        return product;
    }
}

// --- URL REDIRECT CHECK ---
var currentUrl = window.location.href;
if (currentUrl.indexOf('/search') !== -1 ||
    currentUrl.indexOf('/oops') !== -1 ||
    currentUrl.indexOf('/home') !== -1 ||
    currentUrl.indexOf('/error') !== -1) {
    product.remarks = 'Soft 404: redirected to non-product page (' + currentUrl + ')';
    return product;
}

// --- JSON-LD EXTRACTION ---
var jsonld = null;
var scripts = document.querySelectorAll('script[type="application/ld+json"]');
for (var i = 0; i < scripts.length; i++) {
    try {
        var raw = scripts[i].textContent;
        if (!raw || !raw.trim()) continue;
        var data = JSON.parse(raw);
        var items = Array.isArray(data) ? data : [data];
        for (var j = 0; j < items.length; j++) {
            if (items[j]['@type'] === 'Product') {
                jsonld = items[j];
                break;
            }
        }
        if (jsonld) break;
    } catch(e) {}
}

if (jsonld) {
    product.has_product_jsonld = true;
    product.title = jsonld.name || '';
    product.description = jsonld.description || '';
    product.sku = jsonld.sku || jsonld.mpn || '';

    if (jsonld.brand && jsonld.brand.name) {
        product.brand = jsonld.brand.name;
    }

    if (jsonld.offers) {
        var offers = Array.isArray(jsonld.offers) ? jsonld.offers[0] : jsonld.offers;
        product.price = offers.price || '';
        product.currency = offers.priceCurrency || '';
        product.availability = offers.availability || '';

        var highPrice = offers.highPrice || '';
        var lowPrice = offers.lowPrice || offers.price || '';
        if (highPrice && parseFloat(highPrice) > parseFloat(lowPrice || 0)) {
            product.original_price = highPrice;
        }
    }

    if (jsonld.image) {
        if (Array.isArray(jsonld.image)) {
            product._jsonld_images = jsonld.image;
        } else if (typeof jsonld.image === 'string') {
            product._jsonld_images = [jsonld.image];
        }
    }

    if (jsonld.aggregateRating) {
        product.rating = jsonld.aggregateRating.ratingValue || '';
        product.review_count = jsonld.aggregateRating.reviewCount || '';
    }

    if (jsonld.additionalProperty) {
        var props = Array.isArray(jsonld.additionalProperty) ? jsonld.additionalProperty : [jsonld.additionalProperty];
        for (var p = 0; p < props.length; p++) {
            if (props[p].name === 'Color' || props[p].name === 'color') {
                product.color = props[p].value || '';
            }
        }
    }
}

// --- CSS FALLBACK FOR MISSING FIELDS ---
if (!product.title) {
    var titleSelectors = [
        'h1[class*="product"]', '[data-testid="product-name"]',
        '.product-details h1', 'h1.product-name', '.product-name h1',
        'h1[itemprop="name"]', 'h1'
    ];
    for (var t = 0; t < titleSelectors.length; t++) {
        var el = document.querySelector(titleSelectors[t]);
        if (el && el.textContent.trim()) {
            product.title = el.textContent.trim();
            break;
        }
    }
}

if (!product.price) {
    var priceSelectors = [
        '[class*="price"] .value', '[data-testid="product-price"]',
        '.product-price .sales', '.price .sales .value',
        '[class*="price-current"]', '[class*="price"] span[itemprop="price"]',
        '.price [class*="sales"]'
    ];
    for (var p = 0; p < priceSelectors.length; p++) {
        var pEl = document.querySelector(priceSelectors[p]);
        if (pEl) {
            var pText = pEl.textContent.trim() || pEl.getAttribute('content') || '';
            if (pText) { product.price = pText; break; }
        }
    }
}

if (!product.original_price) {
    var origSelectors = [
        '[class*="price"] [class*="strike"]', '[class*="price"] [class*="compare"]',
        '[class*="compare"]', '[class*="was"]', '.price .standard .value',
        '[data-testid="list-price"]'
    ];
    for (var o = 0; o < origSelectors.length; o++) {
        var oEl = document.querySelector(origSelectors[o]);
        if (oEl) {
            var oText = oEl.textContent.trim() || oEl.getAttribute('content') || '';
            if (oText) { product.original_price = oText; break; }
        }
    }
}

if (!product.availability) {
    var availSelectors = [
        '[class*="availability"]', '[class*="stock"]',
        '[data-testid="availability"]', '.inventory-status'
    ];
    for (var a = 0; a < availSelectors.length; a++) {
        var aEl = document.querySelector(availSelectors[a]);
        if (aEl) {
            var aText = aEl.textContent.trim().toLowerCase();
            if (aText) {
                product.availability = aText.indexOf('in stock') !== -1 ||
                    aText.indexOf('available') !== -1 ? 'In Stock' : 'Out of Stock';
                break;
            }
        }
    }
}

if (!product.sku) {
    var skuSelectors = [
        '[class*="sku"]', '[data-testid="sku"]', '.product-id', '[class*="pid"]'
    ];
    for (var s = 0; s < skuSelectors.length; s++) {
        var sEl = document.querySelector(skuSelectors[s]);
        if (sEl) {
            var sText = sEl.textContent.trim();
            if (sText) { product.sku = sText; break; }
        }
    }
}

if (!product.sku) {
    var urlMatch = window.location.href.match(/([A-Za-z0-9]{6,})\\.html/);
    if (urlMatch) product.sku = urlMatch[1];
}

var pathMatch = window.location.href.match(/\\/en-us\\/([^/]+)\\/[^/]+\\.html/);
if (pathMatch) product.category = pathMatch[1];

if (!product.color) {
    var colorSelectors = [
        '[class*="color"] [class*="selected"]', '[class*="swatch"][class*="active"]',
        '[data-testid="color-name"]', '[class*="color"] [class*="swatch-selected"]'
    ];
    for (var c = 0; c < colorSelectors.length; c++) {
        var cEl = document.querySelector(colorSelectors[c]);
        if (cEl) {
            var cText = cEl.textContent.trim() || cEl.getAttribute('aria-label') || '';
            if (cText) { product.color = cText; break; }
        }
    }
}

return product;
"""

# Phase 2: Extract product images scoped to gallery
EXTRACT_IMAGES_JS = """
var images = [];
var gallerySelectors = [
    '[class*="product-image"] img',
    '[class*="carousel"] img',
    '[class*="gallery"] img',
    '[data-auto-id="product-image"] img',
    '.pdp-gallery img',
    '[data-testid*="gallery"] img',
    '.product-detail img'
];
var seen = {};
for (var s = 0; s < gallerySelectors.length; s++) {
    var imgEls = document.querySelectorAll(gallerySelectors[s]);
    for (var i = 0; i < imgEls.length; i++) {
        var src = imgEls[i].getAttribute('src') || imgEls[i].getAttribute('data-src') || '';
        if (!src) continue;
        if (src.indexOf('data:') === 0) continue;
        if (src.indexOf('logo') !== -1) continue;
        if (src.indexOf('/icon/') !== -1) continue;
        if (src.indexOf('/emoji/') !== -1) continue;
        if (src.indexOf('/flags/') !== -1) continue;
        if (src.indexOf('/navigation/') !== -1) continue;
        if (src.indexOf('/brand.assets/') !== -1) continue;
        if (src.indexOf('sprite') !== -1) continue;
        if (src.indexOf('pixel') !== -1) continue;
        if (src.indexOf('blank.gif') !== -1) continue;
        if (src.indexOf('swatch') !== -1) continue;
        if (!seen[src]) {
            seen[src] = true;
            images.push(src);
        }
    }
    if (images.length > 0) break;
}
return images;
"""

# Phase 1: Search input interaction JS
PERFORM_SEARCH_JS = """
var searchInput = document.querySelector(
    'input[type="search"], input[name="q"], input[placeholder*="Search"], ' +
    'input[data-auto-id="search-input"], input[class*="search"]'
);
if (searchInput) {
    searchInput.value = arguments[0];
    searchInput.dispatchEvent(new Event('input', {bubbles: true}));
    searchInput.dispatchEvent(new Event('change', {bubbles: true}));
    return true;
}
return false;
"""

SUBMIT_SEARCH_JS = """
var form = document.querySelector('form[action*="search"], form[data-action*="Search"]');
if (form) { form.submit(); return true; }
var btn = document.querySelector('button[type="submit"], button[data-auto-id="search-submit"]');
if (btn) { btn.click(); return true; }
var input = document.querySelector('input[type="search"], input[name="q"]');
if (input) {
    input.dispatchEvent(new Event('keydown', {key: 'Enter', keyCode: 13, bubbles: true}));
    return true;
}
return false;
"""

SCROLL_DOWN_JS = """
var current = window.scrollY;
window.scrollTo(0, current + 800);
return window.scrollY;
"""

# Scroll to bottom of page progressively
SCROLL_TO_BOTTOM_JS = """
var totalHeight = document.body.scrollHeight || document.documentElement.scrollHeight;
var current = window.scrollY;
var step = Math.max(400, Math.floor(totalHeight / 20));
window.scrollTo(0, current + step);
return window.scrollY;
"""

# Simulate human-like interaction to establish legitimacy with anti-bot sensors
HUMAN_INTERACTION_JS = """
var evt = new MouseEvent('mousemove', {clientX: 200 + Math.random() * 400, clientY: 300 + Math.random() * 300, bubbles: true});
document.dispatchEvent(evt);
window.scrollBy(0, 100 + Math.floor(Math.random() * 200));
return true;
"""

# ---------------------------------------------------------------------------
# Core Functions
# ---------------------------------------------------------------------------


def open_page(driver: Any, url: str, reconnect_time: int = UC_RECONNECT_TIME) -> bool:
    """Navigate to a URL using UC Mode reconnect. Returns False on block/redirect."""
    try:
        driver.uc_open_with_reconnect(url, reconnect_time=reconnect_time)
        time.sleep(3)
        status = driver.execute_script(CHECK_REDIRECT_JS)
        if status:
            logger.warning(
                f"Block/redirect detected on {url}: "
                f"status={status.get('status')}, reason={status.get('reason')}, "
                f"actual_url={status.get('url', 'unknown')}"
            )
            return False
        return True
    except Exception as e:
        logger.error(f"Failed to open {url}: {e}")
        return False


def warmup_session(driver: Any) -> bool:
    """Visit homepage, wait for anti-bot sensors, accept cookies.

    Includes retry logic: if warmup fails, waits and retries up to
    WARMUP_MAX_RETRIES times before giving up.
    """
    for attempt in range(1, WARMUP_MAX_RETRIES + 1):
        logger.info(f"Warm-up attempt {attempt}/{WARMUP_MAX_RETRIES}: visiting {HOMEPAGE_URL}")
        try:
            driver.uc_open_with_reconnect(HOMEPAGE_URL, reconnect_time=UC_RECONNECT_TIME)
            time.sleep(5)

            actual_url = driver.current_url
            logger.info(f"  Landed on: {actual_url}")

            block_type = driver.execute_script(DETECT_BLOCK_JS)
            if block_type:
                logger.warning(
                    f"  {block_type.upper()} BLOCK DETECTED (attempt {attempt})"
                )
                if attempt < WARMUP_MAX_RETRIES:
                    wait = 10 * attempt
                    logger.info(f"  Waiting {wait}s before retry...")
                    time.sleep(wait)
                    continue
                return False

            logger.info(f"  Waiting {WARMUP_WAIT}s for anti-bot sensor data collection...")
            for tick in range(0, WARMUP_WAIT, 3):
                time.sleep(3)
                try:
                    driver.execute_script(HUMAN_INTERACTION_JS)
                except Exception:
                    pass

            block_type = driver.execute_script(DETECT_BLOCK_JS)
            if block_type:
                logger.warning(
                    f"  {block_type.upper()} BLOCK DETECTED after warmup (attempt {attempt})"
                )
                if attempt < WARMUP_MAX_RETRIES:
                    wait = 10 * attempt
                    logger.info(f"  Waiting {wait}s before retry...")
                    time.sleep(wait)
                    continue
                return False

            clicked = driver.execute_script(ACCEPT_COOKIES_JS)
            if clicked:
                logger.info("  Accepted cookies")
                time.sleep(2)

            logger.info("Warm-up complete")
            return True

        except Exception as e:
            logger.error(f"  Warm-up attempt {attempt} failed with exception: {e}")
            if attempt < WARMUP_MAX_RETRIES:
                wait = 10 * attempt
                logger.info(f"  Waiting {wait}s before retry...")
                time.sleep(wait)
                continue
            return False

    return False


def is_product_url(url: str) -> bool:
    """Check if URL looks like a product page (not a category/content page)."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    # Must end with .html (possibly with query params after)
    clean_path = path.split("?")[0].split("#")[0]
    if not clean_path.endswith(".html"):
        return False
    for fragment in NON_PRODUCT_FRAGMENTS:
        if fragment in path.lower():
            return False
    # Must have at least 3 path segments (locale + category + product)
    segments = [s for s in clean_path.split("/") if s]
    if len(segments) < 3:
        return False
    return True


def wait_for_product_cards(driver: Any, timeout: int = DISCOVERY_RENDER_TIMEOUT) -> list[str]:
    """Wait for product cards to render on a listing page with retry loop.

    Retries URL extraction with scrolling until links are found or timeout.
    This handles SPA/React-rendered product grids that need time to hydrate.
    """
    start = time.time()
    all_urls: list[str] = []

    while time.time() - start < timeout:
        # Try extracting URLs
        raw_urls = driver.execute_script(EXTRACT_LISTING_URLS_JS) or []
        product_urls = [u for u in raw_urls if is_product_url(u)]

        if product_urls:
            logger.info(f"  Found {len(product_urls)} product URLs after {time.time() - start:.1f}s")
            return product_urls

        elapsed = time.time() - start
        logger.info(f"  No product URLs yet (elapsed: {elapsed:.1f}s), scrolling and retrying...")

        # Debug: log what links ARE on the page
        if elapsed < 10:
            link_info = driver.execute_script(DEBUG_LOG_ALL_LINKS_JS)
            if link_info:
                sample = link_info.get("sample", [])
                html_count = len([s for s in sample if ".html" in s])
                logger.info(f"  Total links on page: {link_info.get('totalLinks', 0)}, "
                            f"with .html in sample: {html_count}")
                if html_count > 0:
                    for link in sample[:5]:
                        if ".html" in link:
                            logger.info(f"    Sample .html link: {link}")
                else:
                    logger.info(f"    First 5 links: {sample[:5]}")

        # Scroll down to trigger lazy loading
        driver.execute_script(SCROLL_TO_BOTTOM_JS)
        time.sleep(2)

    # Final attempt — log page source snippet for debugging
    logger.warning(f"  Timeout after {timeout}s waiting for product cards")
    try:
        page_src = driver.execute_script("return document.body ? document.body.innerHTML.substring(0, 3000) : 'NO BODY';")
        logger.debug(f"  Page source snippet: {page_src[:1000]}")

        # Count product-like elements as a diagnostic
        tile_count = driver.execute_script("""
            var tiles = document.querySelectorAll('[class*="product-tile"], [class*="product_tile"], [data-auto-id="product-tile"], [class*="grid"] [class*="tile"], article, .product-card');
            return tiles.length;
        """)
        logger.info(f"  Product tile elements found: {tile_count}")
    except Exception as e:
        logger.warning(f"  Could not get debug info: {e}")

    return all_urls


# ---------------------------------------------------------------------------
# Phase 1: Discover Product URLs
# ---------------------------------------------------------------------------


def discover_from_search(driver: Any, query: str) -> list[str]:
    """Navigate search URL with a query and collect product URLs from results.

    Tries multiple URL patterns and uses a wait loop for SPA rendering.
    """
    search_url = f"{SEARCH_URL_BASE}?q={query}"
    logger.info(f"Phase 1: Searching for '{query}' at {search_url}")

    # Try all known URL patterns for search
    search_urls_to_try = [
        search_url,
        f"{PRODUCTS_URL}?q={query}",
        f"{SITE_URL}en-us/products?q={query}",
        f"{SITE_URL}search?q={query}",
    ]

    loaded = False
    for url in search_urls_to_try:
        logger.info(f"  Trying: {url}")
        if open_page(driver, url):
            loaded = True
            logger.info(f"  Loaded: {driver.current_url}")
            break
        logger.warning(f"  Failed to load: {url}")

    if not loaded:
        logger.error("All search URL patterns failed")
        return []

    # Use wait loop to handle SPA rendering
    all_product_urls = wait_for_product_cards(driver)
    if not all_product_urls:
        # Try form-based search as last resort
        logger.info("  URL-based search found no products, trying form-based search...")
        search_input_found = driver.execute_script(PERFORM_SEARCH_JS, query)
        if search_input_found:
            time.sleep(1)
            driver.execute_script(SUBMIT_SEARCH_JS)
            time.sleep(5)
            current_url = driver.current_url
            logger.info(f"  URL after form submit: {current_url}")
            all_product_urls = wait_for_product_cards(driver)

    if not all_product_urls:
        logger.warning("  No product URLs found from search")
        return []

    logger.info(f"  Page 1: found {len(all_product_urls)} product URLs")

    # Pagination: navigate through next pages
    for page_num in range(2, MAX_PAGES + 1):
        time.sleep(DELAY_BETWEEN_REQUESTS)

        pagination_info = driver.execute_script(CHECK_PAGINATION_JS) or {}

        next_url = None
        if pagination_info.get("hasMore") and pagination_info.get("nextUrl"):
            next_url = pagination_info["nextUrl"]
            if not next_url.startswith("http"):
                next_url = urljoin(driver.current_url, next_url)
        else:
            # Try SFCC pagination: ?start=N&sz=24
            current_url = driver.current_url
            base_url_no_params = current_url.split("?")[0] if "?" in current_url else current_url
            sz = 24
            start = (page_num - 1) * sz
            next_url = f"{base_url_no_params}?start={start}&sz={sz}"

        if next_url:
            logger.info(f"  Pagination page {page_num}: trying {next_url}")
            if not open_page(driver, next_url):
                logger.info(f"  Pagination failed, stopping at page {page_num - 1}")
                break

            page_urls = wait_for_product_cards(driver, timeout=15)
            new_urls = [u for u in page_urls if u not in all_product_urls]
            if not new_urls:
                logger.info(f"  No new URLs on page {page_num}, stopping")
                break
            all_product_urls.extend(new_urls)
            logger.info(f"  Page {page_num}: {len(new_urls)} new (total: {len(all_product_urls)})")
        else:
            logger.info(f"  No more pages, stopping at page {page_num - 1}")
            break

    return all_product_urls


def discover_from_category(driver: Any, category_url: str) -> list[str]:
    """Navigate a category URL and collect product URLs from the listing page."""
    logger.info(f"Phase 1: Discovering products from category: {category_url}")

    if not open_page(driver, category_url):
        logger.error(f"Category page failed: {category_url}")
        return []

    all_product_urls = wait_for_product_cards(driver)
    logger.info(f"  Page 1: found {len(all_product_urls)} product URLs")

    for page_num in range(2, MAX_PAGES + 1):
        time.sleep(DELAY_BETWEEN_REQUESTS)

        current_url = driver.current_url
        base_url_no_params = current_url.split("?")[0] if "?" in current_url else current_url
        sz = 24
        start = (page_num - 1) * sz
        next_url = f"{base_url_no_params}?start={start}&sz={sz}"

        if not open_page(driver, next_url):
            logger.info(f"  No more pages, stopping at page {page_num - 1}")
            break

        page_urls = wait_for_product_cards(driver, timeout=15)
        new_urls = [u for u in page_urls if u not in all_product_urls]
        if not new_urls:
            break

        all_product_urls.extend(new_urls)
        logger.info(f"  Page {page_num}: {len(new_urls)} new (total: {len(all_product_urls)})")

    return all_product_urls


def discover_from_products_page(driver: Any, query: str = "") -> list[str]:
    """Navigate to the /products page (confirmed working URL) and discover products.

    This is the PRIMARY discovery method since navigation_analysis confirmed
    products were found at https://www.calvinklein.com/products
    """
    if query:
        url = f"{PRODUCTS_URL}?q={query}"
    else:
        url = PRODUCTS_URL

    logger.info(f"Phase 1: Discovering products from /products page: {url}")

    if not open_page(driver, url):
        logger.error(f"/products page failed: {url}")
        return []

    all_product_urls = wait_for_product_cards(driver)
    logger.info(f"  Page 1: found {len(all_product_urls)} product URLs")

    # Pagination
    for page_num in range(2, MAX_PAGES + 1):
        time.sleep(DELAY_BETWEEN_REQUESTS)

        current_url = driver.current_url
        base_url_no_params = current_url.split("?")[0] if "?" in current_url else current_url
        sz = 24
        start = (page_num - 1) * sz

        # Append query param if original URL had one
        if query and "q=" not in base_url_no_params:
            next_url = f"{base_url_no_params}?q={query}&start={start}&sz={sz}"
        else:
            next_url = f"{base_url_no_params}?start={start}&sz={sz}"

        logger.info(f"  Pagination page {page_num}: {next_url}")
        if not open_page(driver, next_url):
            logger.info(f"  Pagination failed, stopping at page {page_num - 1}")
            break

        page_urls = wait_for_product_cards(driver, timeout=15)
        new_urls = [u for u in page_urls if u not in all_product_urls]
        if not new_urls:
            logger.info(f"  No new URLs on page {page_num}, stopping")
            break
        all_product_urls.extend(new_urls)
        logger.info(f"  Page {page_num}: {len(new_urls)} new (total: {len(all_product_urls)})")

    return all_product_urls


# ---------------------------------------------------------------------------
# Phase 2: Scrape Product Pages
# ---------------------------------------------------------------------------


def scrape_product_page(url: str, src_url: str, index: int, xvfb: bool = True) -> dict[str, Any]:
    """Create a fresh SB() session for each product and extract data."""
    try:
        with SB(**_make_sb_kwargs(xvfb=xvfb)) as sb:
            driver = sb.driver

            if not open_page(driver, url):
                return Product(
                    id=index, url=url, src_url=src_url,
                    currency=CURRENCY_DEFAULT, status_code=403,
                    scraped_at=datetime.now(timezone.utc).isoformat(),
                    remarks="Redirect/block on page load",
                ).to_dict()

            data = driver.execute_script(EXTRACT_PRODUCT_JS, src_url) or {}

            product = Product(
                id=index,
                title=data.get("title", ""),
                price="",
                availability="",
                original_price="",
                currency=data.get("currency", CURRENCY_DEFAULT),
                url=data.get("url", url),
                src_url=data.get("src_url", src_url),
                location=data.get("location", ""),
                status_code=200,
                scraped_at=datetime.now(timezone.utc).isoformat(),
                remarks=data.get("remarks", ""),
                brand=data.get("brand", "Calvin Klein"),
                sku=data.get("sku", ""),
                description=clean_html(data.get("description", "")),
                category=data.get("category", ""),
                color=data.get("color", ""),
                rating=data.get("rating", ""),
                review_count=data.get("review_count", ""),
            )

            # Format availability
            raw_avail = data.get("availability", "")
            if raw_avail:
                product.availability = map_availability(raw_avail)

            # Format price with currency symbol
            raw_price = data.get("price", "")
            if raw_price:
                product.price = format_price(str(raw_price), product.currency)

            # Format original price
            raw_orig = data.get("original_price", "")
            if raw_orig:
                product.original_price = format_price(str(raw_orig), product.currency)

            # Extract images (scoped to gallery)
            images = driver.execute_script(EXTRACT_IMAGES_JS) or []
            jsonld_images = data.get("_jsonld_images", [])
            if jsonld_images:
                for img_url in jsonld_images:
                    clean_url = img_url.split("?")[0] if "?" in img_url else img_url
                    if clean_url not in [im.split("?")[0] for im in images]:
                        images.insert(0, img_url)
            product.images = images[:15]

            # Check if JSON-LD had no Product type and no title found
            if not data.get("has_product_jsonld") and not product.title:
                product.remarks = product.remarks or "No JSON-LD Product found and no title extracted"
                product.status_code = 0

            # Soft 404 detection
            if product.remarks and "soft 404" in product.remarks.lower():
                product.title = ""
                product.price = ""
                product.availability = ""
                product.status_code = 404

            return product.to_dict()

    except Exception as e:
        logger.error(f"  [{index}] Session failed for {url}: {e}")
        return Product(
            id=index, url=url, src_url=src_url,
            currency=CURRENCY_DEFAULT, status_code=0,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            remarks=f"Session error: {str(e)[:200]}",
        ).to_dict()


# ---------------------------------------------------------------------------
# Utility Functions
# ---------------------------------------------------------------------------


def clean_html(html_str: str) -> str:
    """Strip HTML tags and clean whitespace."""
    if not html_str:
        return ""
    text = re.sub(r"<[^>]+>", " ", html_str)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


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
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump({"urls": urls}, f, indent=2, ensure_ascii=False)
    logger.info(f"Saved {len(urls)} URLs to {filepath}")


def apply_product_limits(urls: list[str], sample: bool, limit: Optional[int]) -> list[str]:
    """Apply --sample and --limit constraints to URL list."""
    if sample:
        urls = urls[:5]
    if limit is not None and limit > 0:
        urls = urls[:limit]
    return urls


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description=f"Calvin Klein Product Scraper (SeleniumBase UC Mode — {PLATFORM})"
    )
    parser.add_argument("--sample", action="store_true", help="Scrape only 5 products")
    parser.add_argument("--limit", type=int, default=None, help="Max products to scrape")
    parser.add_argument("--input", type=str, default=None, help="Path to input URLs JSON file")
    parser.add_argument("--urls", nargs="+", default=None, help="Product URLs as CLI arguments")
    parser.add_argument(
        "--query", type=str, default=DEFAULT_QUERY,
        help=f"Search query for Phase 1 discovery (default: '{DEFAULT_QUERY}')"
    )
    parser.add_argument(
        "--category-url", type=str, default=None,
        help="Category URL for Phase 1 discovery (alternative to search)"
    )
    parser.add_argument("--no-proxy", action="store_true", help="Skip proxy, connect directly")
    parser.add_argument("--xvfb", action="store_true", help="Use Xvfb virtual display (Docker)")
    args = parser.parse_args()

    start_time = time.time()

    logger.info("=" * 80)
    logger.info(f"Starting scraper for {SITE_NAME}")
    logger.info(f"Site: {SITE_URL}")
    logger.info(f"Platform: {PLATFORM}")
    logger.info(f"Scraping method: {SCRAPING_METHOD}")
    logger.info(f"Xvfb: {args.xvfb}")
    logger.info(f"Search query: {args.query}")
    logger.info(f"Proxy tier: residential")
    logger.info("=" * 80)

    product_urls: list[str] = []
    src_url_value: str = ""

    # --- Determine URL source ---
    if args.urls:
        product_urls = [u.strip("\"'") for u in args.urls]
        src_url_value = ""
        logger.info(f"Using {len(product_urls)} URLs from CLI arguments")

    elif args.input:
        product_urls = load_urls_from_file(args.input)
        src_url_value = ""
        logger.info(f"Using {len(product_urls)} URLs from input file: {args.input}")

    else:
        # Phase 1: Discovery via search or category (default behavior)
        logger.info("Phase 1: Discovering product URLs via search...")

        no_proxy = getattr(args, "no_proxy", False)
        with SB(**_make_sb_kwargs(xvfb=args.xvfb, no_proxy=no_proxy)) as sb:
            driver = sb.driver
            try:
                if not warmup_session(driver):
                    logger.error("Warm-up failed after all retries, cannot proceed with discovery")
                    sys.exit(1)

                if args.category_url:
                    product_urls = discover_from_category(driver, args.category_url)
                    src_url_value = args.category_url
                else:
                    # Try /products page first (confirmed working), then search as fallback
                    logger.info("Trying /products page (confirmed discovery URL)...")
                    product_urls = discover_from_products_page(driver, args.query)
                    src_url_value = f"{PRODUCTS_URL}?q={args.query}"

                    if not product_urls:
                        logger.info("/products page found no results, falling back to search...")
                        product_urls = discover_from_search(driver, args.query)
                        src_url_value = f"{SEARCH_URL_BASE}?q={args.query}"

            except Exception as e:
                logger.error(f"Discovery failed: {e}")
                sys.exit(1)

        logger.info(f"Phase 1 complete: discovered {len(product_urls)} product URLs")

        if product_urls:
            save_urls_to_file(INPUT_FILE, product_urls)

    # Apply limits
    product_urls = apply_product_limits(product_urls, args.sample, args.limit)

    if not product_urls:
        logger.error("No product URLs to scrape — cannot proceed")
        sys.exit(1)

    logger.info(f"Total products to scrape: {len(product_urls)}")
    logger.info("Phase 2: Scraping product pages (per-page session architecture)")
    logger.info("-" * 80)

    results: list[dict[str, Any]] = []
    failed_count = 0

    for i, url in enumerate(product_urls):
        try:
            product = scrape_product_page(
                url=url,
                src_url=src_url_value or url,
                index=i + 1,
                xvfb=args.xvfb,
            )

            title = product.get("title", "")
            price = product.get("price", "")
            remarks = product.get("remarks", "")

            if title:
                results.append(product)
                logger.info(
                    f"  [{i + 1}/{len(product_urls)}] {title[:60]} \u2014 {price}"
                )
            else:
                failed_count += 1
                results.append(product)
                logger.warning(
                    f"  [{i + 1}/{len(product_urls)}] No title: {url[:80]}"
                    + (f" ({remarks})" if remarks else "")
                )

        except Exception as e:
            logger.error(f"  [{i + 1}/{len(product_urls)}] Error scraping {url[:80]}: {e}")
            failed_count += 1
            results.append(Product(
                id=i + 1, url=url, src_url=src_url_value or url,
                currency=CURRENCY_DEFAULT, status_code=0,
                scraped_at=datetime.now(timezone.utc).isoformat(),
                remarks=f"Error: {str(e)[:200]}",
            ).to_dict())

        # Progress reporting
        if (i + 1) % 25 == 0:
            percent = ((i + 1) / len(product_urls)) * 100
            logger.info(f"Progress: [{i + 1}/{len(product_urls)}] ({percent:.1f}%)")

        # Rate limiting delay between products
        if i < len(product_urls) - 1:
            time.sleep(DELAY_BETWEEN_REQUESTS)

    # --- Write Output ---
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

    os.makedirs(os.path.dirname(OUTPUT_FILE) if os.path.dirname(OUTPUT_FILE) else ".", exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    success_count = len(results) - failed_count

    logger.info("=" * 80)
    logger.info("EXTRACTION COMPLETE")
    logger.info(f"Total: {len(results)}, Success: {success_count}, Failed: {failed_count}")
    logger.info(f"Duration: {round(time.time() - start_time, 2)}s")
    logger.info(f"Output: {OUTPUT_FILE}")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
