#!/usr/bin/env python3
"""
Kirkland & Ellis Careers Gateway Scraper

Two-phase scraper for www.kirkland.com/careers gateway pages.

Phase 1: Discover careers gateway subpages by crawling /careers and
         following all same-domain /careers/* links.
Phase 2: Extract external job board links and metadata from each page.

NOTE: www.kirkland.com does NOT host actual job postings. The /careers
section is a gateway linking to external job board platforms:
  - recruitx.kirkland.com   (U.S. lateral attorneys)
  - staffjobsus.kirkland.com (U.S. staff positions)
  - fsr.cvmailuk.com         (UK positions)

This scraper catalogs those gateway pages and the external job board links
they contain.

Usage:
    python3 scraper.py                                   # full discovery + extraction
    python3 scraper.py --query "careers"                 # search-based discovery
    python3 scraper.py --sample                          # first 5 items via Phase 1 discovery
    python3 scraper.py --limit 20                       # cap at 20 items
    python3 scraper.py --input custom_urls.json         # use specific URL list
    python3 scraper.py --urls URL1 URL2 URL3            # URLs via CLI
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
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

SITE_NAME = "Kirkland & Ellis"
SITE_URL = "https://www.kirkland.com"
PLATFORM = "custom_cms"
SITE_SLUG = "kirkland-com"
SCRAPING_METHOD = "http_requests"
PROXY_TIER = "none"
OUTPUT_KEY = "jobs"
CONTENT_TYPE = "job_posting"

# Phase 1: Discovery
DEFAULT_QUERY = "careers"
CAREERS_BASE_URL = "https://www.kirkland.com/careers"
MAX_PAGES = None  # unlimited — crawl all discovered subpages
DELAY_BETWEEN_REQUESTS = 1.5  # seconds between requests
PHASE2_WORKERS = 4  # concurrent extraction workers

# Phase 2: Extraction selectors (from product_analysis)
EXTERNAL_LINK_SELECTORS = [
    'a[href*="recruitx"]',
    'a[href*="staffjobs"]',
    'a[href*="cvmailuk"]',
    'a[href*="jobBoard"]',
]
EXTERNAL_JOB_BOARD_DOMAINS = [
    "recruitx.kirkland.com",
    "staffjobsus.kirkland.com",
    "fsr.cvmailuk.com",
]

# Known careers seed URLs to start discovery
SEED_URLS = [
    "https://www.kirkland.com/careers",
]

# Gateway pages don't have standard job_posting fields (company/location).
# Use lenient filter — title-only is sufficient for gateway pages.
CORE_FILTER_FIELDS: list[str] = []

# Output
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CURRENCY = "USD"

# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(SITE_SLUG)

# ═══════════════════════════════════════════════════════════════════════════════
# CHECKPOINT — resume Phase 2 after a crash/retry
# ═══════════════════════════════════════════════════════════════════════════════

_CHECKPOINT_PATH = os.path.join(SCRIPT_DIR, "discovered_urls_checkpoint.json")


def _write_checkpoint(urls: list[str]) -> None:
    """Save discovered URLs so a crash-retry can resume Phase 2 directly."""
    try:
        with open(_CHECKPOINT_PATH, "w") as f:
            json.dump({"urls": list(urls), "count": len(urls), "ts": time.time()}, f)
        logger.debug("Checkpoint: saved %d URLs to %s", len(urls), _CHECKPOINT_PATH)
    except Exception as exc:
        logger.warning("Checkpoint: write failed: %s", exc)


def _load_checkpoint() -> list[str]:
    """Load discovered URLs from a previous run's checkpoint (if any)."""
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
# HTTP PRIMITIVE
# ═══════════════════════════════════════════════════════════════════════════════

