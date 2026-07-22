#!/usr/bin/env python3
"""Myntra Scraper — Two-Phase Architecture (Pure HTTP)

Phase 1: Discover product URLs by fetching Myntra listing pages via httpx
         and parsing embedded window.__myx JSON data + HTML fallback.
         Also tries Myntra's internal /gateway/v2/search/data API.
Phase 2: Extract structured data from each PDP via direct httpx GET
         (Myntra product pages are server-rendered with JSON-LD).

Usage:
    python3 scraper_draft.py                          # full discovery+extraction (default: "watches")
    python3 scraper_draft.py --query "shirts"         # search for shirts
    python3 scraper_draft.py --sample --query "watches"  # quick test: 5 items via Phase 1
    python3 scraper_draft.py --sample                  # quick test: 5 items from input_urls.json
    python3 scraper_draft.py --limit 50                # max 50 items
    python3 scraper_draft.py --input custom_urls.json  # explicit product URL file
    python3 scraper_draft.py --urls URL1 URL2          # CLI product URLs
    python3 scraper_draft.py --discover-only           # Phase 1 only
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
from html import unescape
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

SITE_NAME = "Myntra"
SITE_URL = "https://www.myntra.com"
PLATFORM = "custom"
SCRAPING_METHOD = "http_requests"
SITE_SLUG = "myntra-com"
PROXY_TIER = "none"

# Phase 1: Search & Discovery.
DEFAULT_QUERY = "watches"
SEARCH_URL_PATTERN = "https://www.myntra.com/{query}"
WORKING_SEARCH_URL = "https://www.myntra.com/watches"

# Myntra internal search API (used as Phase 1 alternative when __myx parsing fails).
SEARCH_API_URL = "https://www.myntra.com/gateway/v2/search/data"
SEARCH_API_ROWS = 100  # max items per API page

# Pagination: Myntra uses ?p=N (page numbers, 1-indexed).
PAGINATION_TYPE = "page_param"
PAGE_PARAM_NAME = "p"
ITEMS_PER_PAGE = 50
MAX_PAGES = None  # unlimited — paginate until exhaustion

# Phase 2: Extraction.
DELAY_BETWEEN_REQUESTS = 1.5
PHASE2_WORKERS = 8
MAX_RETRIES = 3
BACKOFF_BASE = 2.0
HTTP_TIMEOUT = 20

CATEGORY_URLS: list[str] = []

# Output.
OUTPUT_KEY = "products"
CONTENT_TYPE = "product"
CURRENCY = "INR"
CURRENCY_SYMBOL = "\u20b9"
SRC_URL = SITE_URL

# File paths.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_CHECKPOINT_PATH = os.path.join(SCRIPT_DIR, "discovered_urls_checkpoint.json")
INPUT_FILE = os.path.join(SCRIPT_DIR, "input_urls.json")
LOG_FILE = os.path.join(os.path.dirname(SCRIPT_DIR), "logs", f"{SITE_SLUG}.log")

# Product URL regex for filtering.
_PRODUCT_URL_RE = re.compile(
    r"^https?://(?:www\.)?myntra\.com/[a-z]+/[a-z0-9\-]+/[a-z0-9\-]+/[A-Za-z0-9]+/buy(?:\?|$)"
)

# Soft-404 indicators.
_SOFT404_PATTERNS = re.compile(
    r"product\s+not\s+found|unavailable|discontinued|no\s+longer\s+available|page\s+not\s+found",
    re.IGNORECASE,
)

# Skip image URL patterns.
_SKIP_IMAGE_PATTERNS = re.compile(
    r"/brand\.assets/|/emoji/|/flags/|/icon/|/navigation/|sprite",
    re.IGNORECASE,
)

# Content-type output filter.
# For products, title alone is sufficient to keep the item.
# Products without JSON-LD or with missing offers may not have price/availability.
_CONTENT_FILTER_FIELDS = {
    "product": [],  # title is the only hard requirement; price/availability are optional
    "article": ["author", "publish_date"],
    "job_posting": ["company", "location"],
}
CORE_FILTER_FIELDS = _CONTENT_FILTER_FIELDS.get(CONTENT_TYPE, [])

# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════════════

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
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
# CHECKPOINT
# ═══════════════════════════════════════════════════════════════════════════════

def _write_checkpoint(urls: list[str]) -> None:
    """Save discovered URLs so a crash-retry can resume from here."""
    try:
        with open(_CHECKPOINT_PATH, "w", encoding="utf-8") as f:
            json.dump({"urls": list(urls), "count": len(urls), "ts": time.time()}, f)
        logger.debug("Checkpoint: saved %d URLs to %s", len(urls), _CHECKPOINT_PATH)
    except Exception as exc:
        logger.warning("Checkpoint: write failed: %s", exc)


def _load_checkpoint() -> list[str]:
    """Load discovered URLs from a previous run's checkpoint (if any)."""
    try:
        if os.path.isfile(_CHECKPOINT_PATH):
            with open(_CHECKPOINT_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            urls = data.get("urls", [])
            if urls:
                logger.info(
                    "Checkpoint: RESUMING with %d URLs from %s",
                    len(urls), _CHECKPOINT_PATH,
                )
                return urls
    except Exception as exc:
        logger.warning("Checkpoint: load failed: %s", exc)
    return []


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP PRIMITIVE
# ═══════════════════════════════════════════════════════════════════════════════

_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}


