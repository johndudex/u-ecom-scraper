#!/usr/bin/env python3
"""Vista Staff Job Board Scraper — Two-Phase HTTP.

Phase 1: Discover job-detail URLs from SSR listing pages at /job-board/.
Phase 2: Scrape each detail page for title, location, specialty, etc.

Usage:
    python3 scraper_draft.py                          # full discovery + scrape
    python3 scraper_draft.py --query AZ               # pin to a state
    python3 scraper_draft.py --sample                # quick test (5 jobs)
    python3 scraper_draft.py --limit 50              # cap at 50
    python3 scraper_draft.py --input urls.json       # skip Phase 1
    python3 scraper_draft.py --urls <url1> <url2>     # skip Phase 1
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ═══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

SITE_NAME: str = "VISTA Staffing Solutions"
SITE_URL: str = "https://www.vistastaff.com"
PLATFORM: str = "custom"
SCRAPING_METHOD: str = "http_requests"
SITE_SLUG: str = "vistastaff-com"
OUTPUT_KEY: str = "jobs"
COMPANY: str = "VISTA Staffing Solutions"

BASE_URL: str = "https://www.vistastaff.com/job-board/"
ALL_JOBS_URL: str = (
    "https://www.vistastaff.com/job-board/"
    "?profession=all&specialty=all&type=&state=all"
)

HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    # NOTE: Do NOT set Accept-Encoding — let requests auto-negotiate only
    # encodings it can decompress (gzip, deflate). If brotli is not installed,
    # a manual "Accept-Encoding: gzip, deflate, br" causes the server to send
    # brotli-compressed data that requests CANNOT decompress → garbled bytes.
    "Connection": "keep-alive",
    "Cache-Control": "max-age=0",
}

DELAY: float = 2.0
TIMEOUT: int = 30
MAX_RETRIES: int = 3

# Regex: /job-board/<slug>-<digits>[-<digits>]/  e.g. ...-147564/ or ...-147339-2/
JOB_DETAIL_RE: re.Pattern = re.compile(r"/job-board/.+-\d+(?:-\d+)?/?$")
JOB_ID_RE: re.Pattern = re.compile(r"-(\d+)(?:-\d+)?/?$")
STATE_RE: re.Pattern = re.compile(r",\s*([A-Za-z]{2})$")
CITY_RE: re.Pattern = re.compile(r"^(.+),\s*[A-Za-z]{2}$")

# Profession filter IDs from the site's <select>
PROFESSION_IDS: list[str] = ["361", "362", "363", "364", "441", "455"]

STATE_CODES: list[str] = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN",
    "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV",
]

STATE_NAME_MAP: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN",
    "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
}

# ─── Paths ───────────────────────────────────────────────────────────────────

SCRIPT_DIR: str = os.path.dirname(os.path.abspath(__file__))
TIMESTAMP: str = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
OUTPUT_FILE: str = os.path.join(SCRIPT_DIR, f"output_{TIMESTAMP}.json")
LOG_FILE: str = os.path.join(
    os.path.dirname(SCRIPT_DIR) or ".", "logs", f"{SITE_SLUG}.log"
)

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
logger: logging.Logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP HELPERS
# ═══════════════════════════════════════════════════════════════════════════════


def http_get(url: str) -> requests.Response:
    """Perform HTTP GET with retries."""
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            return resp
        except requests.RequestException as exc:
            if attempt < MAX_RETRIES - 1:
                wait = DELAY * (attempt + 1)
                logger.warning(
                    "GET %s failed (attempt %d/%d): %s — retry in %.1fs",
                    url[:100], attempt + 1, MAX_RETRIES, exc, wait,
                )
                time.sleep(wait)
            else:
                logger.error("GET %s failed after %d attempts: %s", url[:100], MAX_RETRIES, exc)
                raise
    # Unreachable
    raise RuntimeError(f"Failed to fetch {url}")


def normalise_state(query: str) -> str:
    """Convert a user query to a state code or 'all'."""
    s = query.strip()
    if not s or s.lower() == "all":
        return "all"
    up = s.upper()
    if len(up) == 2 and up in STATE_CODES:
        return up
    low = s.lower()
    if low in STATE_NAME_MAP:
        return STATE_NAME_MAP[low]
    return "all"


def extract_job_id(url: str) -> str:
    """Extract numeric job ID from a URL."""
    m = JOB_ID_RE.search(url)
    return m.group(1) if m else "0"


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 1 — DISCOVER JOB URLs
# ═══════════════════════════════════════════════════════════════════════════════


def parse_job_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """Extract (absolute_url, job_id) pairs from listing page HTML.

    Selects all <a> tags whose href contains '/job-board/', then filters to
    only job-detail URLs (no query strings, matches /job-board/<slug>-<id>/).
    """
    soup = BeautifulSoup(html, "html.parser")
    pairs: list[tuple[str, str]] = []

    for a in soup.select("a[href*='/job-board/']"):
        href: str = a.get("href", "")
        if not href:
            continue
        # Skip filter/search URLs (have query params)
        if "?" in href:
            continue
        # Skip bare /job-board/ (the listing page itself)
        if href.rstrip("/") == "/job-board/" or href.rstrip("/") == BASE_URL.rstrip("/"):
            continue
        # Resolve relative
        if not href.startswith("http"):
            href = urljoin(base_url, href)
        # Must match /job-board/<slug>-<digits>/
        if JOB_DETAIL_RE.search(href):
            jid = extract_job_id(href)
            pairs.append((href, jid))

    return pairs


def next_page_url(soup: BeautifulSoup, base_url: str) -> Optional[str]:
    """Find pagination next link."""
    nxt = soup.select_one('a[rel="next"]')
    if nxt:
        href = nxt.get("href", "")
        if href:
            return urljoin(base_url, href) if not href.startswith("http") else href
    return None


def crawl_seed(
    seed_url: str,
    label: str,
    seen: dict[str, dict],
    session: requests.Session,
    stop_early: bool = False,
) -> int:
    """Paginate a listing seed, collecting new job-detail URLs.

    Returns count of newly discovered jobs.
    """
    current = seed_url
    page = 0
    new_total = 0

    while current:
        page += 1
        try:
            time.sleep(DELAY)
            resp = session.get(current, timeout=TIMEOUT)
            logger.info(
                "  [%s] p%d: HTTP %d (%d bytes)",
                label, page, resp.status_code, len(resp.text),
            )
            if resp.status_code != 200:
                logger.warning("  [%s] p%d: non-200 — stopping", label, page)
                break

            links = parse_job_links(resp.text, current)
            page_new = 0
            for url, jid in links:
                if jid not in seen:
                    seen[jid] = {"url": url, "id": jid, "src_url": seed_url}
                    new_total += 1
                    page_new += 1

            logger.info(
                "  [%s] p%d: %d links, %d new (cumulative %d)",
                label, page, len(links), page_new, len(seen),
            )

            # No new links on page > 1 → exhausted
            if page_new == 0 and page > 1:
                break

            soup = BeautifulSoup(resp.text, "html.parser")
            nxt = next_page_url(soup, current)
            if not nxt or nxt == current:
                break
            current = nxt

            if stop_early and len(seen) >= 5:
                break

        except Exception as exc:
            logger.error("  [%s] p%d error: %s", label, page, exc)
            break

    return new_total


def build_seeds(state_code: str) -> list[tuple[str, str]]:
    """Build listing seed URLs.

    If state_code != 'all': iterate professions for that state.
    If state_code == 'all': iterate all professions + all states.
    """
    seeds: list[tuple[str, str]] = []

    if state_code != "all":
        # Single state — iterate professions
        seeds.append((
            f"all/{state_code}",
            f"{BASE_URL}?profession=all&specialty=all&type=&state={state_code}",
        ))
        for pid in PROFESSION_IDS:
            seeds.append((
                f"p={pid}/{state_code}",
                f"{BASE_URL}?profession={pid}&specialty=all&type=&state={state_code}",
            ))
    else:
        # Full catalog
        seeds.append(("all/all", ALL_JOBS_URL))
        for pid in PROFESSION_IDS:
            seeds.append((
                f"p={pid}",
                f"{BASE_URL}?profession={pid}&specialty=all&type=&state=all",
            ))
        # Iterate states
        for st in STATE_CODES:
            seeds.append((
                f"s={st}",
                f"{BASE_URL}?profession=all&specialty=all&type=&state={st}",
            ))

    return seeds


def phase1_discover(query: str = "all", sample: bool = False) -> list[dict]:
    """Phase 1: discover job-detail URLs from listing pages."""
    state_code = normalise_state(query)
    seeds = build_seeds(state_code)
    seen: dict[str, dict] = {}

    logger.info("Phase 1: %d seeds (state=%s)", len(seeds), state_code)

    session = requests.Session()
    session.headers.update(HEADERS)

    for i, (label, url) in enumerate(seeds):
        logger.info("Phase 1 seed %d/%d: %s", i + 1, len(seeds), label)
        try:
            crawl_seed(url, label, seen, session, stop_early=sample)
        except Exception as exc:
            logger.error("Phase 1 seed %s error: %s", label, exc)
        if sample and len(seen) >= 5:
            break

    session.close()

    result = list(seen.values())
    logger.info("Phase 1 done: %d unique jobs discovered", len(result))
    if sample and len(result) > 5:
        result = result[:5]
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 2 — SCRAPE DETAIL PAGES
# ═══════════════════════════════════════════════════════════════════════════════


def extract_sidebar_value(soup: BeautifulSoup, label_keyword: str) -> str:
    """Find a <li> in .singular-job-post__details where <strong> contains
    label_keyword (case-insensitive). Return the text after the <strong>."""
    for li in soup.select(".singular-job-post__details li"):
        strong = li.find("strong")
        if not strong:
            continue
        if label_keyword.lower() in strong.get_text(strip=True).lower():
            full = li.get_text(separator=" ", strip=True)
            # Remove the label portion (the strong text + colon)
            label_part = strong.get_text(strip=True)
            value = full
            if label_part in value:
                value = value.split(label_part, 1)[-1].strip()
            # Strip leading colon/dash/space
            value = value.lstrip(":- ").strip()
            return value
    return ""


def extract_highlight_field(text: str, label: str) -> str:
    """Extract 'Label: Value' from content text."""
    pat = re.compile(re.escape(label) + r"\s*:?\s*(.+?)(?:\n|$)", re.IGNORECASE)
    m = pat.search(text)
    return m.group(1).strip() if m else ""


def extract_jsonld_date(soup: BeautifulSoup) -> str:
    """Extract datePublished from WebPage JSON-LD."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            raw = script.string
            if not raw:
                continue
            data = json.loads(raw)
            items: list[dict] = [data] if isinstance(data, dict) else data
            for item in items:
                if isinstance(item, dict) and item.get("@type") == "WebPage":
                    dp = item.get("datePublished", "")
                    if dp:
                        return dp
        except (json.JSONDecodeError, TypeError, AttributeError):
            continue
    return ""


