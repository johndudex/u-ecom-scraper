#!/usr/bin/env python3
"""
LocumTenens.com Job Scraper — Two-Phase Navigation Architecture

Phase 1 (Playwright): Navigate the QuickSearch form, iterate ALL specialties with
  Location=Alabama, submit each, apply JobAge=7 (last 7 days), paginate results,
  and collect unique job URLs.
Phase 2 (HTTP requests): Fetch each discovered job-detail page, extract structured
  data from JSON-LD + CSS selectors, and write output JSON.

Usage:
    python3 scraper_draft.py                          # full extraction (default)
    python3 scraper_draft.py --sample                 # first 5 items only
    python3 scraper_draft.py --limit 20               # max 20 items
    python3 scraper_draft.py --no-proxy                # no proxy (default for this site)
    python3 scraper_draft.py --input urls.json         # scrape pre-provided URLs
    python3 scraper_draft.py --urls URL1 URL2 ...      # scrape specific URLs
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
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
PLATFORM = "custom ASP.NET MVC"
SITE_SLUG = "locumtenens-com"
CONTENT_TYPE = "job_posting"
OUTPUT_KEY = "jobs"
CURRENCY = "USD"

# Phase 1: Form interaction
QUICKSEARCH_URL = f"{SITE_URL}/Resources/JobSearch/QuickSearch"
DISCIPLINES_SELECTOR = 'select[name="Disciplines"]'
SPECIALTIES_SELECTOR = 'select[name="Specialties"]'
LOCATIONS_SELECTOR = 'select[name="Locations"]'
SUBMIT_SELECTOR = 'input[type="submit"]'
RESULTS_PAGE_PARAM = "pgNum"
RESULTS_PER_PAGE = 25
MAX_PAGES = 50

# Phase 1: Item extraction from results
ITEM_CONTAINER_SELECTOR = ".job-results-list"
ITEM_LINK_SELECTOR = 'a[href*="/job-"]'
TOTAL_COUNT_SELECTOR = "span.cds-text-midnight.cds-text-fw-bold"
TOTAL_COUNT_PATTERN = r"\d+\s*-\s*\d+\s+of\s+(\d+)"

# Phase 1: Filter form on results page
FILTER_JOB_AGE_SELECTOR = 'select[name="JobAge"]'
FILTER_SUBMIT_SELECTOR = 'button[type="submit"]'

# Phase 2: HTTP extraction
SCRAPING_METHOD = "http_requests"
PROXY_TIER = "none"
DELAY_BETWEEN_REQUESTS = 1.0
PAGE_LOAD_TIMEOUT = 30000

# Content filter for job_posting output
CORE_FILTER_FIELDS = ["company", "location"]

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, f"logs/{SITE_SLUG}.log")

# Default search query (used as label in output metadata)
DEFAULT_QUERY = "all jobs posted in the last 7 days in Alabama"

# Job URL pattern: /{specialty}-jobs/{role}/{state}/job-{id}
JOB_URL_REGEX = re.compile(
    r"https?://(?:www\.)?locumtenens\.com/[a-z0-9-]+-jobs/[a-z0-9-]+/[a-z0-9-]+/job-\d+",
    re.IGNORECASE,
)

# ═══════════════════════════════════════════════════════════════════════════════
# HTML STRIPPER
# ═══════════════════════════════════════════════════════════════════════════════


class _HTMLStripper(HTMLParser):
    """Minimal HTML-to-text stripper."""

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts).strip()


def strip_html(html: str) -> str:
    s = _HTMLStripper()
    s.feed(html)
    return s.get_text()


# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════════════

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
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
# PHASE 1: URL DISCOVERY (Playwright — browser-driven form interaction)
# ═══════════════════════════════════════════════════════════════════════════════


def _is_job_url(href: str) -> bool:
    """Check if a URL looks like a job detail page."""
    if not href:
        return False
    # Normalize relative paths
    if href.startswith("/"):
        href = SITE_URL.rstrip("/") + href
    return bool(JOB_URL_REGEX.match(href))


def _extract_job_links_from_page(page) -> list[str]:
    """Extract job links from a search results page."""
    links: list[str] = []
    seen: set[str] = set()

    # Primary: use the known container + link selector
    try:
        containers = page.query_selector_all(ITEM_CONTAINER_SELECTOR)
        for container in containers:
            link_els = container.query_selector_all(ITEM_LINK_SELECTOR)
            for el in link_els:
                href = el.get_attribute("href") or ""
                if href:
                    if href.startswith("/"):
                        href = SITE_URL.rstrip("/") + href
                    if _is_job_url(href) and href not in seen:
                        links.append(href)
                        seen.add(href)
    except Exception as exc:
        logger.warning("Phase 1: container extraction failed: %s", exc)

    # Fallback: scan all anchors on the page
    if not links:
        try:
            all_hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
            for h in all_hrefs:
                if _is_job_url(h) and h not in seen:
                    links.append(h)
                    seen.add(h)
        except Exception as exc:
            logger.warning("Phase 1: fallback link extraction failed: %s", exc)

    return links


def _submit_search_form(page, location: str = "AL", specialty_value: Optional[str] = None) -> Optional[str]:
    """Fill and submit the QuickSearch form. Returns the resulting URL or None."""
    try:
        page.goto(QUICKSEARCH_URL, timeout=PAGE_LOAD_TIMEOUT)
        page.wait_for_load_state("domcontentloaded")
        time.sleep(3)

        # Wait for selects to be ready
        page.wait_for_selector(DISCIPLINES_SELECTOR, timeout=10000)

        # Reset Discipline to "Any" (value="0")
        page.select_option(DISCIPLINES_SELECTOR, "0")
        time.sleep(0.5)

        # Set Location to Alabama
        page.select_option(LOCATIONS_SELECTOR, location)
        time.sleep(0.5)

        # Set Specialty if provided
        if specialty_value:
            page.select_option(SPECIALTIES_SELECTOR, specialty_value)
            time.sleep(0.5)

        # Click submit button (try input[type='submit'] first, then fallback chain)
        submitted = False
        for sel in [
            'input[type="submit"]',
            'button[type="submit"]',
            "form button",
        ]:
            try:
                btn = page.query_selector(sel)
                if btn:
                    btn.click()
                    submitted = True
                    break
            except Exception:
                continue

        if not submitted:
            # Last resort: form.requestSubmit()
            try:
                page.evaluate("document.querySelector('form')?.requestSubmit()")
                submitted = True
            except Exception:
                pass

        if not submitted:
            logger.warning("Phase 1: Could not submit search form")
            return None

        page.wait_for_load_state("domcontentloaded")
        time.sleep(5)

        # The form POST redirects to /Resources/JobSearch/SearchResults?sId=XXXX
        result_url = page.url
        logger.info("Phase 1: Search submitted → %s", result_url)

        # Verify we landed on a results page
        if "SearchResults" in result_url or "sId=" in result_url:
            return result_url

        return None

    except Exception as exc:
        logger.error("Phase 1: Form submission error: %s", exc)
        return None


def _apply_date_filter(page, job_age: str = "7") -> bool:
    """Apply the JobAge filter on the results page and re-submit."""
    try:
        # Wait for the filter form to be present
        page.wait_for_selector(FILTER_JOB_AGE_SELECTOR, timeout=10000)
        time.sleep(1)

        page.select_option(FILTER_JOB_AGE_SELECTOR, job_age)
        time.sleep(0.5)

        # Submit the filter form
        submitted = False
        for sel in [
            'button[type="submit"]',
            'input[type="submit"]',
            "form button",
        ]:
            try:
                btn = page.query_selector(sel)
                if btn:
                    btn.click()
                    submitted = True
                    break
            except Exception:
                continue

        if not submitted:
            logger.warning("Phase 1: Could not submit date filter")
            return False

        page.wait_for_load_state("domcontentloaded")
        time.sleep(5)
        logger.info("Phase 1: Date filter applied (JobAge=%s) → %s", job_age, page.url)
        return True

    except Exception as exc:
        logger.warning("Phase 1: Date filter error: %s", exc)
        return False


def _paginate_results(page, base_results_url: str, limit: Optional[int]) -> list[str]:
    """Paginate through search results collecting all job URLs."""
    all_urls: list[str] = []
    seen: set[str] = set()
    current_page = 1

    # Extract links from the current page (already loaded)
    new_links = _extract_job_links_from_page(page)
    for link in new_links:
        if link not in seen:
            all_urls.append(link)
            seen.add(link)

    logger.info("Phase 1: Page 1 → %d items (total: %d)", len(new_links), len(all_urls))

    while current_page < MAX_PAGES:
        if limit and len(all_urls) >= limit:
            logger.info("Phase 1: Reached limit=%d", limit)
            break

        # Build next page URL
        current_page += 1
        separator = "&" if "?" in base_results_url else "?"
        next_url = f"{base_results_url}{separator}{RESULTS_PAGE_PARAM}={current_page}"

        try:
            page.goto(next_url, timeout=PAGE_LOAD_TIMEOUT)
            page.wait_for_load_state("domcontentloaded")
            time.sleep(3)
        except Exception as exc:
            logger.warning("Phase 1: Failed to load page %d: %s", current_page, exc)
            break

        new_links = _extract_job_links_from_page(page)
        new_count = 0
        for link in new_links:
            if link not in seen:
                all_urls.append(link)
                seen.add(link)
                new_count += 1

        logger.info(
            "Phase 1: Page %d → %d items (%d new, total: %d)",
            current_page,
            len(new_links),
            new_count,
            len(all_urls),
        )

        # No new items → last page
        if new_count == 0:
            logger.info("Phase 1: No new items on page %d, stopping pagination", current_page)
            break

    return all_urls


def _discover_all_job_urls(page, limit: Optional[int] = None) -> list[str]:
    """Discover all job URLs by iterating specialties with Location=Alabama, JobAge=7 days.

    The search form REQUIRES a Specialty selection to submit. To cover ALL categories,
    we iterate every option in the Specialties <select> dropdown, submit once per
    specialty, apply the 7-day date filter, paginate, and deduplicate URLs.
    """
    all_urls: list[str] = []
    seen: set[str] = set()

    try:
        page.goto(QUICKSEARCH_URL, timeout=PAGE_LOAD_TIMEOUT)
        page.wait_for_load_state("domcontentloaded")
        time.sleep(3)
        page.wait_for_selector(SPECIALTIES_SELECTOR, timeout=10000)

        # Get all specialty options
        specialty_options = page.eval_on_selector(
            SPECIALTIES_SELECTOR,
            """sel => {
                const opts = [];
                for (const o of sel.options) {
                    if (o.value) opts.push({value: o.value, text: o.text});
                }
                return opts;
            }""",
        )
        logger.info("Phase 1: Found %d specialty options to iterate", len(specialty_options))
    except Exception as exc:
        logger.error("Phase 1: Failed to load QuickSearch page: %s", exc)
        return all_urls

    for idx, opt in enumerate(specialty_options):
        if limit and len(all_urls) >= limit:
            break

        spec_value = opt["value"]
        spec_text = opt["text"]
        logger.info(
            "Phase 1: [%d/%d] Submitting search: Specialty='%s'",
            idx + 1,
            len(specialty_options),
            spec_text,
        )

        results_url = _submit_search_form(page, location="AL", specialty_value=spec_value)
        if not results_url:
            logger.info("  → No results URL (specialty may have no jobs)")
            continue

        # Check if results page actually has results
        page_links = _extract_job_links_from_page(page)
        if not page_links:
            logger.info("  → No job links on results page, skipping specialty")
            continue

        # Apply 7-day date filter
        _apply_date_filter(page, job_age="7")

        # Paginate and collect URLs
        # Use the page.url after filter as base (filter re-submits and may change sId)
        filtered_url = page.url
        spec_urls = _paginate_results(page, filtered_url, limit)

        new_count = 0
        for url in spec_urls:
            if url not in seen:
                all_urls.append(url)
                seen.add(url)
                new_count += 1

        logger.info("  → Specialty '%s': %d URLs (%d new, cumulative: %d)", spec_text, len(spec_urls), new_count, len(all_urls))

    logger.info("Phase 1: Discovery complete — %d unique job URLs", len(all_urls))
    return all_urls


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 2: HTTP-BASED EXTRACTION (requests + BeautifulSoup + JSON-LD)
# ═══════════════════════════════════════════════════════════════════════════════


def _make_session() -> requests.Session:
    """Create a new HTTP session with appropriate headers."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Connection": "keep-alive",
    })
    return s


