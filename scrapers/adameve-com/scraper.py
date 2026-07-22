#!/usr/bin/env python3
"""
Adam & Eve Navigation Scraper — Two-Phase Architecture

Phase 1: Navigate the site's lingerie category page to discover product URLs.
  - Fetches the listing page, parses product links from the HTML grid.
  - Follows pagination (?pgNum=N) to discover all products across pages.
Phase 2: Scrape each discovered product page via HTTP for structured data.
  - Extracts title, price, availability, brand, description, images, etc.
  - Uses JSON-LD (primary) + CSS/OG fallbacks.
  - Concurrent extraction with ThreadPoolExecutor.

Strategy: http_requests (server-rendered, no anti-bot, no proxy).
Usage:
    python3 scraper_draft.py                          # full discovery + extraction
    python3 scraper_draft.py --sample                 # first 5 products only
    python3 scraper_draft.py --limit 50               # max 50 products
    python3 scraper_draft.py --input input_urls.json  # use provided seed URLs
    python3 scraper_draft.py --urls "https://..."     # CLI product URLs (skip Phase 1)
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
from urllib.parse import urlparse, urljoin

import httpx
from bs4 import BeautifulSoup

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

SITE_NAME = "Adam & Eve"
SITE_URL = "https://www.adameve.com"
PLATFORM = "custom"
SITE_SLUG = "adameve-com"
SCRAPING_METHOD = "http_requests"
CURRENCY = "USD"
OUTPUT_KEY = "products"
CONTENT_TYPE = "product"

# Phase 1: Navigation / Discovery
DEFAULT_SEED_URL = "https://www.adameve.com/lingerie-ch-951.aspx"
PAGE_PARAM_NAME = "pgNum"  # pagination parameter observed on this ASP.NET site
MAX_PAGES = 50  # high ceiling; loop stops on no-new-items

# Phase 1: Product link selectors on listing pages
# Adam & Eve product cards link to .aspx product detail pages.
ITEM_LINK_SELECTOR = "a[href*='.aspx']"
ITEM_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?adameve\.com/[A-Za-z0-9\-]+/[A-Za-z0-9\-]+/[A-Za-z0-9\-]+/sp-[A-Za-z0-9\-]+-\d+\.aspx",
    re.IGNORECASE,
)

# Phase 2: Rate limiting & concurrency
DELAY_BETWEEN_REQUESTS = 1.0
MAX_WORKERS = 8

# Checkpoint (resume after crash)
_CHECKPOINT_PATH = os.path.join(SCRIPT_DIR, "discovered_urls_checkpoint.json")

# ── Content filter fields ───────────────────────────────────────────────
_CONTENT_FILTER_FIELDS = {
    "product": ["price", "availability"],
    "article": ["author", "publish_date"],
    "job_posting": ["company", "location"],
}
CORE_FILTER_FIELDS = _CONTENT_FILTER_FIELDS.get(CONTENT_TYPE, [])

# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════════════

LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, f"{SITE_SLUG}.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(SITE_SLUG)


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP CLIENT
# ═══════════════════════════════════════════════════════════════════════════════

def _build_headers() -> dict:
    """Return headers that mimic a normal browser request."""
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    }


def _fetch(url: str, client: httpx.Client) -> Optional[httpx.Response]:
    """Fetch a URL, returning None on failure. Logs warnings."""
    try:
        resp = client.get(url, headers=_build_headers(), timeout=20, follow_redirects=True)
        resp.raise_for_status()
        return resp
    except httpx.HTTPStatusError as e:
        logger.warning("HTTP error %s for %s: %s", e.response.status_code, url[:80], e)
        return e.response if e.response else None
    except httpx.RequestError as e:
        logger.warning("Request error for %s: %s", url[:80], e)
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# CHECKPOINT HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _write_checkpoint(urls: list[str]) -> None:
    try:
        with open(_CHECKPOINT_PATH, "w") as f:
            json.dump({"urls": list(urls), "count": len(urls), "ts": time.time()}, f)
        logger.debug("Checkpoint: saved %d URLs", len(urls))
    except Exception as exc:
        logger.warning("Checkpoint write failed: %s", exc)


def _load_checkpoint() -> list[str]:
    try:
        if os.path.isfile(_CHECKPOINT_PATH):
            with open(_CHECKPOINT_PATH, "r") as f:
                data = json.load(f)
            urls = data.get("urls", [])
            if urls:
                logger.info("Checkpoint: RESUMING with %d URLs", len(urls))
                return urls
    except Exception as exc:
        logger.warning("Checkpoint load failed: %s", exc)
    return []


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 1: URL DISCOVERY
# ═══════════════════════════════════════════════════════════════════════════════

def _is_product_url(href: str) -> bool:
    """Check if a URL looks like an Adam & Eve product detail page.

    Product URLs follow patterns like:
      /lingerie/womens-wear/fetish-wear/sp-ambrosia-collared-vest-and-skirt-set-109854.aspx
    Key characteristics:
      - Contains /sp- prefix in the last path segment
      - Ends with .aspx
      - Has a numeric product code before .aspx (typically 5-6 digits)
    Non-product pages to EXCLUDE:
      - Category pages: /...-c-XXXX.aspx (e.g., butt-plugs-c-1054.aspx)
      - Channel pages: /...-ch-XXX.aspx (e.g., lingerie-ch-951.aspx)
      - Article/guide pages: /sex-guides/... (no /sp- prefix)
      - Promo/info pages
    """
    if not href:
        return False
    # Must be same domain
    parsed = urlparse(href)
    if "adameve.com" not in (parsed.hostname or "").lower():
        return False
    path = parsed.path.lower()
    if not path.endswith(".aspx"):
        return False
    # Exclude category/channel pages (ch-XXXX.aspx, -c-XXXXX.aspx)
    if re.search(r"/ch-\d+\.aspx$", path, re.IGNORECASE):
        return False
    if re.search(r"-c-\d+\.aspx$", path, re.IGNORECASE):
        return False
    # Exclude sex-guides articles (they don't have /sp- prefix)
    if "/sex-guides/" in path:
        return False
    # Product pages MUST have /sp- in the URL path (this is the definitive marker)
    # e.g. /lingerie/womens-wear/fetish-wear/sp-ambrosia-...-109854.aspx
    last_seg = path.rstrip("/").split("/")[-1]
    if last_seg.startswith("sp-"):
        return True
    # Some product URLs may not have /sp- but are clearly products
    # They have a long slug with a numeric product ID at the end
    segs = [s for s in parsed.path.strip("/").split("/") if s]
    if len(segs) >= 4:
        # Must NOT be a category (-c-) or channel (-ch-) page
        last = segs[-1].replace(".aspx", "")
        if "-c-" not in last and "-ch-" not in last:
            # Has a product-code-style number at the end (4+ digits)
            if re.search(r"-\d{4,6}\.aspx$", last, re.IGNORECASE):
                return True
    return False


def _extract_product_links_from_html(html: str, base_url: str) -> list[str]:
    """Parse listing page HTML and extract product detail page URLs."""
    soup = BeautifulSoup(html, "html.parser")
    links: list[str] = []
    seen: set[str] = set()

    # Primary: look for all <a> tags and filter by product URL pattern
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].strip()
        if not href:
            continue
        # Make absolute
        if href.startswith("/"):
            href = SITE_URL.rstrip("/") + href
        elif not href.startswith("http"):
            href = urljoin(base_url, href)
        if href in seen:
            continue
        if _is_product_url(href):
            links.append(href)
            seen.add(href)

    return links


def _get_next_page_url(base_url: str, current_page: int) -> Optional[str]:
    """Construct the next page URL using pgNum parameter."""
    parsed = urlparse(base_url)
    # Build new query string
    params = []
    if parsed.query:
        for part in parsed.query.split("&"):
            if part.startswith("pgNum="):
                params.append(f"pgNum={current_page + 1}")
            else:
                params.append(part)
    else:
        params.append(f"pgNum={current_page + 1}")
    new_query = "&".join(params)
    return parsed._replace(query=new_query).geturl()


def discover_urls(
    seed_url: str,
    max_pages: Optional[int] = None,
    limit: Optional[int] = None,
) -> tuple[list[str], str]:
    """Phase 1: Discover product URLs from listing pages.

    Returns (urls, stop_reason).
    """
    stop_reason = "no_next_link"
    all_urls: list[str] = []
    seen: set[str] = set()

    with httpx.Client() as client:
        # Fetch first page
        current_url = seed_url
        page_num = 0

        while True:
            if max_pages and page_num >= max_pages:
                stop_reason = "max_pages_hit"
                break
            if limit and len(all_urls) >= limit:
                stop_reason = "max_pages_hit"
                break

            logger.info("Phase 1: Fetching page %d → %s", page_num + 1, current_url[:80])
            resp = _fetch(current_url, client)
            if resp is None:
                stop_reason = "navigate_error"
                break
            if resp.status_code == 404:
                logger.info("Phase 1: 404 at page %d, stopping", page_num + 1)
                stop_reason = "no_new_items"
                break

            html = resp.text
            page_links = _extract_product_links_from_html(html, current_url)
            new_links = [u for u in page_links if u not in seen]
            new_count = len(new_links)

            logger.info(
                "Phase 1: Page %d → %d links (%d new)", page_num + 1, len(page_links), new_count
            )

            for link in new_links:
                if link not in seen:
                    all_urls.append(link)
                    seen.add(link)

            if new_count == 0 and page_num > 0:
                stop_reason = "no_new_items"
                break

            # Check if the HTML contains a "load more" or pagination hint
            soup = BeautifulSoup(html, "html.parser")
            has_more = _has_more_pages(soup, html)
            if not has_more and page_num > 0:
                stop_reason = "no_next_link"
                break

            # Try next page
            page_num += 1
            next_url = _get_next_page_url(seed_url, page_num)
            if not next_url:
                stop_reason = "no_next_link"
                break

            current_url = next_url
            time.sleep(DELAY_BETWEEN_REQUESTS)

    unique_urls = list(dict.fromkeys(all_urls))
    if limit:
        unique_urls = unique_urls[:limit]
    logger.info("Phase 1: Discovered %d unique product URLs (stop_reason=%s)", len(unique_urls), stop_reason)
    return unique_urls, stop_reason


def _has_more_pages(soup: BeautifulSoup, html: str) -> bool:
    """Detect if there are more pages to scrape.

    Adam & Eve uses pgNum-based pagination. Check for:
    - Next page links
    - Pagination controls
    - "Load more" buttons
    """
    # Check for next/last page links in pagination
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].lower()
        if "pgnum" in href or "page" in href:
            text = a_tag.get_text(strip=True).lower()
            if text in ("next", "›", ">", "next page"):
                return True

    # Check for "load more" buttons
    for el in soup.find_all(["button", "a", "div"]):
        text = el.get_text(strip=True).lower()
        if "load more" in text or "view all" in text or "show more" in text:
            return True

    # Check if pagination exists at all (if pgNum is in any link, pagination exists)
    for a_tag in soup.find_all("a", href=True):
        if "pgnum" in a_tag["href"].lower():
            return True

    return False


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 2: PRODUCT EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_jsonld_product(html: str) -> Optional[dict]:
    """Extract the Product JSON-LD block from page HTML.

    Adam & Eve's JSON-LD has PascalCase property names and may have a trailing
    character after the closing brace. Uses brace-depth matching for robustness.
    """
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        text = script.string
        if not text:
            continue
        # Extract the JSON object using brace-depth matching
        json_str = _extract_json_by_braces(text)
        if not json_str:
            continue
        try:
            data = json.loads(json_str)
            # Check @type - could be "Product" or nested
            atype = data.get("@type", "")
            if atype == "Product":
                return data
            # Sometimes @type is a list
            if isinstance(atype, list) and "Product" in atype:
                return data
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def _extract_json_by_braces(text: str) -> Optional[str]:
    """Extract the outermost JSON object using brace-depth matching.

    Handles trailing characters after the closing brace.
    """
    depth = 0
    start = -1
    in_string = False
    escape_next = False

    for i, ch in enumerate(text):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                return text[start : i + 1]
    return None


def _get_jsonld_value(data: dict, *keys: str) -> any:
    """Access a nested JSON-LD value, case-insensitive for the last key.

    Handles PascalCase (Name, Mpn, Image, Brand, etc.) used by Adam & Eve.
    """
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        # Try exact key first
        if key in current:
            current = current[key]
            continue
        # Case-insensitive fallback
        found = False
        for k, v in current.items():
            if k.lower() == key.lower():
                current = v
                found = True
                break
        if not found:
            return None
    return current


def _normalize_availability(avail: str) -> str:
    """Normalize schema.org availability URL or plain text to standard values."""
    if not avail:
        return ""
    avail_lower = avail.lower()
    if "instock" in avail_lower or "in stock" in avail_lower:
        return "In Stock"
    if "outofstock" in avail_lower or "out of stock" in avail_lower or "soldout" in avail_lower:
        return "Out of Stock"
    if "preorder" in avail_lower:
        return "Pre-order"
    if "limited" in avail_lower:
        return "Limited Availability"
    return avail.strip()


def _format_price(price_val, currency: str = "USD") -> str:
    """Format a price value into string with currency symbol."""
    if price_val is None:
        return ""
    currency_symbols = {"USD": "$", "EUR": "€", "GBP": "£", "CAD": "C$", "AUD": "A$"}
    symbol = currency_symbols.get(currency, f"{currency} ")
    if isinstance(price_val, (int, float)):
        return f"{symbol}{price_val:,.2f}"
    # String price
    price_str = str(price_val).strip()
    if not price_str:
        return ""
    # If already has currency symbol
    if price_str[0] in "$€£":
        return price_str
    try:
        return f"{symbol}{float(price_str):,.2f}"
    except ValueError:
        return f"{symbol}{price_str}"


def _detect_soft_404(url: str, final_url: str, soup: BeautifulSoup, jsonld: Optional[dict]) -> Optional[str]:
    """Detect soft 404s on product pages.

    Returns a remarks string if soft 404 detected, None otherwise.
    """
    # Check if final URL redirected to a different page
    if final_url and url and final_url.rstrip("/") != url.rstrip("/"):
        # Minor redirects (http→https, www→non-www) are OK
        p1 = urlparse(url)
        p2 = urlparse(final_url)
        if p1.path.lower().rstrip("/") != p2.path.lower().rstrip("/"):
            # Check if redirected to a category/search page
            if _is_category_or_search(p2.path):
                return "Soft 404: redirected to non-product page"

    # Check JSON-LD type
    if jsonld is None:
        # No Product JSON-LD found — check if this is actually a product page
        title = soup.title.string if soup.title else ""
        h1 = soup.find("h1")
        h1_text = h1.get_text(strip=True) if h1 else ""
        for text in [title, h1_text]:
            lower = text.lower()
            if any(phrase in lower for phrase in [
                "page not found", "product not found", "item not available",
                "no longer available", "discontinued", "not found",
                "error 404", "oops",
            ]):
                return f"Soft 404: {text.strip()}"

    return None


def _is_category_or_search(path: str) -> bool:
    """Check if a path looks like a category/search page, not a product page."""
    lower = path.lower()
    if "ch-" in lower and lower.endswith(".aspx"):
        return True
    if "/search" in lower:
        return True
    if lower.endswith("/") or lower in ("", "/"):
        return True
    return False


def extract_product(url: str, src_url: str, index: int) -> dict:
    """Phase 2: Extract product data from a single product page URL.

    Uses a thread-local httpx client. Returns a product dict.
    """
    import httpx as _httpx

    # Thread-local client
    thread_client = _httpx.Client(
        headers=_build_headers(),
        timeout=20,
        follow_redirects=True,
    )

    try:
        resp = thread_client.get(url)
        final_url = str(resp.url)
        status_code = resp.status_code
        html = resp.text
    except Exception as exc:
        logger.warning("Phase 2: Failed to fetch %s: %s", url[:80], exc)
        return {
            "id": index,
            "url": url,
            "src_url": src_url,
            "status_code": 0,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "remarks": f"Error: {str(exc)[:200]}",
            "title": "",
            "price": "",
            "availability": "",
            "original_price": "",
            "currency": "",
            "location": "",
        }
    finally:
        thread_client.close()

    soup = BeautifulSoup(html, "html.parser")

    # Parse JSON-LD
    jsonld = _extract_jsonld_product(html)

    # Soft 404 check
    soft_404 = _detect_soft_404(url, final_url, soup, jsonld)
    if soft_404:
        return {
            "id": index,
            "url": url,
            "src_url": src_url,
            "status_code": status_code,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "remarks": soft_404,
            "title": "",
            "price": "",
            "availability": "",
            "original_price": "",
            "currency": "",
            "location": "",
        }

    # ── Extract fields ─────────────────────────────────────────────────
    item: dict = {
        "id": index,
        "url": final_url,
        "src_url": src_url,
        "status_code": status_code,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": "",
        "location": "",
    }

    # --- Title ---
    item["title"] = ""
    if jsonld:
        item["title"] = str(_get_jsonld_value(jsonld, "Name") or "")
    if not item["title"]:
        h1 = soup.find("h1", class_="item_title") or soup.find("h1")
        if h1:
            item["title"] = h1.get_text(strip=True)
    if not item["title"]:
        og_title = soup.find("meta", property="og:title")
        if og_title:
            t = og_title.get("content", "")
            # Strip suffix like " - Women's Lingerie | Adam & Eve"
            item["title"] = re.sub(r"\s*[-|]\s*(Women's\s+)?\w+'s\s+\w+\s*\|\s*Adam\s*&\s*Eve\s*$", "", t).strip()

    # --- Price & Currency ---
    item["price"] = ""
    item["currency"] = CURRENCY
    item["original_price"] = ""

    if jsonld:
        offers = _get_jsonld_value(jsonld, "Offers")
        if isinstance(offers, dict):
            price_val = _get_jsonld_value(offers, "Price")
            curr = _get_jsonld_value(offers, "PriceCurrency") or CURRENCY
            item["currency"] = str(curr)
            if price_val is not None:
                item["price"] = _format_price(price_val, curr)
        elif isinstance(offers, list):
            for offer in offers:
                if isinstance(offer, dict):
                    price_val = _get_jsonld_value(offer, "Price")
                    curr = _get_jsonld_value(offer, "PriceCurrency") or CURRENCY
                    item["currency"] = str(curr)
                    if price_val is not None:
                        item["price"] = _format_price(price_val, curr)
                    break

    # Fallback: hidden input #product-price
    if not item["price"]:
        hidden_price = soup.find("input", id="product-price")
        if hidden_price:
            val = hidden_price.get("value", "").strip()
            if val:
                try:
                    item["price"] = _format_price(float(val), item["currency"])
                except ValueError:
                    item["price"] = f"${val}"

    # Fallback: og:price:amount
    if not item["price"]:
        og_price = soup.find("meta", property="og:price:amount")
        if og_price:
            val = og_price.get("content", "").strip()
            if val:
                item["price"] = _format_price(val, item["currency"])

    # --- Original Price (sale price) ---
    # Only populate when on sale: .ae-sale-price is visible (no 'hide' class)
    # and .ae-normal-price has 'hide' class.
    # The .ae-price--was element shows the original price only during sales.
    # Check: if .ae-sale-price exists and does NOT have 'hide' class → on sale
    sale_section = soup.find(class_="ae-sale-price")
    if sale_section:
        sale_classes = sale_section.get("class", [])
        if "hide" not in sale_classes:
            # Product IS on sale → extract original price
            was_el = soup.find(class_="ae-price--was")
            if was_el:
                was_text = was_el.get_text(strip=True)
                if was_text:
                    item["original_price"] = was_text

    # --- Availability ---
    item["availability"] = ""
    if jsonld:
        offers = _get_jsonld_value(jsonld, "Offers")
        avail = None
        if isinstance(offers, dict):
            avail = _get_jsonld_value(offers, "Availability")
        elif isinstance(offers, list):
            for offer in offers:
                if isinstance(offer, dict):
                    avail = _get_jsonld_value(offer, "Availability")
                    break
        if avail:
            item["availability"] = _normalize_availability(str(avail))

    if not item["availability"]:
        og_avail = soup.find("meta", property="og:availability")
        if og_avail:
            item["availability"] = _normalize_availability(og_avail.get("content", ""))

    # Fallback: data-viewitem JSON on wishlist button
    if not item["availability"]:
        wishlist_btn = soup.find(attrs={"data-wishlisticon": True})
        if wishlist_btn:
            viewitem_str = wishlist_btn.get("data-viewitem", "")
            if viewitem_str:
                try:
                    viewitem = json.loads(viewitem_str)
                    stock = viewitem.get("stock_status", "")
                    if stock:
                        item["availability"] = _normalize_availability(stock)
                except (json.JSONDecodeError, TypeError):
                    pass

    # --- Brand ---
    item["brand"] = ""
    if jsonld:
        brand = _get_jsonld_value(jsonld, "Brand", "Name")
        if brand:
            item["brand"] = str(brand)
    if not item["brand"]:
        og_brand = soup.find("meta", property="og:brand")
        if og_brand:
            item["brand"] = og_brand.get("content", "")

    # --- SKU ---
    item["sku"] = ""
    if jsonld:
        mpn = _get_jsonld_value(jsonld, "Mpn")
        if mpn:
            item["sku"] = str(mpn)
    if not item["sku"]:
        wishlist_btn = soup.find(attrs={"data-wishlisticon": True})
        if wishlist_btn:
            item["sku"] = wishlist_btn.get("data-sku", "")

    # --- Description ---
    item["description"] = ""
    if jsonld:
        desc = _get_jsonld_value(jsonld, "Description")
        if desc:
            item["description"] = str(desc).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not item["description"]:
        desc_el = soup.find(class_=re.compile(r"description", re.IGNORECASE))
        if desc_el:
            item["description"] = desc_el.get_text(strip=True, separator=" ")[:2000]
    if not item["description"]:
        og_desc = soup.find("meta", property="og:description")
        if og_desc:
            item["description"] = og_desc.get("content", "")

    # --- Category ---
    item["category"] = ""
    breadcrumbs = soup.select(".breadcrumb.ae-breadcrumbs__list li, .breadcrumb li")
    if len(breadcrumbs) > 2:
        # Skip first (Home) and last (product name)
        crumb_texts = [li.get_text(strip=True) for li in breadcrumbs[1:-1]]
        if crumb_texts:
            item["category"] = " > ".join(crumb_texts)

    # --- Rating ---
    item["rating"] = ""
    if jsonld:
        agg = _get_jsonld_value(jsonld, "AggregateRating")
        if isinstance(agg, dict):
            rv = _get_jsonld_value(agg, "RatingValue")
            if rv is not None:
                item["rating"] = str(rv)

    # --- Review Count ---
    item["review_count"] = ""
    if jsonld:
        agg = _get_jsonld_value(jsonld, "AggregateRating")
        if isinstance(agg, dict):
            rc = _get_jsonld_value(agg, "RatingCount")
            if rc is not None:
                item["review_count"] = str(rc)

    # --- Image (primary) ---
    item["image"] = ""
    og_img = soup.find("meta", property="og:image")
    if og_img:
        item["image"] = og_img.get("content", "")
    if not item["image"] and jsonld:
        img = _get_jsonld_value(jsonld, "Image")
        if img:
            if isinstance(img, list):
                item["image"] = str(img[0]) if img else ""
            else:
                item["image"] = str(img)
            # Upgrade thumbnail to full-size
            item["image"] = re.sub(r"-\d+x\d+\.", "-0x0.", item["image"])

    # --- Images (gallery) ---
    item["images"] = []
    glider_slides = soup.select(".glider-slide img")
    if glider_slides:
        for img_tag in glider_slides:
            parent = img_tag.find_parent()
            # Exclude recommendation/also-bought sections
            if parent and any(
                c in (parent.get("class") or []) or c in str(parent.get("id") or "")
                for c in ["recommend", "also", "recent", "similar"]
            ):
                continue
            src = img_tag.get("src") or img_tag.get("data-src", "")
            if src:
                # Upgrade to full-size
                src = re.sub(r"-\d+x\d+\.", "-0x0.", src)
                if src not in item["images"]:
                    item["images"].append(src)
    # Limit images
    item["images"] = item["images"][:5]

    # --- Variants ---
    item["variants"] = []
    variant_select = soup.find("select", class_="product-variant-select")
    if variant_select:
        for opt in variant_select.find_all("option"):
            val = opt.get("value", "").strip()
            if not val:
                continue
            label = opt.get_text(strip=True)
            price_match = re.search(r"\$([0-9.]+)", label)
            variant_info = {"variant_id": val, "label": label}
            if price_match:
                variant_info["price"] = price_match.group(1)
            item["variants"].append(variant_info)

    return item


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description=f"{SITE_NAME} Navigation Scraper")
    parser.add_argument("--input", type=str, help="Path to input URLs JSON file")
    parser.add_argument("--urls", nargs="+", help="Product URLs as CLI arguments (skip Phase 1)")
    parser.add_argument("--query", type=str, help="Search query (constructs search URL)")
    parser.add_argument("--category-url", type=str, help="Category URL to start discovery from")
    parser.add_argument("--sample", action="store_true", help="Scrape first 5 products only")
    parser.add_argument("--limit", type=int, default=None, help="Max products to scrape")
    parser.add_argument("--no-proxy", action="store_true", default=True, help="Disable proxy (default)")
    parser.add_argument("--discover-only", action="store_true", help="Run Phase 1 only")
    parser.add_argument("--fresh-discovery", action="store_true", help="Ignore checkpoint")
    args = parser.parse_args()

    limit = 5 if args.sample else args.limit
    start_time = time.time()
    discovered_urls: list[str] = []
    stop_reason = "no_next_link"
    ran_phase1 = False
    skipped_reason: Optional[str] = None

    logger.info("=" * 80)
    logger.info(f"Starting scraper for {SITE_NAME}")
    logger.info("=" * 80)

    # ── Determine URL source ───────────────────────────────────────────
    # --urls takes highest precedence (explicit product URLs, skip Phase 1)
    if args.urls:
        discovered_urls = list(args.urls)
        skipped_reason = "cli_urls"
        logger.info("Using %d URLs from --urls CLI arg", len(discovered_urls))

    # --input takes next precedence (must override checkpoint)
    elif args.input:
        try:
            with open(args.input, "r") as f:
                data = json.load(f)
            input_urls = data.get("urls", [])
            # Check if these are product URLs or seed/category URLs
            product_urls = [u for u in input_urls if _is_product_url(u)]
            if product_urls:
                discovered_urls = product_urls
                skipped_reason = "input_product_urls"
                logger.info("Using %d product URLs from --input", len(discovered_urls))
            else:
                # Seed/category URLs → run Phase 1 discovery
                seed = input_urls[0] if input_urls else DEFAULT_SEED_URL
                ran_phase1 = True
                logger.info("Using seed URL from --input: %s", seed)
                discovered_urls, stop_reason = discover_urls(seed, MAX_PAGES, limit)
                _write_checkpoint(discovered_urls)
        except Exception as exc:
            logger.error("Failed to read --input %s: %s", args.input, exc)
            sys.exit(1)

    # Default: Phase 1 discovery from checkpoint or default seed
    else:
        checkpoint_urls = [] if args.fresh_discovery else _load_checkpoint()
        if checkpoint_urls:
            discovered_urls = checkpoint_urls
            skipped_reason = "checkpoint_loaded"
            logger.info("Resuming from checkpoint: %d URLs", len(discovered_urls))
        else:
            ran_phase1 = True
            # Determine seed URL
            seed_url = args.category_url or DEFAULT_SEED_URL
            logger.info("Phase 1: Discovering from seed: %s", seed_url)
            discovered_urls, stop_reason = discover_urls(seed_url, MAX_PAGES, limit)
            _write_checkpoint(discovered_urls)

    if not ran_phase1:
        stop_reason = "skipped"

    total = len(discovered_urls)
    logger.info("Total product URLs: %d", total)

    if not discovered_urls:
        logger.warning("No product URLs discovered (stop_reason=%s)", stop_reason)
    elif args.discover_only:
        logger.info("--discover-only: skipping Phase 2 extraction")

    # ── Phase 2: Extract data ───────────────────────────────────────────
    items: list[dict] = []

    if discovered_urls and not args.discover_only:
        logger.info("=" * 80)
        logger.info(f"Phase 2: Extracting data from {total} products")
        logger.info("=" * 80)

        # Use ThreadPoolExecutor for concurrent HTTP extraction
        src_url_base = discovered_urls[0]  # approximate src_url
        results = [None] * total

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {}
            for i, url in enumerate(discovered_urls):
                fut = executor.submit(extract_product, url, src_url_base, i)
                futures[fut] = i

            completed = 0
            for fut in as_completed(futures):
                idx = futures[fut]
                try:
                    result = fut.result()
                    results[idx] = result
                except Exception as exc:
                    logger.error("Exception extracting URL index %d: %s", idx, exc)
                    results[idx] = {
                        "id": idx,
                        "url": discovered_urls[idx],
                        "src_url": src_url_base,
                        "status_code": 0,
                        "scraped_at": datetime.now(timezone.utc).isoformat(),
                        "remarks": f"Error: {str(exc)[:200]}",
                        "title": "",
                        "price": "",
                        "availability": "",
                        "original_price": "",
                        "currency": "",
                        "location": "",
                    }
                completed += 1
                if completed % 25 == 0 or completed == total:
                    logger.info(
                        "Progress: [%d/%d] (%.1f%%)",
                        completed, total, (completed / total) * 100,
                    )

        # Re-index IDs sequentially
        items = []
        for i, result in enumerate(results):
            if result is not None:
                result["id"] = i + 1
                items.append(result)

    # ── Output filter ───────────────────────────────────────────────────
    _extra = [f for f in CORE_FILTER_FIELDS if f and f != "title"]
    _before = len(items)
    items = [
        it for it in items
        if it.get("title") and (not _extra or any(it.get(f) for f in _extra))
    ]
    if len(items) != _before:
        logger.info(
            "Output filter: %d → %d items (dropped %d without core fields)",
            _before, len(items), _before - len(items),
        )

    # ── Write output ────────────────────────────────────────────────────
    discovery_coverage = {
        "stop_reason": stop_reason,
        "found": len(items),
        "discovered_urls": len(discovered_urls),
        "ran_phase1": ran_phase1,
        "skipped_reason": skipped_reason,
    }

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
            "scraping_duration_seconds": round(time.time() - start_time, 1),
            "discovered_urls": len(discovered_urls),
            "extracted_items": len(items),
            "failed_products": sum(1 for it in items if it.get("remarks") and "Error" in it.get("remarks", "")),
            "rate_limit_delay": DELAY_BETWEEN_REQUESTS,
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
    logger.info(f"EXTRACTION COMPLETE")
    success = len([i for i in items if i.get("title")])
    failed = len(discovered_urls) - success
    logger.info(f"Total: {len(items)}, Success: {success}, Failed: {failed}")
    logger.info(f"Output: {output_filename}")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
