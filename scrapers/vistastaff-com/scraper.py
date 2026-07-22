#!/usr/bin/env python3
"""VistaStaff job posting scraper.

Extracts job posting data from individual job detail pages on vistastaff.com.
Uses plain HTTP requests (no browser) since the WordPress site renders
server-side with full HTML.
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SITE_NAME = "Vista Staff"
SITE_URL = "https://www.vistastaff.com"
PLATFORM = "wordpress"
SCRAPING_METHOD = "http_requests"
CONTENT_TYPE = "job_posting"
OUTPUT_KEY = "jobs"

DELAY = 2.0  # seconds between requests (rate limiting)
MAX_WORKERS = 8  # concurrent threads for detail-page extraction

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "vistastaff_com.log")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------
@dataclass
class JobPosting:
    """Represents a single job posting extracted from a detail page."""

    id: int = 0
    title: str = ""
    company: str = ""
    location: str = ""
    specialty: str = ""
    job_type: str = ""
    job_number: str = ""
    zip_code: str = ""
    url: str = ""
    src_url: str = ""
    description: str = ""
    date_posted: str = ""
    availability: str = ""
    currency: str = "USD"
    price: str = ""
    original_price: str = ""
    status_code: int = 0
    scraped_at: str = ""
    remarks: str = ""


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JSON-LD helper
# ---------------------------------------------------------------------------
def extract_jsonld(soup: BeautifulSoup) -> list[dict]:
    """Extract all JSON-LD blocks from the page."""
    results: list[dict] = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
            if isinstance(data, list):
                results.extend(data)
            else:
                results.append(data)
        except (json.JSONDecodeError, TypeError):
            continue
    return results


def get_date_published(soup: BeautifulSoup) -> str:
    """Extract datePublished from WebPage JSON-LD if available."""
    for block in extract_jsonld(soup):
        if block.get("@type") == "WebPage":
            return block.get("datePublished", "")
    return ""


# ---------------------------------------------------------------------------
# Soft-404 detection
# ---------------------------------------------------------------------------
SOFT_404_PATTERNS = re.compile(
    r"not found|no longer available|unavailable|discontinued|page not found|404",
    re.IGNORECASE,
)


def detect_soft_404(soup: BeautifulSoup, final_url: str, requested_url: str) -> Optional[str]:
    """Return a remark string if the page is a soft-404, else None."""
    # Check redirect
    if final_url and requested_url and not _url_matches(final_url, requested_url):
        return f"Soft 404: redirected to {final_url}"

    # Check title / H1
    h1 = soup.find("h1")
    title_tag = soup.find("title")
    for el in (h1, title_tag):
        text = el.get_text(strip=True) if el else ""
        if text and SOFT_404_PATTERNS.search(text):
            return f"Soft 404: '{text}'"

    return None


def _url_matches(a: str, b: str) -> bool:
    """Loose URL comparison — ignore trailing slash and scheme differences."""
    a_norm = a.rstrip("/").lower()
    b_norm = b.rstrip("/").lower()
    return a_norm == b_norm or a_norm.endswith(b_norm) or b_norm.endswith(a_norm)


# ---------------------------------------------------------------------------
# Job detail extraction
# ---------------------------------------------------------------------------
def extract_job_from_detail(url: str) -> JobPosting:
    """Fetch a single job detail page and extract all fields."""
    job = JobPosting(url=url, src_url=url)
    job.scraped_at = datetime.now(timezone.utc).isoformat()

    resp = None
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
            job.status_code = resp.status_code
            break
        except requests.RequestException as e:
            if attempt < 2:
                wait = 2 ** (attempt + 1)
                logger.warning("Attempt %d failed for %s: %s — retrying in %ds", attempt + 1, url, e, wait)
                time.sleep(wait)
            else:
                logger.error("All 3 attempts failed for %s: %s", url, e)
                job.remarks = f"Request error: {e}"
                return job

    if resp.status_code != 200:
        job.remarks = f"HTTP {resp.status_code}"
        return job

    soup = BeautifulSoup(resp.text, "html.parser")

    # Soft-404 check
    soft404 = detect_soft_404(soup, str(resp.url), url)
    if soft404:
        job.remarks = soft404
        return job

    # --- Title ---
    title_el = soup.select_one("h1.singular-job-post__title")
    if title_el:
        job.title = title_el.get_text(strip=True)

    # --- Company ---
    job.company = "VISTA Staffing"

    # --- Description ---
    desc_el = soup.select_one("div.singular-job-post__content")
    if desc_el:
        job.description = desc_el.get_text(separator="\n", strip=True)

    # --- Sidebar details table ---
    # The detail page has a table with columns: location, specialty, job type,
    # zip code, job number. These are in table.job-feed__table or the sidebar
    # ul.singular-job-post__details. We try both.
    sidebar = soup.select_one("ul.singular-job-post__details")
    table = soup.select_one("table.job-feed__table")

    # Extract from table first (per product analysis selectors)
    if table:
        for row in table.select("tbody tr"):
            cells = row.select("td")
            cell_classes = [td.get("class", []) for td in cells]
            flat_classes = [c for cls in cell_classes for c in cls]
            cell_texts = [td.get_text(strip=True) for td in cells]

            if "specialty" in flat_classes:
                idx = flat_classes.index("specialty")
                job.specialty = cell_texts[idx] if idx < len(cell_texts) else ""
            if "state" in flat_classes:
                idx = flat_classes.index("state")
                job.location = cell_texts[idx] if idx < len(cell_texts) else ""
            if "type" in flat_classes:
                idx = flat_classes.index("type")
                job.job_type = cell_texts[idx] if idx < len(cell_texts) else ""
            if "job-number" in flat_classes:
                idx = flat_classes.index("job-number")
                job.job_number = cell_texts[idx] if idx < len(cell_texts) else ""
            if "zip" in flat_classes:
                idx = flat_classes.index("zip")
                job.zip_code = cell_texts[idx] if idx < len(cell_texts) else ""

    # Extract from sidebar list items as fallback
    if sidebar:
        for li in sidebar.select("li"):
            li_text = li.get_text(strip=True)
            # Look for known labels
            if not job.specialty and "specialty" in li_text.lower():
                job.specialty = li_text.split(":", 1)[-1].strip() if ":" in li_text else li_text
            if not job.location and ("location" in li_text.lower() or "state" in li_text.lower()):
                job.location = li_text.split(":", 1)[-1].strip() if ":" in li_text else li_text
            if not job.job_number and "job #" in li_text.lower():
                job.job_number = li_text.split(":", 1)[-1].strip() if ":" in li_text else li_text
            if not job.job_type and "type" in li_text.lower():
                job.job_type = li_text.split(":", 1)[-1].strip() if ":" in li_text else li_text

    # --- URL from the page (subtitle link in table) ---
    link_el = soup.select_one("table.job-feed__table tbody tr td.subtitle a[href]")
    if link_el:
        job.url = link_el.get("href", "")
        if not job.url.startswith("http"):
            job.url = SITE_URL + job.url

    # --- job_number fallback from URL slug ---
    if not job.job_number:
        url_match = re.search(r"(\d{5,})/?$", url)
        if url_match:
            job.job_number = url_match.group(1)

    # --- date_posted from JSON-LD ---
    job.date_posted = get_date_published(soup)

    # --- Availability: for job postings, default to "Open" ---
    if not job.remarks:
        job.availability = "Open"

    return job


# ---------------------------------------------------------------------------
# Sequential extraction wrapper (for --sample / --limit)
# ---------------------------------------------------------------------------
def extract_jobs_sequential(urls: list[str]) -> list[JobPosting]:
    """Extract jobs one-by-one with rate limiting."""
    results: list[JobPosting] = []
    for i, url in enumerate(urls, 1):
        logger.info("Processing [%d/%d]: %s", i, len(urls), url)
        job = extract_job_from_detail(url)
        job.id = i
        results.append(job)
        time.sleep(DELAY)
        if len(results) % 25 == 0 or len(results) == len(urls):
            pct = (len(results) / len(urls)) * 100
            logger.info("Progress: [%d/%d] (%.1f%%)", len(results), len(urls), pct)
    return results


# ---------------------------------------------------------------------------
# Concurrent extraction wrapper (for full runs)
# ---------------------------------------------------------------------------
def extract_jobs_concurrent(urls: list[str]) -> list[JobPosting]:
    """Extract jobs concurrently with a thread pool."""
    results_map: dict[int, JobPosting] = {}

    def _fetch(idx: int, url: str) -> tuple[int, JobPosting]:
        logger.info("Processing [%d/%d]: %s", idx + 1, len(urls), url)
        job = extract_job_from_detail(url)
        job.id = idx + 1
        time.sleep(DELAY)
        return idx, job

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(_fetch, i, url): i for i, url in enumerate(urls)
        }
        completed = 0
        for future in as_completed(futures):
            idx, job = future.result()
            results_map[idx] = job
            completed += 1
            if completed % 25 == 0 or completed == len(urls):
                pct = (completed / len(urls)) * 100
                logger.info("Progress: [%d/%d] (%.1f%%)", completed, len(urls), pct)

    return [results_map[i] for i in sorted(results_map.keys())]


# ---------------------------------------------------------------------------
# Content-type filter
# ---------------------------------------------------------------------------
def is_valid_job(job: JobPosting) -> bool:
    """A valid job must have at least a title, unless it already has an error remark."""
    if job.remarks:
        return True  # Keep error items in output so user can see failures
    return bool(job.title)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def load_urls(path: str) -> list[str]:
    """Load URLs from a JSON file."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("urls", [])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=f"Scrape {SITE_NAME} job postings")
    parser.add_argument("--input", type=str, help="Path to input URLs JSON file")
    parser.add_argument("--urls", nargs="+", help="Job detail URLs as CLI arguments")
    parser.add_argument("--sample", action="store_true", help="Scrape up to 5 URLs only")
    parser.add_argument("--limit", type=int, default=0, help="Max jobs to scrape (0=all)")
    parser.add_argument("--no-proxy", action="store_true", default=True, help="No proxy (default)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    start_time = time.time()

    # --- Load URLs ---
    # --input takes absolute precedence
    if args.input:
        urls = load_urls(args.input)
    elif args.urls:
        urls = args.urls
    else:
        default_input = os.path.join(SCRIPT_DIR, "input_urls.json")
        if os.path.exists(default_input):
            urls = load_urls(default_input)
        else:
            logger.error("No input URLs found. Provide --input, --urls, or input_urls.json")
            sys.exit(1)

    # --- Apply limits ---
    if args.sample:
        urls = urls[:5]
        logger.info("Sample mode: limiting to first 5 URLs")
    elif args.limit > 0:
        urls = urls[: args.limit]
        logger.info("Limit mode: scraping %d URLs", args.limit)

    logger.info("=" * 80)
    logger.info("Starting scraper for %s", SITE_NAME)
    logger.info("Total URLs to scrape: %d", len(urls))
    logger.info("=" * 80)

    # --- Extract ---
    if len(urls) <= 10:
        results = extract_jobs_sequential(urls)
    else:
        results = extract_jobs_concurrent(urls)

    # --- Filter valid jobs ---
    valid_results = [j for j in results if is_valid_job(j)]
    invalid_count = len(results) - len(valid_results)
    if invalid_count:
        logger.warning("Filtered out %d items without a title", invalid_count)

    # --- Serialize ---
    output_jobs = []
    for j in valid_results:
        output_jobs.append(
            {
                "id": j.id,
                "title": j.title,
                "company": j.company,
                "location": j.location,
                "specialty": j.specialty,
                "job_type": j.job_type,
                "job_number": j.job_number,
                "zip_code": j.zip_code,
                "url": j.url,
                "src_url": j.src_url,
                "description": j.description,
                "date_posted": j.date_posted,
                "availability": j.availability,
                "currency": j.currency,
                "price": j.price,
                "original_price": j.original_price,
                "status_code": j.status_code,
                "scraped_at": j.scraped_at,
                "remarks": j.remarks,
            }
        )

    failed_count = sum(1 for j in valid_results if j.remarks and ("error" in j.remarks.lower() or "404" in j.remarks.lower()))

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    output_file = os.path.join(SCRIPT_DIR, f"output_{timestamp}.json")

    output = {
        "site": {
            "name": SITE_NAME,
            "url": SITE_URL,
            "platform": PLATFORM,
            "scraping_method": SCRAPING_METHOD,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        },
        OUTPUT_KEY: output_jobs,
        "metadata": {
            "scraping_duration_seconds": round(time.time() - start_time, 2),
            "failed_products": failed_count,
            "rate_limit_delay": DELAY,
            "content_type": CONTENT_TYPE,
        },
    }

    with open(output_file, "w", encoding="utf-8") as f:
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
    logger.info("Total extracted: %d, Failed: %d", len(valid_results), failed_count)
    logger.info("Output saved to: %s", output_file)
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
