#!/usr/bin/env python3
"""
LocumTenens.com Job Scraper — Two-Phase Architecture

Phase 1: Discover job URLs by iterating the Specialties <select> on the
         QuickSearch POST form using Playwright, then paginating results.
Phase 2: Extract structured data from each job detail page concurrently
         using requests + BeautifulSoup (pages are server-rendered).

The QuickSearch form requires a Specialty selection and uses anti-forgery
tokens + server-side sessions, so raw requests.post() returns HTTP 500.
Phase 1 MUST use a real browser (Playwright).

Usage:
    python3 scraper_draft.py                          # full discovery + extraction
    python3 scraper_draft.py --sample                  # 5 jobs from input_urls.json
    python3 scraper_draft.py --limit 50                # cap at 50 jobs
    python3 scraper_draft.py --urls URL1 URL2 ...     # specific URLs
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

SITE_NAME = "LocumTenens.com"
SITE_URL = "https://www.locumtenens.com"
PLATFORM = "custom"
SITE_SLUG = "locumtenens-com"
SCRAPING_METHOD = "playwright_navigation"
PROXY_TIER = "none"
OUTPUT_KEY = "jobs"
CONTENT_TYPE = "job_posting"
CURRENCY = "USD"

# Phase 1 — QuickSearch form (browser-based)
QUICK_SEARCH_URL = f"{SITE_URL}/Resources/JobSearch/QuickSearch"
ITEM_CONTAINER_SELECTOR = "li.job-results-item"
ITEM_LINK_SELECTOR = "a.job-link"
JOB_URL_REGEX = re.compile(r"/job-\d+")
JOB_ID_REGEX = re.compile(r"job-(\d+)")
PAGINATION_PARAM = "page"
MAX_PAGES = None  # unlimited — paginate until exhaustion
ROTATE_EVERY = 25  # relaunch browser every N pages to prevent crashes

# Phase 2 — extraction (HTTP)
PHASE2_MAX_WORKERS = 8
REQUEST_TIMEOUT = 15
DELAY_BETWEEN_REQUESTS = 1.0

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Crash-recovery checkpoint
_CHECKPOINT_PATH = os.path.join(SCRIPT_DIR, "discovered_urls_checkpoint.json")

# Thread-local storage for per-thread requests.Session in Phase 2
_thread_local = threading.local()

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
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(SITE_SLUG)


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════


def _create_session() -> requests.Session:
    """Create a fresh requests.Session with browser-like headers."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
    })
    return s


def _get_thread_session() -> requests.Session:
    """Return the thread-local Session (each worker thread gets its own)."""
    if not hasattr(_thread_local, "session"):
        _thread_local.session = _create_session()
    return _thread_local.session


# ── Checkpoint (resume after crash) ────────────────────────────────────────


def _write_checkpoint(urls: list[str], src_urls: list[str]) -> None:
    """Persist discovered URLs so a crashed run can resume."""
    try:
        with open(_CHECKPOINT_PATH, "w") as f:
            json.dump({
                "urls": urls,
                "src_urls": src_urls,
                "count": len(urls),
                "ts": time.time(),
            }, f)
    except Exception as exc:
        logger.warning("Checkpoint write failed: %s", exc)


def _load_checkpoint() -> tuple[list[str], list[str]]:
    """Load URLs from a previous run's checkpoint (if any)."""
    try:
        if os.path.isfile(_CHECKPOINT_PATH):
            with open(_CHECKPOINT_PATH) as f:
                data = json.load(f)
            urls = data.get("urls", [])
            if urls:
                logger.info(
                    "Checkpoint: RESUMING with %d URLs", len(urls)
                )
                return urls, data.get("src_urls", urls[:])
    except Exception as exc:
        logger.warning("Checkpoint load failed: %s", exc)
    return [], []


def _clear_checkpoint() -> None:
    """Remove checkpoint after successful completion."""
    try:
        if os.path.isfile(_CHECKPOINT_PATH):
            os.remove(_CHECKPOINT_PATH)
    except OSError:
        pass


