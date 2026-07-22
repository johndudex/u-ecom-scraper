#!/usr/bin/env python3
"""HTTP Navigation Scraper for LocumTenens.com — job board.

Two-phase architecture:

  Phase 1: Discover job URLs by iterating through ALL specialties in the
           QuickSearch form <select>, POSTing once per specialty, paginating
           search results, and deduplicating by job ID.
  Phase 2: Extract structured data from each discovered job page via direct
           HTTP GET. JSON-LD JobPosting + description-HTML parsing happen
           locally. Items fetched concurrently with ThreadPoolExecutor.

Usage:
    python3 scraper_draft.py                                  # full run (form-search)
    python3 scraper_draft.py --sample                         # first 5 items only
    python3 scraper_draft.py --limit 50                       # cap item count
    python3 scraper_draft.py --input input_urls.json          # specific URLs
"""

import argparse
import json
import logging
import os
import re
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse, urljoin

import httpx
from bs4 import BeautifulSoup

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION — adapted from template for locumtenens.com
# ═══════════════════════════════════════════════════════════════════════════════

SITE_NAME = "LocumTenens.com"
SITE_URL = "https://www.locumtenens.com"
PLATFORM = "custom"
SITE_SLUG = "locumtenens-com"

# Execution model
NAVIGATE_TIMEOUT = 120
MAX_RETRIES = 3
BACKOFF_BASE = 2.0
PHASE2_WORKERS = 8

# Phase 1: Navigation — form-search iteration
SEARCH_URL_PATTERN = "https://www.locumtenens.com/Resources/JobSearch/QuickSearch"
SEARCH_BOX_SELECTOR = ""
SEARCH_SUBMIT_SELECTOR = ""
CATEGORY_URLS = []

# Form-search iteration (locumtenens REQUIRES a specialty selection)
FORM_ACTION = "https://www.locumtenens.com/Resources/JobSearch/QuickSearch"
FORM_METHOD = "POST"
FORM_SELECT_NAME = "Specialties"
FORM_BASE_URL = "https://www.locumtenens.com/Resources/JobSearch/QuickSearch"

# Pagination
PAGINATION_TYPE = "next_button"
NEXT_BUTTON_SELECTOR = ""
PAGE_PARAM_NAME = ""
ITEMS_PER_PAGE = 25
MAX_PAGES = None  # unlimited — paginate to exhaustion
TOTAL_COUNT_SELECTOR = ""

DISCOVERY_DEADLINE_SECONDS = 600
COVERAGE_TARGET_TOTAL: Optional[int] = None

# Phase 1: Item link extraction
ITEM_CONTAINER_SELECTOR = ""
ITEM_LINK_SELECTOR = ""
ITEM_URL_PATTERN = r"/job-\d+"

# Phase 2: Extraction
SCRAPING_METHOD = "http_navigation"
PROXY_TIER = "none"
DELAY_BETWEEN_REQUESTS = 0.3

# Output
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_KEY = "jobs"
CONTENT_TYPE = "job_posting"
CURRENCY = "USD"

SRC_URL = SITE_URL