def _extract_jsonld(soup: BeautifulSoup) -> Optional[dict]:
    """Extract JobPosting JSON-LD block from the page."""
    try:
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string)
                if isinstance(data, dict) and data.get("@type") == "JobPosting":
                    return data
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and item.get("@type") == "JobPosting":
                            return item
            except (json.JSONDecodeError, TypeError):
                continue
    except Exception as exc:
        logger.warning("JSON-LD parse error: %s", exc)
    return None


def _extract_job_data(session: requests.Session, url: str, src_url: str, index: int) -> dict:
    """Extract structured job data from a single job detail page via HTTP."""
    try:
        resp = session.get(url, timeout=15)
        status_code = resp.status_code
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.error("Phase 2: HTTP error for %s: %s", url[:80], exc)
        return _error_item(url, src_url, status_code=getattr(exc.response, "status_code", 0), error=str(exc), index=index)

    soup = BeautifulSoup(resp.text, "html.parser")
    jsonld = _extract_jsonld(soup)

    item: dict = {
        "id": 0,
        "title": "",
        "price": "",
        "availability": "",
        "original_price": "",
        "currency": CURRENCY,
        "url": url,
        "src_url": src_url,
        "location": "",
        "status_code": status_code,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": "",
    }

    # ── Soft 404 detection ─────────────────────────────────────────────
    title_tag = soup.find("title")
    title_text = (title_tag.get_text(strip=True) if title_tag else "").lower()
    h1_tag = soup.find("h1")
    h1_text = (h1_tag.get_text(strip=True) if h1_tag else "").lower()
    soft404_phrases = ["not found", "unavailable", "discontinued", "no longer available", "page not found", "404"]
    if any(phrase in title_text or phrase in h1_text for phrase in soft404_phrases):
        item["remarks"] = "Soft 404: job not found"
        logger.warning("Phase 2: Soft 404 detected for %s", url[:80])
        return item

    # Check for redirect
    final_url = resp.url.rstrip("/")
    if final_url != url.rstrip("/") and not _is_job_url(final_url):
        item["remarks"] = "Soft 404: redirected to non-job page"
        return item

    # Check JSON-LD presence for job pages
    if not jsonld:
        # Still attempt CSS extraction — some pages may lack JSON-LD
        logger.debug("Phase 2: No JSON-LD for %s, using CSS only", url[:60])

    # ── Title: h1 (primary — full descriptive title) ────────────────────
    try:
        h1_el = soup.select_one("h1.job-details-header")
        if h1_el:
            item["title"] = h1_el.get_text(strip=True)
        elif h1_tag:
            item["title"] = h1_text.strip() or ""
        elif jsonld:
            # Fallback to JSON-LD title (note: abbreviated like "DNP")
            item["title"] = jsonld.get("title", "")
    except Exception:
        pass

    # ── Job ID ──────────────────────────────────────────────────────────
    try:
        if jsonld:
            ident = jsonld.get("identifier", {})
            if isinstance(ident, dict):
                item["job_id"] = ident.get("value", "")
            elif isinstance(ident, str):
                item["job_id"] = ident
    except Exception:
        pass
    if not item.get("job_id"):
        try:
            el = soup.select_one(".job-details-top-text.text-end")
            if el:
                text = el.get_text(strip=True)
                item["job_id"] = text.replace("Job ID:", "").strip()
        except Exception:
            pass

    # ── Specialty ───────────────────────────────────────────────────────
    try:
        el = soup.select_one(".row > .col-6:first-child .job-details-top-text")
        if el:
            text = el.get_text(strip=True)
            if "Job ID:" not in text:
                item["specialty"] = text
    except Exception:
        pass

    # ── Location ────────────────────────────────────────────────────────
    try:
        if jsonld:
            loc = jsonld.get("jobLocation", {})
            if isinstance(loc, dict):
                addr = loc.get("address", {})
                if isinstance(addr, dict):
                    item["location"] = addr.get("addressRegion", "") or addr.get("addressLocality", "")
    except Exception:
        pass
    if not item.get("location"):
        try:
            els = soup.select(".job-post-locations")
            if els:
                item["location"] = els[0].get_text(strip=True)
        except Exception:
            pass

    # ── Date posted ─────────────────────────────────────────────────────
    try:
        if jsonld:
            item["date_posted"] = jsonld.get("datePosted", "")
    except Exception:
        pass

    # ── Employment type ────────────────────────────────────────────────
    try:
        if jsonld:
            item["employment_type"] = jsonld.get("employmentType", "")
    except Exception:
        pass

    # ── Description ────────────────────────────────────────────────────
    try:
        if jsonld:
            desc_html = jsonld.get("description", "")
            if desc_html:
                item["description"] = strip_html(desc_html)
    except Exception:
        pass
    if not item.get("description"):
        try:
            el = soup.select_one(".job-details-body")
            if el:
                item["description"] = el.get_text(separator=" ", strip=True)
        except Exception:
            pass

    # ── Dates needed ────────────────────────────────────────────────────
    try:
        el = soup.select_one(".job-details-glance-list li:first-child")
        if el:
            text = el.get_text(strip=True)
            item["dates_needed"] = text.replace("Dates needed:", "").strip()
    except Exception:
        pass

    # ── Shift type ─────────────────────────────────────────────────────
    try:
        els = soup.select(".job-details-glance-list li")
        if len(els) > 1:
            text = els[1].get_text(strip=True)
            item["shift_type"] = text.replace("Shift type:", "").strip()
    except Exception:
        pass

    # ── Assignment type ────────────────────────────────────────────────
    try:
        els = soup.select(".job-details-glance-list li")
        if len(els) > 2:
            text = els[2].get_text(strip=True)
            item["assignment_type"] = text.replace("Assignment type:", "").strip()
    except Exception:
        pass

    # ── Recruiter name ──────────────────────────────────────────────────
    try:
        el = soup.select_one(".job-details-body .flex-grow-1 h5")
        if el:
            text = el.get_text(strip=True)
            if text.lower() not in ("additional job details", ""):
                item["recruiter_name"] = text
    except Exception:
        pass

    # ── Verified badge ─────────────────────────────────────────────────
    try:
        el = soup.select_one(".job-is-tier-1 .cds-badge")
        if el:
            item["verified"] = el.get_text(strip=True)
    except Exception:
        pass

    # ── Job category ────────────────────────────────────────────────────
    try:
        # Primary: CSS selector for 4th .job-post-locations element
        el = soup.select_one(".job-post-locations:nth-child(4)")
        if el:
            item["job_category"] = el.get_text(strip=True)
        else:
            # Fallback: grab 4th element from the full list
            els = soup.select(".job-post-locations")
            if len(els) >= 4:
                item["job_category"] = els[3].get_text(strip=True)
    except Exception:
        pass
    # Fallback: parse role from URL path (e.g. /family-practice-jobs/dnp/virginia/job-1339893)
    if not item.get("job_category"):
        try:
            parsed = urlparse(url)
            path_segs = [s for s in parsed.path.strip("/").split("/") if s]
            # URL pattern: /{specialty}-jobs/{role}/{state}/job-{id}
            if len(path_segs) >= 4 and path_segs[-1].startswith("job-"):
                role_seg = path_segs[2] if len(path_segs) >= 3 else ""
                if role_seg:
                    item["job_category"] = role_seg.upper()
        except Exception:
            pass

    # ── Hiring organization ───────────────────────────────────────────
    try:
        if jsonld:
            org = jsonld.get("hiringOrganization", {})
            if isinstance(org, dict):
                item["company"] = org.get("name", "")
                item["hiring_organization"] = item["company"]
    except Exception:
        pass

    # ── Industry ───────────────────────────────────────────────────────
    try:
        if jsonld:
            item["industry"] = jsonld.get("industry", "")
    except Exception:
        pass

    # ── Valid through ───────────────────────────────────────────────────
    try:
        if jsonld:
            item["valid_through"] = jsonld.get("validThrough", "")
    except Exception:
        pass

    # ── Phase 2 safety-net filter: check location is Alabama ─────────────
    # The discovery phase already filters by Alabama, but re-check on detail page
    location_lower = (item.get("location") or "").lower()
    if location_lower and location_lower not in ("al", "alabama"):
        item["remarks"] = f"Location mismatch: '{item.get('location')}' is not Alabama"

    # ── Phase 2 safety-net filter: check date is within 7 days ──────────
    date_posted = item.get("date_posted", "")
    if date_posted:
        try:
            posted = datetime.fromisoformat(date_posted)
            cutoff = datetime.now(timezone.utc) - timedelta(days=7)
            if posted.tzinfo is None:
                posted = posted.replace(tzinfo=timezone.utc)
            if posted < cutoff:
                item["remarks"] = (item.get("remarks") or "") + f" Posted {date_posted} (>7 days old)"
        except (ValueError, TypeError):
            pass

    return item


