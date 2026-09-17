#!/usr/bin/env python3
"""JetPens HTTP Navigation Scraper — two-phase (browser-rung listing + curl_cffi PDP).

Transport contract (ACCESS RECIPE, measured — do not strip):
  LISTING (Phase 1):  cloak_datacenter  — browser rung via browser_service /navigate
  PDP (Phase 2):      fingerprint_chrome_residential — curl_cffi (chrome TLS) HTTP,
                      reached as the src.http_fetch ladder's top rung (min_tier="fingerprint").
Plain-HTTP rungs are BLOCKED on this site (Cloudflare TLS-fingerprint scoring); only the
browser rung measured 200-with-content for listings and only the fingerprint rung for items.

Usage:
    python3 scraper_draft.py --listing-url "https://www.jetpens.com/Beginner-Fountain-Pens/ct/1419"
    python3 scraper_draft.py --input workspace/jetpens-com/input_urls.json --sample
    python3 scraper_draft.py --urls https://www.jetpens.com/.../pd/21932 [--limit 5]
    python3 scraper_draft.py --query "fountain pen"          # search mode
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
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src.page_analysis import extract_jsonld, phase2_instant_fail  # noqa: E402
from src.http_fetch import create_fetch_page, create_fetch_text, SoftBlock  # noqa: E402
from src.discovery import discover_item_urls, config_for_load_more  # _DISCOVERY_IMPORT_APPLIED (enforced — do not remove)

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

SITE_NAME = "JetPens"
SITE_URL = "https://www.jetpens.com"
PLATFORM = "custom"
SITE_SLUG = "jetpens-com"
SITE_HOST = "www.jetpens.com"

OUTPUT_KEY = "products"
CONTENT_TYPE = "product"

# Promoted listing (navigation_analysis): the ct/1419 category found 20 pd/ items.
DEFAULT_LISTING_URL = "https://www.jetpens.com/Beginner-Fountain-Pens/ct/1419"
PDP_PATH_RE = re.compile(r"/pd/\d+")
PRODUCT_LISTING_URLS = [DEFAULT_LISTING_URL]

BROWSER_SERVICE_URL = os.environ.get("BROWSER_SERVICE_URL", "http://browser_service:8001")

# ACCESS RECIPE tiers: listing=cloak+datacenter (probe-measured), items=residential
# fingerprint rung (the ladder's top rung is curl_cffi impersonate=chrome).
ENV_PROXY_TIER = (os.environ.get("SCRAPER_PROXY_TIER") or "datacenter").strip().lower()
if ENV_PROXY_TIER not in ("none", "datacenter", "residential"):
    ENV_PROXY_TIER = "datacenter"
STEALTH = "cloak"
if (os.environ.get("STEALTH_BROWSER") or os.environ.get("SCRAPER_STEALTH") or "").strip().lower() in ("none", "false", "0"):
    STEALTH = "none"

MAX_PAGES = None            # unlimited — full extraction, never cap
# Measured PDP transport: curl_cffi fingerprint rung. NOTE: the shared ladder's
# min_tier is an INT index (0=none, 1=datacenter, 2=residential, 3=fingerprint)
# owned by the closure — the real value is resolved from fetch_page.tiers_total
# right after the factory is built below (Code Writer adapted, retry-1 fix).
PDP_MIN_TIER = 0
DELAY_BETWEEN_REQUESTS = 1.5
PHASE2_WORKERS = 4
DISCOVERY_DEADLINE_SECONDS = 1500
NAVIGATE_TIMEOUT = 120
SETTLE_MS = 6000

CURRENCY = "USD"

ITEM_LINK_SELECTOR = 'a[href*="/pd/"]'

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(SITE_SLUG)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_CHECKPOINT_PATH = os.path.join(SCRIPT_DIR, "discovered_urls_checkpoint.json")

_SOFT_404_PATTERNS = re.compile(
    r"(not\s*found|no\s*longer\s*available|discontinued|unavailable|"
    r"product\s*has\s*been\s*removed|page\s*cannot\s*be\s*found|"
    r"404\s*error|out\s*of\s*print)",
    re.I,
)


# ═══════════════════════════════════════════════════════════════════════════════
# NORMALIZERS
# ═══════════════════════════════════════════════════════════════════════════════

def _norm_price(value) -> Optional[float]:
    """'1,234.56' / '$17.00' / 24.99 → float; None when no digits present."""
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
    """Normalize to 'in_stock' / 'out_of_stock' (schema.org URI aware)."""
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
    if compact in ("in_stock", "instock", "available", "presale", "limitedavailability"):
        return "in_stock"
    if compact in ("out_of_stock", "outofstock", "unavailable", "sold_out", "soldout", "discontinued"):
        return "out_of_stock"
    return text


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 1 — LISTING FETCH (browser rung via browser_service /navigate, cloak+datacenter)
# ═══════════════════════════════════════════════════════════════════════════════

def _navigate(url, retry=0):
    """POST /navigate — the cloak+datacenter browser rung. Returns response dict or None."""
    payload = {
        "url": url,
        "actions": [],
        "extract": {},
        "stealth": "cloak" if STEALTH == "cloak" else "none",
        "proxy_tier": ENV_PROXY_TIER,
        "timeout": NAVIGATE_TIMEOUT,
        "return_what": "all",
        "settle_ms": SETTLE_MS,
    }
    endpoint = f"{BROWSER_SERVICE_URL}/navigate"
    for attempt in range(3):
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
                return {"success": False, "url": url, "html": "", "status_code": 404}
            if r.status_code in (429, 502, 503):
                retry_after = r.headers.get("Retry-After") or 5
                try:
                    retry_after = int(retry_after)
                except (TypeError, ValueError):
                    retry_after = 5
                logger.debug("navigate: %d on %s, backoff %ds", r.status_code, url[:60], retry_after)
                time.sleep(retry_after)
                continue
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            logger.debug("navigate: transient %s: %s", url[:60], exc)
        time.sleep(min(2.0 ** (attempt + retry), 30))
    logger.warning("navigate: exhausted retries on %s", url[:80])
    return None


def _extract_listing_links(html: str) -> list:
    """Phase 1 callback: ABSOLUTE item URLs (…/pd/<digits>) from a listing page."""
    if not html:
        return []
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as exc:
        logger.warning("Phase 1: HTML parse failed: %s", exc)
        return []
    links: list = []
    try:
        for a in soup.select(ITEM_LINK_SELECTOR):
            href = (a.get("href") or "").strip()
            if not href:
                continue
            url = _make_absolute(href)
            if not url or url in links:
                continue
            if PDP_PATH_RE.search(urlparse(url).path) and SITE_HOST in (urlparse(url).hostname or ""):
                links.append(url)
    except Exception as exc:
        logger.warning("Phase 1: selector failed: %s", exc)
    return links


def _make_absolute(href: str) -> str:
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


# Code Writer adapted (cycle-3 remediation, tester-prescribed): Phase 1 now
# rides the VERIFIED listing transport — curl_cffi impersonate=chrome over the
# residential proxy, reached as the shared ladder's top (fingerprint) rung via
# create_fetch_text. The cycle-2 browser rung (/navigate, cloak+datacenter)
# harvested 0 links from a 50+-item category on both attempts
# (stop_reason=empty_first_page); plain-HTTP rungs are TLS-scored to 403, so
# the fingerprint rung is the FIRST attempt, with the full ladder walk and the
# browser rung kept as bounded block-recovery rungs below.
LISTING_FETCH = create_fetch_text(delay_s=DELAY_BETWEEN_REQUESTS, headers={
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
})

try:
    LISTING_MIN_TIER = max(0, int(getattr(LISTING_FETCH, "tiers_total", 4)) - 1)
except (TypeError, ValueError):
    LISTING_MIN_TIER = 3


def _fetch_listing_html(url: str, min_tier: int) -> tuple:
    """One listing fetch over the shared ladder. Returns (html, status, block_reason)."""
    result = LISTING_FETCH(url, min_tier=min_tier)
    if _is_soft_block(result):
        return "", 0, getattr(result, "reason", "soft_block")
    if isinstance(result, tuple):
        text, status = (list(result) + ["", 0])[:2]
        return (text or ""), (status or 0), ""
    return "", 0, "no_response"


def _fetch_page_links_with_retry(url: str, seen: set) -> tuple:
    """Fetch one listing page; a 0-LINK body is a BLOCK signal, never a short page.

    Bounded recovery ladder for a linkless body: full HTTP tier walk
    (min_tier=0 → none→datacenter→residential), then the cloak+datacenter
    browser rung. Returns (fresh_urls, page_had_links).
    """
    html, status, block = _fetch_listing_html(url, LISTING_MIN_TIER)
    links = _extract_listing_links(html)
    if links:
        return [u for u in links if u not in seen], True
    logger.warning(
        "Phase 1: 0 links on %s (status=%s block=%s) — retrying via full HTTP ladder",
        url[:80], status, block or "none",
    )
    time.sleep(2.0)
    html, status, block = _fetch_listing_html(url, 0)
    links = _extract_listing_links(html)
    if links:
        return [u for u in links if u not in seen], True
    resp = _navigate(url)
    if resp and resp.get("success"):
        links = _extract_listing_links(resp.get("html", ""))
        if links:
            logger.info("Phase 1: browser rung recovered %d links on %s", len(links), url[:80])
            return [u for u in links if u not in seen], True
    logger.warning(
        "Phase 1: page still linkless after all rungs: %s (last status=%s)", url[:80], status
    )
    return [], False


def _discover_urls_via_category(
    category_url: str,
    max_pages: Optional[int] = None,
    limit: Optional[int] = None,
) -> tuple:
    """Phase 1: paginate a listing over chrome-TLS HTTP. Returns (urls, stop_reason)."""
    logger.info("Phase 1: Browsing listing → %s", category_url)
    deadline = time.time() + DISCOVERY_DEADLINE_SECONDS

    seen: set = set()
    fresh, had_links = _fetch_page_links_with_retry(category_url, seen)
    if not had_links:
        logger.error("Phase 1: every rung returned 0 item links for %s", category_url[:80])
        return [], "empty_first_page"
    all_urls = list(fresh)
    seen.update(fresh)
    logger.info("Phase 1: page 1 → %d links", len(all_urls))

    stop_reason = "no_next_link"
    current_page = 2
    while True:
        if max_pages and current_page > max_pages:
            stop_reason = "max_pages_hit"
            break
        if limit and len(all_urls) >= limit:
            stop_reason = "limit_hit"
            break
        if time.time() > deadline:
            logger.warning("Phase 1: deadline exceeded — stopping")
            stop_reason = "max_pages_hit"
            break

        next_url = _set_query_param(category_url, "page", current_page)
        fresh, had_links = _fetch_page_links_with_retry(next_url, seen)
        if not had_links or not fresh:
            # All rungs linkless, or links but nothing new → pagination exhausted.
            stop_reason = "no_new_items"
            logger.info("Phase 1: page %d yielded nothing new — stopping", current_page)
            break

        logger.info("Phase 1: page %d → %d new links (total %d)",
                    current_page, len(fresh), len(all_urls) + len(fresh))
        all_urls.extend(fresh)
        seen.update(fresh)
        current_page += 1
        if DELAY_BETWEEN_REQUESTS:
            time.sleep(DELAY_BETWEEN_REQUESTS)

    unique_urls = list(dict.fromkeys(all_urls))
    if limit:
        unique_urls = unique_urls[:limit]
    logger.info("Phase 1: %d total item URLs (%s)", len(unique_urls), stop_reason)
    return unique_urls, stop_reason


def _set_query_param(url: str, param: str, value) -> str:
    from urllib.parse import parse_qsl, urlencode, urlunparse
    p = urlparse(url)
    qs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k != param]
    qs.append((param, str(value)))
    return urlunparse(p._replace(query=urlencode(qs)))


def _run_discovery(listing_url: str, max_pages=None, limit=None):
    """Zero-yield self-heal: if the primary listing finds 0 URLs, retry the known-good one."""
    urls, stop_reason = _discover_urls_via_category(listing_url, max_pages, limit)
    if not urls and listing_url.rstrip("/") != DEFAULT_LISTING_URL.rstrip("/"):
        logger.warning("Phase 1: 0 URLs from %s — falling back to %s", listing_url[:80], DEFAULT_LISTING_URL)
        urls, stop_reason = _discover_urls_via_category(DEFAULT_LISTING_URL, max_pages, limit)
    # Code Writer adapted (cycle-3, tester Tier-1 note): an honest empty is
    # 'empty_first_page' — never launder it through the fallback as
    # 'no_next_link' (a clean stop), which reclassified a blocked first page.
    if not urls and stop_reason in ("no_next_link", "no_new_items"):
        stop_reason = "empty_first_page"
    return urls, stop_reason


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 2 — ITEM EXTRACTION (curl_cffi fingerprint rung via src.http_fetch ladder)
# ═══════════════════════════════════════════════════════════════════════════════

# Code Writer adapted (retry-1 fix): create_fetch_page() accepts ONLY
# (delay_s, headers) — it never had a min_tier kwarg (the previous call passed
# one and crashed the process at import). The per-fetch minimum rung belongs to
# the CLOSURE (fetch_page(url, min_tier=<int>)), whose int index pins the
# ladder slice: 0=none, 1=datacenter, 2=residential, 3=fingerprint.
fetch_page = create_fetch_page(delay_s=DELAY_BETWEEN_REQUESTS, headers={
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
})
# Resolve the fingerprint rung's INT index from the closure itself — never hardcode:
# the rung only exists when curl_cffi imported and the config kill-switch is off
# (then tiers_total = 1 none + 2 escalation + 1 fingerprint = 4). If the rung is
# absent (slim image), degrading to the top proxy tier keeps Phase 2 on the
# strongest available identity instead of crashing on an unmeasured one.
try:
    PDP_MIN_TIER = max(0, int(getattr(fetch_page, "tiers_total", 4)) - 1)
except (TypeError, ValueError):
    PDP_MIN_TIER = 3


def _populate_from_jsonld(item: dict, blocks: list) -> None:
    """Fill the user's five fields from the case-insensitive 'product' JSON-LD block."""
    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("@type", "")
        if isinstance(btype, list):
            btype = btype[0] if btype else ""
        if str(btype).strip().lower() != "product":
            continue

        item.setdefault("title", block.get("name") or "")
        item["description"] = re.sub(r"<[^>]+>", " ", block.get("description") or "").strip()

        offers = block.get("offers") or {}
        offers_list = offers if isinstance(offers, list) else [offers]
        best_price = None
        best_avail = None
        best_cur = None
        for offer in offers_list:
            if not isinstance(offer, dict):
                continue
            p = _norm_price(offer.get("price"))
            if p is not None and (best_price is None or p < best_price):
                best_price = p
                best_cur = offer.get("priceCurrency") or best_cur
                best_avail = offer.get("availability") or best_avail
            elif offer.get("availability") and not best_avail:
                best_avail = offer.get("availability")
        if best_price is not None:
            item["price"] = best_price
        if best_cur:
            item["currency"] = str(best_cur).strip().upper()
        if best_avail:
            item["availability"] = _norm_availability(best_avail)
        if "currency" not in item:
            item["currency"] = CURRENCY
        break