_CONTENT_FILTER_FIELDS = {
    "product": ["price", "availability"],
    "article": ["author", "publish_date"],
    "job_posting": ["location"],
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
# HTTP PRIMITIVES — direct HTTP (no browser_service needed for this site)
# ═══════════════════════════════════════════════════════════════════════════════

_thread_local = threading.local()


def _get_session() -> httpx.Client:
    """Return a thread-local httpx.Client (NOT thread-safe to share)."""
    if not hasattr(_thread_local, "session"):
        _thread_local.session = httpx.Client(
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=30,
            follow_redirects=True,
        )
    return _thread_local.session


def _http_get(url: str) -> tuple[str, int]:
    """Plain HTTP GET. Returns (html, status_code)."""
    try:
        session = _get_session()
        r = session.get(url)
        return r.text, r.status_code
    except Exception as exc:
        logger.warning("_http_get %s failed: %s", url[:60], exc)
        return "", 0


def _http_post(url: str, data: dict) -> tuple[str, int]:
    """Plain HTTP POST. Returns (html, status_code)."""
    try:
        session = _get_session()
        r = session.post(url, data=data)
        return r.text, r.status_code
    except Exception as exc:
        logger.warning("_http_post %s failed: %s", url[:60], exc)
        return "", 0


def _http_get_with_retry(url: str) -> tuple[str, int]:
    """HTTP GET with exponential backoff for transient errors."""
    for attempt in range(MAX_RETRIES):
        html, status = _http_get(url)
        if status and status < 500 and status != 429:
            return html, status
        if status:
            logger.debug("GET %s → %d (attempt %d/%d)", url[:60], status, attempt + 1, MAX_RETRIES)
        time.sleep(min(BACKOFF_BASE ** (attempt + 1), 30))
    return "", 0


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
    """Job URL detector — locumtenens job URLs contain /job-{digits}."""
    if not href:
        return False
    site_host = (urlparse(SITE_URL).hostname or "").lower()
    if site_host and site_host not in href.lower():
        return False
    # locumtenens job URLs: /{specialty}-jobs/{profession}/{state}/job-{id}
    if re.search(r"/job-\d+", href):
        return True
    path = urlparse(href).path.strip("/")
    if not path or len(path) < 6:
        return False
    segs = path.split("/")
    last = segs[-1]
    if len(segs) == 1 and len(last) < 12 and not any(c.isdigit() for c in last):
        return False
    return True


def _set_query_param(url: str, param: str, value) -> str:
    p = urlparse(url)
    qs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k != param]
    qs.append((param, str(value)))
    return urlunparse(p._replace(query=urlencode(qs)))


_OFFSET_PARAMS = {"offset", "start", "skip", "begin", "from"}


def _extract_next_href(html: str) -> Optional[str]:
    """Find a 'next page' href in listing HTML."""
    if not html:
        return None
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return None
    # Semantic fallbacks
    for sel in ('a[rel="next"]', "a.next", "li.next a", 'a[aria-label="Next"]',
                'a[aria-label="next"]', "a:contains('Next')", "a:contains('>')"):
        try:
            el = soup.select_one(sel)
            if el and el.get("href"):
                return _make_absolute(el["href"])
        except Exception:
            pass
    return None


def _get_next_page_url(final_url: str, next_page_num: int, html: str = None) -> Optional[str]:
    """Construct the URL for the next page of results."""
    if PAGE_PARAM_NAME and PAGE_PARAM_NAME not in ("",):
        if PAGE_PARAM_NAME in _OFFSET_PARAMS:
            value = (next_page_num - 1) * (ITEMS_PER_PAGE or 25)
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
    """Extract job page URLs from listing HTML — 3-tier fallback.

    Tier 1: ITEM_CONTAINER_SELECTOR ▸ ITEM_LINK_SELECTOR (scoped per card)
    Tier 2: bare ITEM_LINK_SELECTOR (page-wide)
    Tier 3: every a[href] filtered by ITEM_URL_PATTERN + _is_product_url
    """
    if not html:
        return []
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as exc:
        logger.warning("Phase 1: HTML parse failed: %s", exc)
        return []

    links: list[str] = []

    # Tier 1: container + link selector
    if ITEM_CONTAINER_SELECTOR:
        try:
            containers = soup.select(ITEM_CONTAINER_SELECTOR)
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

    # Tier 2: bare link selector (page-wide)
    if not links and ITEM_LINK_SELECTOR:
        try:
            for a in soup.select(ITEM_LINK_SELECTOR):
                href = a.get("href", "")
                if href:
                    links.append(_make_absolute(href))
        except Exception as exc:
            logger.warning("Phase 1: bare link selector failed: %s", exc)

    # Tier 3: broad fallback — all anchors matching job URL pattern
    if len(links) < 20:
        pattern = None
        if ITEM_URL_PATTERN:
            try:
                pattern = re.compile(ITEM_URL_PATTERN)
            except re.error:
                pattern = None
        existing = set(links)
        added = 0
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            if not href:
                continue
            url = _make_absolute(href)
            if not url or url in existing:
                continue
            if pattern and not pattern.search(url):
                continue
            if not _is_product_url(url):
                continue
            links.append(url)
            existing.add(url)
            added += 1
        if added:
            logger.info("Phase 1: broad fallback captured %d additional links", added)

    return list(dict.fromkeys(links))


