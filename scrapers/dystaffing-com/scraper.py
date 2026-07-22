#!/usr/bin/env python3
"""
SSR Div-List Scraper — dystaffing.com job board

All 490+ jobs are embedded as .single-job divs on the single listing page
at /job-search. No individual job detail URLs exist. Extracts job records
directly from the listing DOM.

Usage:
    python3 scraper_draft.py
    python3 scraper_draft.py --sample --input input_urls.json
    python3 scraper_draft.py --listing-url https://dystaffing.com/job-search --limit 10
"""
import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, parse_qs, urlencode

from bs4 import BeautifulSoup

# ── Configuration (Code Writer adapted) ──────────────────────────────────────

OUTPUT_KEY = "jobs"
ITEM_SELECTOR = ".single-job"
DEFAULT_LISTING_URL = "https://dystaffing.com/job-search"

# Rate limiting
REQUEST_DELAY = 1.0
MAX_RETRIES = 3
BACKOFF_BASE = 2.0

# No proxy for this site
PROXY_TIER = "none"

BROWSER_SERVICE_URL = os.environ.get("BROWSER_SERVICE_URL", "http://browser_service:8001")

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

# ── HTTP fetch ───────────────────────────────────────────────────────────────

def _fetch(url, retry=0):
    """HTTP GET with retries and rate limiting. No proxy for this site."""
    import httpx

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    time.sleep(REQUEST_DELAY)

    for attempt in range(MAX_RETRIES):
        try:
            with httpx.Client(headers=headers, timeout=30, follow_redirects=True) as client:
                resp = client.get(url)
            return resp.text, resp.status_code, str(resp.url)
        except Exception as exc:
            logger.warning("Fetch error (attempt %d): %s", attempt + 1, exc)
            if attempt < MAX_RETRIES - 1:
                time.sleep(min(BACKOFF_BASE ** (attempt + 1), 30))
    return "", 0, url


# ── Browser-based fetch (fallback for JS-rendered pages) ──────────────────────

def _fetch_browser(url):
    """Fetch URL using the browser service for JS-rendered content."""
    import httpx

    payload = {
        "url": url,
        "wait": 4000,
        "selector": ITEM_SELECTOR,
        "viewport": {"width": 1920, "height": 1080},
    }

    try:
        with httpx.Client(timeout=60) as client:
            resp = client.post(f"{BROWSER_SERVICE_URL}/scrape", json=payload)
        resp.raise_for_status()
        data = resp.json()
        html = data.get("html", "") or ""
        # The browser service returns status 200 even if the page itself had a different code
        return html, data.get("status_code", 200), url
    except Exception as exc:
        logger.error("Browser fetch failed: %s", exc)
        return "", 0, url


# ── Item extraction (Code Writer adapted) ────────────────────────────────────

def _find_items(soup):
    """Find all .single-job containers on the listing page."""
    return soup.select(ITEM_SELECTOR)


def _clean_html(html_str):
    """Remove HTML tags and clean up whitespace for description field."""
    if not html_str:
        return ""
    # Remove script/style tags and their content
    clean = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', html_str, flags=re.DOTALL | re.I)
    # Replace <br> and <p> with newlines
    clean = re.sub(r'<br\s*/?>', '\n', clean, flags=re.I)
    clean = re.sub(r'</(p|div|li|h[1-6])>', '\n', clean, flags=re.I)
    # Strip all remaining tags
    clean = re.sub(r'<[^>]+>', '', clean)
    # Collapse whitespace
    clean = re.sub(r'[ \t]+', ' ', clean)
    clean = re.sub(r'\n\s*\n', '\n', clean)
    return clean.strip()


def _extract_record(element, base_url, status_code=200):
    """Extract fields from a single .single-job container.

    Code Writer adapted: field selectors from product analysis.
    """
    record = {}

    # Code Writer adapted: title from .single-job-title
    title_el = element.select_one(".single-job-title")
    record["title"] = title_el.get_text(strip=True) if title_el else ""

    # Code Writer adapted: job_id from data-job-id attribute
    record["job_id"] = element.get("data-job-id", "") or ""

    # Code Writer adapted: location from .single-job-info-bar > div:first-child > p
    info_bar = element.select_one(".single-job-info-bar")
    if info_bar:
        divs = info_bar.select("div")
        if divs:
            first_p = divs[0].select_one("p")
            record["location"] = first_p.get_text(strip=True) if first_p else ""
            # Code Writer adapted: job_type from .single-job-info-bar > div:last-child > p
            last_p = divs[-1].select_one("p")
            record["job_type"] = last_p.get_text(strip=True) if last_p else ""
        else:
            record["location"] = ""
            record["job_type"] = ""
    else:
        record["location"] = ""
        record["job_type"] = ""

    # Code Writer adapted: specialty from data-specialty attribute
    record["specialty"] = element.get("data-specialty", "") or ""

    # Code Writer adapted: city from data-city attribute
    record["city"] = element.get("data-city", "") or ""

    # Code Writer adapted: state from data-state attribute
    record["state"] = element.get("data-state", "") or ""

    # Code Writer adapted: provider_type from data-provider-type attribute
    record["provider_type"] = element.get("data-provider-type", "") or ""

    # Code Writer adapted: description from .single-job-description
    desc_el = element.select_one(".single-job-description")
    if desc_el:
        record["description"] = _clean_html(desc_el.decode_contents())
    else:
        record["description"] = ""

    # Code Writer adapted: url — no individual job pages, set to listing URL
    record["url"] = base_url

    # Code Writer adapted: src_url — listing page where discovered
    record["src_url"] = base_url

    # Code Writer adapted: remarks
    record["remarks"] = ""

    # Code Writer adapted: scraped_at as ISO-8601 timestamp
    record["scraped_at"] = datetime.now(timezone.utc).isoformat()

    # Code Writer adapted: status_code from HTTP response
    record["status_code"] = status_code

    return record