def _error_item(url: str, src_url: str, error: str) -> dict:
    return {
        "url": url,
        "src_url": src_url,
        "status_code": 0,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": f"Error: {error[:200]}",
    }


def _soft_404_check(item: dict, html: str, final_url: str) -> None:
    """Flag soft-404s: no Product JSON-LD, error text in title/h1, or off-target redirect."""
    if item.get("remarks"):
        return
    blocks = item.get("_jsonld_blocks") or []
    has_product = any(
        isinstance(b, dict) and str(b.get("@type", "")).strip().lower() == "product"
        for b in blocks
    )
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.get_text(strip=True) if soup.title else "")
    h1 = (soup.h1.get_text(strip=True) if soup.h1 else "")
    blob = f"{title} {h1}"
    if _SOFT_404_PATTERNS.search(blob) and not has_product:
        item["remarks"] = f"Soft 404: page signals unavailable ({blob[:120]})"
        for k in ("title", "price", "description", "availability"):
            item.pop(k, None)
        return
    req_path = urlparse(item["url"]).path.rstrip("/").lower()
    final_path = urlparse(final_url or item["url"]).path.rstrip("/").lower()
    if req_path != final_path and PDP_PATH_RE.search(final_path or ""):
        item["remarks"] = f"Soft 404: redirected to {final_url}"