def _discover_urls_via_form_search(
    max_pages: Optional[int] = None,
    limit: Optional[int] = None,
) -> tuple[list[str], str]:
    """Phase 1: Discover job URLs by iterating through ALL specialty options.

    1. Fetch the QuickSearch form page to parse <select> options + hidden fields
    2. For each specialty: POST the form, extract job URLs, paginate
    3. Deduplicate by job ID across all specialties
    """
    all_urls: list[str] = []
    seen_ids: set[str] = set()

    form_page_url = FORM_BASE_URL
    form_action_url = FORM_ACTION
    if not form_action_url or not FORM_SELECT_NAME:
        logger.error("Phase 1 (form-search): FORM_ACTION or FORM_SELECT_NAME not set")
        return [], "navigate_error"

    # Fetch form page for hidden fields + select options
    form_html, form_status = _http_get(form_page_url)
    if not form_html:
        logger.error("Phase 1 (form-search): could not fetch form page %s", form_page_url)
        return [], "navigate_error"

    form_soup = BeautifulSoup(form_html, "html.parser")

    # Extract hidden fields
    hidden_fields: dict[str, str] = {}
    for inp in form_soup.find_all("input", {"type": "hidden"}):
        name = inp.get("name", "")
        val = inp.get("value", "")
        if name:
            hidden_fields[name] = val
    logger.info("Phase 1 (form-search): %d hidden fields from form page", len(hidden_fields))

    # Parse all options from the target <select>
    select_el = form_soup.find("select", {"name": FORM_SELECT_NAME})
    if not select_el:
        logger.error("Phase 1 (form-search): <select name='%s'> not found on form page", FORM_SELECT_NAME)
        return [], "navigate_error"

    options = []
    for opt in select_el.find_all("option"):
        val = (opt.get("value") or "").strip()
        text = (opt.get_text() or "").strip()
        if val and text and not re.match(r"^(any|all|select|please|title|specialty\s)", text, re.I):
            options.append((val, text))
    logger.info("Phase 1 (form-search): %d options to iterate in <%s>", len(options), FORM_SELECT_NAME)

    if not options:
        logger.error("Phase 1 (form-search): no valid options found")
        return [], "navigate_error"

    deadline = time.time() + DISCOVERY_DEADLINE_SECONDS
    stop_reason = "no_next_link"

    for idx, (opt_val, opt_text) in enumerate(options):
        if time.time() > deadline:
            logger.warning("Phase 1 (form-search): deadline exceeded after %d/%d options", idx, len(options))
            stop_reason = "max_pages_hit"
            break
        if limit and len(all_urls) >= limit:
            logger.info("Phase 1 (form-search): reached limit=%d", limit)
            stop_reason = "max_pages_hit"
            break

        # Submit the form with this specialty option
        form_data = {**hidden_fields, FORM_SELECT_NAME: opt_val}
        page_num = 1
        results_url = form_action_url  # may be updated by redirect after POST

        while True:
            if max_pages and page_num > max_pages:
                break
            if time.time() > deadline:
                break

            if page_num == 1:
                # POST the form for the first page
                resp_html, status = _http_post(results_url, form_data)
            else:
                # GET subsequent pages via next-link (session-scoped results)
                resp_html, status = _http_get(results_url)

            if not resp_html or status >= 400:
                logger.warning("Phase 1 (form-search): option '%s' page %d → status %d", opt_text[:30], page_num, status)
                break

            page_urls = _extract_item_links(resp_html)
            new_count = 0
            for url in page_urls:
                item_id = re.search(r'/job-(\d+)', url) or re.search(r'/(\d{4,})/?$', url)
                dedup_key = item_id.group(1) if item_id else url
                if dedup_key not in seen_ids:
                    seen_ids.add(dedup_key)
                    all_urls.append(url)
                    new_count += 1

            if new_count == 0:
                break  # no new items → done with this option

            # Check for next page
            next_link = _extract_next_href(resp_html)
            if not next_link:
                break  # no more pages for this option
            else:
                results_url = next_link
                page_num += 1

        if idx % 10 == 0 or idx == len(options) - 1:
            logger.info("Phase 1 (form-search): [%d/%d] option '%s' → %d total URLs",
                        idx + 1, len(options), opt_text[:30], len(all_urls))

    unique_urls = list(dict.fromkeys(all_urls))
    if limit:
        unique_urls = unique_urls[:limit]
    logger.info("Phase 1 (form-search): Discovered %d total job URLs across %d options (%s)",
                len(unique_urls), len(options), stop_reason)
    return unique_urls, stop_reason


