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
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse, urljoin

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

    The bare httpx GET this used to be ran unproxied with no escalation —
    the SSR fallback path always egressed from the direct IP, burning it
    against Cloudflare reputation even on runs whose browser phase needed
    a proxy.

    Returns (html, status_code); ("", 0) when every tier fails — the same
    falsy contract as before — OR the shared ladder's SoftBlock signal when
    the site answered a tier with a challenge-served-as-200 [T1.3]. A
    SoftBlock is falsy, so legacy ``if not html`` callers keep working, and
    block-aware callers can tell a soft wall from a transport failure and
    report the honest stop reason. Falls back to the bare GET when the
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


# Code Writer adapted [REMEDIATION — job cycle-1 FAIL]: per-thread shared-ladder
# fetch. The previous cycle fetched every PDP through browser /navigate
# (cloak+none) and got Cloudflare-challenged / JS-shell pages — zero core
# fields, 10/10 dropped. The measured PDP transport (probe evidence:
# pdp_method=direct_http_datacenter, 200 + 1.51 MB server-rendered HTML with a
# full Product JSON-LD block) is DATACENTER-PROXY HTTP with no JS at all.
# Phase 2 now fetches PDPs through `create_fetch_text` — the shared proxy
# ladder (session cookie continuity, tier escalation, curl_cffi fingerprint
# rung) — never a hand-rolled session.get(). The Session is NOT thread-safe
# and Phase 2 runs in a ThreadPoolExecutor, so each worker thread gets its
# own ladder closure (thread-local) instead of sharing one.
_FETCH_TEXT_TLS = threading.local()


def _get_thread_fetch_text():
    """Per-thread `fetch_text` built from the shared ladder factory.

    Returns (fetch_text, SoftBlock_cls_or_None). Falls back to a bare httpx
    GET when the image predates src.http_fetch (same degradation as
    _http_get above).
    """
    cached = getattr(_FETCH_TEXT_TLS, "fetch_text", None)
    if cached is not None:
        return cached
    try:
        from src.http_fetch import create_fetch_text

        fetch_text = create_fetch_text(
            delay_s=1.0,
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
    except ImportError:
        _FETCH_TEXT_TLS.fetch_text = None
        return None
    _FETCH_TEXT_TLS.fetch_text = fetch_text
    return fetch_text


def _get_soft_block_cls():
    try:
        from src.http_fetch import SoftBlock

        return SoftBlock
    except ImportError:
        return None


def _is_challenge_body(text: str) -> bool:
    """Strong challenge-shape check for an HTTP 200 body (local twin of
    detect_soft_block's marker list — used to classify 403/200 walls)."""
    if not text:
        return True
    lowered = text.lower()
    return any(
        m in lowered
        for m in (
            "attention required",
            "access denied",
            "verify you are human",
            "checking your browser",
            "just a moment",
            "cf-chl",
            "cf_chl",
        )
    )


def _page_title(html: str) -> str:
    """First <title> text (≤300 chars) — cheap redirect/block classifier."""
    m = re.search(r"<title[^>]*>(.*?)</title>", html[:200_000], re.I | re.S)
    return m.group(1).strip()[:300] if m else ""
from bs4 import BeautifulSoup

# Make src.* importable (scraper runs from scrapers/{slug}/).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from src.page_analysis import (  # noqa: E402  (pure-python helper, no browser import)
    extract_jsonld,
    phase2_instant_fail,
)
from src.discovery import discover_item_urls, config_for_load_more  # _DISCOVERY_IMPORT_APPLIED (enforced — do not remove)

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION — code_writer substitutes {PLACEHOLDERS} from analysis artifacts.
# ═══════════════════════════════════════════════════════════════════════════════

SITE_NAME = "Brooks Brothers India"
SITE_URL = "https://brooksbrothers.in"
PLATFORM = "custom_headless_storefront"
SITE_SLUG = "brooksbrothers-in"

# ── Execution model ──────────────────────────────────────────────────────────
BROWSER_SERVICE_URL = os.environ.get("BROWSER_SERVICE_URL", "http://browser_service:8001")

# Per-site cloak flag forwarded in every /navigate body. MEASURED ACCESS RECIPE
# (scraper_analysis): stealth=cloak, PDP tier=none, LISTING tier=datacenter.
# Resolution: constant below; env (STEALTH_BROWSER/SCRAPER_STEALTH) overrides
# so run_execution can force cloak deterministically without relying on the LLM.
STEALTH = "cloak"
_env_stealth = (os.environ.get("STEALTH_BROWSER") or os.environ.get("SCRAPER_STEALTH") or "").strip().lower()
if _env_stealth in ("cloak", "true", "1"):
    STEALTH = "cloak"
elif _env_stealth in ("none", "false", "0"):
    STEALTH = "none"

# Per-call render budget (seconds). browser_service caps a single navigate
# (actions + load) at this; we give httpx a 30s cushion on top for transport.
NAVIGATE_TIMEOUT = 120

# Per-call settle floor (ms) forwarded to /navigate.
NAVIGATE_SETTLE_MS = 4000
_env_settle = os.environ.get("NAVIGATE_SETTLE_MS", "").strip()
try:
    NAVIGATE_SETTLE_MS = int(_env_settle or NAVIGATE_SETTLE_MS)
except (TypeError, ValueError):
    NAVIGATE_SETTLE_MS = 4000