def scrape_one(url: str, job_id: str, src_url: str) -> dict[str, Any]:
    """Fetch and parse a single job detail page.

    Returns a standardised dict with all output fields.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    job: dict[str, Any] = {
        "id": 0,
        "title": "",
        "company": COMPANY,
        "location": "",
        "salary": "",
        "description": "",
        "requirements": "",
        "job_type": "",
        "apply_url": url,
        "url": url,
        "src_url": src_url,
        "status_code": 0,
        "scraped_at": now_iso,
        "remarks": "",
        "specialty": "",
        "state": "",
        "city": "",
        "job_number": job_id,
        "profession": "",
        "setting": "",
        "schedule": "",
        "emr": "",
        "contact_email": "",
        "contact_phone": "",
        "posted_date": "",
    }

    try:
        time.sleep(DELAY)
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        job["status_code"] = resp.status_code

        logger.info(
            "  [%s] HTTP %d, %d bytes, content-type=%s",
            job_id, resp.status_code, len(resp.text),
            resp.headers.get("content-type", "?")[:60],
        )

        if resp.status_code != 200:
            job["remarks"] = f"HTTP {resp.status_code}"
            logger.warning("  [%s] Non-200 status", job_id)
            return job

        html = resp.text

        # Soft 404: check page title for markers
        soup = BeautifulSoup(html, "html.parser")
        title_tag = soup.find("title")
        title_tag_text = title_tag.get_text(strip=True).lower() if title_tag else ""
        for marker in ("page not found", "404", "error", "unavailable"):
            if marker in title_tag_text:
                job["remarks"] = f"Soft 404: {marker} in page title"
                logger.warning("  [%s] Soft 404 detected in <title>", job_id)
                return job

        # ── Title (primary selector) ──────────────────────────────────
        h1 = soup.select_one("h1.singular-job-post__title")
        if h1:
            job["title"] = h1.get_text(strip=True)

        # ── Title (fallback: any h1) ────────────────────────────────────
        if not job["title"]:
            h1_any = soup.find("h1")
            if h1_any:
                job["title"] = h1_any.get_text(strip=True)
                logger.info("  [%s] Title from fallback h1", job_id)

        # ── Title (fallback: og:title) ────────────────────────────────
        if not job["title"]:
            og_title = soup.find("meta", attrs={"property": "og:title"})
            if og_title and og_title.get("content"):
                job["title"] = og_title["content"].strip()
                logger.info("  [%s] Title from og:title", job_id)

        # ── Title (fallback: document.title) ───────────────────────────
        if not job["title"] and title_tag:
            t = title_tag.get_text(strip=True)
            if len(t) > 5 and t != "VISTA Staffing Solutions":
                job["title"] = t
                logger.info("  [%s] Title from <title> tag", job_id)

        if not job["title"]:
            logger.warning("  [%s] NO TITLE FOUND — dumping HTML snippet", job_id)
            # Dump first 500 chars of body for debugging
            body = soup.find("body")
            body_snippet = body.get_text(strip=True)[:500] if body else html[:500]
            logger.info("  [%s] body snippet: %s", job_id, body_snippet)
            job["remarks"] = "No title found"
            return job

        logger.info("  [%s] Title: %s", job_id, job["title"][:80])

        # ── OG URL ────────────────────────────────────────────────────
        og_url = soup.find("meta", attrs={"property": "og:url"})
        if og_url and og_url.get("content"):
            job["url"] = og_url["content"]
            job["apply_url"] = og_url["content"]

        # ── Sidebar details ───────────────────────────────────────────
        job["specialty"] = extract_sidebar_value(soup, "Specialty")
        job["location"] = extract_sidebar_value(soup, "Location")
        job_number = extract_sidebar_value(soup, "Job Number")
        if job_number:
            job["job_number"] = job_number

        logger.info(
            "  [%s] specialty=%s location=%s job_number=%s",
            job_id, job["specialty"][:30] if job["specialty"] else "-",
            job["location"][:30] if job["location"] else "-",
            job["job_number"],
        )

        # ── State / City ──────────────────────────────────────────────
        if job["location"]:
            sm = STATE_RE.search(job["location"])
            if sm:
                job["state"] = sm.group(1).upper()
            cm = CITY_RE.search(job["location"])
            if cm:
                job["city"] = cm.group(1).strip()

        # ── Profession from title ─────────────────────────────────────
        for sep in ["–", "-", "—"]:
            if sep in job["title"]:
                job["profession"] = job["title"].split(sep, 1)[0].strip()
                break

        # ── Description ───────────────────────────────────────────────
        content_el = soup.select_one(".singular-job-post__content")
        if content_el:
            about = content_el.find("div", class_="about")
            if about:
                about.decompose()
            job["description"] = content_el.get_text(strip=True, separator="\n")
        else:
            # Fallback: grab all text from article
            article = soup.find("article")
            if article:
                job["description"] = article.get_text(strip=True, separator="\n")
            logger.info("  [%s] Description from %s", job_id, "article" if article else "none")

        # ── Job type ──────────────────────────────────────────────────
        desc_lower = (job["description"] or "").lower()
        if "locum tenens" in desc_lower:
            job["job_type"] = "locum tenens"
        elif "permanent" in desc_lower:
            job["job_type"] = "permanent"

        # ── Opportunity highlights ─────────────────────────────────────
        content_text = content_el.get_text("\n") if content_el else ""
        job["setting"] = extract_highlight_field(content_text, "Job Setting")
        job["schedule"] = extract_highlight_field(content_text, "Schedule")
        job["emr"] = extract_highlight_field(content_text, "EMR")

        # ── Contact info ──────────────────────────────────────────────
        mailto = soup.select_one('.singular-job-post__details a[href^="mailto"]')
        if mailto:
            job["contact_email"] = mailto.get("href", "").replace("mailto:", "")
        tel = soup.select_one('.singular-job-post__details a[href^="tel"]')
        if tel:
            job["contact_phone"] = tel.get("href", "").replace("tel:", "")

        # ── Posted date from JSON-LD ──────────────────────────────────
        job["posted_date"] = extract_jsonld_date(soup)

    except Exception as exc:
        logger.error("  [%s] Exception: %s", job_id, exc, exc_info=True)
        job["remarks"] = f"Error: {exc}"

    return job


# ═══════════════════════════════════════════════════════════════════════════════
# CONTENT FILTER
# ═══════════════════════════════════════════════════════════════════════════════


def is_valid_job(job: dict[str, Any]) -> bool:
    """Return True if job has a title AND at least one other core field.

    Core fields for job_posting: company, location, description.
    """
    if not job.get("title"):
        return False
    return any(job.get(f) for f in ("company", "location", "description"))


# ═══════════════════════════════════════════════════════════════════════════════
# INPUT HELPERS
# ═══════════════════════════════════════════════════════════════════════════════


def load_input_urls(filepath: str) -> list[dict[str, str]]:
    """Load URLs from a JSON file into [{url, id, src_url}, ...]."""
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    result: list[dict[str, str]] = []
    for item in data.get("urls", []):
        if isinstance(item, str):
            jid = extract_job_id(item) or "0"
            result.append({"url": item, "id": jid, "src_url": item})
        elif isinstance(item, dict):
            url = item.get("url", "")
            jid = item.get("id") or extract_job_id(url) or "0"
            result.append({
                "url": url,
                "id": str(jid),
                "src_url": item.get("src_url", url),
            })
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════


def main() -> None:
    parser = argparse.ArgumentParser(description=f"Scraper for {SITE_NAME}")
    parser.add_argument("--input", type=str, default=None, help="Input URLs JSON file")
    parser.add_argument("--urls", nargs="+", default=None, help="Job URLs as CLI args")
    parser.add_argument("--sample", action="store_true", help="Quick test (5 jobs)")
    parser.add_argument("--limit", type=int, default=None, help="Max jobs to scrape")
    parser.add_argument("--query", type=str, default="", help="State filter (e.g. AZ, Alabama)")
    parser.add_argument("--no-proxy", action="store_true", default=True, help="No proxy (default)")
    args = parser.parse_args()

    start = time.time()

    logger.info("=" * 80)
    logger.info("Starting scraper for %s", SITE_NAME)
    logger.info("Site: %s | Method: %s", SITE_URL, SCRAPING_METHOD)
    logger.info("Output: %s", OUTPUT_FILE)
    if args.query:
        logger.info("Query: %s", args.query)
    logger.info("=" * 80)

    # ─── Determine URLs ───────────────────────────────────────────────

    jobs_info: list[dict[str, str]] = []

    if args.urls:
        for u in args.urls:
            u = u.strip("\"'")
            jid = extract_job_id(u) or "0"
            jobs_info.append({"url": u, "id": jid, "src_url": u})
        logger.info("Loaded %d URLs from --urls", len(jobs_info))

    elif args.input:
        path = args.input
        if not os.path.isfile(path):
            alt = os.path.join(SCRIPT_DIR, path)
            if os.path.isfile(alt):
                path = alt
            else:
                logger.error("Input file not found: %s", args.input)
                sys.exit(1)
        jobs_info = load_input_urls(path)
        logger.info("Loaded %d URLs from --input %s", len(jobs_info), path)

    if not jobs_info:
        # Default: Phase 1 discovery
        query = args.query or "all"
        logger.info("Phase 1: discovering jobs (query=%s)", query)
        jobs_info = phase1_discover(query=query, sample=args.sample)
        logger.info("Discovered %d jobs", len(jobs_info))

    if not jobs_info:
        logger.warning("No jobs found. Exiting.")
        sys.exit(0)

    # ─── Apply limits ─────────────────────────────────────────────────

    if args.sample and len(jobs_info) > 5:
        jobs_info = jobs_info[:5]
    if args.limit:
        jobs_info = jobs_info[:args.limit]

    total = len(jobs_info)
    logger.info("Scraping %d jobs …", total)

    # ─── Phase 2: Extract each job ────────────────────────────────────

    raw: list[dict[str, Any]] = []
    ok_count = 0
    fail_count = 0

    for i, info in enumerate(jobs_info):
        job = scrape_one(info["url"], info["id"], info["src_url"])
        job["id"] = i + 1

        if job.get("title"):
            ok_count += 1
        else:
            fail_count += 1

        raw.append(job)

        # Progress every 25 or on last
        if (i + 1) % 25 == 0 or (i + 1) == total:
            pct = (i + 1) / total * 100
            logger.info(
                "Progress: [%d/%d] (%.1f%%) — ok=%d fail=%d",
                i + 1, total, pct, ok_count, fail_count,
            )

    logger.info("Phase 2 done: %d ok, %d fail", ok_count, fail_count)

    # ─── Content filter ──────────────────────────────────────────────

    filtered = [j for j in raw if is_valid_job(j)]
    dropped = len(raw) - len(filtered)
    if dropped:
        logger.info("Content filter: kept %d, dropped %d non-job items", len(filtered), dropped)

    # Re-number
    for idx, j in enumerate(filtered, start=1):
        j["id"] = idx

    # ─── Write output ─────────────────────────────────────────────────

    output: dict[str, Any] = {
        "site": {
            "name": SITE_NAME,
            "url": SITE_URL,
            "platform": PLATFORM,
            "scraping_method": SCRAPING_METHOD,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        },
        OUTPUT_KEY: filtered,
        "metadata": {
            "scraping_duration_seconds": round(time.time() - start, 2),
            "failed_products": dropped,
            "rate_limit_delay": DELAY,
        },
    }

    os.makedirs(os.path.dirname(OUTPUT_FILE) or ".", exist_ok=True)

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

    logger.info("=" * 80)
    logger.info("EXTRACTION COMPLETE")
    logger.info("Output: %d jobs (dropped %d)", len(filtered), dropped)
    logger.info("Duration: %.1fs", time.time() - start)
    logger.info("File: %s", OUTPUT_FILE)
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
