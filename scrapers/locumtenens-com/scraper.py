#!/usr/bin/env python3
"""
LocumTenens.com Job Scraper — Two-Phase HTTP Architecture

Phase 1: Submit QuickSearch form per specialty → paginate search results → collect job URLs
Phase 2: HTTP GET each job page → extract JSON-LD JobPosting + visible-text fields

Strategy: http_requests (no proxy, no anti-bot, server-rendered HTML with JSON-LD)
"""

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
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse

import requests
from bs4 import BeautifulSoup

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

SITE_NAME = "LocumTenens.com"
SITE_URL = "https://www.locumtenens.com"
PLATFORM = "Custom"
SITE_SLUG = "locumtenens-com"

OUTPUT_KEY = "jobs"
CONTENT_TYPE = "job_posting"
PROXY_TIER = "none"
CURRENCY = "USD"
DELAY_BETWEEN_REQUESTS = 1
MAX_PAGES = 50
PAGE_LOAD_TIMEOUT = 30
COVERAGE_TARGET_TOTAL: Optional[int] = None

# Phase 1: Form-search configuration
FORM_ACTION = "https://www.locumtenens.com/Resources/JobSearch/QuickSearch"
FORM_METHOD = "POST"
FORM_SELECT_NAME = "Specialties"
FORM_PAGE_URL = "https://www.locumtenens.com/job-search"
SEARCH_RESULTS_BASE = "https://www.locumtenens.com/Resources/JobSearch/SearchResults"

# Job URL pattern: /{specialty}-jobs/{role}/{state}/job-{id}
ITEM_URL_PATTERN = re.compile(r"/job-\d+(?:[/?#]|$)")

# Checkpoint path
_CHECKPOINT_PATH = os.path.join(SCRIPT_DIR, "discovered_urls_checkpoint.json")

# Thread-local sessions for Phase 2 concurrency
_tls = threading.local()

# HTTP headers
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Output filter: drop items without title + at least one core field
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
# SESSION / HTTP HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(DEFAULT_HEADERS)
    return s


def _get_session() -> requests.Session:
    """Thread-local session for concurrent Phase 2 extraction."""
    if not hasattr(_tls, "session"):
        _tls.session = _make_session()
    return _tls.session


# ═══════════════════════════════════════════════════════════════════════════════
# CHECKPOINT
# ═══════════════════════════════════════════════════════════════════════════════

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
# DISCOVERY STATE (contract §1/§2)
# ═══════════════════════════════════════════════════════════════════════════════

def _new_discovery_state() -> dict:
    return {
        "stop_reason": "no_next_link",
        "max_pages_hit": False,
        "dimensions_iterated": 0,
        "dimensions_total": 0,
    }


def _set_stop_reason(state: dict, reason: str) -> None:
    if state["stop_reason"] == "navigate_error" and reason != "navigate_error":
        return
    state["stop_reason"] = reason


def _is_rate_limited(status: int) -> bool:
    """Check if HTTP status indicates rate-limiting or server error."""
    return status in (429, 502, 503) or status >= 500


# ═══════════════════════════════════════════════════════════════════════════════
# URL HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _normalize_href(href: str, base_url: str) -> str:
    """Resolve a relative href to an absolute URL."""
    if not href:
        return ""
    if href.startswith("/"):
        return SITE_URL.rstrip("/") + href
    if not href.startswith("http"):
        return urljoin(base_url, href)
    return href


def _set_query_param(url: str, param: str, value) -> str:
    """Replace a query param in the URL (not append)."""
    p = urlparse(url)
    qs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k != param]
    qs.append((param, str(value)))
    return urlunparse(p._replace(query=urlencode(qs)))


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 1: URL DISCOVERY
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_item_links(html: str, base_url: str) -> list[str]:
    """Extract job page URLs from search results HTML."""
    soup = BeautifulSoup(html, "html.parser")
    links: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = _normalize_href(a["href"], base_url)
        if ITEM_URL_PATTERN.search(href) and href not in seen:
            links.append(href)
            seen.add(href)
    return links