def _discover_urls_via_category(
    category_url: str,
    max_pages: Optional[int] = None,
    limit: Optional[int] = None,
) -> tuple[list[str], str]:
    """Phase 1b: Discover item URLs from a category/listing page."""
    logger.info("Phase 1: Browsing listing → %s", category_url)
    html, status = _http_get_with_retry(category_url)
    if not html or status >= 400:
        logger.error("Phase 1: listing fetch failed for %s (status %d)", category_url[:60], status)
        return [], "navigate_error"

    all_urls: list[str] = _extract_item_links(html)
    stop_reason = "no_next_link"
    current_page = 1
    while True:
        if max_pages and current_page >= max_pages:
            stop_reason = "max_pages_hit"
            break
        if limit and len(all_urls) >= limit:
            stop_reason = "max_pages_hit"
            break

        next_url = _get_next_page_url(category_url, current_page + 1, html)
        if not next_url:
            stop_reason = "no_next_link"
            break

        logger.info("Phase 1: Listing page %d", current_page + 1)
        html, status = _http_get_with_retry(next_url)
        if not html or status >= 400:
            stop_reason = "navigate_error"
            break

        new_urls = _extract_item_links(html)
        if not new_urls or not (set(new_urls) - set(all_urls)):
            if not new_urls or (ITEMS_PER_PAGE and len(new_urls) < ITEMS_PER_PAGE):
                stop_reason = "short_page"
            else:
                stop_reason = "no_new_items"
            break

        all_urls.extend(new_urls)
        current_page += 1
        if DELAY_BETWEEN_REQUESTS:
            time.sleep(DELAY_BETWEEN_REQUESTS)

    unique_urls = list(dict.fromkeys(all_urls))
    if limit:
        unique_urls = unique_urls[:limit]
    logger.info("Phase 1: Discovered %d total item URLs from listing (%s)", len(unique_urls), stop_reason)
    return unique_urls, stop_reason


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 2: ITEM EXTRACTION (concurrent)
# ═══════════════════════════════════════════════════════════════════════════════

# Code Writer adapted: JSON-LD extraction + description HTML parsing for job fields

def _extract_jsonld(html: str) -> list[dict]:
    """Extract all JSON-LD blocks from HTML, returning parsed dicts."""
    blocks = []
    if not html:
        return blocks
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return blocks
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        text = script.string or script.get_text() or ""
        text = text.strip()
        if not text:
            continue
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            continue
        # Handle @graph arrays
        if isinstance(data, dict) and "@graph" in data:
            for item in data["@graph"]:
                if isinstance(item, dict):
                    blocks.append(item)
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    blocks.append(item)
        elif isinstance(data, dict):
            blocks.append(data)
    return blocks


def _parse_description_field(desc_html: str, field_name: str) -> str:
    """Parse a key-value field from JSON-LD description HTML.

    The description contains list items like:
      <li><strong>Job Reference Id</strong>: 12345</li>
      <li><strong>Dates Needed</strong>: ASAP - Ongoing</li>
    """
    if not desc_html:
        return ""
    try:
        soup = BeautifulSoup(desc_html, "html.parser")
    except Exception:
        return ""
    # Look for <strong> or <b> tag containing the field name
    for tag in soup.find_all(["strong", "b", "dt", "label"]):
        text = tag.get_text(strip=True).lower()
        if field_name.lower() in text:
            # Get text after the strong tag — sibling or parent's remaining text
            parent = tag.parent
            if parent:
                full_text = parent.get_text(separator=" ", strip=True)
                # Extract the value after the field name
                pattern = re.compile(
                    re.escape(field_name) + r"[\s:]*[:\s]*(.+?)(?:\n|$)",
                    re.I,
                )
                match = pattern.search(full_text)
                if match:
                    return match.group(1).strip()
            # Fallback: get next sibling text
            nxt = tag.next_sibling
            if nxt and isinstance(nxt, str):
                val = nxt.strip().lstrip(":").strip()
                if val:
                    return val
    return ""


