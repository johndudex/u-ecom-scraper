#!/usr/bin/env python3
"""Amergis Healthcare Job Scraper — HTTP Navigation (browser_service).

Two-phase architecture:
  Phase 1: Discover job URLs by navigating /jobs/ page (JS-rendered) via
           browser_service, extracting job links from the rendered DOM.
  Phase 2: Extract structured job data from each detail page (SSR WordPress).

Usage:
    python3 scraper.py --category-url "https://www.amergis.com/jobs/"
    python3 scraper.py --sample
    python3 scraper.py --input input_urls.json
    python3 scraper.py --urls URL1 URL2
    python3 scraper.py --limit 50
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
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse

import httpx
from bs4 import BeautifulSoup

# Make src.* importable
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from src.page_analysis import extract_jsonld  # noqa: E402

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

SITE_NAME = "Amergis Healthcare"
SITE_URL = "https://www.amergis.com"
PLATFORM = "WordPress (custom job board)"
SITE_SLUG = "amergis-com"
OUTPUT_KEY = "jobs"
CONTENT_TYPE = "job_posting"
CURRENCY = "USD"

# Code Writer adapted: browser_service for JS-rendered /jobs/ page
BROWSER_SERVICE_URL = os.environ.get("BROWSER_SERVICE_URL", "http://browser_service:8001")

# No stealth needed — WordPress site with minimal anti-bot
STEALTH = "none"

NAVIGATE_TIMEOUT = 120
MAX_RETRIES = 3
BACKOFF_BASE = 2.0
PHASE2_WORKERS = 8  # Code Writer adapted: 8 workers per requirements
DELAY_BETWEEN_REQUESTS = 0.5

# Phase 1: Discovery via /jobs/ listing page
SEARCH_URL_PATTERN = "https://www.amergis.com/jobs/"
SEARCH_BOX_SELECTOR = ""
SEARCH_SUBMIT_SELECTOR = ""
CATEGORY_URLS = []
PAGE_PARAM_NAME = ""  # No query-param pagination on /jobs/
ITEMS_PER_PAGE = None
MAX_PAGES = None
TOTAL_COUNT_SELECTOR = ""
DISCOVERY_DEADLINE_SECONDS = 300
COVERAGE_TARGET_TOTAL: Optional[int] = None

# Item link extraction from /jobs/ rendered page
ITEM_CONTAINER_SELECTOR = ""
ITEM_LINK_SELECTOR = "a[href*='/job/']"
ITEM_URL_PATTERN = r"/job/\d+"

# Phase 2: Extraction
SCRAPING_METHOD = "http_navigation"
PROXY_TIER = "none"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_URL = SITE_URL

# Output filter: job_posting core fields
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(SITE_SLUG)

# ═══════════════════════════════════════════════════════════════════════════════
# CHECKPOINT
# ═══════════════════════════════════════════════════════════════════════════════

_CHECKPOINT_PATH = os.path.join(SCRIPT_DIR, "discovered_urls_checkpoint.json")
_INPUT_FILE = os.path.join(SCRIPT_DIR, "input_urls.json")


def _write_checkpoint(urls: list[str]) -> None:
    try:
        with open(_CHECKPOINT_PATH, "w") as f:
            json.dump({"urls": list(urls), "count": len(urls), "ts": time.time()}, f)
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
# CORE HTTP PRIMITIVE — browser_service POST /navigate
# ═══════════════════════════════════════════════════════════════════════════════


def _navigate(url, actions=None, extract=None, retry=0):
    """POST /navigate with exponential backoff. Returns response dict or None."""
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
                return {"success": False, "url": url, "html": "", "status_code": 404}
            if r.status_code in (429, 502, 503):
                retry_after = r.headers.get("retry_after") or r.headers.get("Retry-After") or 5
                try:
                    retry_after = int(retry_after)
                except (TypeError, ValueError):
                    retry_after = 5
                logger.debug("navigate: %d on %s, backing off %ds (attempt %d/%d)",
                             r.status_code, url[:60], retry_after, attempt + 1, MAX_RETRIES)
                time.sleep(retry_after)
                continue
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            logger.debug("navigate: transient error on %s: %s (attempt %d/%d)",
                         url[:60], exc, attempt + 1, MAX_RETRIES)
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


def _is_job_url(href: str) -> bool:
    """Check if URL looks like a job detail page: /job/{id}-{slug}/"""
    if not href:
        return False
    site_host = (urlparse(SITE_URL).hostname or "").lower()
    if site_host and site_host not in href.lower():
        return False
    path = urlparse(href).path.strip("/")
    return bool(re.match(r"job/\d+-.+", path))


def _set_query_param(url: str, param: str, value) -> str:
    p = urlparse(url)
    qs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k != param]
    qs.append((param, str(value)))
    return urlunparse(p._replace(query=urlencode(qs)))


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
    "page_loaded": 0,  # Code Writer adapted: single-page no-pagination
}


def _merge_stop_reason(current: str, new: str) -> str:
    if _STOP_REASON_PRIORITY.get(new, 0) > _STOP_REASON_PRIORITY.get(current, 0):
        return new
    return current


def _extract_item_links(html: str) -> list[str]:
    """Extract job page URLs from the /jobs/ listing page HTML.

    The /jobs/ page is JS-rendered, so this runs on browser_service output.
    """
    if not html:
        return []
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as exc:
        logger.warning("Phase 1: HTML parse failed: %s", exc)
        return []

    links: list[str] = []
    seen: set[str] = set()

    # Code Writer adapted: extract all anchors with /job/ in href
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if not href:
            continue
        url = _make_absolute(href)
        if not url or url in seen:
            continue
        if not _is_job_url(url):
            continue
        links.append(url)
        seen.add(url)

    return links


def _discover_urls_via_category(
    category_url: str,
    max_pages: Optional[int] = None,
    limit: Optional[int] = None,
) -> tuple[list[str], str]:
    """Phase 1: Discover job URLs from the /jobs/ listing page.

    The /jobs/ page is JavaScript-rendered — browser_service renders it,
    then we extract job links from the resulting HTML. The page may load
    all jobs or paginate via JS; we scroll to ensure all are loaded.
    """
    logger.info("Phase 1: Navigating to %s", category_url)

    # Actions: wait for DOM + scroll to load lazy content + sleep for JS API
    actions = [
        {"type": "wait", "state": "domcontentloaded"},
        {"type": "sleep", "ms": 5000},
        {"type": "scroll", "direction": "bottom"},
        {"type": "sleep", "ms": 3000},
        {"type": "scroll", "direction": "bottom"},
        {"type": "sleep", "ms": 3000},
    ]

    resp = _navigate(category_url, actions=actions)
    if not resp or not resp.get("success"):
        blocked = bool(resp and resp.get("blocked"))
        logger.error("Phase 1: navigate failed for %s%s",
                     category_url, " (blocked)" if blocked else "")
        return [], "navigate_error"

    final_url = resp.get("url") or category_url
    html = resp.get("html", "")
    all_urls: list[str] = _extract_item_links(html)

    logger.info("Phase 1: %s → %d job URLs found", category_url[:60], len(all_urls))

    # Check if there's a "Load More" or pagination button we should click
    if html:
        try:
            soup = BeautifulSoup(html, "html.parser")
            # Look for load-more buttons or pagination
            load_more = soup.select_one("button.load-more, a.load-more, .pagination a.next, a[rel='next']")
            if load_more and max_pages is None or (max_pages and 1 < max_pages):
                logger.info("Phase 1: Found pagination/load-more element, clicking...")
                more_actions = [
                    {"type": "click", "selector": "button.load-more, a.load-more, .pagination a.next, a[rel='next']"},
                    {"type": "sleep", "ms": 5000},
                    {"type": "scroll", "direction": "bottom"},
                    {"type": "sleep", "ms": 3000},
                ]
                resp2 = _navigate(final_url, actions=more_actions)
                if resp2 and resp2.get("success"):
                    html2 = resp2.get("html", "")
                    more_urls = _extract_item_links(html2)
                    new_count = len(set(more_urls) - set(all_urls))
                    if new_count > 0:
                        logger.info("Phase 1: Page 2 → %d new URLs", new_count)
                        all_urls.extend(more_urls)
        except Exception as exc:
            logger.debug("Phase 1: pagination check failed: %s", exc)

    # Dedupe, preserve order
    unique_urls = list(dict.fromkeys(all_urls))
    if limit:
        unique_urls = unique_urls[:limit]

    stop_reason = "page_loaded" if unique_urls else "no_next_link"
    logger.info("Phase 1: Total %d unique job URLs (%s)", len(unique_urls), stop_reason)
    return unique_urls, stop_reason


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 2: ITEM EXTRACTION (concurrent)
# ═══════════════════════════════════════════════════════════════════════════════


def _clean_html(html_str: str) -> str:
    """Strip HTML tags and normalize whitespace."""
    if not html_str:
        return ""
    text = re.sub(r"<[^>]+>", " ", html_str)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_detail_field(soup: BeautifulSoup, label: str) -> str:
    """Extract a value from .job-details li.job-detail by its <strong> label."""
    # Code Writer adapted: field extraction per field map
    for li in soup.select(".job-details li.job-detail"):
        strong = li.find("strong")
        if strong and strong.get_text(strip=True).startswith(label):
            value = li.get_text(strip=True)
            strong_text = strong.get_text(strip=True)
            if value.startswith(strong_text):
                value = value[len(strong_text):].strip()
            return value
    return ""


def _error_item(url: str, src_url: str, error: str) -> dict:
    return {
        "url": url,
        "src_url": src_url,
        "status_code": 0,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": f"Error: {error[:200]}",
    }


def _extract_item(item_url: str, src_url: str) -> dict:
    """Phase 2: Extract structured job data from a single detail page.

    Job detail pages are SSR WordPress — browser_service renders them,
    then we parse with CSS selectors per the field map.
    """
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
        "status_code": resp.get("status_code", 200),
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": "",
    }

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return _error_item(item_url, src_url, "HTML parse failed")

    # --- Soft 404 detection ---
    page_title = soup.title.get_text(strip=True).lower() if soup.title else ""
    h1_el = soup.select_one(".job-header h1")
    if h1_el:
        h1_text = h1_el.get_text(strip=True).lower()
        if any(kw in h1_text for kw in ("not found", "unavailable", "discontinued", "no longer available")):
            item["remarks"] = "Soft 404: job not found"
            return item
    if any(kw in page_title for kw in ("not found", "page not found", "404", "error")):
        item["remarks"] = "Soft 404: job not found"
        return item

    # --- Title ---
    # Code Writer adapted: .job-header h1 per field map
    if h1_el:
        item["title"] = h1_el.get_text(strip=True)

    # --- Detail fields via labeled <strong> in li.job-detail ---
    item["location"] = _extract_detail_field(soup, "Location")
    item["category"] = _extract_detail_field(soup, "Category")
    item["contract_duration"] = _extract_detail_field(soup, "Contract Duration")
    item["date_posted"] = _extract_detail_field(soup, "Date Posted")
    item["est_pay"] = _extract_detail_field(soup, "Est. Pay")
    item["job_type"] = _extract_detail_field(soup, "Job Type")
    item["position_id"] = _extract_detail_field(soup, "Position ID")

    # Derive company from site name if not explicitly on page
    item["company"] = SITE_NAME

    # --- Description ---
    # Code Writer adapted: article.mb-5 per field map
    desc_el = soup.select_one("section.block--section-wrapper.mb-5 .wp-block-column article.mb-5")
    if not desc_el:
        desc_el = soup.select_one("article.mb-5")
    if desc_el:
        item["description"] = _clean_html(str(desc_el))

    # --- Apply URL ---
    apply_btn = soup.select_one("section.block--section-wrapper.mb-5 a.btn.btn-primary[href*='/application/']")
    if apply_btn:
        href = apply_btn.get("href", "")
        item["apply_url"] = href if href.startswith("http") else urljoin(SITE_URL, href)

    # --- Breadcrumb ---
    bc_el = soup.select_one(".yoast-breadcrumbs")
    if bc_el:
        item["breadcrumb"] = bc_el.get_text(strip=True)

    return item


def _extract_item_safe(item_url: str, src_url: str) -> dict:
    """Phase 2 wrapper — never raises."""
    try:
        return _extract_item(item_url, src_url)
    except Exception as exc:
        logger.error("Phase 2: unexpected failure on %s: %s", item_url[:80], exc)
        return _error_item(item_url, src_url, str(exc))


# ═══════════════════════════════════════════════════════════════════════════════
# INPUT HELPERS
# ═══════════════════════════════════════════════════════════════════════════════


def _load_urls_from_file(filepath: str) -> list[str]:
    """Load URLs from a JSON file with a 'urls' key."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        urls = data.get("urls", [])
        if isinstance(urls, list):
            return [u for u in urls if isinstance(u, str) and u.startswith("http")]
    except Exception as exc:
        logger.error("Failed to load URLs from %s: %s", filepath, exc)
    return []


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(description=f"{SITE_NAME} Job Scraper")
    parser.add_argument("--query", type=str, help="Search query (maps to /jobs/ listing)")
    parser.add_argument("--category-url", type=str, help="Category/listing URL to crawl")
    parser.add_argument("--listing-url", type=str, help="Listing page URL to paginate")
    parser.add_argument("--sample", action="store_true", help="Scrape first 5 items only")
    parser.add_argument("--limit", type=int, default=None, help="Max items to scrape")
    parser.add_argument("--input", type=str, default=None, help="Path to input URLs JSON file (takes precedence)")
    parser.add_argument("--urls", nargs="+", default=None, help="Job URLs as CLI arguments")
    parser.add_argument("--no-proxy", action="store_true", help="Disable proxy")
    parser.add_argument("--headless", action="store_true", default=True, help="Accepted for CLI compatibility")
    parser.add_argument("--discover-only", action="store_true", help="Run Phase 1 only, skip extraction")
    parser.add_argument("--fresh-discovery", action="store_true", help="Ignore checkpoint, run Phase 1 fresh")
    args = parser.parse_args()

    if args.no_proxy:
        global PROXY_TIER
        PROXY_TIER = "none"

    limit = 5 if args.sample else args.limit
    start_time = time.time()
    discovered_urls: list[str] = []

    # Coverage-gate state
    ran_phase1 = True
    skipped_reason: Optional[str] = None
    aggregate_stop_reason = "no_next_link"
    max_pages_hit = False
    dimensions_iterated = 0
    dimensions_total = len(CATEGORY_URLS) if isinstance(CATEGORY_URLS, list) else 0

    # Determine the listing URL for discovery
    listing_url = args.category_url or args.listing_url or "https://www.amergis.com/jobs/"

    # ── --sample mode: use URLs from input_urls.json ────────────────────────
    if args.sample:
        sample_urls = _load_urls_from_file(_INPUT_FILE)
        # Filter to only job detail URLs
        sample_urls = [u for u in sample_urls if _is_job_url(u)]
        if sample_urls:
            discovered_urls = sample_urls[:5]
            ran_phase1 = False
            skipped_reason = "sample_input_file"
            aggregate_stop_reason = "skipped"
            logger.info("--sample: using %d URLs from input_urls.json", len(discovered_urls))
        else:
            # Fallback: discover then sample
            logger.info("--sample: no job URLs in input_urls.json, running discovery...")

    # ── --input takes precedence over everything ────────────────────────────
    if not discovered_urls and args.input:
        input_urls = _load_urls_from_file(args.input)
        if input_urls:
            discovered_urls = input_urls
            ran_phase1 = False
            skipped_reason = "input_file"
            aggregate_stop_reason = "skipped"
            logger.info("--input: loaded %d URLs from %s", len(discovered_urls), args.input)

    # ── --urls: inline URLs ─────────────────────────────────────────────────
    if not discovered_urls and args.urls:
        discovered_urls = [u for u in args.urls if u.startswith("http")]
        ran_phase1 = False
        skipped_reason = "url_list_mode"
        aggregate_stop_reason = "skipped"
        logger.info("--urls: %d URLs provided via CLI", len(discovered_urls))

    # ── Phase 1: Discovery (default behavior) ───────────────────────────────
    if not discovered_urls:
        # Skip checkpoint if --fresh-discovery
        checkpoint_urls = [] if args.fresh_discovery else _load_checkpoint()
        if checkpoint_urls:
            discovered_urls = checkpoint_urls
            ran_phase1 = False
            skipped_reason = "checkpoint_loaded"
            aggregate_stop_reason = "skipped"
            logger.info("Phase 1: SKIPPED (resumed from checkpoint, %d URLs)", len(discovered_urls))
        else:
            logger.info("Phase 1: Discovering job URLs from %s", listing_url)
            discovered_urls, primary_reason = _discover_urls_via_category(listing_url, MAX_PAGES, limit)
            aggregate_stop_reason = _merge_stop_reason(aggregate_stop_reason, primary_reason)
            max_pages_hit = max_pages_hit or primary_reason == "max_pages_hit"
            _write_checkpoint(discovered_urls)

    # Apply sample/limit caps
    if args.sample and len(discovered_urls) > 5:
        discovered_urls = discovered_urls[:5]
    if args.limit:
        discovered_urls = discovered_urls[:args.limit]

    logger.info("Total URLs to process: %d", len(discovered_urls))

    if not discovered_urls and not args.discover_only:
        logger.warning("No job URLs discovered")
        sys.exit(0)

    # ── Phase 2: Extract data concurrently ──────────────────────────────────
    total = len(discovered_urls)
    items: list[dict] = []

    if args.discover_only:
        logger.info("--discover-only: skipping Phase 2 (%d URLs discovered)", total)
    elif discovered_urls:
        logger.info("Phase 2: Extracting %d jobs with %d workers", total, PHASE2_WORKERS)
        completed = 0
        with ThreadPoolExecutor(max_workers=PHASE2_WORKERS) as pool:
            futures = {
                pool.submit(_extract_item_safe, url, listing_url): url
                for url in discovered_urls
            }
            for future in as_completed(futures):
                url = futures[future]
                completed += 1
                try:
                    item = future.result()
                except Exception as exc:
                    item = _error_item(url, listing_url, str(exc))
                items.append(item)
                status = "ok" if item.get("title") else "skip"
                logger.info("Progress: [%d/%d] (%.1f%%) %s — %s",
                            completed, total, (completed / total) * 100, status, url[:90])

    # ── Output filter ───────────────────────────────────────────────────────
    extra = [f for f in CORE_FILTER_FIELDS if f and f != "title"]
    before = len(items)
    items = [
        it for it in items
        if it.get("title") and (not extra or any(it.get(f) for f in extra))
    ]
    if len(items) != before:
        logger.info("output filter: %d → %d items (dropped %d without core fields)",
                     before, len(items), before - len(items))

    # ── discovery_coverage block ────────────────────────────────────────────
    discovery_coverage = {
        "stop_reason": aggregate_stop_reason,
        "found": len(items),
        "discovered_urls": len(discovered_urls),
        "expected_total": COVERAGE_TARGET_TOTAL,
        "dimensions_iterated": dimensions_iterated,
        "dimensions_total": dimensions_total,
        "max_pages_hit": max_pages_hit,
        "ran_phase1": ran_phase1,
        "skipped_reason": skipped_reason,
    }

    output = {
        "site": {
            "name": SITE_NAME,
            "url": SITE_URL,
            "platform": PLATFORM,
            "scraping_method": "http_navigation",
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        },
        OUTPUT_KEY: items,
        "metadata": {
            "scraping_duration_seconds": round(time.time() - start_time, 1),
            "discovered_urls": len(discovered_urls),
            "extracted_items": len(items),
            "execution_model": "http_navigate",
            "stealth": "cloak" if str(STEALTH).lower() == "cloak" else "none",
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

    logger.info("Done: %d/%d items in %.1fs → %s",
                len([i for i in items if i.get("title")]),
                total, time.time() - start_time, output_filename)


if __name__ == "__main__":
    main()