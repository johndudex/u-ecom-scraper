#!/usr/bin/env python3
"""HTTP Navigation Scraper — calls browser_service POST /navigate per page.

Site: thenorthface.com (client-hydrated storefront; direct HTTP is hard-403,
every fetch rides the browser_service cloak rung — cloak_none recipe).

Two-phase architecture (mirrors templates/navigation_scraper.py):

  Phase 1: Discover item URLs by crawling the promoted category/listing page,
           then paginating. Each page fetch is one POST /navigate call; link
           extraction + pagination are computed locally on the returned HTML.
  Phase 2: Extract structured data from each discovered item page. Item
           fetches run concurrently in a ThreadPoolExecutor; each item is
           one POST /navigate call. JSON-LD + CSS parsing happen locally.

Usage:
    python3 scraper.py --listing-url "https://www.thenorthface.com/en-us/c/womens-211718"
    python3 scraper.py --sample                                  # first 5 seed items only
    python3 scraper.py --input input_urls.json --sample          # seed-file test run
    python3 scraper.py --limit 50                                # cap item count
    python3 scraper.py                                           # FULL run (default listing)
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
from typing import Optional
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import httpx

# Simple HTTP helpers for form-search discovery (direct HTTP, not browser_service).

# [wave-15 3.4] One shared ladder closure per process (cookie continuity, job-58).
_FETCH_TEXT = None


def _get_fetch_text():
    global _FETCH_TEXT
    if _FETCH_TEXT is None:
        from src.http_fetch import create_fetch_text

        _FETCH_TEXT = create_fetch_text(
            delay_s=1.0,
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
    return _FETCH_TEXT


# [T1.3] The shared ladder's challenge signal. Guarded: the draft must keep
# working when the browser-service image predates src.http_fetch.
try:
    from src.http_fetch import SoftBlock  # noqa: E402
except ImportError:  # pragma: no cover
    SoftBlock = None


def _is_soft_block(obj) -> bool:
    """True when ``obj`` is the shared ladder's challenge signal.

    Guarded so a pre-src.http_fetch image (where no SoftBlock can ever
    occur) degrades to plain False instead of raising.
    """
    return SoftBlock is not None and isinstance(obj, SoftBlock)


def _http_get(url: str):
    """HTTP GET through the shared proxy ladder [wave-15 3.4].

    Returns (html, status_code); ("", 0) when every tier fails — the same
    falsy contract as before — OR the shared ladder's SoftBlock signal when
    the site answered a tier with a challenge-served-as-200 [T1.3].
    """
    try:
        result = _get_fetch_text()(url)
    except ImportError:
        try:
            with httpx.Client(headers={"User-Agent": "Mozilla/5.0"}, timeout=30, follow_redirects=True) as c:
                r = c.get(url)
                return r.text, r.status_code
        except Exception as exc:
            logger.warning("_http_get %s failed: %s", url[:60], exc)
            return "", 0
    if _is_soft_block(result):
        logger.warning(
            "SOFT BLOCK (200, %s) on %s — reporting the challenge signal",
            getattr(result, "reason", "?"), url[:60],
        )
        return result
    if not result:
        return "", 0
    return result

def _http_post(url: str, data: dict) -> tuple[str, int]:
    """Plain HTTP POST. Returns (html, status_code)."""
    try:
        with httpx.Client(headers={"User-Agent": "Mozilla/5.0"}, timeout=30, follow_redirects=True) as c:
            r = c.post(url, data=data)
            return r.text, r.status_code
    except Exception as exc:
        logger.warning("_http_post %s failed: %s", url[:60], exc)
        return "", 0
from bs4 import BeautifulSoup

# Make src.* importable (scraper runs from scrapers/{slug}/).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


# ── Local fallbacks for the (guarded) src.page_analysis helpers ──────────────
# Code Writer adapted: pure-python JSON-LD parsing + instant-fail check live
# here so the draft also runs on images without src.page_analysis. The real
# module is preferred when importable (identical call signature/return shape).


def _local_extract_jsonld(html: str) -> list:
    """Regex + json.loads extraction of application/ld+json blocks."""
    blocks: list = []
    if not html:
        return blocks
    for m in re.finditer(
        r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        html, re.S | re.I,
    ):
        raw = (m.group(1) or "").strip()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            blocks.append(parsed)
        elif isinstance(parsed, list):
            blocks.extend(b for b in parsed if isinstance(b, dict))
    return blocks


def _local_phase2_instant_fail(elapsed_s: float, total: int, min_fetch_s: float, workers: int = 1) -> bool:
    """True when Phase 2 finished faster than items*floor/workers allows."""
    try:
        if total <= 0:
            return False
        return elapsed_s < (total * min_fetch_s * 0.5) / max(int(workers) or 1, 1)
    except Exception:
        return False


try:
    from src.page_analysis import (  # noqa: E402  (pure-python helper, no browser import)
        extract_jsonld,
        phase2_instant_fail,
    )
except Exception:  # Code Writer adapted: guarded — degrade to local fallbacks
    extract_jsonld = _local_extract_jsonld
    phase2_instant_fail = _local_phase2_instant_fail

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION — code_writer substitutes {PLACEHOLDERS} from analysis artifacts.
# ═══════════════════════════════════════════════════════════════════════════════

SITE_NAME = "The North Face"
SITE_URL = "https://www.thenorthface.com"
PLATFORM = "unknown"
SITE_SLUG = "thenorthface-com"

# ── Execution model ──────────────────────────────────────────────────────────
BROWSER_SERVICE_URL = os.environ.get("BROWSER_SERVICE_URL", "http://browser_service:8001")

# Per-site cloak flag (access recipe: cloak_none — stealth browser, no proxy).
STEALTH = "cloak"
_env_stealth = (os.environ.get("STEALTH_BROWSER") or os.environ.get("SCRAPER_STEALTH") or "").strip().lower()
if _env_stealth in ("cloak", "true", "1"):
    STEALTH = "cloak"
elif STEALTH.startswith("{") and STEALTH.endswith("}"):
    STEALTH = "none"

NAVIGATE_TIMEOUT = 120

# Render-completeness floor: TNF hydrates client-side (~14s in the probe), the
# server escalates bounded backoff waits while the page still lacks content.
NAVIGATE_SETTLE_MS = 6000
_env_settle = os.environ.get("NAVIGATE_SETTLE_MS", "").strip()
try:
    NAVIGATE_SETTLE_MS = int(_env_settle or NAVIGATE_SETTLE_MS)
except (TypeError, ValueError):
    NAVIGATE_SETTLE_MS = 4000
NAVIGATE_SETTLE_MS = max(0, min(NAVIGATE_SETTLE_MS, 45000))

MAX_RETRIES = 3
BACKOFF_BASE = 2.0

PHASE2_WORKERS = 2
PHASE2_MIN_FETCH_S = 0.5

# ── Phase 1: Navigation ─────────────────────────────────────────────────────
SEARCH_URL_PATTERN = ""
SEARCH_BOX_SELECTOR = ""
SEARCH_SUBMIT_SELECTOR = ""
# Promoted listing from navigation_analysis (search criteria / category page).
CATEGORY_URLS: list = []

# ── Phase 1: Form-search iteration (unused on this site) ─────────────────────
FORM_ACTION = ""
FORM_METHOD = "POST"
FORM_SELECT_NAME = ""
FORM_BASE_URL = ""

# ── Phase 1: Pagination ─────────────────────────────────────────────────────
# discovery_config: type=load_more, page_param null → template falls back to
# semantic next-href extraction; paginate until exhaustion (no arbitrary cap).
PAGINATION_TYPE = "load_more"
NEXT_BUTTON_SELECTOR = ""
PAGE_PARAM_NAME = ""
ITEMS_PER_PAGE: Optional[int] = None
MAX_PAGES: Optional[int] = None
TOTAL_COUNT_SELECTOR = ""
DISCOVERY_DEADLINE_SECONDS = 300

# Coverage gate: no trusted total known for this site.
COVERAGE_TARGET_TOTAL: Optional[int] = None

# ── Phase 1: Item link extraction ───────────────────────────────────────────
# STRICT product-card tokens only: TNF PDPs are /en-us/p/<...> deep paths.
ITEM_CONTAINER_SELECTOR = ""
ITEM_LINK_SELECTOR = "a[href*='/p/']"
ITEM_URL_PATTERN = r"/en-us/p/"

# ── Phase 2: Extraction ─────────────────────────────────────────────────────
SCRAPING_METHOD = "http_navigation"
PROXY_TIER = "none"
_env_tier = (os.environ.get("SCRAPER_PROXY_TIER") or "").strip().lower()
if _env_tier in ("none", "datacenter", "residential"):
    PROXY_TIER = _env_tier
DELAY_BETWEEN_REQUESTS = 1.0

# Known-good promoted listing — the zero-yield self-heal fallback target.
DEFAULT_LISTING_URL = "https://www.thenorthface.com/en-us/c/womens-211718"

# ── Output ──────────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_KEY = "products"
CONTENT_TYPE = "product"
CURRENCY = "USD"

SRC_URL = SITE_URL

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
# CHECKPOINT — resume Phase 2 after a crash/retry without re-discovering.
# ═══════════════════════════════════════════════════════════════════════════════

_CHECKPOINT_PATH = os.path.join(SCRIPT_DIR, "discovered_urls_checkpoint.json")


def _write_checkpoint(urls: list[str]) -> None:
    """Save discovered URLs so a crash-retry can resume Phase 2 directly."""
    try:
        if not urls:
            try:
                if os.path.isfile(_CHECKPOINT_PATH):
                    with open(_CHECKPOINT_PATH, "r") as f:
                        prev = json.load(f)
                    if prev.get("urls"):
                        logger.warning(
                            "Checkpoint: refusing to overwrite %d banked URLs "
                            "with 0 — keeping %s",
                            len(prev["urls"]), _CHECKPOINT_PATH,
                        )
                        return
            except Exception:
                pass
        with open(_CHECKPOINT_PATH, "w") as f:
            json.dump({"urls": list(urls), "count": len(urls), "ts": time.time()}, f)
        logger.debug("Checkpoint: saved %d URLs to %s", len(urls), _CHECKPOINT_PATH)
    except Exception as exc:
        logger.warning("Checkpoint: write failed: %s", exc)


def _load_checkpoint() -> list[str]:
    """Load discovered URLs from a previous run's checkpoint (if any)."""
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
# CORE HTTP PRIMITIVE — one POST /navigate per page.
# ═══════════════════════════════════════════════════════════════════════════════

