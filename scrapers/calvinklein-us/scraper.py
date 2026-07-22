#!/usr/bin/env python3
"""Calvin Klein US — HTTP Navigation Scraper.

Two-phase architecture:
  Phase 1: Discover item URLs by navigating the women's category page via
           browser_service POST /navigate, then paginating. Each page fetch
           is one POST /navigate call; link extraction + pagination are
           computed locally on the returned HTML.
  Phase 2: Extract structured data from each discovered item page. Item
           fetches run concurrently in a ThreadPoolExecutor; each item is
           one POST /navigate call. JSON-LD + CSS parsing happen locally.

Usage:
    python3 scraper_draft.py                                    # default: full discovery
    python3 scraper_draft.py --sample                           # first 5 items only
    python3 scraper_draft.py --limit 50                         # cap at 50 items
    python3 scraper_draft.py --query "jeans"                    # custom search query
    python3 scraper_draft.py --category-url "https://..."       # custom category URL
    python3 scraper_draft.py --no-proxy                         # force no proxy
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import httpx
from bs4 import BeautifulSoup

# Make src.* importable (scraper runs from scrapers/{slug}/).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from src.page_analysis import extract_jsonld  # noqa: E402

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

SITE_NAME = "Calvin Klein US"
SITE_URL = "https://www.calvinklein.us"
PLATFORM = "custom"
SITE_SLUG = "calvinklein-us"

# ── Execution model ─────────────────────────────────────────────────────────
BROWSER_SERVICE_URL = os.environ.get("BROWSER_SERVICE_URL", "http://browser_service:8001")

# Calvin Klein US has anti-bot protection — stealth/cloak is required.
STEALTH = "cloak"
_env_stealth = (os.environ.get("STEALTH_BROWSER") or os.environ.get("SCRAPER_STEALTH") or "").strip().lower()
if _env_stealth in ("cloak", "true", "1"):
    STEALTH = "cloak"

NAVIGATE_TIMEOUT = 120
MAX_RETRIES = 3
BACKOFF_BASE = 2.0
PHASE2_WORKERS = 4

# ── Phase 1: Navigation ─────────────────────────────────────────────────────
DEFAULT_QUERY = "women"
# The confirmed working listing URL from navigation_analysis.
DEFAULT_LISTING_URL = "https://www.calvinklein.us/en/women?ab=global_banner_1_cta_w_L1_260716"
SEARCH_URL_PATTERN = "https://www.calvinklein.us/en/women?ab=global_banner_1_cta_w_L1_260716"
SEARCH_BOX_SELECTOR = ""
SEARCH_SUBMIT_SELECTOR = ""
CATEGORY_URLS = []

# ── Phase 1: Pagination ─────────────────────────────────────────────────────
PAGINATION_TYPE = "page_param"
NEXT_BUTTON_SELECTOR = ""
PAGE_PARAM_NAME = "page"
ITEMS_PER_PAGE = 48
MAX_PAGES = 20
TOTAL_COUNT_SELECTOR = ""
DISCOVERY_DEADLINE_SECONDS = 300

# ── Phase 1: Item link extraction ───────────────────────────────────────────
# Code Writer adapted: SPA product tile selectors for Calvin Klein category pages
ITEM_CONTAINER_SELECTOR = "[class*='product-tile'], [class*='product-card'], [class*='product-item'], [data-product-id], [class*='ProductTile'], [class*='ProductCard']"
ITEM_LINK_SELECTOR = "a[href*='.html']"
# Code Writer adapted: broadened pattern — actual URLs have 4+ path segments
# e.g. /en/women/apparel/tops/sculpt-denim-trucker-button-down-shirt/47G749G-N8D.html
ITEM_URL_PATTERN = r"/en/[^?]*\.html"

# ── Phase 2: Extraction ─────────────────────────────────────────────────────
SCRAPING_METHOD = "http_navigation"
PROXY_TIER = "none"
DELAY_BETWEEN_REQUESTS = 2.0

# ── Output ──────────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_KEY = "products"
CONTENT_TYPE = "product"
CURRENCY = "USD"

SRC_URL = SITE_URL

_CONTENT_FILTER_FIELDS = {
    "product": ["price", "availability"],
    "article": ["author", "publish_date"],
    "job_posting": ["company", "location"],
    "forum_thread": ["author"],
    "serp": ["url", "snippet"],
    "page_content": [],
}
CORE_FILTER_FIELDS = _CONTENT_FILTER_FIELDS.get(CONTENT_TYPE, [])

# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════════════

LOG_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, f"{SITE_SLUG}.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
logger = logging.getLogger(SITE_SLUG)

# ═══════════════════════════════════════════════════════════════════════════════
# CHECKPOINT
# ═══════════════════════════════════════════════════════════════════════════════

_CHECKPOINT_PATH = os.path.join(SCRIPT_DIR, "discovered_urls_checkpoint.json")


def _write_checkpoint(urls: list[str]) -> None:
    try:
        with open(_CHECKPOINT_PATH, "w") as f:
            json.dump({"urls": list(urls), "count": len(urls), "ts": time.time()}, f)
        logger.debug("Checkpoint: saved %d URLs to %s", len(urls), _CHECKPOINT_PATH)
    except Exception as exc:
        logger.warning("Checkpoint: write failed: %s", exc)


def _load_checkpoint() -> list[str]:
    try:
        if os.path.isfile(_CHECKPOINT_PATH):
            with open(_CHECKPOINT_PATH, "r") as f:
                data = json.load(f)
            urls = data.get("urls", [])
            if urls:
                logger.info("Checkpoint: RESUMING with %d URLs from %s", len(urls), _CHECKPOINT_PATH)
                return urls
    except Exception as exc:
        logger.warning("Checkpoint: load failed: %s", exc)
    return []


# ═══════════════════════════════════════════════════════════════════════════════
# CORE HTTP PRIMITIVE — one POST /navigate per page.
# ═══════════════════════════════════════════════════════════════════════════════


def _navigate(url, actions=None, extract=None, retry=0):
    """POST /navigate with exponential backoff. Returns the response dict or None."""
    payload = {
        "url": url,
        "actions": actions or [],
        "extract": extract or {},
        "stealth": "cloak" if str(STEALTH).lower() == "cloak" else "none",
        "proxy_tier": PROXY_TIER if (PROXY_TIER and PROXY_TIER != "{PROXY_TIER}") else "none",
        "timeout": NAVIGATE_TIMEOUT,
        "return_what": "all",
    }
    endpoint = f"{BROWSER_SERVICE_URL}/navigate"
    for attempt in range(MAX_RETRIES):
        try:
            r = httpx.post(endpoint, json=payload, timeout=NAVIGATE_TIMEOUT + 30)
            if r.status_code == 200:
                data = r.json()
                if data.get("success"):
                    return data
                if data.get("blocked"):
                    logger.warning("navigate: BLOCKED on %s", url[:80])
                    return data
            elif r.status_code == 404:
                logger.debug("navigate: 404 on %s (terminal)", url[:80])
                return {"success": False, "url": url, "html": "", "status_code": 404}

            if r.status_code in (429, 502, 503):
                retry_after = r.headers.get("retry_after") or r.headers.get("Retry-After") or 5
                try:
                    retry_after = int(retry_after)
                except (TypeError, ValueError):
                    retry_after = 5
                logger.debug(
                    "navigate: %d on %s, backing off %ds (attempt %d/%d)",
                    r.status_code, url[:60], retry_after, attempt + 1, MAX_RETRIES,
                )
                time.sleep(retry_after)
                continue
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            logger.debug(
                "navigate: transient error on %s: %s (attempt %d/%d)",
                url[:60], exc, attempt + 1, MAX_RETRIES,
            )

        time.sleep(min(BACKOFF_BASE ** (attempt + retry), 30))

    logger.warning("navigate: exhausted %d retries on %s", MAX_RETRIES, url[:80])
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# URL HELPERS
# ═══════════════════════════════════════════════════════════════════════════════


def _make_absolute(href: str) -> str:
    if not href:
        return ""
    if href.startswith(("http://", "https://")):
        return href
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return SITE_URL.rstrip("/") + href
    return SITE_URL.rstrip("/") + "/" + href


def _is_product_url(href: str) -> bool:
    """Calvin Klein US product URL detector.

    Product URLs match: /en/{category}/{product-slug}/{SKU}-{color}.html
    e.g. /en/women/apparel/tops/sculpt-denim-trucker-button-down-shirt/47G749G-N8D.html
    """
    if not href:
        return False
    site_host = (urlparse(SITE_URL).hostname or "").lower()
    if site_host and site_host not in href.lower():
        return False
    path = urlparse(href).path.strip("/")
    if not path or len(path) < 6:
        return False
    # Must end with .html
    if not path.endswith(".html"):
        return False
    segs = path.split("/")
    # Product URLs have at least: en / category / slug / SKU.html
    # but can have more: en/women/apparel/tops/slug/SKU.html
    if len(segs) < 4:
        return False
    last = segs[-1]
    # The last segment (before .html) should contain a digit (SKU or color code)
    sku_part = last.replace(".html", "")
    if not any(c.isdigit() for c in sku_part):
        return False
    return True


def _set_query_param(url: str, param: str, value) -> str:
    p = urlparse(url)
    qs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k != param]
    qs.append((param, str(value)))
    return urlunparse(p._replace(query=urlencode(qs)))


_OFFSET_PARAMS = {"offset", "start", "skip", "begin", "from"}


def _extract_next_href(html: str) -> Optional[str]:
    if not html:
        return None
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return None
    for sel in ('a[rel="next"]', "a.next", "li.next a", 'a[aria-label="Next"]'):
        try:
            el = soup.select_one(sel)
            if el and el.get("href"):
                return _make_absolute(el["href"])
        except Exception:
            pass
    return None


def _get_next_page_url(final_url: str, next_page_num: int, html: str = None) -> Optional[str]:
    """Construct the URL for the next page of results."""
    if PAGE_PARAM_NAME and PAGE_PARAM_NAME not in ("", "{PAGE_PARAM_NAME}"):
        if PAGINATION_TYPE in ("page_param", "", None):
            if PAGE_PARAM_NAME in _OFFSET_PARAMS:
                value = (next_page_num - 1) * (ITEMS_PER_PAGE or 48)
            else:
                value = next_page_num
            return _set_query_param(final_url, PAGE_PARAM_NAME, value)
    if html:
        href = _extract_next_href(html)
        if href:
            return href
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 1: URL DISCOVERY
# ═══════════════════════════════════════════════════════════════════════════════

_STOP_REASON_PRIORITY = {
    "navigate_error": 5,
    "dedup_flat": 4,
    "max_pages_hit": 3,
    "no_new_items": 2,
    "short_page": 1,
    "no_next_link": 0,
    "skipped": -1,
}


def _merge_stop_reason(current: str, new: str) -> str:
    if _STOP_REASON_PRIORITY.get(new, 0) > _STOP_REASON_PRIORITY.get(current, 0):
        return new
    return current


def _extract_item_links(html: str) -> list[str]:
    """Extract item page URLs from listing HTML — multi-tier fallback.

    Code Writer adapted: Calvin Klein US is a React SPA. The category page
    may render product tiles client-side after scroll. We extract from:
      Tier 1: container + link selector (product tiles)
      Tier 2: bare link selector
      Tier 3: broad fallback — all anchors matching /en/...*.html pattern
      Tier 4: regex scan of raw HTML for href=".../en/.../*.html" strings
    """
    if not html:
        return []
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as exc:
        logger.warning("Phase 1: HTML parse failed: %s", exc)
        return []

    links: list[str] = []

    # Tier 1: container + link selector (product tiles).
    if ITEM_CONTAINER_SELECTOR:
        try:
            containers = soup.select(ITEM_CONTAINER_SELECTOR)
            logger.debug("Phase 1: Tier 1 found %d containers", len(containers))
        except Exception as exc:
            logger.warning("Phase 1: bad ITEM_CONTAINER_SELECTOR %r: %s", ITEM_CONTAINER_SELECTOR, exc)
            containers = []
        for container in containers:
            try:
                matches = container.select(ITEM_LINK_SELECTOR) if ITEM_LINK_SELECTOR else []
            except Exception:
                matches = []
            for a in matches:
                href = a.get("href", "")
                if href:
                    links.append(_make_absolute(href))
        if links:
            logger.info("Phase 1: Tier 1 (containers) → %d links", len(links))

    # Tier 2: bare link selector.
    if not links and ITEM_LINK_SELECTOR:
        try:
            for a in soup.select(ITEM_LINK_SELECTOR):
                href = a.get("href", "")
                if href:
                    links.append(_make_absolute(href))
        except Exception as exc:
            logger.warning("Phase 1: bare link selector failed: %s", exc)

    # Tier 3: broad fallback — all anchors matching product URL pattern.
    if len(links) < 20:
        pattern = re.compile(ITEM_URL_PATTERN)
        existing = set(links)
        added = 0
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            if not href:
                continue
            url = _make_absolute(href)
            if not url or url in existing:
                continue
            if not pattern.search(url):
                continue
            if not _is_product_url(url):
                continue
            links.append(url)
            existing.add(url)
            added += 1
        if added:
            logger.info("Phase 1: Tier 3 (broad anchors) captured %d additional links", added)

    # Tier 4: regex scan of raw HTML for embedded href strings (SPA data
    # sometimes stores URLs in JSON data attributes or script tags that bs4
    # may not parse as clickable anchors).
    if len(links) < 20:
        pattern = re.compile(ITEM_URL_PATTERN)
        existing = set(links)
        added = 0
        # Find all href="..." and data-url="..." patterns in raw HTML
        for match in re.finditer(r'(?:href|data-url|data-href)=["\']([^"\']+)["\']', html):
            href = match.group(1)
            url = _make_absolute(href)
            if not url or url in existing:
                continue
            if not pattern.search(url):
                continue
            if not _is_product_url(url):
                continue
            links.append(url)
            existing.add(url)
            added += 1
        if added:
            logger.info("Phase 1: Tier 4 (regex scan) captured %d additional links", added)

    return list(dict.fromkeys(links))


def _discover_urls_via_category(
    category_url: str,
    max_pages: Optional[int] = None,
    limit: Optional[int] = None,
) -> tuple[list[str], str]:
    """Phase 1: Discover item URLs from a category/listing page."""
    logger.info("Phase 1: Browsing category → %s", category_url)

    # Code Writer adapted: add actions to scroll the page for SPA lazy loading.
    # Calvin Klein US is a React SPA — products load client-side after scrolling.
    # We do multiple scroll steps with waits to trigger lazy loading.
    actions = [
        {"type": "wait", "state": "domcontentloaded"},
        {"type": "sleep", "ms": 5000},
        {"type": "scroll", "y": 1000},
        {"type": "sleep", "ms": 3000},
        {"type": "scroll", "y": 2500},
        {"type": "sleep", "ms": 3000},
        {"type": "scroll", "y": 5000},
        {"type": "sleep", "ms": 3000},
        {"type": "scroll", "y": 8000},
        {"type": "sleep", "ms": 3000},
    ]

    resp = _navigate(category_url, actions=actions)
    if not resp or not resp.get("success"):
        blocked = bool(resp and resp.get("blocked"))
        logger.error(
            "Phase 1: category navigate failed for %s%s",
            category_url, " (blocked)" if blocked else "",
        )
        return [], "navigate_error"

    final_url = resp.get("url") or category_url
    html = resp.get("html", "")

    # Code Writer adapted: detect anti-bot error page and log it
    if html and ("something went wrong" in html.lower() or "ckdr:" in html.lower() or "error" in html[:500].lower()):
        logger.warning("Phase 1: category page may show error/anti-bot content (first 200 chars: %s)", html[:200])

    all_urls: list[str] = _extract_item_links(html)
    logger.info("Phase 1: category page 1 → %d items (final_url=%s)", len(all_urls), final_url[:80])

    stop_reason = "no_next_link"
    current_page = 1
    while True:
        if max_pages and current_page >= max_pages:
            logger.info("Phase 1: Reached max_pages=%d", max_pages)
            stop_reason = "max_pages_hit"
            break
        if limit and len(all_urls) >= limit:
            logger.info("Phase 1: Reached limit=%d", limit)
            stop_reason = "max_pages_hit"
            break

        next_url = _get_next_page_url(final_url, current_page + 1, html)
        if not next_url:
            logger.info("Phase 1: No more pages (stopped at page %d)", current_page)
            stop_reason = "no_next_link"
            break

        logger.info("Phase 1: Category page %d → %s", current_page + 1, next_url[:80])
        # Code Writer adapted: add scroll actions for lazy-loaded product grids
        page_actions = [
            {"type": "wait", "state": "domcontentloaded"},
            {"type": "sleep", "ms": 5000},
            {"type": "scroll", "y": 1000},
            {"type": "sleep", "ms": 3000},
            {"type": "scroll", "y": 2500},
            {"type": "sleep", "ms": 3000},
            {"type": "scroll", "y": 5000},
            {"type": "sleep", "ms": 3000},
            {"type": "scroll", "y": 8000},
            {"type": "sleep", "ms": 3000},
        ]
        resp = _navigate(next_url, actions=page_actions)
        if not resp or not resp.get("success"):
            logger.warning("Phase 1: page %d navigate failed, stopping", current_page + 1)
            stop_reason = "navigate_error"
            break

        final_url = resp.get("url") or next_url
        html = resp.get("html", "")
        new_urls = _extract_item_links(html)
        new_count = len(set(new_urls) - set(all_urls))
        logger.info(
            "Phase 1: Page %d → %d items (%d new)",
            current_page + 1, len(new_urls), new_count,
        )

        if new_count == 0:
            if not new_urls or (ITEMS_PER_PAGE and len(new_urls) < ITEMS_PER_PAGE):
                stop_reason = "short_page"
            else:
                stop_reason = "no_new_items"
            logger.info("Phase 1: page %d stopping (%s)", current_page + 1, stop_reason)
            break

        all_urls.extend(new_urls)
        current_page += 1
        if DELAY_BETWEEN_REQUESTS:
            time.sleep(DELAY_BETWEEN_REQUESTS)

    unique_urls = list(dict.fromkeys(all_urls))
    if limit:
        unique_urls = unique_urls[:limit]
    logger.info("Phase 1: Discovered %d total item URLs from category (%s)", len(unique_urls), stop_reason)
    return unique_urls, stop_reason


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 2: ITEM EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════════

# Code Writer adapted: CSS selector constants from field map
TITLE_SELECTORS = [
    'h1.product-name', 'h1[class*="product"]', 'h1[class*="name"]',
    'h1[class*="title"]', '[data-product-name]', '.pdp-title', 'h1',
]
PRICE_SELECTORS = [
    '[class*="price"][class*="current"]', '[class*="price"][class*="sale"]',
    '.product-price', '[data-price]', '[class*="price-value"]',
    '[data-testid*="price"]',
]
PRICE_FALLBACK_SELECTORS = ['[class*="price"]']
ORIGINAL_PRICE_SELECTORS = [
    '[class*="price"][class*="original"]', '[class*="price"][class*="compare"]',
    '[class*="price"][class*="was"]', '.original-price', '[class*="list-price"]',
    '[class*="strike"]', 'del', 's',
]
AVAILABILITY_SELECTORS = [
    '[class*="availability"]', '[class*="stock"]', '[data-in-stock]',
    '[class*="inventory"]', '.stock-status',
]
DESCRIPTION_SELECTORS = [
    '[class*="description"]', '[class*="details"]', '[class*="product-info"]',
    '.product-description', '[data-description]',
]
COLOR_SELECTORS = [
    '[class*="color"] [class*="selected"]', '[class*="swatch"][class*="active"]',
    '[class*="color-swatch"][aria-checked="true"]',
]
RATING_SELECTORS = [
    '[class*="rating"]', '[class*="star"]', '[class*="review"][class*="score"]',
    '[data-rating]',
]
REVIEW_COUNT_SELECTORS = [
    '[class*="review"][class*="count"]', '[class*="rating"][class*="count"]',
]
IMAGE_SELECTORS = [
    '[class*="product-image"] img', '[class*="gallery"] img',
    '[class*="thumbnail"] img', '[data-main-image] img', '.pdp-image img',
]
IMAGE_FALLBACK_SELECTORS = ['img[src*="product"]', 'img[src*="calvinklein"]']

# Image skip patterns (non-product images)
_IMAGE_SKIP_PATTERNS = [
    "/brand.assets/", "/emoji/", "/flags/", "/icon/", "/navigation/",
    "logo", "sprite", "pixel", "blank.gif", "swatch", "placeholder",
]


def _populate_from_jsonld(item: dict, jsonld_blocks: list[dict]) -> None:
    """Fill item from JSON-LD Product blocks."""
    for block in jsonld_blocks:
        block_type = block.get("@type", "")
        if isinstance(block_type, list):
            block_type = block_type[0] if block_type else ""

        if block_type == "Product":
            item["title"] = block.get("name", "") or item.get("title", "")
            offers = block.get("offers", {})
            if isinstance(offers, dict):
                offers_list = [offers]
            elif isinstance(offers, list):
                offers_list = offers
            else:
                offers_list = []

            for offer in offers_list:
                if not isinstance(offer, dict):
                    continue
                price = offer.get("price", "")
                currency = offer.get("priceCurrency", CURRENCY)
                if price:
                    item["price"] = _format_price(str(price), currency)
                    item["currency"] = currency
                    break

            avail = ""
            for offer in offers_list:
                if isinstance(offer, dict) and offer.get("availability"):
                    avail = offer["availability"]
                    break
            if avail:
                item["availability"] = "Out of Stock" if (
                    "OutOfStock" in avail or "SoldOut" in avail
                ) else "In Stock"

            if "currency" not in item:
                item["currency"] = CURRENCY

            item["description"] = block.get("description", "")

            # Original price from highPrice
            for offer in offers_list:
                if isinstance(offer, dict):
                    high_price = offer.get("highPrice", "")
                    if high_price:
                        item["original_price"] = _format_price(str(high_price), item.get("currency", CURRENCY))
                        break

            # Brand
            brand = block.get("brand", {})
            if isinstance(brand, dict):
                item["brand"] = brand.get("name", "Calvin Klein")
            elif isinstance(brand, str):
                item["brand"] = brand

            # SKU
            item["sku"] = block.get("sku", "") or block.get("mpn", "")

            # Color
            additional_props = block.get("additionalProperty", [])
            if isinstance(additional_props, list):
                for prop in additional_props:
                    if isinstance(prop, dict) and prop.get("name", "").lower() in ("color", "colour"):
                        item["color"] = prop.get("value", "")
                        break

            # Rating and review count
            agg_rating = block.get("aggregateRating", {})
            if isinstance(agg_rating, dict):
                item["rating"] = str(agg_rating.get("ratingValue", ""))
                item["review_count"] = str(agg_rating.get("reviewCount", ""))

            # Images
            images = block.get("image", [])
            if isinstance(images, str):
                images = [images]
            if images:
                filtered = _filter_images(images)
                if filtered:
                    item["images"] = filtered
            break


def _format_price(price_str: str, currency: str = "USD") -> str:
    """Format a price string with currency symbol."""
    if not price_str:
        return ""
    cleaned = re.sub(r"[^\d.,]", "", str(price_str)).strip()
    if not cleaned:
        return str(price_str)
    symbol = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency, "$")
    return f"{symbol}{cleaned}"


def _normalize_availability(avail_str: str) -> str:
    if not avail_str:
        return ""
    lower = avail_str.lower()
    if "instock" in lower or "in stock" in lower or "available" in lower:
        return "In Stock"
    if "outofstock" in lower or "out of stock" in lower or "soldout" in lower or "unavailable" in lower:
        return "Out of Stock"
    return "In Stock"


def _filter_images(images: list[str]) -> list[str]:
    """Filter out non-product images (logos, icons, banners, etc.)."""
    filtered = []
    seen = set()
    for src in images:
        if not src:
            continue
        lower_src = src.lower()
        if any(skip in lower_src for skip in _IMAGE_SKIP_PATTERNS):
            continue
        if src.startswith("data:"):
            continue
        if src.startswith("//"):
            src = "https:" + src
        clean = src.split("?")[0]
        if clean not in seen:
            seen.add(clean)
            filtered.append(src)
    return filtered


def _detect_soft_404(html: str, soup: BeautifulSoup, item_url: str) -> Optional[str]:
    """Detect soft 404 / error pages on Calvin Klein US."""
    if not html:
        return "Soft 404: empty page"

    # Check page title for error indicators
    title_tag = soup.find("title")
    title_text = (title_tag.get_text(strip=True) if title_tag else "").lower()
    if "error" in title_text or "not found" in title_text:
        return f"Soft 404: page title contains error indicator"

    # Check H1 for not-found indicators
    h1 = soup.find("h1")
    h1_text = (h1.get_text(strip=True) if h1 else "").lower()
    not_found_markers = ["not found", "unavailable", "discontinued", "no longer available", "something went wrong"]
    for marker in not_found_markers:
        if marker in h1_text:
            return f"Soft 404: h1 contains '{marker}'"

    # Check for error page text
    body_text = soup.get_text()[:5000].lower() if soup else ""
    if "oops! something went wrong" in body_text or "ckdr:" in body_text.lower():
        return "Soft 404: error page detected (anti-bot or server error)"

    return None


def _css_fallback_extract(soup: BeautifulSoup, item: dict, item_url: str) -> None:
    """Extract fields via CSS selectors when JSON-LD is unavailable.

    Code Writer adapted: implements the field map's CSS selectors.
    """

    def _try_selectors(selectors: list[str], fallback: list[str] = None) -> str:
        for sel in selectors:
            try:
                el = soup.select_one(sel)
                if el:
                    text = el.get_text(strip=True)
                    if text:
                        return text
            except Exception:
                pass
        if fallback:
            for sel in fallback:
                try:
                    el = soup.select_one(sel)
                    if el:
                        text = el.get_text(strip=True)
                        if text:
                            return text
                except Exception:
                    pass
        return ""

    # Title
    if not item.get("title"):
        title = _try_selectors(TITLE_SELECTORS)
        if title:
            item["title"] = title

    # Price
    if not item.get("price"):
        price = _try_selectors(PRICE_SELECTORS, PRICE_FALLBACK_SELECTORS)
        if price:
            currency = item.get("currency", CURRENCY)
            item["price"] = _format_price(price, currency)

    # Original price
    if not item.get("original_price"):
        orig_price = _try_selectors(ORIGINAL_PRICE_SELECTORS)
        if orig_price:
            currency = item.get("currency", CURRENCY)
            item["original_price"] = _format_price(orig_price, currency)

    # Availability
    if not item.get("availability"):
        avail = _try_selectors(AVAILABILITY_SELECTORS)
        if avail:
            item["availability"] = _normalize_availability(avail)

    # Description
    if not item.get("description"):
        desc = _try_selectors(DESCRIPTION_SELECTORS)
        if desc:
            item["description"] = desc

    # Color
    if not item.get("color"):
        color = _try_selectors(COLOR_SELECTORS)
        if color:
            item["color"] = color

    # Rating
    if not item.get("rating"):
        rating = _try_selectors(RATING_SELECTORS)
        if rating:
            item["rating"] = rating

    # Review count
    if not item.get("review_count"):
        review_count = _try_selectors(REVIEW_COUNT_SELECTORS)
        if review_count:
            item["review_count"] = review_count

    # Images
    if not item.get("images"):
        images: list[str] = []
        for sel in IMAGE_SELECTORS:
            try:
                els = soup.select(sel)
                for el in els:
                    src = el.get("src") or el.get("data-src") or ""
                    if src:
                        images.append(src)
                if images:
                    break
            except Exception:
                pass
        if not images:
            for sel in IMAGE_FALLBACK_SELECTORS:
                try:
                    els = soup.select(sel)
                    for el in els:
                        src = el.get("src") or el.get("data-src") or ""
                        if src:
                            images.append(src)
                    if images:
                        break
                except Exception:
                    pass
        filtered = _filter_images(images)
        if filtered:
            item["images"] = filtered

    # Currency (from meta tags)
    if not item.get("currency"):
        for sel in ('meta[property="product:price:currency"]', 'meta[property="og:price:currency"]'):
            try:
                el = soup.select_one(sel)
                if el and el.get("content"):
                    item["currency"] = el["content"]
                    break
            except Exception:
                pass
        if not item.get("currency"):
            item["currency"] = CURRENCY

    # Brand (known from domain)
    if not item.get("brand"):
        item["brand"] = "Calvin Klein"

    # SKU from URL
    if not item.get("sku"):
        sku_match = re.search(r"/([A-Z0-9]+-[A-Z0-9]+)\.html", item_url)
        if sku_match:
            item["sku"] = sku_match.group(1)

    # Category from URL
    if not item.get("category"):
        path = urlparse(item_url).path.strip("/")
        parts = path.split("/")
        if len(parts) >= 3:
            # /en/women/apparel/tops/product/SKU.html → women/apparel/tops
            cat_parts = [p for p in parts[1:-2] if p and p != "en"]
            if cat_parts:
                item["category"] = " > ".join(cat_parts)


def _error_item(url: str, src_url: str, error: str) -> dict:
    return {
        "url": url,
        "src_url": src_url,
        "status_code": 0,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": f"Error: {error[:200]}",
    }


def _extract_item(item_url: str, src_url: str) -> dict:
    """Phase 2: Extract structured data from a single item page."""
    resp = _navigate(item_url)
    if not resp:
        return _error_item(item_url, src_url, "navigate failed after retries")
    if resp.get("blocked"):
        return _error_item(item_url, src_url, "blocked (anti-bot wall)")
    if resp.get("status_code") == 404:
        return _error_item(item_url, src_url, "404 not found")

    html = resp.get("html", "")
    item: dict = {
        "url": item_url,
        "src_url": src_url,
        "status_code": 200,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": "",
    }

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as exc:
        logger.warning("Phase 2: HTML parse failed for %s: %s", item_url[:60], exc)
        soup = None

    # Soft 404 detection
    if soup:
        soft_404 = _detect_soft_404(html, soup, item_url)
        if soft_404:
            item["remarks"] = soft_404
            item["status_code"] = 404
            return item

    # JSON-LD extraction
    try:
        jsonld_blocks = extract_jsonld(html)
        if jsonld_blocks:
            _populate_from_jsonld(item, jsonld_blocks)
    except Exception as exc:
        logger.warning("Phase 2: JSON-LD extraction failed for %s: %s", item_url[:60], exc)

    # CSS fallback extraction
    if soup:
        _css_fallback_extract(soup, item, item_url)

    return item


def _extract_item_safe(item_url: str, src_url: str) -> dict:
    try:
        return _extract_item(item_url, src_url)
    except Exception as exc:
        logger.error("Phase 2: unexpected failure on %s: %s", item_url[:80], exc)
        return _error_item(item_url, src_url, str(exc))


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(description=f"{SITE_NAME} HTTP Navigation Scraper")
    parser.add_argument("--query", type=str, default=DEFAULT_QUERY, help="Search query")
    parser.add_argument("--category-url", type=str, default=None, help="Category URL to crawl")
    parser.add_argument("--listing-url", type=str, default=None, help="Listing page URL")
    parser.add_argument("--input", type=str, default=None, help="Path to input URLs JSON file")
    parser.add_argument("--urls", nargs="+", default=None, help="Product URLs as CLI arguments")
    parser.add_argument("--sample", action="store_true", help="Scrape first 5 items only")
    parser.add_argument("--limit", type=int, default=None, help="Max items to scrape")
    parser.add_argument(
        "--no-proxy", action="store_true",
        help="Disable proxy (proxy_tier forced to 'none')",
    )
    parser.add_argument(
        "--headless", action="store_true", default=True,
        help="Accepted for CLI compatibility",
    )
    parser.add_argument(
        "--fresh-discovery", action="store_true",
        help="Ignore checkpoint and run Phase 1 from scratch",
    )
    args = parser.parse_args()

    # --no-proxy overrides the configured PROXY_TIER for this run.
    global PROXY_TIER
    if args.no_proxy:
        PROXY_TIER = "none"

    limit = 5 if args.sample else args.limit
    start_time = time.time()
    discovered_urls: list[str] = []

    ran_phase1 = True
    skipped_reason: Optional[str] = None
    aggregate_stop_reason = "no_next_link"
    max_pages_hit = False

    # ── --input takes precedence over checkpoint ──────────────────────────
    if args.input:
        try:
            with open(args.input, "r", encoding="utf-8") as f:
                data = json.load(f)
            discovered_urls = data.get("urls", [])
            logger.info("Loaded %d URLs from input file: %s", len(discovered_urls), args.input)
            ran_phase1 = False
            skipped_reason = "input_file"
            aggregate_stop_reason = "skipped"
        except Exception as exc:
            logger.error("Failed to load input file %s: %s", args.input, exc)
            sys.exit(1)

    # ── --urls CLI argument ───────────────────────────────────────────────
    if not discovered_urls and args.urls:
        discovered_urls = list(args.urls)
        logger.info("Using %d URLs from CLI arguments", len(discovered_urls))
        ran_phase1 = False
        skipped_reason = "url_list"
        aggregate_stop_reason = "skipped"

    # ── Resume from checkpoint ────────────────────────────────────────────
    if not discovered_urls and not args.fresh_discovery:
        checkpoint_urls = _load_checkpoint()
        if checkpoint_urls:
            discovered_urls = checkpoint_urls
            ran_phase1 = False
            skipped_reason = "checkpoint_loaded"
            aggregate_stop_reason = "skipped"

    # ── Phase 1: Discover URLs ────────────────────────────────────────────
    if not discovered_urls:
        category_url = args.category_url or args.listing_url or DEFAULT_LISTING_URL
        logger.info("Phase 1: discovering via category %s", category_url[:80])
        discovered_urls, primary_reason = _discover_urls_via_category(category_url, MAX_PAGES, limit)
        aggregate_stop_reason = _merge_stop_reason(aggregate_stop_reason, primary_reason)
        max_pages_hit = max_pages_hit or primary_reason == "max_pages_hit"
        logger.info("Phase 1: discovered %d URLs", len(discovered_urls))
        _write_checkpoint(discovered_urls)

    src_url_base = args.category_url or args.listing_url or DEFAULT_LISTING_URL
    if args.input or args.urls:
        src_url_base = "(input)"

    if not discovered_urls:
        logger.error("No URLs to scrape")
        _write_output([], start_time, aggregate_stop_reason, ran_phase1, skipped_reason)
        sys.exit(1)

    # ── Phase 2: Scrape each item ─────────────────────────────────────────
    logger.info("Phase 2: scraping %d items with %d workers", len(discovered_urls), PHASE2_WORKERS)
    results: list[dict] = [None] * len(discovered_urls)

    with ThreadPoolExecutor(max_workers=PHASE2_WORKERS) as executor:
        future_to_idx = {}
        for idx, url in enumerate(discovered_urls):
            future = executor.submit(_extract_item_safe, url, src_url_base)
            future_to_idx[future] = idx

        completed = 0
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as exc:
                results[idx] = _error_item(discovered_urls[idx], src_url_base, str(exc))
            completed += 1
            if completed % 10 == 0 or completed == len(discovered_urls):
                logger.info(
                    "Phase 2: %d/%d items processed (%.0f%%)",
                    completed, len(discovered_urls), completed / len(discovered_urls) * 100,
                )

    # Replace any None results with error items
    results = [r if r else _error_item(discovered_urls[i], src_url_base, "unknown error") for i, r in enumerate(results)]

    # ── Output filter ─────────────────────────────────────────────────────
    before_count = len(results)
    filtered_results = [
        it for it in results
        if it.get("title") and (not CORE_FILTER_FIELDS or any(it.get(f) for f in CORE_FILTER_FIELDS))
    ]
    # Keep error items too (for reporting) but mark them
    error_items = [it for it in results if not it.get("title")]
    if len(filtered_results) + len(error_items) != before_count:
        logger.info(
            "Output filter: %d items with title + core fields, %d error items",
            len(filtered_results), len(error_items),
        )

    # Assign sequential IDs
    for idx, item in enumerate(filtered_results, start=1):
        item["id"] = idx

    # ── Write output ──────────────────────────────────────────────────────
    _write_output(filtered_results, start_time, aggregate_stop_reason, ran_phase1, skipped_reason)

    logger.info("=" * 80)
    logger.info(
        "EXTRACTION COMPLETE: %d products, %d errors, %.1fs",
        len(filtered_results), len(error_items), time.time() - start_time,
    )
    logger.info("=" * 80)


def _write_output(
    results: list[dict],
    start_time: float,
    stop_reason: str,
    ran_phase1: bool,
    skipped_reason: Optional[str],
) -> None:
    """Write the output JSON file."""
    output = {
        "site": {
            "name": SITE_NAME,
            "url": SITE_URL,
            "platform": PLATFORM,
            "scraping_method": SCRAPING_METHOD,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        },
        OUTPUT_KEY: results,
        "metadata": {
            "scraping_duration_seconds": round(time.time() - start_time, 2),
            "total_items": len(results),
            "discovery_coverage": {
                "ran_phase1": ran_phase1,
                "stop_reason": stop_reason,
                "skipped_reason": skipped_reason,
            },
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

    logger.info("Output written to %s", output_filename)


if __name__ == "__main__":
    main()
