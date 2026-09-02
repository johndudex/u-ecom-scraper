#!/usr/bin/env python3
"""HTTP Navigation Scraper — calls browser_service POST /navigate per page.

Two-phase architecture (mirrors templates/navigation_scraper.py):

  Phase 1: Discover item URLs by submitting a search form or crawling a
           category/listing page, then paginating. Each page fetch is one
           POST /navigate call; link extraction + pagination are computed
           locally on the returned HTML.
  Phase 2: Extract structured data from each discovered item page. Item
           fetches run concurrently in a ThreadPoolExecutor; each item is
           one POST /navigate call. JSON-LD + CSS parsing happen locally.

Runs in the Celery worker container. Pure HTTP — imports no browser engine,
which is what makes the in-process router send it through _run_in_process
instead of the legacy /scrape subprocess path.

Usage:
    python3 scraper.py --query "footwear sneakers"                     # search mode
    python3 scraper.py --category-url "https://site.com/cat/shoes"     # category mode
    python3 scraper.py --listing-url "https://site.com/shop"           # listing mode
    python3 scraper.py --sample                                        # first 5 items only
    python3 scraper.py --limit 50                                      # cap item count
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


def _http_get(url: str) -> tuple[str, int]:
    """HTTP GET through the shared proxy ladder [wave-15 3.4].

    The bare httpx GET this used to be ran unproxied with no escalation —
    the SSR fallback path always egressed from the direct IP, burning it
    against Akamai-class reputation even on runs whose browser phase needed
    a proxy. Returns (html, status_code); ("", 0) when every tier fails —
    the same falsy contract as before. Falls back to the bare GET when the
    image predates the shared module.
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
from src.page_analysis import (  # noqa: E402  (pure-python helper, no browser import)
    extract_jsonld,
    phase2_instant_fail,
)

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION — code_writer substitutes {PLACEHOLDERS} from analysis artifacts.
# The substitution surface intentionally mirrors navigation_scraper.py so the
# existing fill logic carries over unchanged.
# ═══════════════════════════════════════════════════════════════════════════════

SITE_NAME = "{SITE_NAME}"
SITE_URL = "{SITE_URL}"
PLATFORM = "{PLATFORM}"
SITE_SLUG = "{SITE_SLUG}"

# ── Execution model (NEW — not in navigation_scraper.py) ─────────────────────
# browser_service base URL. Already injected into the celery-worker env
# (docker-compose.yml). The scraper never launches a browser itself — it asks
# browser_service to do so per call via POST /navigate.
BROWSER_SERVICE_URL = os.environ.get("BROWSER_SERVICE_URL", "http://browser_service:8001")

# Per-site cloak flag forwarded in every /navigate body. "cloak" engages the
# stealth browser on the server side; "none" uses the plain browser.
# Resolution: code_writer substitutes {STEALTH} below (writes "cloak" for
# anti-bot sites). Env (STEALTH_BROWSER/SCRAPER_STEALTH) overrides it so
# run_execution can force cloak deterministically without relying on the LLM.
# An UNFILLED "{STEALTH}" literal fails safe to "none" — it used to be sent
# verbatim and silently treated as no-cloak by browser_service, which blocked
# anti-bot sites (calvinklein/americaneagle got 0 items).
STEALTH = "{STEALTH}"
_env_stealth = (os.environ.get("STEALTH_BROWSER") or os.environ.get("SCRAPER_STEALTH") or "").strip().lower()
if _env_stealth in ("cloak", "true", "1"):
    STEALTH = "cloak"
elif STEALTH.startswith("{") and STEALTH.endswith("}"):
    STEALTH = "none"

# Per-call render budget (seconds). browser_service caps a single navigate
# (actions + load) at this; we give httpx a 30s cushion on top for transport.
NAVIGATE_TIMEOUT = 120

# Retry policy for transient failures (5xx, 429, timeouts, connect errors).
MAX_RETRIES = 3
BACKOFF_BASE = 2.0  # exponential: BACKOFF_BASE ** attempt, capped at 30s

# Phase 2 concurrency. With the server's NAVIGATE_SEMAPHORE=3, one worker will
# routinely receive HTTP 429 — _navigate absorbs it via retry_after backoff.
# Do NOT raise above 4 without also raising the server semaphore.
PHASE2_WORKERS = 4
# [T3.13c/job-76 myhouse] Lower bound on one real /navigate item fetch (a
# browser navigation never returns faster). Feeds the phase2_instant_fail
# detector — Phase 2 finishing in under half of items*floor/workers means
# the network was never hit.
PHASE2_MIN_FETCH_S = 0.5

# ── Phase 1: Navigation ─────────────────────────────────────────────────────
SEARCH_URL_PATTERN = "{SEARCH_URL_PATTERN}"        # e.g. "https://site.com/search?q={query}"
SEARCH_BOX_SELECTOR = "{SEARCH_BOX_SELECTOR}"      # e.g. "input[type='search']"
SEARCH_SUBMIT_SELECTOR = "{SEARCH_SUBMIT_SELECTOR}"  # e.g. "button[type='submit']"
CATEGORY_URLS = {CATEGORY_URLS}                    # list of category URLs to also crawl

# ── Phase 1: Form-search iteration (for sites that REQUIRE a <select> filter) ──
# When FORM_ACTION is set, Phase 1 iterates through all options in the named
# <select>, submitting the form once per option. This gets the FULL result set
# (e.g. locumtenens has 208 specialties; without iteration, only 1 is scraped).
# Leave FORM_ACTION empty to use URL-based discovery (the default).
FORM_ACTION = ""                 # e.g. "/Resources/JobSearch/QuickSearch"
FORM_METHOD = "POST"            # POST or GET
FORM_SELECT_NAME = ""           # e.g. "Specialties" — the <select> name to iterate
FORM_BASE_URL = ""              # the page containing the form (for CSRF token fetch)

