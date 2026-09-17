#!/usr/bin/env python3
"""
API Scraper — Rent The Runway storefront membership-tier API
(https://www.renttherunway.com/api/storefront/membershipTiers).

Adapted by Code Writer for Universal Ecommerce Scraper.

VERIFIED TRANSPORT (analyzer, live GETs — direct_http, no proxy, no auth):
  * The endpoint returns a BARE TOP-LEVEL JSON ARRAY (no envelope, no total).
  * Pagination params (limit/offset/page) are IGNORED by the server: a
    page-2-shaped request returns the identical full array. The complete
    dataset arrives in ONE GET; the draft still runs ONE verification probe
    against next-page params per run instead of assuming exhaustion.
  * The HTML PDP (Belle Midi Dress) is a client-side SPA and is intentionally
    NOT fetched: there is no per-record API. Phase 2 transforms the snapshot
    records in-process (the template's own fast path when discovery carries
    full records).

Usage:
    python3 scraper.py                    # seeded replay OR discovery
    python3 scraper.py --fresh-discovery  # Phase 1 via the storefront API
    python3 scraper.py --input urls.json  # explicit input file
    python3 scraper.py --urls url1 url2   # URLs as CLI arguments
    python3 scraper.py --sample           # 5 records only
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from src.proxy import ProxyConfig  # noqa: F401  (tier ladder lives in src.http_fetch now)
from src.discovery import discover_item_urls, config_for_load_more  # _DISCOVERY_IMPORT_APPLIED (enforced — do not remove)

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

SITE_NAME = "Rent The Runway"
SITE_URL = "https://www.renttherunway.com/shop/designers/amur/belle_midi_dress?availabilityScope=reserve"
PLATFORM = "renttherunway_storefront_api"
SCRAPING_METHOD = "internal_api"
SITE_SLUG = "renttherunway-com"
OUTPUT_KEY = "products"  # Output Contract: items live under output["products"]

API_BASE_URL = "https://www.renttherunway.com/api/storefront"
API_PRODUCTS_ENDPOINT = "/membershipTiers"
API_KEY = None
API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Content-Type": "application/json",
    # Origin + Referer matching the site (public API — no auth/cookies needed).
    "Origin": "https://www.renttherunway.com",
    "Referer": "https://www.renttherunway.com/",
}
if API_KEY:
    API_HEADERS["Authorization"] = f"Bearer {API_KEY}"

PAGINATION_TYPE = "offset"
PAGE_SIZE = 100  # server ignores paging params (verified) — one GET is the full set
DELAY_BETWEEN_REQUESTS = 1.0
MAX_RETRIES = 3

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Unique per process: a second-resolution timestamp collides when two runs
# of this draft start in the same second.
TIMESTAMP = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S_%f")
OUTPUT_FILE = os.path.join(SCRIPT_DIR, f"output_{TIMESTAMP}_{os.getpid()}.json")
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

proxy_config = ProxyConfig.get_instance()


# ═══════════════════════════════════════════════════════════════════════════════
# INPUT HANDLING
# ═══════════════════════════════════════════════════════════════════════════════

def load_urls_from_file(filepath: str) -> list[str]:
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("urls", [])


def save_urls_to_file(filepath: str, urls: list[str]) -> None:
    # [job-77] A 0-URL discovery must not DESTROY an existing seed file.
    if not urls and os.path.exists(filepath):
        logger.warning(f"Refusing to overwrite {filepath} with an empty URL list")
        return
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump({"urls": urls}, f, indent=2, ensure_ascii=False)
    logger.info(f"Saved {len(urls)} URLs to {filepath}")


# ═══════════════════════════════════════════════════════════════════════════════
# API FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_api(endpoint: str, params: Optional[dict] = None):
    """Phase-1 API fetch — rides the SAME shared ladder as Phase 2 [T1.3].

    Returns the parsed payload, a SoftBlock signal (challenge served as 200 —
    falsy on purpose), or None when every tier failed. Falls back to a bare
    GET only when the image predates the module.
    """
    url = f"{API_BASE_URL}{endpoint}"
    try:
        result = _get_fetch_json()(url, params=params)
        # Code Writer adapted: size-only soft-block signals get one
        # floor-disabled re-issue (this API's legit payload is known-small).
        return _soft_block_retry(result, url, params)
    except ImportError:
        logger.warning("src.http_fetch unavailable — falling back to a bare GET")
        try:
            response = requests.get(url, params=params, headers=API_HEADERS, timeout=15)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"API request failed: {e}")
            return None


# ── FIELD NORMALIZERS — inline on purpose, NOT a src import ──────────────────

def _norm_price(value) -> Optional[float]:
    """Strip currency tokens/symbols from a price. Returns a FLOAT or None.

    "USD 0.00" → 0.0 (a legitimate zero — tier 'Pause With Items' really is
    priced at 0.00), 24.99 → 24.99, "N/A" → None. Unparseable is EMPTY,
    never zero.
    """
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


# Code Writer adapted: currency lives as the LEADING TOKEN of basePrice
# ("USD 0.00") — there is no dedicated currency key in the response.
_CURRENCY_TOKEN_RE = re.compile(r"^([A-Za-z]{3})\s")


def _currency_from_price_token(raw_price) -> str:
    """'USD 0.00' → 'USD'. Always USD on all 9 live records; fall back to it."""
    if raw_price:
        match = _CURRENCY_TOKEN_RE.match(str(raw_price).strip())
        if match:
            return match.group(1).upper()
    return "USD"


def _as_records(data) -> list:
    """Code Writer adapted: normalize a payload to the record list.

    The live response body is a BARE TOP-LEVEL JSON ARRAY (no wrapper, no
    total) — the template's ``data.get(...)`` chain would AttributeError on
    it, so list payloads are handled FIRST, wrapper keys second.
    """
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for key in ("membershipTiers", "tiers", "data", "items", "products", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
    return []


def _as_id_str(value) -> str:
    """API record id → string, VERBATIM (fix cycle 1).

    The tester's HIGH finding: ids were fabricated as enumerate(index+1) while
    the live ids are non-contiguous [1,2,4,5,6,7,8,9,11] ('Pause With Items'
    is 11, not 3), which broke the documented dedupe key. A record id is data
    — never synthesized, only cast for type consistency. Missing → ''.
    """
    if value is None:
        return ""
    return str(value)


def transform_api_product(api_product: dict, index: int, src_url: str) -> dict:
    """Code Writer adapted: map a membershipTier record to the output fields.

    Field map (analyzer-verified on all 9 live records — record keys are
    type, id, membershipTierRevisionId, name, basePrice, baseSlotUpgradePrice,
    slotCount, monthlyShipmentLimit, inventoryEligibilities, shippingFee,
    enabledAt, createdAt, updatedAt):
      * membership_tier_id ← id VERBATIM (str-cast), the documented dedupe
        key — NEVER a list position (fix cycle 1, HIGH finding).
      * title         ← name (fallback: the type literal; NEVER the numeric id)
      * price         ← basePrice as a FLOAT via _norm_price; fallback to
                        baseSlotUpgradePrice ONLY when basePrice is missing —
                        tier 'Pause With Items' legitimately carries 0.00.
      * currency      ← leading token of basePrice ('USD …' → 'USD').
      * record_type / slot_count / monthly_shipment_limit /
        membership_tier_revision_id / enabled_at / created_at / updated_at
        ← copied from the record verbatim (fix cycle 1: the 10 optional
        api-mapped fields are now emitted, not dropped).
      * base_slot_upgrade_price / shipping_fee ← FLOATS via _norm_price
        (money fields are numbers: "USD 31.00" → 31.0, "USD 1.99" → 1.99).
      * inventory_eligibilities ← the LIVE JSON ARRAY as-is (analyzer
        correction: do not guess-decode); a JSON-encoded string form is
        tolerated via json.loads only when it actually parses.
      * original_price, brand, description, availability ← PROVEN ABSENT in
        the response → empty (no invented values).
      * url           ← caller-supplied site URL (overwritten by main() from
                        the seed list); src_url ← the API endpoint itself.
    ``index`` stays in the signature (template call sites pass it) but ids
    are never derived from it.
    """
    name = str(api_product.get("name") or "").strip()
    title = name or str(api_product.get("type") or "").strip()
    raw_price = api_product.get("basePrice")
    price = _norm_price(raw_price)
    if price is None:
        price = _norm_price(api_product.get("baseSlotUpgradePrice"))

    # inventoryEligibilities: the LIVE API returns a REAL JSON ARRAY on every
    # record; accept a JSON-encoded string without inventing a decode that
    # could mangle a plain (non-JSON) string value.
    eligibilities = api_product.get("inventoryEligibilities")
    if isinstance(eligibilities, str):
        text = eligibilities.strip()
        if text.startswith("[") or text.startswith("{"):
            try:
                eligibilities = json.loads(text)
            except ValueError:
                pass  # not JSON after all — keep the raw string
    if eligibilities is None:
        eligibilities = ""

    def _copy(key: str):
        value = api_product.get(key)
        return "" if value is None else value

    remarks = "Membership tier from storefront API; pagination params ignored by server"
    if not title and price is None:
        # Soft-404 analogue: a record with neither a name nor any price is
        # not usable product data — leave the fields empty and say why.
        remarks = "Soft 404: API record has neither name nor price"

    tier_id = _as_id_str(api_product.get("id"))
    return {
        # Fix cycle 1 (HIGH): the real API id — as membership_tier_id AND as
        # the generic id, so no fabricated 1..N index survives anywhere.
        "id": tier_id,
        "membership_tier_id": tier_id,
        "title": title,
        "price": price,
        "availability": "",  # no stock/availability field exists in the response
        "original_price": "",  # no compare-at/was price exists in the response
        "currency": _currency_from_price_token(raw_price),
        "description": "",
        "brand": "",
        # Fix cycle 1 (MEDIUM): the remaining mapped api fields, verbatim.
        "record_type": str(_copy("type")),
        "membership_tier_revision_id": str(_copy("membershipTierRevisionId")),
        "slot_count": _copy("slotCount"),
        "monthly_shipment_limit": _copy("monthlyShipmentLimit"),
        "base_slot_upgrade_price": _norm_price(api_product.get("baseSlotUpgradePrice")),
        "shipping_fee": _norm_price(api_product.get("shippingFee")),
        "inventory_eligibilities": eligibilities,
        "enabled_at": str(_copy("enabledAt")),
        "created_at": str(_copy("createdAt")),
        "updated_at": str(_copy("updatedAt")),
        "url": str(api_product.get("url") or SITE_URL),
        "src_url": src_url,
        "location": "",
        "status_code": 200,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "remarks": remarks,
    }


def transform_api_jobs(raw_jobs: list[dict], src_url: str = "") -> list[dict]:
    """JOB boards: map raw API items via the generic resolver (unused for
    this membership-tier dataset — kept for template parity)."""
    from src.job_fields import map_jobs

    if not raw_jobs:
        return []
    jobs = map_jobs(sample_items=raw_jobs, raw_items=raw_jobs)
    now = datetime.now(timezone.utc).isoformat()
    for idx, j in enumerate(jobs, start=1):
        j.setdefault("id", idx)
        if src_url:
            j.setdefault("src_url", src_url)
        j.setdefault("status_code", 200)
        j.setdefault("scraped_at", now)
        j.setdefault("remarks", "")
    return jobs


def fetch_all_products_via_api() -> tuple[list[str], list[dict]]:
    """Phase 1 — the storefront membership-tier API.

    ONE GET returns the complete dataset; the draft still VERIFIES rather
    than assumes: when a page comes back short of PAGE_SIZE, it probes the
    next page's params once and only concludes exhaustion when the probe
    yields zero NEW record ids.
    """
    all_products: list[dict] = []
    seen_ids: set[str] = set()
    offset = 0
    cursor = None
    page = 1

    # [T0.5] Why discovery stopped — emitted as metadata.discovery_coverage.
    global _DISCOVERY_META
    _DISCOVERY_META = {
        "stop_reason": "no_next_link",
        "ran_phase1": True,
        "skipped_reason": "",
        "discovered_urls": 0,
        "pages_fetched": 0,
        "soft_block_escalations": 0,
        "max_pages_hit": False,
    }

    while True:
        if PAGINATION_TYPE == "offset":
            params = {"limit": PAGE_SIZE, "offset": offset}
        elif PAGINATION_TYPE == "page":
            params = {"limit": PAGE_SIZE, "page": page}
        elif PAGINATION_TYPE == "cursor":
            params = {"limit": PAGE_SIZE, "cursor": cursor} if cursor else {"limit": PAGE_SIZE}
        else:
            params = {"limit": PAGE_SIZE}

        logger.info(f"Fetching API page {page}: {API_PRODUCTS_ENDPOINT} {params}")
        result = fetch_api(API_PRODUCTS_ENDPOINT, params=params)

        if isinstance(result, SoftBlock):
            logger.error(
                f"Discovery page {page}: challenge served as 200 "
                f"({getattr(result, 'reason', '?')}) — every ladder tier "
                f"returned a challenge"
            )
            _DISCOVERY_META["soft_block_escalations"] += 1
            if page == 1:
                _DISCOVERY_META["stop_reason"] = "empty_first_page"
            break
        if not result:
            logger.error(f"Discovery page {page}: every ladder tier failed")
            if page == 1:
                _DISCOVERY_META["stop_reason"] = "navigate_error"
            break

        data = result[0]
        _DISCOVERY_META["pages_fetched"] = page

        # Code Writer adapted: bare-array body FIRST (this API's real shape),
        # wrapper-object keys second — the template's .get() chain would
        # AttributeError on a list payload.
        products = _as_records(data)
        if not products:
            _DISCOVERY_META["stop_reason"] = "no_next_link"
            break

        new_products = [p for p in products if str(p.get("id")) not in seen_ids]
        for raw in new_products:
            seen_ids.add(str(raw.get("id")))
            all_products.append(raw)

        logger.info(
            f"Page {page}: {len(products)} records "
            f"({len(new_products)} new, total: {len(all_products)})"
        )

        if len(products) < PAGE_SIZE:
            # Code Writer adapted [verify, don't assume]: the analyzer verdict
            # says this server IGNORES paging params. Probe the next page once;
            # only conclude the dataset is complete when it yields zero NEW ids.
            probe_params = dict(params)
            if PAGINATION_TYPE == "offset":
                probe_params["offset"] = offset + PAGE_SIZE
            elif PAGINATION_TYPE == "page":
                probe_params["page"] = page + 1
            probe = fetch_api(API_PRODUCTS_ENDPOINT, params=probe_params)
            probe_records = (
                _as_records(probe[0])
                if probe and not isinstance(probe, SoftBlock)
                else []
            )
            probe_new = [p for p in probe_records if str(p.get("id")) not in seen_ids]
            if probe_new:
                logger.info(
                    f"Probe page {page + 1}: {len(probe_new)} NEW records — "
                    f"pagination IS honored, continuing"
                )
                for raw in probe_new:
                    seen_ids.add(str(raw.get("id")))
                    all_products.append(raw)
                if PAGINATION_TYPE == "offset":
                    offset += PAGE_SIZE
                elif PAGINATION_TYPE == "cursor":
                    cursor = data.get("next_cursor") or data.get("cursor")
                    if not cursor:
                        break
                page += 1
                continue
            logger.info(
                f"Probe page {page + 1}: no new records — server ignores "
                f"pagination params; dataset complete ({len(all_products)} tiers)"
            )
            _DISCOVERY_META["stop_reason"] = "no_pagination_params_ignored"
            break

        total = (
            data.get("totalCount") or data.get("total_count") or data.get("count")
            or data.get("total") or data.get("totalResults") or data.get("totalJobs")
        ) if isinstance(data, dict) else None
        if isinstance(total, int) and total > 0 and len(all_products) >= total:
            break

        if PAGINATION_TYPE == "cursor":
            cursor = data.get("next_cursor") or data.get("cursor")
            if not cursor:
                break
        elif PAGINATION_TYPE == "offset":
            offset += PAGE_SIZE
        page += 1

    # Code Writer adapted: membership-tier records carry NO url/handle key
    # (verified record keys). The FULL raw records are returned alongside so
    # Phase 2 transforms in-process (no per-item fetch exists to make); the
    # item "URLs" are the caller-supplied site URL, one entry per record,
    # keeping the seed file one-entry-per-record for replay runs.
    urls = [SITE_URL for _ in all_products]
    _DISCOVERY_META["discovered_urls"] = len(urls)
    return urls, all_products


# [wave-15 3.4] Per-item fetch closure — ONE per process (the Session must
# persist across items for cookie continuity, job-58).
_FETCH_JSON = None

# [T0.5] Phase-1 discovery outcome, emitted as metadata.discovery_coverage.
_DISCOVERY_META: dict = {
    "stop_reason": "skipped",
    "ran_phase1": False,
    "skipped_reason": "seeded_input",
    "discovered_urls": 0,
    "pages_fetched": 0,
    "soft_block_escalations": 0,
    "max_pages_hit": False,
}

# Code Writer adapted: process-cached snapshot of the ONE GET that IS the
# dataset (there is no per-record endpoint to hit per seed URL).
_TIER_SNAPSHOT: Optional[list] = None

try:  # guarded: the browser-service image may predate src.http_fetch
    from src.http_fetch import SoftBlock, SOFT_BLOCK_MIN_BYTES_ENV  # noqa: E402
except ImportError:  # pragma: no cover
    class SoftBlock:  # type: ignore[no-redef]
        """Image predates the shared module — no signal can ever occur."""

        def __init__(self, *a, **k):
            self.reason = "unavailable"

        def __bool__(self):
            return False

    SOFT_BLOCK_MIN_BYTES_ENV = "SCRAPER_SOFT_BLOCK_MIN_BYTES"


def _soft_block_retry(result, url: str, params: Optional[dict] = None):
    """Code Writer adapted: re-issue ONCE when the challenge-shape detector
    fired only on BODY SIZE (``under_min_bytes``).

    Why: the generic min-bytes floor is calibrated for HTML LISTING bodies,
    but this API's complete, legitimate payload is a BARE JSON ARRAY of 9
    membership tiers ≈ 3.3KB (measured live: 200 + 3,320-byte valid JSON on
    the direct rung; analyzer verdict: no anti-bot, proxy tier none). The
    armed floor therefore misclassifies the site's ENTIRE real response as a
    challenge stub and every tier would "fail" identically. The retry is
    self-validating: it rides the SAME shared closure (same session, same
    ladder), and any real challenge page is HTML — ``fetch_json`` rejects a
    non-JSON 200 body as None, so a genuine wall can never become data.
    Strong challenge MARKERS (reason ``challenge_marker``) are honored as-is.
    """
    if not isinstance(result, SoftBlock):
        return result
    if getattr(result, "reason", "") != "under_min_bytes":
        return result  # a real challenge marker — honor the signal
    previous = os.environ.get(SOFT_BLOCK_MIN_BYTES_ENV, "0")
    logger.warning(
        "SOFT BLOCK was body-size-only (%s bytes) on a JSON API whose legit "
        "payload is known-small — re-issuing once with the generic HTML "
        "floor disabled (self-validating: non-JSON still fails)",
        getattr(result, "body_bytes", "?"),
    )
    os.environ[SOFT_BLOCK_MIN_BYTES_ENV] = "0"
    try:
        return _get_fetch_json()(url, params=params)
    finally:
        os.environ[SOFT_BLOCK_MIN_BYTES_ENV] = previous


def _get_fetch_json():
    global _FETCH_JSON
    if _FETCH_JSON is None:
        from src.http_fetch import create_fetch_json

        _FETCH_JSON = create_fetch_json(
            delay_s=DELAY_BETWEEN_REQUESTS, headers=API_HEADERS
        )
    return _FETCH_JSON


def _fetch_membership_tiers() -> list:
    """Code Writer adapted: ONE cached GET of membershipTiers (the dataset).

    Rides the SAME shared proxy ladder as Phase 1 (its first rung IS direct —
    this site is verified reachable with no proxy). A 200 body that does not
    parse as JSON is a failed fetch, never an exception.
    """
    global _TIER_SNAPSHOT
    if _TIER_SNAPSHOT is not None:
        return _TIER_SNAPSHOT
    url = f"{API_BASE_URL}{API_PRODUCTS_ENDPOINT}"
    try:
        result = _get_fetch_json()(url)
        # Code Writer adapted: size-only soft-block signals get one
        # floor-disabled re-issue (this API's legit payload is known-small).
        result = _soft_block_retry(result, url)
    except ImportError:
        logger.warning("src.http_fetch unavailable — falling back to a bare GET")
        try:
            response = requests.get(url, headers=API_HEADERS, timeout=15)
            response.raise_for_status()
            result = (response.json(), response.status_code)
        except Exception as e:
            logger.error(f"Failed to scrape {url}: {e}")
            result = None
    if not result or isinstance(result, SoftBlock):
        logger.error(
            "membershipTiers: proxy ladder returned no JSON / challenge — "
            "seeded replay cannot extract"
        )
        _TIER_SNAPSHOT = []
        return _TIER_SNAPSHOT
    data, _status = result
    _TIER_SNAPSHOT = _as_records(data)
    logger.info(
        f"membershipTiers snapshot: {len(_TIER_SNAPSHOT)} records "
        f"(HTTP {_status}, bare-array shape: {isinstance(data, list)})"
    )
    return _TIER_SNAPSHOT


def scrape_product(url: str, src_url: str) -> Optional[dict]:
    """Phase-2 per-URL fetch — KEPT signature; body adapted to this API.

    There is NO per-record endpoint: the single GET of membershipTiers is the
    complete dataset and the SPA PDP serves no per-URL JSON. A seed URL is
    resolved through the cached snapshot; a synthesized
    ``#membership-tier-<id>`` fragment selects the exact record.
    """
    try:
        records = _fetch_membership_tiers()
        if not records:
            return None
        match = re.search(r"#membership-tier-([^#?/]+)", url or "")
        if match:
            for raw in records:
                if str(raw.get("id")) == match.group(1):
                    return transform_api_product(raw, 0, src_url)
        return transform_api_product(records[0], 0, src_url)
    except Exception as e:
        logger.error(f"Failed to scrape {url}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

# ── CLI CONTRACT — keep every line below when adapting ───────────────────────
# Declared flags are never stripped at launch; an UNDECLARED one is. Keep:
#   --fresh-discovery  the ONLY discovery trigger for the api family (execution
#                      always passes it) — there is NO listing page; discovery
#                      IS the API. SCRAPER_FORCE_DISCOVERY is the env gate.
#   --input/--sample/--limit  testing
#   --discover-only / --listing-url  list_page contract (this job)
# ──────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description=f"API scraper for {SITE_NAME}")
    parser.add_argument("--sample", action="store_true", help="Scrape only 5 products")
    parser.add_argument("--limit", type=int, default=None, help="Max products to scrape")
    parser.add_argument("--input", type=str, default=None, help="Path to input URLs JSON file")
    parser.add_argument("--urls", nargs="+", default=None, help="Product URLs as arguments")
    parser.add_argument("--fresh-discovery", action="store_true",
                        help="Bypass input_urls.json and rediscover via API pagination")
    parser.add_argument("--discover-only", action="store_true",
                        help="Run Phase 1 discovery, save the seed file, skip Phase 2")
    parser.add_argument("--listing-url", type=str, default=None,
                        help="Promoted listing URL (list_page contract). Discovery for "
                             "this api-family job IS the storefront API regardless.")
    parser.add_argument("--no-proxy", action="store_true",
                        help="Pin direct egress (site verified reachable with no proxy)")
    args = parser.parse_args()

    if args.input:
        # Relative seed-file values resolve against the scraper's own directory.
        args.input = os.path.join(SCRIPT_DIR, args.input)

    if args.no_proxy:
        # Task contract: direct HTTP measured 200 on this site (proxy tier
        # none). The shared ladder's first rung IS direct; this also neuters
        # any configured escalation tier so a --no-proxy run can never egress
        # through a proxy. The curl_cffi fingerprint rung then runs DIRECT
        # with browser TLS = the measured fingerprint_chrome_none transport.
        try:
            proxy_config.get_proxy_dict = lambda tier: None  # type: ignore[method-assign]
            logger.info("--no-proxy: direct egress pinned (escalation tiers disabled)")
        except Exception as exc:
            logger.warning(f"--no-proxy could not pin direct egress: {exc}")

    start_time = time.time()

    logger.info("=" * 80)
    logger.info(f"Starting API scraper for {SITE_NAME}")
    logger.info(f"Site: {SITE_URL}")
    logger.info(f"API: {API_BASE_URL}{API_PRODUCTS_ENDPOINT}")
    logger.info(f"Output: {OUTPUT_FILE}")
    logger.info("=" * 80)

    product_urls = []
    raw_products = []
    src_url = f"{API_BASE_URL}{API_PRODUCTS_ENDPOINT}"

    # F6: run_execution always passes --fresh-discovery for nav/api jobs.
    # SCRAPER_LISTING_URL is the env gate required by the list_page contract,
    # read BEFORE the seed-file gate; for this api-family job discovery IS
    # the storefront API (the listing page is a client-side SPA — never
    # fetched), so the env var triggers API discovery rather than a DOM crawl.
    _env_listing = os.environ.get("SCRAPER_LISTING_URL", "").strip()
    _force_fresh = args.fresh_discovery or bool(
        os.environ.get("SCRAPER_FORCE_DISCOVERY", "").strip()
    )
    _wants_discovery = _force_fresh or args.discover_only or _env_listing or args.listing_url
    if _wants_discovery:
        trigger = (
            "flag" if args.fresh_discovery
            else "--discover-only" if args.discover_only
            else "--listing-url" if args.listing_url
            else "env SCRAPER_LISTING_URL"
        )
        logger.info(f"Fresh discovery ({trigger}): fetching the storefront membership-tier API")
        product_urls, raw_products = fetch_all_products_via_api()
        save_urls_to_file(INPUT_FILE, product_urls)
    elif args.urls:
        product_urls = args.urls
    elif args.input:
        product_urls = load_urls_from_file(args.input)
    elif os.path.exists(INPUT_FILE):
        # Seeded replay (retry-all): the seed only supplies caller URLs —
        # Phase 2 still extracts the FULL dataset from the API snapshot, so a
        # no-flag run can never ship fewer records than the source exposes.
        product_urls = load_urls_from_file(INPUT_FILE)
    else:
        logger.info("No seed file found — defaulting to Phase 1 API discovery (full extraction)")
        product_urls, raw_products = fetch_all_products_via_api()
        save_urls_to_file(INPUT_FILE, product_urls)

    # [T0.5] Raw pre-cut count — --sample/--limit truncate product_urls below.
    discovered_urls_raw = len(product_urls)

    if args.sample:
        product_urls = product_urls[:5]
    if args.limit:
        product_urls = product_urls[: args.limit]

    logger.info(f"Total products to scrape: {len(product_urls)}")

    results = []
    failed = 0

    if raw_products and len(raw_products) == len(product_urls):
        # Discovery already carried the FULL records — transform in-process
        # (this API has no per-item fetch to repeat).
        for i, raw in enumerate(raw_products[: len(product_urls)]):
            try:
                product = transform_api_product(raw, i + 1, src_url)
                # Code Writer adapted: field map anchors `url` to the
                # caller-supplied URL (seed entry), not an API field.
                if i < len(product_urls):
                    product["url"] = str(product_urls[i]).split("#", 1)[0]
                results.append(product)
            except Exception as e:
                logger.error(f"Error transforming product {i + 1}: {e}")
                failed += 1
    else:
        # Code Writer adapted: seeded replay. There is no per-item endpoint —
        # resolve the ONE cached snapshot and emit ONE RECORD PER TIER,
        # pairing record i with caller URL i (field map: url = caller-supplied
        # input URL). A 1-URL seed therefore still yields the complete dataset.
        snapshot = list(_fetch_membership_tiers())
        cap = 5 if args.sample else args.limit
        if cap:
            snapshot = snapshot[:cap]
        if not snapshot:
            failed += 1
        for idx, raw in enumerate(snapshot):
            try:
                caller = (
                    product_urls[idx] if idx < len(product_urls)
                    else (product_urls[0] if product_urls else SITE_URL)
                )
                product = transform_api_product(raw, idx + 1, src_url)
                product["url"] = str(caller).split("#", 1)[0]
                results.append(product)
                if (idx + 1) % 25 == 0:
                    percent = ((idx + 1) / len(snapshot)) * 100
                    logger.info(f"Progress: [{idx + 1}/{len(snapshot)}] ({percent:.1f}%)")
            except Exception as e:
                logger.error(f"Error transforming tier {idx + 1}: {e}")
                failed += 1

    # Output contract: keep records carrying a title AND at least one core
    # field (price here — 0.00 is a real value and is not None).
    before = len(results)
    results = [
        r for r in results
        if str(r.get("title") or "").strip() and r.get("price") is not None
    ]
    if before != len(results):
        logger.warning(f"Dropped {before - len(results)} record(s) without title+price")

    if args.discover_only:
        logger.info(
            f"--discover-only: Phase 1 complete ({discovered_urls_raw} URLs) — "
            f"skipping Phase 2 extraction"
        )
        results = []

    output = {
        "site": {
            "name": SITE_NAME,
            "url": SITE_URL,
            "platform": PLATFORM,
            "scraping_method": SCRAPING_METHOD,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        },
        "products": results,
        "metadata": {
            "phase": "discovery" if args.discover_only else "extraction",
            "scraping_duration_seconds": round(time.time() - start_time, 2),
            "failed_products": failed,
            "rate_limit_delay": DELAY_BETWEEN_REQUESTS,
            "discovered_urls": discovered_urls_raw,
            # Full-catalogue coverage evidence (HARD RULE): pages fetched and
            # unique records discovered before any --sample/--limit cut.
            "pages_fetched": _DISCOVERY_META.get("pages_fetched", 0),
            "total_discovered": _DISCOVERY_META.get("discovered_urls", discovered_urls_raw),
            "discovery_coverage": {
                **_DISCOVERY_META,
                "found": len(results),
            },
        },
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        # _OUTPUT_FILTER_APPLIED — drop non-item pages (content-type aware)
        _FILTER_FIELDS = ['price', 'availability', 'currency']
        try:
            _OUTPUT_KEY = OUTPUT_KEY if 'OUTPUT_KEY' in dir() else next(
                (k for k, v in output.items() if isinstance(v, list)
                 and v and isinstance(v[0], dict)), None)
            if _OUTPUT_KEY:
                _before = len(output[_OUTPUT_KEY])
                output[_OUTPUT_KEY] = [p for p in output[_OUTPUT_KEY] if p.get('title') and (p.get('price') or p.get('availability') or p.get('currency'))]
                _after = len(output[_OUTPUT_KEY])
                if _before != _after:
                    logger.info('output filter: %d → %d items (removed %d without title+price,availability,currency)',
                                 _before, _after, _before - _after)
        except Exception:
            pass


        json.dump(output, f, indent=2, ensure_ascii=False)

    logger.info("=" * 80)
    logger.info("EXTRACTION COMPLETE")
    logger.info(f"Total: {len(results)}, Failed: {failed}")
    logger.info(f"Duration: {round(time.time() - start_time, 2)}s")
    logger.info(f"Output: {OUTPUT_FILE}")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