def _get_next_page_url(html: str, current_url: str, next_page_num: int) -> Optional[str]:
    """Determine the URL for the next page of search results.

    Checks for explicit next links (rel=next, "Next" text) in the HTML first.
    Falls back to None if no pagination link is found.
    """
    soup = BeautifulSoup(html, "html.parser")

    # 1. Explicit rel="next" link
    for a in soup.find_all("a", attrs={"rel": "next"}):
        href = _normalize_href(a.get("href", ""), current_url)
        if href:
            return href

    # 2. Next-button text links
    next_texts = {"next", "next page", ">", "»", "›", "next »"}
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True).lower()
        if text in next_texts:
            href = _normalize_href(a["href"], current_url)
            if href:
                return href

    # 3. Semantic CSS selectors
    for sel in ("a.next", "li.next a", ".pagination .next a",
                "[aria-label*='next' i] a", "a[aria-label*='next' i]"):
        try:
            el = soup.select_one(sel)
            if el and el.get("href"):
                return _normalize_href(el["href"], current_url)
        except Exception:
            pass

    return None


def _get_form_specialties(session: requests.Session) -> tuple[list[str], dict]:
    """GET the form page and extract specialty options + hidden form fields."""
    # Try multiple candidate form page URLs
    candidate_urls = [
        FORM_PAGE_URL,
        "https://www.locumtenens.com/Resources/JobSearch/QuickSearch",
        "https://www.locumtenens.com/Resources/JobSearch",
        SITE_URL,
    ]

    for page_url in candidate_urls:
        try:
            resp = session.get(page_url, timeout=PAGE_LOAD_TIMEOUT)
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "html.parser")

            # Find the select with the specialty options
            select = soup.find("select", {"name": FORM_SELECT_NAME})
            if not select:
                # Try finding any select that contains specialty-like options
                for sel in soup.find_all("select"):
                    if sel.get("name", "").lower() in ("specialties", "specialty"):
                        select = sel
                        break

            if not select:
                continue

            options = []
            for opt in select.find_all("option"):
                val = opt.get("value", "").strip()
                if val:
                    options.append(val)

            if not options:
                continue

            # Extract hidden form fields from the form
            hidden: dict[str, str] = {}
            form = select.find_parent("form") or soup.find("form")
            if form:
                for inp in form.find_all("input", {"type": "hidden"}):
                    name = inp.get("name", "")
                    val = inp.get("value", "")
                    if name:
                        hidden[name] = val

            logger.info("Form page: found %d specialty options, %d hidden fields at %s",
                        len(options), len(hidden), page_url)
            return options, hidden

        except Exception as exc:
            logger.debug("Form page %s failed: %s", page_url, exc)
            continue

    logger.warning("Could not find specialty select on any form page")
    return [], {}