try:
    from src.geo import detect_country as _detect_country
except Exception:  # stripped image without src/ — degrade, never die
    _detect_country = None


def _effective_proxy_tier() -> str:
    """PROXY_TIER with the UNFILLED-placeholder case resolved (never sent raw)."""
    tier = PROXY_TIER if (PROXY_TIER and not PROXY_TIER.startswith("{")) else "none"
    return tier if tier in ("none", "datacenter", "residential") else "none"


def _navigate(url, actions=None, extract=None, retry=0, settle_ms=None):
    """POST /navigate with exponential backoff. Returns the response dict or None.

    Contract:
      - 200 + success=True  → return data (has url/html/data/...)
      - 200 + blocked=True  → terminal (anti-bot wall); return data
      - 429 / 503 / 502     → retryable; honor Retry-After
      - 5xx / timeouts      → retryable; exponential backoff
      - 404                 → terminal
    """
    payload = {
        "url": url,
        "actions": actions or [],
        "extract": extract or {},
        "stealth": "cloak" if str(STEALTH).lower() == "cloak" else "none",
        "proxy_tier": _effective_proxy_tier(),
        "country": (
            _detect_country(SITE_URL)
            if (_detect_country and _effective_proxy_tier() != "none")
            else None
        ),
        "timeout": NAVIGATE_TIMEOUT,
        "return_what": "all",
        "settle_ms": NAVIGATE_SETTLE_MS if settle_ms is None else max(0, int(settle_ms)),
    }
    endpoint = f"{BROWSER_SERVICE_URL}/navigate"
    last_throttled = False
    last_unavailable: dict = {}
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
                logger.debug("navigate: 404 on %s (terminal)", url[:80])
                return {"success": False, "url": url, "html": "", "status_code": 404}

            if r.status_code in (429, 502, 503):
                last_throttled = last_throttled or r.status_code == 429
                retry_after = r.headers.get("Retry-After")
                body: dict = {}
                try:
                    body = r.json() or {}
                except ValueError:
                    body = {}
                if not retry_after:
                    retry_after = body.get("retry_after")
                try:
                    retry_after = int(retry_after)
                except (TypeError, ValueError):
                    retry_after = 5
                if r.status_code in (502, 503):
                    last_unavailable = {
                        "status": r.status_code,
                        "server_error_class": body.get("error_class") or "",
                        "error": str(body.get("error") or r.text or "")[:300],
                    }
                    global _nav_unavailable_status, _nav_unavailable_class
                    _nav_unavailable_status = r.status_code
                    _nav_unavailable_class = last_unavailable["server_error_class"]
                    if attempt == 0 or attempt == MAX_RETRIES - 1:
                        logger.warning(
                            "navigate: BROWSER-SERVICE %d on %s (attempt %d/%d) error_class=%s error=%s",
                            r.status_code, url[:60], attempt + 1, MAX_RETRIES,
                            last_unavailable["server_error_class"] or "-",
                            last_unavailable["error"][:160] or "-",
                        )
                else:
                    logger.debug(
                        "navigate: %d on %s, backing off %ds (attempt %d/%d)",
                        r.status_code, url[:60], retry_after, attempt + 1, MAX_RETRIES,
                    )
                time.sleep(retry_after)
                continue
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            logger.debug(
                "navigate: transient error on %s: %s (attempt %d/%d)",
                url[:60], exc, attempt + 1, MAX_RETRIES,
            )

        time.sleep(min(BACKOFF_BASE ** (attempt + retry), 30))

    logger.warning("navigate: exhausted %d retries on %s", MAX_RETRIES, url[:80])
    if last_unavailable:
        return {
            "success": False,
            "navigate_unavailable": True,
            "url": url,
            "html": "",
            **last_unavailable,
        }
    if last_throttled:
        return {"success": False, "throttled": True, "status": 429, "url": url, "html": ""}
    return None


def _nav_fail_reason(resp) -> str:
    """Stop reason for a failed ``_navigate`` call (B3)."""
    if isinstance(resp, dict):
        if resp.get("navigate_unavailable"):
            return "navigate_unavailable"
        if resp.get("throttled"):
            return "navigate_throttled"
    return "navigate_error"


_nav_unavailable_status = 0
_nav_unavailable_class = ""


# ═══════════════════════════════════════════════════════════════════════════════
# URL HELPERS — pure functions, no browser dependency.
# ═══════════════════════════════════════════════════════════════════════════════