NAVIGATE_SETTLE_MS = max(0, min(NAVIGATE_SETTLE_MS, 45000))

# Retry policy for transient failures (5xx, 429, timeouts, connect errors).
MAX_RETRIES = 3
BACKOFF_BASE = 2.0  # exponential: BACKOFF_BASE ** attempt, capped at 30s

# Phase 2 concurrency. Server NAVIGATE_SEMAPHORE=3 → keep at/below 3; 429 is
# absorbed by _navigate's retry_after backoff.
# Phase 2 concurrency — REMEDIATION: item fetches are now HTTP-based
# (datacenter-proxy ladder), so per the Full Extraction rules we raise
# workers (thread-local sessions inside src.http_fetch make this safe).
PHASE2_WORKERS = 8
# [T3.13c/job-76] Lower bound on one real /navigate item fetch (a browser
# navigation never returns faster). Feeds the phase2_instant_fail detector.
PHASE2_MIN_FETCH_S = 0.5

# ── Phase 1: Navigation ─────────────────────────────────────────────────────
SEARCH_URL_PATTERN = ""
SEARCH_BOX_SELECTOR = ""
SEARCH_SUBMIT_SELECTOR = ""
# Promoted listing from navigation_analysis — Phase 1 crawls THIS (and each
# category in CATEGORY_URLS) to exhaustion via the template's pagination.
CATEGORY_URLS = [
    "https://brooksbrothers.in/collection/collection-men",
]

# ── Phase 1: Form-search iteration (unused for this site) ────────────────────
FORM_ACTION = ""
FORM_METHOD = "POST"
FORM_SELECT_NAME = ""
FORM_BASE_URL = ""

# ── Phase 1: Pagination ─────────────────────────────────────────────────────
# discovery_config.json: type=load_more, page_param_name=null, next_button_selector=null.
# With no page param the template's _get_next_page_url falls back to the
# next-button href path (semantic rel=next / .next selectors).
PAGINATION_TYPE = "load_more"
NEXT_BUTTON_SELECTOR = ""
PAGE_PARAM_NAME = ""
ITEMS_PER_PAGE = None
MAX_PAGES = None  # unlimited — full extraction, no arbitrary cap
TOTAL_COUNT_SELECTOR = ""
# Fail-fast wall-clock deadline for Phase 1 discovery.
DISCOVERY_DEADLINE_SECONDS = 300

# Coverage gate (contract §1 expected_total): unknown for this site.
COVERAGE_TARGET_TOTAL: Optional[int] = None

# ── Phase 1: Item link extraction ───────────────────────────────────────────
# STRICT product-card selectors only (hard rule: no OR'd permissive catch-all —
# Phase 2 slices the HEAD of the list, so a nav anchor at the head poisons the
# run). This headless storefront's cards link /product/{slug}-{id}.
ITEM_CONTAINER_SELECTOR = ""
ITEM_LINK_SELECTOR = 'a[href*="/product/"]'
ITEM_URL_PATTERN = r"/product/[^/?#]+-1737\d+$"

# ── Phase 2: Extraction ─────────────────────────────────────────────────────
SCRAPING_METHOD = "http_navigation"
PROXY_TIER = "none"  # PDP tier (measured: cloak_none). Discovery overrides to datacenter.
_env_tier = (os.environ.get("SCRAPER_PROXY_TIER") or "").strip().lower()
if _env_tier in ("none", "datacenter", "residential"):
    PROXY_TIER = _env_tier
DELAY_BETWEEN_REQUESTS = 1.0

# ── Output ──────────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_KEY = "products"
CONTENT_TYPE = "product"
CURRENCY = "INR"

SRC_URL = SITE_URL

# ── USER SCHEMA — product name, price, currency, description, size ──────────
# The standard field table does NOT apply to this job; extract ONLY these.
USER_FIELDS = ["title", "price", "currency", "description", "size"]

# Output filter core fields (user schema: price is the core identifying field
# beyond title). Kept in the template's mechanism shape.
_CONTENT_FILTER_FIELDS = {
    "product": ["price"],
}
CORE_FILTER_FIELDS = _CONTENT_FILTER_FIELDS.get(CONTENT_TYPE, [])

# Code Writer adapted: MEASURED ACCESS RECIPE — the probe proved a TIERED
# recipe (listing pages → cloak+datacenter; item/PDP pages → cloak+none).
# The template carries a single PROXY_TIER for every call; this site needs
# both rungs, so Phase 1 sets the tier per call and Phase 2 uses the PDP tier.
DISCOVERY_PROXY_TIER = "datacenter"
_env_disc_tier = (os.environ.get("SCRAPER_DISCOVERY_PROXY_TIER") or "").strip().lower()
if _env_disc_tier in ("none", "datacenter", "residential"):
    DISCOVERY_PROXY_TIER = _env_disc_tier


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
    """Save discovered URLs so a crash-retry can resume Phase 2 directly.

    [wave-24 W24-2] Never overwrite a banked checkpoint with 0 URLs.
    """
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