def _discover_urls_via_form_search(
    session: requests.Session,
    state: dict,
    max_pages: Optional[int] = None,
    limit: Optional[int] = None,
) -> list[str]:
    """Phase 1: Discover job URLs by submitting the QuickSearch form per specialty.

    Iterates through ALL <select> options, submits once per option, paginates,
    and deduplicates. The form REQUIRES a selection (returns 500 without one).
    """
    all_urls: list[str] = []
    seen: set[str] = set()

    # Get form options + hidden fields
    specialties, hidden_fields = _get_form_specialties(session)

    if not specialties:
        logger.warning("No specialty options found; trying direct search results URL")
        # Fallback: try direct GET to search results page
        for fallback_url in [SEARCH_RESULTS_BASE,
                             "https://www.locumtenens.com/Resources/JobSearch/SearchResults?sId=70516297"]:
            try:
                resp = session.get(fallback_url, timeout=PAGE_LOAD_TIMEOUT)
                if resp.status_code == 200:
                    page_urls = _extract_item_links(resp.text, str(resp.url))
                    new = [u for u in page_urls if u not in seen]
                    all_urls.extend(new)
                    seen.update(new)
                    if all_urls:
                        logger.info("Fallback search found %d URLs", len(all_urls))
                        break
            except Exception as exc:
                logger.warning("Fallback search %s failed: %s", fallback_url[:60], exc)

        if not all_urls:
            _set_stop_reason(state, "navigate_error")
        _write_checkpoint(all_urls)
        return list(dict.fromkeys(all_urls))

    state["dimensions_total"] = len(specialties)
    dim_idx = 0

    for specialty in specialties:
        dim_idx += 1
        try:
            # Build form data
            form_data = dict(hidden_fields)
            form_data[FORM_SELECT_NAME] = specialty

            logger.info("Phase 1 [%d/%d]: searching specialty '%s'",
                        dim_idx, len(specialties), specialty)

            resp = session.post(
                FORM_ACTION, data=form_data,
                timeout=PAGE_LOAD_TIMEOUT, allow_redirects=True,
            )

            if _is_rate_limited(resp.status_code):
                logger.warning("Phase 1: POST returned %d for '%s'", resp.status_code, specialty)
                _set_stop_reason(state, "navigate_error")
                time.sleep(DELAY_BETWEEN_REQUESTS * 2)
                continue

            if resp.status_code != 200:
                logger.warning("Phase 1: POST returned %d for '%s'", resp.status_code, specialty)
                continue

            final_url = str(resp.url)
            page_html = resp.text

            # Extract links from first page
            page_urls = _extract_item_links(page_html, final_url)
            new = [u for u in page_urls if u not in seen]
            all_urls.extend(new)
            seen.update(new)
            logger.info("Phase 1 [%d]: '%s' → %d items (%d new)",
                        dim_idx, specialty, len(page_urls), len(new))

            # Paginate through all result pages
            current_page = 1
            while True:
                if max_pages and current_page >= max_pages:
                    state["max_pages_hit"] = True
                    _set_stop_reason(state, "max_pages_hit")
                    break
                if limit and len(all_urls) >= limit:
                    _set_stop_reason(state, "max_pages_hit")
                    break

                next_url = _get_next_page_url(page_html, final_url, current_page + 1)
                if not next_url:
                    break

                time.sleep(DELAY_BETWEEN_REQUESTS)
                try:
                    resp2 = session.get(next_url, timeout=PAGE_LOAD_TIMEOUT)
                except Exception as exc:
                    logger.warning("Phase 1: pagination fetch failed: %s", exc)
                    _set_stop_reason(state, "navigate_error")
                    break

                if _is_rate_limited(resp2.status_code):
                    _set_stop_reason(state, "navigate_error")
                    break

                page_html = resp2.text
                final_url = str(resp2.url)
                page_urls = _extract_item_links(page_html, final_url)
                new = [u for u in page_urls if u not in seen]

                if not new:
                    _set_stop_reason(state, "no_new_items")
                    break

                all_urls.extend(new)
                seen.update(new)
                logger.info("Phase 1 [%d]: '%s' page %d → %d new",
                            dim_idx, specialty, current_page + 1, len(new))
                current_page += 1

            _write_checkpoint(all_urls)

            if limit and len(all_urls) >= limit:
                break

        except Exception as exc:
            logger.error("Phase 1 [%d]: specialty '%s' failed: %s", dim_idx, specialty, exc)
            _set_stop_reason(state, "navigate_error")
            continue

        time.sleep(DELAY_BETWEEN_REQUESTS)

    state["dimensions_iterated"] = dim_idx

    if limit:
        all_urls = all_urls[:limit]

    unique = list(dict.fromkeys(all_urls))
    logger.info("Phase 1: Discovered %d total job URLs across %d specialties",
                len(unique), dim_idx)
    return unique


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 2: ITEM EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════════