def _parse_description_section(desc_html: str, section_name: str) -> str:
    """Parse a paragraph section from JSON-LD description HTML.

    Sections like 'About the Facility' contain heading + paragraph text.
    """
    if not desc_html:
        return ""
    try:
        soup = BeautifulSoup(desc_html, "html.parser")
    except Exception:
        return ""
    # Look for heading (h2/h3/h4) containing section name
    for heading in soup.find_all(["h2", "h3", "h4", "h5", "strong"]):
        text = heading.get_text(strip=True).lower()
        if section_name.lower() in text:
            # Get all text from the heading until the next heading of same/higher level
            parts = []
            for sibling in heading.next_siblings:
                if hasattr(sibling, "name") and sibling.name in ("h2", "h3", "h4", "h5"):
                    break
                if isinstance(sibling, str):
                    t = sibling.strip()
                    if t:
                        parts.append(t)
                elif hasattr(sibling, "get_text"):
                    t = sibling.get_text(strip=True)
                    if t:
                        parts.append(t)
            if parts:
                return " ".join(parts).strip()
    return ""


def _extract_url_fields(url: str) -> dict:
    """Extract profession and specialty from the job URL path.

    URL pattern: /{specialty}-jobs/{profession}/{state}/job-{numeric_id}
    """
    fields = {}
    if not url:
        return fields
    path = urlparse(url).path.strip("/")
    segs = path.split("/")
    # segs[0] = specialty-jobs, segs[1] = profession, segs[2] = state, segs[3] = job-id
    if len(segs) >= 1:
        specialty = segs[0]
        fields["specialty"] = specialty.replace("-jobs", "").replace("-", " ")
    if len(segs) >= 2:
        fields["profession"] = segs[1].replace("-", " ")
    return fields


def _populate_from_jsonld(item: dict, jsonld_blocks: list[dict]) -> None:
    """Fill item from JSON-LD JobPosting block."""
    for block in jsonld_blocks:
        block_type = block.get("@type", "")
        if isinstance(block_type, list):
            block_type = block_type[0] if block_type else ""

        if block_type != "JobPosting":
            continue

        item["title"] = block.get("title", "") or item.get("title", "")

        org = block.get("hiringOrganization", {})
        if isinstance(org, dict):
            item["company"] = org.get("name", "")

        loc = block.get("jobLocation", {})
        if isinstance(loc, dict):
            addr = loc.get("address", {})
            if isinstance(addr, dict):
                city = addr.get("addressLocality", "")
                region = addr.get("addressRegion", "")
                if city and region:
                    item["location"] = f"{city}, {region}"
                elif city:
                    item["location"] = city
                elif region:
                    item["location"] = region

        # posted_date
        date_posted = block.get("datePosted", "")
        if date_posted:
            item["posted_date"] = date_posted

        # Full description (HTML) — store raw for parsing
        desc_html = block.get("description", "")
        item["description"] = desc_html

        # Parse sub-fields from description HTML
        if desc_html:
            # Job Reference Id / job_id
            job_id = _parse_description_field(desc_html, "Job Reference Id")
            if job_id:
                item["job_id"] = job_id

            # Dates Needed
            dates = _parse_description_field(desc_html, "Dates Needed")
            if dates:
                item["dates_needed"] = dates

            # Assignment Type
            atype = _parse_description_field(desc_html, "Assignment Type")
            if atype:
                item["assignment_type"] = atype

            # Call Required
            call = _parse_description_field(desc_html, "Call Required")
            if call:
                item["call_required"] = call

            # Board Certification Required
            bcert = _parse_description_field(desc_html, "Board Certification Required")
            if bcert:
                item["board_certification_required"] = bcert

            # Job Duration
            dur = _parse_description_field(desc_html, "Job Duration")
            if dur:
                item["job_duration"] = dur

            # Facility Description
            facility = _parse_description_section(desc_html, "About the Facility")
            if facility:
                item["facility_description"] = facility

        break


def _error_item(url: str, src_url: str, error: str) -> dict:
    return {
        "url": url,
        "src_url": src_url,
        "status_code": 0,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": f"Error: {error[:200]}",
    }