def _extract_item(item_url: str, src_url: str) -> dict:
    """Phase 2: fetch one PDP via the fingerprint rung; extract the 5 user fields."""
    result = fetch_page(item_url, min_tier=PDP_MIN_TIER)
    if _is_soft_block(result):
        return _error_item(item_url, src_url, f"soft block: {getattr(result, 'reason', 'challenge')}")
    if isinstance(result, tuple):
        page, status = (result + (None,))[:2] if len(result) < 2 else result
    elif isinstance(result, str):
        page, status = result, 200
    else:
        page, status = None, 0
    # Code Writer adapted (retry-1 fix): the shared ladder returns
    # (BeautifulSoup, status_code), while every parser below (extract_jsonld's
    # regex, meta-tag search) needs the RAW HTML STRING — serialize a soup
    # instead of feeding the object into string parsers (which previously
    # raised TypeError inside extract_jsonld and zeroed every field).
    html = str(page) if isinstance(page, BeautifulSoup) else (page or "")
    if not html:
        return _error_item(item_url, src_url, f"fetch failed (status={status})")

    item: dict = {
        "url": item_url,
        "src_url": src_url,
        "status_code": status if status else 200,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": "",
    }

    try:
        blocks = extract_jsonld(html) or []
        item["_jsonld_blocks"] = blocks
        _populate_from_jsonld(item, blocks)
    except Exception as exc:
        logger.warning("Phase 2: JSON-LD failed on %s: %s", item_url[:60], exc)

    # CSS fallbacks for the required fields.
    if not item.get("title"):
        soup = BeautifulSoup(html, "html.parser")
        h1 = soup.select_one("h1")
        if h1:
            item["title"] = h1.get_text(" ", strip=True)
    if not item.get("description"):
        soup = BeautifulSoup(html, "html.parser")
        meta = soup.find("meta", attrs={"name": "description"})
        if meta:
            item["description"] = (meta.get("content") or "").strip()
    if not item.get("price"):
        soup = BeautifulSoup(html, "html.parser")
        meta = soup.find("meta", attrs={"property": "product:price:amount"})
        if meta:
            item["price"] = _norm_price(meta.get("content"))
            cur = soup.find("meta", attrs={"property": "product:price:currency"})
            if cur and "currency" not in item:
                item["currency"] = (cur.get("content") or "").strip().upper()
    if not item.get("availability"):
        soup = BeautifulSoup(html, "html.parser")
        blob = soup.get_text(" ", strip=True)[:6000]
        if re.search(r"\b(in stock|add to cart|buy now|available)\b", blob, re.I):
            item["availability"] = "in_stock"
        elif re.search(r"\b(out of stock|sold out|notify me|backorder)\b", blob, re.I):
            item["availability"] = "out_of_stock"
    if "currency" not in item:
        item["currency"] = CURRENCY

    _soft_404_check(item, html, item_url)
    item.pop("_jsonld_blocks", None)
    return item