def _extract_job_id(url: str) -> str:
    """Pull the numeric job id from a URL for deduplication."""
    m = JOB_ID_REGEX.search(url)
    return m.group(1) if m else url


def _browser_alive(page) -> bool:
    """Probe whether the browser page is still responsive."""
    try:
        page.evaluate("1")
        return True
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 1 — URL DISCOVERY (Playwright)
# ═══════════════════════════════════════════════════════════════════════════════


def _extract_job_links_from_page(page) -> list[str]:
    """Extract job links from the current results page using Playwright."""
    links: list[str] = []
    try:
        containers = page.query_selector_all(ITEM_CONTAINER_SELECTOR)
        for container in containers:
            link_el = container.query_selector(ITEM_LINK_SELECTOR)
            if link_el:
                href = link_el.get_attribute("href") or ""
                if href:
                    if href.startswith("/"):
                        href = SITE_URL.rstrip("/") + href
                    links.append(href)
    except Exception as exc:
        logger.warning("Phase 1: Error extracting item links: %s", exc)

    # Fallback: bare link selector
    if not links:
        try:
            link_els = page.query_selector_all(ITEM_LINK_SELECTOR)
            for link_el in link_els:
                href = link_el.get_attribute("href") or ""
                if href:
                    if href.startswith("/"):
                        href = SITE_URL.rstrip("/") + href
                    links.append(href)
        except Exception as exc:
            logger.warning("Phase 1: Fallback link extraction failed: %s", exc)

    return links


def _get_next_page_url(page, next_page_num: int) -> Optional[str]:
    """Determine the URL for the next page of results.

    Uses the template's runtime fallback chain: next-button selectors
    first, then ?page=N param fallback.
    """
    # Try common next-button selectors
    for sel in (
        'a[rel="next"]',
        'a.next',
        'li.next a',
        '[aria-label*="next" i]',
        'a:has-text("Next")',
        'button:has-text("Next")',
        'a:has-text(">")',
        '.pagination a.active + a',
    ):
        try:
            btn = page.query_selector(sel)
            if btn:
                href = btn.get_attribute("href") or ""
                if href:
                    if href.startswith("http"):
                        return href
                    return SITE_URL.rstrip("/") + (
                        href if href.startswith("/") else "/" + href
                    )
                # SPA-style: click and return new URL
                try:
                    btn.click()
                    page.wait_for_load_state("domcontentloaded")
                    time.sleep(8)
                    if page.url:
                        return page.url
                except Exception:
                    pass
        except Exception:
            pass

    # Param-based fallback: ?page=N
    current_url = page.url
    sep = "&" if "?" in current_url else "?"
    return f"{current_url}{sep}{PAGINATION_PARAM}={next_page_num}"


def _paginate_results_page(page, base_url: str, max_pages: Optional[int] = None) -> list[str]:
    """Follow pagination on a results page and return all deduplicated job links.

    Starts with the current page (base_url) and follows next pages.
    """
    all_links: list[str] = []
    seen: set[str] = set()

    # First page: already loaded in the browser
    current_links = _extract_job_links_from_page(page)
    for lnk in current_links:
        jid = _extract_job_id(lnk)
        if jid not in seen:
            seen.add(jid)
            all_links.append(lnk)

    for page_num in range(2, (max_pages or 10_000) + 1):
        next_url = _get_next_page_url(page, page_num)
        if not next_url:
            break

        try:
            page.goto(next_url, timeout=30000)
            page.wait_for_load_state("domcontentloaded")
            time.sleep(5)
        except Exception as exc:
            logger.warning(
                "Phase 1: Pagination page %d failed: %s", page_num, exc
            )
            break

        new_links = _extract_job_links_from_page(page)
        new_count = 0
        for lnk in new_links:
            jid = _extract_job_id(lnk)
            if jid not in seen:
                seen.add(jid)
                all_links.append(lnk)
                new_count += 1

        logger.debug(
            "Phase 1:   page %d → %d raw, %d new (running %d)",
            page_num, len(new_links), new_count, len(all_links),
        )

        if new_count == 0:
            logger.info("Phase 1: No new items on page %d, stopping", page_num)
            break

    return all_links