def _http_get(url: str, extra_headers: Optional[dict] = None) -> tuple[Optional[str], int, str]:
    """Fetch a URL via direct httpx GET with retry/backoff.

    Returns (html/text, status_code, final_url).
    """
    headers = {**_HTTP_HEADERS, **(extra_headers or {})}
    for attempt in range(MAX_RETRIES):
        try:
            with httpx.Client(
                timeout=HTTP_TIMEOUT,
                follow_redirects=True,
                headers=headers,
            ) as client:
                resp = client.get(url)
                status = resp.status_code
                final_url = str(resp.url)
                if status == 200:
                    return resp.text, status, final_url
                if status == 404:
                    return resp.text, status, final_url
                if status in (429, 502, 503, 500):
                    retry_after = 5
                    ra = resp.headers.get("retry-after") or resp.headers.get("Retry-After")
                    if ra:
                        try:
                            retry_after = int(ra)
                        except (TypeError, ValueError):
                            pass
                    logger.debug(
                        "http_get: %d on %s, backing off %ds (attempt %d/%d)",
                        status, url[:60], retry_after, attempt + 1, MAX_RETRIES,
                    )
                    time.sleep(retry_after)
                    continue
                logger.debug("http_get: HTTP %d on %s (terminal)", status, url[:60])
                return resp.text, status, final_url
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            logger.debug(
                "http_get: transient error on %s: %s (attempt %d/%d)",
                url[:60], exc, attempt + 1, MAX_RETRIES,
            )
        time.sleep(min(BACKOFF_BASE ** attempt, 15))
    logger.warning("http_get: exhausted %d retries on %s", MAX_RETRIES, url[:80])
    return None, 0, url


# ═══════════════════════════════════════════════════════════════════════════════
# URL HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _make_absolute(href: str) -> str:
    """Convert relative URL to absolute Myntra URL."""
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
    """Myntra-specific: /category/brand/product-name/SKU/buy pattern."""
    if not href:
        return False
    parsed = urlparse(href)
    if "myntra.com" not in (parsed.hostname or "").lower():
        return False
    path = parsed.path.strip("/")
    if not path or len(path) < 6:
        return False
    if "/buy" not in path:
        return False
    segs = path.split("/")
    if len(segs) >= 5 and segs[-1] == "buy":
        return True
    if len(segs) >= 4:
        return True
    return False


def _set_query_param(url: str, param: str, value) -> str:
    """Replace or add a query parameter."""
    p = urlparse(url)
    qs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k != param]
    qs.append((param, str(value)))
    return urlunparse(p._replace(query=urlencode(qs)))


def _build_product_url(product: dict) -> Optional[str]:
    """Build a Myntra product URL from embedded product data fields.

    Myntra product URL format: /category/brand/product-name/sku/buy
    Fields come from window.__myx.searchData.results.products[]
    """
    # The landingPageUrl is often the full product URL
    landing_url = product.get("landingPageUrl") or product.get("productUrl")
    if landing_url:
        return _make_absolute(landing_url)

    # Try different field name patterns
    pid = product.get("productId") or product.get("product_id") or product.get("id")
    if not pid:
        return None

    # Construct from fields
    category = product.get("category") or product.get("productType") or product.get("type") or ""
    brand = product.get("brand") or product.get("brandName") or ""
    name = product.get("productName") or product.get("name") or product.get("title") or ""

    if category and brand and name and pid:
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        brand_slug = re.sub(r"[^a-z0-9]+", "-", brand.lower()).strip("-")
        cat_slug = re.sub(r"[^a-z0-9]+", "-", category.lower()).strip("-")
        return f"{SITE_URL}/{cat_slug}/{brand_slug}/{slug}/{pid}/buy"

    return None


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 1: URL DISCOVERY
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_myx_data(html: str) -> Optional[dict]:
    """Parse window.__myx JSON data from Myntra listing page HTML.

    Myntra embeds full product listing data in a script tag:
    window.__myx = {"searchData": {"results": {"products": [...], "totalCount": N}}}
    Multiple assignments may exist; we find the one with searchData.
    """
    for match in re.finditer(r'window\.__myx\s*=\s*', html):
        start = match.end()
        remaining = html[start:]
        try:
            decoder = json.JSONDecoder()
            data, _ = decoder.raw_decode(remaining)
            if isinstance(data, dict) and "searchData" in data:
                return data
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def _extract_product_links_from_html(html: str) -> list[str]:
    """Extract product links from listing page HTML using BeautifulSoup.

    Looks for anchor tags with /buy in the href (Myntra product page pattern).
    """
    links: list[str] = []
    try:
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            if href and "/buy" in href:
                full_url = _make_absolute(href)
                if _is_product_url(full_url):
                    links.append(full_url)
    except Exception as exc:
        logger.warning("Phase 1: HTML link extraction failed: %s", exc)
    return list(dict.fromkeys(links))