# ── Phase 1: Pagination ─────────────────────────────────────────────────────
PAGINATION_TYPE = "{PAGINATION_TYPE}"              # "page_param" | "next_button" | "infinite_scroll"
NEXT_BUTTON_SELECTOR = "{NEXT_BUTTON_SELECTOR}"    # e.g. "a.next-page"
PAGE_PARAM_NAME = "{PAGE_PARAM_NAME}"              # e.g. "page"
ITEMS_PER_PAGE = {ITEMS_PER_PAGE}                  # page size (for offset pagination); null if unknown
MAX_PAGES = {MAX_PAGES}                            # null for unlimited
TOTAL_COUNT_SELECTOR = "{TOTAL_COUNT_SELECTOR}"    # e.g. ".results-count" (informational)
# Fail-fast wall-clock deadline for Phase 1 discovery. Iterating many categories
# against a blocking/slow site can run long; exceeding this yields
# stop_reason="navigate_error" (gate treats as "gave up" → strategy switch)
# instead of an unbounded hang. Bounds discovery regardless of failure mode.
DISCOVERY_DEADLINE_SECONDS = 300

# Coverage gate (contract §1 expected_total): trusted total item count when
# deterministically known from analysis (e.g. site-reported "3771 jobs across
# 207 specialties"). None → the Tier 3 ratio gate stays a no-op. code_writer
# may populate this per-site; Phase 1 ships None (unknown).
COVERAGE_TARGET_TOTAL: Optional[int] = None

# ── Phase 1: Item link extraction ───────────────────────────────────────────
ITEM_CONTAINER_SELECTOR = "{ITEM_CONTAINER_SELECTOR}"  # e.g. ".product-grid"
ITEM_LINK_SELECTOR = "{ITEM_LINK_SELECTOR}"            # e.g. "a.product-link"
ITEM_URL_PATTERN = "{ITEM_URL_PATTERN}"                # e.g. r"/product/([^/]+)"

# ── Phase 2: Extraction ─────────────────────────────────────────────────────
SCRAPING_METHOD = "{SCRAPING_METHOD}"              # informational (e.g. "http_navigation")
PROXY_TIER = "{PROXY_TIER}"                        # "none" | "datacenter" | "residential"
# [wave-15 3.5] The staged probe tier overrides the writer's guess (an
# UNFILLED placeholder or a wrong guess alike): run_execution stages
# SCRAPER_PROXY_TIER from the probe's working method, so every /navigate
# call egresses the SAME identity the probe proved works.
_env_tier = (os.environ.get("SCRAPER_PROXY_TIER") or "").strip().lower()
if _env_tier in ("none", "datacenter", "residential"):
    PROXY_TIER = _env_tier
DELAY_BETWEEN_REQUESTS = {DELAY_BETWEEN_REQUESTS}

# ── Output ──────────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_KEY = "{OUTPUT_KEY}"                        # "products", "articles", "jobs", etc.
CONTENT_TYPE = "{CONTENT_TYPE}"                    # drives JSON-LD → field mapping
CURRENCY = "{CURRENCY}" or "USD"

SRC_URL = SITE_URL

# Output filter: drop items that failed extraction (no title) or that lack any
# of the content type's core identifying fields. The broad discovery fallback
# can capture a few non-item pages (nav/category roots); this keeps output clean.
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
# Written next to the scraper on the Celery-side filesystem.
# ═══════════════════════════════════════════════════════════════════════════════

_CHECKPOINT_PATH = os.path.join(SCRIPT_DIR, "discovered_urls_checkpoint.json")


def _write_checkpoint(urls: list[str]) -> None:
    """Save discovered URLs so a crash-retry can resume Phase 2 directly."""
    try:
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


# [wave-15 3.2] Geo-pin proxied egress to the site's country: a datacenter/
# residential peer from the WRONG country re-rolls geo-sensitive bot scoring
# and can trip geo-CDN redirects mid-run. Unproxied runs carry no country —
# Bright Data isn't in play and detect_country would only be guessing.
try:
    from src.geo import detect_country as _detect_country
except Exception:  # stripped image without src/ — degrade, never die
    _detect_country = None


def _effective_proxy_tier() -> str:
    """PROXY_TIER with the UNFILLED-placeholder case resolved (never sent raw)."""
    tier = PROXY_TIER if (PROXY_TIER and not PROXY_TIER.startswith("{")) else "none"
    return tier if tier in ("none", "datacenter", "residential") else "none"