def discover_urls_via_playwright(
    page,
    max_specialties: Optional[int] = None,
    limit: Optional[int] = None,
    max_pages: Optional[int] = None,
) -> tuple[list[str], list[str]]:
    """Iterate all specialties in the QuickSearch form using Playwright,
    submit each, paginate results, and collect every unique job URL.

    Returns (urls, src_urls) — parallel lists.
    """
    logger.info("Phase 1: Loading QuickSearch page …")
    try:
        page.goto(QUICK_SEARCH_URL, timeout=30000)
        page.wait_for_load_state("domcontentloaded")
        time.sleep(8)
    except Exception as exc:
        logger.error("Phase 1: Failed to load QuickSearch: %s", exc)
        return [], []

    # Parse specialty options from the <select>
    specialty_options: list[dict[str, str]] = []
    try:
        options = page.eval_on_selector_all(
            "select[name='Specialties'] option",
            "els => els.map(e => ({ value: e.value, text: e.textContent.trim() }))",
        )
        for opt in options:
            val = opt.get("value", "").strip()
            text = opt.get("text", "").strip()
            if val:  # skip empty placeholder
                specialty_options.append({"value": val, "text": text})
    except Exception as exc:
        logger.error("Phase 1: Failed to parse specialty options: %s", exc)
        return [], []

    if not specialty_options:
        logger.error("Phase 1: No specialty options found — aborting")
        return [], []

    if max_specialties:
        specialty_options = specialty_options[:max_specialties]

    total_specs = len(specialty_options)
    all_urls: list[str] = []
    all_src_urls: list[str] = []
    seen_ids: set[str] = set()
    checkpoint_interval = 10

    for idx, spec in enumerate(specialty_options, 1):
        logger.info(
            "Phase 1: [%d/%d] Specialty: %s (val=%s)",
            idx, total_specs, spec["text"], spec["value"],
        )

        # Navigate to the QuickSearch form fresh for each specialty
        try:
            page.goto(QUICK_SEARCH_URL, timeout=30000)
            page.wait_for_load_state("domcontentloaded")
            time.sleep(5)
        except Exception as exc:
            logger.warning(
                "Phase 1: Failed to reload QuickSearch for %s: %s",
                spec["text"], exc,
            )
            time.sleep(DELAY_BETWEEN_REQUESTS)
            continue

        # Select the specialty
        try:
            page.select_option("select[name='Specialties']", spec["value"])
            time.sleep(1)

            # Locations: leave at default (Any / first option)
            # Disciplines: leave at default

            # Submit the form — try input[type='submit'] first, then
            # button[type='submit'], then form.requestSubmit()
            submitted = False
            for submit_sel in [
                "form input[type='submit']",
                "form button[type='submit']",
                "form input[value='Search']",
                "form button:has-text('Search')",
            ]:
                try:
                    btn = page.query_selector(submit_sel)
                    if btn:
                        btn.click()
                        submitted = True
                        break
                except Exception:
                    pass

            if not submitted:
                try:
                    page.evaluate("document.querySelector('form').requestSubmit()")
                    submitted = True
                except Exception:
                    pass

            if not submitted:
                logger.warning(
                    "Phase 1: Could not submit form for %s", spec["text"]
                )
                continue

            # Wait for navigation to results page
            page.wait_for_load_state("domcontentloaded")
            time.sleep(8)

            current_url = page.url
            logger.info(
                "Phase 1: Landed on %s", current_url[:100],
            )

            # Check if we're on a results page
            if "SearchResults" not in current_url and "sId=" not in current_url:
                logger.warning(
                    "Phase 1: Unexpected URL after submit: %s", current_url,
                )
                time.sleep(DELAY_BETWEEN_REQUESTS)
                continue

        except Exception as exc:
            logger.warning(
                "Phase 1: Form interaction failed for %s: %s",
                spec["text"], exc,
            )
            time.sleep(DELAY_BETWEEN_REQUESTS)
            continue

        # Paginate results from this specialty
        links = _paginate_results_page(page, current_url, max_pages)

        new_count = 0
        for lnk in links:
            jid = _extract_job_id(lnk)
            if jid not in seen_ids:
                seen_ids.add(jid)
                all_urls.append(lnk)
                all_src_urls.append(current_url)
                new_count += 1

        logger.info(
            "Phase 1: [%d/%d] %s → %d jobs (%d new, cumulative %d)",
            idx, total_specs, spec["text"], len(links), new_count, len(all_urls),
        )

        if limit and len(all_urls) >= limit:
            logger.info(
                "Phase 1: Reached limit=%d — stopping discovery", limit
            )
            all_urls = all_urls[:limit]
            all_src_urls = all_src_urls[:limit]
            break

        # Incremental checkpoint
        if idx % checkpoint_interval == 0:
            _write_checkpoint(all_urls, all_src_urls)

        time.sleep(DELAY_BETWEEN_REQUESTS)

    logger.info(
        "Phase 1 complete — %d unique job URLs discovered across %d specialties",
        len(all_urls), total_specs,
    )
    return all_urls, all_src_urls


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 2 — ITEM EXTRACTION (requests + BeautifulSoup)
# ═══════════════════════════════════════════════════════════════════════════════


