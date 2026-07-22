#!/usr/bin/env python3
"""
AMN Healthcare Job Scraper

Discovers jobs via the public ONEAmnJobSearch REST API and extracts
structured data from each job object.  Iterates over all Communities
(categories) for full catalog coverage.

Usage:
    python3 scraper_draft.py                          # full extraction (all communities)
    python3 scraper_draft.py --sample                 # first 5 jobs (quick test)
    python3 scraper_draft.py --limit 100              # cap at 100 jobs
    python3 scraper_draft.py --query "Alabama"        # filter by location
    python3 scraper_draft.py --input input_urls.json  # use pre-provided job URLs
    python3 scraper_draft.py --urls <url1> <url2>    # scrape specific job pages
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
from typing import Optional

import requests

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

SITE_NAME = "AMN Healthcare"
SITE_URL = "https://www.amnhealthcare.com"
PLATFORM = "custom"
SCRAPING_METHOD = "internal_api"
SITE_SLUG = "amnhealthcare-com"

# Public API endpoint — no auth, no proxy needed
API_BASE_URL = "https://api.amnhealthcare.io/ONEAmnJobSearch/v1/JobSearch"

# Communities to iterate (category filter — iterate strategy)
COMMUNITIES = [
    "Nursing",
    "Allied",
    "Physician",
    "Advanced-Practice",
    "Dentistry",
    "Leadership",
    "Schools",
    "Language-Services",
    "Revenue-Cycle",
]

# Pagination
PAGE_SIZE = 100  # large page size for fewer requests
TOTAL_COUNT_FIELD = "jobCount"
JOBS_ARRAY_FIELD = "jobs"
PAGE_PARAM = "PageNumber"
PAGE_SIZE_PARAM = "PageSize"

# Rate limiting
DELAY_BETWEEN_REQUESTS = 0.5  # fast public API, but polite

# Default query (used for --query flag)
DEFAULT_QUERY = ""

# Path for output
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TIMESTAMP = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
OUTPUT_FILE = os.path.join(SCRIPT_DIR, f"output_{TIMESTAMP}.json")
INPUT_FILE = os.path.join(SCRIPT_DIR, "input_urls.json")
LOG_FILE = os.path.join(os.path.dirname(SCRIPT_DIR), "logs", f"{SITE_SLUG}.log")

# HTTP headers — mimic a real browser
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Origin": "https://www.amnhealthcare.com",
    "Referer": "https://www.amnhealthcare.com/jobs/",
}

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
# GENERIC JOB FIELD RESOLVER
# ═══════════════════════════════════════════════════════════════════════════════

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from src.job_fields import map_jobs  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════════════
# API FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def _make_session() -> requests.Session:
    """Create a thread-local requests.Session with browser headers."""
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch_api_page(
    session: requests.Session,
    params: dict[str, str],
    retries: int = 3,
) -> Optional[dict]:
    """Fetch a single API page with retry logic. Returns parsed JSON or None."""
    for attempt in range(retries):
        try:
            time.sleep(DELAY_BETWEEN_REQUESTS)
            resp = session.get(API_BASE_URL, params=params, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            if resp.status_code in (429, 503):
                wait = 2 ** attempt * 2
                logger.warning(f"Rate limit/5xx ({resp.status_code}), retry in {wait}s")
                time.sleep(wait)
                continue
            logger.error(f"HTTP error {resp.status_code}: {e}")
            return None
        except requests.RequestException as e:
            logger.error(f"Request failed (attempt {attempt + 1}/{retries}): {e}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    return None


def _build_base_params(query: str = "", location: str = "") -> dict[str, str]:
    """Build minimal base params (no facet FilterTypes — those control which
    filter facets are *returned*, not which items match)."""
    params: dict[str, str] = {
        PAGE_SIZE_PARAM: str(PAGE_SIZE),
        PAGE_PARAM: "1",
        "orderby": "relevance",
    }
    if query:
        params["Keywords"] = query
    if location:
        params["Location"] = location
        params["LocationDistance"] = "30"
    return params


def discover_jobs_via_api(
    session: requests.Session,
    query: str = "",
    location: str = "",
    communities: Optional[list[str]] = None,
) -> list[dict]:
    """Phase 1: Paginate through all communities and collect raw job objects.

    Deduplicates by jobID across communities. Returns the full list of raw
    job dicts from the API.
    """
    if communities is None:
        communities = COMMUNITIES

    seen_ids: set[str] = set()
    all_jobs: list[dict] = []
    total_discovered = 0

    for community in communities:
        params = _build_base_params(query=query, location=location)
        params["Filters"] = f"Communities:{community}"

        page_num = 1
        community_total = 0
        community_jobs: list[dict] = []

        while True:
            params[PAGE_PARAM] = str(page_num)
            logger.info(
                f"Fetching {community} page {page_num} "
                f"(collected: {len(community_jobs)})"
            )

            data = fetch_api_page(session, params)
            if data is None:
                logger.warning(f"No data returned for {community} page {page_num}")
                break

            jobs = data.get(JOBS_ARRAY_FIELD, [])
            if not jobs:
                break

            for job in jobs:
                jid = job.get("jobID", "")
                if jid and jid not in seen_ids:
                    seen_ids.add(jid)
                    job["_src_community"] = community
                    job["_src_url"] = (
                        f"{API_BASE_URL}?"
                        f"Filters=Communities:{community}&"
                        f"{PAGE_PARAM}={page_num}"
                    )
                    community_jobs.append(job)

            community_total = data.get(TOTAL_COUNT_FIELD, 0) or 0
            logger.info(
                f"  {community} page {page_num}: +{len(jobs)} jobs "
                f"(total this community: {len(community_jobs)}"
                f"{f' / {community_total}' if community_total else ''})"
            )

            # Stop if we've reached the reported total or page is short
            if len(jobs) < PAGE_SIZE:
                break
            if community_total and len(community_jobs) >= community_total:
                break

            page_num += 1

        all_jobs.extend(community_jobs)
        total_discovered += len(community_jobs)
        logger.info(
            f"Community '{community}': {len(community_jobs)} unique jobs "
            f"(running total: {total_discovered})"
        )

        # Progress logging every community
        if len(all_jobs) % 500 == 0 and len(all_jobs) > 0:
            logger.info(f"Progress: {total_discovered} jobs discovered so far")

    logger.info(f"Discovery complete: {total_discovered} unique jobs across {len(communities)} communities")
    return all_jobs


# ═══════════════════════════════════════════════════════════════════════════════
# FIELD EXTRACTION — API items are fully populated, no Phase 2 needed
# ═══════════════════════════════════════════════════════════════════════════════

def _build_job_url(job: dict) -> str:
    """Construct the job detail URL from API fields."""
    job_id = job.get("jobID", "")
    if not job_id:
        return ""
    # URL pattern: /job-details/{jobID}/{city}-{state}-{specialty-slug}/
    city_obj = job.get("city") or {}
    state_obj = job.get("state") or {}
    ds_obj = job.get("disciplineSpecialty") or {}

    city = str(city_obj.get("name", "")).lower().replace(" ", "-")
    state_abbrev = str(state_obj.get("abbrev", "")).lower()
    specialty = str(ds_obj.get("specialtyName", "")).lower().replace(" ", "-")

    slug = f"{city}-{state_abbrev}-{specialty}".strip("-")
    return f"{SITE_URL}/job-details/{job_id}/{slug}/"


def _filter_by_date(jobs: list[dict], days: int = 7) -> list[dict]:
    """Keep only jobs posted within the last N days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    filtered = []
    for job in jobs:
        posted = job.get("datePosted", "")
        if not posted:
            filtered.append(job)
            continue
        try:
            dt = datetime.fromisoformat(posted)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt >= cutoff:
                filtered.append(job)
        except (ValueError, TypeError):
            filtered.append(job)  # keep if date can't be parsed
    return filtered