# Code Writer adapted: extraction logic for JSON-LD JobPosting + visible text

def _strip_html(text: str) -> str:
    """Remove HTML tags and normalize whitespace."""
    if not text:
        return ""
    soup = BeautifulSoup(text, "html.parser")
    return soup.get_text(separator=" ", strip=True)


def _parse_json_ld(html: str) -> list[dict]:
    """Parse all JSON-LD script blocks from HTML."""
    soup = BeautifulSoup(html, "html.parser")
    blocks: list[dict] = []
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            raw = script.string or script.get_text() or ""
            data = json.loads(raw)
            # JSON-LD can be a single object, a list, or @graph
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        blocks.append(item)
            elif isinstance(data, dict):
                # Handle @graph
                graph = data.get("@graph")
                if isinstance(graph, list):
                    for item in graph:
                        if isinstance(item, dict):
                            blocks.append(item)
                else:
                    blocks.append(data)
        except (json.JSONDecodeError, TypeError):
            continue
    return blocks


def _extract_job_details(soup: BeautifulSoup) -> dict:
    """Extract job details from the visible-text job details table/section.

    Parses fields like Job Type, Specialty, Board Certification, On Call,
    Compensation, Telemedicine, Government from table rows or text patterns.
    """
    details: dict[str, str] = {}

    detail_fields = {
        "job type": "job_type",
        "specialty": "specialty",
        "board certification": "board_certification",
        "on call": "on_call",
        "compensation": "compensation",
        "telemedicine": "telemedicine",
        "government": "government",
    }

    # Approach 1: Parse table rows (label cell + value cell)
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) >= 2:
            label = cells[0].get_text(strip=True).rstrip(":").strip().lower()
            value = cells[1].get_text(strip=True)
            if label in detail_fields and value:
                details[detail_fields[label]] = value

    # Approach 2: Parse dt/dd pairs
    if not details:
        for dt in soup.find_all("dt"):
            dd = dt.find_next_sibling("dd")
            if dd:
                label = dt.get_text(strip=True).rstrip(":").strip().lower()
                value = dd.get_text(strip=True)
                if label in detail_fields and value:
                    details[detail_fields[label]] = value

    # Approach 3: Regex on full text for "Field: value" patterns
    if not details:
        full_text = soup.get_text(separator="\n")
        all_labels = set(detail_fields.keys()) | {"location", "title"}
        for label, key in detail_fields.items():
            pattern = re.compile(
                rf'{re.escape(label)}\s*:\s*(.+)',
                re.IGNORECASE,
            )
            match = pattern.search(full_text)
            if match:
                val = match.group(1).strip()
                # Skip if value is actually another field's label (e.g. "Telemedicine:")
                val_lower = val.lower().rstrip(":")
                if val_lower in all_labels:
                    continue
                details[key] = val

    return details