def _extract_job_data(url: str, src_url: str) -> dict:
    """Fetch a single job detail page and extract all fields.

    Uses a robust multi-strategy approach for title extraction since the
    exact job posting page DOM may differ from the content-page analysis.
    """
    session = _get_thread_session()

    item: dict = {
        "url": url,
        "src_url": src_url,
        "status_code": 0,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": "",
        "title": "",
        "description": "",
        "body_content": "",
        "category": "",
        "hero_image": "",
        "paragraphs": [],
        "headings": [],
        "lists": [],
    }

    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        item["status_code"] = resp.status_code
        resp.raise_for_status()
    except requests.RequestException as exc:
        item["remarks"] = f"Error fetching page: {exc}"
        logger.warning("Phase 2: Fetch failed for %s: %s", url[:80], exc)
        return item

    try:
        soup = BeautifulSoup(resp.text, "html.parser")

        # ── Debug: log the page title for diagnostics ─────────────
        page_title = soup.title.get_text(strip=True) if soup.title else "(no title tag)"
        logger.debug("Phase 2: page <title> = '%s' for %s", page_title, url[:80])

        # ── Soft-404 detection ────────────────────────────────────
        # Only use strong, specific indicators — avoid overly broad words
        # like "error" which appear in normal page titles.
        h1_el = soup.find("h1")
        h1_text = h1_el.get_text(strip=True) if h1_el else ""
        soft404_phrases = [
            "not found",
            "page not found",
            "no longer available",
            "has been filled",
            "position has been filled",
            "job no longer available",
            "this job is no longer",
            "unavailable",
            "discontinued",
        ]
        combined_text = f"{page_title} {h1_text}".lower()
        for phrase in soft404_phrases:
            if phrase in combined_text:
                item["remarks"] = f"Soft 404: {phrase}"
                logger.info("Phase 2: Soft 404 detected ('%s') on %s", phrase, url[:80])
                return item

        # Check for redirect to a completely different domain or non-job page
        final_url = resp.url or url
        if final_url.rstrip("/") != url.rstrip("/"):
            final_path = urlparse(final_url).path
            if not JOB_URL_REGEX.search(final_path):
                # Only flag as soft-404 if redirected to a completely different
                # section of the site (e.g., homepage, search page)
                if len(final_path) < 10 or "/search" in final_path.lower():
                    item["remarks"] = f"Soft 404: redirected to {final_url[:100]}"
                    logger.info("Phase 2: Redirect to non-job page: %s", final_url[:100])
                    return item
        item["url"] = final_url

        # ── Title — multi-strategy extraction ─────────────────────
        # Strategy 1: Specific job page selectors (may exist on some pages)
        title_text = ""
        title_el = (
            soup.select_one("h1.interior-page-title")
            or soup.select_one("h1.job-title")
            or soup.select_one("h1.page-title")
            or soup.select_one("h1.heading")
            or soup.select_one("h1")
        )
        if title_el:
            title_text = title_el.get_text(strip=True)

        # Strategy 2: If h1 is empty/too short, try og:title meta tag
        if not title_text or len(title_text) < 3:
            og_title = soup.select_one("meta[property='og:title']")
            if og_title and og_title.get("content"):
                title_text = og_title["content"].strip()
                # Strip common suffixes like " | LocumTenens.com"
                title_text = re.split(r"\s*[\|\-–—]\s*LocumTenens", title_text, 1)[0].strip()

        # Strategy 3: Use the HTML <title> tag as last resort
        if not title_text or len(title_text) < 3:
            raw_title = page_title
            # Clean up the page title: remove site name suffix
            title_text = re.split(
                r"\s*[\|\-–—]\s*", raw_title, 1
            )[0].strip()
            # Also try splitting on " - LocumTenens" specifically
            if not title_text or len(title_text) < 3:
                title_text = re.split(
                    r"\s*[\|\-–—]\s*LocumTenens", raw_title, 1
                )[0].strip()

        # Strategy 4: Try any prominent heading or the first large text
        if not title_text or len(title_text) < 3:
            for selector in [".title", ".job-heading", ".page-heading", "[class*='title']"]:
                el = soup.select_one(selector)
                if el:
                    txt = el.get_text(strip=True)
                    if txt and len(txt) >= 3 and len(txt) < 200:
                        title_text = txt
                        break

        item["title"] = title_text

        if not item["title"]:
            logger.warning(
                "Phase 2: No title found for %s — page_title='%s', h1='%s'",
                url[:80], page_title, h1_text,
            )

        # ── URL (og:url) — use canonical if available ──────────
        url_meta = soup.select_one("meta[property='og:url']")
        canonical = soup.select_one("link[rel='canonical']")
        if url_meta and url_meta.get("content"):
            item["url"] = url_meta["content"]
        elif canonical and canonical.get("href"):
            item["url"] = canonical["href"]

        # ── Description (meta description / og:description) ──────
        desc_el = (
            soup.select_one("meta[name='description']")
            or soup.select_one("meta[property='og:description']")
        )
        item["description"] = (
            desc_el.get("content", "").strip() if desc_el else ""
        )

        # ── Category ──────────────────────────────────────────────
        cat_el = (
            soup.select_one("h6.cds-overline")
            or soup.select_one(".job-category")
            or soup.select_one(".category-label")
            or soup.select_one("[itemtype='http://schema.org/BreadcrumbList'] li:last-child a")
            or soup.select_one(".breadcrumb li:last-child a")
        )
        item["category"] = cat_el.get_text(strip=True) if cat_el else ""

        # ── Body content ──────────────────────────────────────────
        body_el = (
            soup.select_one(".interior-page-content .umb-grid")
            or soup.select_one(".interior-page-content")
            or soup.select_one("main")
            or soup.select_one(".job-details")
            or soup.select_one(".job-description")
            or soup.select_one("[role='main']")
            or soup.select_one(".content")
        )
        if body_el:
            item["body_content"] = body_el.get_text(
                separator=" ", strip=True
            )

        # ── Hero image ───────────────────────────────────────────
        hero_img = ""
        hero_section = soup.select_one("section.interior-page-header-img")
        if hero_section:
            style = hero_section.get("style", "")
            m = re.search(r"url\(['\"]?(.*?)['\"]?\)", style)
            if m:
                hero_img = m.group(1)
        if not hero_img:
            og_img = soup.select_one("meta[property='og:image']")
            hero_img = og_img.get("content", "") if og_img else ""
        item["hero_image"] = hero_img

        # ── Paragraphs (text > 20 chars) ────────────────────────
        paragraphs = []
        p_selectors = [
            ".interior-page-content p",
            "main p",
            ".job-details p",
            ".job-description p",
            "[role='main'] p",
            "article p",
        ]
        for sel in p_selectors:
            for p in soup.select(sel):
                txt = p.get_text(strip=True)
                if len(txt) > 20 and txt not in paragraphs:
                    paragraphs.append(txt)
            if paragraphs:
                break
        if not paragraphs:
            for p in soup.find_all("p"):
                txt = p.get_text(strip=True)
                if len(txt) > 20 and txt not in paragraphs:
                    paragraphs.append(txt)
        item["paragraphs"] = paragraphs

        # ── Headings ──────────────────────────────────────────────
        headings = []
        h_selectors = [
            ".interior-page-content",
            "main",
            ".job-details",
            ".job-description",
            "[role='main']",
            "article",
        ]
        for scope in h_selectors:
            scoped = soup.select_one(scope)
            if scoped:
                for h in scoped.find_all(["h2", "h3", "h4"]):
                    level = int(h.name[1])
                    txt = h.get_text(strip=True)
                    if txt:
                        headings.append({"level": level, "text": txt})
                if headings:
                    break
        if not headings:
            for h in soup.find_all(["h2", "h3", "h4"]):
                level = int(h.name[1])
                txt = h.get_text(strip=True)
                if txt:
                    headings.append({"level": level, "text": txt})
        item["headings"] = headings

        # ── Lists ─────────────────────────────────────────────────
        lists = []
        for scope_sel in [
            ".interior-page-content",
            "main",
            ".job-details",
            ".job-description",
            "[role='main']",
        ]:
            scoped = soup.select_one(scope_sel)
            if scoped:
                for lst in scoped.find_all(["ul", "ol"]):
                    list_type = lst.name.lower()
                    list_items = [
                        li.get_text(strip=True)
                        for li in lst.find_all("li")
                        if li.get_text(strip=True)
                    ]
                    if list_items:
                        lists.append({"type": list_type, "items": list_items})
                if lists:
                    break
        item["lists"] = lists

    except Exception as exc:
        item["remarks"] = f"Extraction error: {exc}"
        logger.warning("Phase 2: Extraction error for %s: %s", url[:80], exc)

    return item


