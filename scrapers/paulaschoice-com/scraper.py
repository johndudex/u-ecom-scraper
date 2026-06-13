#!/usr/bin/env python3
"""
Paula's Choice Product Scraper — SeleniumBase UC Mode (Residential Proxy)

Extracts product data from paulaschoice.com using undetected Chrome (SeleniumBase UC)
with residential proxy. The site is protected by Akamai Bot Manager, which blocks
datacenter proxies and direct connections. Residential IPs are required.

The page is fully JavaScript-rendered; all common CSS selectors (h1, price, sku, etc.)
return NOT FOUND. Extraction relies entirely on JSON-LD structured data present in
the rendered DOM. WebDriverWait polling (up to 25 seconds) waits for JSON-LD blocks.

Anti-bot bypass strategy:
  1. Visit homepage FIRST to establish Akamai _abck cookies (18s warmup)
  2. Accept cookie consent if present
  3. Navigate to product URLs one by one with rate limiting

Usage:
    python3 scraper.py                         # reads input_urls.json from same folder
    python3 scraper.py --input urls.json        # explicit input file
    python3 scraper.py --urls url1 url2         # URLs as CLI arguments
    python3 scraper.py --sample                 # scrape only 5 products
    python3 scraper.py --limit 10               # scrape max 10 products
    python3 scraper.py --xvfb                   # use Xvfb virtual display (Docker)
    python3 scraper.py --no-proxy               # skip proxy, connect directly (will likely be blocked)
    python3 scraper.py --proxy-tier datacenter # override to datacenter proxy
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

from selenium.webdriver.support.ui import WebDriverWait
from seleniumbase import SB

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.environ.get("PROJECT_ROOT", os.path.join(SCRIPT_DIR, ".."))
sys.path.insert(0, PROJECT_ROOT)
from src.proxy import (
    ProxyConfig,
    build_proxy_url,
    should_warn_residential,
    warn_residential_usage,
)  # noqa: E402

SITE_NAME = "Paula's Choice"
SITE_URL = "https://www.paulaschoice.com"
PLATFORM = "custom"
SCRAPING_METHOD = "seleniumbase_uc"
SITE_SLUG = "paulaschoice-com"

DELAY_BETWEEN_REQUESTS = 3
PAGE_LOAD_TIMEOUT = 30
UC_RECONNECT_TIME = 4
# Max time to wait for JSON-LD blocks to appear after page load
JSONLD_WAIT_TIMEOUT = 25
# Initial pause after page navigation to let JS framework begin rendering
INITIAL_RENDER_WAIT = 5
# Homepage warmup wait — Akamai needs ~18s for sensor data collection + _abck cookie
WARMUP_WAIT = 18
# Minimum page source length to consider the page actually loaded (not blank proxy page)
MIN_SOURCE_LENGTH = 100
# Max retries for warmup (proxy blank page / block page)
WARMUP_MAX_RETRIES = 2
# Cooldown between warmup retries
WARMUP_RETRY_COOLDOWN = 10

TIMESTAMP = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
OUTPUT_FILE = os.path.join(SCRIPT_DIR, f"output_{TIMESTAMP}.json")
INPUT_FILE = os.path.join(SCRIPT_DIR, "input_urls.json")
LOG_FILE = os.path.join(PROJECT_ROOT, "logs", f"{SITE_SLUG}.log")

os.makedirs(os.path.dirname(LOG_FILE) if os.path.dirname(LOG_FILE) else ".", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

proxy_config = ProxyConfig.get_instance()

# ---------------------------------------------------------------------------
# Schema.org availability URL -> human-readable text
# ---------------------------------------------------------------------------
AVAILABILITY_MAP: dict[str, str] = {
    "https://schema.org/InStock": "In Stock",
    "https://schema.org/OutOfStock": "Out of Stock",
    "https://schema.org/PreOrder": "Pre-Order",
    "https://schema.org/LimitedAvailability": "Limited Availability",
    "https://schema.org/SoldOut": "Sold Out",
    "https://schema.org/Discontinued": "Discontinued",
    "http://schema.org/InStock": "In Stock",
    "http://schema.org/OutOfStock": "Out of Stock",
    "http://schema.org/PreOrder": "Pre-Order",
    "http://schema.org/LimitedAvailability": "Limited Availability",
    "http://schema.org/SoldOut": "Sold Out",
    "http://schema.org/Discontinued": "Discontinued",
}


def normalize_availability(raw: Optional[str]) -> str:
    """Map a schema.org availability URL to human-readable text."""
    if not raw:
        return ""
    for key, value in AVAILABILITY_MAP.items():
        if key.lower() in raw.lower():
            return value
    # Fallback: try to extract the last segment of the URL
    match = re.search(
        r"(?i)(instock|outofstock|preorder|limitedavailability|soldout|discontinued)",
        raw,
    )
    if match:
        token = match.group(1).capitalize()
        readable = re.sub(r"([A-Z])", r" \1", token).strip()
        return readable
    return raw


def format_price(amount: Any, currency: str = "USD") -> str:
    """Format a numeric price with currency symbol."""
    if not amount:
        return ""
    try:
        value = float(amount)
    except (ValueError, TypeError):
        return str(amount)
    currency_symbols: dict[str, str] = {
        "USD": "$",
        "EUR": "\u20ac",
        "GBP": "\u00a3",
        "CAD": "C$",
        "AUD": "A$",
        "JPY": "\u00a5",
        "CNY": "\u00a5",
    }
    symbol = currency_symbols.get(currency, f"{currency} ")
    if currency == "JPY":
        return f"{symbol}{int(value):,}"
    return f"{symbol}{value:,.2f}"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class Product:
    """Represents a scraped product with all standard output fields."""

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
    # Extended fields from JSON-LD
    description: str = ""
    brand: str = ""
    sku: str = ""
    mpn: str = ""
    rating: str = ""
    review_count: str = ""
    images: list[str] = field(default_factory=list)
    reviews: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to output-ready dictionary with only non-empty extras."""
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
        if self.brand:
            result["brand"] = self.brand
        if self.sku:
            result["sku"] = self.sku
        if self.mpn:
            result["mpn"] = self.mpn
        if self.rating:
            result["rating"] = self.rating
        if self.review_count:
            result["review_count"] = self.review_count
        if self.images:
            result["images"] = self.images
        if self.reviews:
            result["reviews"] = self.reviews
        return result


