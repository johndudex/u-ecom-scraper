#!/usr/bin/env python3
"""HTTP Navigation Scraper — calls browser_service POST /navigate per page.

Two-phase architecture (mirrors templates/navigation_scraper.py):

  Phase 1: Discover item URLs by paginating the promoted category/listing
           page. Each page fetch is one POST /navigate call; link extraction +
           pagination are computed locally on the returned HTML.
  Phase 2: Extract structured data from each discovered item page. Item
           fetches run concurrently in a ThreadPoolExecutor; each item is
           one POST /navigate call. JSON-LD + CSS parsing happen locally.

teva.com specifics (Code Writer adapted):
  - DataDome anti-bot: plain HTTP is 403-challenged. The MEASURED working
    transports are residential-proxy fetches (listing=fingerprint_safari184_
    residential, PDP=fingerprint_chrome_residential). All fetches therefore
    ride proxy_tier=residential through browser_service, with a REAL
    escalation ladder (residential re-issue -> browser + stealth cloak) on
    any block — soft_block_escalations counts every genuine re-issue.
  - SSR pages embed one rich schema.org Product JSON-LD block; ALL requested
    fields (title/price/currency/description/availability) come from it.

Usage:
    python3 scraper.py --listing-url "https://www.teva.com/c/women-view-all"
    python3 scraper.py --sample                                        # first 5 items only
    python3 scraper.py --limit 50                                      # cap item count
    python3 scraper.py --input input_urls.json                         # seed URLs
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
    """True when ``obj`` is the shared ladder's challenge signal."""
    return SoftBlock is not None and isinstance(obj, SoftBlock)


def _http_get(url: str):
    """HTTP GET through the shared proxy ladder [wave-15 3.4]."""
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

# Code Writer adapted: guarded import + local pure-python fallbacks so the
# draft also runs in sandboxes where src/ is absent (page_analysis is pure
# python — regex JSON-LD parse + instant-fail arithmetic).
try:
    from src.page_analysis import (  # noqa: E402
        extract_jsonld,
        phase2_instant_fail,
    )
except Exception:  # pragma: no cover — verification sandbox without src/
    _JSONLD_SCRIPT_RE = re.compile(
        r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
        re.I | re.S,
    )

    def extract_jsonld(html):  # noqa: E302
        """Local JSON-LD parser: all ld+json blocks, @graph unwrapped."""
        blocks: list = []
        if not html:
            return blocks
        for match in _JSONLD_SCRIPT_RE.finditer(html):
            raw = (match.group(1) or "").strip()
            if not raw:
                continue
            data = None
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                try:
                    data = json.loads(re.sub(r"^\s*/\*.*?\*/\s*", "", raw, flags=re.S))
                except Exception as exc:
                    logger.debug("JSON-LD block parse failed: %s", exc)
                    continue
            entries = data if isinstance(data, list) else [data]
            for entry in entries:
                if isinstance(entry, dict):
                    blocks.append(entry)
                    graph = entry.get("@graph")
                    if isinstance(graph, list):
                        blocks.extend(g for g in graph if isinstance(g, dict))
        return blocks

    def phase2_instant_fail(duration_s, total_items, min_fetch_s, workers=1):  # noqa: E302
        """True when Phase 2 finished faster than half its per-fetch floor."""
        try:
            if not total_items:
                return False
            return duration_s < (total_items * min_fetch_s * 0.5) / max(workers or 1, 1)
        except Exception:
            return False


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION — code_writer substitutes {PLACEHOLDERS} from analysis artifacts.
# ═══════════════════════════════════════════════════════════════════════════════

SITE_NAME = "Teva"
SITE_URL = "https://www.teva.com"
PLATFORM = "deckers-ssr-custom"
SITE_SLUG = "teva-com"

# ── Execution model ──────────────────────────────────────────────────────────
BROWSER_SERVICE_URL = os.environ.get("BROWSER_SERVICE_URL", "http://browser_service:8001")

