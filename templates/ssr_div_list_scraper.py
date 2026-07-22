#!/usr/bin/env python3
"""
SSR Div-List Scraper Template

For sites where items are server-rendered as div/li elements with data-*-id
attributes on a LISTING page (no per-item detail pages). Examples:
- dystaffing.com/job-search: <li class="single-job" data-job-id="...">
- Workday portals, job boards with inline listings.

Single-phase: fetch listing → find item containers → extract fields from each
container's DOM → paginate → output. No Phase 1 discovery / Phase 2 detail fetch.

Usage:
    python3 scraper_draft.py --sample --input input_urls.json
    python3 scraper_draft.py --listing-url https://site.com/jobs --limit 500
"""
import argparse
import json
import logging
import os
import re
import sys
import time
from urllib.parse import urljoin, urlparse, parse_qs, urlencode

from bs4 import BeautifulSoup

# ── Configuration (Code Writer adapts these) ──────────────────────────────────

OUTPUT_KEY = "jobs"            # "products" for e-commerce, "jobs" for job boards
ITEM_SELECTOR = "[data-job-id]"  # CSS selector for item containers on the listing page
LISTING_URL = ""               # Default listing URL (set by code_writer from nav analysis)

# Rate limiting
REQUEST_DELAY = 1.0            # seconds between requests
MAX_RETRIES = 3
BACKOFF_BASE = 2.0

# Pagination
MAX_PAGES = 50                 # hard cap on pages to scrape
PAGE_PARAM = "page"            # query param for pagination (?page=2)

# Proxy
PROXY_TIER = os.environ.get("PROXY_TIER", "none")
STEALTH = os.environ.get("STEALTH", os.environ.get("STEALTH_BROWSER", "none")).lower()
_env_stealth = (os.environ.get("STEALTH_BROWSER") or os.environ.get("SCRAPER_STEALTH") or "").strip().lower()
if _env_stealth in ("cloak", "true", "1"):
    STEALTH = "cloak"
elif STEALTH.startswith("{") and STEALTH.endswith("}"):
    STEALTH = "none"

BROWSER_SERVICE_URL = os.environ.get("BROWSER_SERVICE_URL", "http://browser_service:8001")

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

# ── HTTP fetch with proxy escalation ──────────────────────────────────────────

def _get_proxy():
    """Return a proxies dict for httpx, or None."""
    if PROXY_TIER == "none":
        return None
    try:
        import sys as _sys, os as _os
        _sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
        from src.proxy import ProxyConfig
        cfg = ProxyConfig.from_file("config/proxy.json")
        return cfg.get_proxies(PROXY_TIER)
    except Exception:
        return None


def _fetch(url, retry=0):
    """HTTP GET with retries, proxy escalation, and rate limiting."""
    import httpx

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    proxies = _get_proxy()
    time.sleep(REQUEST_DELAY)

    for attempt in range(MAX_RETRIES):
        try:
            with httpx.Client(headers=headers, timeout=30, follow_redirects=True, proxies=proxies) as client:
                resp = client.get(url)
            if resp.status_code < 400:
                return resp.text, resp.status_code, str(resp.url)
            if resp.status_code in (429, 503) and attempt < MAX_RETRIES - 1:
                wait = min(BACKOFF_BASE ** (attempt + 1), 30)
                logger.warning("Rate limited (%d), retrying in %ds", resp.status_code, wait)
                time.sleep(wait)
                continue
            return resp.text, resp.status_code, str(resp.url)
        except Exception as exc:
            logger.warning("Fetch error (attempt %d): %s", attempt + 1, exc)
            if attempt < MAX_RETRIES - 1:
                time.sleep(min(BACKOFF_BASE ** (attempt + 1), 30))
    return "", 0, url


# ── Item extraction (Code Writer adapts _extract_record) ──────────────────────

def _find_items(soup):
    """Find all item containers on the listing page using ITEM_SELECTOR."""
    return soup.select(ITEM_SELECTOR)