def _discover_urls_via_search(
    query: str,
    max_pages: Optional[int] = None,
    limit: Optional[int] = None,
) -> tuple[list[str], str]:
    """Phase 1: Discover product URLs from Myntra listing pages.

    Strategy: fetch listing page HTML, parse embedded __myx JSON.
    If __myx parsing fails, fall back to HTML link extraction.
    Also attempts Myntra's internal search API as a second strategy.

    Returns (urls, stop_reason).
    """
    search_url = SEARCH_URL_PATTERN.replace("{query}", query)
    logger.info("Phase 1: Searching for '%s' -> %s", query, search_url)

    all_urls: list[str] = []
    all_seen: set[str] = set()
    stop_reason = "no_next_link"
    current_page = 0
    myx_found = False

    while True:
        # Construct paginated URL
        if current_page == 0:
            page_url = search_url
        else:
            page_url = _set_query_param(search_url, PAGE_PARAM_NAME, current_page)

        html, status, final_url = _http_get(page_url)
        if html is None:
            logger.warning("Phase 1: page %d fetch failed", current_page + 1)
            stop_reason = "navigate_error"
            break

        if status != 200:
            logger.warning("Phase 1: page %d returned HTTP %d", current_page + 1, status)
            stop_reason = "navigate_error"
            break

        page_new_count = 0

        # Strategy 1: Parse embedded __myx JSON data.
        myx_data = _extract_myx_data(html)
        if myx_data:
            myx_found = True
            results = myx_data.get("searchData", {}).get("results", {})
            products = results.get("products", [])
            total_count = results.get("totalCount", 0)
            has_next = results.get("hasNextPage", False)

            logger.info(
                "Phase 1: page %d -> __myx found, %d products (total=%s, hasNext=%s)",
                current_page + 1, len(products), total_count, has_next,
            )

            for product in products:
                url = _build_product_url(product)
                if url and url not in all_seen:
                    all_urls.append(url)
                    all_seen.add(url)
                    page_new_count += 1

            # Check termination conditions
            if max_pages and current_page >= max_pages:
                stop_reason = "max_pages_hit"
                break

            if limit and len(all_urls) >= limit:
                stop_reason = "max_pages_hit"
                break

            if not products or len(products) < (ITEMS_PER_PAGE // 2):
                stop_reason = "short_page"
                logger.info(
                    "Phase 1: page %d short (got %d items), stopping",
                    current_page + 1, len(products),
                )
                break

            if not has_next:
                stop_reason = "no_next_link"
                break

            if (
                isinstance(total_count, int)
                and total_count > 0
                and len(all_urls) >= total_count
            ):
                stop_reason = "no_next_link"
                break

        else:
            # Strategy 2: HTML link extraction fallback.
            logger.info(
                "Phase 1: page %d — no __myx data, trying HTML link extraction",
                current_page + 1,
            )
            page_urls = _extract_product_links_from_html(html)
            new_urls = [u for u in page_urls if u not in all_seen]
            all_urls.extend(new_urls)
            all_seen.update(new_urls)
            page_new_count = len(new_urls)

            logger.info(
                "Phase 1: page %d -> %d links from HTML (%d new, cumulative: %d)",
                current_page + 1, len(page_urls), page_new_count, len(all_urls),
            )

            if not page_new_count:
                stop_reason = "no_new_items"
                break

            if max_pages and current_page >= max_pages:
                stop_reason = "max_pages_hit"
                break

            if limit and len(all_urls) >= limit:
                stop_reason = "max_pages_hit"
                break

        logger.info(
            "Phase 1: page %d -> %d new URLs (cumulative: %d)",
            current_page + 1, page_new_count, len(all_urls),
        )

        current_page += 1
        time.sleep(DELAY_BETWEEN_REQUESTS)

    # If __myx parsing worked, great. If not and we got 0 URLs, try API.
    if not all_urls and not myx_found:
        logger.info("Phase 1: __myx parsing and HTML links both yielded 0 URLs")
        logger.info("Phase 1: Trying Myntra internal search API as fallback")
        api_urls = _discover_via_search_api(query, limit)
        if api_urls:
            all_urls = api_urls
            stop_reason = "no_next_link"
            logger.info("Phase 1: API fallback found %d URLs", len(api_urls))

    unique_urls = list(dict.fromkeys(all_urls))
    if limit:
        unique_urls = unique_urls[:limit]
    logger.info(
        "Phase 1: Discovered %d total item URLs (stop_reason=%s)",
        len(unique_urls), stop_reason,
    )
    return unique_urls, stop_reason


def _discover_via_search_api(
    query: str,
    limit: Optional[int] = None,
) -> list[str]:
    """Phase 1 fallback: Use Myntra's internal search API endpoint.

    POST to /gateway/v2/search/data with query parameters.
    Returns product URLs from the API response.
    """
    api_urls: list[str] = []
    all_seen: set[str] = set()
    page = 0

    api_headers = {
        **_HTTP_HEADERS,
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Myntra-Source": "web",
    }

    while True:
        offset = page * SEARCH_API_ROWS
        data = {
            "query": query,
            "rows": SEARCH_API_ROWS,
            "start": offset,
            "f": "",
            "searchType": "category",
        }

        try:
            with httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True, headers=api_headers) as client:
                resp = client.post(SEARCH_API_URL, data=data)
                if resp.status_code != 200:
                    logger.debug(
                        "Search API: HTTP %d (offset=%d)", resp.status_code, offset,
                    )
                    break
                try:
                    resp_data = resp.json()
                except Exception:
                    logger.debug("Search API: non-JSON response")
                    break
        except Exception as exc:
            logger.debug("Search API: error: %s", exc)
            break

        products = []
        if isinstance(resp_data, dict):
            # Try multiple response structures
            results = resp_data.get("results", {})
            if isinstance(results, dict):
                products = results.get("products", [])
            elif isinstance(results, list):
                products = results
            if not products:
                products = resp_data.get("products", [])

        new_count = 0
        for product in products:
            url = _build_product_url(product)
            if url and url not in all_seen:
                api_urls.append(url)
                all_seen.add(url)
                new_count += 1

        logger.info(
            "Search API: offset=%d -> %d products (%d new, cumulative: %d)",
            offset, len(products), new_count, len(api_urls),
        )

        if not products or new_count == 0:
            break

        if limit and len(api_urls) >= limit:
            break

        page += 1
        time.sleep(DELAY_BETWEEN_REQUESTS)

    return api_urls


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 2: ITEM EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════════