def _is_soft_block(obj) -> bool:
    return SoftBlock is not None and isinstance(obj, SoftBlock)


def _extract_item_safe(item_url: str, src_url: str) -> dict:
    try:
        return _extract_item(item_url, src_url)
    except Exception as exc:
        logger.error("Phase 2: unexpected failure on %s: %s", item_url[:80], exc)
        return _error_item(item_url, src_url, str(exc))


# ═══════════════════════════════════════════════════════════════════════════════
# CHECKPOINT
# ═══════════════════════════════════════════════════════════════════════════════

def _write_checkpoint(urls: list) -> None:
    try:
        if not urls and os.path.isfile(_CHECKPOINT_PATH):
            try:
                with open(_CHECKPOINT_PATH) as f:
                    prev = json.load(f)
                if prev.get("urls"):
                    logger.warning("Checkpoint: keeping %d banked URLs", len(prev["urls"]))
                    return
            except Exception:
                pass
        with open(_CHECKPOINT_PATH, "w") as f:
            json.dump({"urls": list(urls), "count": len(urls), "ts": time.time()}, f)
    except Exception as exc:
        logger.warning("Checkpoint: write failed: %s", exc)


def _load_checkpoint() -> list:
    try:
        if os.path.isfile(_CHECKPOINT_PATH):
            with open(_CHECKPOINT_PATH) as f:
                data = json.load(f)
            urls = data.get("urls", [])
            if urls:
                logger.info("Checkpoint: RESUMING with %d URLs", len(urls))
                return urls
    except Exception as exc:
        logger.warning("Checkpoint: load failed: %s", exc)
    return []