def _error_item(url: str, src_url: str, status_code: int = 0, error: str = "", index: int = 0) -> dict:
    return {
        "id": index,
        "title": "",
        "price": "",
        "availability": "",
        "original_price": "",
        "currency": CURRENCY,
        "url": url,
        "src_url": src_url,
        "location": "",
        "status_code": status_code,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": f"Error: {error[:200]}",
    }


def _extract_items_concurrent(urls: list[str], src_url: str, limit: Optional[int] = None) -> list[dict]:
    """Extract job data concurrently using ThreadPoolExecutor."""
    # Index URLs to preserve discovery order
    indexed_urls = list(enumerate(urls[:limit] if limit else urls))
    results_map: dict[int, dict] = {}
    failed = 0

    def _worker(idx: int, url: str) -> tuple[int, dict]:
        session = _make_session()
        try:
            time.sleep(DELAY_BETWEEN_REQUESTS * 0.3)  # slight per-thread jitter
            item = _extract_job_data(session, url, src_url, idx)
            return idx, item
        except Exception as exc:
            logger.error("Worker error for %s: %s", url[:80], exc)
            return idx, _error_item(url, src_url, error=str(exc), index=idx)
        finally:
            session.close()

    total = len(indexed_urls)
    logger.info("Phase 2: Extracting %d jobs with ThreadPoolExecutor(max_workers=8)", total)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(_worker, idx, url): idx
            for idx, url in indexed_urls
        }
        for i, future in enumerate(as_completed(futures), 1):
            idx, item = future.result()
            results_map[idx] = item
            if not item.get("title"):
                failed += 1
            if i % 25 == 0 or i == total:
                pct = (i / total) * 100
                logger.info("Phase 2: Progress [%d/%d] (%.1f%%)", i, total, pct)

    # Reassemble in discovery order
    ordered = [results_map[idx] for idx in sorted(results_map.keys())]

    # Assign sequential IDs
    for seq, item in enumerate(ordered, 1):
        item["id"] = seq

    logger.info("Phase 2: Extraction complete — %d success, %d failed", len(ordered) - failed, failed)
    return ordered