def _make_absolute(href: str) -> str:
    """Resolve a possibly-relative href against SITE_URL.

    A leading '//' is protocol-relative ONLY when the next segment is a
    plausible netloc (contains a dot). Otherwise it is a malformed site path.
    """
    if not href:
        return ""
    if href.startswith(("http://", "https://")):
        return href
    if href.startswith("//"):
        rest = href[2:]
        if "." in rest.split("/", 1)[0]:
            return "https:" + href
        return SITE_URL.rstrip("/") + "/" + rest
    if href.startswith("/"):
        return SITE_URL.rstrip("/") + href
    return SITE_URL.rstrip("/") + "/" + href


def _is_product_url(href: str) -> bool:
    """Generic item-detail URL detector — no site-specific tokens."""
    if not href:
        return False
    site_host = (urlparse(SITE_URL).hostname or "").lower()
    if site_host and site_host not in href.lower():
        return False
    path = urlparse(href).path.strip("/")
    if not path or len(path) < 6:
        return False
    segs = path.split("/")
    last = segs[-1]
    if len(segs) == 1 and len(last) < 12 and not any(c.isdigit() for c in last):
        return False
    return True


def _set_query_param(url: str, param: str, value) -> str:
    """Return ``url`` with the query ``param`` REPLACED (not appended)."""
    p = urlparse(url)
    qs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k != param]
    qs.append((param, str(value)))
    return urlunparse(p._replace(query=urlencode(qs)))


_OFFSET_PARAMS = {"offset", "start", "skip", "begin", "from"}


def _extract_next_href(html: str) -> Optional[str]:
    """Find a 'next page' href in listing HTML via semantic selectors."""
    if not html:
        return None
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return None
    if NEXT_BUTTON_SELECTOR and NEXT_BUTTON_SELECTOR != "{NEXT_BUTTON_SELECTOR}":
        try:
            el = soup.select_one(NEXT_BUTTON_SELECTOR)
            if el and el.get("href"):
                return _make_absolute(el["href"])
        except Exception:
            pass
    for sel in ('a[rel="next"]', "a.next", "li.next a"):
        try:
            el = soup.select_one(sel)
            if el and el.get("href"):
                return _make_absolute(el["href"])
        except Exception:
            pass
    return None


def _get_next_page_url(final_url: str, next_page_num: int, html: str = None) -> Optional[str]:
    """Construct the URL for the next page of results.

    PREFERRED: construct ``?{PAGE_PARAM_NAME}=N`` directly on the post-action
    ``final_url``. Falls back to a semantic next-button href parsed from HTML.
    """
    if PAGE_PARAM_NAME and PAGE_PARAM_NAME not in ("", "{PAGE_PARAM_NAME}"):
        if PAGINATION_TYPE in ("page_param", "", None) or (
            PAGINATION_TYPE not in ("cursor", "infinite_scroll", "load_more")
        ):
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
    "navigate_unavailable": 6,
    "navigate_error": 5,
    "empty_first_page": 5,
    "dedup_flat": 4,
    "navigate_throttled": 3,
    "max_pages_hit": 3,
    "no_new_items": 2,
    "short_page": 1,
    "no_next_link": 0,
    "skipped": -1,
}


def _merge_stop_reason(current: str, new: str) -> str:
    """Return the more-concerning of two stop_reasons (highest priority wins)."""
    if _STOP_REASON_PRIORITY.get(new, 0) > _STOP_REASON_PRIORITY.get(current, 0):
        return new
    return current


def _build_search_actions(query: str) -> list[dict]:
    """Build the /navigate action list for a form-driven search submit."""
    actions: list[dict] = []
    if SEARCH_BOX_SELECTOR and SEARCH_BOX_SELECTOR != "{SEARCH_BOX_SELECTOR}":
        actions.append({"type": "fill", "selector": SEARCH_BOX_SELECTOR, "value": query})
    if SEARCH_SUBMIT_SELECTOR and SEARCH_SUBMIT_SELECTOR != "{SEARCH_SUBMIT_SELECTOR}":
        actions.append({"type": "click", "selector": SEARCH_SUBMIT_SELECTOR})
    actions.append({"type": "wait", "state": "domcontentloaded"})
    actions.append({"type": "sleep", "ms": 8000})
    return actions


def _extract_item_links(html: str) -> list[str]:
    """Extract item page URLs from listing HTML — local parse, 3-tier fallback.

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

    if ITEM_CONTAINER_SELECTOR and ITEM_CONTAINER_SELECTOR != "{ITEM_CONTAINER_SELECTOR}":
        try:
            containers = soup.select(ITEM_CONTAINER_SELECTOR)
        except Exception as exc:
            logger.warning("Phase 1: bad ITEM_CONTAINER_SELECTOR %r: %s", ITEM_CONTAINER_SELECTOR, exc)
            containers = []
        for container in containers:
            try:
                matches = container.select(ITEM_LINK_SELECTOR) if (
                    ITEM_LINK_SELECTOR and ITEM_LINK_SELECTOR != "{ITEM_LINK_SELECTOR}"
                ) else []
            except Exception:
                matches = []
            for a in matches:
                href = a.get("href", "")
                if href:
                    links.append(_make_absolute(href))

    if not links and ITEM_LINK_SELECTOR and ITEM_LINK_SELECTOR != "{ITEM_LINK_SELECTOR}":
        try:
            for a in soup.select(ITEM_LINK_SELECTOR):
                href = a.get("href", "")
                if href:
                    links.append(_make_absolute(href))
        except Exception as exc:
            logger.warning("Phase 1: bare link selector failed: %s", exc)

    if len(links) < 20:
        pattern = None
        if ITEM_URL_PATTERN and ITEM_URL_PATTERN not in ("", "{ITEM_URL_PATTERN}"):
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


def _discover_urls_via_search(
    query: str,
    max_pages: Optional[int] = None,
    limit: Optional[int] = None,
) -> tuple[list[str], str]:
    """Phase 1a: Discover item URLs by submitting the site's search form."""
    search_url = (
        SEARCH_URL_PATTERN.replace("{query}", query)
        if "{query}" in SEARCH_URL_PATTERN
        else SEARCH_URL_PATTERN
    )
    logger.info("Phase 1: Searching for '%s' → %s", query, search_url)

    actions = _build_search_actions(query)
    resp = _navigate(search_url, actions=actions)
    if not resp or not resp.get("success"):
        blocked = bool(resp and resp.get("blocked"))
        fail_reason = _nav_fail_reason(resp)
        logger.error(
            "Phase 1: search navigate failed for %s%s (stop_reason=%s)",
            search_url, " (blocked)" if blocked else "", fail_reason,
        )
        return [], fail_reason

    final_url = resp.get("url") or search_url
    html = resp.get("html", "")
    all_urls: list[str] = _extract_item_links(html)
    logger.info("Phase 1: search page 1 → %d items (final_url=%s)", len(all_urls), final_url[:80])

    stop_reason = "no_next_link"
    current_page = 1
    while True:
        if max_pages and current_page >= max_pages:
            logger.info("Phase 1: Reached max_pages=%d", max_pages)
            stop_reason = "max_pages_hit"
            break
        if limit and len(all_urls) >= limit:
            logger.info("Phase 1: Reached limit=%d", limit)
            stop_reason = "max_pages_hit"
            break

        next_url = _get_next_page_url(final_url, current_page + 1, html)
        if not next_url:
            logger.info("Phase 1: No more pages (stopped at page %d)", current_page)
            stop_reason = "no_next_link"
            break

        logger.info("Phase 1: Navigating to page %d", current_page + 1)
        resp = _navigate(next_url)
        if not resp or not resp.get("success"):
            stop_reason = _nav_fail_reason(resp)
            logger.warning(
                "Phase 1: page %d navigate failed, stopping (%s)", current_page + 1, stop_reason
            )
            break

        final_url = resp.get("url") or next_url
        html = resp.get("html", "")
        new_urls = _extract_item_links(html)
        new_count = len(set(new_urls) - set(all_urls))
        logger.info(
            "Phase 1: Page %d → %d items (%d new)",
            current_page + 1, len(new_urls), new_count,
        )

        if new_count == 0:
            if not new_urls or (ITEMS_PER_PAGE and len(new_urls) < ITEMS_PER_PAGE):
                stop_reason = "short_page"
            else:
                stop_reason = "no_new_items"
            logger.info("Phase 1: page %d stopping (%s)", current_page + 1, stop_reason)
            break

        all_urls.extend(new_urls)
        current_page += 1
        if DELAY_BETWEEN_REQUESTS:
            time.sleep(DELAY_BETWEEN_REQUESTS)

    unique_urls = list(dict.fromkeys(all_urls))
    if limit:
        unique_urls = unique_urls[:limit]
    logger.info("Phase 1: Discovered %d total item URLs via search (%s)", len(unique_urls), stop_reason)
    return unique_urls, stop_reason