def _extract_record(element, base_url):
    """Extract fields from a single item container.

    Code Writer: substitute the field map's selectors/methods here.
    The element is a BeautifulSoup Tag (one item container from _find_items).
    Return a dict of {field_name: value}.
    """
    record = {}

    # Code Writer adapted: title
    title_el = element.find(["h2", "h3", "h4", "a"], class_=re.compile(r"title|name|heading", re.I))
    record["title"] = title_el.get_text(strip=True) if title_el else ""

    # Code Writer adapted: url (if the item has a link)
    link = element.find("a", href=True)
    record["url"] = urljoin(base_url, link["href"]) if link else ""

    # Code Writer adapted: data-id
    record["id"] = element.get("data-job-id") or element.get("data-id") or element.get("data-product-id") or ""

    return record


# ── Core scraping ─────────────────────────────────────────────────────────────

def _construct_page_url(base_url, page_num):
    """Add or update the pagination query param."""
    parsed = urlparse(base_url)
    params = parse_qs(parsed.query)
    params[PAGE_PARAM] = [str(page_num)]
    new_query = urlencode({k: v[0] for k, v in params.items()})
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{new_query}"


def scrape_listing(listing_url, limit=0):
    """Scrape items from a listing page with pagination.

    Args:
        listing_url: The URL of the listing page (page 1).
        limit: Max items to extract (0 = unlimited).
    """
    all_records = []
    seen_ids = set()

    for page in range(1, MAX_PAGES + 1):
        url = listing_url if page == 1 else _construct_page_url(listing_url, page)
        logger.info("Fetching page %d: %s", page, url)

        html, status, final_url = _fetch(url)
        if not html or status >= 400:
            logger.warning("Page %d failed (status %d), stopping", page, status)
            break

        soup = BeautifulSoup(html, "html.parser")
        items = _find_items(soup)
        logger.info("Page %d: found %d items", page, len(items))

        if not items:
            logger.info("No items on page %d, stopping", page)
            break

        page_records = 0
        for element in items:
            record = _extract_record(element, final_url)
            if not record:
                continue

            # Deduplicate by id or url
            item_id = record.get("id") or record.get("url") or ""
            if item_id and item_id in seen_ids:
                continue
            if item_id:
                seen_ids.add(item_id)

            # Quality gate: drop items without a title
            if not record.get("title"):
                continue

            all_records.append(record)
            page_records += 1

            if limit and len(all_records) >= limit:
                logger.info("Reached limit of %d items", limit)
                return all_records

        logger.info("Page %d: extracted %d records (total: %d)", page, page_records, len(all_records))

        if page_records == 0:
            logger.info("No records extracted on page %d, stopping", page)
            break

    return all_records


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="SSR Div-List Scraper")
    parser.add_argument("--listing-url", default=LISTING_URL, help="Listing page URL")
    parser.add_argument("--input", default="input_urls.json", help="Path to input_urls.json")
    parser.add_argument("--urls", nargs="*", default=[], help="Inline URLs")
    parser.add_argument("--sample", action="store_true", help="Sample mode (≤5 items)")
    parser.add_argument("--limit", type=int, default=0, help="Max items (0=unlimited)")
    args = parser.parse_args()

    # Determine the listing URL
    listing_url = args.listing_url
    if not listing_url and args.urls:
        listing_url = args.urls[0]
    if not listing_url:
        input_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), args.input)
        if os.path.exists(input_path):
            with open(input_path) as f:
                data = json.load(f)
            urls = data.get("urls", [])
            if urls:
                listing_url = urls[0]

    if not listing_url:
        logger.error("No listing URL provided. Use --listing-url or provide input_urls.json.")
        sys.exit(1)

    limit = args.limit or (5 if args.sample else 0)
    logger.info("Starting scrape: %s (limit=%d)", listing_url, limit)

    records = scrape_listing(listing_url, limit=limit)

    # Output
    output = {
        "site": urlparse(listing_url).netloc,
        OUTPUT_KEY: records,
        "metadata": {
            "listing_url": listing_url,
            "total_items": len(records),
            "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        },
    }

    output_file = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        f"output_{time.strftime('%Y-%m-%d_%H%M%S')}.json",
    )
    with open(output_file, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False, default=str)

    logger.info("Done: %d records → %s", len(records), output_file)
    print(json.dumps({"output_file": output_file, "total_items": len(records)}, indent=2))


if __name__ == "__main__":
    main()