def _navigate(url, actions=None, extract=None, retry=0, settle_ms=None, proxy_tier=None):
    """POST /navigate with exponential backoff. Returns the response dict or None.

    `proxy_tier` (Code Writer adapted): per-call tier override — Phase 1
    listing fetches ride the MEASURED datacenter rung, Phase 2 PDP fetches
    ride the measured none tier. Falls back to _effective_proxy_tier().
    """
    payload = {
        "url": url,
        "actions": actions or [],
        "extract": extract or {},
        "stealth": "cloak" if str(STEALTH).lower() == "cloak" else "none",
        "proxy_tier": proxy_tier if proxy_tier in ("none", "datacenter", "residential") else _effective_proxy_tier(),
        "country": (
            _detect_country(SITE_URL)
            if (_detect_country and (proxy_tier or _effective_proxy_tier()) != "none")
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
                # success=False, not blocked → server-side error; fall through to retry.
            elif r.status_code == 404:
                logger.debug("navigate: 404 on %s (terminal)", url[:80])
                return {"success": False, "url": url, "html": "", "status_code": 404}

            # 429 / 503 / 502 / other 5xx → retryable.
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

        # Generic exponential backoff for non-Retry-After cases.
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


# B3: last browser-service outage detail seen by ``_navigate`` — read by main().
_nav_unavailable_status = 0
_nav_unavailable_class = ""


# ═══════════════════════════════════════════════════════════════════════════════
# URL HELPERS — pure functions, no browser dependency.
# ═══════════════════════════════════════════════════════════════════════════════


def _make_absolute(href: str) -> str:
    """Resolve a possibly-relative href against SITE_URL.

    A leading '//' is protocol-relative ONLY when the next segment is a
    plausible netloc (contains a dot). Otherwise it is a MALFORMED site
    path and is joined against the site root.
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
    """Item-detail URL detector for brooksbrothers.in.

    Code Writer adapted: strict — the path must contain '/product/' AND end in
    a numeric id ({slug}-{digits}). Nav/category roots never match. Also used
    as the hard filter on every discovered link before it enters Phase 2.
    """
    if not href:
        return False
    site_host = (urlparse(SITE_URL).hostname or "").lower()
    if site_host and site_host not in href.lower():
        return False
    path = urlparse(href).path
    if "/product/" not in path:
        return False
    last = path.rstrip("/").rsplit("/", 1)[-1]
    # /product/{slug}-{numeric-id} — require trailing digits (id ≥ 4 digits)
    return bool(re.match(r".+-\d{4,}$", last))


def _set_query_param(url: str, param: str, value) -> str:
    """Return ``url`` with the query ``param`` REPLACED (not appended)."""
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
    if NEXT_BUTTON_SELECTOR:
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
    ``final_url``. Falls back to a semantic next-button href parsed from HTML.

    Code Writer adapted (pagination param discovery, no inline loop): this
    site's pagination type is load_more with NO detected page param
    (discovery_config: page_param_name=null), so construction-first cannot
    fire until a param is known. When the listing HTML advertises one, cache
    it (once) so construction-first takes over deterministically.
    """
    global _discovered_page_param
    if _discovered_page_param is None:
        _discovered_page_param = _detect_pagination_param(final_url, html or "")
    if _discovered_page_param:
        if _discovered_page_param in _OFFSET_PARAMS:
            value = (next_page_num - 1) * (ITEMS_PER_PAGE or 25)
        else:
            value = next_page_num
        return _set_query_param(final_url, _discovered_page_param, value)

    # Next-button href parsed from the HTML.
    if html:
        href = _extract_next_href(html)
        if href:
            return href
    return None


# Code Writer adapted: single auto-detect of the listing's pagination param.
_discovered_page_param: Optional[str] = None


def _detect_pagination_param(final_url: str, html: str) -> Optional[str]:
    """One-shot detection of the listing pagination param (no inline loop).

    Sources, best-first: (a) a ?page=/pg=/p= style param already present on
    the final listing URL, (b) a numbered pager href (?param=N) in the HTML
    — only accept params whose value is exactly the page number. Returns None
    (→ template's next-button-href path decides) when nothing is detectable.
    """
    for cand in ("page", "pg", "p"):
        if cand in parse_qsl(urlparse(final_url).query, keep_blank_values=True) or f"{cand}=" in (
            urlparse(final_url).query
        ):
            return cand
    if html:
        for m in re.finditer(r'[?&](\w+)=([2-9]\d*)["\'&]', html):
            if m.group(1) in ("page", "pg", "p", "pageNumber", "currentPage"):
                return m.group(1)
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
    if SEARCH_BOX_SELECTOR:
        actions.append({"type": "fill", "selector": SEARCH_BOX_SELECTOR, "value": query})
    if SEARCH_SUBMIT_SELECTOR:
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

    Code Writer adapted: tiers 1-2 use a STRICT '/product/' anchor selector
    (no permissive catch-all), and EVERY candidate is hard-filtered by
    _is_product_url() (the {slug}-{id} shape) before entering the result.
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
    if ITEM_CONTAINER_SELECTOR:
        try:
            containers = soup.select(ITEM_CONTAINER_SELECTOR)
        except Exception as exc:
            logger.warning("Phase 1: bad ITEM_CONTAINER_SELECTOR %r: %s", ITEM_CONTAINER_SELECTOR, exc)
            containers = []
        for container in containers:
            try:
                matches = container.select(ITEM_LINK_SELECTOR)
            except Exception:
                matches = []
            for a in matches:
                href = a.get("href", "")
                if href:
                    url = _make_absolute(href)
                    if _is_product_url(url):
                        links.append(url)

    # Tier 2: bare link selector (page-wide).
    if not links:
        try:
            for a in soup.select(ITEM_LINK_SELECTOR):
                href = a.get("href", "")
                if href:
                    url = _make_absolute(href)
                    if _is_product_url(url):
                        links.append(url)
        except Exception as exc:
            logger.warning("Phase 1: bare link selector failed: %s", exc)

    # Tier 3: broad fallback — all anchors matching the item URL pattern.
    # Every candidate still passes the strict _is_product_url shape check.
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

    # Dedupe, preserve first-seen order. Strip the tracking params but KEEP
    # ?size= — this site's variant is URL-addressable (?size=XS).
    deduped = []
    for u in links:
        p = urlparse(u)
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k == "size"]
        clean = urlunparse(p._replace(query=urlencode(q)))
        if clean not in deduped:
            deduped.append(clean)
    return deduped


def _discover_urls_via_search(
    query: str,
    max_pages: Optional[int] = None,
    limit: Optional[int] = None,
) -> tuple[list[str], str]:
    """Phase 1a: Discover item URLs by submitting the site's search form.

    Returns ``(urls, stop_reason)``. A navigate failure is "navigate_error"
    (FAIL), strictly distinct from exhaustion reasons.
    """
    search_url = (
        SEARCH_URL_PATTERN.replace("{query}", query)
        if "{query}" in SEARCH_URL_PATTERN
        else SEARCH_URL_PATTERN
    )
    logger.info("Phase 1: Searching for '%s' → %s", query, search_url)

    actions = _build_search_actions(query)
    resp = _navigate(search_url, actions=actions, proxy_tier=DISCOVERY_PROXY_TIER)
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
        resp = _navigate(next_url, proxy_tier=DISCOVERY_PROXY_TIER)
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

    # Resolve form URLs
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
                item_id = re.search(r'-1737(\d+)$', url) or re.search(r'/(\d{4,})/?$', url)
                dedup_key = item_id.group(0) if item_id else url
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
                    item_id = re.search(r'-1737(\d+)$', url) or re.search(r'/(\d{4,})/?$', url)
                    dedup_key = item_id.group(0) if item_id else url
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
    """Phase 1b: Discover item URLs from a category/listing page.

    Code Writer adapted: listing fetches ride the MEASURED datacenter rung
    (cloak_datacenter) — the probe proved the cloak+none rung blocks on
    listings; per-call proxy_tier overrides the default.
    """
    logger.info("Phase 1: Browsing category → %s", category_url)
    resp = _navigate(category_url, proxy_tier=DISCOVERY_PROXY_TIER)
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
        resp = _navigate(next_url, proxy_tier=DISCOVERY_PROXY_TIER)
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

# ── FIELD NORMALIZERS — inline on purpose, NOT a src import ──────────────────

def _norm_price(value) -> Optional[float]:
    """Strip currency symbols/whitespace from a price → float (never a string)."""
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


# ── SOFT-404 DETECTION [hard requirement] ────────────────────────────────────

_SOFT404_PATTERNS = re.compile(
    r"not\s*found|unavailable|discontinued|no\s*longer\s*available|page\s*not\s*found",
    re.I,
)


def _detect_soft_404(html: str, final_url: str, requested_url: str) -> Optional[str]:
    """Return a remark string when this is a soft-404, else None.

    Checks: (a) page <title>/H1 carries not-found/discontinued language,
    (b) Cloudflare challenge title, (c) the final URL redirected away from
    the /product/ path, (d) no Product JSON-LD at all.
    """
    if not html:
        return "empty response body"
    try:
        soup = BeautifulSoup(html[:200_000], "html.parser")
    except Exception:
        return None
    title = (soup.title.get_text(" ", strip=True) if soup.title else "")[:300]
    h1_el = soup.select_one("h1")
    h1 = h1_el.get_text(" ", strip=True)[:300] if h1_el else ""
    if re.search(r"attention\s+required.*cloudflare|cloudflare", title, re.I):
        return "Cloudflare challenge page (403) — not a product page"
    for probe in (title, h1):
        if probe and _SOFT404_PATTERNS.search(probe):
            return f"Soft 404: page title/H1 = '{probe[:80]}'"
    # Redirected off the product path (search/home/404 page)?
    try:
        req_path = urlparse(requested_url).path
        final_path = urlparse(final_url or requested_url).path
        if "/product/" in req_path and final_path != req_path and "/product/" not in final_path:
            return f"Soft 404: redirected off product path to {final_path}"
    except Exception:
        pass
    blocks = extract_jsonld(html)
    if not blocks or not any(
        (b.get("@type") == "Product" or (isinstance(b.get("@type"), list) and "Product" in b.get("@type")))
        for b in blocks
        if isinstance(b, dict)
    ):
        return "no Product JSON-LD block — not a product page"
    return None


# ── JSON-LD FIELD EXTRACTION (user schema: name/price/currency/description) ──

def _offers_price_and_currency(offers) -> tuple[Optional[float], str]:
    """(price, currency) from an offers node — object | array | AggregateOffer.

    Code Writer adapted [REMEDIATION]: rejects the known-bad price shapes
    the analyzer flagged ('0', '0.00', 'NaN') — a non-positive or
    unparseable price is treated as ABSENT so the next key/offer is tried
    instead of shipping a zero price.
    """
    if isinstance(offers, list):
        best: tuple[Optional[float], str] = (None, "")
        for offer in offers:
            p, c = _offers_price_and_currency(offer)
            if p is not None and p > 0:
                return p, c
            if best[0] is None and c:
                best = (p, c)  # keep currency context even from a bad price
        return best
    if not isinstance(offers, dict):
        return None, ""
    # AggregateOffer: lowPrice first (the live selling price), then highPrice.
    for key in ("price", "lowPrice", "highPrice"):
        raw = offers.get(key)
        if raw in (None, ""):
            continue
        price = _norm_price(raw)
        if price is not None and price > 0:
            return price, offers.get("priceCurrency", "") or ""
    # Nested offers (AggregateOffer.offers → list of Offers).
    if "offers" in offers:
        p, c = _offers_price_and_currency(offers["offers"])
        if p is not None:
            return p, c
    return None, ""


def _populate_from_jsonld(item: dict, jsonld_blocks: list[dict]) -> None:
    """Fill the USER-SCHEMA fields from JSON-LD — Product blocks only.

    Code Writer adapted: maps ONLY name → title, offers.price → price,
    offers.priceCurrency → currency, description → description, per the
    product_analysis field map. Standard-table fields (brand, sku, images,
    availability …) are NOT requested and NOT emitted.
    """
    for block in jsonld_blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("@type", "")
        if isinstance(block_type, list):
            block_type = block_type[0] if block_type else ""
        if block_type != "Product":
            continue

        if block.get("name") and not item.get("title"):
            item["title"] = str(block["name"]).strip()
        price, currency = _offers_price_and_currency(block.get("offers"))
        if price is not None and not item.get("price"):
            item["price"] = price
        if currency and not item.get("currency"):
            item["currency"] = str(currency).strip().upper()
        if block.get("description") and not item.get("description"):
            desc = str(block["description"]).strip()
            # Meta-description STUB guard (analysis): the meta description is
            # a stub; a JSON-LD description equal to it is not the real one.
            if desc and not _META_STUB_PATTERN.fullmatch(desc):
                item["description"] = desc
        break


_META_STUB_PATTERN = re.compile(r"^sports\s+shirt$", re.I)


def _extract_size(html: str, item_url: str) -> str:
    """User-schema `size` — MOST RELIABLE: the ?size= query param on the URL.

    Every product URL on this site carries the variant (?size=XS observed).
    Fallback (only when the param is absent): the selected size control in
    the DOM ([aria-checked='true'] / .selected / [aria-pressed='true']).
    """
    try:
        parsed = urlparse(item_url)
        for key, val in parse_qsl(parsed.query, keep_blank_values=True):
            if key.lower() == "size" and val.strip():
                return val.strip()
    except Exception:
        pass
    try:
        soup = BeautifulSoup(html[:400_000], "html.parser")
        for sel in (
            "[class*='size' i] [aria-checked='true']",
            "[class*='size' i] .active",
            "[data-size].selected",
            "[class*='swatch' i] [aria-pressed='true']",
        ):
            el = soup.select_one(sel)
            if el:
                text = el.get_text(" ", strip=True)
                if text:
                    return text
    except Exception:
        pass
    return ""


def _error_item(url: str, src_url: str, error: str) -> dict:
    return {
        "url": url,
        "src_url": src_url,
        "status_code": 0,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": f"Error: {error[:200]}",
    }


def _fetch_item_page(item_url: str) -> tuple[str, int, str, str]:
    """Fetch one PDP through the MEASURED transport — datacenter-proxy HTTP.

    Code Writer adapted [REMEDIATION]: PDP fetches ride the shared proxy
    ladder (``create_fetch_text`` — session cookie continuity, tier
    escalation, curl_cffi browser-TLS fingerprint rung), NOT a hand-rolled
    session.get() and NOT the browser /navigate path the probe measured as
    Cloudflare-403'd.

    Returns ``(html, status_code, final_url, transport_note)``.
    ``html`` is "" when every rung failed.

    Per the anti-bot guidance: on an HTTP 403 / ladder failure or a
    challenge-shaped 200 (title 'Attention Required! | Cloudflare'), retry
    ONCE via min_tier=1 — slicing the ladder to start at the fingerprint
    rung (browser-TLS impersonation) instead of the rung that refused us.
    """
    fetch_text = _get_thread_fetch_text()
    soft_block_cls = _get_soft_block_cls()
    if fetch_text is None:
        # Pre-src.http_fetch image — degrade to the guarded _http_get.
        got = _http_get(item_url)
        if _is_soft_block(got):
            return "", 200, "", "http_fallback_soft_block"
        html, status = got if got else ("", 0)
        return html, status, item_url, "http_fallback"

    transport_note = "datacenter_http"
    result = fetch_text(item_url, min_tier=0)
    if soft_block_cls is not None and isinstance(result, soft_block_cls):
        logger.warning(
            "PDP soft block (200 challenge, %s) on %s — retrying via fingerprint rung",
            result.reason, item_url[:80],
        )
        transport_note = "datacenter_http_softblock→fingerprint"
        result = fetch_text(item_url, min_tier=1)
    elif not result:
        logger.warning(
            "PDP fetch failed on the ladder (403/transport) for %s — "
            "retrying via fingerprint rung", item_url[:80],
        )
        transport_note = "datacenter_http_fail→fingerprint"
        result = fetch_text(item_url, min_tier=1)

    if soft_block_cls is not None and isinstance(result, soft_block_cls):
        logger.error(
            "PDP still challenge-shaped after escalation: %s (%s)",
            item_url[:80], result.reason,
        )
        return "", 200, "", transport_note + "_still_blocked"
    if not result:
        return "", 0, "", transport_note + "_exhausted"

    html, status_code = result
    final_url = item_url
    # Challenge-shape 200s (Cloudflare ships them as 200) — classify honestly
    # so the item record shows WHY it produced no fields.
    if status_code == 200 and _is_challenge_body(html):
        logger.error(
            "PDP 200 body is a challenge page ('%s') for %s",
            _page_title(html)[:80], item_url[:80],
        )
        return html, status_code, final_url, transport_note + "_challenge_200"
    return html, status_code, final_url, transport_note


def _extract_item(item_url: str, src_url: str) -> dict:
    """Phase 2: Extract structured data from a single item page.

    Code Writer adapted [REMEDIATION — cycle-1 FAIL, 0/10 items]: the
    previous extractor fetched each PDP through browser /navigate (cloak +
    tier 'none'); this site's tiered Cloudflare wall challenged those pages
    (or served a JS shell whose JSON-LD never rendered), so zero core fields
    were extracted and the output filter dropped all 10. The MEASURED PDP
    transport is datacenter-proxy HTTP with NO JS — the probe got HTTP 200
    with a 1.51 MB server-rendered body containing the complete Product
    JSON-LD block (name, description, offers.price, offers.priceCurrency).

    This extractor now:
      1. fetches the PDP via the shared proxy ladder (datacenter-proxy HTTP,
         curl_cffi fingerprint fallback) — not a hand-rolled session.get();
      2. classifies 403 / challenge-shaped 200s, and escalates ONCE through
         the ladder's fingerprint (browser-TLS) rung;
      3. parses all <script type="application/ld+json"> blocks in PYTHON,
         selects @type=='Product' (guarding @type arrays), and reads name,
         description (meta-STUB-guarded), and offers — Offer object,
         array of Offers, or AggregateOffer — for price/priceCurrency;
      4. takes `size` from the ?size= query param on the discovered URL
         (verified source), with the selected-size DOM control as fallback;
      5. records status_code / scraped_at / src_url per item and returns the
         item record even when fields are missing (failure diagnosability).
    """
    if DELAY_BETWEEN_REQUESTS:
        time.sleep(DELAY_BETWEEN_REQUESTS)

    html, status_code, final_url, transport = _fetch_item_page(item_url)
    if not html:
        # Anti-bot guidance (last rung): fall back to the cloak stealth
        # browser once — it survives walls that plain datacenter HTTP hits.
        resp = _navigate(item_url, proxy_tier=PROXY_TIER)
        if resp and resp.get("success") and resp.get("html"):
            html = resp["html"]
            status_code = resp.get("status_code", 200)
            final_url = resp.get("url") or item_url
            transport += "+stealth_browser_fallback"
            logger.info("Phase 2: stealth-browser fallback succeeded for %s", item_url[:80])
    item: dict = {
        "url": final_url or item_url,
        "src_url": src_url,
        "status_code": status_code,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": "",
    }
    if not html:
        item["remarks"] = (
            f"Error: fetch failed after retries (transport={transport}, status={status_code})"
        )
        return item

    # SOFT-404 gate — detect BEFORE extracting any field data.
    soft404 = _detect_soft_404(html, final_url, item_url)
    if soft404:
        item["remarks"] = soft404
        logger.info("Phase 2: soft-404 on %s — %s", item_url[:80], soft404)
        return item

    # JSON-LD extraction (user schema fields only) — pure Python parsing.
    try:
        jsonld_blocks = extract_jsonld(html)
        if jsonld_blocks:
            _populate_from_jsonld(item, jsonld_blocks)
    except Exception as exc:
        logger.warning("Phase 2: JSON-LD extraction failed for %s: %s", item_url[:60], exc)

    # description fallback: meta[name='description'] — with the STUB guard
    # (meta equals 'Sports Shirt' → treat description as unavailable).
    if not item.get("description"):
        try:
            soup = BeautifulSoup(html, "html.parser")
            meta = soup.select_one("meta[name='description']")
            if meta and meta.get("content"):
                desc = meta["content"].strip()
                if desc and not _META_STUB_PATTERN.fullmatch(desc):
                    item["description"] = desc
        except Exception:
            pass

    # size (user schema): URL param first, selected size control as fallback.
    if not item.get("size"):
        item["size"] = _extract_size(html, item_url)

    # currency last-resort: derive 'INR' from the ₹ symbol in the DOM price
    # area (analysis fallback) when JSON-LD gave no priceCurrency.
    if not item.get("currency"):
        try:
            soup = BeautifulSoup(html, "html.parser")
            for el in soup.select("[class*='price' i]"):
                if "₹" in el.get_text(" ", strip=True):
                    item["currency"] = "INR"
                    break
        except Exception:
            pass

    # price last-resort: a DOM price node near the buy-box when JSON-LD
    # carried no usable price (analysis fallback — never ship a stub).
    if not item.get("price"):
        try:
            soup = BeautifulSoup(html, "html.parser")
            for el in soup.select("[data-testid*='price' i], [class*='price' i]"):
                candidate = _norm_price(el.get_text(" ", strip=True))
                if candidate is not None and candidate > 0:
                    item["price"] = candidate
                    break
        except Exception:
            pass

    # url (kept bookkeeping field): final post-redirect URL, then canonical.
    if final_url:
        item["url"] = final_url
    else:
        try:
            soup = BeautifulSoup(html, "html.parser")
            canon = soup.select_one("link[rel='canonical']")
            if canon and canon.get("href"):
                item["url"] = urljoin(item_url, canon["href"])
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
# INPUT-URL / CLI-URL HELPERS (bookkeeping — NOT a discovery fallback)
# ═══════════════════════════════════════════════════════════════════════════════


def _load_input_urls(path: str) -> list[str]:
    """Read seed product URLs from a JSON file (list of strings, or a dict
    with a 'urls' key). Never widened — same-host only, as provided."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        logger.warning("input_urls: could not read %s: %s", path, exc)
        return []
    if isinstance(data, dict):
        data = data.get("urls") or data.get("items") or []
    if not isinstance(data, list):
        return []
    urls = []
    for u in data:
        if isinstance(u, str) and u.startswith("http"):
            urls.append(u)
        elif isinstance(u, dict) and isinstance(u.get("url"), str) and u["url"].startswith("http"):
            urls.append(u["url"])
    return urls


# ── CLI CONTRACT — keep every line below when adapting ───────────────────────
# The pipeline launches this scraper with EXACTLY these names. A flag missing
# from the argparse below is STRIPPED at launch and discovery silently falls
# back to the seed file (input_urls.json). Same for the SCRAPER_LISTING_URL
# env read in main(). ADD flags if you need them; NEVER remove or rename:
#   --fresh-discovery  always (execution)     --listing-url  navigation/list_page
#   --query            search_term            --input/--sample/--limit  testing
#   --discover-only    Phase-1 probe          (+ SCRAPER_LISTING_URL env read)
# ──────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description=f"{SITE_NAME} HTTP Navigation Scraper")
    parser.add_argument("--query", type=str, help="Search query for navigation mode")
    parser.add_argument("--category-url", type=str, help="Category URL to crawl")
    parser.add_argument("--listing-url", type=str, help="Listing page URL to paginate")
    parser.add_argument("--input", type=str, help="Path to input URLs JSON file")
    parser.add_argument("--urls", type=str, nargs="+", help="Product URLs as CLI arguments")
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
             "extraction.",
    )
    parser.add_argument(
        "--fresh-discovery", action="store_true",
        help="Ignore any discovered_urls_checkpoint.json and run Phase 1 from "
             "scratch (still writes a checkpoint as normal).",
    )
    args = parser.parse_args()

    # --no-proxy overrides the configured PROXY_TIER for this run.
    global PROXY_TIER, DISCOVERY_PROXY_TIER
    if args.no_proxy:
        PROXY_TIER = "none"
        DISCOVERY_PROXY_TIER = "none"

    # F6 DETERMINISTIC DISCOVERY GATE (env-var): run_execution injects
    # SCRAPER_LISTING_URL; the checkpoint gate below honors fresh_discovery.
    _env_listing = os.environ.get("SCRAPER_LISTING_URL", "").strip()
    if _env_listing:
        args.listing_url = _env_listing
        args.fresh_discovery = True
        logger.info("Env gate: SCRAPER_LISTING_URL → --listing-url %s (fresh)",
                    _env_listing[:80])

    limit = 5 if args.sample else args.limit
    start_time = time.time()
    discovered_urls: list[str] = []
    src_url_base = ""

    # ── Coverage-gate state (contract §1) ───────────────────────────────────
    ran_phase1 = True
    skipped_reason: Optional[str] = None
    aggregate_stop_reason = "no_next_link"
    max_pages_hit = False
    dimensions_iterated = 0
    dimensions_total = len(CATEGORY_URLS) if isinstance(CATEGORY_URLS, list) else 0

    # ── Input-URL / CLI-URL mode (bookkeeping — NOT a discovery fallback) ───
    # Code Writer adapted: --urls / --input provide KNOWN product URLs to
    # extract (test runs). They are evaluated BEFORE the checkpoint gate
    # (--input MUST take precedence over any checkpoint file), but they NEVER
    # substitute for discovery in the default (no-args) run — that still runs
    # Phase 1 from the listing URL / search.
    if args.urls:
        discovered_urls = list(dict.fromkeys(u.strip() for u in args.urls if u.strip()))
        src_url_base = args.listing_url or (CATEGORY_URLS[0] if CATEGORY_URLS else SITE_URL)
        ran_phase1 = False
        skipped_reason = "url_list"
        aggregate_stop_reason = "skipped"
        logger.info("URL list mode: %d URLs from --urls (no discovery)", len(discovered_urls))
    elif args.input:
        # --input MUST take precedence over any checkpoint file.
        discovered_urls = _load_input_urls(args.input)
        src_url_base = args.listing_url or (CATEGORY_URLS[0] if CATEGORY_URLS else SITE_URL)
        ran_phase1 = False
        skipped_reason = "url_list"
        aggregate_stop_reason = "skipped"
        logger.info("Input mode: %d URLs from %s (no discovery)", len(discovered_urls), args.input)

    # ── Resume from checkpoint if present (unless --fresh-discovery) ─────────
    # Only consulted when neither --urls nor --input provided the URL list.
    if not discovered_urls:
        checkpoint_urls = [] if args.fresh_discovery else _load_checkpoint()
        if checkpoint_urls:
            discovered_urls = checkpoint_urls
            ran_phase1 = False
            skipped_reason = "checkpoint_loaded"
            aggregate_stop_reason = "skipped"
            src_url_base = src_url_base or "(checkpoint)"
            logger.info(
                "Phase 1: SKIPPED (resumed from checkpoint with %d URLs)", len(discovered_urls),
            )

    # ── Phase 1: Discover URLs ──────────────────────────────────────────────
    if not discovered_urls:
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
            # DEFAULT (no-args) branch: ALWAYS full Phase 1 discovery — never
            # fall back to input_urls.json.
            logger.info(
                "Phase 1: default discovery via promoted listing %s",
                (CATEGORY_URLS[0] if CATEGORY_URLS else "")[:60],
            )
            src_url_base = CATEGORY_URLS[0] if CATEGORY_URLS else SITE_URL
            if CATEGORY_URLS:
                all_found: list[str] = []
                existing: set = set()
                for cat_url in CATEGORY_URLS:
                    cat_urls, cat_reason = _discover_urls_via_category(cat_url, MAX_PAGES, limit)
                    new = [u for u in cat_urls if u not in existing]
                    discovered_urls.extend(new)
                    existing.update(new)
                    aggregate_stop_reason = _merge_stop_reason(aggregate_stop_reason, cat_reason)
                    max_pages_hit = max_pages_hit or cat_reason == "max_pages_hit"
                all_found = discovered_urls
                dimensions_iterated = len(CATEGORY_URLS)
                discovered_urls = list(dict.fromkeys(all_found))
            else:
                sys.exit(1)
        logger.info("Phase 1: discovered %d URLs (pre-category)", len(discovered_urls))
        _write_checkpoint(discovered_urls)
    else:
        src_url_base = src_url_base or args.query or args.category_url or args.listing_url or "(seed)"

    # ── Phase 1b: Also discover from CATEGORY_URLS (skip on sample / resume) ─
    if (
        not args.sample
        and not discovered_urls
        and CATEGORY_URLS
        and ran_phase1
        and aggregate_stop_reason not in ("skipped",)
    ):
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

    # [job-58] Aggregate-level reclassification: zero URLs + only PASS-flavored
    # stop reasons is the "200-but-blocked" signature, NOT a genuine end.
    if not discovered_urls and aggregate_stop_reason in (
        "short_page", "no_next_link", "no_new_items"
    ):
        aggregate_stop_reason = "empty_first_page"

    # Code Writer adapted (self-heal rule 5): zero-yield discovery retries ONCE
    # with the known-good DEFAULT_LISTING_URL before giving up — the harness
    # may pass a listing whose card markup the selector missed.
    DEFAULT_LISTING_URL = "https://brooksbrothers.in/collection/collection-men"
    if not discovered_urls and DEFAULT_LISTING_URL not in (
        args.listing_url,
        args.category_url,
        *(CATEGORY_URLS or []),
    ):
        logger.warning(
            "ZERO-YIELD DISCOVERY — retrying ONCE with known-good listing %s",
            DEFAULT_LISTING_URL,
        )
        args.listing_url = DEFAULT_LISTING_URL
        retry_urls, retry_reason = _discover_urls_via_category(DEFAULT_LISTING_URL, MAX_PAGES, limit)
        if retry_urls:
            discovered_urls = retry_urls
            src_url_base = DEFAULT_LISTING_URL
            aggregate_stop_reason = retry_reason
            max_pages_hit = max_pages_hit or retry_reason == "max_pages_hit"
            logger.info("Zero-yield retry: %d URLs from the known-good listing", len(discovered_urls))

    if not discovered_urls and not args.discover_only:
        # [job-88] Exit non-zero with a parseable marker — a clean exit-0 with
        # no output file is indistinguishable from "wrote nothing".
        logger.error("DISCOVERY_ZERO: no item URLs discovered under the given listing")
        print("DISCOVERY_ZERO: no item URLs discovered under the given listing", file=sys.stderr)
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
                pool.submit(_extract_item_safe, url, src_url_base): url
                for url in discovered_urls
            }
            for future in as_completed(futures):
                url = futures[future]
                completed += 1
                try:
                    item = future.result()
                except Exception as exc:
                    item = _error_item(url, src_url_base, str(exc))
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

    # ── Output filter: user-schema core fields only ─────────────────────────
    # Drop extraction failures + items lacking title and the core field set
    # (price) — soft-404 items (remarks set, no fields) fall out here.
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
    output_filename = os.path.join(SCRIPT_DIR, f"output_{timestamp}_{os.getpid()}.json")
    with open(output_filename, "w", encoding="utf-8") as f:
        # _OUTPUT_FILTER_APPLIED — drop non-item pages (content-type aware)
        _FILTER_FIELDS = ['product name', 'price', 'currency', 'description', 'size']
        try:
            _OUTPUT_KEY = OUTPUT_KEY if 'OUTPUT_KEY' in dir() else next(
                (k for k, v in output.items() if isinstance(v, list)
                 and v and isinstance(v[0], dict)), None)
            if _OUTPUT_KEY:
                _before = len(output[_OUTPUT_KEY])
                output[_OUTPUT_KEY] = [p for p in output[_OUTPUT_KEY] if p.get('product name') or p.get('price') or p.get('currency') or p.get('description') or p.get('size')]
                _after = len(output[_OUTPUT_KEY])
                if _before != _after:
                    logger.info('output filter: %d → %d items (removed %d without any of product name,price,currency,description,size)',
                                 _before, _after, _before - _after)
        except Exception:
            pass


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