def _discover_urls_via_form_search(
    max_pages: Optional[int] = None,
    limit: Optional[int] = None,
) -> tuple[list[str], str]:
    """Phase 1c: Discover item URLs by iterating through ALL options of a <select>."""
    from urllib.parse import urljoin
    from bs4 import BeautifulSoup

    all_urls: list[str] = []
    seen_ids: set[str] = set()

    form_page_url = FORM_BASE_URL or SEARCH_URL_PATTERN.split("?")[0]
    form_action_url = urljoin(form_page_url, FORM_ACTION) if FORM_ACTION else ""
    if not form_action_url or not FORM_SELECT_NAME:
        logger.error("Phase 1 (form-search): FORM_ACTION or FORM_SELECT_NAME not set")
        return [], "navigate_error"

    form_html = _http_get(form_page_url)
    if _is_soft_block(form_html):
        logger.error(
            "Phase 1 (form-search): form page %s soft-blocked (%s)",
            form_page_url[:80], getattr(form_html, "reason", "?"),
        )
        return [], "navigate_error"
    if not form_html:
        logger.error("Phase 1 (form-search): could not fetch form page %s", form_page_url)
        return [], "navigate_error"

    form_soup = BeautifulSoup(form_html, "html.parser")

    hidden_fields: dict[str, str] = {}
    for inp in form_soup.find_all("input", {"type": "hidden"}):
        name = inp.get("name", "")
        val = inp.get("value", "")
        if name:
            hidden_fields[name] = val
    logger.info("Phase 1 (form-search): %d hidden fields from form page", len(hidden_fields))

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

        form_data = {**hidden_fields, FORM_SELECT_NAME: opt_val}
        page_num = 1
        while True:
            if max_pages and page_num > max_pages:
                break
            if time.time() > deadline:
                break

            if FORM_METHOD.upper() == "POST":
                resp_html, status = _http_post(form_action_url, form_data)
            else:
                _got = _http_get(form_action_url + "?" + "&".join(f"{k}={v}" for k, v in form_data.items()))
                if _is_soft_block(_got):
                    logger.warning("Phase 1 (form-search): option '%s' page %d soft-blocked (%s)", opt_text[:30], page_num, getattr(_got, "reason", "?"))
                    break
                resp_html, status = _got

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
                break

            soup = BeautifulSoup(resp_html, "html.parser")
            next_link = None
            for sel in ['a[rel="next"]', 'a.next', 'li.next a', 'a[aria-label="Next"]']:
                el = soup.select_one(sel)
                if el and el.get("href"):
                    next_link = urljoin(form_action_url, el["href"])
                    break
            if not next_link:
                next_link_test = f"{form_action_url}?page={page_num + 1}"
                if f"page={page_num + 1}" not in resp_html:
                    break
                form_data = {**hidden_fields, FORM_SELECT_NAME: opt_val}
                break
            else:
                _next_got = _http_get(next_link)
                if _is_soft_block(_next_got):
                    logger.warning("Phase 1 (form-search): next page soft-blocked (%s)", getattr(_next_got, "reason", "?"))
                    break
                next_html, next_status = _next_got
                if not next_html or next_status >= 400:
                    break
                next_urls = _extract_item_links(next_html)
                for url in next_urls:
                    item_id = re.search(r'/job-(\d+)', url) or re.search(r'/(\d{4,})/?$', url)
                    dedup_key = item_id.group(1) if item_id else url
                    if dedup_key not in seen_ids:
                        seen_ids.add(dedup_key)
                        all_urls.append(url)
                page_num += 1
                if not next_urls:
                    break

            page_num += 1

        logger.info("Phase 1 (form-search): option '%s' → %d total URLs so far", opt_text[:30], len(all_urls))

    unique_urls = list(dict.fromkeys(all_urls))
    if limit:
        unique_urls = unique_urls[:limit]
    logger.info("Phase 1 (form-search): Discovered %d total item URLs across %d options (%s)",
                len(unique_urls), len(options), stop_reason)
    return unique_urls, stop_reason


def _discover_urls_via_category(
    category_url: str,
    max_pages: Optional[int] = None,
    limit: Optional[int] = None,
) -> tuple[list[str], str]:
    """Phase 1b: Discover item URLs from a category/listing page."""
    logger.info("Phase 1: Browsing category → %s", category_url)
    resp = _navigate(category_url)
    if not resp or not resp.get("success"):
        blocked = bool(resp and resp.get("blocked"))
        fail_reason = _nav_fail_reason(resp)
        logger.error(
            "Phase 1: category navigate failed for %s%s (stop_reason=%s)",
            category_url, " (blocked)" if blocked else "", fail_reason,
        )
        return [], fail_reason

    final_url = resp.get("url") or category_url
    html = resp.get("html", "")
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

        next_url = _get_next_page_url(final_url, current_page + 1, html)
        if not next_url:
            stop_reason = "no_next_link"
            break

        logger.info("Phase 1: Category page %d", current_page + 1)
        resp = _navigate(next_url)
        if not resp or not resp.get("success"):
            stop_reason = _nav_fail_reason(resp)
            break

        final_url = resp.get("url") or next_url
        html = resp.get("html", "")
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
    logger.info("Phase 1: Discovered %d total item URLs from category (%s)", len(unique_urls), stop_reason)
    return unique_urls, stop_reason


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 2: ITEM EXTRACTION (concurrent)
# ═══════════════════════════════════════════════════════════════════════════════


