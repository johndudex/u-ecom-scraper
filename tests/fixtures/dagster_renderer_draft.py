#!/usr/bin/env python3
"""
HTTP Requests Scraper Template (synthetic fixture).

Shape-matched to the ``requests_scraper`` template family that
``webapp/agents/dagster_renderer.py`` recognises: module-level
``clean_html`` / ``make_absolute_url`` / ``fetch_page`` / ``extract_jsonld`` /
``extract_product_from_page`` / ``discover_product_urls``, plus the usual
configuration constants.  Used by ``tests/test_dagster_renderer.py`` as a
hermetic stand-in for a real generated draft (real drafts live under the File
Master and are not guaranteed to be present when the suite runs).
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from html import unescape
from typing import Optional
from urllib.parse import urljoin

import requests
from src.discovery import discover_item_urls, config_for_load_more  # enforced import
from bs4 import BeautifulSoup

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION - Update these values
# ═══════════════════════════════════════════════════════════════════════════════

SITE_NAME = "Fixture Widgets"
SITE_URL = "https://fixtures.example.com"
PLATFORM = "custom"
SCRAPING_METHOD = "http_requests"
SITE_SLUG = "fixtures-example-com"

PRODUCT_LISTING_URL = "https://fixtures.example.com/shop"
SRC_URL = "https://fixtures.example.com/shop"
PAGE_PARAM_NAME = "page"
if PAGE_PARAM_NAME.startswith("{") and PAGE_PARAM_NAME.endswith("}"):
    PAGE_PARAM_NAME = "page"
PRODUCT_LISTING_URLS = [PRODUCT_LISTING_URL]
DELAY_BETWEEN_REQUESTS = 1
MAX_RETRIES = 3
MAX_PAGES = None
DISCOVERY_DEADLINE_SECONDS = 300

HEADERS = {
    "User-Agent": "fixture-agent/1.0 (compatible)",
    "Accept": "text/html,*/*;q=0.8",
}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TIMESTAMP = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
OUTPUT_FILE = os.path.join(SCRIPT_DIR, f"output_{TIMESTAMP}.json")
INPUT_FILE = os.path.join(SCRIPT_DIR, "input_urls.json")
LOG_FILE = os.path.join(os.path.dirname(SCRIPT_DIR), "logs", f"{SITE_SLUG}.log")

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def clean_html(html_str: str) -> str:
    if not html_str:
        return ""
    text = re.sub(r"<[^>]+>", " ", html_str)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def make_absolute_url(url: str, base: str = SITE_URL) -> str:
    if not url:
        return ""
    if url.startswith("http"):
        return url
    if url.startswith("//"):
        return f"https:{url}"
    return urljoin(base, url)


def fetch_page(url: str) -> Optional[tuple[BeautifulSoup, int]]:
    for attempt in range(MAX_RETRIES):
        try:
            time.sleep(DELAY_BETWEEN_REQUESTS)
            response = requests.get(url, headers=HEADERS, timeout=30, verify=False)
            response.raise_for_status()
            return BeautifulSoup(response.text, "html.parser"), response.status_code
        except requests.RequestException as e:
            logger.error(f"Failed to fetch {url} (attempt {attempt + 1}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(DELAY_BETWEEN_REQUESTS * 2)

    return None


# ═══════════════════════════════════════════════════════════════════════════════
# EXTRACTION - CUSTOMIZE selectors below
# ═══════════════════════════════════════════════════════════════════════════════

def extract_jsonld(soup: BeautifulSoup) -> Optional[dict]:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
            if isinstance(data, dict) and data.get("@type") == "Product":
                return data
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("@type") == "Product":
                        return item
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def extract_product_from_page(soup: BeautifulSoup, url: str, status_code: int, src_url: str) -> dict:
    product = {
        "id": 0,
        "title": "",
        "price": "",
        "availability": "",
        "url": url,
        "src_url": src_url,
        "status_code": status_code,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": "",
    }

    # Soft 404 detection: the title node and the price node are both missing.
    title_el = soup.select_one("h1.product-title")
    price_el = soup.select_one("span.price-value")
    desc_el = soup.select_one("div.product-description")

    if not title_el and not price_el:
        product["remarks"] = "Soft 404: content not found"
        return product

    jsonld = extract_jsonld(soup)

    # Title
    product["title"] = title_el.get_text(strip=True) if title_el else (jsonld or {}).get("name", "")

    # Price
    product["price"] = price_el.get_text(strip=True) if price_el else ""

    # Availability (JSON-LD first, CSS fallback)
    availability = (jsonld or {}).get("availability", "")
    if not availability:
        availability = "In Stock" if soup.select_one("meta[property='product:availability']") else ""
    product["availability"] = clean_html(availability)

    # Description
    if desc_el:
        product["remarks"] = ""
        product["description"] = clean_html(str(desc_el))

    return product


# ═══════════════════════════════════════════════════════════════════════════════
# DISCOVERY
# ═══════════════════════════════════════════════════════════════════════════════

def discover_product_urls() -> tuple[list[str], dict]:
    """Phase 1: discover product URLs by paginating through listing pages."""
    all_urls = []
    seen = set()
    stop_reason = "no_next_link"
    _deadline_start = time.monotonic()

    for listing_url in PRODUCT_LISTING_URLS:
        page = 1
        while True:
            if time.monotonic() - _deadline_start > DISCOVERY_DEADLINE_SECONDS:
                stop_reason = "navigate_error"
                break
            sep = "&" if "?" in listing_url else "?"
            paginated_url = f"{listing_url}{sep}{PAGE_PARAM_NAME}={page}"

            result = fetch_page(paginated_url)
            if not result:
                stop_reason = "navigate_error"
                break

            soup, _ = result
            links = soup.select("a[href*='/product/']")
            new_on_page = 0
            for link in links:
                href = link.get("href", "")
                absolute_url = make_absolute_url(href)
                if absolute_url and absolute_url not in seen:
                    seen.add(absolute_url)
                    all_urls.append(absolute_url)
                    new_on_page += 1

            if len(links) == 0:
                stop_reason = "short_page"
                break

            if new_on_page == 0:
                stop_reason = "no_new_items"
                break

            if MAX_PAGES is not None and page >= MAX_PAGES:
                stop_reason = "max_pages_hit"
                break

            page += 1

    discovery_meta = {
        "stop_reason": stop_reason,
        "max_pages_hit": stop_reason == "max_pages_hit",
        "discovered_urls": len(all_urls),
    }
    return all_urls, discovery_meta


def load_urls_from_file(filepath: str) -> list[str]:
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("urls", [])


def save_urls_to_file(filepath: str, urls: list[str]) -> None:
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump({"urls": urls}, f, indent=2, ensure_ascii=False)


def main():
    parser = argparse.ArgumentParser(description=f"HTTP scraper for {SITE_NAME}")
    parser.add_argument("--sample", action="store_true")
    parser.add_argument("--input", type=str, default=None)
    parser.add_argument("--discover-only", action="store_true")
    args = parser.parse_args()

    if args.discover_only:
        urls, _meta = discover_product_urls()
        save_urls_to_file(INPUT_FILE, urls)
        return

    urls = load_urls_from_file(args.input) if args.input else discover_product_urls()[0]
    results = []
    for i, url in enumerate(urls):
        result = fetch_page(url)
        if result:
            soup, status_code = result
            product = extract_product_from_page(soup, url, status_code, SRC_URL)
            product["id"] = i + 1
            results.append(product)

    output = {"site": SITE_URL, "products": results}
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