def _extract_items_sequential(urls: list[str], src_url: str, limit: Optional[int] = None) -> list[dict]:
    """Extract job data sequentially (for --sample or small sets)."""
    session = _make_session()
    items: list[dict] = []
    total = len(urls[:limit] if limit else urls)
    failed = 0

    for i, url in enumerate(urls[:limit] if limit else urls, 1):
        logger.info("Phase 2: Progress [%d/%d] (%.1f%%) — %s", i, total, (i / total) * 100, url[:100])
        item = _extract_job_data(session, url, src_url, i)
        item["id"] = i
        items.append(item)
        if not item.get("title"):
            failed += 1
        if i < total:
            time.sleep(DELAY_BETWEEN_REQUESTS)

    session.close()
    logger.info("Phase 2: Sequential extraction — %d success, %d failed", len(items) - failed, failed)
    return items


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(description=f"{SITE_NAME} Job Scraper (Navigation)")
    parser.add_argument("--input", type=str, help="Path to input URLs JSON file")
    parser.add_argument("--urls", nargs="+", help="Job URLs as CLI arguments")
    parser.add_argument("--sample", action="store_true", help="Scrape first 5 items only")
    parser.add_argument("--limit", type=int, default=None, help="Max items to scrape")
    parser.add_argument("--no-proxy", action="store_true", default=True, help="Disable proxy (default)")
    parser.add_argument("--headless", action="store_true", default=True, help="Headless browser mode")
    args = parser.parse_args()

    limit = 5 if args.sample else args.limit
    start_time = time.time()

    logger.info("=" * 80)
    logger.info("Starting scraper for %s", SITE_NAME)
    logger.info("Content type: %s | Output key: %s", CONTENT_TYPE, OUTPUT_KEY)
    logger.info("=" * 80)

    discovered_urls: list[str] = []
    src_url: str = SITE_URL

    # ── Mode: --input or --urls provided → skip Phase 1 ─────────────────
    if args.input or args.urls:
        if args.input:
            try:
                with open(args.input, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    discovered_urls = data.get("urls", [])
            except Exception as exc:
                logger.error("Failed to read %s: %s", args.input, exc)
                sys.exit(1)
        elif args.urls:
            discovered_urls = list(args.urls)

        logger.info("Loaded %d URLs from input (--input/--urls)", len(discovered_urls))
        for url in discovered_urls:
            src_url = url
            break
    else:
        # ── Default: Phase 1 discovery via search form ────────────────
        logger.info("Phase 1: Discovering job URLs via search form (Alabama, last 7 days, all specialties)")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=args.headless)
            context = browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()

            discovered_urls = _discover_all_job_urls(page, limit=limit)
            src_url = f"{SITE_URL}/Resources/JobSearch/SearchResults (Alabama, 7 days)"

            browser.close()

        if not discovered_urls:
            logger.warning("No job URLs discovered — exiting")
            sys.exit(0)

        logger.info("Total products discovered: %d", len(discovered_urls))

    # ── Phase 2: Extract data ───────────────────────────────────────────
    if not discovered_urls:
        logger.warning("No URLs to extract — exiting")
        sys.exit(0)

    logger.info("=" * 80)
    logger.info("Phase 2: Extracting data from %d jobs", len(discovered_urls[:limit] if limit else discovered_urls))
    logger.info("=" * 80)

    # Use concurrent extraction for larger sets, sequential for small/sample
    effective_urls = discovered_urls[:limit] if limit else discovered_urls
    if len(effective_urls) > 10:
        items = _extract_items_concurrent(discovered_urls, src_url, limit=limit)
    else:
        items = _extract_items_sequential(discovered_urls, src_url, limit=limit)

    # ── Output filter ───────────────────────────────────────────────────
    _before = len(items)
    items = [
        it for it in items
        if it.get("title") and (
            not CORE_FILTER_FIELDS or any(it.get(f) for f in CORE_FILTER_FIELDS)
        )
    ]
    if len(items) != _before:
        logger.info(
            "Output filter: %d → %d items (dropped %d without core fields)",
            _before, len(items), _before - len(items),
        )

    # ── Write output ────────────────────────────────────────────────────
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    output_filename = os.path.join(SCRIPT_DIR, f"output_{timestamp}.json")

    output = {
        "site": {
            "name": SITE_NAME,
            "url": SITE_URL,
            "platform": PLATFORM,
            "scraping_method": f"playwright_navigation + {SCRAPING_METHOD}",
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        },
        OUTPUT_KEY: items,
        "metadata": {
            "scraping_duration_seconds": round(time.time() - start_time, 1),
            "discovered_urls": len(discovered_urls),
            "extracted_items": len(items),
            "failed_products": sum(1 for it in items if not it.get("title")),
            "rate_limit_delay": DELAY_BETWEEN_REQUESTS,
        },
    }

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

    success = sum(1 for it in items if it.get("title"))
    failed = sum(1 for it in items if not it.get("title"))

    logger.info("=" * 80)
    logger.info("EXTRACTION COMPLETE")
    logger.info("Total discovered: %d | Extracted: %d | Success: %d | Failed: %d",
                len(discovered_urls), len(items), success, failed)
    logger.info("Duration: %.1fs → %s", time.time() - start_time, output_filename)
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