def _norm_price(value) -> Optional[float]:
    """Strip currency symbols/whitespace from a price. Returns a FLOAT or None."""
    if value is None:
        return None
    cleaned = re.sub(r"[^\d.,-]", "", str(value).strip())
    if not re.search(r"\d", cleaned):
        return None
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = re.sub(r",(?=\d{3}(?:\D|$))", "", cleaned).replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _norm_availability(value) -> Optional[str]:
    """Normalize availability to ``in_stock`` / ``out_of_stock``."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "in_stock" if value else "out_of_stock"
    text = str(value).strip().lower()
    if not text:
        return None
    if "://" in text:
        text = text.rsplit("/", 1)[-1]
    compact = text.replace("-", "_").replace(" ", "")
    if compact in ("in_stock", "instock", "available"):
        return "in_stock"
    if compact in ("out_of_stock", "outofstock", "unavailable", "sold_out", "soldout"):
        return "out_of_stock"
    return text


# ── Code Writer adapted: TNF-specific extraction helpers ─────────────────────
# The storefront hydrates client-side; the server HTML carries the JSON-LD
# @graph while the buybox (price/availability) renders after hydration. All
# selectors below run on the POST-hydration HTML /navigate returns.

_CURRENCY_SYMBOLS = {"$": "USD", "£": "GBP", "€": "EUR", "¥": "JPY"}
_PRICE_PREV_HINT_RE = re.compile(r"was|strike|compare|original|regular|list", re.I)
_REC_SCOPE_HINT_RE = re.compile(
    r"recommend|carousel|also-?like|similar|related|recently|suggestion", re.I
)
_SOLD_OUT_RE = re.compile(
    r"sold out|out of stock|notify me|back in stock|unavailable|discontinued", re.I
)
_ADD_CART_RE = re.compile(r"add to (cart|bag|basket)", re.I)
_SOFT404_RE = re.compile(
    r"\b(not found|no longer available|discontinued|no longer exists|"
    r"page cannot be found|404 error|error 404)\b",
    re.I,
)


def _iter_jsonld_nodes(blocks) -> list:
    """Yield every node from JSON-LD blocks, flattening ``@graph`` envelopes.

    Code Writer adapted (port of analyzer JS): TNF ships ONE ld+json script
    whose root is {@context, @graph: [5 nodes]} — the FIRST node is WebSite
    and the Product/ProductGroup node lives INSIDE @graph. Never expect a
    top-level Product block.
    """
    nodes: list = []
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        graph = block.get("@graph")
        if isinstance(graph, list):
            for node in graph:
                if isinstance(node, dict):
                    nodes.append(node)
        else:
            nodes.append(block)
    return nodes


def _node_type(node: dict) -> str:
    t = node.get("@type", "")
    if isinstance(t, list):
        return " ".join(str(x) for x in t)
    return str(t)


def _collect_offer_dicts(offers) -> list:
    """Flatten every offers shape: dict, list, AggregateOffer{offers:[...]}."""
    out: list = []
    stack = [offers]
    while stack:
        cur = stack.pop(0)
        if isinstance(cur, list):
            stack.extend(cur)
        elif isinstance(cur, dict):
            out.append(cur)
            inner = cur.get("offers")
            if inner is not None and inner is not cur:
                stack.append(inner)
    return out


def _populate_from_jsonld(item: dict, jsonld_blocks: list) -> bool:
    """Fill ``item`` from JSON-LD, @graph-aware. Returns True on Product found.

    Code Writer adapted: replaces the template's flat-block dispatch. This
    site wraps Product inside @graph and (for color/size families) may use a
    ProductGroup with an AggregateOffer (lowPrice/offers[]/priceSpecification).
    The user schema is EXACTLY {product name, price, currency, description,
    availability} — no other product fields are emitted. Coexisting
    price-like values are oriented BY VALUE: lower = current price.
    """
    found_product = False
    for node in _iter_jsonld_nodes(jsonld_blocks):
        if not re.search(r"product", _node_type(node), re.I):
            continue
        found_product = True

        if node.get("name") and not item.get("title"):
            item["title"] = str(node["name"]).strip()
        if node.get("description") and not item.get("description"):
            item["description"] = str(node["description"]).strip()

        offer_dicts = _collect_offer_dicts(node.get("offers"))
        price_vals: list[float] = []
        currency = ""
        availability = ""
        specs: list = []
        for offer in offer_dicts:
            if isinstance(offer.get("priceSpecification"), dict):
                specs.append(offer["priceSpecification"])
            for key in ("price", "lowPrice"):
                if offer.get(key) is not None:
                    val = _norm_price(offer.get(key))
                    if val is not None:
                        price_vals.append(val)
            if not currency and offer.get("priceCurrency"):
                currency = str(offer["priceCurrency"]).strip()
            if not availability and offer.get("availability"):
                availability = str(offer["availability"]).strip()
        for spec in specs:
            if spec.get("price") is not None:
                val = _norm_price(spec.get("price"))
                if val is not None:
                    price_vals.append(val)
            if not currency and spec.get("priceCurrency"):
                currency = str(spec["priceCurrency"]).strip()

        if price_vals and not item.get("price"):
            item["price"] = min(price_vals)
        if currency and not item.get("currency"):
            item["currency"] = currency
        if availability and not item.get("availability"):
            item["availability"] = _norm_availability(availability)
        if "currency" not in item or not item.get("currency"):
            item["currency"] = CURRENCY
    return found_product


def _in_recommendation_scope(node) -> bool:
    """True when the node sits inside a recommendations/carousel container."""
    parent = node.parent
    depth = 0
    while parent is not None and depth < 12:
        try:
            marker = " ".join(filter(None, [
                " ".join(parent.get("class") or []),
                parent.get("id") or "",
                parent.get("data-testid") or "",
            ]))
        except Exception:
            return False
        if _REC_SCOPE_HINT_RE.search(marker):
            return True
        parent = parent.parent
        depth += 1
    return False


def _dom_price_fallback(soup) -> dict:
    """Client-hydrated buybox fallback — price + currency from price-ish nodes.

    Scoped to genuine price nodes only: previous-price markers (was/strike/
    compare/...) and recommendation carousels are excluded; coexisting values
    are oriented by VALUE (lower = current).
    """
    out: dict = {"price": None, "currency": None}
    values: list[float] = []
    for sel in ("[data-price]", "[data-testid*='price' i]", "[class*='price' i]"):
        try:
            nodes = soup.select(sel)
        except Exception:
            continue
        for node in nodes:
            try:
                if _in_recommendation_scope(node):
                    continue
                marker = " ".join(filter(None, [
                    " ".join(node.get("class") or []),
                    node.get("id") or "",
                    node.get("data-testid") or "",
                ]))
                if _PRICE_PREV_HINT_RE.search(marker):
                    continue
                text = node.get_text(" ", strip=True)
            except Exception:
                continue
            if not text or len(text) > 80:
                continue
            for amount in re.findall(r"[$£€]\s?\d[\d.,]*", text):
                val = _norm_price(amount)
                if val is not None and 1 <= val <= 100000:
                    values.append(val)
                    if out["currency"] is None:
                        for sym, code in _CURRENCY_SYMBOLS.items():
                            if sym in amount:
                                out["currency"] = code
                                break
        if values:
            break
    if values:
        out["price"] = min(values)
    return out


def _dom_availability_fallback(soup) -> Optional[str]:
    """Availability fallback — buybox-scoped, strong tokens only."""
    for sel in (
        "[itemprop='availability']",
        "[data-testid*='availability' i]",
        "[class*='availability' i]",
        "[data-testid*='stock' i]",
        "[data-inventory]",
        "[data-stock]",
    ):
        try:
            node = soup.select_one(sel)
        except Exception:
            node = None
        if not node:
            continue
        raw = (
            node.get("content")
            or node.get("href")
            or node.get("data-availability")
            or node.get("data-inventory")
            or node.get("data-stock")
            or node.get_text(" ", strip=True)
        )
        norm = _norm_availability(raw)
        if norm in ("in_stock", "out_of_stock"):
            return norm
    in_stock_seen = False
    try:
        ctas = soup.select("button, a, input[type='submit'], [role='button']")
    except Exception:
        ctas = []
    for node in ctas:
        try:
            text = node.get_text(" ", strip=True)
        except Exception:
            continue
        if not text or len(text) > 60:
            continue
        if _SOLD_OUT_RE.search(text):
            return "out_of_stock"
        if _ADD_CART_RE.search(text):
            in_stock_seen = True
    return "in_stock" if in_stock_seen else None


def _error_item(url: str, src_url: str, error: str) -> dict:
    return {
        "url": url,
        "src_url": src_url,
        "status_code": 0,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": f"Error: {error[:200]}",
    }


def _hreflang_alternates(html: str) -> dict:
    """hreflang → absolute URL map from ``<link rel="alternate">`` (first wins)."""
    alts: dict = {}
    try:
        soup = BeautifulSoup(html, "html.parser")
        for link in soup.find_all("link", rel=lambda v: v and "alternate" in v):
            lang = (link.get("hreflang") or "").strip().lower()
            href = (link.get("href") or "").strip()
            if lang and href.startswith("http"):
                alts.setdefault(lang, href)
    except Exception:
        pass
    return alts


def _locale_escalation_urls(html: str, item_url: str, max_candidates: int = 2) -> list:
    """Locale alternates of this page worth one retry, best-first [wave-17 S15b]."""
    try:
        from urllib.parse import urlparse as _urlparse
    except Exception:  # pragma: no cover
        return []
    alts = _hreflang_alternates(html)
    if not alts:
        return []
    item_host = (_urlparse(item_url).hostname or "").lower()
    orig = item_url.split("?", 1)[0].split("#", 1)[0]
    egress = ""
    if _detect_country:
        try:
            egress = (_detect_country(SITE_URL) or "").strip().lower()
        except Exception:
            egress = ""
    ordered_langs = []
    if egress and egress != "us":
        ordered_langs.append(f"en-{egress}")
    ordered_langs += ["en-us", "x-default"]
    urls: list = []
    for lang in ordered_langs:
        u = alts.get(lang)
        if not u:
            continue
        if (_urlparse(u).hostname or "").lower() != item_host:
            continue
        if u.split("?", 1)[0].split("#", 1)[0] == orig:
            continue
        if u not in urls:
            urls.append(u)
        if len(urls) >= max_candidates:
            break
    return urls


def _extract_item(item_url: str, src_url: str) -> dict:
    """Phase 2: Extract structured data from a single item page.

    One POST /navigate call fetches the page (after any redirects). JSON-LD +
    CSS parsing happen locally on the returned HTML. Failures become error
    dicts so the job continues instead of aborting on one bad page.
    """
    if DELAY_BETWEEN_REQUESTS:
        time.sleep(DELAY_BETWEEN_REQUESTS)

    resp = _navigate(item_url)
    if not resp:
        return _error_item(item_url, src_url, "navigate failed after retries")
    if resp.get("navigate_unavailable"):
        return _error_item(
            item_url,
            src_url,
            "browser-service unavailable"
            + (f" ({resp.get('server_error_class') or 'http ' + str(resp.get('status'))})" if resp.get("server_error_class") or resp.get("status") else ""),
        )
    if resp.get("blocked"):
        return _error_item(item_url, src_url, "blocked (anti-bot wall)")
    if resp.get("status_code") == 404:
        return _error_item(item_url, src_url, "404 not found")

    html = resp.get("html", "")
    final_url = resp.get("url") or item_url
    item: dict = {
        "url": item_url,
        "src_url": src_url,
        "status_code": 200,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": "",
    }

    # JSON-LD extraction (@graph-aware).
    found_product = False
    try:
        jsonld_blocks = extract_jsonld(html)
        if jsonld_blocks:
            found_product = _populate_from_jsonld(item, jsonld_blocks)
    except Exception as exc:
        logger.warning("Phase 2: JSON-LD extraction failed for %s: %s", item_url[:60], exc)

    # Local DOM parse (title h1 fallback + hydrated-buybox fallbacks + soft 404).
    soup = None
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        soup = None
    h1_text = ""
    if soup:
        try:
            h1 = soup.select_one("h1")
            if h1:
                h1_text = h1.get_text(strip=True)
        except Exception:
            h1_text = ""
        if h1_text and not item.get("title"):
            item["title"] = h1_text

        # Code Writer adapted: DOM fallbacks for the client-hydrated buybox.
        if not item.get("price"):
            price_fb = _dom_price_fallback(soup)
            if price_fb.get("price") is not None:
                item["price"] = price_fb["price"]
            if price_fb.get("currency") and not item.get("currency"):
                item["currency"] = price_fb["currency"]
        if not item.get("availability"):
            av = _dom_availability_fallback(soup)
            if av:
                item["availability"] = av
        if not item.get("description"):
            for sel in ("meta[property='og:description']", "meta[name='description']"):
                try:
                    meta = soup.select_one(sel)
                except Exception:
                    meta = None
                if meta is not None and meta.get("content"):
                    item["description"] = str(meta.get("content")).strip()
                    break
        try:
            og_url = soup.select_one("meta[property='og:url']")
            if og_url is not None and og_url.get("content"):
                item["url"] = _make_absolute(str(og_url.get("content")).strip())
        except Exception:
            pass

    # Code Writer adapted: soft-404 detection (REQUIRED per spec).
    soft404_reason = ""
    probe_text = " ".join(filter(None, [item.get("title", ""), h1_text]))
    if _SOFT404_RE.search(probe_text):
        soft404_reason = "product not found in page title/h1"
    elif (
        re.search(r"/p/", urlparse(item_url).path or "")
        and not re.search(r"/p/", urlparse(final_url).path or "")
    ):
        soft404_reason = f"redirected off product path → {final_url[:120]}"
    elif not found_product and not item.get("price"):
        soft404_reason = "no Product JSON-LD and no price on page"
    if soft404_reason:
        logger.info("Phase 2: soft-404 on %s (%s)", item_url[:80], soft404_reason)
        item["title"] = ""
        item["price"] = None
        item["availability"] = None
        item["description"] = ""
        item["remarks"] = f"Soft 404: {soft404_reason}"
        return item

    # [wave-17 S15b] Locale escalation for a price-less PDP render.
    if not item.get("price"):
        for alt_url in _locale_escalation_urls(html, item_url):
            alt_resp = _navigate(alt_url)
            if (
                not alt_resp
                or alt_resp.get("blocked")
                or alt_resp.get("navigate_unavailable")
                or not alt_resp.get("html")
            ):
                continue
            alt_item: dict = dict(item)
            try:
                alt_blocks = extract_jsonld(alt_resp["html"])
                if alt_blocks:
                    _populate_from_jsonld(alt_item, alt_blocks)
            except Exception:
                pass
            if alt_item.get("price"):
                for k, v in alt_item.items():
                    if v and not item.get(k):
                        item[k] = v
                item["locale_escalated_from"] = item_url
                item["locale_escalated_url"] = alt_url
                break

    return item


def _extract_item_safe(item_url: str, src_url: str) -> dict:
    """Phase 2 wrapper — never raises; converts any exception to an error item."""
    try:
        return _extract_item(item_url, src_url)
    except Exception as exc:
        logger.error("Phase 2: unexpected failure on %s: %s", item_url[:80], exc)
        return _error_item(item_url, src_url, str(exc))


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════


# ── CLI CONTRACT — keep every line below when adapting ───────────────────────
# The pipeline launches this scraper with EXACTLY these names. A flag missing
# from the argparse below is STRIPPED at launch and discovery silently falls
# back to the seed file (input_urls.json). Same for the SCRAPER_LISTING_URL
# env read in main().
# ──────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description=f"{SITE_NAME} HTTP Navigation Scraper")
    parser.add_argument("--query", type=str, help="Search query for navigation mode")
    parser.add_argument("--category-url", type=str, help="Category URL to crawl")
    parser.add_argument("--listing-url", type=str, help="Listing page URL to paginate")
    parser.add_argument("--sample", action="store_true", help="Scrape first 5 items only")
    parser.add_argument("--limit", type=int, default=None, help="Max items to scrape")
    # Code Writer adapted: seed-file contract flags (--input checked BEFORE the
    # checkpoint gate; --urls takes inline product URLs).
    parser.add_argument("--input", type=str, help="Path to input URLs JSON file")
    parser.add_argument("--urls", type=str, nargs="+", help="Product URLs as CLI arguments")
    parser.add_argument(
        "--no-proxy", action="store_true",
        help="Disable proxy (proxy_tier forced to 'none' in /navigate body)",
    )
    parser.add_argument(
        "--headless", action="store_true", default=True,
        help="Accepted for CLI compatibility (browser launches are server-side)",
    )
    parser.add_argument(
        "--discover-only", action="store_true",
        help="Run Phase 1 discovery to exhaustion, emit the output JSON with the "
             "discovery_coverage metadata block populated, then SKIP Phase 2 "
             "extraction.",
    )
    parser.add_argument(
        "--fresh-discovery", action="store_true",
        help="Ignore any discovered_urls_checkpoint.json and run Phase 1 from "
             "scratch (still writes a checkpoint as normal).",
    )
    args = parser.parse_args()

    # --no-proxy overrides the configured PROXY_TIER for this run.
    global PROXY_TIER
    if args.no_proxy:
        PROXY_TIER = "none"

    # F6 DETERMINISTIC DISCOVERY GATE (env-var): feeds the existing CLI
    # contract rather than adding a parallel branch — run_execution injects
    # SCRAPER_LISTING_URL because the LLM-adapted argparse may drop the flags.
    _env_listing = os.environ.get("SCRAPER_LISTING_URL", "").strip()
    if _env_listing:
        args.listing_url = _env_listing
        args.fresh_discovery = True  # the checkpoint gate below honors this
        logger.info("Env gate: SCRAPER_LISTING_URL → --listing-url %s (fresh)",
                    _env_listing[:80])

    # ── Code Writer adapted: seed URLs (--input / --urls) ───────────────────
    # HARD CONTRACT: args.input is checked BEFORE _load_checkpoint(); when a
    # seed source is present, the checkpoint load is skipped entirely and
    # Phase 1 discovery is not re-run for those URLs. Same-host only.
    seed_urls: list[str] = list(args.urls) if args.urls else []
    if args.input:
        _input_path = args.input
        if not os.path.isfile(_input_path):
            _candidate = os.path.join(SCRIPT_DIR, _input_path)
            if os.path.isfile(_candidate):
                _input_path = _candidate
        if os.path.isfile(_input_path):
            try:
                with open(_input_path, "r") as _f:
                    _data = json.load(_f)
                if isinstance(_data, dict):
                    seed_urls = [u for u in _data.get("urls", []) if isinstance(u, str)]
                elif isinstance(_data, list):
                    seed_urls = [u for u in _data if isinstance(u, str)]
                logger.info("--input: %d URLs loaded from %s", len(seed_urls), _input_path)
            except Exception as exc:
                logger.warning("--input: failed to parse %s: %s", _input_path, exc)
        else:
            logger.warning("--input: file not found: %s", args.input)
    _site_host = (urlparse(SITE_URL).hostname or "").lower()
    seed_urls = [
        u for u in seed_urls
        if (urlparse(u).hostname or "").lower() == _site_host
    ]
    if args.sample and not seed_urls:
        # --sample must ride URLs already in input_urls.json (never discovery).
        _default_input = os.path.join(SCRIPT_DIR, "input_urls.json")
        if os.path.isfile(_default_input):
            try:
                with open(_default_input, "r") as _f:
                    _data = json.load(_f)
                seed_urls = [
                    u for u in (_data.get("urls", []) if isinstance(_data, dict) else [])
                    if isinstance(u, str) and (urlparse(u).hostname or "").lower() == _site_host
                ]
                if seed_urls:
                    logger.info(
                        "--sample: using %d URLs from %s (Phase 1 discovery skipped)",
                        len(seed_urls), _default_input,
                    )
            except Exception as exc:
                logger.warning("--sample: default input file parse failed: %s", exc)

    limit = 5 if args.sample else args.limit
    start_time = time.time()
    discovered_urls: list[str] = []

    # ── Coverage-gate state (contract §1) ───────────────────────────────────
    ran_phase1 = True
    skipped_reason: Optional[str] = None
    aggregate_stop_reason = "no_next_link"
    max_pages_hit = False
    dimensions_iterated = 0
    dimensions_total = len(CATEGORY_URLS) if isinstance(CATEGORY_URLS, list) else 0

    # ── Resume from checkpoint if present (unless --fresh-discovery / seeds) ─
    # Code Writer adapted: seed input takes precedence over the checkpoint.
    checkpoint_urls = [] if (seed_urls or args.fresh_discovery) else _load_checkpoint()
    if seed_urls:
        discovered_urls = seed_urls
        ran_phase1 = False
        skipped_reason = "seed_input"
        aggregate_stop_reason = "skipped"
        src_url_base = None  # per-item src_url (== the item URL) for seeds
        logger.info(
            "Seed input: %d URLs (--input/--urls) — checkpoint + Phase 1 skipped",
            len(discovered_urls),
        )
    elif checkpoint_urls:
        discovered_urls = checkpoint_urls
        ran_phase1 = False
        skipped_reason = "checkpoint_loaded"
        aggregate_stop_reason = "skipped"
        logger.info(
            "Phase 1: SKIPPED (resumed from checkpoint with %d URLs)", len(discovered_urls),
        )

    # ── Phase 1: Discover URLs ──────────────────────────────────────────────
    if not seed_urls and not discovered_urls:
        if FORM_ACTION and FORM_SELECT_NAME:
            logger.info("Phase 1: form-search iteration (FORM_ACTION=%s, SELECT=%s)", FORM_ACTION, FORM_SELECT_NAME)
            discovered_urls, primary_reason = _discover_urls_via_form_search(MAX_PAGES, limit)
            src_url_base = FORM_ACTION
            aggregate_stop_reason = _merge_stop_reason(aggregate_stop_reason, primary_reason)
            max_pages_hit = max_pages_hit or primary_reason == "max_pages_hit"
        elif args.query:
            logger.info("Phase 1: discovering via search '%s'", args.query[:50])
            discovered_urls, primary_reason = _discover_urls_via_search(args.query, MAX_PAGES, limit)
            src_url_base = SEARCH_URL_PATTERN.replace("{query}", args.query)
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
            # Code Writer adapted: no-args default = FULL Phase 1 discovery on
            # the promoted listing (navigation_analysis). NEVER a fallback to
            # input_urls.json.
            logger.info(
                "Phase 1: no start point given — DEFAULT_LISTING_URL %s", DEFAULT_LISTING_URL,
            )
            args.listing_url = DEFAULT_LISTING_URL
            discovered_urls, primary_reason = _discover_urls_via_category(DEFAULT_LISTING_URL, MAX_PAGES, limit)
            src_url_base = DEFAULT_LISTING_URL
            aggregate_stop_reason = _merge_stop_reason(aggregate_stop_reason, primary_reason)
            max_pages_hit = max_pages_hit or primary_reason == "max_pages_hit"

        # Code Writer adapted: zero-yield self-heal — a listing whose card
        # markup the selectors miss must not end the run at 0 URLs. Retry ONCE
        # with the known-good promoted listing before reporting empty.
        if not discovered_urls and (args.listing_url or "") != DEFAULT_LISTING_URL:
            logger.warning(
                "Phase 1: 0 item URLs discovered — retrying ONCE with "
                "DEFAULT_LISTING_URL %s",
                DEFAULT_LISTING_URL,
            )
            try:
                _fb_urls, _fb_reason = _discover_urls_via_category(DEFAULT_LISTING_URL, MAX_PAGES, limit)
            except Exception as _fb_exc:
                logger.warning("Phase 1: fallback discovery failed: %s", _fb_exc)
                _fb_urls, _fb_reason = [], "navigate_error"
            if _fb_urls:
                discovered_urls = _fb_urls
                src_url_base = DEFAULT_LISTING_URL
            aggregate_stop_reason = _merge_stop_reason(aggregate_stop_reason, _fb_reason)
            max_pages_hit = max_pages_hit or _fb_reason == "max_pages_hit"

        logger.info("Phase 1: discovered %d URLs (pre-category)", len(discovered_urls))
        _write_checkpoint(discovered_urls)
    elif seed_urls:
        src_url_base = None
    else:
        src_url_base = args.query or args.category_url or args.listing_url or "(checkpoint)"

    # ── Phase 1b: Also discover from CATEGORY_URLS (skip on sample / resume) ─
    if not args.sample and CATEGORY_URLS and not checkpoint_urls and not seed_urls:
        existing = set(discovered_urls)
        search_q = (args.query or "").lower()
        cat_idx = 0
        _deadline_start = time.monotonic()
        for cat_url in CATEGORY_URLS:
            if time.monotonic() - _deadline_start > DISCOVERY_DEADLINE_SECONDS:
                logger.warning(
                    "Phase 1b: discovery exceeded %ss deadline — stopping (navigate_error)",
                    DISCOVERY_DEADLINE_SECONDS,
                )
                aggregate_stop_reason = _merge_stop_reason(aggregate_stop_reason, "navigate_error")
                break
            if not isinstance(cat_url, str) or cat_url in existing:
                continue
            if search_q and search_q not in cat_url.lower():
                continue
            cat_idx += 1
            try:
                logger.info("Phase 1b [%d]: visiting category %s", cat_idx, cat_url[:60])
                cat_urls, cat_reason = _discover_urls_via_category(cat_url, MAX_PAGES, limit)
                new = [u for u in cat_urls if u not in existing]
                if new:
                    discovered_urls.extend(new)
                    existing.update(new)
                    logger.info(
                        "Phase 1b [%d]: %s -> %d new URLs (total %d)",
                        cat_idx, cat_url[:40], len(new), len(discovered_urls),
                    )
                aggregate_stop_reason = _merge_stop_reason(aggregate_stop_reason, cat_reason)
                max_pages_hit = max_pages_hit or cat_reason == "max_pages_hit"
                _write_checkpoint(discovered_urls)
            except Exception as cat_exc:
                logger.warning(
                    "Phase 1b [%d]: category %s failed: %s", cat_idx, cat_url[:40], cat_exc,
                )
                aggregate_stop_reason = _merge_stop_reason(aggregate_stop_reason, "navigate_error")

        dimensions_iterated = cat_idx
        discovered_urls = list(dict.fromkeys(discovered_urls))
        if limit:
            discovered_urls = discovered_urls[:limit]
        _write_checkpoint(discovered_urls)
        logger.info("Phase 1 complete: %d total URLs discovered", len(discovered_urls))

    # [job-58 birkenstock] Aggregate-level reclassification.
    if not discovered_urls and aggregate_stop_reason in (
        "short_page", "no_next_link", "no_new_items"
    ):
        aggregate_stop_reason = "empty_first_page"

    if not discovered_urls and not args.discover_only:
        logger.error("DISCOVERY_ZERO: no item URLs discovered under the given listing")
        print(
            "DISCOVERY_ZERO: no item URLs discovered under the given listing",
            file=sys.stderr,
        )
        if aggregate_stop_reason == "navigate_unavailable":
            print(
                "NAVIGATE_UNAVAILABLE: browser-service unavailable during discovery"
                f" (status={_nav_unavailable_status} error_class={_nav_unavailable_class})",
                file=sys.stderr,
            )
        sys.exit(3)

    # ── Phase 2: Extract data concurrently (--discover-only skips it) ────────
    total = len(discovered_urls)
    items: list[dict] = []
    phase2_instant = False
    if args.discover_only:
        logger.info(
            "--discover-only: skipping Phase 2 extraction (%d URLs discovered, "
            "stop_reason=%s)", total, aggregate_stop_reason,
        )
    elif discovered_urls:
        logger.info(
            "Phase 2: Extracting data from %d items (%d workers)",
            total, PHASE2_WORKERS,
        )
        completed = 0
        phase2_start = time.monotonic()
        with ThreadPoolExecutor(max_workers=PHASE2_WORKERS) as pool:
            futures = {
                # Code Writer adapted: seed items carry src_url == item URL;
                # discovery items carry src_url == the listing URL.
                pool.submit(_extract_item_safe, url, src_url_base or url): url
                for url in discovered_urls
            }
            for future in as_completed(futures):
                url = futures[future]
                completed += 1
                try:
                    item = future.result()
                except Exception as exc:
                    item = _error_item(url, src_url_base or url, str(exc))
                items.append(item)
                status = "ok" if item.get("title") else "skip"
                logger.info(
                    "Progress: [%d/%d] (%.1f%%) %s — %s",
                    completed, total, (completed / total) * 100, status, url[:90],
                )
        phase2_instant = phase2_instant_fail(
            time.monotonic() - phase2_start, total, PHASE2_MIN_FETCH_S,
            workers=PHASE2_WORKERS,
        )
        if phase2_instant:
            logger.warning(
                "PHASE2 INSTANT FAIL: %s items in %.2fs (< %.2fs floor at "
                "%s workers) — the item fetches never actually happened",
                total, time.monotonic() - phase2_start,
                total * PHASE2_MIN_FETCH_S * 0.5 / PHASE2_WORKERS,
                PHASE2_WORKERS,
            )

    # ── Output filter ───────────────────────────────────────────────────────
    extra = [f for f in CORE_FILTER_FIELDS if f and f != "title"]
    before = len(items)
    items = [
        it for it in items
        if it.get("title") and (not extra or any(it.get(f) for f in extra))
    ]
    if len(items) != before:
        logger.info(
            "output filter: %d → %d items (dropped %d without core fields)",
            before, len(items), before - len(items),
        )

    # ── discovery_coverage block (contract §1) ──────────────────────────────
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
        "phase2_instant_fail": phase2_instant,
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
            "phase": "discovery" if args.discover_only else "extraction",
            "scraping_duration_seconds": round(time.time() - start_time, 1),
            "discovered_urls": len(discovered_urls),
            "extracted_items": len(items),
            "execution_model": "http_navigate",
            "stealth": "cloak" if str(STEALTH).lower() == "cloak" else "none",
            "discovery_coverage": discovery_coverage,
        },
    }

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S_%f")
    output_filename = os.path.join(
        SCRIPT_DIR, f"output_{timestamp}_{os.getpid()}.json"
    )
    with open(output_filename, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False, default=str)

    logger.info(
        "Done: %d/%d items in %.1fs → %s",
        len([i for i in items if i.get("title")]),
        total,
        time.time() - start_time,
        output_filename,
    )


if __name__ == "__main__":
    main()