# Per-site cloak flag forwarded in every /navigate body.
# Code Writer adapted: teva's ACCESS RECIPE measures stealth=none for browser
# calls (the working rungs are residential TLS-fingerprint fetches); the cloak
# rung is engaged per-call by the escalation ladder only when a block happens.
STEALTH = "none"
_env_stealth = (os.environ.get("STEALTH_BROWSER") or os.environ.get("SCRAPER_STEALTH") or "").strip().lower()
if _env_stealth in ("cloak", "true", "1"):
    STEALTH = "cloak"
elif STEALTH.startswith("{") and STEALTH.endswith("}"):
    STEALTH = "none"

NAVIGATE_TIMEOUT = 120
NAVIGATE_SETTLE_MS = "4000"
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
SEARCH_URL_PATTERN = "https://www.teva.com/search?q={query}"
SEARCH_BOX_SELECTOR = ""
SEARCH_SUBMIT_SELECTOR = ""
CATEGORY_URLS = []                                 # Phase 1b dormant — the promoted listing paginates the women catalog
# Code Writer adapted: promoted listing from navigation_analysis — the default
# (no-args) run MUST do full Phase 1 discovery from here.
DEFAULT_LISTING_URL = "https://www.teva.com/c/women-view-all"

FORM_ACTION = ""
FORM_METHOD = "POST"
FORM_SELECT_NAME = ""
FORM_BASE_URL = ""

# ── Phase 1: Pagination ─────────────────────────────────────────────────────
# Code Writer adapted: teva is a load_more storefront, but its SSR category
# pages honor ?page=N. The template constructs ?page=N deterministically via
# _get_next_page_url and stops via its no_new_items/short_page dedup gate, so
# an ignored param costs nothing — and a honored param yields the full catalog.
PAGINATION_TYPE = "page_param"
NEXT_BUTTON_SELECTOR = ""
PAGE_PARAM_NAME = "page"
ITEMS_PER_PAGE = None
MAX_PAGES = None                                   # unlimited — paginate to exhaustion
TOTAL_COUNT_SELECTOR = ""
DISCOVERY_DEADLINE_SECONDS = 300

COVERAGE_TARGET_TOTAL: Optional[int] = None

# ── Phase 1: Item link extraction ───────────────────────────────────────────
# Code Writer adapted: teva PDP shape is /p/{category}/{slug}/{numeric-id}.
# Strict pattern only — no permissive catch-all (job-318 lesson).
ITEM_CONTAINER_SELECTOR = ""
ITEM_LINK_SELECTOR = ""
ITEM_URL_PATTERN = r"/p/[^/?#]+/[^/?#]+/\d+"

# ── Phase 2: Extraction ─────────────────────────────────────────────────────
SCRAPING_METHOD = "http_navigation"
# Code Writer adapted: ACCESS RECIPE — both measured rungs (listing
# fingerprint_safari184_residential, PDP fingerprint_chrome_residential) carry
# the RESIDENTIAL proxy component; the previous draft's non-residential tier
# drew the DataDome 403. Discovery and PDP tiers both start residential here.
# SCRAPER_PROXY_TIER env / --no-proxy still override, as the template allows.
PROXY_TIER = "residential"
_env_tier = (os.environ.get("SCRAPER_PROXY_TIER") or "").strip().lower()
if _env_tier in ("none", "datacenter", "residential"):
    PROXY_TIER = _env_tier
DELAY_BETWEEN_REQUESTS = 1.0

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

# ── Ban-escalation state (Code Writer adapted — remediation) ────────────────
# The failed draft ANNOUNCED escalation but never re-issued a request
# (soft_block_escalations=0). These counters are incremented ONLY when a
# higher-rung request actually goes out, and are reported in metadata.
_SOFT_BLOCK_ESCALATIONS = 0
_RETRIED_EMPTY_DISCOVERY = False
_FORCE_BROWSER_RUNG = False