# ── Core scraping ────────────────────────────────────────────────────────────

def scrape_listing(listing_url, limit=0, use_browser=False):
    """Scrape all job items from the listing page.

    All jobs are on a single page at /job-search (no pagination).
    """
    all_records = []
    seen_ids = set()

    logger.info("Fetching listing page: %s", listing_url)

    if use_browser:
        logger.info("Using browser service for JS-rendered content")
        html, status_code, final_url = _fetch_browser(listing_url)
    else:
        html, status_code, final_url = _fetch(listing_url)

    if not html or status_code >= 400:
        logger.warning("Failed to fetch listing page (status %d)", status_code)
        return all_records

    soup = BeautifulSoup(html, "html.parser")
    items = _find_items(soup)
    logger.info("Found %d .single-job items", len(items))

    if not items:
        logger.info("No items found on listing page")
        return all_records

    # If HTTP returned no items, try browser as fallback
    if len(items) == 0 and not use_browser:
        logger.info("HTTP returned empty listing, trying browser service...")
        html, status_code, final_url = _fetch_browser(listing_url)
        if html:
            soup = BeautifulSoup(html, "html.parser")
            items = _find_items(soup)
            logger.info("Browser: found %d items", len(items))

    page_records = 0
    for element in items:
        record = _extract_record(element, final_url, status_code)
        if not record:
            continue

        # Deduplicate by job_id
        item_id = record.get("job_id") or record.get("title") or ""
        if item_id and item_id in seen_ids:
            continue
        if item_id:
            seen_ids.add(item_id)

        # Quality gate: drop items without title
        if not record.get("title"):
            continue

        all_records.append(record)
        page_records += 1

        if limit and len(all_records) >= limit:
            logger.info("Reached limit of %d items", limit)
            break

    logger.info("Extracted %d records", len(all_records))
    return all_records


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="DyStaffing Job Board Scraper")
    parser.add_argument("--listing-url", default=DEFAULT_LISTING_URL,
                        help="Listing page URL (default: /job-search)")
    parser.add_argument("--input", default=None,
                        help="Path to input_urls.json (takes precedence over default)")
    parser.add_argument("--urls", nargs="*", default=[], help="Inline URLs")
    parser.add_argument("--sample", action="store_true", help="Sample mode (≤5 items)")
    parser.add_argument("--limit", type=int, default=0, help="Max items (0=unlimited)")
    parser.add_argument("--no-proxy", action="store_true", default=True,
                        help="No proxy (default for this site)")
    args = parser.parse_args()

    # Determine the listing URL
    listing_url = args.listing_url

    # --input takes precedence over everything
    if args.input:
        input_path = args.input
        if not os.path.isabs(input_path):
            input_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), input_path)
        if os.path.exists(input_path):
            with open(input_path) as f:
                data = json.load(f)
            urls = data.get("urls", [])
            if urls:
                listing_url = urls[0]
                logger.info("Using listing URL from --input: %s", listing_url)

    # --urls takes precedence if provided
    if args.urls:
        listing_url = args.urls[0]
        logger.info("Using listing URL from --urls: %s", listing_url)

    if not listing_url:
        logger.error("No listing URL provided.")
        sys.exit(1)

    limit = args.limit or (5 if args.sample else 0)
    logger.info("Starting scrape: %s (limit=%d)", listing_url, limit)

    records = scrape_listing(listing_url, limit=limit)

    # If HTTP got 0 items and we haven't tried browser yet, try it
    if len(records) == 0:
        logger.info("HTTP mode returned 0 items, retrying with browser...")
        records = scrape_listing(listing_url, limit=limit, use_browser=True)

    # Output
    output = {
        "site": urlparse(listing_url).netloc,
        OUTPUT_KEY: records,
        "metadata": {
            "listing_url": listing_url,
            "total_items": len(records),
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        },
    }

    output_file = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        f"output_{time.strftime('%Y-%m-%d_%H%M%S')}.json",
    )
    with open(output_file, "w") as f:
        # _OUTPUT_FILTER_APPLIED — drop non-item pages (content-type aware)
        _FILTER_FIELDS = ['company', 'location', 'description']
        try:
            _before = len(output.get(OUTPUT_KEY, []))
            output[OUTPUT_KEY] = [p for p in output.get(OUTPUT_KEY, []) if p.get('title') and (p.get('company') or p.get('location') or p.get('description'))]
            _after = len(output[OUTPUT_KEY])
            if _before != _after:
                logger.info('output filter: %d → %d items (removed %d without title+company,location,description)',
                             _before, _after, _before - _after)
        except Exception:
            pass


        json.dump(output, f, indent=2, ensure_ascii=False, default=str)

    logger.info("Done: %d records → %s", len(records), output_file)
    print(json.dumps({"output_file": output_file, "total_items": len(records)}, indent=2))


if __name__ == "__main__":
    main()