def _navigate(url, actions=None, extract=None, retry=0):
    """POST /navigate with exponential backoff. Returns the response dict or None.

    Contract (see docs/browser-service-rework-plan.md Step 1):
      - 200 + success=True  → return data (has url/html/data/...)
      - 200 + blocked=True  → terminal (anti-bot wall); return data so caller
                              can distinguish "blocked" from "navigate failed"
      - 429 / 503 / 502     → retryable; honor Retry-After (header first, then
                              the JSON body's retry_after)
      - 5xx / timeouts /    → retryable; exponential backoff (base 2, cap 30s)
        connect errors
      - 404                 → terminal; return the body so the caller can record
                              an error item without burning the retry budget

    The `retry` arg is a backoff-exponent floor (callers may pre-bias it).
    Returns None only after all MAX_RETRIES attempts are exhausted — EXCEPT:
      - a 429-exhaustion returns a terminal throttled dict
        (``{"success": False, "throttled": True, "status": 429, ...}``), and
      - an infrastructure exhaustion (browser-service 502/503 — the GATEWAY is
        down, not the site) returns a terminal ``navigate_unavailable`` dict
        carrying ``server_error_class`` + the server's error text (B3: prod
        #210 burned 3 writer rounds patching retry code against a dead gateway
        because the 502 body was discarded and logged at DEBUG).
    so discovery can emit stop_reason="navigate_throttled" /
    "navigate_unavailable": both are INCONCLUSIVE coverage (refused, not
    beaten), never a strategy verdict.
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
                    # Anti-bot wall — retrying just burns the budget. Surface it.
                    logger.warning("navigate: BLOCKED on %s", url[:80])
                    return data
                # success=False, not blocked → server-side error; fall through to retry.
            elif r.status_code == 404:
                # Page genuinely gone — terminal, do not retry.
                logger.debug("navigate: 404 on %s (terminal)", url[:80])
                return {"success": False, "url": url, "html": "", "status_code": 404}

            # 429 / 503 / 502 / other 5xx → retryable.
            if r.status_code in (429, 502, 503):
                last_throttled = last_throttled or r.status_code == 429
                # Retry-After: header first (server emits it), then the JSON
                # body's retry_after (older server builds) — neither → 5s.
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
                    # B3: keep the server's own diagnosis — this is the
                    # artifact that one-lines a gateway outage RCA.
                    last_unavailable = {
                        "status": r.status_code,
                        "server_error_class": body.get("error_class") or "",
                        "error": str(body.get("error") or r.text or "")[:300],
                    }
                    global _nav_unavailable_status, _nav_unavailable_class
                    _nav_unavailable_status = r.status_code
                    _nav_unavailable_class = last_unavailable["server_error_class"]
                    # First and last attempts go to WARNING (the old DEBUG-only
                    # log is why job #210's 71×502s were invisible in the draft).
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

        # Generic exponential backoff for non-Retry-After cases.
        time.sleep(min(BACKOFF_BASE ** (attempt + retry), 30))

    logger.warning("navigate: exhausted %d retries on %s", MAX_RETRIES, url[:80])
    if last_unavailable:
        # Gateway down (502/503 persisted across the whole retry ladder):
        # terminal marker dict so discovery records navigate_unavailable —
        # an INFRASTRUCTURE verdict, never a site/strategy failure.
        return {
            "success": False,
            "navigate_unavailable": True,
            "url": url,
            "html": "",
            **last_unavailable,
        }
    if last_throttled:
        # Throttle, not breakage: terminal marker dict so discovery records
        # navigate_throttled (INCONCLUSIVE) instead of navigate_error (FAIL).
        return {"success": False, "throttled": True, "status": 429, "url": url, "html": ""}
    return None


def _nav_fail_reason(resp) -> str:
    """Stop reason for a failed ``_navigate`` call (B3).

    ``navigate_unavailable`` — browser-service itself refused/crashed (502/503
    terminal dict): infrastructure, INCONCLUSIVE. ``navigate_throttled`` — 429
    backpressure. ``navigate_error`` — everything else (block, 5xx from the
    site, transport exhaustion): a real strategy-level FAIL.
    """
    if isinstance(resp, dict):
        if resp.get("navigate_unavailable"):
            return "navigate_unavailable"
        if resp.get("throttled"):
            return "navigate_throttled"
    return "navigate_error"


# B3: last browser-service outage detail seen by ``_navigate`` — read by main()
# when discovery ends with zero URLs so the NAVIGATE_UNAVAILABLE stderr marker
# carries the status/error_class the cascade should park on.
_nav_unavailable_status = 0
_nav_unavailable_class = ""


# ═══════════════════════════════════════════════════════════════════════════════
# URL HELPERS — pure functions, no browser dependency.
# ═══════════════════════════════════════════════════════════════════════════════


def _make_absolute(href: str) -> str:
    """Resolve a possibly-relative href against SITE_URL."""
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
    """Generic item-detail URL detector — no site-specific tokens.

    True for same-domain URLs whose path looks like a detail page (deep/long
    slug, usually carrying a numeric code); False for shallow nav/category roots
    (``/women``, ``/sale``, ``/account``, locale roots like ``/en-us``).
    Structural floor only — code_writer may override with a tighter per-site
    version (regex + slug list from product_analysis) when there's signal.
    """
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
    # Shallow single-segment root with no digit → nav/category (e.g. /women, /sale).
    if len(segs) == 1 and len(last) < 12 and not any(c.isdigit() for c in last):
        return False
    return True


def _set_query_param(url: str, param: str, value) -> str:
    """Return ``url`` with the query ``param`` REPLACED (not appended).

    Using urllib.parse ensures we never produce duplicate params
    (``?pgNum=2&pgNum=3``) which servers resolve inconsistently and which
    caused pagination to re-fetch earlier pages.
    """
    p = urlparse(url)
    qs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k != param]
    qs.append((param, str(value)))
    return urlunparse(p._replace(query=urlencode(qs)))


# Offset-style params (value = (page-1)*items_per_page, not the page number).
_OFFSET_PARAMS = {"offset", "start", "skip", "begin", "from"}


def _extract_next_href(html: str) -> Optional[str]:
    """Find a 'next page' href in listing HTML via semantic selectors."""
    if not html:
        return None
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return None
    # Declared selector first.
    if NEXT_BUTTON_SELECTOR and NEXT_BUTTON_SELECTOR != "{NEXT_BUTTON_SELECTOR}":
        try:
            el = soup.select_one(NEXT_BUTTON_SELECTOR)
            if el and el.get("href"):
                return _make_absolute(el["href"])
        except Exception:
            pass
    # Semantic fallbacks (CSS attribute / class — bs4-compatible).
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
    ``final_url`` — deterministic, DOM-independent, immune to the first-match
    pitfall of clicking numbered pager links. Falls back to a semantic
    next-button href parsed from the page HTML.

    ``final_url`` is the post-action URL returned by /navigate (carries any
    session/query params the form submit added). ``html`` is the same call's
    HTML, used only for next-button href extraction.
    """
    # 1. Construction-first whenever a page param is known.
    if PAGE_PARAM_NAME and PAGE_PARAM_NAME not in ("", "{PAGE_PARAM_NAME}"):
        if PAGINATION_TYPE in ("page_param", "", None) or (
            PAGINATION_TYPE not in ("cursor", "infinite_scroll", "load_more")
        ):
            if PAGE_PARAM_NAME in _OFFSET_PARAMS:
                value = (next_page_num - 1) * (ITEMS_PER_PAGE or 25)
            else:
                value = next_page_num
            return _set_query_param(final_url, PAGE_PARAM_NAME, value)

    # 2. Next-button href parsed from the HTML.
    if html:
        href = _extract_next_href(html)
        if href:
            return href
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 1: URL DISCOVERY
# ═══════════════════════════════════════════════════════════════════════════════