# Escalation ladders: rung 0 is the MEASURED working transport (residential);
# later rungs re-issue on a fresh residential identity, then on the full
# stealth browser (the remediation-sanctioned Playwright+stealth rung).
_LISTING_LADDER = [
    {"proxy_tier": "residential", "stealth": "none"},
    {"proxy_tier": "residential", "stealth": "none"},
    {"proxy_tier": "residential", "stealth": "cloak"},
]
_PDP_LADDER = [
    {"proxy_tier": "residential", "stealth": "none"},
    {"proxy_tier": "residential", "stealth": "cloak"},
]

# Challenge-page signatures served with HTTP 200 (the "200-but-blocked" trap).
_CHALLENGE_MARKERS = (
    "captcha-delivery.com",
    "ddjskey",
    "datadome",
    "var dd = {",
    "px-captcha",
    "_pxhdr",
    "cf-chl",
    "just a moment",
)


def _looks_like_challenge(html: str) -> bool:
    """True when the HTML is an anti-bot interstitial, not site content."""
    if not html:
        return False
    low = html[:20000].lower()
    return any(marker in low for marker in _CHALLENGE_MARKERS)


# Code Writer adapted (live-run finding): DataDome is embedded as an inline
# defense script on EVERY real teva page — marker hits alone convict genuine
# content. The live run proved it: the successful cloak listing render carried
# dd markers AND 32 product links. Content, not markers, adjudicates.
_PRODUCT_URL_RE = re.compile(r"/p/[^/?#]+/[^/?#]+/\d+")


def _page_has_content(html: str) -> bool:
    """True when HTML carries real site content, not just a challenge shell."""
    if not html:
        return False
    return len(_PRODUCT_URL_RE.findall(html)) >= 3


def _blocked_signal(resp) -> str:
    """Short reason string when a /navigate response looks blocked, else ''."""
    if not isinstance(resp, dict):
        return "navigate_failed"
    if resp.get("blocked"):
        return "anti_bot_wall"
    status = resp.get("status_code") or 0
    if status in (401, 403, 429, 503):
        return f"http_{status}"
    if _looks_like_challenge(resp.get("html") or "") and not _page_has_content(resp.get("html") or ""):
        return "challenge_page_200"
    return ""


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