def extract_jobs(raw_jobs: list[dict]) -> list[dict]:
    """Map raw API job objects to standard output fields using the generic
    resolver.  Construct URLs where the API doesn't provide them."""
    if not raw_jobs:
        return []

    # Use the first batch as the sample for field inference
    sample = raw_jobs[:min(10, len(raw_jobs))]
    jobs = map_jobs(sample_items=sample, raw_items=raw_jobs)

    now = datetime.now(timezone.utc).isoformat()
    for idx, (j, raw) in enumerate(zip(jobs, raw_jobs), start=1):
        # Ensure standard fields are set
        j.setdefault("id", idx)
        j.setdefault("status_code", 200)
        j.setdefault("scraped_at", now)
        j.setdefault("remarks", "")

        # src_url from discovery
        src_url = raw.get("_src_url", "")
        if not src_url:
            community = raw.get("_src_community", raw.get("communities", [""])[0] if raw.get("communities") else "")
            src_url = f"{API_BASE_URL}?Filters=Communities:{community}" if community else API_BASE_URL
        j["src_url"] = src_url

        # Construct URL if resolver didn't find one
        if not j.get("url"):
            j["url"] = _build_job_url(raw)

        # Remove internal fields
        raw.pop("_src_community", None)
        raw.pop("_src_url", None)

    return jobs


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 2: Scrape individual job detail pages (for URLs or --input/--urls mode)
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_jsonld_job(html: str, page_url: str) -> Optional[dict]:
    """Extract JobPosting JSON-LD from a job detail page's HTML."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
            if isinstance(data, list):
                for block in data:
                    if block.get("@type") == "JobPosting":
                        return block
            elif isinstance(data, dict) and data.get("@type") == "JobPosting":
                return data
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def scrape_job_detail(
    session: requests.Session,
    url: str,
    index: int,
) -> Optional[dict]:
    """Fetch a job detail page and extract structured data from JSON-LD."""
    try:
        time.sleep(DELAY_BETWEEN_REQUESTS)
        resp = session.get(url, timeout=30)
        if resp.status_code != 200:
            logger.warning(f"HTTP {resp.status_code} for {url}")
            return {
                "id": index,
                "title": "",
                "url": url,
                "src_url": url,
                "status_code": resp.status_code,
                "scraped_at": datetime.now(timezone.utc).isoformat(),
                "remarks": f"HTTP {resp.status_code}",
            }

        html = resp.text
        # Soft 404 detection
        title_match = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE)
        page_title = title_match.group(1).strip().lower() if title_match else ""
        soft404_phrases = [
            "not found", "no longer available", "has been filled",
            "job no longer available", "this job is no longer",
        ]
        for phrase in soft404_phrases:
            if phrase in page_title:
                return {
                    "id": index,
                    "title": "",
                    "url": url,
                    "src_url": url,
                    "status_code": resp.status_code,
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                    "remarks": f"Soft 404: {phrase}",
                }

        jsonld = _extract_jsonld_job(html, url)
        if not jsonld:
            return {
                "id": index,
                "title": "",
                "url": url,
                "src_url": url,
                "status_code": resp.status_code,
                "scraped_at": datetime.now(timezone.utc).isoformat(),
                "remarks": "No JSON-LD JobPosting found",
            }

        # Map via resolver
        mapped = map_jobs(sample_items=[jsonld], raw_items=[jsonld])
        if mapped:
            job = mapped[0]
            job["id"] = index
            job["url"] = url
            job["src_url"] = url
            job["status_code"] = resp.status_code
            job["scraped_at"] = datetime.now(timezone.utc).isoformat()
            job["remarks"] = ""
            return job
    except Exception as e:
        logger.error(f"Error scraping {url}: {e}")

    return {
        "id": index,
        "title": "",
        "url": url,
        "src_url": url,
        "status_code": 0,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": f"Extraction error",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# OUTPUT FILTER — keep only items with real content
# ═══════════════════════════════════════════════════════════════════════════════

def _passes_filter(job: dict) -> bool:
    """Content-type-aware output filter: keep items with a title AND at
    least one other core field (for job_posting: company, location, or
    description).  Items with only a title (e.g. nav pages, soft-404s) are
    dropped unless they have a remarks field indicating a deliberate capture.
    """
    title = (job.get("title") or "").strip()
    if not title:
        return False
    # For jobs, at least one of: company, location, description
    has_content = any(
        (job.get(f) or "").strip()
        for f in ("company", "location", "description", "salary", "job_type")
    )
    return has_content


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description=f"Job scraper for {SITE_NAME}",
    )
    parser.add_argument("--sample", action="store_true", help="Scrape only 5 jobs")
    parser.add_argument("--limit", type=int, default=None, help="Max jobs to scrape")
    parser.add_argument("--input", type=str, default=None, help="Path to input URLs JSON file")
    parser.add_argument("--urls", nargs="+", default=None, help="Job URLs as arguments")
    parser.add_argument("--query", type=str, default=None, help="Search keyword (e.g. 'ICU')")
    parser.add_argument("--location", type=str, default=None, help="Location filter (e.g. 'Alabama')")
    parser.add_argument("--no-proxy", action="store_true", default=True, help="No proxy (default)")
    args = parser.parse_args()

    start_time = time.time()

    logger.info("=" * 80)
    logger.info(f"Starting job scraper for {SITE_NAME}")
    logger.info(f"Site: {SITE_URL}")
    logger.info(f"API: {API_BASE_URL}")
    logger.info(f"Output: {OUTPUT_FILE}")
    logger.info("=" * 80)

    results: list[dict] = []
    failed_count = 0

    # ── Mode 1: Direct URL input (--input or --urls) ────────────────────────
    if args.urls or args.input:
        urls: list[str] = []
        if args.urls:
            urls = list(args.urls)
        elif args.input:
            with open(args.input, "r", encoding="utf-8") as f:
                urls = json.load(f).get("urls", [])
        elif os.path.exists(INPUT_FILE):
            with open(INPUT_FILE, "r", encoding="utf-8") as f:
                urls = json.load(f).get("urls", [])

        if not urls:
            logger.error("No URLs found in input. Exiting.")
            return

        logger.info(f"Scraping {len(urls)} job detail pages from input URLs")
        session = _make_session()

        # Apply limits
        if args.sample:
            urls = urls[:5]
        if args.limit:
            urls = urls[: args.limit]

        # Concurrent Phase 2 extraction
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(scrape_job_detail, _make_session(), url, idx): url
                for idx, url in enumerate(urls, start=1)
            }
            for future in as_completed(futures):
                job = future.result()
                if job and _passes_filter(job):
                    results.append(job)
                else:
                    failed_count += 1

                if len(results) % 25 == 0:
                    percent = (len(results) + failed_count) / len(urls) * 100
                    logger.info(
                        f"Progress: [{len(results) + failed_count}/{len(urls)}] "
                        f"({percent:.1f}%)"
                    )

        # Preserve original order
        results.sort(key=lambda j: j.get("id", 0))

    # ── Mode 2: API discovery (default) ─────────────────────────────────────
    else:
        query = args.query if args.query else DEFAULT_QUERY
        location = args.location if args.location else ""

        if query:
            logger.info(f"Search query: '{query}'")
        if location:
            logger.info(f"Location filter: '{location}'")

        session = _make_session()

        logger.info("Phase 1: Discovering jobs via API...")
        raw_jobs = discover_jobs_via_api(
            session=session,
            query=query,
            location=location,
        )

        if not raw_jobs:
            logger.warning("No jobs discovered via API")
            results = []
        else:
            logger.info(f"Phase 2: Mapping {len(raw_jobs)} raw jobs to standard fields...")

            # Apply date filter (last 7 days)
            raw_jobs = _filter_by_date(raw_jobs, days=7)
            logger.info(f"After 7-day date filter: {len(raw_jobs)} jobs")

            # Apply limits
            if args.sample:
                raw_jobs = raw_jobs[:5]
            if args.limit:
                raw_jobs = raw_jobs[: args.limit]

            # Map fields using generic resolver
            results = extract_jobs(raw_jobs)

            # Filter out items without real content
            pre_filter = len(results)
            results = [j for j in results if _passes_filter(j)]
            failed_count = pre_filter - len(results)
            logger.info(
                f"Output filter: {pre_filter} -> {len(results)} "
                f"({failed_count} dropped as empty/soft-404)"
            )

    # ── Re-index sequentially ───────────────────────────────────────────────
    for idx, job in enumerate(results, start=1):
        job["id"] = idx

    # ── Write output ────────────────────────────────────────────────────────
    output = {
        "site": {
            "name": SITE_NAME,
            "url": SITE_URL,
            "platform": PLATFORM,
            "scraping_method": SCRAPING_METHOD,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        },
        "jobs": results,
        "metadata": {
            "scraping_duration_seconds": round(time.time() - start_time, 2),
            "failed_products": failed_count,
            "rate_limit_delay": DELAY_BETWEEN_REQUESTS,
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
    logger.info(f"Total: {len(results)}, Failed: {failed_count}")
    logger.info(f"Duration: {round(time.time() - start_time, 2)}s")
    logger.info(f"Output: {OUTPUT_FILE}")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