def extract_all_concurrent(
    urls: list[str],
    src_urls: list[str],
    max_workers: int = PHASE2_MAX_WORKERS,
) -> list[dict]:
    """Extract data from all discovered URLs using a thread pool."""
    logger.info(
        "Phase 2: Extracting %d jobs with %d workers",
        len(urls), max_workers,
    )

    results: list[Optional[dict]] = [None] * len(urls)
    success_count = 0
    fail_count = 0

    def _worker(index: int, url: str, src: str) -> tuple[int, dict]:
        return index, _extract_job_data(url, src)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_worker, i, url, src): i
            for i, (url, src) in enumerate(zip(urls, src_urls))
        }
        completed = 0
        for future in as_completed(futures):
            completed += 1
            idx = futures[future]
            try:
                _, item = future.result()
                results[idx] = item
                if item.get("title"):
                    success_count += 1
                else:
                    fail_count += 1
            except Exception as exc:
                results[idx] = {
                    "url": urls[idx],
                    "src_url": src_urls[idx],
                    "status_code": 0,
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                    "remarks": f"Worker error: {exc}",
                }
                fail_count += 1

            if completed % 25 == 0 or completed == len(urls):
                pct = (completed / len(urls)) * 100
                logger.info(
                    "Progress: [%d/%d] (%.1f%%)", completed, len(urls), pct
                )

    logger.info(
        "Phase 2 complete — success: %d, failed: %d",
        success_count, fail_count,
    )
    return [r for r in results if r is not None]


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════