def _load_seed_urls(path: str) -> list[str]:
    """Code Writer adapted: load --input seed file (list or {"urls": [...]})

    Keeps only same-host http(s) URLs — the file must never widen discovery."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        logger.warning("Seed file %s unreadable: %s", path, exc)
        return []
    if isinstance(data, dict):
        data = data.get("urls") or data.get("items") or []
    if not isinstance(data, list):
        return []
    site_host = (urlparse(SITE_URL).hostname or "").lower()
    out: list[str] = []
    for entry in data:
        if not isinstance(entry, str) or not entry.startswith(("http://", "https://")):
            continue
        host = (urlparse(entry).hostname or "").lower()
        if site_host and host != site_host:
            continue
        if entry not in out:
            out.append(entry)
    logger.info("Seed file: %d same-host URLs from %s", len(out), path)
    return out


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


# Code Writer adapted: added OPTIONAL per-call proxy_tier/stealth_mode kwargs
# (default None → previous global behavior; every existing call site valid).
# Needed so the listing and PDP ladders can ride their measured tiers.
def _navigate(url, actions=None, extract=None, retry=0, settle_ms=None,
              proxy_tier: Optional[str] = None, stealth_mode: Optional[str] = None):
    """POST /navigate with exponential backoff. Returns the response dict or None.

    Contract:
      - 200 + success=True  → return data (has url/html/data/...)
      - 200 + blocked=True  → terminal (anti-bot wall); return data so caller
                              can distinguish "blocked" from "navigate failed"
      - 429 / 503 / 502     → retryable; honor Retry-After
      - 5xx / timeouts /    → retryable; exponential backoff (base 2, cap 30s)
      - 404                 → terminal; return the body so the caller can record
                              an error item without burning the retry budget

    Returns None only after all MAX_RETRIES attempts are exhausted — EXCEPT:
      - a 429-exhaustion returns a terminal throttled dict
      - an infrastructure exhaustion returns a terminal navigate_unavailable dict
    """
    _stealth = stealth_mode if stealth_mode is not None else STEALTH
    payload = {
        "url": url,
        "actions": actions or [],
        "extract": extract or {},
        "stealth": "cloak" if str(_stealth).lower() == "cloak" else "none",
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


# B3: last browser-service outage detail seen by ``_navigate``.
_nav_unavailable_status = 0
_nav_unavailable_class = ""


# Code Writer adapted — REMEDIATION CORE: real ban escalation. On any block
# signal (403/anti-bot wall/challenge-as-200/navigate failure) the request is
# RE-ISSUED on the next rung of the ladder after a backoff, and
# soft_block_escalations is incremented per genuine re-issue.
def _navigate_with_escalation(url, ladder, label, actions=None):
    """Fetch ``url`` walking ``ladder`` rung-by-rung until content returns.

    Returns the last /navigate response dict (callers apply the standard
    ``not resp or not resp.get("success")`` checks). Honors --no-proxy /
    SCRAPER_PROXY_TIER=none by pinning every rung to the "none" tier.
    """
    global _SOFT_BLOCK_ESCALATIONS
    global_tier = _effective_proxy_tier()
    rungs = ladder[-1:] if _FORCE_BROWSER_RUNG else ladder
    resp = None
    for idx, rung in enumerate(rungs):
        rung_tier = rung["proxy_tier"] if global_tier != "none" else "none"
        resp = _navigate(
            url,
            actions=actions,
            proxy_tier=rung_tier,
            stealth_mode=rung["stealth"],
        )
        block = _blocked_signal(resp)
        if not block and isinstance(resp, dict) and resp.get("success"):
            if idx:
                logger.info(
                    "%s: RECOVERED on rung %d/%d (tier=%s stealth=%s) after %d escalation(s)",
                    label, idx + 1, len(rungs), rung_tier, rung["stealth"], _SOFT_BLOCK_ESCALATIONS,
                )
            return resp
        if block:
            _SOFT_BLOCK_ESCALATIONS += 1
            logger.warning(
                "%s: BLOCKED (%s) on rung %d/%d (tier=%s stealth=%s) — "
                "soft_block_escalations=%d",
                label, block, idx + 1, len(rungs), rung_tier, rung["stealth"],
                _SOFT_BLOCK_ESCALATIONS,
            )
            if idx < len(rungs) - 1:
                backoff = 6 * (idx + 1)
                logger.warning(
                    "%s: escalating to next rung (%s) in %ds",
                    label,
                    f"tier={rungs[idx + 1]['proxy_tier']} stealth={rungs[idx + 1]['stealth']}",
                    backoff,
                )
                time.sleep(backoff)
    logger.error("%s: BLOCKED on every rung — giving up on %s", label, url[:80])
    return resp


# ═══════════════════════════════════════════════════════════════════════════════
# URL HELPERS — pure functions, no browser dependency.
# ═══════════════════════════════════════════════════════════════════════════════


def _make_absolute(href: str) -> str:
    """Resolve a possibly-relative href against SITE_URL.

    [wave-19 T1.9] A leading '//' is protocol-relative ONLY when the next
    segment is a plausible netloc (contains a dot)."""
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
    ``final_url`` — deterministic, DOM-independent. Falls back to a semantic
    next-button href parsed from the page HTML.
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

    Teva: the strict /p/{cat}/{slug}/{id} ITEM_URL_PATTERN drives Tier 3,
    which is the active tier (no card-level selectors are known-stable).
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
    """Phase 1a: Discover item URLs by submitting the site's search form.

    Returns ``(urls, stop_reason)``. A navigate failure is ``navigate_error``
    (FAIL), never an exhaustion reason. Code Writer adapted: fetches ride the
    listing escalation ladder + a wall-clock deadline.
    """
    search_url = (
        SEARCH_URL_PATTERN.replace("{query}", query)
        if "{query}" in SEARCH_URL_PATTERN
        else SEARCH_URL_PATTERN
    )
    logger.info("Phase 1: Searching for '%s' → %s", query, search_url)

    actions = _build_search_actions(query)
    resp = _navigate_with_escalation(search_url, _LISTING_LADDER, "Phase1 search", actions=actions)
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
    deadline = time.time() + DISCOVERY_DEADLINE_SECONDS
    while True:
        if max_pages and current_page >= max_pages:
            logger.info("Phase 1: Reached max_pages=%d", max_pages)
            stop_reason = "max_pages_hit"
            break
        if limit and len(all_urls) >= limit:
            logger.info("Phase 1: Reached limit=%d", limit)
            stop_reason = "max_pages_hit"
            break
        if time.time() > deadline:
            # Code Writer adapted: bounded discovery wall-clock.
            logger.warning("Phase 1: discovery deadline (%ss) reached at page %d",
                           DISCOVERY_DEADLINE_SECONDS, current_page)
            stop_reason = "max_pages_hit"
            break

        next_url = _get_next_page_url(final_url, current_page + 1, html)
        if not next_url:
            logger.info("Phase 1: No more pages (stopped at page %d)", current_page)
            stop_reason = "no_next_link"
            break

        logger.info("Phase 1: Navigating to page %d", current_page + 1)
        resp = _navigate_with_escalation(next_url, _LISTING_LADDER, "Phase1 search-paginate")
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
    """Phase 1c: Discover item URLs by iterating through ALL options of a <select>.

    (Dormant on teva — FORM_ACTION/FORM_SELECT_NAME unset; template verbatim.)
    """
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
    """Phase 1b: Discover item URLs from a category/listing page.

    Code Writer adapted: every listing fetch rides the escalation ladder
    (_navigate_with_escalation) so a DataDome 403 triggers genuine higher-rung
    re-issues instead of ending discovery; plus a wall-clock deadline.
    """
    logger.info("Phase 1: Browsing category → %s", category_url)
    resp = _navigate_with_escalation(category_url, _LISTING_LADDER, "Phase1 listing")
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
    logger.info("Phase 1: listing page 1 → %d item URLs (final_url=%s)", len(all_urls), final_url[:80])

    stop_reason = "no_next_link"
    current_page = 1
    deadline = time.time() + DISCOVERY_DEADLINE_SECONDS
    while True:
        if max_pages and current_page >= max_pages:
            stop_reason = "max_pages_hit"
            break
        if limit and len(all_urls) >= limit:
            stop_reason = "max_pages_hit"
            break
        if time.time() > deadline:
            # Code Writer adapted: bounded discovery wall-clock.
            logger.warning("Phase 1: discovery deadline (%ss) reached at page %d",
                           DISCOVERY_DEADLINE_SECONDS, current_page)
            stop_reason = "max_pages_hit"
            break

        next_url = _get_next_page_url(final_url, current_page + 1, html)
        if not next_url:
            stop_reason = "no_next_link"
            break

        logger.info("Phase 1: Category page %d", current_page + 1)
        resp = _navigate_with_escalation(next_url, _LISTING_LADDER, "Phase1 listing-paginate")
        if not resp or not resp.get("success"):
            stop_reason = _nav_fail_reason(resp)
            logger.warning(
                "Phase 1: listing page %d navigate failed, stopping (%s)",
                current_page + 1, stop_reason,
            )
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


def _discover_primary(args, limit: Optional[int]):
    """Code Writer adapted: the template's Phase-1 dispatch chain, factored so
    the empty-discovery retry can re-invoke it on the strongest rung.
    Returns (urls, stop_reason, src_url_base)."""
    if FORM_ACTION and FORM_SELECT_NAME:
        urls, reason = _discover_urls_via_form_search(MAX_PAGES, limit)
        return urls, reason, FORM_ACTION
    if args.query:
        urls, reason = _discover_urls_via_search(args.query, MAX_PAGES, limit)
        return urls, reason, SEARCH_URL_PATTERN.replace("{query}", args.query)
    if args.category_url:
        urls, reason = _discover_urls_via_category(args.category_url, MAX_PAGES, limit)
        return urls, reason, args.category_url
    listing_url = args.listing_url or DEFAULT_LISTING_URL
    urls, reason = _discover_urls_via_category(listing_url, MAX_PAGES, limit)
    return urls, reason, listing_url


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 2: ITEM EXTRACTION (concurrent)
# ═══════════════════════════════════════════════════════════════════════════════


def _norm_price(value) -> Optional[float]:
    """Strip currency symbols/whitespace from a price → float (or None)."""
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
    if "://" in text:  # e.g. http://schema.org/InStock
        text = text.rsplit("/", 1)[-1]
    compact = text.replace("-", "_").replace(" ", "")
    if compact in ("in_stock", "instock", "available"):
        return "in_stock"
    if compact in ("out_of_stock", "outofstock", "unavailable", "sold_out", "soldout"):
        return "out_of_stock"
    return text


def _populate_from_jsonld(item: dict, jsonld_blocks: list[dict]) -> bool:
    """Fill ``item`` from JSON-LD blocks, CONTENT_TYPE-aware.

    Code Writer adapted (field map — the ONLY requested fields):
      title        ← Product.name              (never the <title> tag → no
                                              " | TEVA®" suffix)
      price        ← Product.offers.price      (numeric via _norm_price)
      currency     ← Product.offers.priceCurrency (fallback '$'→USD)
      description  ← Product.description       (NEVER <meta name=description> —
                                              that is site-wide boilerplate)
      availability ← Product.offers.availability (normalized in_stock/out_of_stock;
                                              mirrored to the schema's
                                              "avaliability" spelling)
    Returns True when a Product block populated the item.
    """
    populated = False
    for block in jsonld_blocks:
        block_type = block.get("@type", "")
        if isinstance(block_type, list):
            block_type = block_type[0] if block_type else ""

        if CONTENT_TYPE == "product" and block_type in ("Product", "ProductGroup"):
            populated = True
            item["title"] = (block.get("name") or "").strip() or item.get("title", "")
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
                price = offer.get("price")
                if price is None and isinstance(offer.get("priceSpecification"), dict):
                    price = offer["priceSpecification"].get("price")
                if price is not None:
                    norm = _norm_price(price)
                    if norm is not None:
                        item["price"] = norm
                    item["currency"] = offer.get("priceCurrency") or CURRENCY
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
            # JSON-LD product copy ONLY — never the <meta> boilerplate.
            if block.get("description"):
                item["description"] = block["description"]
            # Schema alias: the requested field is spelled "avaliability".
            if item.get("availability"):
                item["avaliability"] = item["availability"]
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
    return populated


def _error_item(url: str, src_url: str, error: str) -> dict:
    return {
        "url": url,
        "src_url": src_url,
        "status_code": 0,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": f"Error: {error[:200]}",
    }


_SOFT404_MARKERS = (
    "page not found",
    "product not found",
    "no longer available",
    "currently unavailable",
    "discontinued",
    "404 error",
)


def _soft404_signal(has_product_ld: bool, html: str, resp, item_url: str) -> Optional[str]:
    """Reason string when this page is NOT a real product page, else None.

    Soft-404 contract: JSON-LD Product presence, title/h1 markers, and
    redirect-to-non-product detection. (Variant PDPs legitimately redirect to
    a canonical path, so a redirect alone never convicts — only a redirect
    WITHOUT product JSON-LD does.)"""
    text_probe = ""
    try:
        soup = BeautifulSoup(html or "", "html.parser")
        title_tag = soup.title.get_text(strip=True).lower() if soup.title else ""
        h1 = soup.h1.get_text(strip=True).lower() if soup.h1 else ""
        text_probe = f"{title_tag} {h1}"
    except Exception:
        pass
    for marker in _SOFT404_MARKERS:
        if marker in text_probe:
            return f'page title/h1 indicates "{marker}"'
    if not has_product_ld:
        final_url = ""
        if isinstance(resp, dict):
            final_url = resp.get("url") or ""
        try:
            same_path = (
                not final_url
                or urlparse(final_url).path.rstrip("/") == urlparse(item_url).path.rstrip("/")
            )
        except Exception:
            same_path = True
        if not same_path:
            return f"redirected to a non-product page ({final_url[:120]})"
        return "no JSON-LD Product block on page"
    return None


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
        from urllib.parse import urlparse
    except Exception:  # pragma: no cover
        return []
    alts = _hreflang_alternates(html)
    if not alts:
        return []
    item_host = (urlparse(item_url).hostname or "").lower()
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
        if (urlparse(u).hostname or "").lower() != item_host:
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

    Code Writer adapted: fetches ride the PDP escalation ladder; JSON-LD
    Product block supplies every requested field; challenge pages and
    soft-404s are detected and reported in remarks.
    """
    if DELAY_BETWEEN_REQUESTS:
        time.sleep(DELAY_BETWEEN_REQUESTS)

    resp = _navigate_with_escalation(item_url, _PDP_LADDER, "Phase2 pdp")
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
    item: dict = {
        "url": item_url,
        "src_url": src_url,
        "status_code": resp.get("status_code") or 200,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": "",
    }

    # JSON-LD extraction (shared helper when present, local fallback otherwise).
    has_product_ld = False
    try:
        jsonld_blocks = extract_jsonld(html)
        if jsonld_blocks:
            has_product_ld = _populate_from_jsonld(item, jsonld_blocks)
    except Exception as exc:
        logger.warning("Phase 2: JSON-LD extraction failed for %s: %s", item_url[:60], exc)

    # An anti-bot interstitial served with HTTP 200 is NOT product data —
    # but DataDome's inline defense script rides every REAL teva page, so
    # markers alone never convict: only a marker hit WITHOUT product JSON-LD
    # is a challenge shell (Code Writer adapted, live-run finding).
    if not has_product_ld and _looks_like_challenge(html):
        item["remarks"] = "Blocked: anti-bot challenge page served instead of product data"
        return item

    # CSS fallback for title (h1) when JSON-LD didn't yield one. Teva's h1 can
    # render empty pre-hydration — only accept non-empty text.
    if not item.get("title"):
        try:
            soup = BeautifulSoup(html, "html.parser")
            h1 = soup.select_one("h1")
            if h1:
                h1_text = h1.get_text(strip=True)
                if h1_text:
                    item["title"] = h1_text
        except Exception:
            pass

    # Conservative buybox price fallback (meta itemprop) when JSON-LD lacks one.
    if not item.get("price"):
        try:
            soup = soup if "soup" in dir() else BeautifulSoup(html, "html.parser")  # reuse if parsed
            meta_price = soup.select_one('[itemprop="price"]')
            if meta_price is not None:
                raw = meta_price.get("content") or meta_price.get_text(strip=True)
                norm = _norm_price(raw)
                if norm is not None:
                    item["price"] = norm
                    if "currency" not in item:
                        item["currency"] = CURRENCY
        except Exception:
            pass

    # Soft-404 detection — non-product pages get remarks, no extracted fields.
    soft404 = _soft404_signal(has_product_ld, html, resp, item_url)
    if soft404:
        item["remarks"] = f"Soft 404: {soft404}"
        for key in ("title", "price", "currency", "description", "availability", "avaliability"):
            item.pop(key, None)
        return item

    # [wave-17 S15b] Locale escalation for price-less renders.
    if not item.get("price"):
        for alt_url in _locale_escalation_urls(html, item_url):
            alt_resp = _navigate_with_escalation(alt_url, _PDP_LADDER, "Phase2 pdp-locale")
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
    # Code Writer adapted: HARD-CONTRACT seed flags (--input beats checkpoint).
    parser.add_argument("--input", type=str, default=None, help="Path to input URLs JSON file")
    parser.add_argument("--urls", type=str, nargs="+", default=None, help="Product URLs as CLI arguments")
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

    global PROXY_TIER, _SOFT_BLOCK_ESCALATIONS
    global _FORCE_BROWSER_RUNG, _RETRIED_EMPTY_DISCOVERY
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

    # ── Seeds (--input/--urls) take precedence over the checkpoint ──────────
    # Code Writer adapted: HARD CONTRACT — args.input is checked BEFORE
    # _load_checkpoint(); when set, the checkpoint load is skipped entirely.
    # Seeds are narrowed to PRODUCT-pattern URLs: input_urls.json may still
    # hold category/listing stubs from a failed prior discovery, and feeding
    # those to Phase 2 yields only soft-404s. With zero usable product seeds
    # the run falls through to Phase 1 discovery (never an empty pass).
    seed_urls: list[str] = []
    if args.input or args.urls:
        raw_seeds: list[str] = []
        if args.input:
            raw_seeds = _load_seed_urls(args.input)
        else:
            site_host = (urlparse(SITE_URL).hostname or "").lower()
            raw_seeds = [
                u for u in (args.urls or [])
                if isinstance(u, str)
                and u.startswith(("http://", "https://"))
                and (not site_host or (urlparse(u).hostname or "").lower() == site_host)
            ]
            logger.info("CLI seeds: %d same-host URLs via --urls", len(raw_seeds))
        seed_pat = re.compile(ITEM_URL_PATTERN)
        seed_urls = [u for u in raw_seeds if seed_pat.search(u)]
        dropped = len(raw_seeds) - len(seed_urls)
        if dropped:
            logger.warning(
                "Seeds: dropped %d non-product URL(s) (not matching %s) — "
                "category stubs are discovery inputs, not PDPs",
                dropped, ITEM_URL_PATTERN,
            )
        if raw_seeds and not seed_urls:
            logger.warning(
                "Seeds: NO product URLs in the seed file — falling through to "
                "Phase 1 discovery from the listing"
            )

    checkpoint_urls: list[str] = []
    if seed_urls:
        discovered_urls = list(seed_urls)
        ran_phase1 = False
        skipped_reason = "input_file"
        aggregate_stop_reason = "skipped"
        logger.info("Phase 1: SKIPPED (--input/--urls seed with %d URLs)", len(discovered_urls))
    elif not args.fresh_discovery:
        checkpoint_urls = _load_checkpoint()
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
        # Code Writer adapted — REMEDIATION: empty discovery retries ONCE on
        # the strongest rung (browser + stealth) before giving up, per the
        # "retry empty discovery at least once" instruction.
        for discovery_attempt in range(2):
            if discovery_attempt == 1:
                _FORCE_BROWSER_RUNG = True
                _RETRIED_EMPTY_DISCOVERY = True
                logger.warning(
                    "Phase 1: empty discovery — retrying ONCE on the browser+"
                    "stealth rung (retried_empty_discovery=True)"
                )
            discovered_urls, primary_reason, src_url_base = _discover_primary(args, limit)
            aggregate_stop_reason = _merge_stop_reason(aggregate_stop_reason, primary_reason)
            max_pages_hit = max_pages_hit or primary_reason == "max_pages_hit"
            if discovered_urls:
                break
        logger.info("Phase 1: discovered %d URLs (pre-category)", len(discovered_urls))
        _write_checkpoint(discovered_urls)
    elif seed_urls:
        src_url_base = ""  # seed mode: src_url is the item URL itself
    else:
        src_url_base = args.query or args.category_url or args.listing_url or "(checkpoint)"

    # ── Phase 1b: Also discover from CATEGORY_URLS (skip on sample / resume) ─
    if not args.sample and CATEGORY_URLS and not seed_urls and not checkpoint_urls:
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
                # src_url = the item URL itself when seeded via --input/--urls.
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
        # Code Writer adapted — REMEDIATION evidence: every genuine higher-rung
        # re-issue and the empty-discovery retry, surfaced for the tester.
        "soft_block_escalations": _SOFT_BLOCK_ESCALATIONS,
        "retried_empty_discovery": _RETRIED_EMPTY_DISCOVERY,
        "access_recipe": {
            "listing_rung": "residential (fingerprint_safari184_residential)",
            "pdp_rung": "residential (fingerprint_chrome_residential)",
            "escalation_rungs": ["residential re-issue", "browser+stealth cloak"],
        },
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
            "proxy_tier": _effective_proxy_tier(),
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