# ---------------------------------------------------------------------------
# JavaScript extraction snippets (var-based for Selenium execute_script)
# ---------------------------------------------------------------------------

# Extract all JSON-LD script blocks and return as JSON string array
EXTRACT_JSONLD_JS = """
var scripts = document.querySelectorAll('script[type="application/ld+json"]');
var blocks = [];
for (var i = 0; i < scripts.length; i++) {
    try {
        var raw = scripts[i].textContent.trim();
        if (raw) {
            blocks.push(raw);
        }
    } catch(e) {}
}
return JSON.stringify(blocks);
"""

# Count JSON-LD blocks — used by WebDriverWait to poll for their presence
COUNT_JSONLD_JS = """
return document.querySelectorAll('script[type="application/ld+json"]').length;
"""

# Detect common block pages (Akamai, Cloudflare, generic access denied)
DETECT_BLOCK_JS = """
var bodyText = document.body ? document.body.innerText.toUpperCase() : '';
if (bodyText.indexOf('UNFORTUNATELY WE ARE UNABLE TO GIVE YOU ACCESS') !== -1) return 'akamai';
if (bodyText.indexOf('JUST A MOMENT') !== -1) return 'cloudflare';
if (bodyText.indexOf('ACCESS DENIED') !== -1) return 'generic';
if (bodyText.indexOf('ROBOT CHECK') !== -1) return 'captcha';
if (bodyText.indexOf('UNUSUAL ACTIVITY') !== -1) return 'suspicious';
return null;
"""