def _extract_item_data(session: requests.Session, url: str, src_url: str) -> dict:
    """Phase 2: Extract structured data from a single job page via HTTP."""
    item: dict = {
        "url": url,
        "src_url": src_url,
        "status_code": 0,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": "",
    }

    try:
        resp = session.get(url, timeout=PAGE_LOAD_TIMEOUT)
        item["status_code"] = resp.status_code
    except Exception as exc:
        logger.error("Phase 2: Failed to fetch %s: %s", url[:80], exc)
        item["remarks"] = f"Error: {str(exc)[:200]}"
        return item

    html = resp.text
    final_url = str(resp.url)

    # ── Soft 404 detection ─────────────────────────────────────────────
    if resp.status_code == 200:
        soup_check = BeautifulSoup(html, "html.parser")
        title_tag = soup_check.find("title")
        page_title = title_tag.get_text(strip=True).lower() if title_tag else ""
        h1_el = soup_check.find("h1")
        h1_text = h1_el.get_text(strip=True).lower() if h1_el else ""

        soft_404_markers = [
            "not found", "no longer available", "unavailable",
            "discontinued", "page not found", "expired", "removed",
            "job has been filled", "position closed",
        ]
        if any(m in page_title for m in soft_404_markers) or \
           any(m in h1_text for m in soft_404_markers):
            item["remarks"] = "Soft 404: job not found"
            return item

        # Check if final URL redirected away from the job page
        if final_url != url and not ITEM_URL_PATTERN.search(final_url):
            item["remarks"] = f"Soft 404: redirected to {final_url[:100]}"
            return item

    # ── Parse JSON-LD JobPosting ───────────────────────────────────────
    json_ld_blocks = _parse_json_ld(html)
    job_posting: Optional[dict] = None
    for block in json_ld_blocks:
        btype = block.get("@type", "")
        if isinstance(btype, list):
            if "JobPosting" in btype:
                job_posting = block
                break
        elif btype == "JobPosting":
            job_posting = block
            break

    soup = BeautifulSoup(html, "html.parser")

    if job_posting:
        # Primary fields (field map: title, description)
        item["title"] = job_posting.get("title", "")
        item["description"] = _strip_html(job_posting.get("description", ""))

        # Company
        org = job_posting.get("hiringOrganization", {})
        if isinstance(org, dict):
            item["company"] = org.get("name", "")

        # Location
        loc = job_posting.get("jobLocation", {})
        if isinstance(loc, dict):
            addr = loc.get("address", {})
            if isinstance(addr, dict):
                locality = addr.get("addressLocality", "")
                region = addr.get("addressRegion", "")
                country = addr.get("addressCountry", "")
                parts = [p for p in [locality, region] if p]
                item["location"] = ", ".join(parts) if parts else ""
                if country:
                    item["country"] = str(country)

        # Employment type / dates / industry
        emp_type = job_posting.get("employmentType", "")
        if emp_type:
            item["employment_type"] = str(emp_type)

        date_posted = job_posting.get("datePosted", "")
        if date_posted:
            item["date_posted"] = str(date_posted)

        valid_through = job_posting.get("validThrough", "")
        if valid_through:
            item["valid_through"] = str(valid_through)

        industry = job_posting.get("industry", "")
        if industry:
            item["industry"] = str(industry)

        # Identifier
        identifier = job_posting.get("identifier", {})
        if isinstance(identifier, dict):
            job_id = identifier.get("value", "")
            if job_id:
                item["job_id"] = str(job_id)
    else:
        # No JobPosting JSON-LD found — likely not a job page
        logger.warning("Phase 2: No JobPosting JSON-LD for %s", url[:80])

    # ── Fallback: h1 title if JSON-LD title is empty ───────────────────
    if not item.get("title"):
        h1_el = soup.find("h1")
        if h1_el:
            item["title"] = h1_el.get_text(strip=True)

    # ── Extract job_type + other details from visible text ─────────────
    details = _extract_job_details(soup)
    if "job_type" in details:
        item["job_type"] = details["job_type"]
    elif item.get("employment_type"):
        item["job_type"] = item["employment_type"]

    # Add other visible-text details
    for key, val in details.items():
        if key != "job_type" and val and key not in item:
            item[key] = val

    # ── Final soft-404 check: no title means not a valid job page ──────
    if not item.get("title") and not job_posting:
        item["remarks"] = "No job data found on page"

    return item