def _load_seed_urls(path: str) -> list:
    # Code Writer adapted (cycle-3, MEDIUM seed_input fix): resolve a relative
    # --input against the SCRAPER'S directory first, then CWD — the tester
    # passes `--input input_urls.json` while CWD=/app, so a CWD-relative open
    # raised Errno 2 and seed mode silently degraded into discovery (which
    # then aborted with DISCOVERY_ZERO). Absolute paths pass through. If the
    # explicitly requested file exists NOWHERE, fail fast instead of
    # silently falling back.
    candidates = [path]
    if not os.path.isabs(path):
        candidates.append(os.path.join(SCRIPT_DIR, path))
        candidates.append(os.path.join(os.path.dirname(SCRIPT_DIR), path))
    resolved = next((c for c in candidates if os.path.isfile(c)), None)
    if resolved is None:
        raise FileNotFoundError(
            f"--input file not found (tried: {', '.join(os.path.abspath(c) for c in candidates)})"
        )
    logger.info("Seed file resolved: %s → %s", path, resolved)
    try:
        with open(resolved) as f:
            data = json.load(f)
    except Exception as exc:
        logger.error("Seed %s unreadable: %s", resolved, exc)
        raise
    if isinstance(data, dict):
        data = data.get("urls") or data.get("products") or []
    return [u for u in data if isinstance(u, str)]


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description=f"{SITE_NAME} HTTP Navigation Scraper")
    parser.add_argument("--query", type=str, help="Search query for navigation mode")
    parser.add_argument("--category-url", type=str, help="Category URL to crawl")
    parser.add_argument("--listing-url", type=str, help="Listing page URL to paginate")
    parser.add_argument("--input", type=str, help="Path to input URLs JSON file")
    parser.add_argument("--urls", type=str, nargs="+", help="Product URLs as CLI arguments")
    parser.add_argument("--sample", action="store_true", help="Scrape only 5 items")
    parser.add_argument("--limit", type=int, default=None, help="Max items to scrape")
    parser.add_argument("--no-proxy", action="store_true", help="Disable proxy for this run")
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--discover-only", action="store_true",
                        help="Run Phase 1 to exhaustion, emit discovery_coverage, skip Phase 2")
    parser.add_argument("--fresh-discovery", action="store_true",
                        help="Ignore any discovered_urls_checkpoint.json and run Phase 1 from scratch")
    args = parser.parse_args()

    global ENV_PROXY_TIER
    if args.no_proxy:
        ENV_PROXY_TIER = "none"

    _env_listing = os.environ.get("SCRAPER_LISTING_URL", "").strip()
    if _env_listing:
        args.listing_url = _env_listing
        args.fresh_discovery = True
        logger.info("Env gate: SCRAPER_LISTING_URL → --listing-url %s (fresh)", _env_listing[:80])

    limit = 5 if args.sample else args.limit
    start_time = time.time()
    discovered_urls: list = []
    ran_phase1 = True
    skipped_reason: Optional[str] = None
    aggregate_stop_reason = "no_next_link"

    # --input takes precedence over checkpoint: never let a stale file override seeds.
    # (cycle-3: an explicitly-requested --input that cannot be resolved is a HARD
    # error — FileNotFoundError from _load_seed_urls — never a silent fallback
    # into Phase 1 discovery, which is how run 2 died with DISCOVERY_ZERO.)
    seed_urls: list = []
    if args.input:
        seed_urls = _load_seed_urls(args.input)
        if not seed_urls:
            raise SystemExit(f"SEED_EMPTY: --input {args.input!r} resolved but contained no URLs")
        logger.info("Seed mode: %d URLs from %s", len(seed_urls), args.input)
    if args.urls:
        seed_urls = list(args.urls)
        logger.info("Seed mode: %d URLs from --urls", len(seed_urls))

    checkpoint_urls = [] if (args.fresh_discovery or seed_urls) else _load_checkpoint()
    if checkpoint_urls:
        discovered_urls = checkpoint_urls
        ran_phase1 = False
        skipped_reason = "checkpoint_loaded"
        aggregate_stop_reason = "skipped"

    if seed_urls and not checkpoint_urls:
        discovered_urls = seed_urls
        ran_phase1 = False
        skipped_reason = "seed_urls"
        aggregate_stop_reason = "skipped"

    if not discovered_urls:
        listing_url = args.listing_url or DEFAULT_LISTING_URL
        logger.info("Phase 1: discovering via listing %s", listing_url[:80])
        discovered_urls, aggregate_stop_reason = _run_discovery(listing_url, MAX_PAGES, limit)
        src_url_base = args.listing_url or DEFAULT_LISTING_URL
        _write_checkpoint(discovered_urls)
    else:
        src_url_base = args.listing_url or DEFAULT_LISTING_URL

    if not discovered_urls and aggregate_stop_reason in ("short_page", "no_next_link", "no_new_items"):
        aggregate_stop_reason = "empty_first_page"

    if not discovered_urls and not args.discover_only:
        logger.error("DISCOVERY_ZERO: no item URLs discovered under the given listing")
        print("DISCOVERY_ZERO: no item URLs discovered under the given listing", file=sys.stderr)
        sys.exit(3)

    # ── Phase 2: Extract concurrently via the fingerprint rung ──────────────
    total = len(discovered_urls)
    items: list = []
    phase2_instant = False
    if args.discover_only:
        logger.info("--discover-only: skipping Phase 2 (%d URLs, stop_reason=%s)", total, aggregate_stop_reason)
    elif discovered_urls:
        logger.info("Phase 2: Extracting %d items (%d workers, min_tier=%s)", total, PHASE2_WORKERS, PDP_MIN_TIER)
        phase2_start = time.monotonic()
        results: dict = {}
        with ThreadPoolExecutor(max_workers=PHASE2_WORKERS) as pool:
            futures = {pool.submit(_extract_item_safe, u, src_url_base): u for u in discovered_urls}
            for i, future in enumerate(as_completed(futures), 1):
                url = futures[future]
                try:
                    results[url] = future.result()
                except Exception as exc:
                    results[url] = _error_item(url, src_url_base, str(exc))
                if i % 10 == 0 or i == total:
                    logger.info("Progress: %d/%d items", i, total)
        items = [results[u] for u in discovered_urls if u in results]
        phase2_instant = phase2_instant_fail(
            time.monotonic() - phase2_start, total, 0.5, workers=PHASE2_WORKERS
        )

    # Output filter — the user schema is name/price/currency/description/availability.
    before = len(items)
    items = [it for it in items if it.get("title") and (it.get("price") is not None or it.get("availability"))]
    if len(items) != before:
        logger.info("Output filter: %d → %d items (dropped %d lacking required fields)", before, len(items), before - len(items))

    discovery_coverage = {
        "stop_reason": aggregate_stop_reason,
        "found": len(items),
        "discovered_urls": len(discovered_urls),
        "expected_total": None,
        "dimensions_iterated": 1,
        "dimensions_total": 1,
        "max_pages_hit": aggregate_stop_reason == "max_pages_hit",
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
            "stealth": STEALTH,
            "proxy_tier": ENV_PROXY_TIER,
            "discovery_coverage": discovery_coverage,
        },
    }

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S_%f")
    output_filename = os.path.join(SCRIPT_DIR, f"output_{timestamp}_{os.getpid()}.json")
    with open(output_filename, "w", encoding="utf-8") as f:
        # _OUTPUT_FILTER_APPLIED — drop non-item pages (content-type aware)
        _FILTER_FIELDS = ['product name', 'price', 'currency', 'description', 'avalibilty']
        try:
            _OUTPUT_KEY = OUTPUT_KEY if 'OUTPUT_KEY' in dir() else next(
                (k for k, v in output.items() if isinstance(v, list)
                 and v and isinstance(v[0], dict)), None)
            if _OUTPUT_KEY:
                _before = len(output[_OUTPUT_KEY])
                output[_OUTPUT_KEY] = [p for p in output[_OUTPUT_KEY] if p.get('product name') or p.get('price') or p.get('currency') or p.get('description') or p.get('avalibilty')]
                _after = len(output[_OUTPUT_KEY])
                if _before != _after:
                    logger.info('output filter: %d → %d items (removed %d without any of product name,price,currency,description,avalibilty)',
                                 _before, _after, _before - _after)
        except Exception:
            pass


        json.dump(output, f, indent=2, ensure_ascii=False, default=str)

    logger.info("Done: %d/%d items in %.1fs → %s", len(items), total, time.time() - start_time, output_filename)


if __name__ == "__main__":
    main()