# Accept cookie consent — tries multiple common selectors
ACCEPT_COOKIES_JS = """
var selectors = [
    'button[class*="accept"]',
    'button[class*="cookie"]',
    'button[class*="consent"]',
    'a[class*="consent"]',
    '[data-testid*="cookie"]',
    '#onetrust-accept-btn-handler',
    '[data-auto-id="accept-cookie-btn"]',
    '.cookie-accept',
    '#accept-cookies',
    'button[aria-label*="Accept"]',
    'button[aria-label*="accept"]',
    'button[aria-label*="Cookie"]'
];
for (var i = 0; i < selectors.length; i++) {
    try {
        var btn = document.querySelector(selectors[i]);
        if (btn && btn.offsetParent !== null) {
            btn.click();
            return 'clicked: ' + selectors[i];
        }
    } catch(e) {}
}
// Also try by text content
var allBtns = document.querySelectorAll('button, a, [role="button"]');
for (var j = 0; j < allBtns.length; j++) {
    var txt = (allBtns[j].textContent || '').trim().toLowerCase();
    if (txt === 'accept' || txt === 'accept all cookies' || txt === 'accept all'
        || txt === 'agree' || txt === 'agree all' || txt === 'got it'
        || txt === 'i agree' || txt === 'yes' || txt === 'okay') {
        try {
            allBtns[j].click();
            return 'clicked by text: ' + txt;
        } catch(e) {}
    }
}
return 'no_cookie_banner';
"""


def parse_jsonld_blocks(raw_blocks_json: str) -> list[dict[str, Any]]:
    """Parse the JSON string of JSON-LD blocks into a list of dicts."""
    blocks: list[dict[str, Any]] = []
    try:
        raw_list: list[str] = json.loads(raw_blocks_json)
        for raw in raw_list:
            try:
                data = json.loads(raw)
                if isinstance(data, list):
                    blocks.extend(data)
                elif isinstance(data, dict):
                    blocks.append(data)
            except json.JSONDecodeError:
                continue
    except json.JSONDecodeError:
        pass
    return blocks