def main() -> None:
    parser = argparse.ArgumentParser(
        description=f"{SITE_NAME} Job Scraper"
    )
    parser.add_argument(
        "--input", type=str, help="Path to input URLs JSON file"
    )
    parser.add_argument(
        "--urls", nargs="+", help="Job URLs passed on the CLI"
    )
    parser.add_argument(
        "--sample", action="store_true", help="Scrape first 5 items only"
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Max items to scrape"
    )
    parser.add_argument(
        "--no-proxy", action="store_true", default=True,
        help="Disable proxy (default: no proxy)",
    )
    parser.add_argument(
        "--query", type=str, default=None,
        help="Search query (default: iterate all specialties)",
    )
    parser.add_argument(
        "--headless", action="store_true", default=True,
        help="Headless mode (default: True)",
    )
    args = parser.parse_args()

    limit = 5 if args.sample else args.limit
    start_time = time.time()

    logger.info("=" * 80)
    logger.info("Starting scraper for %s", SITE_NAME)
    logger.info("Scraping method: %s", SCRAPING_METHOD)
    logger.info("=" * 80)

    discovered_urls: list[str] = []
    src_urls: list[str] = []

    # ── --urls mode: extract specific URLs directly ──────────────
    if args.urls:
        discovered_urls = list(args.urls)
        src_urls = list(args.urls)
        logger.info("Using %d URLs from --urls flag", len(discovered_urls))

    # ── --input mode ──────────────────────────────────────────────
    elif args.input:
        try:
            with open(args.input) as f:
                data = json.load(f)
            discovered_urls = data.get("urls", [])
            src_urls = list(discovered_urls)
            logger.info(
                "Using %d URLs from --input %s",
                len(discovered_urls), args.input,
            )
        except Exception as exc:
            logger.error("Failed to read --input file: %s", exc)
            sys.exit(1)

    # ── --sample mode: skip Phase 1, use input_urls.json ─────────
    elif args.sample:
        input_path = os.path.join(SCRIPT_DIR, "input_urls.json")
        if not os.path.isfile(input_path):
            logger.error(
                "--sample requires input_urls.json but it does not exist at %s",
                input_path,
            )
            sys.exit(1)
        try:
            with open(input_path) as f:
                data = json.load(f)
            discovered_urls = data.get("urls", [])
            src_urls = list(discovered_urls)
            logger.info(
                "Sample mode: using %d URLs from input_urls.json",
                len(discovered_urls),
            )
        except Exception as exc:
            logger.error("Failed to read input_urls.json: %s", exc)
            sys.exit(1)

    # ── Default: Phase 1 discovery via Playwright ─────────────────
    else:
        ck_urls, ck_src = _load_checkpoint()
        if ck_urls:
            discovered_urls = ck_urls
            src_urls = ck_src
            logger.info(
                "Phase 1: SKIPPED — resumed from checkpoint with %d URLs",
                len(ck_urls),
            )
        else:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=args.headless)
                context = browser.new_context(
                    viewport={"width": 1920, "height": 1080},
                    user_agent=DEFAULT_USER_AGENT,
                )
                page = context.new_page()

                discovered_urls, src_urls = discover_urls_via_playwright(
                    page,
                    max_specialties=None,
                    limit=limit,
                    max_pages=MAX_PAGES,
                )
                _write_checkpoint(discovered_urls, src_urls)

                browser.close()

    # ── Guard: nothing discovered ──────────────────────────────────
    if not discovered_urls:
        logger.warning("No job URLs discovered — exiting")
        sys.exit(0)

    # Apply limit (applies after discovery)
    if limit:
        discovered_urls = discovered_urls[:limit]
        src_urls = src_urls[:limit]

    total_products = len(discovered_urls)
    logger.info("Total products: %d", total_products)

    # ── Phase 2: Extract data concurrently ────────────────────────
    items = extract_all_concurrent(discovered_urls, src_urls)

    # ── Output filter: keep only items with a title ────────────────
    before = len(items)
    items = [it for it in items if it.get("title")]
    dropped = before - len(items)
    if dropped:
        logger.info(
            "Output filter: %d → %d items (dropped %d without title)",
            before, len(items), dropped,
        )

    # ── Write output file ──────────────────────────────────────────
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
            "rate_limit_delay": DELAY_BETWEEN_REQUESTS,
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

    # Cleanup checkpoint on successful completion
    _clear_checkpoint()

    success = len([i for i in items if i.get("title")])
    logger.info("=" * 80)
    logger.info("EXTRACTION COMPLETE")
    logger.info(
        "Total: %d, Success: %d, Failed: %d",
        total_products, success, dropped,
    )
    logger.info("Output: %s", output_filename)
    logger.info("Duration: %.1fs", time.time() - start_time)
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