def _fetch_page(url: str, timeout: int = 20) -> tuple[int, str, str]:
    """Fetch a page with httpx. Returns (status_code, html, final_url).

    Retries on transient errors (5xx, 429, timeouts).
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    max_retries = 3
    for attempt in range(max_retries):
        try:
            with httpx.Client(follow_redirects=True, timeout=timeout) as client:
                response = client.get(url, headers=headers)
                status = response.status_code
                html = response.text
                final_url = str(response.url)
                if status in (429, 502, 503) or status >= 500:
                    logger.warning(
                        "Fetch: HTTP %d on %s (attempt %d/%d)",
                        status, url[:60], attempt + 1, max_retries,
                    )
                    time.sleep(2 ** attempt)
                    continue
                return status, html, final_url
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            logger.warning(
                "Fetch: transient error on %s: %s (attempt %d/%d)",
                url[:60], exc, attempt + 1, max_retries,
            )
            time.sleep(2 ** attempt)

    logger.warning("Fetch: exhausted %d retries on %s", max_retries, url[:80])
    return 0, "", url


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 1: URL DISCOVERY
# ═══════════════════════════════════════════════════════════════════════════════

def _is_careers_gateway_url(href: str) -> bool:
    """Check if a URL is a careers gateway subpage.

    True for same-domain URLs under /careers/ with depth >= 2 segments
    (e.g., /careers/united-states/laterals). False for the main /careers
    hub itself, other site sections, and external domains.
    """
    if not href:
        return False
    parsed = urlparse(href)
    site_host = urlparse(SITE_URL).hostname.lower()
    if parsed.hostname and parsed.hostname.lower() != site_host:
        return False
    path = parsed.path.strip("/")
    # Must be under /careers/ with at least one sub-segment
    if not path.startswith("careers/"):
        return False
    parts = path.split("/")
    # At minimum: careers/region or careers/region/category
    if len(parts) < 2:
        return False
    return True


def _extract_careers_links(html: str, base_url: str) -> list[str]:
    """Extract all careers gateway subpage URLs from an HTML page."""
    if not html:
        return []
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as exc:
        logger.warning("Phase 1: HTML parse failed: %s", exc)
        return []

    links: list[str] = []
    seen: set[str] = set()

    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if not href:
            continue
        # Make absolute
        absolute = urljoin(base_url, href)
        if absolute in seen:
            continue
        if _is_careers_gateway_url(absolute):
            links.append(absolute)
            seen.add(absolute)

    return links


def _discover_careers_subpages(
    max_pages: Optional[int] = None,
    limit: Optional[int] = None,
) -> tuple[list[str], str]:
    """Phase 1: Discover all careers gateway subpages by crawling /careers.

    Strategy:
    1. Fetch the main /careers page and the homepage
    2. Extract all /careers/* links
    3. For each discovered subpage, fetch and extract more links (recursive depth-1)
    4. Dedup and return

    Returns (urls, stop_reason).
    """
    logger.info("Phase 1: Starting careers gateway discovery from %s", CAREERS_BASE_URL)
    all_urls: list[str] = []
    seen: set[str] = set()

    # Fetch pages to discover links from
    pages_to_fetch = list(SEED_URLS)
    # Also add homepage (may have careers links in nav/mega-menu)
    pages_to_fetch.append(SITE_URL)

    # Round 1: Fetch seed pages and extract careers links
    for seed_url in pages_to_fetch:
        status, html, final_url = _fetch_page(seed_url)
        if status == 0 or not html:
            logger.warning("Phase 1: Failed to fetch seed %s (status=%d)", seed_url[:60], status)
            continue

        sublinks = _extract_careers_links(html, final_url)
        for link in sublinks:
            if link not in seen:
                all_urls.append(link)
                seen.add(link)

        logger.info(
            "Phase 1: Seed %s → %d careers links found (total: %d)",
            seed_url[:50], len(sublinks), len(all_urls),
        )
        time.sleep(DELAY_BETWEEN_REQUESTS)

    # Round 2: Fetch each discovered subpage to find deeper links
    second_level: list[str] = list(all_urls)
    for subpage_url in second_level:
        if limit and len(all_urls) >= limit:
            break
        if max_pages and len(seen) >= max_pages:
            break

        status, html, final_url = _fetch_page(subpage_url)
        if status == 0 or not html:
            continue

        sublinks = _extract_careers_links(html, final_url)
        new_count = 0
        for link in sublinks:
            if link not in seen:
                all_urls.append(link)
                seen.add(link)
                new_count += 1

        if new_count > 0:
            logger.info(
                "Phase 1: Subpage %s → %d new links (total: %d)",
                subpage_url[:50], new_count, len(all_urls),
            )
        time.sleep(DELAY_BETWEEN_REQUESTS)

    # Dedup preserving order
    unique_urls = list(dict.fromkeys(all_urls))
    if limit:
        unique_urls = unique_urls[:limit]

    stop_reason = "no_new_items" if len(unique_urls) > 0 else "navigate_error"
    logger.info(
        "Phase 1: Discovered %d careers gateway URLs (%s)",
        len(unique_urls), stop_reason,
    )
    return unique_urls, stop_reason


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 2: ITEM EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_external_job_board_links(soup: BeautifulSoup) -> list[dict]:
    """Extract links to external job board platforms from the page.

    Returns list of {"href": "...", "text": "..."} dicts.
    """
    links: list[dict] = []
    seen_hrefs: set[str] = set()

    for selector in EXTERNAL_LINK_SELECTORS:
        try:
            for a in soup.select(selector):
                href = a.get("href", "")
                text = a.get_text(strip=True)
                if not href or href in seen_hrefs:
                    continue
                # Make absolute if needed
                if href.startswith("/"):
                    href = SITE_URL.rstrip("/") + href
                seen_hrefs.add(href)
                links.append({"href": href, "text": text})
        except Exception:
            pass

    # Also scan for any link to known external job board domains
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if not href or href in seen_hrefs:
            continue
        parsed = urlparse(href)
        hostname = (parsed.hostname or "").lower()
        for domain in EXTERNAL_JOB_BOARD_DOMAINS:
            if domain in hostname:
                text = a.get_text(strip=True)
                if href.startswith("/"):
                    href = SITE_URL.rstrip("/") + href
                seen_hrefs.add(href)
                links.append({"href": href, "text": text})
                break

    return links


def _extract_gateway_page(page_url: str, src_url: str) -> dict:
    """Phase 2: Extract data from a careers gateway page.

    Extracts:
    - title (from <meta name="title"> or <title>)
    - description (from <meta name="description">)
    - external_job_board_links (links to external job boards)
    - url, src_url, status_code, scraped_at, remarks
    """
    status_code, html, final_url = _fetch_page(page_url)

    if status_code == 0:
        return _error_item(page_url, src_url, "fetch failed after retries")

    item: dict = {
        "id": 0,  # assigned after successful extraction
        "title": "",
        "url": final_url,
        "src_url": src_url,
        "status_code": status_code,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": "",
        "external_job_board_links": [],
        "description": "",
    }

    # Soft 404 detection
    if status_code == 404:
        item["remarks"] = "Soft 404: page not found"
        return item

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as exc:
        item["remarks"] = f"Error: HTML parse failed: {exc}"
        return item

    # Check for soft 404 in page content
    page_title_tag = soup.find("title")
    page_title = page_title_tag.get_text(strip=True) if page_title_tag else ""
    h1 = soup.select_one("h1")
    h1_text = h1.get_text(strip=True).lower() if h1 else ""

    soft_404_patterns = ["not found", "unavailable", "discontinued", "no longer available", "page not found"]
    for pattern in soft_404_patterns:
        if pattern in page_title.lower() or pattern in h1_text:
            item["remarks"] = f"Soft 404: {pattern}"
            return item

    # Check if final URL differs significantly from requested URL (redirect away)
    if final_url and page_url and urlparse(final_url).path != urlparse(page_url).path:
        if not _is_careers_gateway_url(final_url):
            item["remarks"] = f"Soft 404: redirected to {final_url}"
            return item

    # Extract title
    # Method: meta[name="title"] (per product_analysis)
    meta_title = soup.find("meta", attrs={"name": "title"})
    if meta_title and meta_title.get("content"):
        item["title"] = meta_title["content"].strip()
    # Fallback: <title> tag
    if not item["title"] and page_title:
        item["title"] = page_title
    # Fallback: <h1>
    if not item["title"] and h1:
        item["title"] = h1.get_text(strip=True)

    # Extract description
    meta_desc = soup.find("meta", attrs={"name": "description"})
    if meta_desc and meta_desc.get("content"):
        item["description"] = meta_desc["content"].strip()
    # Fallback: first meaningful paragraph text (truncated to 500 chars)
    if not item["description"]:
        for tag in soup.select("p"):
            text = tag.get_text(strip=True)
            if len(text) > 40:
                item["description"] = text[:500]
                break

    # Extract external job board links
    item["external_job_board_links"] = _extract_external_job_board_links(soup)

    # Add remark about the nature of this page
    if item["external_job_board_links"]:
        domains = set()
        for link in item["external_job_board_links"]:
            hostname = urlparse(link["href"]).hostname or ""
            for domain in EXTERNAL_JOB_BOARD_DOMAINS:
                if domain in hostname:
                    domains.add(domain)
        item["remarks"] = f"Gateway page linking to: {', '.join(sorted(domains))}"
    else:
        item["remarks"] = "Gateway page (no external job board links found)"

    return item


def _error_item(url: str, src_url: str, error: str) -> dict:
    """Create an error item dict for a failed extraction."""
    return {
        "id": 0,
        "title": "",
        "url": url,
        "src_url": src_url,
        "status_code": 0,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": f"Error: {error[:200]}",
        "external_job_board_links": [],
        "description": "",
    }


def _extract_item_safe(item_url: str, src_url: str) -> dict:
    """Wrapper — never raises; converts exceptions to error items."""
    try:
        return _extract_gateway_page(item_url, src_url)
    except Exception as exc:
        logger.error("Phase 2: unexpected failure on %s: %s", item_url[:80], exc)
        return _error_item(item_url, src_url, str(exc))


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description=f"{SITE_NAME} Careers Gateway Scraper")
    parser.add_argument("--query", type=str, help="Search query (default: 'careers')")
    parser.add_argument("--category-url", type=str, help="Category URL to crawl")
    parser.add_argument("--listing-url", type=str, help="Listing page URL to paginate")
    parser.add_argument("--sample", action="store_true", help="Scrape first 5 items only")
    parser.add_argument("--limit", type=int, default=None, help="Max items to scrape")
    parser.add_argument("--input", type=str, help="Path to input URLs JSON file")
    parser.add_argument("--urls", nargs="+", help="Product URLs as CLI arguments")
    parser.add_argument("--no-proxy", action="store_true", help="Disable proxy (no-op for this site)")
    parser.add_argument(
        "--discover-only", action="store_true",
        help="Run Phase 1 discovery only; skip Phase 2 extraction",
    )
    parser.add_argument(
        "--fresh-discovery", action="store_true",
        help="Ignore existing checkpoint; run Phase 1 from scratch",
    )
    args = parser.parse_args()

    sample_limit = 5 if args.sample else None
    limit = sample_limit if sample_limit else args.limit
    start_time = time.time()
    discovered_urls: list[str] = []
    src_url_base = CAREERS_BASE_URL

    # ── Discovery-coverage state ────────────────────────────────────────────
    ran_phase1 = True
    skipped_reason: Optional[str] = None
    aggregate_stop_reason = "no_next_link"
    max_pages_hit = False

    logger.info("=" * 80)
    logger.info(f"Starting scraper for {SITE_NAME}")
    logger.info("Site URL: %s", SITE_URL)
    logger.info("=" * 80)

    # ── CLI URLs mode: URLs provided directly via --urls ───────────────────
    if args.urls:
        discovered_urls = list(args.urls)
        if sample_limit:
            discovered_urls = discovered_urls[:sample_limit]
        ran_phase1 = False
        skipped_reason = "cli_urls"
        aggregate_stop_reason = "skipped"
        logger.info("Using %d URLs from --urls", len(discovered_urls))

    # ── --input mode: URLs from file ──────────────────────────────────────
    elif args.input:
        try:
            with open(args.input, "r") as f:
                data = json.load(f)
            discovered_urls = data.get("urls", [])
            if sample_limit:
                discovered_urls = discovered_urls[:sample_limit]
            ran_phase1 = False
            skipped_reason = "input_file"
            aggregate_stop_reason = "skipped"
            logger.info("Using %d URLs from --input %s", len(discovered_urls), args.input)
        except Exception as exc:
            logger.error("Failed to read input file %s: %s", args.input, exc)
            sys.exit(1)

    # ── Checkpoint resume ──────────────────────────────────────────────────
    # NEVER load checkpoint when --sample or --fresh-discovery is set —
    # those require fresh Phase 1 discovery, not a stale checkpoint.
    else:
        force_fresh = args.fresh_discovery or args.sample
        checkpoint_urls = [] if force_fresh else _load_checkpoint()
        if checkpoint_urls:
            discovered_urls = checkpoint_urls
            ran_phase1 = False
            skipped_reason = "checkpoint_loaded"
            aggregate_stop_reason = "skipped"
            logger.info(
                "Phase 1: SKIPPED (resumed from checkpoint with %d URLs)",
                len(discovered_urls),
            )

    # ── Phase 1: Discover URLs ───────────────────────────────────────────
    if not discovered_urls:
        query = args.query or DEFAULT_QUERY
        logger.info("Phase 1: discovering via query '%s'", query)

        discovered_urls, stop_reason = _discover_careers_subpages(MAX_PAGES, limit)
        aggregate_stop_reason = stop_reason
        max_pages_hit = stop_reason == "max_pages_hit"
        _write_checkpoint(discovered_urls)

        # Apply sample limit to discovered URLs if --sample is set
        if sample_limit and len(discovered_urls) > sample_limit:
            logger.info(
                "Phase 1: sample mode, limiting %d discovered URLs to %d",
                len(discovered_urls), sample_limit,
            )
            discovered_urls = discovered_urls[:sample_limit]

        logger.info("Phase 1: discovered %d URLs", len(discovered_urls))
    else:
        logger.info("Phase 1: SKIPPED, using %d existing URLs", len(discovered_urls))

    logger.info("Total products: %d", len(discovered_urls))
    logger.info("=" * 80)

    if not discovered_urls:
        logger.warning("No item URLs discovered")
        # Still emit output so the gate can see the failure

    # ── Phase 2: Extract data concurrently ────────────────────────────────
    items: list[dict] = []

    if args.discover_only:
        logger.info(
            "--discover-only: skipping Phase 2 extraction (%d URLs discovered)",
            len(discovered_urls),
        )
    elif discovered_urls:
        total = len(discovered_urls)
        logger.info(
            "Phase 2: Extracting data from %d items (%d workers)",
            total, PHASE2_WORKERS,
        )

        completed = 0
        with ThreadPoolExecutor(max_workers=PHASE2_WORKERS) as pool:
            future_to_url = {
                pool.submit(_extract_item_safe, url, url): idx
                for idx, url in enumerate(discovered_urls)
            }
            results_by_index: dict[int, dict] = {}

            for future in as_completed(future_to_url):
                idx = future_to_url[future]
                try:
                    item = future.result()
                except Exception as exc:
                    url = discovered_urls[idx]
                    item = _error_item(url, url, str(exc))

                results_by_index[idx] = item
                completed += 1

                if completed % 25 == 0 or completed == total:
                    percent = (completed / total) * 100
                    logger.info(
                        "Progress: [%d/%d] (%.1f%%)", completed, total, percent,
                    )

        # Reassemble in discovery order
        items = [results_by_index[i] for i in range(len(discovered_urls)) if i in results_by_index]

    # ── Assign sequential IDs ─────────────────────────────────────────────
    for i, item in enumerate(items, 1):
        item["id"] = i

    # ── Output filter ─────────────────────────────────────────────────────
    # For job_posting_gateway: keep any item with a title. No company/location
    # fields exist on this site, so we only filter out items without a title.
    before = len(items)
    items = [it for it in items if it.get("title")]
    if len(items) != before:
        logger.info(
            "output filter: %d → %d items (dropped %d without title)",
            before, len(items), before - len(items),
        )

    # ── discovery_coverage block ──────────────────────────────────────────
    discovery_coverage = {
        "stop_reason": aggregate_stop_reason,
        "found": len(items),
        "discovered_urls": len(discovered_urls),
        "expected_total": None,
        "dimensions_iterated": 0,
        "dimensions_total": 0,
        "max_pages_hit": max_pages_hit,
        "ran_phase1": ran_phase1,
        "skipped_reason": skipped_reason,
    }

    # ── Build output ───────────────────────────────────────────────────────
    success = sum(1 for it in items if it.get("title"))
    failed = len(discovered_urls) - success

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
            "failed_products": failed,
            "rate_limit_delay": DELAY_BETWEEN_REQUESTS,
            "discovered_urls": len(discovered_urls),
            "extracted_items": len(items),
            "execution_model": "http_requests",
            "stealth": "none",
            "discovery_coverage": discovery_coverage,
        },
    }

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    output_filename = os.path.join(SCRIPT_DIR, f"output_{timestamp}.json")

    with open(output_filename, "w", encoding="utf-8") as f:
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

    logger.info("=" * 80)
    logger.info("EXTRACTION COMPLETE")
    logger.info("Total: %d, Success: %d, Failed: %d", len(items), success, failed)
    logger.info("Output: %s", output_filename)
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