def find_product_block(
    blocks: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Find the JSON-LD block with @type 'Product' or 'ProductGroup'.

    Prefers a Product block that has 'offers' (the primary data block).
    Falls back to any Product type block.
    """
    # First pass: find Product block WITH offers (primary data block)
    for block in blocks:
        atype = block.get("@type", "")
        types = [atype] if isinstance(atype, str) else list(atype)
        if any(t in ("Product", "ProductGroup") for t in types):
            if block.get("offers"):
                return block
    # Second pass: any Product block
    for block in blocks:
        atype = block.get("@type", "")
        types = [atype] if isinstance(atype, str) else list(atype)
        if any(t in ("Product", "ProductGroup") for t in types):
            return block
    return None


def extract_product_from_jsonld(
    product_data: dict[str, Any],
    url: str,
    src_url: str,
    index: int,
) -> Product:
    """Map JSON-LD Product block fields to a Product dataclass."""
    product = Product(
        id=index,
        url=url,
        src_url=src_url,
        status_code=200,
        scraped_at=datetime.now(timezone.utc).isoformat(),
    )

    # Title
    product.title = str(product_data.get("name", "")).strip()

    # Brand
    brand_data = product_data.get("brand")
    if isinstance(brand_data, dict):
        product.brand = str(brand_data.get("name", "")).strip()
    elif isinstance(brand_data, str):
        product.brand = brand_data.strip()

    # SKU & MPN
    product.sku = str(product_data.get("sku", "")).strip()
    product.mpn = str(product_data.get("mpn", "")).strip()

    # Description — clean HTML tags if present
    desc = product_data.get("description", "")
    if isinstance(desc, str):
        product.description = re.sub(r"<[^>]+>", " ", desc)
        product.description = re.sub(r"&[a-zA-Z]+;", " ", product.description)
        product.description = re.sub(r"\s+", " ", product.description).strip()

    # Images — scope to product gallery only
    raw_images = product_data.get("image")
    if isinstance(raw_images, str):
        raw_images = [raw_images]
    if isinstance(raw_images, list):
        filtered_images: list[str] = []
        skip_patterns = [
            "/brand.assets/",
            "/emoji/",
            "/flags/",
            "/icon/",
            "/navigation/",
        ]
        for img_url in raw_images:
            img_str = str(img_url)
            if any(pattern.lower() in img_str.lower() for pattern in skip_patterns):
                continue
            filtered_images.append(img_str)
        product.images = filtered_images[:15]  # Cap at 15 product images

    # Offers — price, currency, availability, original price
    offers_data = product_data.get("offers")
    if isinstance(offers_data, list):
        offers_data = offers_data[0] if offers_data else None

    if isinstance(offers_data, dict):
        currency = str(offers_data.get("priceCurrency", "USD")).strip()
        product.currency = currency

        # Current price
        raw_price = offers_data.get("price")
        if raw_price is not None:
            product.price = format_price(raw_price, currency)

        # Original price via highPrice (if > price, it's on sale)
        raw_high = offers_data.get("highPrice")
        raw_low = offers_data.get("lowPrice")
        if raw_high is not None:
            try:
                high = float(raw_high)
                current = float(raw_price) if raw_price else 0
                if high > current:
                    product.original_price = format_price(raw_high, currency)
            except (ValueError, TypeError):
                pass
        elif raw_low is not None:
            try:
                low = float(raw_low)
                current = float(raw_price) if raw_price else 0
                if low > current:
                    product.original_price = format_price(raw_low, currency)
            except (ValueError, TypeError):
                pass

        # If no original price via highPrice, check for multiple offers
        all_offers = product_data.get("offers", [])
        if (
            isinstance(all_offers, list)
            and len(all_offers) > 1
            and not product.original_price
        ):
            for offer in all_offers:
                offer_price = offer.get("price")
                offer_currency = str(offer.get("priceCurrency", currency)).strip()
                try:
                    op = float(offer_price) if offer_price else 0
                    cp = float(raw_price) if raw_price else 0
                    if op > cp:
                        product.original_price = format_price(
                            offer_price, offer_currency
                        )
                        break
                except (ValueError, TypeError):
                    continue

        # Availability
        raw_avail = offers_data.get("availability", "")
        product.availability = normalize_availability(str(raw_avail).strip())

        # URL from offers
        offer_url = offers_data.get("url", "")
        if offer_url and not product.url:
            product.url = offer_url

    # Aggregate rating
    rating_data = product_data.get("aggregateRating")
    if isinstance(rating_data, dict):
        rv = rating_data.get("ratingValue")
        if rv is not None:
            product.rating = str(rv)
        rc = rating_data.get("reviewCount")
        if rc is not None:
            product.review_count = str(rc)

    # Individual reviews (top-level Product.review array)
    raw_reviews = product_data.get("review")
    if isinstance(raw_reviews, list):
        for rev in raw_reviews[:20]:  # Cap at 20 reviews
            if not isinstance(rev, dict):
                continue
            review_entry: dict[str, Any] = {}
            author = rev.get("author")
            if isinstance(author, dict):
                review_entry["author"] = author.get("name", "")
            elif isinstance(author, str):
                review_entry["author"] = author
            review_entry["rating"] = rev.get("reviewRating", {})
            if isinstance(review_entry["rating"], dict):
                review_entry["rating"] = review_entry["rating"].get("ratingValue", "")
            review_body = rev.get("reviewBody", "")
            if review_body:
                review_entry["body"] = review_body
            if review_entry:
                product.reviews.append(review_entry)

    # URL fallback
    if not product.url:
        product.url = url

    return product


def log_diagnostic_info(driver: Any, index: int, url: str) -> None:
    """Log diagnostic information to help debug extraction failures."""
    try:
        page_title = driver.title
        logger.warning(f"  [{index}] DIAGNOSTIC — Page title: '{page_title}'")
    except Exception:
        logger.warning(f"  [{index}] DIAGNOSTIC — Could not get page title")

    try:
        source_len = len(driver.page_source)
        logger.warning(f"  [{index}] DIAGNOSTIC — Page source length: {source_len} chars")
    except Exception:
        pass

    try:
        body_snippet = driver.execute_script(
            "return document.body ? document.body.innerText.substring(0, 500) : 'NO BODY'"
        )
        logger.warning(f"  [{index}] DIAGNOSTIC — Body snippet: {body_snippet[:300]}")
    except Exception:
        logger.warning(f"  [{index}] DIAGNOSTIC — Could not read body text")

    try:
        ld_count = driver.execute_script(COUNT_JSONLD_JS)
        logger.warning(f"  [{index}] DIAGNOSTIC — JSON-LD block count: {ld_count}")
    except Exception:
        logger.warning(f"  [{index}] DIAGNOSTIC — Could not count JSON-LD blocks")

    logger.warning(f"  [{index}] DIAGNOSTIC — URL: {url}")


def warmup_session(driver: Any, retry_count: int = 0) -> bool:
    """Visit homepage first to establish Akamai _abck cookies and sensor data.

    This is CRITICAL for Akamai-protected sites. Without visiting the homepage
    and waiting for sensor data collection, direct product URL navigation
    triggers 403 or blank pages.

    Supports retry with cooldown for transient proxy issues.

    Steps:
      1. Navigate to homepage
      2. Wait WARMUP_WAIT seconds for Akamai sensor data collection
      3. Check for block page
      4. Accept cookie consent if present
      5. Verify homepage loaded successfully
    """
    attempt_label = f" (attempt {retry_count + 1}/{WARMUP_MAX_RETRIES + 1})" if retry_count > 0 else ""
    logger.info(f"Warming up session{attempt_label}: visiting {SITE_URL}")
    driver.uc_open_with_reconnect(SITE_URL, reconnect_time=UC_RECONNECT_TIME)

    logger.info(
        f"Waiting {WARMUP_WAIT}s for Akamai sensor data collection "
        f"and _abck cookie establishment..."
    )
    time.sleep(WARMUP_WAIT)

    # Check if homepage loaded (early blank-page detection)
    try:
        source_len = len(driver.page_source)
        page_title = driver.title.strip()
        if source_len < MIN_SOURCE_LENGTH and not page_title:
            logger.error(
                f"WARMUP FAILED — Homepage returned blank page "
                f"(source_len={source_len}, title='{page_title}')"
            )
            if retry_count < WARMUP_MAX_RETRIES:
                logger.info(
                    f"Retrying warmup in {WARMUP_RETRY_COOLDOWN}s..."
                )
                time.sleep(WARMUP_RETRY_COOLDOWN)
                return warmup_session(driver, retry_count + 1)
            else:
                logger.error(
                    "Max warmup retries exhausted. Proxy may be returning "
                    "empty responses. Try --no-proxy or a different proxy tier."
                )
                return False
    except Exception as e:
        logger.error(f"WARMUP FAILED — Error reading page source: {e}")
        if retry_count < WARMUP_MAX_RETRIES:
            logger.info(f"Retrying warmup in {WARMUP_RETRY_COOLDOWN}s...")
            time.sleep(WARMUP_RETRY_COOLDOWN)
            return warmup_session(driver, retry_count + 1)
        return False

    # Detect block pages on homepage
    block_type = driver.execute_script(DETECT_BLOCK_JS)
    if block_type:
        logger.error(
            f"WARMUP FAILED — {block_type.upper()} block detected on homepage"
        )
        log_diagnostic_info(driver, 0, SITE_URL)
        if retry_count < WARMUP_MAX_RETRIES:
            logger.info(f"Retrying warmup in {WARMUP_RETRY_COOLDOWN}s...")
            time.sleep(WARMUP_RETRY_COOLDOWN)
            return warmup_session(driver, retry_count + 1)
        return False

    logger.info(f"Homepage loaded successfully (title: '{driver.title}')")

    # Accept cookie consent if present
    try:
        cookie_result = driver.execute_script(ACCEPT_COOKIES_JS)
        if cookie_result and cookie_result != "no_cookie_banner":
            logger.info(f"Cookie consent: {cookie_result}")
            time.sleep(2)  # Allow consent dialog to close
        else:
            logger.info("No cookie consent banner found")
    except Exception as e:
        logger.warning(f"Error checking cookie consent: {e}")

    # Verify homepage has meaningful content after warmup
    try:
        final_source_len = len(driver.page_source)
        logger.info(f"Warm-up complete{attempt_label} (homepage source: {final_source_len} chars)")
    except Exception:
        pass

    return True


def scrape_product_page(
    driver: Any,
    url: str,
    src_url: str,
    index: int,
) -> Product:
    """Navigate to a product URL and extract data from JSON-LD.

    Uses WebDriverWait polling to wait for JSON-LD blocks to appear
    (up to JSONLD_WAIT_TIMEOUT seconds), since the page is fully JS-rendered.
    """
    logger.info(f"  [{index}] Loading: {url}")

    # Navigate to product page
    driver.uc_open_with_reconnect(url, reconnect_time=UC_RECONNECT_TIME)

    # Allow JS framework to begin initialization before polling
    logger.info(
        f"  [{index}] Waiting {INITIAL_RENDER_WAIT}s for JS framework to initialize..."
    )
    time.sleep(INITIAL_RENDER_WAIT)

    # ---- Early blank-page detection (CRITICAL — saves 25s per failure) ----
    try:
        source_len = len(driver.page_source)
        page_title = driver.title.strip() if driver.title else ""
        if source_len < MIN_SOURCE_LENGTH and not page_title:
            logger.warning(
                f"  [{index}] BLANK PAGE DETECTED — "
                f"source_len={source_len}, title='{page_title}'. "
                f"Proxy may be returning empty responses."
            )
            return Product(
                id=index,
                url=url,
                src_url=src_url,
                status_code=0,
                scraped_at=datetime.now(timezone.utc).isoformat(),
                remarks="PROXY_BLANK_PAGE — Proxy returned empty response",
            )
    except Exception:
        pass

    # Detect block pages
    block_type = driver.execute_script(DETECT_BLOCK_JS)
    if block_type:
        logger.warning(f"  [{index}] {block_type.upper()} block detected on {url}")
        log_diagnostic_info(driver, index, url)
        return Product(
            id=index,
            url=url,
            src_url=src_url,
            status_code=403,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            remarks=f"{block_type.upper()} BLOCKED",
        )

    # Wait for JSON-LD blocks to appear using WebDriverWait polling
    jsonld_found = False
    try:
        logger.info(
            f"  [{index}] Polling for JSON-LD blocks "
            f"(timeout={JSONLD_WAIT_TIMEOUT}s, poll=1s)..."
        )
        wait = WebDriverWait(driver, JSONLD_WAIT_TIMEOUT, poll_frequency=1.0)
        wait.until(lambda d: d.execute_script(COUNT_JSONLD_JS) > 0)
        jsonld_found = True
        ld_count = driver.execute_script(COUNT_JSONLD_JS)
        logger.info(f"  [{index}] JSON-LD blocks found: {ld_count}")
    except Exception:
        logger.warning(
            f"  [{index}] JSON-LD blocks did NOT appear within "
            f"{JSONLD_WAIT_TIMEOUT}s timeout"
        )
        log_diagnostic_info(driver, index, url)

    if not jsonld_found:
        return Product(
            id=index,
            url=url,
            src_url=src_url,
            status_code=200,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            remarks="No JSON-LD blocks found after waiting for JS render",
        )

    # Extract JSON-LD blocks from rendered DOM
    raw_blocks_json = driver.execute_script(EXTRACT_JSONLD_JS) or "[]"

    blocks = parse_jsonld_blocks(raw_blocks_json)
    product_block = find_product_block(blocks)

    if not product_block:
        logger.warning(
            f"  [{index}] JSON-LD blocks found but no Product type block on {url}"
        )
        block_types = [b.get("@type", "unknown") for b in blocks]
        logger.warning(f"  [{index}] Block types found: {block_types}")
        return Product(
            id=index,
            url=url,
            src_url=src_url,
            status_code=200,
            scraped_at=datetime.now(timezone.utc).isoformat(),
            remarks=f"No Product JSON-LD block found. Types: {block_types}",
        )

    product = extract_product_from_jsonld(product_block, url, src_url, index)

    if product.title:
        logger.info(
            f"  [{index}] {product.title[:60]} — {product.price} "
            f"({product.availability})"
        )
    else:
        logger.warning(f"  [{index}] No title extracted from JSON-LD: {url}")

    return product


def load_urls_from_file(filepath: str) -> list[str]:
    """Load product URLs from a JSON file."""
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("urls", [])


def build_sb_proxy_arg(proxy_url: str) -> str:
    """Convert a proxy URL from our config format to SeleniumBase format.

    SeleniumBase expects: ``host:port`` or ``user:pass@host:port``.
    Our build_proxy_url returns: ``http://user:pass@host:port``.

    We pass the full URL to SeleniumBase which internally handles parsing.
    However, SeleniumBase's proxy parameter expects the format without
    the http:// scheme prefix — just user:pass@host:port.

    For Bright Data superproxy, we also try the format with country-code
    appended to the username for better residential routing.
    """
    # Remove the scheme prefix if present
    cleaned = proxy_url
    if cleaned.startswith("http://"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("https://"):
        cleaned = cleaned[8:]
    return cleaned


def main() -> None:
    """Main entry point for the scraper."""
    parser = argparse.ArgumentParser(
        description=f"Scraper for {SITE_NAME} (SeleniumBase UC Mode)",
    )
    parser.add_argument("--sample", action="store_true", help="Scrape only 5 products")
    parser.add_argument("--limit", type=int, default=None, help="Max products to scrape")
    parser.add_argument(
        "--input", type=str, default=None, help="Path to input URLs JSON file"
    )
    parser.add_argument(
        "--urls", nargs="+", default=None, help="Product URLs as arguments"
    )
    parser.add_argument(
        "--no-proxy", action="store_true", help="Skip proxy, connect directly"
    )
    parser.add_argument(
        "--xvfb", action="store_true", help="Use Xvfb virtual display (Docker)"
    )
    parser.add_argument(
        "--proxy-tier",
        type=str,
        default="residential",
        choices=["datacenter", "residential"],
        help="Proxy tier to use (default: residential — datacenter is blocked)",
    )
    args = parser.parse_args()

    start_time = time.time()

    logger.info("=" * 80)
    logger.info(f"Starting SeleniumBase UC Mode scraper for {SITE_NAME}")
    logger.info(f"Site: {SITE_URL}")
    logger.info(f"Extraction: JSON-LD only (all CSS selectors return NOT FOUND)")
    logger.info(
        f"JSON-LD wait: {INITIAL_RENDER_WAIT}s initial + {JSONLD_WAIT_TIMEOUT}s poll"
    )
    logger.info(f"Warmup: {WARMUP_WAIT}s on homepage (Akamai _abck cookie)")
    logger.info(f"Warmup retries: {WARMUP_MAX_RETRIES} (cooldown {WARMUP_RETRY_COOLDOWN}s)")
    logger.info(f"Output: {OUTPUT_FILE}")
    logger.info(f"Xvfb: {args.xvfb}")
    logger.info("=" * 80)

    # --- Load product URLs ---
    product_urls: list[str] = []

    if args.urls:
        product_urls = list(args.urls)
    elif args.input:
        product_urls = load_urls_from_file(args.input)
    elif os.path.exists(INPUT_FILE):
        product_urls = load_urls_from_file(INPUT_FILE)

    if not product_urls:
        logger.error(
            "No product URLs provided. Use --urls, --input, or create input_urls.json"
        )
        sys.exit(1)

    if args.sample:
        product_urls = product_urls[:5]
    if args.limit:
        product_urls = product_urls[: args.limit]

    logger.info(f"Total products to scrape: {len(product_urls)}")

    # --- Build proxy configuration ---
    proxy_server: Optional[str] = None
    proxy_tier = args.proxy_tier

    if not args.no_proxy:
        if should_warn_residential(proxy_tier):
            warn_residential_usage(SITE_URL, proxy_config)
        proxy_server = build_proxy_url(proxy_tier, proxy_config)
        if proxy_server:
            logger.info(f"Using {proxy_tier} proxy")
        else:
            logger.warning(
                f"No {proxy_tier} proxy configured, connecting directly"
            )

    sb_kwargs: dict[str, Any] = {
        "uc": True,
        "xvfb": args.xvfb,
    }

    if proxy_server:
        # Convert to SeleniumBase proxy format (strip http:// scheme)
        sb_proxy_arg = build_sb_proxy_arg(proxy_server)
        sb_kwargs["proxy"] = sb_proxy_arg
        # Safe display for logs — hide credentials
        safe_display = sb_proxy_arg.split("@")[-1] if "@" in sb_proxy_arg else sb_proxy_arg
        logger.info(f"Proxy endpoint: {safe_display}")

    results: list[dict[str, Any]] = []
    failed = 0

    with SB(**sb_kwargs) as sb:
        driver = sb.driver
        driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)

        try:
            # ---- CRITICAL: Warmup session on homepage FIRST ----
            logger.info("=" * 80)
            warmup_ok = warmup_session(driver)
            if not warmup_ok:
                logger.error(
                    "Homepage warmup failed after all retries. Proceeding anyway, "
                    "but expect blocking/blank pages on product pages."
                )
            else:
                logger.info(
                    "Warmup successful — Akamai cookies should be established"
                )
            logger.info("=" * 80)

            for i, url in enumerate(product_urls):
                src_url = url  # src_url = product URL when coming from input
                try:
                    product = scrape_product_page(driver, url, src_url, i + 1)
                    results.append(product.to_dict())
                    if not product.title:
                        failed += 1
                except Exception as e:
                    logger.error(
                        f"  [{i + 1}/{len(product_urls)}] Error scraping {url}: {e}"
                    )
                    try:
                        log_diagnostic_info(driver, i + 1, url)
                    except Exception:
                        pass
                    results.append(
                        Product(
                            id=i + 1,
                            url=url,
                            src_url=src_url,
                            status_code=0,
                            scraped_at=datetime.now(timezone.utc).isoformat(),
                            remarks=str(e),
                        ).to_dict()
                    )
                    failed += 1

                # Progress reporting
                if (i + 1) % 25 == 0:
                    percent = ((i + 1) / len(product_urls)) * 100
                    logger.info(
                        f"Progress: [{i + 1}/{len(product_urls)}] ({percent:.1f}%)"
                    )

                # Rate limiting delay between requests
                if i < len(product_urls) - 1:
                    time.sleep(DELAY_BETWEEN_REQUESTS)

        except Exception as exc:
            logger.error(f"Fatal browser session error: {exc}")

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

    os.makedirs(
        os.path.dirname(OUTPUT_FILE) if os.path.dirname(OUTPUT_FILE) else ".",
        exist_ok=True,
    )
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    logger.info("=" * 80)
    logger.info("EXTRACTION COMPLETE")
    logger.info(
        f"Total: {len(results)}, Success: {len(results) - failed}, Failed: {failed}"
    )
    logger.info(f"Duration: {round(time.time() - start_time, 2)}s")
    logger.info(f"Output: {OUTPUT_FILE}")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