def _extract_item(item_url: str, src_url: str) -> dict:
    """Phase 2: Extract structured data from a single job page."""
    html, status = _http_get_with_retry(item_url)
    if not html:
        return _error_item(item_url, src_url, "fetch failed after retries")
    if status == 404:
        return _error_item(item_url, src_url, "404 not found")
    if status >= 400:
        return _error_item(item_url, src_url, f"HTTP {status}")

    item: dict = {
        "url": item_url,
        "src_url": src_url,
        "status_code": 200,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": "",
    }

    # Soft 404 detection
    soup = None
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        pass

    # Extract profession/specialty from URL
    url_fields = _extract_url_fields(item_url)
    item["profession"] = url_fields.get("profession", "")
    item["specialty"] = url_fields.get("specialty", "")

    # JSON-LD extraction
    try:
        jsonld_blocks = _extract_jsonld(html)
        has_jobposting = any(
            (b.get("@type") == "JobPosting" or
             (isinstance(b.get("@type"), list) and "JobPosting" in b.get("@type", [])))
            for b in jsonld_blocks
        )
        if jsonld_blocks:
            _populate_from_jsonld(item, jsonld_blocks)
    except Exception as exc:
        logger.warning("Phase 2: JSON-LD extraction failed for %s: %s", item_url[:60], exc)

    # CSS fallbacks
    if not item.get("title") and soup:
        h1 = soup.select_one("h1")
        if h1:
            item["title"] = h1.get_text(strip=True)

    # Soft 404 detection
    if not has_jobposting if "has_jobposting" in dir() else True:
        page_title = ""
        if soup:
            title_tag = soup.find("title")
            if title_tag:
                page_title = title_tag.get_text(strip=True).lower()
        if any(kw in page_title for kw in ["not found", "unavailable", "discontinued", "no longer"]):
            item["remarks"] = "Soft 404: job not found"
            item["title"] = ""
            return item

    # Location fallback from page text
    if not item.get("location"):
        if soup:
            text = soup.get_text(separator=" ", strip=True)
            loc_match = re.search(
                r"Posted\s+\S+\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)?,\s*[A-Z]{2})",
                text,
            )
            if loc_match:
                item["location"] = loc_match.group(1)

    # job_id fallback from page text
    if not item.get("job_id"):
        if soup:
            text = soup.get_text(separator=" ", strip=True)
            jid_match = re.search(r"Job Reference Id[:\s]*(\S+)", text, re.I)
            if jid_match:
                item["job_id"] = jid_match.group(1).strip()
            else:
                jid_match2 = re.search(r"Job ID[:\s]*(\S+)", text, re.I)
                if jid_match2:
                    item["job_id"] = jid_match2.group(1).strip()

    # Description fallback from DOM
    if not item.get("description") and soup:
        desc_el = soup.select_one('[class*="description"]') or soup.select_one(".job-description")
        if desc_el:
            item["description"] = desc_el.get_text(separator=" ", strip=True)

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
    parser = argparse.ArgumentParser(description=f"{SITE_NAME} HTTP Navigation Scraper")
    parser.add_argument("--query", type=str, help="Search query for navigation mode")
    parser.add_argument("--category-url", type=str, help="Category URL to crawl")
    parser.add_argument("--listing-url", type=str, help="Listing page URL to paginate")
    parser.add_argument("--input", type=str, help="Path to input URLs JSON file")
    parser.add_argument("--urls", nargs="+", help="Product URLs as CLI arguments")
    parser.add_argument("--sample", action="store_true", help="Scrape first 5 items only")
    parser.add_argument("--limit", type=int, default=None, help="Max items to scrape")
    parser.add_argument("--no-proxy", action="store_true", default=True,
                        help="Disable proxy (default: no proxy for this site)")
    parser.add_argument("--headless", action="store_true", default=True, help="CLI compatibility")
    parser.add_argument("--discover-only", action="store_true",
                        help="Run Phase 1 only, skip Phase 2 extraction")
    parser.add_argument("--fresh-discovery", action="store_true",
                        help="Ignore checkpoint, run Phase 1 from scratch")
    args = parser.parse_args()

    global PROXY_TIER
    if args.no_proxy:
        PROXY_TIER = "none"

    limit = 5 if args.sample else args.limit
    start_time = time.time()
    discovered_urls: list[str] = []

    # ── Coverage-gate state ─────────────────────────────────────────────
    ran_phase1 = True
    skipped_reason: Optional[str] = None
    aggregate_stop_reason = "no_next_link"
    max_pages_hit = False

    # ── Determine URL source ────────────────────────────────────────────
    # Priority: --input > --urls > checkpoint > Phase 1 discovery
    input_source = False
    if args.input:
        # --input takes precedence over checkpoint
        try:
            with open(args.input) as f:
                input_data = json.load(f)
            discovered_urls = input_data.get("urls", [])
            logger.info("Loaded %d URLs from --input %s", len(discovered_urls), args.input)
            input_source = True
            ran_phase1 = False
            skipped_reason = "input_file"
            aggregate_stop_reason = "skipped"
        except Exception as exc:
            logger.error("Failed to load input file %s: %s", args.input, exc)
    elif args.urls:
        discovered_urls = list(args.urls)
        logger.info("Loaded %d URLs from --urls", len(discovered_urls))
        input_source = True
        ran_phase1 = False
        skipped_reason = "url_list"
        aggregate_stop_reason = "skipped"

    # Checkpoint resume (only if not using --input/--urls and not --fresh-discovery)
    if not discovered_urls and not args.fresh_discovery:
        checkpoint_urls = _load_checkpoint()
        if checkpoint_urls:
            discovered_urls = checkpoint_urls
            ran_phase1 = False
            skipped_reason = "checkpoint_loaded"
            aggregate_stop_reason = "skipped"
            logger.info("Phase 1: SKIPPED (resumed from checkpoint with %d URLs)", len(discovered_urls))

    src_url_base = SITE_URL

    # ── Phase 1: Discover URLs ──────────────────────────────────────────
    if not discovered_urls:
        if FORM_ACTION and FORM_SELECT_NAME:
            # Form-search iteration: iterate ALL specialty options
            logger.info("Phase 1: form-search iteration (FORM_ACTION=%s, SELECT=%s)", FORM_ACTION, FORM_SELECT_NAME)
            discovered_urls, primary_reason = _discover_urls_via_form_search(MAX_PAGES, limit)
            src_url_base = FORM_ACTION
            aggregate_stop_reason = _merge_stop_reason(aggregate_stop_reason, primary_reason)
            max_pages_hit = max_pages_hit or primary_reason == "max_pages_hit"
        elif args.query:
            logger.info("Phase 1: discovering via search '%s'", args.query[:50])
            discovered_urls, primary_reason = _discover_urls_via_category(
                f"{SITE_URL}/Resources/JobSearch/SearchResults", MAX_PAGES, limit
            )
            src_url_base = "search"
            aggregate_stop_reason = _merge_stop_reason(aggregate_stop_reason, primary_reason)
            max_pages_hit = max_pages_hit or primary_reason == "max_pages_hit"
        elif args.category_url:
            logger.info("Phase 1: discovering via category %s", args.category_url[:50])
            discovered_urls, primary_reason = _discover_urls_via_category(args.category_url, MAX_PAGES, limit)
            src_url_base = args.category_url
            aggregate_stop_reason = _merge_stop_reason(aggregate_stop_reason, primary_reason)
            max_pages_hit = max_pages_hit or primary_reason == "max_pages_hit"
        elif args.listing_url:
            logger.info("Phase 1: discovering via listing %s", args.listing_url[:50])
            discovered_urls, primary_reason = _discover_urls_via_category(args.listing_url, MAX_PAGES, limit)
            src_url_base = args.listing_url
            aggregate_stop_reason = _merge_stop_reason(aggregate_stop_reason, primary_reason)
            max_pages_hit = max_pages_hit or primary_reason == "max_pages_hit"
        else:
            logger.error("No discovery method available")
            sys.exit(1)
        logger.info("Phase 1: discovered %d URLs", len(discovered_urls))
        _write_checkpoint(discovered_urls)

    if not discovered_urls:
        logger.error("Phase 1: discovered 0 URLs — nothing to extract")
        # Still emit output with coverage metadata
        output = {
            "site": SITE_URL,
            OUTPUT_KEY: [],
            "metadata": {
                "total_items": 0,
                "successful": 0,
                "failed": 0,
                "scraped_at": datetime.now(timezone.utc).isoformat(),
                "duration_seconds": round(time.time() - start_time, 1),
            },
        }
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = os.path.join(SCRIPT_DIR, f"output_{ts}.json")
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


            json.dump(output, f, indent=2, ensure_ascii=False)
        print(json.dumps(output, indent=2))
        return

    logger.info("Phase 1 complete: %d URLs discovered in %.1fs", len(discovered_urls), time.time() - start_time)

    if args.discover_only:
        logger.info("--discover-only: skipping Phase 2 extraction")
        output = {
            "site": SITE_URL,
            OUTPUT_KEY: [],
            "metadata": {
                "total_items": len(discovered_urls),
                "successful": 0,
                "failed": 0,
                "scraped_at": datetime.now(timezone.utc).isoformat(),
                "duration_seconds": round(time.time() - start_time, 1),
                "discovered_urls": discovered_urls,
            },
        }
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = os.path.join(SCRIPT_DIR, f"output_{ts}.json")
        with open(output_file, "w") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
        print(json.dumps(output, indent=2))
        return

    # ── Phase 2: Extract items concurrently ─────────────────────────────
    phase2_start = time.time()
    items: list[dict] = [None] * len(discovered_urls)  # preserve order by index

    workers = min(PHASE2_WORKERS, len(discovered_urls))
    logger.info("Phase 2: extracting %d items with %d workers", len(discovered_urls), workers)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_extract_item_safe, url, src_url_base): idx
            for idx, url in enumerate(discovered_urls)
        }
        completed = 0
        for future in as_completed(futures):
            idx = futures[future]
            try:
                item = future.result()
                items[idx] = item
            except Exception as exc:
                logger.error("Phase 2: future failed for %s: %s", discovered_urls[idx][:60], exc)
                items[idx] = _error_item(discovered_urls[idx], src_url_base, str(exc))
            completed += 1
            if completed % 50 == 0 or completed == len(discovered_urls):
                logger.info("Phase 2: %d/%d items extracted (%.1fs)", completed, len(discovered_urls), time.time() - phase2_start)

    # ── Filter: drop items with no title ────────────────────────────────
    valid_items = []
    error_items = []
    for item in items:
        if not item:
            continue
        if item.get("remarks", "").startswith("Error:"):
            error_items.append(item)
            continue
        if not item.get("title"):
            # Check soft 404
            if "Soft 404" in item.get("remarks", ""):
                error_items.append(item)
                continue
            # Drop items without title and without core fields
            if not any(item.get(f) for f in CORE_FILTER_FIELDS):
                logger.debug("Phase 2: dropping item without title: %s", item.get("url", "")[:60])
                continue
        valid_items.append(item)

    logger.info("Phase 2: %d valid, %d errors, %d dropped",
                len(valid_items), len(error_items),
                len(items) - len(valid_items) - len(error_items))

    phase2_duration = time.time() - phase2_start
    total_duration = time.time() - start_time

    # ── Output ──────────────────────────────────────────────────────────
    output = {
        "site": SITE_URL,
        OUTPUT_KEY: valid_items,
        "metadata": {
            "total_items": len(valid_items),
            "successful": len(valid_items),
            "failed": len(error_items),
            "discovered_urls": len(discovered_urls),
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": round(total_duration, 1),
            "phase2_duration_seconds": round(phase2_duration, 1),
            "discovery_coverage": {
                "ran_phase1": ran_phase1,
                "skipped_reason": skipped_reason,
                "stop_reason": aggregate_stop_reason,
                "max_pages_hit": max_pages_hit,
                "expected_total": COVERAGE_TARGET_TOTAL,
                "discovered_count": len(discovered_urls),
            },
        },
    }

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = os.path.join(SCRIPT_DIR, f"output_{ts}.json")
    with open(output_file, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    logger.info("Done: %d jobs written to %s (%.1fs total)",
                len(valid_items), output_file, total_duration)
    print(json.dumps(output, indent=2, ensure_ascii=False)[:5000])


if __name__ == "__main__":
    main()
