#!/usr/bin/env python3
"""
Aya Healthcare Job Scraper

Scrapes job postings from the Aya Healthcare public REST API.
All job data comes from https://api.ayahealthcare.com/AyaHealthcareWeb/job/search
No browser, no proxy, no anti-bot bypass needed.

Usage:
    python3 scraper_draft.py                         # full extraction, all jobs
    python3 scraper_draft.py --query "Alabama"        # filter by location
    python3 scraper_draft.py --sample                  # first 5 jobs (quick test)
    python3 scraper_draft.py --limit 50                # max 50 jobs
    python3 scraper_draft.py --input urls.json         # explicit input file (ignored for API mode)
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

# ── path for src imports ────────────────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from src.job_fields import map_jobs

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

SITE_NAME = "Aya Healthcare"
SITE_URL = "https://www.ayahealthcare.com"
PLATFORM = "custom (proprietary healthcare job board)"
SCRAPING_METHOD = "internal_api"
SITE_SLUG = "ayahealthcare-com"
CONTENT_TYPE = "job_posting"
OUTPUT_KEY = "jobs"

API_BASE_URL = "https://api.ayahealthcare.com"
API_ENDPOINT = "/AyaHealthcareWeb/job/search"
JOB_URL_TEMPLATE = "https://www.ayahealthcare.com/travel-nursing/jobs/{jobID}"

# Pagination
PAGE_SIZE = 500  # max page size to minimize API calls
DELAY_BETWEEN_REQUESTS = 0.5  # seconds between API calls
MAX_RETRIES = 3

# Date filter: only keep jobs posted within last N days
DAYS_FILTER = 7

# Default search query (location)
DEFAULT_QUERY = ""

# API headers — real browser UA, JSON accept
API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Origin": "https://www.ayahealthcare.com",
    "Referer": "https://www.ayahealthcare.com/travel-nursing/travel-nursing-jobs/",
}

# Paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TIMESTAMP = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
OUTPUT_FILE = os.path.join(SCRIPT_DIR, f"output_{TIMESTAMP}.json")
INPUT_FILE = os.path.join(SCRIPT_DIR, "input_urls.json")
LOG_FILE = os.path.join(os.path.dirname(SCRIPT_DIR), "logs", f"{SITE_SLUG}.log")

# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════════════

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

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
# API FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_api_page(
    params: dict,
    session: requests.Session,
) -> Optional[dict]:
    """Fetch one page from the Aya Healthcare job search API.

    Direct HTTP, no proxy, no anti-bot bypass. Returns parsed JSON or None.
    """
    url = f"{API_BASE_URL}{API_ENDPOINT}"

    for attempt in range(MAX_RETRIES):
        try:
            time.sleep(DELAY_BETWEEN_REQUESTS)
            response = session.get(
                url,
                params=params,
                headers=API_HEADERS,
                timeout=30,
            )
            if response.status_code == 200:
                return response.json()

            logger.warning(
                f"API returned {response.status_code} (attempt {attempt + 1}/{MAX_RETRIES})"
            )
            if response.status_code in (400, 401, 403, 404):
                logger.error(f"Terminal error {response.status_code}, not retrying")
                return None

        except requests.RequestException as e:
            logger.error(
                f"API request failed (attempt {attempt + 1}/{MAX_RETRIES}): {e}"
            )

        if attempt < MAX_RETRIES - 1:
            time.sleep(DELAY_BETWEEN_REQUESTS * 2 * (attempt + 1))

    return None


def discover_jobs(query: str = "", limit: Optional[int] = None) -> list[dict]:
    """Phase 1: Discover all jobs via paginated API calls.

    Paginates through all results using offset-based pagination.
    Returns list of raw API job objects.

    Args:
        query: Location search string (e.g. "Alabama"). Empty = all locations.
        limit: Optional cap on number of jobs to return.

    Returns:
        List of raw job dicts from the API.
    """
    session = requests.Session()
    all_jobs: list[dict] = []
    offset = 0
    page = 0
    reported_total: Optional[int] = None

    # Try to find the right location param name.
    # Common candidates: LocationSearch, location, state, State, city, q
    location_params_to_try = ["LocationSearch", "location", "state", "State", "city", "q"]

    # First, determine which location param works by testing with a small request
    working_location_param = None
    if query:
        logger.info(f"Testing location param for query: '{query}'")
        for loc_param in location_params_to_try:
            test_params = {
                loc_param: query,
                "limit": 1,
                "offset": 0,
            }
            test_data = fetch_api_page(test_params, session)
            if test_data is not None:
                jobs = _extract_job_list(test_data)
                total = _extract_total(test_data)
                if jobs or (total is not None and total > 0):
                    working_location_param = loc_param
                    logger.info(
                        f"Location param '{loc_param}' works: "
                        f"total={total}, first page jobs={len(jobs)}"
                    )
                    break
                # API returned 200 but 0 results — could be valid (no jobs in that location)
                # or could be wrong param. If total is explicitly 0, it's valid.
                if total == 0:
                    working_location_param = loc_param
                    logger.info(
                        f"Location param '{loc_param}' accepted (0 results for '{query}')"
                    )
                    break

        if working_location_param is None:
            logger.warning(
                f"No working location param found for '{query}'. "
                f"Falling back to no location filter."
            )

    while True:
        page += 1
        params: dict = {
            "limit": PAGE_SIZE,
            "offset": offset,
        }
        if query and working_location_param:
            params[working_location_param] = query

        logger.info(
            f"Fetching API page {page}: offset={offset}, limit={PAGE_SIZE}"
        )
        data = fetch_api_page(params, session)

        if data is None:
            logger.warning(f"No data returned for page {page}, stopping pagination")
            break

        jobs = _extract_job_list(data)
        total = _extract_total(data)

        if reported_total is None and total is not None:
            reported_total = total
            logger.info(f"API reports total jobs: {reported_total}")

        if not jobs:
            logger.info(f"Page {page}: 0 jobs, stopping pagination")
            break

        all_jobs.extend(jobs)
        logger.info(
            f"Page {page}: {len(jobs)} jobs (total collected: {len(all_jobs)})"
        )

        # Check progress
        if (len(all_jobs) % 50 == 0) or (len(jobs) < PAGE_SIZE):
            percent = (
                (len(all_jobs) / reported_total * 100)
                if reported_total and reported_total > 0
                else 0
            )
            logger.info(
                f"Progress: {len(all_jobs)} jobs collected "
                f"({percent:.1f}% of {reported_total or 'unknown'})"
            )

        # Stop conditions
        if len(jobs) < PAGE_SIZE:
            logger.info("Page returned fewer items than page size — exhausted")
            break

        if reported_total is not None and len(all_jobs) >= reported_total:
            logger.info(f"Reached API total ({reported_total}), stopping")
            break

        if limit and len(all_jobs) >= limit:
            logger.info(f"Reached user limit ({limit}), stopping")
            break

        offset += PAGE_SIZE

    logger.info(f"Discovery complete: {len(all_jobs)} jobs found")
    return all_jobs


def _extract_job_list(data: dict) -> list[dict]:
    """Extract the job list from an API response.

    The API may return jobs as a direct list, or nested under a key.
    """
    if isinstance(data, list):
        return data

    # Try common response structures
    for key in ("jobs", "data", "results", "items", "jobSearchResultItems", "jobSearchResults", "list"):
        if key in data:
            val = data[key]
            if isinstance(val, list):
                return val

    # If the response is a dict but none of the keys match, look for a list value
    for val in data.values():
        if isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict):
            # Check if first element looks like a job (has common job fields)
            first = val[0]
            job_indicators = {"jobID", "expertiseText", "professionText", "city", "state", "facilityName"}
            if any(k in first for k in job_indicators):
                return val

    return []


def _extract_total(data: dict) -> Optional[int]:
    """Extract the total count from an API response."""
    if isinstance(data, list):
        return len(data)

    for key in ("totalCount", "total_count", "count", "total", "totalResults",
                "totalJobs", "totalResultsCount", "numberOfResults", "recordCount"):
        val = data.get(key)
        if isinstance(val, int):
            return val

    return None


# ═══════════════════════════════════════════════════════════════════════════════
# FIELD MAPPING
# ═══════════════════════════════════════════════════════════════════════════════

def map_job_fields(raw_jobs: list[dict]) -> list[dict]:
    """Map raw API jobs to standard output fields using the generic resolver.

    Also constructs the URL from jobID and applies the date filter.
    """
    if not raw_jobs:
        return []

    # Use the first batch as samples for field inference, then map all items
    sample = raw_jobs[:min(len(raw_jobs), 20)]
    mapped = map_jobs(sample_items=sample, raw_items=raw_jobs)

    now_iso = datetime.now(timezone.utc).isoformat()
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=DAYS_FILTER)

    filtered: list[dict] = []
    for idx, job in enumerate(mapped, start=1):
        job.setdefault("id", idx)
        job.setdefault("status_code", 200)
        job.setdefault("scraped_at", now_iso)
        job.setdefault("remarks", "")

        # Construct URL from jobID if empty
        if not job.get("url"):
            # Try to get jobID from the original raw item
            raw = raw_jobs[idx - 1] if idx - 1 < len(raw_jobs) else {}
            job_id = raw.get("jobID") or ""
            if job_id:
                job["url"] = JOB_URL_TEMPLATE.format(jobID=job_id)

        # Set src_url to the jobs listing page
        if not job.get("src_url"):
            job["src_url"] = "https://www.ayahealthcare.com/travel-nursing/travel-nursing-jobs/"

        # Date filter: keep only jobs posted within DAYS_FILTER days
        posted_date_str = job.get("posted_date", "")
        if posted_date_str:
            try:
                # Handle various date formats
                posted = _parse_posted_date(posted_date_str)
                if posted and posted < cutoff_date:
                    continue  # too old, skip
            except Exception:
                pass  # if we can't parse the date, include the job

        # Title fallback: if title is empty, try composing from expertise + profession
        if not job.get("title") and idx - 1 < len(raw_jobs):
            raw = raw_jobs[idx - 1]
            expertise = raw.get("expertiseText", "") or ""
            profession = raw.get("professionText", "") or ""
            if expertise and profession:
                job["title"] = f"{expertise} - {profession}"
            elif expertise:
                job["title"] = expertise
            elif profession:
                job["title"] = profession

        # Location fallback: if location is empty, try composing from city + state
        if not job.get("location") and idx - 1 < len(raw_jobs):
            raw = raw_jobs[idx - 1]
            city = raw.get("city", "") or ""
            state_abbrev = raw.get("stateAbbrev", "") or raw.get("state", "") or ""
            if city and state_abbrev:
                job["location"] = f"{city}, {state_abbrev}"

        # Currency: Aya Healthcare is USD-only
        if not job.get("currency"):
            job["currency"] = "USD"

        filtered.append(job)

    return filtered


def _parse_posted_date(date_str: str) -> Optional[datetime]:
    """Parse a posted date string to a datetime object."""
    if not date_str:
        return None
    s = str(date_str).strip()
    if not s or s.lower() in ("null", "undefined"):
        return None

    # ISO-8601 formats
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        pass

    # Try common formats
    formats = [
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%m/%d/%y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue

    return None


# ═══════════════════════════════════════════════════════════════════════════════
# CONTENT-TYPE OUTPUT FILTER
# ═══════════════════════════════════════════════════════════════════════════════

def output_filter(jobs: list[dict]) -> list[dict]:
    """Drop items that lack title AND all core fields (other than title/url).

    For job_posting, core fields are: title, company, location, description, url.
    Keep items that have a title AND at least one other core field.
    """
    filtered = []
    for job in jobs:
        title = (job.get("title") or "").strip()
        # For jobs: need title + at least one of: company, location, description, salary
        other_fields = [
            (job.get("company") or "").strip(),
            (job.get("location") or "").strip(),
            (job.get("description") or "").strip(),
            (job.get("salary") or "").strip(),
            (job.get("job_type") or "").strip(),
        ]
        if title and any(f for f in other_fields):
            filtered.append(job)
    return filtered


# ═══════════════════════════════════════════════════════════════════════════════
# INPUT HANDLING
# ═══════════════════════════════════════════════════════════════════════════════

def load_urls_from_file(filepath: str) -> list[str]:
    """Load URLs from a JSON file."""
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("urls", [])


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description=f"Job scraper for {SITE_NAME}")
    parser.add_argument("--sample", action="store_true", help="Scrape only 5 jobs")
    parser.add_argument("--limit", type=int, default=None, help="Max jobs to scrape")
    parser.add_argument("--input", type=str, default=None, help="Path to input URLs JSON file")
    parser.add_argument("--urls", nargs="+", default=None, help="Job URLs as arguments")
    parser.add_argument(
        "--query", type=str, default=DEFAULT_QUERY,
        help="Location search query (e.g. 'Alabama')",
    )
    parser.add_argument("--no-proxy", action="store_true", default=True, help="No proxy (default)")
    args = parser.parse_args()

    start_time = time.time()

    logger.info("=" * 80)
    logger.info(f"Starting job scraper for {SITE_NAME}")
    logger.info(f"Site: {SITE_URL}")
    logger.info(f"API: {API_BASE_URL}{API_ENDPOINT}")
    logger.info(f"Output: {OUTPUT_FILE}")
    logger.info(f"Date filter: last {DAYS_FILTER} days")
    if args.query:
        logger.info(f"Location query: '{args.query}'")
    if args.sample:
        logger.info("Sample mode: limiting to 5 jobs")
    if args.limit:
        logger.info(f"User limit: {args.limit} jobs")
    logger.info("=" * 80)

    # Phase 1: Discover jobs via API
    query = args.query or DEFAULT_QUERY
    discovery_limit = None
    if args.sample:
        discovery_limit = 5
    elif args.limit:
        discovery_limit = args.limit

    raw_jobs = discover_jobs(query=query, limit=discovery_limit)

    if not raw_jobs:
        logger.warning("No jobs discovered from API")
        raw_jobs = []

    # Phase 2: Map fields using generic resolver
    logger.info(f"Mapping {len(raw_jobs)} raw jobs to standard fields...")
    mapped_jobs = map_job_fields(raw_jobs)

    # Apply output filter (keep only items with title + core fields)
    mapped_jobs = output_filter(mapped_jobs)
    logger.info(f"After output filter: {len(mapped_jobs)} jobs")

    # Re-index after filtering
    for idx, job in enumerate(mapped_jobs, start=1):
        job["id"] = idx

    # Apply sample/limit caps on mapped results
    if args.sample:
        mapped_jobs = mapped_jobs[:5]
    elif args.limit:
        mapped_jobs = mapped_jobs[: args.limit]

    elapsed = round(time.time() - start_time, 2)

    # Build output
    output = {
        "site": {
            "name": SITE_NAME,
            "url": SITE_URL,
            "platform": PLATFORM,
            "scraping_method": SCRAPING_METHOD,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        },
        OUTPUT_KEY: mapped_jobs,
        "metadata": {
            "scraping_duration_seconds": elapsed,
            "failed_products": 0,
            "rate_limit_delay": DELAY_BETWEEN_REQUESTS,
            "total_discovered": len(raw_jobs),
            "date_filter_days": DAYS_FILTER,
            "query": query,
        },
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
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

    logger.info("=" * 80)
    logger.info("EXTRACTION COMPLETE")
    logger.info(f"Total discovered: {len(raw_jobs)}")
    logger.info(f"After date filter + output filter: {len(mapped_jobs)}")
    logger.info(f"Duration: {elapsed}s")
    logger.info(f"Output: {OUTPUT_FILE}")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