# Discovery stop_reason priority — aggregates the most-concerning termination
# reason across multiple discovery calls (primary search/category + each
# CATEGORY_URL). Higher = more concerning. CRITICAL (H4): a navigate failure
# MUST outrank exhaustion reasons, otherwise the coverage gate false-PASSes a
# blocked scraper. See docs/discovery-coverage-gate-contract.md §2.
_STOP_REASON_PRIORITY = {
    "navigate_unavailable": 6,  # INCONCLUSIVE-INFRA — browser-service itself is
    #                         down (502/503 across the ladder). The GATEWAY
    #                         failed, not the site: must outrank navigate_error
    #                         so the cascade parks instead of "fixing" code.
    "navigate_error": 5,   # FAIL  — gave up due to 502/503/block (NOT exhaustion)
    "empty_first_page": 5,  # FAIL  — [job-58] zero URLs, no hard error = the
    #                         "200-but-blocked" signature (challenge/consent
    #                         wall served with HTTP 200), NOT a genuine end
    "dedup_flat": 4,        # FAIL  — broken dedup / feed injection suspected
    "navigate_throttled": 3,  # INCONCLUSIVE — 429 backpressure; coverage unproven, NOT a site defect
    "max_pages_hit": 3,     # INCONCLUSIVE — hit a cap, did not exhaust
    "no_new_items": 2,      # PASS/INCONCLUSIVE — consecutive pages were all dupes
    "short_page": 1,        # PASS  — genuine end (last page thinned out)
    "no_next_link": 0,      # PASS  — no next-page element/URL found (default)
    "skipped": -1,          # Phase 1 did not run (checkpoint / url_list)
}


def _merge_stop_reason(current: str, new: str) -> str:
    """Return the more-concerning of two stop_reasons (highest priority wins)."""
    if _STOP_REASON_PRIORITY.get(new, 0) > _STOP_REASON_PRIORITY.get(current, 0):
        return new
    return current