def _clean_html(html_str: str) -> str:
    """Strip HTML tags and normalize whitespace."""
    if not html_str:
        return ""
    text = re.sub(r"<[^>]+>", " ", html_str)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _format_price(price_val, currency: str = CURRENCY) -> str:
    """Format a numeric price with currency symbol."""
    if not price_val:
        return ""
    try:
        num = float(str(price_val).replace(",", "").strip())
        if num <= 0:
            return ""
        return f"{CURRENCY_SYMBOL}{num:,.0f}"
    except (ValueError, TypeError):
        return str(price_val)


def _extract_breadcrumbs(soup: BeautifulSoup) -> list[str]:
    """Extract breadcrumb names from BreadcrumbList JSON-LD."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            text = script.string or ""
            data = json.loads(text)
            blocks = [data] if isinstance(data, dict) else (data if isinstance(data, list) else [])
            for block in blocks:
                if isinstance(block, dict) and block.get("@type") == "BreadcrumbList":
                    crumbs = []
                    for item in block.get("itemListElement", []):
                        if isinstance(item, dict):
                            name = item.get("name", "")
                            if name:
                                crumbs.append(name)
                    return crumbs
        except (json.JSONDecodeError, TypeError):
            continue
    return []


def _extract_images_from_html(soup: BeautifulSoup) -> list[str]:
    """Extract product gallery image URLs from .image-grid-image inline styles."""
    images: list[str] = []
    for el in soup.select(".image-grid-image"):
        style = el.get("style", "") or ""
        matches = re.findall(r'url\(["\']?(https?://[^"\')\s]+)["\']?\)', style)
        for m in matches:
            if not _SKIP_IMAGE_PATTERNS.search(m):
                images.append(m)
    return images


def _check_soft_404(soup: BeautifulSoup, url: str, final_url: str, jsonld) -> Optional[str]:
    """Detect soft-404 product pages."""
    if jsonld is None:
        canonical = soup.select_one('link[rel="canonical"]')
        if canonical:
            canon_href = canonical.get("href", "")
            if "/buy" not in canon_href:
                return "Soft 404: no Product JSON-LD and canonical not a product page"
    title_text = ""
    h1_el = soup.select_one("h1")
    if h1_el:
        title_text = h1_el.get_text(strip=True)
    title_tag = soup.find("title")
    if title_tag:
        title_text = f"{title_text} {title_tag.get_text(strip=True)}"
    if _SOFT404_PATTERNS.search(title_text):
        return f"Soft 404: product not found (title: {title_text[:100]})"
    if final_url and url and final_url != url:
        parsed_req = urlparse(url)
        parsed_final = urlparse(final_url)
        if parsed_req.path != parsed_final.path and "/buy" not in parsed_final.path:
            return f"Soft 404: redirected to {final_url}"
    return None


def _extract_jsonld_from_html(html: str) -> list[dict]:
    """Extract all JSON-LD blocks from HTML."""
    blocks: list[dict] = []
    try:
        soup = BeautifulSoup(html, "html.parser")
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                text = script.string or ""
                data = json.loads(text)
                if isinstance(data, list):
                    blocks.extend(data)
                elif isinstance(data, dict):
                    blocks.append(data)
            except (json.JSONDecodeError, TypeError):
                continue
    except Exception:
        pass
    return blocks


def _error_item(url: str, src_url: str, error: str) -> dict:
    return {
        "url": url,
        "src_url": src_url,
        "status_code": 0,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": f"Error: {error[:200]}",
    }


def _extract_item(item_url: str, src_url: str) -> dict:
    """Phase 2: Extract structured data from a single product page via direct HTTP.

    Myntra product pages are fully server-rendered with complete JSON-LD.
    """
    html, status, final_url = _http_get(item_url)
    if html is None:
        return _error_item(item_url, src_url, "HTTP request failed after retries")
    if status == 404:
        return _error_item(item_url, src_url, "404 not found")
    if status >= 400:
        return _error_item(item_url, src_url, f"HTTP {status}")

    item: dict = {
        "url": item_url,
        "src_url": src_url,
        "status_code": status,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": "",
    }

    soup = BeautifulSoup(html, "html.parser")

    # ── JSON-LD extraction (primary) ────────────────────────────────────
    jsonld_product = None
    try:
        jsonld_blocks = _extract_jsonld_from_html(html)
        for block in jsonld_blocks:
            block_type = block.get("@type", "")
            if isinstance(block_type, list):
                block_type = block_type[0] if block_type else ""
            if block_type == "Product":
                jsonld_product = block
                break
    except Exception as exc:
        logger.warning("Phase 2: JSON-LD extraction failed for %s: %s", item_url[:60], exc)

    # Soft-404 check.
    soft404 = _check_soft_404(soup, item_url, final_url, jsonld_product)
    if soft404:
        item["remarks"] = soft404
        return item

    # ── Title ──────────────────────────────────────────────────────────
    if jsonld_product:
        item["title"] = jsonld_product.get("name", "")
    if not item.get("title"):
        el = soup.select_one("h1.pdp-product-title, .pdp-name")
        if el:
            item["title"] = el.get_text(strip=True)
        else:
            h1 = soup.select_one("h1")
            if h1:
                item["title"] = h1.get_text(strip=True)

    # ── Brand ────────────────────────────────────────────────────────────
    if jsonld_product:
        brand = jsonld_product.get("brand", {})
        if isinstance(brand, dict):
            item["brand"] = brand.get("name", "")
    if not item.get("brand"):
        brand_el = soup.select_one("h1.pdp-product-title")
        if brand_el:
            item["brand"] = brand_el.get_text(strip=True)

    # ── Price (from JSON-LD offers) ──────────────────────────────────────
    if jsonld_product:
        offers = jsonld_product.get("offers", {})
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
                item["price"] = _format_price(price, currency)
                item["currency"] = currency
                break
        # Availability.
        for offer in offers_list:
            if isinstance(offer, dict) and offer.get("availability"):
                avail = offer["availability"]
                if "OutOfStock" in avail or "SoldOut" in avail:
                    item["availability"] = "Out of Stock"
                else:
                    item["availability"] = "In Stock"
                break
        # Offer URL.
        for offer in offers_list:
            if isinstance(offer, dict) and offer.get("url"):
                item["url"] = _make_absolute(offer["url"])
                break
    if "currency" not in item:
        item["currency"] = CURRENCY

    # ── Price fallback (CSS) ───────────────────────────────────────────
    if not item.get("price"):
        el = soup.select_one(".pdp-price")
        if el:
            nums = re.findall(r"[\d,]+\.?\d*", el.get_text(strip=True))
            if nums:
                item["price"] = _format_price(nums[0])

    # ── Original price (MRP) — CSS only ──────────────────────────────────
    orig_el = soup.select_one(".pdp-mrp, .pdp-price-info span[class*='pdp-mrp']")
    if orig_el:
        orig_text = orig_el.get_text(strip=True)
        nums = re.findall(r"[\d,]+\.?\d*", orig_text)
        if nums:
            item["original_price"] = _format_price(nums[0])

    # ── Discount — CSS only ───────────────────────────────────────────
    disc_el = soup.select_one(".pdp-discount")
    if disc_el:
        item["discount"] = disc_el.get_text(strip=True)

    # ── Description — try multiple selectors ─────────────────────────────
    desc_el = (
        soup.select_one(".pdp-product-descriptionContent")
        or soup.select_one(".pdp-description")
        or soup.select_one("[data-auto-id='pdp-description']")
        or soup.select_one(".pdp-product-description .pdp-product-descriptionContent")
    )
    if desc_el:
        item["description"] = _clean_html(desc_el.decode_contents())
    elif jsonld_product and jsonld_product.get("description"):
        desc_text = jsonld_product["description"]
        if desc_text != item.get("title", ""):
            item["description"] = desc_text

    # ── Images (JSON-LD + CSS fallback) ──────────────────────────────────
    if jsonld_product:
        images = jsonld_product.get("image", [])
        if isinstance(images, str):
            images = [images]
        if images:
            item["image"] = images[0]
            item["images"] = images

    bg_images = _extract_images_from_html(soup)
    if bg_images:
        if not item.get("images") or len(bg_images) > len(item.get("images", [])):
            item["images"] = bg_images
            item["image"] = bg_images[0]

    # ── SKU / MPN (JSON-LD only) ────────────────────────────────────────
    if jsonld_product:
        item["sku"] = jsonld_product.get("sku", "")
        item["mpn"] = jsonld_product.get("mpn", "")

    # ── Rating (CSS) ────────────────────────────────────────────────────
    rating_el = soup.select_one(".index-overallRating")
    if rating_el:
        rating_text = rating_el.get_text(strip=True)
        parts = rating_text.split("|")
        if parts:
            item["rating"] = parts[0].strip()

    # ── Review count (CSS) ──────────────────────────────────────────────
    review_el = soup.select_one(".index-ratingsCount")
    if review_el:
        item["review_count"] = review_el.get_text(strip=True)

    # ── Category (from breadcrumbs JSON-LD) ──────────────────────────────
    crumbs = _extract_breadcrumbs(soup)
    if crumbs and len(crumbs) > 1:
        skip = {"home", "myntra", "shop"}
        for crumb in reversed(crumbs):
            if crumb.lower() not in skip:
                item["category"] = crumb
                break

    # ── URL fallback (canonical) ────────────────────────────────────────
    canonical = soup.select_one('link[rel="canonical"]')
    if canonical:
        canon_href = canonical.get("href", "")
        if canon_href and "/buy" in canon_href:
            item["url"] = canon_href

    # ── Color variants ───────────────────────────────────────────────────
    color_links = soup.select(".colors-image a")
    if color_links:
        variants = []
        for a in color_links:
            variant: dict = {}
            vhref = a.get("href", "")
            if vhref:
                variant["url"] = _make_absolute(vhref)
            img = a.find("img")
            if img:
                variant["image"] = img.get("src", "")
            if variant:
                variants.append(variant)
        if variants:
            item["color_variants"] = variants

    # ── Size variants ──────────────────────────────────────────────────
    size_btns = soup.select(".size-buttons-size-button")
    if size_btns:
        sizes = []
        for btn in size_btns:
            size_text = btn.get_text(strip=True)
            classes = btn.get("class", [])
            in_stock = not any("not-in-stock" in str(c).lower() for c in classes)
            sizes.append({"size": size_text, "in_stock": in_stock})
        if sizes:
            item["sizes"] = sizes

    # ── Specs ───────────────────────────────────────────────────────────
    spec_rows = soup.select(".index-row")
    if spec_rows:
        specs: dict = {}
        for row in spec_rows:
            key_el = row.select_one(".index-rowKey")
            val_el = row.select_one(".index-rowValue")
            if key_el and val_el:
                key = key_el.get_text(strip=True)
                val = val_el.get_text(strip=True)
                if key and val:
                    specs[key] = val
        if specs:
            item["specs"] = specs

    return item


def _extract_item_safe(item_url: str, src_url: str) -> dict:
    """Phase 2 wrapper — never raises."""
    try:
        return _extract_item(item_url, src_url)
    except Exception as exc:
        logger.error("Phase 2: unexpected failure on %s: %s", item_url[:80], exc)
        return _error_item(item_url, src_url, str(exc))


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description=f"{SITE_NAME} Scraper (Pure HTTP)")
    parser.add_argument("--query", type=str, default=None, help="Search query (default: watches)")
    parser.add_argument("--category-url", type=str, default=None, help="Category URL to crawl")
    parser.add_argument("--listing-url", type=str, default=None, help="Listing page URL")
    parser.add_argument("--sample", action="store_true", help="Scrape first 5 items only")
    parser.add_argument("--limit", type=int, default=None, help="Max items to scrape")
    parser.add_argument("--input", type=str, default=None, help="Path to input URLs JSON file")
    parser.add_argument("--urls", nargs="+", default=None, help="Product URLs as CLI arguments")
    parser.add_argument("--no-proxy", action="store_true", default=True, help="Disable proxy (default)")
    parser.add_argument("--discover-only", action="store_true", help="Phase 1 only")
    parser.add_argument("--fresh-discovery", action="store_true", help="Ignore checkpoint")
    args = parser.parse_args()

    limit = 5 if args.sample else args.limit
    start_time = time.time()
    discovered_urls: list[str] = []
    src_url_base = SITE_URL

    # Coverage-gate state.
    ran_phase1 = False
    skipped_reason: Optional[str] = None
    aggregate_stop_reason = "no_next_link"
    max_pages_hit = False
    dimensions_iterated = 0
    dimensions_total = len(CATEGORY_URLS) if isinstance(CATEGORY_URLS, list) else 0

    logger.info("=" * 80)
    logger.info("Starting scraper for %s", SITE_NAME)
    logger.info("Site: %s", SITE_URL)
    logger.info("Method: http_requests (httpx for both Phase 1 and Phase 2)")
    logger.info("=" * 80)

    # ══════════════════════════════════════════════════════════════════
    # URL SOURCE PRIORITY:
    #   1. --input flag (HIGHEST — always takes precedence)
    #   2. --urls flag
    #   3. --sample WITH --query -> run Phase 1 discovery with limit=5
    #   4. --sample WITHOUT --query -> use input_urls.json
    #   5. Default (no flags) -> run Phase 1 discovery
    # ══════════════════════════════════════════════════════════════════

    if args.input:
        # --input takes HIGHEST precedence — ignores checkpoint entirely.
        logger.info("Loading URLs from --input: %s", args.input)
        try:
            with open(args.input, "r", encoding="utf-8") as f:
                data = json.load(f)
            raw_urls = data.get("urls", [])
            discovered_urls = [u for u in raw_urls if _is_product_url(u)]
            non_product = len(raw_urls) - len(discovered_urls)
            if non_product:
                logger.info("Filtered %d non-product URLs (kept %d)", non_product, len(discovered_urls))
            ran_phase1 = False
            skipped_reason = "input_arg"
            logger.info("Loaded %d product URLs from --input file", len(discovered_urls))
        except Exception as exc:
            logger.error("Failed to load --input file: %s", exc)
            sys.exit(1)

    elif args.urls:
        discovered_urls = [u for u in args.urls if _is_product_url(u)]
        ran_phase1 = False
        skipped_reason = "url_list_mode"
        logger.info("Using %d product URLs from --urls", len(discovered_urls))

    elif args.sample and args.query:
        # KEY FIX: --sample WITH --query runs Phase 1 discovery with limit=5.
        # This allows testing Phase 1 in sample mode.
        # NOTE: --sample implies --fresh-discovery (don't reuse old checkpoints
        # in sample mode — always re-discover a small fresh set).
        logger.info(
            "Sample mode WITH --query '%s': running Phase 1 discovery (limit=%d)",
            args.query, limit,
        )
        query = args.query or DEFAULT_QUERY
        src_url_base = SEARCH_URL_PATTERN.replace("{query}", query)
        # In sample+query mode, always run fresh discovery (skip checkpoint).
        ran_phase1 = True
        discovered_urls, aggregate_stop_reason = _discover_urls_via_search(
            query, MAX_PAGES, limit
        )
        max_pages_hit = aggregate_stop_reason == "max_pages_hit"

    elif args.sample and os.path.isfile(INPUT_FILE):
        # --sample WITHOUT --query: use input_urls.json for quick Phase 2 test.
        logger.info("Sample mode: skipping discovery, using URLs from %s", INPUT_FILE)
        try:
            with open(INPUT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            raw_urls = data.get("urls", [])
            discovered_urls = [u for u in raw_urls if _is_product_url(u)]
            ran_phase1 = False
            skipped_reason = "sample_input"
            logger.info("Loaded %d product URLs from input_urls.json", len(discovered_urls))
        except Exception as exc:
            logger.warning("Failed to load input_urls.json: %s", exc)
            discovered_urls = []

    else:
        # Default (no --sample, no --input, no --urls): run Phase 1 discovery.
        query = args.query or DEFAULT_QUERY
        src_url_base = SEARCH_URL_PATTERN.replace("{query}", query)
        checkpoint_urls = [] if args.fresh_discovery else _load_checkpoint()
        if checkpoint_urls:
            discovered_urls = checkpoint_urls
            ran_phase1 = False
            skipped_reason = "checkpoint_loaded"
            logger.info("Loaded %d URLs from checkpoint", len(discovered_urls))
        else:
            ran_phase1 = True
            discovered_urls, aggregate_stop_reason = _discover_urls_via_search(
                query, MAX_PAGES, limit
            )
            max_pages_hit = aggregate_stop_reason == "max_pages_hit"
            _write_checkpoint(discovered_urls)

    if not discovered_urls and not args.discover_only:
        logger.warning("No item URLs discovered")

    # Apply limit.
    if limit and len(discovered_urls) > limit:
        discovered_urls = discovered_urls[:limit]

    logger.info(
        "Phase 1: %d URLs (stop_reason=%s, ran_phase1=%s)",
        len(discovered_urls),
        aggregate_stop_reason if ran_phase1 else "skipped",
        ran_phase1,
    )

    # ══════════════════════════════════════════════════════════════════
    # Phase 2: Extract data concurrently via HTTP
    # ══════════════════════════════════════════════════════════════════
    total = len(discovered_urls)
    items: list[dict] = []

    if args.discover_only:
        logger.info(
            "--discover-only: skipping Phase 2 (%d URLs discovered)", total,
        )
    elif discovered_urls:
        logger.info(
            "Phase 2: Extracting data from %d items (%d workers)", total, PHASE2_WORKERS,
        )
        completed = 0

        def _extract_with_idx(idx_url):
            idx, url = idx_url
            return idx, _extract_item_safe(url, src_url_base)

        with ThreadPoolExecutor(max_workers=PHASE2_WORKERS) as pool:
            futures = {
                pool.submit(_extract_with_idx, (i, url)): url
                for i, url in enumerate(discovered_urls)
            }
            results_map: dict[int, dict] = {}
            for future in as_completed(futures):
                try:
                    idx, result_item = future.result()
                    results_map[idx] = result_item
                    completed += 1
                    if completed % 25 == 0 or completed == total:
                        percent = (completed / total) * 100
                        logger.info(
                            "Progress: [%d/%d] (%.1f%%)", completed, total, percent,
                        )
                except Exception as exc:
                    url = futures[future]
                    results_map[len(results_map)] = _error_item(url, src_url_base, str(exc))
                    completed += 1

            items = [
                results_map[i] for i in range(len(discovered_urls)) if i in results_map
            ]

    # ── Output filter ───────────────────────────────────────────────────
    extra = [f for f in CORE_FILTER_FIELDS if f and f != "title"]
    before = len(items)
    items = [
        it for it in items
        if it.get("title") and (not extra or any(it.get(f) for f in extra))
    ]
    if len(items) != before:
        logger.info(
            "output filter: %d -> %d items (dropped %d without core fields)",
            before, len(items), before - len(items),
        )

    # ── Discovery coverage block ────────────────────────────────────────
    discovery_coverage = {
        "stop_reason": aggregate_stop_reason if ran_phase1 else "skipped",
        "found": len(items),
        "discovered_urls": len(discovered_urls),
        "expected_total": None,
        "dimensions_iterated": dimensions_iterated,
        "dimensions_total": dimensions_total,
        "max_pages_hit": max_pages_hit,
        "ran_phase1": ran_phase1,
        "skipped_reason": skipped_reason,
    }

    # ── Write output ────────────────────────────────────────────────────
    output = {
        "site": {
            "name": SITE_NAME,
            "url": SITE_URL,
            "platform": PLATFORM,
            "scraping_method": SCRAPING_METHOD,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        },
        OUTPUT_KEY: items,
        "metadata": {
            "scraping_duration_seconds": round(time.time() - start_time, 2),
            "failed_products": sum(1 for i in items if not i.get("title")),
            "rate_limit_delay": DELAY_BETWEEN_REQUESTS,
            "discovered_urls": len(discovered_urls),
            "execution_model": "pure_http",
            "discovery_coverage": discovery_coverage,
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
    mode = "DISCOVERY COMPLETE" if args.discover_only else "EXTRACTION COMPLETE"
    logger.info(mode)
    logger.info(
        "Total: %d, Success: %d, Failed: %d",
        total,
        len([i for i in items if i.get("title")]),
        sum(1 for i in items if not i.get("title")),
    )
    logger.info(
        "Discovery: stop_reason=%s, found=%d, discovered=%d, ran_phase1=%s",
        discovery_coverage["stop_reason"], len(items), len(discovered_urls), ran_phase1,
    )
    logger.info("Duration: %.1fs", time.time() - start_time)
    logger.info("Output: %s", output_filename)
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