def _error_item(url: str, src_url: str, error: str) -> dict:
    return {
        "url": url,
        "src_url": src_url,
        "status_code": 0,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": f"Error: {error[:200]}",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description=f"{SITE_NAME} Job Scraper")
    parser.add_argument("--query", type=str, default="All Jobs",
                        help="Search query for navigation mode")
    parser.add_argument("--input", type=str,
                        help="Path to input URLs JSON file (takes precedence over checkpoint)")
    parser.add_argument("--urls", nargs="+", help="Job URLs as CLI arguments")
    parser.add_argument("--sample", action="store_true",
                        help="Scrape first 5 items only (uses input_urls.json)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max items to scrape")
    parser.add_argument("--no-proxy", action="store_true", default=True,
                        help="Disable proxy (default for this site)")
    parser.add_argument("--discover-only", action="store_true",
                        help="Run Phase 1 discovery only; skip Phase 2 extraction")
    parser.add_argument("--fresh-discovery", action="store_true",
                        help="Ignore existing checkpoint; run Phase 1 from scratch")
    parser.add_argument("--category-url", type=str,
                        help="Category URL to crawl")
    parser.add_argument("--listing-url", type=str,
                        help="Listing page URL to paginate")
    args = parser.parse_args()

    limit = 5 if args.sample else args.limit

    start_time = time.time()
    discovered_urls: list[str] = []
    items: list[dict] = []
    discovery_state = _new_discovery_state()
    ran_phase1 = False
    skipped_reason: Optional[str] = None
    src_url_base = SITE_URL

    session = _make_session()

    # ── Determine URLs to scrape ───────────────────────────────────────
    # Priority: --input > --urls > --sample > checkpoint > Phase 1 discovery

    if args.input:
        # --input takes PRECEDENCE over checkpoint
        input_path = args.input
        if not os.path.isabs(input_path):
            input_path = os.path.join(SCRIPT_DIR, input_path)
        try:
            with open(input_path) as f:
                data = json.load(f)
            discovered_urls = data.get("urls", [])
            skipped_reason = "input_file"
            logger.info("Loaded %d URLs from --input %s", len(discovered_urls), input_path)
        except Exception as exc:
            logger.error("Failed to load input file: %s", exc)

    elif args.urls:
        discovered_urls = list(args.urls)
        skipped_reason = "cli_urls"
        logger.info("Loaded %d URLs from --urls", len(discovered_urls))

    elif args.sample:
        # Sample mode: use input_urls.json (skip Phase 1 discovery)
        input_path = os.path.join(SCRIPT_DIR, "input_urls.json")
        try:
            with open(input_path) as f:
                data = json.load(f)
            discovered_urls = data.get("urls", [])
            skipped_reason = "sample"
            logger.info("Sample mode: loaded %d URLs from input_urls.json", len(discovered_urls))
        except Exception as exc:
            logger.error("Failed to load sample URLs: %s", exc)

    else:
        # Default: Phase 1 discovery via form search
        # Check for checkpoint first (unless --fresh-discovery)
        checkpoint_urls = [] if args.fresh_discovery else _load_checkpoint()
        if checkpoint_urls:
            discovered_urls = checkpoint_urls
            skipped_reason = "checkpoint_loaded"
        else:
            ran_phase1 = True
            if args.listing_url:
                src_url_base = args.listing_url
                logger.info("Phase 1: discovering via listing %s", args.listing_url[:60])
                try:
                    resp = session.get(args.listing_url, timeout=PAGE_LOAD_TIMEOUT)
                    if resp.status_code == 200:
                        page_html = resp.text
                        final_url = str(resp.url)
                        discovered_urls = _extract_item_links(page_html, final_url)
                        current_page = 1
                        while True:
                            if MAX_PAGES and current_page >= MAX_PAGES:
                                break
                            next_url = _get_next_page_url(page_html, final_url, current_page + 1)
                            if not next_url:
                                break
                            time.sleep(DELAY_BETWEEN_REQUESTS)
                            resp2 = session.get(next_url, timeout=PAGE_LOAD_TIMEOUT)
                            page_html = resp2.text
                            final_url = str(resp2.url)
                            new_urls = _extract_item_links(page_html, final_url)
                            seen_set = set(discovered_urls)
                            new = [u for u in new_urls if u not in seen_set]
                            if not new:
                                break
                            discovered_urls.extend(new)
                            current_page += 1
                except Exception as exc:
                    logger.error("Listing URL discovery failed: %s", exc)
                    _set_stop_reason(discovery_state, "navigate_error")
            elif args.category_url:
                src_url_base = args.category_url
                logger.info("Phase 1: discovering via category %s", args.category_url[:60])
                try:
                    resp = session.get(args.category_url, timeout=PAGE_LOAD_TIMEOUT)
                    if resp.status_code == 200:
                        discovered_urls = _extract_item_links(resp.text, str(resp.url))
                except Exception as exc:
                    logger.error("Category discovery failed: %s", exc)
                    _set_stop_reason(discovery_state, "navigate_error")
            else:
                src_url_base = FORM_ACTION
                discovered_urls = _discover_urls_via_form_search(
                    session, discovery_state, MAX_PAGES, limit,
                )
            _write_checkpoint(discovered_urls)

    if not ran_phase1:
        _set_stop_reason(discovery_state, "skipped")

    if args.discover_only:
        logger.info("--discover-only: skipping Phase 2 extraction")

    if not discovered_urls:
        logger.warning("No item URLs discovered (stop_reason=%s)",
                       discovery_state["stop_reason"])

    # ── Phase 2: Extract data from each URL ────────────────────────────
    if discovered_urls and not args.discover_only:
        total = len(discovered_urls)
        logger.info("Phase 2: Extracting data from %d items", total)

        # Concurrent extraction with ThreadPoolExecutor (HTTP-based Phase 2)
        max_workers = min(8, total)
        results: list[Optional[dict]] = [None] * total

        def _extract_wrapper(idx_url: tuple[int, str]) -> tuple[int, dict]:
            idx, url = idx_url
            try:
                tls_session = _get_session()
                return idx, _extract_item_data(tls_session, url, src_url_base)
            except Exception as exc:
                logger.error("Failed to extract %s: %s", url[:80], exc)
                return idx, _error_item(url, src_url_base, str(exc))

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_extract_wrapper, (i, url)): i
                for i, url in enumerate(discovered_urls)
            }
            done_count = 0
            for future in as_completed(futures):
                idx, item = future.result()
                results[idx] = item
                done_count += 1
                if done_count % 10 == 0 or done_count == total:
                    logger.info("Progress: %d/%d (%.0f%%)",
                                done_count, total, done_count / total * 100)

        items = [r for r in results if r is not None]

    # ── Output filter: drop extraction failures ────────────────────────
    _extra = [f for f in CORE_FILTER_FIELDS if f and f != "title"]
    _before = len(items)
    items = [
        it for it in items
        if it.get("title") and (not _extra or any(it.get(f) for f in _extra))
    ]
    if len(items) != _before:
        logger.info("Output filter: %d → %d items (dropped %d without core fields)",
                    _before, len(items), _before - len(items))

    # ── Discovery-coverage block ───────────────────────────────────────
    discovery_coverage = {
        "stop_reason": discovery_state["stop_reason"],
        "found": len(items),
        "discovered_urls": len(discovered_urls),
        "expected_total": COVERAGE_TARGET_TOTAL,
        "dimensions_iterated": discovery_state["dimensions_iterated"],
        "dimensions_total": discovery_state["dimensions_total"],
        "max_pages_hit": discovery_state["max_pages_hit"],
        "ran_phase1": ran_phase1,
        "skipped_reason": skipped_reason,
    }

    output = {
        "site": {
            "name": SITE_NAME,
            "url": SITE_URL,
            "platform": PLATFORM,
            "scraping_method": "http_requests",
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        },
        OUTPUT_KEY: items,
        "metadata": {
            "scraping_duration_seconds": round(time.time() - start_time, 1),
            "discovered_urls": len(discovered_urls),
            "extracted_items": len(items),
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

    logger.info(
        "Done: %d/%d items in %.1fs (stop_reason=%s) → %s",
        len([i for i in items if i.get("title")]),
        len(discovered_urls),
        time.time() - start_time,
        discovery_state["stop_reason"],
        output_filename,
    )


if __name__ == "__main__":
    main()