def _build_search_actions(query: str) -> list[dict]:
    """Build the /navigate action list for a form-driven search submit.

    Emits: fill the search box → click submit → wait for DOM → sleep 8s.
    The sleep mirrors the HARD RULE from code-writer.md (give SPAs time to
    render results server-side before we parse the HTML). For cascading-select
    sites (parent→child dropdowns), code_writer extends this list with
    {"type":"select","selector":...,"value":...} entries ordered parent-first,
    ahead of the click.
    """
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

    Mirrors navigation_scraper._extract_item_links:
      Tier 1: ITEM_CONTAINER_SELECTOR ▸ ITEM_LINK_SELECTOR (scoped per card)
      Tier 2: bare ITEM_LINK_SELECTOR (page-wide)
      Tier 3: every a[href] filtered by ITEM_URL_PATTERN + _is_product_url

    BeautifulSoup's SoupSieve supports most CSS but not engine-specific
    pseudo-classes; each tier is wrapped so a bad selector falls through to
    the next instead of aborting discovery.
    """
    if not html:
        return []
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as exc:
        logger.warning("Phase 1: HTML parse failed: %s", exc)
        return []

    links: list[str] = []

    # Tier 1: container + link selector.
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

    # Tier 2: bare link selector (page-wide).
    if not links and ITEM_LINK_SELECTOR and ITEM_LINK_SELECTOR != "{ITEM_LINK_SELECTOR}":
        try:
            for a in soup.select(ITEM_LINK_SELECTOR):
                href = a.get("href", "")
                if href:
                    links.append(_make_absolute(href))
        except Exception as exc:
            logger.warning("Phase 1: bare link selector failed: %s", exc)

    # Tier 3: broad fallback — all anchors matching the item URL pattern.
    # Primary selectors above do the real work; this only fires when they miss,
    # and the output filter catches any non-item page that still slips through.
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

    # Dedupe, preserve first-seen order.
    return list(dict.fromkeys(links))


def _discover_urls_via_search(
    query: str,
    max_pages: Optional[int] = None,
    limit: Optional[int] = None,
) -> tuple[list[str], str]:
    """Phase 1a: Discover item URLs by submitting the site's search form.

    Returns ``(urls, stop_reason)`` where ``stop_reason`` follows the
    discovery-coverage-gate enum (contract §2). CRITICAL (H4): when
    ``_navigate`` returns ``None`` / an error status (after MAX_RETRIES on
    429/502/503, or a block), the loop breaks with ``"navigate_error"`` (FAIL)
    — NOT an exhaustion reason. A boolean ``exhausted`` is forbidden.
    """
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

    # Default per contract §2 — loop completed without an explicit break.
    stop_reason = "no_next_link"
    current_page = 1
    while True:
        if max_pages and current_page >= max_pages:
            logger.info("Phase 1: Reached max_pages=%d", max_pages)
            stop_reason = "max_pages_hit"
            break
        if limit and len(all_urls) >= limit:
            logger.info("Phase 1: Reached limit=%d", limit)
            # No dedicated "limit_hit" enum value (contract §2). A user/config
            # cap is the closest match to MAX_PAGES — both are INCONCLUSIVE
            # caps, not exhaustion. See deviation note in return message.
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
            # H4 false-pass guard: _navigate exhausted retries on 429/502/503
            # (or hit a block). This is NOT exhaustion — surface it so the
            # coverage gate can FAIL the run instead of declaring success.
            # A 429-exhaustion is navigate_throttled and a gateway 502/503 is
            # navigate_unavailable (both INCONCLUSIVE): the run was refused by
            # browser-service, not beaten by the site.
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
            # Distinguish genuine end (page thin/empty) from dedup saturation
            # (page full but all duplicates). Contract §2 separates short_page
            # (PASS) from no_new_items (INCONCLUSIVE).
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
    # NOTE: no zero-URL reclassification here. It lives ONCE in main() on the
    # AGGREGATE url list — per-path reclass + _merge_stop_reason would let one
    # blocked secondary category (200-but-empty) fail a run whose primary
    # path found items (T2.1: never fail the run that found items).
    logger.info("Phase 1: Discovered %d total item URLs via search (%s)", len(unique_urls), stop_reason)
    return unique_urls, stop_reason


def _discover_urls_via_form_search(
    max_pages: Optional[int] = None,
    limit: Optional[int] = None,
) -> tuple[list[str], str]:
    """Phase 1c: Discover item URLs by iterating through ALL options of a <select>.

    For sites where the search form REQUIRES a filter (e.g. locumtenens requires
    a Specialty selection — 500 without one). This function:
      1. Fetches the form page (to get CSRF tokens + parse the <select> options)
      2. For each option: submits the form, extracts item URLs, paginates
      3. Deduplicates all URLs across options

    Returns ``(urls, stop_reason)``.
    """
    from urllib.parse import urljoin
    from bs4 import BeautifulSoup

    all_urls: list[str] = []
    seen_ids: set[str] = set()

    # Resolve form URLs
    form_page_url = FORM_BASE_URL or SEARCH_URL_PATTERN.split("?")[0]
    form_action_url = urljoin(form_page_url, FORM_ACTION) if FORM_ACTION else ""
    if not form_action_url or not FORM_SELECT_NAME:
        logger.error("Phase 1 (form-search): FORM_ACTION or FORM_SELECT_NAME not set")
        return [], "navigate_error"

    # Fetch the form page to extract hidden fields (CSRF) + select options
    form_html = _http_get(form_page_url)
    if not form_html:
        logger.error("Phase 1 (form-search): could not fetch form page %s", form_page_url)
        return [], "navigate_error"

    form_soup = BeautifulSoup(form_html, "html.parser")

    # Extract hidden fields (CSRF tokens, viewstate, etc.)
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
        # Skip blank/prompt/Any options — they return 500 on locumtenens
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

        # Submit the form with this option
        form_data = {**hidden_fields, FORM_SELECT_NAME: opt_val}
        page_num = 1
        while True:
            if max_pages and page_num > max_pages:
                break
            if time.time() > deadline:
                break

            # POST/GET to the form action
            if FORM_METHOD.upper() == "POST":
                resp_html, status = _http_post(form_action_url, form_data)
            else:
                resp_html, status = _http_get(form_action_url + "?" + "&".join(f"{k}={v}" for k, v in form_data.items()))

            if not resp_html or status >= 400:
                logger.warning("Phase 1 (form-search): option '%s' page %d → status %d", opt_text[:30], page_num, status)
                break

            page_urls = _extract_item_links(resp_html)
            new_count = 0
            for url in page_urls:
                # Deduplicate by job/item ID in the URL
                item_id = re.search(r'/job-(\d+)', url) or re.search(r'/(\d{4,})/?$', url)
                dedup_key = item_id.group(1) if item_id else url
                if dedup_key not in seen_ids:
                    seen_ids.add(dedup_key)
                    all_urls.append(url)
                    new_count += 1

            if new_count == 0:
                break  # no new items on this page → done with this option

            # Check for next page
            soup = BeautifulSoup(resp_html, "html.parser")
            next_link = None
            for sel in ['a[rel="next"]', 'a.next', 'li.next a', 'a[aria-label="Next"]']:
                el = soup.select_one(sel)
                if el and el.get("href"):
                    next_link = urljoin(form_action_url, el["href"])
                    break
            if not next_link:
                # Try page-param pagination
                next_link_test = f"{form_action_url}?page={page_num + 1}"
                if f"page={page_num + 1}" not in resp_html:
                    break  # no pagination indicator
                # Navigate to next page via GET on the search results URL
                # (many form-search sites use session URLs with ?page=N)
                form_data = {**hidden_fields, FORM_SELECT_NAME: opt_val}
                # The results URL was set by the POST; we'd need to track it.
                # For simplicity, try the page-param URL:
                break  # most form-search results don't support simple page-param
            else:
                # Follow the next link
                next_html, next_status = _http_get(next_link)
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
    # NOTE: see the search-path note — zero-URL reclassification lives in main().
    logger.info("Phase 1 (form-search): Discovered %d total item URLs across %d options (%s)",
                len(unique_urls), len(options), stop_reason)
    return unique_urls, stop_reason


def _discover_urls_via_category(
    category_url: str,
    max_pages: Optional[int] = None,
    limit: Optional[int] = None,
) -> tuple[list[str], str]:
    """Phase 1b: Discover item URLs from a category/listing page.

    Returns ``(urls, stop_reason)``; see ``_discover_urls_via_search`` for the
    stop_reason contract. A navigate failure is ``"navigate_error"`` (FAIL),
    kept strictly distinct from exhaustion reasons.
    """
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

    # Default per contract §2 — loop completed without an explicit break.
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
    # NOTE: see the search-path note — zero-URL reclassification lives in main().
    logger.info("Phase 1: Discovered %d total item URLs from category (%s)", len(unique_urls), stop_reason)
    return unique_urls, stop_reason


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 2: ITEM EXTRACTION (concurrent)
# ═══════════════════════════════════════════════════════════════════════════════


# ── FIELD NORMALIZERS — inline on purpose, NOT a src import ──────────────────
# Drafts execute in the browser-service image: a NEW src import would ImportError
# there until that image is rebuilt, so the ~25 lines live in each python-side
# template verbatim (playwright/UC normalize in-page via JS and don't need them).

def _norm_price(value) -> Optional[str]:
    """Strip currency symbols/whitespace from a price.

    "£1,234.56" → "1234.56", "1.234,56 €" → "1234.56", 24.99 (a JSON-LD
    number) → "24.99". Returns None when no digits are present — an
    unparseable price is EMPTY, never zero (0 would read as a real product
    priced at nothing). The currency stays in its own ``currency`` field.
    """
    if value is None:
        return None
    cleaned = re.sub(r"[^\d.,-]", "", str(value).strip())
    if not re.search(r"\d", cleaned):
        return None
    if "," in cleaned and "." in cleaned:
        # Both separators present: whichever comes LAST is the decimal one.
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        # "1,234" (grouping) vs "1,5" (decimal comma): a comma followed by
        # exactly three digits is grouping, anything else is a decimal comma.
        cleaned = re.sub(r",(?=\d{3}(?:\D|$))", "", cleaned).replace(",", ".")
    return cleaned


def _norm_availability(value) -> Optional[str]:
    """Normalize availability to ``in_stock`` / ``out_of_stock``.

    Accepts the schema.org URI form, InStock / In Stock / in_stock / Available
    and their negatives. Anything unrecognised passes through lowercased —
    availability is never invented (an unknown state is data, not an error).
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "in_stock" if value else "out_of_stock"
    text = str(value).strip().lower()
    if not text:
        return None
    if "://" in text:  # e.g. http://schema.org/InStock
        text = text.rsplit("/", 1)[-1]
    compact = text.replace("-", "_").replace(" ", "")
    if compact in ("in_stock", "instock", "available"):
        return "in_stock"
    if compact in ("out_of_stock", "outofstock", "unavailable", "sold_out", "soldout"):
        return "out_of_stock"
    return text


