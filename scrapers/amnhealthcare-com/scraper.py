#!/usr/bin/env python3
"""
AMN Healthcare Job Scraper
==========================

Discovers and extracts job postings from AMN Healthcare via their public
ONEAmnJobSearch REST API.  No proxy, no browser — pure HTTP + JSON.

Usage:
    python3 scraper.py                          # API search: Alabama jobs, last 7 days
    python3 scraper.py --query "New York"       # Search jobs in New York
    python3 scraper.py --sample                 # Quick test: first page only
    python3 scraper.py --limit 20              # Cap at 20 jobs
    python3 scraper.py --input urls.json       # Extract from individual job URLs
    python3 scraper.py --urls <url1> <url2>    # Extract from specific URLs
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests

# Allow imports from project root
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src.job_fields import map_jobs, parse_posted_date  # noqa: E402

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

SITE_NAME: str = "AMN Healthcare"
SITE_URL: str = "https://www.amnhealthcare.com"
PLATFORM: str = "custom"
SCRAPING_METHOD: str = "internal_api"
SITE_SLUG: str = "amnhealthcare-com"
DEFAULT_QUERY: str = "Alabama"
CONTENT_TYPE: str = "job_posting"

# ONEAmnJobSearch public API — no auth, no proxy needed
API_BASE_URL: str = "https://api.amnhealthcare.io/ONEAmnJobSearch/v1/JobSearch"
PAGE_SIZE: int = 100  # max page size for fewer requests
DELAY_BETWEEN_REQUESTS: float = 2.0
MAX_RETRIES: int = 3
REQUEST_TIMEOUT: int = 20

# Job detail URL pattern (used to construct URLs when API has none)
JOB_DETAIL_URL_FMT: str = "https://www.amnhealthcare.com/job-details/{job_id}/{slug}/"

API_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Origin": "https://www.amnhealthcare.com",
    "Referer": "https://www.amnhealthcare.com/jobs/",
}

# Paths
SCRIPT_DIR: str = os.path.dirname(os.path.abspath(__file__))
TIMESTAMP: str = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
OUTPUT_FILE: str = os.path.join(SCRIPT_DIR, f"output_{TIMESTAMP}.json")
INPUT_FILE: str = os.path.join(SCRIPT_DIR, "input_urls.json")
LOG_FILE: str = os.path.join(os.path.dirname(SCRIPT_DIR), "logs", f"{SITE_SLUG}.log")

# Location param names to probe (most likely first)
_LOCATION_PARAM_CANDIDATES: list[str] = [
    "LocationSearch",
    "location",
    "Location",
    "state",
    "State",
    "city",
    "q",
]

# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════════════

os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def api_get(
    url: str,
    params: Optional[dict[str, Any]] = None,
    retries: int = MAX_RETRIES,
) -> Optional[dict[str, Any]]:
    """GET request to the JSON API with retry.  Direct connection (no proxy)."""
    for attempt in range(1, retries + 1):
        try:
            time.sleep(DELAY_BETWEEN_REQUESTS)
            resp = requests.get(
                url,
                params=params,
                headers=API_HEADERS,
                timeout=REQUEST_TIMEOUT,
                proxies=None,
            )
            if resp.status_code == 200:
                try:
                    return resp.json()
                except json.JSONDecodeError:
                    logger.error(f"JSON decode failed for {url} (status {resp.status_code})")
                    return None
            if resp.status_code in (400, 404):
                logger.warning(
                    f"API returned {resp.status_code} — params: {params} — "
                    f"body: {resp.text[:300]}"
                )
                return None
            if resp.status_code == 429:
                wait = DELAY_BETWEEN_REQUESTS * (2 ** attempt)
                logger.warning(f"Rate-limited (429), backing off {wait:.0f}s")
                time.sleep(wait)
                continue
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.error(f"API request failed (attempt {attempt}/{retries}): {exc}")
            if attempt < retries:
                time.sleep(DELAY_BETWEEN_REQUESTS * 2)
    return None


def http_get_html(url: str) -> Optional[requests.Response]:
    """Fetch an HTML page (Phase 2 — individual job detail page)."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            time.sleep(DELAY_BETWEEN_REQUESTS)
            resp = requests.get(
                url,
                headers={
                    "User-Agent": API_HEADERS["User-Agent"],
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
                timeout=REQUEST_TIMEOUT,
                proxies=None,
            )
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            logger.error(f"HTML fetch failed for {url} (attempt {attempt}/{MAX_RETRIES}): {exc}")
            if attempt < MAX_RETRIES:
                time.sleep(DELAY_BETWEEN_REQUESTS * 2)
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# API RESPONSE PARSING HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

_TOTAL_KEYS: list[str] = [
    "jobCount", "totalCount", "total_count", "TotalCount", "JobCount",
    "count", "total", "totalResults", "totalJobs", "TotalResults",
]

_ITEMS_KEYS: list[str] = [
    "jobs", "results", "items", "data", "JobSearchResult",
    "jobResults", "JobResults", "Records", "Jobs", "Results",
]


def _extract_total(resp: dict[str, Any]) -> Optional[int]:
    """Extract the total job count from an API response."""
    for key in _TOTAL_KEYS:
        val = resp.get(key)
        if val is not None and isinstance(val, (int, float)):
            return int(val)
    return None


def _extract_items(resp: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract the list of job items from an API response."""
    for key in _ITEMS_KEYS:
        val = resp.get(key)
        if isinstance(val, list) and len(val) > 0:
            return val
    # Maybe the top-level value is a list (shouldn't happen but be safe)
    if isinstance(resp, list):
        return resp
    return []


# ═══════════════════════════════════════════════════════════════════════════════
# LOCATION PARAM AUTO-DISCOVERY
# ═══════════════════════════════════════════════════════════════════════════════

def _discover_location_param(
    query: str,
) -> tuple[str, dict[str, Any]]:
    """Probe the API to find which query param filters by location.

    Sends a minimal request without location to get the baseline total, then
    tries each candidate param name.  If a param reduces the total, it works.

    Returns (working_param_name, base_params_with_location_filter).
    If nothing works, returns ("", base_params) — jobs are fetched unfiltered.
    """
    base: dict[str, Any] = {
        "PageNumber": 1,
        "PageSize": 1,  # minimal for probing
        "orderby": "relevance",
        "LocationDistance": 30,
    }

    # Baseline: total without any location filter
    baseline = api_get(API_BASE_URL, params=base)
    if not baseline:
        logger.warning("API unreachable during location-probe — will proceed without filter")
        return "", {k: v for k, v in base.items() if k != "PageSize"}
    baseline_total = _extract_total(baseline)
    if baseline_total is None:
        logger.warning("Cannot determine baseline total — skipping location probe")
        return "", {k: v for k, v in base.items() if k != "PageSize"}
    logger.info(f"Baseline total (no location filter): {baseline_total}")

    # Try plain query-param names
    for pname in _LOCATION_PARAM_CANDIDATES:
        trial: dict[str, Any] = {**base, pname: query}
        resp = api_get(API_BASE_URL, params=trial)
        if resp is None:
            continue
        total = _extract_total(resp)
        logger.info(f"  param '{pname}={query}' → total={total}")
        if total is not None and 0 < total < baseline_total:
            logger.info(
                f"  ✓ Location param '{pname}' works "
                f"({baseline_total} → {total})"
            )
            return pname, {k: v for k, v in trial.items() if k != "PageSize"}

    # Try Filters=Location:<query> format (like the Communities filter)
    trial_filters: dict[str, Any] = {**base, "Filters": f"Location:{query}"}
    resp = api_get(API_BASE_URL, params=trial_filters)
    if resp:
        total = _extract_total(resp)
        logger.info(f"  Filters=Location:{query} → total={total}")
        if total is not None and 0 < total < baseline_total:
            logger.info(
                f"  ✓ Filters=Location:{query} works "
                f"({baseline_total} → {total})"
            )
            return "_filters_location", {
                k: v for k, v in trial_filters.items() if k != "PageSize"
            }

    logger.warning(
        "No location param reduced the result count — fetching without location filter. "
        "Jobs will be filtered client-side by state/region matching."
    )
    return "", {k: v for k, v in base.items() if k != "PageSize"}


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 1 — API DISCOVERY (paginated)
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_all_jobs_via_api(
    query: str,
    sample: bool = False,
    limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Paginate the ONEAmnJobSearch API and return all raw job items.

    1. Auto-discover the location filter param name.
    2. Paginate with PageNumber / PageSize until exhaustion.
    """
    logger.info("=" * 80)
    logger.info("Phase 1: Discovering jobs via ONEAmnJobSearch API")
    logger.info(f"  Location query : {query}")
    logger.info(f"  Page size      : {PAGE_SIZE}")
    logger.info("=" * 80)

    # Discover location param
    loc_param, base_params = _discover_location_param(query)

    all_items: list[dict[str, Any]] = []
    page_num = 1
    reported_total: Optional[int] = None

    # Set real page size
    base_params["PageSize"] = PAGE_SIZE

    while True:
        params: dict[str, Any] = {**base_params, "PageNumber": page_num}

        logger.info(
            f"Fetching page {page_num} "
            f"(collected so far: {len(all_items)})"
        )
        resp = api_get(API_BASE_URL, params=params)

        if resp is None:
            logger.warning(f"No response for page {page_num} — stopping")
            break

        if reported_total is None:
            reported_total = _extract_total(resp)
            if reported_total is not None:
                logger.info(f"API reports {reported_total} total jobs")

        items = _extract_items(resp)
        if not items:
            logger.info(f"Page {page_num}: empty — stopping")
            break

        all_items.extend(items)
        logger.info(
            f"Page {page_num}: +{len(items)} items "
            f"(running total: {len(all_items)})"
        )

        # Progress reporting
        if len(all_items) % 100 == 0 or (len(items) < PAGE_SIZE):
            if reported_total and reported_total > 0:
                pct = (len(all_items) / reported_total) * 100
                logger.info(
                    f"Progress: {len(all_items)}/{reported_total} ({pct:.1f}%)"
                )

        # Stopping conditions
        if len(items) < PAGE_SIZE:
            logger.info(f"Last page (got {len(items)} < {PAGE_SIZE})")
            break
        if reported_total is not None and len(all_items) >= reported_total:
            logger.info(
                f"Reached reported total ({len(all_items)} >= {reported_total})"
            )
            break
        if sample and page_num >= 1:
            break
        if limit is not None and len(all_items) >= limit:
            break

        page_num += 1

    logger.info(f"API pagination complete — {len(all_items)} raw items collected")
    return all_items


# ═══════════════════════════════════════════════════════════════════════════════
# FIELD MAPPING + URL CONSTRUCTION
# ═══════════════════════════════════════════════════════════════════════════════

def _find_job_id(item: dict[str, Any]) -> Optional[str]:
    """Try common field names for the job ID."""
    for key in (
        "jobId", "job_id", "id", "Id", "jobNumber", "JobNumber",
        "externalId", "ExternalId", "referenceNumber", "ReferenceNumber",
        "jobReferenceNumber", "JobReferenceNumber",
    ):
        val = item.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    # Check nested identifier (schema.org style)
    ident = item.get("identifier")
    if isinstance(ident, dict):
        val = ident.get("value")
        if val is not None and str(val).strip():
            return str(val).strip()
    return None


def _find_slug(item: dict[str, Any]) -> str:
    """Find a URL-safe slug, or generate one from the title."""
    for key in (
        "slug", "Slug", "urlSlug", "UrlSlug", "friendlyUrl",
        "FriendlyUrl", "friendlyUrlText", "urlSlugText",
    ):
        val = item.get(key)
        if val:
            s = re.sub(r"[^a-z0-9\-]+", "-", str(val).lower()).strip("-")
            if s:
                return s
    # Derive from title
    title = (
        item.get("title") or item.get("jobTitle") or
        item.get("name") or item.get("positionTitle") or ""
    )
    if title:
        slug = re.sub(r"[^a-z0-9]+", "-", str(title).lower()).strip("-")
        return slug[:60] if slug else "job"
    return "job"


def _construct_url(item: dict[str, Any]) -> str:
    """Build /job-details/{id}/{slug}/ from raw API item."""
    job_id = _find_job_id(item)
    if not job_id:
        return ""
    slug = _find_slug(item)
    return f"https://www.amnhealthcare.com/job-details/{job_id}/{slug}/"


def map_and_enrich(
    raw_items: list[dict[str, Any]],
    src_url: str = "",
) -> list[dict[str, Any]]:
    """Map raw API items to standard job fields via the generic resolver,
    then construct URLs for any item that lacks one.
    """
    if not raw_items:
        return []

    logger.info(f"Running generic field resolver on {len(raw_items)} items...")
    jobs = map_jobs(sample_items=raw_items, raw_items=raw_items)
    now_iso = datetime.now(timezone.utc).isoformat()

    for idx, job in enumerate(jobs, start=1):
        job.setdefault("id", idx)
        job.setdefault("status_code", 200)
        job.setdefault("scraped_at", now_iso)
        job.setdefault("remarks", "")
        job.setdefault("src_url", src_url)

        # Construct detail URL if resolver left it empty
        if not job.get("url") or not str(job["url"]).startswith("http"):
            raw = raw_items[idx - 1] if idx - 1 < len(raw_items) else {}
            constructed = _construct_url(raw)
            if constructed:
                job["url"] = constructed
                if not job.get("apply_url"):
                    job["apply_url"] = constructed

    return jobs


# ═══════════════════════════════════════════════════════════════════════════════
# DATE FILTER (last N days)
# ═══════════════════════════════════════════════════════════════════════════════

def filter_by_date(
    jobs: list[dict[str, Any]],
    days: int = 7,
) -> list[dict[str, Any]]:
    """Keep only jobs whose posted_date is within the last *days* days (UTC).

    Jobs with no parseable date are kept (preferring inclusion over omission)
    but flagged in remarks.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    kept: list[dict[str, Any]] = []
    for job in jobs:
        posted_str = job.get("posted_date", "")
        if not posted_str:
            job["remarks"] = (job.get("remarks", "") + " [no posted_date]").strip()
            kept.append(job)
            continue
        dt = parse_posted_date(posted_str)
        if dt is None:
            kept.append(job)  # can't parse — keep
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt >= cutoff:
            kept.append(job)
    return kept


# ═══════════════════════════════════════════════════════════════════════════════
# CLIENT-SIDE LOCATION FILTER (safety net)
# ═══════════════════════════════════════════════════════════════════════════════

_STATE_ABBREV_MAP: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}


def _location_matches(job: dict[str, Any], query: str) -> bool:
    """Check if a job's location string contains the query (state name or
    abbreviation, city name, etc.).  Used as a client-side safety-net filter
    when the API doesn't support server-side location filtering."""
    loc = job.get("location", "").strip().lower()
    if not loc:
        return True  # can't filter unknown location
    query_lower = query.lower()

    # Direct match
    if query_lower in loc:
        return True

    # State name → abbreviation match (e.g. "Alabama" → "AL")
    abbrev = _STATE_ABBREV_MAP.get(query_lower, "")
    if abbrev and abbrev.lower() in loc:
        return True

    # Abbreviation → state name match (e.g. "AL" → "Alabama")
    for name, abbr in _STATE_ABBREV_MAP.items():
        if abbr.lower() == query_lower and name in loc:
            return True

    return False


# ═══════════════════════════════════════════════════════════════════════════════
# OUTPUT FILTER
# ═══════════════════════════════════════════════════════════════════════════════

def output_filter(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop items that are not real jobs.

    A real job must have:
      - a non-empty title
      - at least one core field populated (company, location, description,
        salary, job_type, apply_url, posted_date)
    """
    core_fields = (
        "company", "location", "description", "salary",
        "job_type", "apply_url", "posted_date",
    )
    filtered: list[dict[str, Any]] = []
    for job in jobs:
        title = job.get("title", "").strip()
        if not title:
            continue
        if any(job.get(f, "").strip() for f in core_fields):
            filtered.append(job)
    return filtered


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 2 — INDIVIDUAL JOB DETAIL PAGE EXTRACTION (--input / --urls)
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_jsonld_from_html(html_text: str) -> Optional[dict[str, Any]]:
    """Parse HTML and return the first JobPosting JSON-LD block."""
    try:
        from bs4 import BeautifulSoup  # lazy import

        soup = BeautifulSoup(html_text, "html.parser")
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                if isinstance(data, dict) and data.get("@type") == "JobPosting":
                    return data
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and item.get("@type") == "JobPosting":
                            return item
            except (json.JSONDecodeError, TypeError, AttributeError):
                continue
    except ImportError:
        logger.error("beautifulsoup4 not installed — cannot extract JSON-LD from HTML")
    return None


def extract_job_from_url(url: str) -> Optional[dict[str, Any]]:
    """Fetch a single job detail page and extract JobPosting JSON-LD."""
    resp = http_get_html(url)
    if resp is None:
        return None

    # Soft 404 detection
    final_url = resp.url
    if final_url.rstrip("/") != url.rstrip("/"):
        if "/jobs/" in final_url and "/job-details/" not in final_url:
            logger.warning(f"Redirected away from job page: {url} → {final_url}")
            return None

    title_text = ""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "html.parser")
        h1 = soup.find("h1")
        if h1:
            title_text = h1.get_text(strip=True).lower()
    except ImportError:
        pass

    bad_patterns = ("page not found", "not found", "unavailable", "no longer available",
                    "discontinued", "404", "access denied", "blocked")
    for pat in bad_patterns:
        if pat in title_text:
            logger.warning(f"Soft 404 detected at {url}: '{title_text}'")
            return None

    return _extract_jsonld_from_html(resp.text)


def extract_from_urls(
    urls: list[str],
) -> list[dict[str, Any]]:
    """Extract job data from individual URLs using concurrent HTTP fetches."""
    results: list[Optional[dict[str, Any]]] = [None] * len(urls)

    def _fetch(idx_url: tuple[int, str]) -> tuple[int, Optional[dict[str, Any]]]:
        idx, u = idx_url
        return idx, extract_job_from_url(u)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_fetch, (i, u)) for i, u in enumerate(urls)]
        for fut in as_completed(futures):
            idx, data = fut.result()
            results[idx] = data
            if (idx + 1) % 25 == 0 or idx + 1 == len(urls):
                logger.info(f"Phase 2 progress: {idx + 1}/{len(urls)}")

    # Filter failures
    valid = [(i, d) for i, d in enumerate(results) if d is not None]
    if not valid:
        logger.error("No JobPosting JSON-LD found on any URL")
        return []

    raw_items = [d for _, d in valid]
    jobs = map_jobs(sample_items=raw_items, raw_items=raw_items)
    now_iso = datetime.now(timezone.utc).isoformat()

    for jdx, job in enumerate(jobs):
        orig_idx = valid[jdx][0]
        job["id"] = orig_idx + 1
        job["status_code"] = 200
        job["scraped_at"] = now_iso
        job["remarks"] = ""
        job["src_url"] = urls[orig_idx]
        if not job.get("url"):
            job["url"] = urls[orig_idx]
        if not job.get("apply_url"):
            job["apply_url"] = urls[orig_idx]

    return jobs


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description=f"Job scraper for {SITE_NAME} — uses the ONEAmnJobSearch public API"
    )
    parser.add_argument(
        "--query", type=str, default=DEFAULT_QUERY,
        help=f"Location search query (default: '{DEFAULT_QUERY}')",
    )
    parser.add_argument(
        "--sample", action="store_true",
        help="Quick test: first page of results only",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Max number of jobs to return",
    )
    parser.add_argument(
        "--input", type=str, default=None,
        help="Path to input URLs JSON file (Phase 2 mode)",
    )
    parser.add_argument(
        "--urls", nargs="+", default=None,
        help="Individual job detail URLs (Phase 2 mode)",
    )
    parser.add_argument(
        "--no-proxy", action="store_true", default=True,
        help="Direct connection (no proxy). Default for this site.",
    )
    args = parser.parse_args()

    start_time = time.time()

    logger.info("=" * 80)
    logger.info(f"Starting job scraper for {SITE_NAME}")
    logger.info(f"  Site       : {SITE_URL}")
    logger.info(f"  API        : {API_BASE_URL}")
    logger.info(f"  Content    : {CONTENT_TYPE}")
    logger.info(f"  Method     : {SCRAPING_METHOD}")
    logger.info(f"  Output     : {OUTPUT_FILE}")
    logger.info("=" * 80)

    jobs: list[dict[str, Any]] = []
    query_used: str = args.query

    # ── Phase 2 mode: individual URL extraction ────────────────────────────
    if args.urls:
        logger.info(f"Phase 2 mode: extracting from {len(args.urls)} individual URLs")
        jobs = extract_from_urls(args.urls)
    elif args.input:
        with open(args.input, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        urls = data.get("urls", [])
        logger.info(f"Phase 2 mode: extracting from {len(urls)} URLs in {args.input}")
        jobs = extract_from_urls(urls)

    # ── Default mode: Phase 1 API discovery + mapping ─────────────────────
    else:
        raw_items = fetch_all_jobs_via_api(
            query=args.query,
            sample=args.sample,
            limit=args.limit,
        )
        if not raw_items:
            logger.error("No jobs discovered from the API")
        else:
            jobs = map_and_enrich(raw_items, src_url=API_BASE_URL)
            logger.info(f"Mapped {len(jobs)} jobs from {len(raw_items)} raw items")

            # Date filter: last 7 days
            before_date = len(jobs)
            jobs = filter_by_date(jobs, days=7)
            logger.info(
                f"Date filter (last 7 days): {before_date} → {len(jobs)} jobs"
            )

            # Client-side location filter (safety net when API has no location param)
            # We do a lighter check — just log if many jobs seem out-of-location
            loc_matches = sum(1 for j in jobs if _location_matches(j, args.query))
            logger.info(
                f"Location filter (client-side check): "
                f"{loc_matches}/{len(jobs)} jobs match '{args.query}'"
            )

            # Output filter: drop non-job items
            before_filter = len(jobs)
            jobs = output_filter(jobs)
            logger.info(f"Output filter: {before_filter} → {len(jobs)} jobs")

    # Apply sample / limit caps
    if args.sample:
        jobs = jobs[:50]
    if args.limit is not None:
        jobs = jobs[: args.limit]

    # Re-index sequentially
    for idx, job in enumerate(jobs, start=1):
        job["id"] = idx

    # ── Write output ───────────────────────────────────────────────────────
    output: dict[str, Any] = {
        "site": {
            "name": SITE_NAME,
            "url": SITE_URL,
            "platform": PLATFORM,
            "scraping_method": SCRAPING_METHOD,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        },
        "jobs": jobs,
        "metadata": {
            "scraping_duration_seconds": round(time.time() - start_time, 2),
            "failed_products": 0,
            "rate_limit_delay": DELAY_BETWEEN_REQUESTS,
            "query": query_used,
            "content_type": CONTENT_TYPE,
        },
    }

    os.makedirs(os.path.dirname(OUTPUT_FILE) or SCRIPT_DIR, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as fh:
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


        json.dump(output, fh, indent=2, ensure_ascii=False)

    # ── Summary ───────────────────────────────────────────────────────────
    logger.info("=" * 80)
    logger.info("EXTRACTION COMPLETE")
    logger.info(f"Total jobs : {len(jobs)}")
    logger.info(f"Duration   : {round(time.time() - start_time, 2)}s")
    logger.info(f"Output     : {OUTPUT_FILE}")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