def _populate_from_jsonld(item: dict, jsonld_blocks: list[dict]) -> None:
    """Fill ``item`` from JSON-LD blocks, CONTENT_TYPE-aware.

    Mirrors the block-type dispatch in navigation_scraper._extract_item_data.
    Field extraction happens entirely on the HTML returned by /navigate —
    no in-page evaluate round-trip.
    """
    for block in jsonld_blocks:
        block_type = block.get("@type", "")
        if isinstance(block_type, list):
            block_type = block_type[0] if block_type else ""

        if CONTENT_TYPE == "product" and block_type in ("Product",):
            item["title"] = block.get("name", "") or item.get("title", "")
            offers = block.get("offers", {})
            if isinstance(offers, dict):
                offers_list = [offers]
            elif isinstance(offers, list):
                offers_list = offers
            else:
                offers_list = []
            for offer in offers_list:
                if not isinstance(offer, dict):
                    continue
                price = offer.get("price", "")
                currency = offer.get("priceCurrency", CURRENCY)
                if price:
                    item["price"] = _norm_price(price)
                    item["currency"] = currency
                    break
            avail = ""
            for offer in offers_list:
                if isinstance(offer, dict) and offer.get("availability"):
                    avail = offer["availability"]
                    break
            if avail:
                item["availability"] = _norm_availability(avail)
            if "currency" not in item:
                item["currency"] = CURRENCY
            item["description"] = block.get("description", "")
            images = block.get("image", [])
            if isinstance(images, str):
                images = [images]
            if images:
                item["images"] = images
            break

        elif CONTENT_TYPE == "article" and block_type in ("Article", "NewsArticle", "BlogPosting"):
            item["title"] = block.get("headline", block.get("name", "")) or item.get("title", "")
            author = block.get("author", {})
            if isinstance(author, dict):
                item["author"] = author.get("name", "")
            elif isinstance(author, list) and author:
                item["author"] = author[0].get("name", "") if isinstance(author[0], dict) else ""
            item["publish_date"] = block.get("datePublished", "")
            item["content"] = block.get("articleBody", "")
            images = block.get("image", [])
            if isinstance(images, str):
                images = [images]
            if images:
                item["images"] = images
            break

        elif CONTENT_TYPE == "job_posting" and block_type == "JobPosting":
            item["title"] = block.get("title", "") or item.get("title", "")
            org = block.get("hiringOrganization", {})
            item["company"] = org.get("name", "") if isinstance(org, dict) else ""
            loc = block.get("jobLocation", {})
            if isinstance(loc, dict):
                addr = loc.get("address", {})
                if isinstance(addr, dict):
                    item["location"] = addr.get("addressLocality", "")
            item["description"] = block.get("description", "")
            break

        elif CONTENT_TYPE == "forum_thread" and block_type in ("DiscussionForumPosting", "Question"):
            item["title"] = block.get("headline", block.get("name", "")) or item.get("title", "")
            author = block.get("author", {})
            if isinstance(author, dict):
                item["author"] = author.get("name", "")
            elif isinstance(author, list) and author:
                item["author"] = author[0].get("name", "") if isinstance(author[0], dict) else ""
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
    """Phase 2: Extract structured data from a single item page.

    One POST /navigate call fetches the page (after any redirects). JSON-LD +
    CSS parsing happen locally on the returned HTML. Failures become error
    dicts so the job continues instead of aborting on one bad page.
    """
    resp = _navigate(item_url)
    if not resp:
        return _error_item(item_url, src_url, "navigate failed after retries")
    if resp.get("navigate_unavailable"):
        # B3: the gateway, not the site — carry the server's diagnosis into
        # the error item so a gateway-outage run is identifiable in output.
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
    item: dict = {
        "url": item_url,
        "src_url": src_url,
        "status_code": 200,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": "",
    }

    # JSON-LD extraction (local helper — pure regex + json.loads).
    try:
        jsonld_blocks = extract_jsonld(html)
        if jsonld_blocks:
            _populate_from_jsonld(item, jsonld_blocks)
    except Exception as exc:
        logger.warning("Phase 2: JSON-LD extraction failed for %s: %s", item_url[:60], exc)

    # CSS fallback for title (h1) when JSON-LD didn't yield one.
    if not item.get("title"):
        try:
            soup = BeautifulSoup(html, "html.parser")
            h1 = soup.select_one("h1")
            if h1:
                item["title"] = h1.get_text(strip=True)
        except Exception:
            pass

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
# env read in main(). ADD flags if you need them; NEVER remove or rename:
#   --fresh-discovery  always (execution)     --listing-url  navigation/list_page
#   --query            search_term            --input/--sample/--limit  testing
#   --discover-only    Phase-1 probe          (+ SCRAPER_LISTING_URL env read)
# Source of truth: webapp/agents/constants.py
# ──────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description=f"{SITE_NAME} HTTP Navigation Scraper")
    parser.add_argument("--query", type=str, help="Search query for navigation mode")
    parser.add_argument("--category-url", type=str, help="Category URL to crawl")
    parser.add_argument("--listing-url", type=str, help="Listing page URL to paginate")
    parser.add_argument("--sample", action="store_true", help="Scrape first 5 items only")
    parser.add_argument("--limit", type=int, default=None, help="Max items to scrape")
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
             "extraction. Lets code_tester probe real discovery yield without "
             "extracting thousands of items.",
    )
    parser.add_argument(
        "--fresh-discovery", action="store_true",
        help="Ignore any discovered_urls_checkpoint.json and run Phase 1 from "
             "scratch (still writes a checkpoint as normal). Fixes checkpoint "
             "cross-contamination between test and execution phases.",
    )
    args = parser.parse_args()

    # --no-proxy overrides the configured PROXY_TIER for this run.
    global PROXY_TIER
    if args.no_proxy:
        PROXY_TIER = "none"

    # F6 DETERMINISTIC DISCOVERY GATE (env-var): feeds the existing CLI
    # contract rather than adding a parallel branch — run_execution injects
    # SCRAPER_LISTING_URL because the LLM-adapted argparse may drop the flags.
    # Deliberately does NOT override FORM_ACTION (form-search sites like
    # locumtenens iterate a <select>'s options across the whole taxonomy;
    # a listing URL would collapse that into one page — a 320-class regression).
    _env_listing = os.environ.get("SCRAPER_LISTING_URL", "").strip()
    if _env_listing:
        args.listing_url = _env_listing
        args.fresh_discovery = True  # the checkpoint gate below honors this
        logger.info("Env gate: SCRAPER_LISTING_URL → --listing-url %s (fresh)",
                    _env_listing[:80])

    limit = 5 if args.sample else args.limit
    start_time = time.time()
    discovered_urls: list[str] = []

    # ── Coverage-gate state (contract §1) ───────────────────────────────────
    # ran_phase1=False emits stop_reason="skipped" + skipped_reason.
    ran_phase1 = True
    skipped_reason: Optional[str] = None
    aggregate_stop_reason = "no_next_link"  # default per contract §2
    max_pages_hit = False
    dimensions_iterated = 0
    dimensions_total = len(CATEGORY_URLS) if isinstance(CATEGORY_URLS, list) else 0

    # ── Resume from checkpoint if present (unless --fresh-discovery) ─────────
    # --fresh-discovery (contract §3/H3) ignores a stale checkpoint so the
    # execution phase does not silently reuse the test phase's discoveries.
    checkpoint_urls = [] if args.fresh_discovery else _load_checkpoint()
    if checkpoint_urls:
        discovered_urls = checkpoint_urls
        ran_phase1 = False
        skipped_reason = "checkpoint_loaded"
        aggregate_stop_reason = "skipped"
        logger.info(
            "Phase 1: SKIPPED (resumed from checkpoint with %d URLs)", len(discovered_urls),
        )

    # ── Phase 1: Discover URLs ──────────────────────────────────────────────
    if not discovered_urls:
        if FORM_ACTION and FORM_SELECT_NAME:
            # Form-search iteration mode: iterate through ALL <select> options
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
            logger.error("No --query, --category-url, or --listing-url provided")
            sys.exit(1)
        logger.info("Phase 1: discovered %d URLs (pre-category)", len(discovered_urls))
        _write_checkpoint(discovered_urls)
    else:
        src_url_base = args.query or args.category_url or args.listing_url or "(checkpoint)"

    # ── Phase 1b: Also discover from CATEGORY_URLS (skip on sample / resume) ─
    if not args.sample and CATEGORY_URLS and not checkpoint_urls:
        existing = set(discovered_urls)
        search_q = (args.query or "").lower()
        cat_idx = 0
        _deadline_start = time.monotonic()
        for cat_url in CATEGORY_URLS:
            # Fail-fast: bound discovery wall-clock so a blocking/slow site can't
            # hang across many categories. navigate_error lets the gate switch.
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
                # A category crashing is a fetch failure for that dimension —
                # surface it as navigate_error (FAIL), not exhaustion.
                aggregate_stop_reason = _merge_stop_reason(aggregate_stop_reason, "navigate_error")

        dimensions_iterated = cat_idx
        discovered_urls = list(dict.fromkeys(discovered_urls))
        if limit:
            discovered_urls = discovered_urls[:limit]
        _write_checkpoint(discovered_urls)
        logger.info("Phase 1 complete: %d total URLs discovered", len(discovered_urls))

    # [job-58 birkenstock] Aggregate-level reclassification: ending Phase 1
    # with ZERO discovered URLs and only PASS-flavored stop reasons is the
    # "200-but-blocked" signature (an anti-bot challenge/consent wall served
    # with HTTP 200 renders no item links), NOT a genuine end-of-catalog — a
    # real catalog-end requires having seen items. Aggregate-only, never
    # per-path: a blocked secondary category must not fail a run whose
    # primary path found items (T2.1).
    if not discovered_urls and aggregate_stop_reason in (
        "short_page", "no_next_link", "no_new_items"
    ):
        aggregate_stop_reason = "empty_first_page"

    if not discovered_urls and not args.discover_only:
        # [job-88 selfridges] A clean exit-0 with no output file makes the run
        # indistinguishable from "wrote nothing" — the executor's only signal
        # is an ABSENCE its rescue gates cannot read. Exit non-zero with a
        # marker so the run lands in the diagnosed-failure branch (full stderr
        # tail) and the strategy ladder instead of the honesty floor.
        logger.error("DISCOVERY_ZERO: no item URLs discovered under the given listing")
        print(
            "DISCOVERY_ZERO: no item URLs discovered under the given listing",
            file=sys.stderr,
        )
        if aggregate_stop_reason == "navigate_unavailable":
            # B3: the infra verdict must survive this exit — run_execution's
            # diagnosed-failure branch reads stderr, and NAVIGATE_UNAVAILABLE
            # (like DISCOVERY_ZERO) is a parseable marker, not prose. Without it
            # a browser-service outage is indistinguishable from a site-shape
            # failure and the cascade burns the whole strategy ladder on it.
            print(
                "NAVIGATE_UNAVAILABLE: browser-service unavailable during discovery"
                f" (status={_nav_unavailable_status} error_class={_nav_unavailable_class})",
                file=sys.stderr,
            )
        sys.exit(3)

    # ── Phase 2: Extract data concurrently (--discover-only skips it) ────────
    # --discover-only (contract §3): run Phase 1 to exhaustion, emit the output
    # JSON with discovery_coverage populated, then SKIP extraction. The output
    # list stays empty; found=0 is expected (the consumer reads discovered_urls
    # / stop_reason / dimensions_* instead).
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
                pool.submit(_extract_item_safe, url, src_url_base): url
                for url in discovered_urls
            }
            for future in as_completed(futures):
                url = futures[future]
                completed += 1
                try:
                    item = future.result()
                except Exception as exc:
                    # _extract_item_safe already guards this, but be defensive.
                    item = _error_item(url, src_url_base, str(exc))
                items.append(item)
                status = "ok" if item.get("title") else "skip"
                logger.info(
                    "Progress: [%d/%d] (%.1f%%) %s — %s",
                    completed, total, (completed / total) * 100, status, url[:90],
                )
        # [T3.13c/job-76 myhouse] Mechanical "fetch actually happened"
        # detector — surfaced in discovery_coverage below.
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
    # Drop extraction failures + items lacking any core field for this type.
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
    # found = POST-filter extracted count (real items), NOT raw discovered_urls.
    # When --discover-only skipped Phase 2, found is 0 by design.
    discovery_coverage = {
        "stop_reason": aggregate_stop_reason,
        "found": len(items),                      # post-filter real items
        "discovered_urls": len(discovered_urls),  # raw pre-filter count (diagnostic)
        "expected_total": COVERAGE_TARGET_TOTAL,  # None when unknown → Tier 3 no-op
        "dimensions_iterated": dimensions_iterated,
        "dimensions_total": dimensions_total,
        "max_pages_hit": max_pages_hit,
        "ran_phase1": ran_phase1,
        "skipped_reason": skipped_reason,
        # [T3.13c/job-76 myhouse] True when Phase 2 finished faster than its
        # per-fetch floor allows — the item fetches never actually happened.
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
            # job-76 myhouse: lets every consumer tell a discovery artifact
            # (URL stubs, no fields) from an extraction result.
            "phase": "discovery" if args.discover_only else "extraction",
            "scraping_duration_seconds": round(time.time() - start_time, 1),
            "discovered_urls": len(discovered_urls),
            "extracted_items": len(items),
            "execution_model": "http_navigate",
            "stealth": "cloak" if str(STEALTH).lower() == "cloak" else "none",
            "discovery_coverage": discovery_coverage,
        },
    }

    # Unique per process: a second-resolution timestamp collides when two
    # runs of this draft start in the same second (job-71 popsockets — the
    # discovery run clobbered the extraction result).
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
