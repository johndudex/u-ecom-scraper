"""Isolated prototype: a bounded, LLM-pruned graph-traversal navigator.

This is a STANDALONE proof-of-concept — it is NOT wired into the scrape graph and
does NOT modify any existing agent. It implements the "do what a human does" model:

    start at the homepage → move toward the jobs → fire the search → look at what
    actually comes back → record the simplest replayable path.

Navigation is modeled as a bounded BFS over the site. At each page we extract
candidate next-steps (same-domain links + a search-form "submit" action), an LLM
**prunes** the ones that are off-goal (marketing / pay / about / login …), and a
**deterministic** goal-check decides when we've actually reached a job-listing
page (a captured backend API, an embedded JSON record-array, ≥N job links, or
results items). The LLM only ever prunes; it never decides that a real listing is
"not a listing", so a wrong prune can't make us miss the goal.

The only existing-code reuse is ``url_judge.judge_candidate_urls`` (the per-step
LLM judge) — imported read-only. Everything else (detectors) is re-implemented
here in small, dependency-light functions so the prototype is self-contained and
unit-testable with the LLM + HTTP mocked.

See ``docs/navigation-traversal-design.md`` for the full design + critique.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from src.pagination_patterns import PATTERNS

logger = logging.getLogger(__name__)

BROWSER_SERVICE_URL = os.environ.get("BROWSER_SERVICE_URL", "http://browser_service:8001")

# set by browser_traverse() so _do_action's domain guard can check goto targets
_START_URL: str = ""

# populated by _do_action when select_form runs, so browser_traverse can capture
# the form replay info into TraversalResult (so code_writer can replay the form)
_last_form_replay: dict = {}

# ─── Bounds (mandatory — without them this regresses to a slow crawl) ───────
MAX_DEPTH = 3
MAX_VISITS = 12
MAX_JUDGE_CALLS = 4          # one batched judge per node, capped
DEFAULT_TIMEOUT = 20.0
RESULTS_GOAL_THRESHOLD = 5   # ≥ this many job/result signals = "this is a listing"

# ─── Detectors (small, standalone) ──────────────────────────────────────────

# href patterns that identify a JOB (or item) detail link on a listing page.
JOB_HREF_PATTERNS = [
    r"/job[-_/]",
    r"/jobs?/[a-z0-9_-]*\d{3,}",
    r"-job-\d+",
    r"/job-\d+",
    r"/position/",
    r"/careers?/(?:job|posting)",
    r"/posting/",
    r"/vacanc",
]
JOB_HREF_RES = [re.compile(p, re.I) for p in JOB_HREF_PATTERNS]

# CSS-ish heuristics for a results/listing container (class substrings, lowercased;
# matched case-insensitively so 'ProductTile' and 'product-tile' both count).
RESULTS_CONTAINER_HINTS = [
    "job-results", "job-result", "job-card", "job-listing", "jobs-list",
    "search-result", "results-item", "listing-item", "product-card",
    "producttile", "product-tile", "productcard", "tile-product",
    "product-item", "plp-card", "grid-item",
]

# anchor text/href tokens that mean "not a listing candidate" (pruned before LLM).
NON_CATEGORY_TOKENS = [
    "login", "signin", "sign-in", "register", "signup", "account", "cart",
    "checkout", "about", "contact", "privacy", "terms", "policy", "cookie",
    "facebook", "twitter", "linkedin", "instagram", "youtube", "tiktok",
    "mailto:", "tel:", "javascript:", "#",
]

# inline-script URL substrings that hint at a backend JSON API.
API_URL_CANDIDATE_RE = re.compile(
    r"https?://[^\s\"'`<>\\]{6,}", re.I
)
API_HINT_TOKENS = ("/search", "/jobs", "/api/", "job/search", "jobsearch",
                   "/listings", "/feed", ".json", "graphql")


def _norm_host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().lstrip("www.")
    except Exception:
        return ""


def _same_site(a: str, b: str) -> bool:
    ha, hb = _norm_host(a), _norm_host(b)
    # allow subdomains (api.x.com vs www.x.com): compare the registrable tail.
    if not ha or not hb:
        return False
    tail = lambda h: ".".join(h.split(".")[-2:]) if h.count(".") >= 2 else h
    return tail(ha) == tail(hb) or ha.endswith(hb) or hb.endswith(ha)


def _registrable(url: str) -> str:
    """Best-effort registrable domain (ayahealthcare.com, calvinklein.co.uk)."""
    host = _norm_host(url)
    if not host:
        return ""
    # handle common two-part TLDs (.co.uk, .com.au, …) — naive last-2 gives "co.uk"
    for tld in _TWO_PART_TLDS:
        if host.endswith(tld):
            pre = host[: -len(tld)].rstrip(".")
            label = pre.split(".")[-1]
            return f"{label}{tld}" if label else host
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


_TWO_PART_TLDS = (
    ".co.uk", ".org.uk", ".ac.uk", ".gov.uk", ".me.uk",
    ".com.au", ".net.au", ".org.au", ".co.nz", ".co.jp", ".co.kr", ".co.za",
    ".com.br", ".com.cn", ".com.hk", ".com.sg", ".com.tw", ".com.mx",
)


def _is_nav_or_junk(href: str, text: str) -> bool:
    blob = f"{href} {text}".lower()
    return any(tok in blob for tok in NON_CATEGORY_TOKENS)


def extract_links(html: str, base_url: str, limit: int = 60) -> list[dict]:
    """Same-domain, non-junk anchor links as [{href, text}]."""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absu = urljoin(base_url, href)
        if not absu.startswith(("http://", "https://")):
            continue
        # NOTE: cross-domain links are intentionally ALLOWED. Many sites host their
        # data on a different registrable domain — Workday portals (myworkdaysites.com),
        # Algolia (algolia.net), headless backends — and the same-site filter dropped
        # the only relevant link before the LLM ever saw it (kirkland dead-ended here).
        # The NON_CATEGORY_TOKENS junk filter below removes login/social/about noise;
        # the url_judge LLM then decides relevance from content_type + query + text.
        text = (a.get_text(" ") or "").strip()[:80]
        if _is_nav_or_junk(absu, text):
            continue
        key = absu.split("#")[0].rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        out.append({"href": absu, "text": text})
        if len(out) >= limit:
            break
    return out


def _keyword_links(html: str, base_url: str, query: str, limit: int = 12) -> list[dict]:
    """Links whose ANCHOR TEXT contains a query token — high-precision auto-keeps.

    Matching on text (not href) avoids false positives: aya's marketing page is at
    ``/travel-nursing/`` but its anchor text is just "Travel" (no "nursing"), so it
    is NOT auto-kept and the LLM still prunes it. calvklein's "Watches & Jewellery"
    text contains "watches" → auto-kept (the LLM was over-pruning the noisy
    mega-menu and dropping even this). These bypass the cap AND the LLM prune.
    """
    q = (query or "").lower()
    if not q:
        return []
    tokens = [t for t in re.split(r"[^a-z0-9]+", q) if len(t) >= 4]
    if not tokens:
        tokens = [q]
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absu = urljoin(base_url, href)
        if not absu.startswith(("http://", "https://")) or not _same_site(absu, base_url):
            continue
        text = (a.get_text(" ") or "").strip()
        if not any(tok in text.lower() for tok in tokens):  # TEXT match only
            continue
        if _is_nav_or_junk(absu, text):
            continue
        key = absu.split("#")[0].rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        out.append({"href": absu, "text": text[:80]})
        if len(out) >= limit:
            break
    return out


@dataclass
class SearchForm:
    action: str
    method: str  # "get" | "post"
    # the field(s) to fill with the query (text search) — may be empty
    text_field: str | None
    # required-ish <select>s we must give a value (name -> first option value)
    selects: dict[str, str]
    is_search: bool


def detect_search_form(html: str, base_url: str) -> SearchForm | None:
    """Find a search form (keyword box OR a multi-select job-search form).

    Returns the form's action/method + how to fill it (query text field, and the
    first valid <option> for each <select>). This is how locumtenens' QuickSearch
    POST becomes a first-class traversal edge.
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return None
    best: SearchForm | None = None
    best_score = 0
    for form in soup.find_all("form"):
        action = urljoin(base_url, (form.get("action") or base_url).strip())
        method = (form.get("method") or "get").lower()
        text_field = None
        selects: dict[str, str] = {}
        # text search input?
        for inp in form.find_all(["input", "textarea"]):
            name = (inp.get("name") or "").lower()
            ph = (inp.get("placeholder") or "").lower()
            typ = (inp.get("type") or "").lower()
            if typ in ("hidden", "submit", "button", "checkbox", "radio"):
                continue
            if name and any(k in name for k in ("search", "q", "keyword", "query", "term")) \
               or any(k in ph for k in ("search", "keyword", "job")):
                text_field = inp.get("name")
        # selects (discipline/specialty/location/jobtype)
        for sel in form.find_all("select"):
            name = (sel.get("name") or "").strip()
            if not name:
                continue
            opts = []
            for o in sel.find_all("option"):
                v = (o.get("value") or "").strip()
                t = (o.get_text() or "").strip()
                if v and t and t.lower() not in ("any", "all", "select...", "please select", ""):
                    opts.append(v)
            if opts:
                selects[name] = opts[0]
        score = (1 if text_field else 0) + (2 if len(selects) >= 2 else 1 if selects else 0)
        # require *some* search signal
        is_search = bool(text_field) or (
            len(selects) >= 2 and any(
                k.lower() in ("specialties", "specialty", "disciplines", "discipline",
                              "profession", "jobtype", "job_type", "locations", "location",
                              "category", "state")
                for k in selects
            )
        )
        if is_search and score > best_score:
            best_score = score
            best = SearchForm(action, method, text_field, selects, True)
    return best


def _form_payload(form: SearchForm, query: str) -> dict:
    """Build the minimal field set to submit a search form.

    BROADEST results first: if there's a text field, send ONLY the text query —
    no specialty/category select (those narrow results to one discipline, e.g.
    locumtenens returned 25 Abdominal Radiology jobs instead of 70+ physician
    jobs when a specific Specialty was picked). Only add a select if there's no
    text field, and prefer a blank/all option to keep results broad.
    """
    data: dict[str, str] = {}
    if form.text_field:
        data[form.text_field] = query
        return data  # text field alone = broadest search; no select narrowing
    # No text field — pick the most specific select to trigger a search.
    # Prefer a blank/all option (broadest); fall back to the first specific option.
    priority = ("specialties", "specialty", "profession", "disciplines",
                "discipline", "category", "jobtype", "job_type", "role")
    chosen = None
    for key in priority:
        for name in form.selects:
            if key in name.lower():
                chosen = name
                break
        if chosen:
            break
    if not chosen and form.selects:
        chosen = next(iter(form.selects))
    if chosen:
        data[chosen] = form.selects[chosen]
    return data


def _inline_scripts(html: str) -> list[str]:
    try:
        soup = BeautifulSoup(html, "html.parser")
        return [s.get_text() or "" for s in soup.find_all("script") if not s.get("src")]
    except Exception:
        return []


_ASSET_RE = re.compile(r"\.(png|jpg|jpeg|svg|css|woff2?|js)(\?|$)", re.I)
_API_PATH_RE = re.compile(
    r"/[A-Za-z0-9][\w./-]*(?:search|jobs|api|feed|listing|graphql|vacanc|posting)[\w./-]*",
    re.I,
)


def _api_candidates_from_text(text: str, reg_domain: str) -> list[str]:
    """Literal API URLs + reconstructed (JS-built host) candidates from one text blob."""
    out: list[str] = []
    seen: set[str] = set()
    for m in API_URL_CANDIDATE_RE.finditer(text):
        u = m.group(0).rstrip(",;)\"'")
        low = u.lower()
        if _ASSET_RE.search(low) or "${" in u:
            continue
        if not any(tok in low for tok in API_HINT_TOKENS):
            continue
        # keep only same-registrable-domain candidates (drop third-party junk like
        # share widgets) — third-party APIs (Algolia) are a separate, rarer case.
        if reg_domain and _registrable(u) != reg_domain:
            continue
        if u not in seen:
            seen.add(u)
            out.append(u)
            if len(out) >= 12:
                return out
    if reg_domain:
        paths: list[str] = []
        for m in _API_PATH_RE.finditer(text):
            p = m.group(0).split("?")[0].split("#")[0]
            low = p.lower()
            if 4 <= len(p) <= 80 and not low.endswith((".js", ".css", ".html", ".png", ".jpg")) \
               and p not in paths:
                paths.append(p)
            if len(paths) >= 14:
                break
        # rank API-like paths first so they're within the verification cap
        def _api_score(p: str) -> int:
            low = p.lower()
            s = 0
            if "/api/" in low:
                s += 5
            if "job/search" in low or "/jobsearch" in low:
                s += 5
            if low.endswith(("/search", "/jobs", "/query", "/feed", "/listings", "/graphql")):
                s += 4
            if "search" in low:
                s += 2
            if "jobs" in low:
                s += 1
            return s
        paths.sort(key=lambda p: -_api_score(p))
        for p in paths[:6]:
            for prefix in ("api.", "www.", "search.", "jobs.", ""):
                host = f"{prefix}{reg_domain}" if prefix else reg_domain
                cand = f"https://{host}{p}"
                if cand not in seen:
                    seen.add(cand)
                    out.append(cand)
    return out


def find_api_candidates(html: str, base_url: str) -> list[str]:
    """Candidate backend JSON-API URLs from the page's INLINE scripts.

    Verified later by firing a GET (verify_api). External JS bundles (where aya
    actually hides its API path) are scanned by ``scan_bundles_for_api``.
    """
    reg = _registrable(base_url)
    found: list[str] = []
    for txt in _inline_scripts(html):
        found.extend(_api_candidates_from_text(txt, reg))
        if len(found) >= 24:
            break
    return found[:24]


_BUNDLE_NAME_HINTS = ("search", "menu", "app", "api", "jobs", "query",
                      "listing", "filter", "catalog", "product")


def _external_scripts(html: str, base_url: str, limit: int = 6) -> list[str]:
    """Same-site .js bundles, prioritized by search-relevance of the filename.

    aya's API lives in ``ayaSearchMenus.js`` — not the first <script> on the page.
    Ranking by name (search/menu/app/…) surfaces the relevant bundle instead of
    jquery/social-share.
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return []
    scored: list[tuple[int, str]] = []
    for s in soup.find_all("script"):
        src = (s.get("src") or "").strip()
        if not src:
            continue
        absu = urljoin(base_url, src)
        if not absu.startswith(("http://", "https://")):
            continue
        if not absu.lower().split("?")[0].endswith(".js"):
            continue
        if not _same_site(absu, base_url):  # don't chase third-party CDNs blindly
            continue
        name = absu.lower().split("/")[-1]
        score = sum(1 for h in _BUNDLE_NAME_HINTS if h in name)
        scored.append((score, absu))
    scored.sort(key=lambda x: -x[0])
    return [u for _, u in scored[:limit]]


def _has_search_app_signal(html: str) -> bool:
    """Cheap gate so we only fetch bundles on pages that look like a JS search app."""
    low = html.lower()
    return any(tok in low for tok in (
        "ayasearch", "search-menu", "search-app", "__next_data__", "__nuxt__",
        "jobsearch", "searchwidget", "algolia", "vue.js", "react.root",
    ))


def scan_bundles_for_api(html: str, base_url: str, fetch_fn: Callable[..., dict],
                         limit: int = 4) -> list[str]:
    """Fetch the page's external JS bundles (HTTP) and scan them for API candidates.

    This is how aya's API is found without a browser: the page only references
    ``ayaSearchMenus.js``; the API path ``/AyaHealthcareWeb/job/search`` lives IN
    that bundle (with a JS-built host). We fetch the bundle, extract path
    fragments, reconstruct ``https://{api.,www.}<domain><path>``, and return
    candidates for verification. Bounded to ``limit`` bundles.
    """
    reg = _registrable(base_url)
    found: list[str] = []
    for burl in _external_scripts(html, base_url, limit):
        try:
            r = fetch_fn(burl, method="GET")
        except Exception:
            continue
        if not r.get("ok"):
            continue
        found.extend(_api_candidates_from_text(r.get("text") or "", reg))
        if len(found) >= 24:
            break
    return found[:24]


def verify_api(url: str, fetch_fn: Callable[..., dict], query: str = "") -> dict | None:
    """Empirically confirm a candidate URL is a JSON API returning a list of records.

    Fires a GET with a small limit/offset and checks the JSON has a list of dicts
    with a count. Returns a normalized descriptor or None. Rejects dropdown-taxonomy
    responses (select-option dicts shaped {disabled, group, selected, text, value}).
    """
    # Keys that identify a select-option/taxonomy response, NOT a data API
    _SELECT_OPTION_KEYS = {"disabled", "group", "selected", "text", "value", "label", "key", "id"}

    base = url.split("?")[0]
    for params in ({"limit": 5, "offset": 0}, {"pageSize": 5, "pageNumber": 1}, {}):
        try:
            r = fetch_fn(base, method="GET", params={**params})
        except Exception:
            continue
        if not r.get("ok"):
            continue
        body = r.get("text") or ""
        data = _safe_json(body)
        items, total = _extract_items_count(data)
        if items is None:
            continue
        sample_keys = list(items[0].keys())[:15] if items else []
        # Bug fix: reject select-option-shaped responses (GetQuickSearchData false positive)
        if set(sample_keys) <= _SELECT_OPTION_KEYS:
            logger.info("verify_api: rejecting %s — keys look like dropdown options: %s", base[:60], sample_keys)
            continue
        return {"url": base, "sample_params": params, "count": total,
                "items_per_page": len(items), "sample_keys": sample_keys}
    return None


def _safe_json(text: str):
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    # find first {...} or [...]
    for op, cl in (("{", "}"), ("[", "]")):
        i = text.find(op)
        if i < 0:
            continue
        depth = 0
        for j in range(i, len(text)):
            if text[j] == op:
                depth += 1
            elif text[j] == cl:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[i : j + 1])
                    except Exception:
                        break
    return None


def _extract_items_count(data) -> tuple[list | None, int | None]:
    """Find the items list + a total count anywhere in a parsed JSON blob."""
    best_items = None
    total = None
    def walk(v):
        nonlocal best_items, total
        if isinstance(v, list) and v and all(isinstance(x, dict) for x in v[:5]):
            if best_items is None or len(v) > len(best_items):
                best_items = v
        elif isinstance(v, dict):
            for k, val in v.items():
                if total is None and k.lower() in ("count", "total", "totalcount",
                                                   "total_count", "totalresults", "resultcount"):
                    try:
                        total = int(val)
                    except Exception:
                        pass
                walk(val)
    walk(data)
    return best_items, total


def count_job_signals(html: str) -> dict:
    """Deterministic listing-page signals (the goal check uses these)."""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return {"job_links": 0, "results_items": 0, "scripts": []}
    # job-detail hrefs
    job_links = 0
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if any(rx.search(href) for rx in JOB_HREF_RES):
            key = href.split("#")[0]
            if key not in seen:
                seen.add(key)
                job_links += 1
    # results container items (case-insensitive — CSS [class*=] would miss 'ProductTile')
    results_items = 0
    for el in soup.find_all(True):
        classes = " ".join(el.get("class") or []).lower()
        if any(h in classes for h in RESULTS_CONTAINER_HINTS):
            results_items += 1
            if results_items >= RESULTS_GOAL_THRESHOLD:
                break
    return {"job_links": job_links, "results_items": results_items}


def goal_check(html: str, base_url: str, raw_fetch: Callable[..., dict] | None = None) -> dict:
    """Decide if this page IS a job listing, and how the data is reached.

    Deterministic (no LLM). Returns a signals dict. Precedence:
    api (verified) > results_items > job_links > embedded_json.
    API/bundle probes use plain httpx (``raw_fetch``) — APIs return JSON, not HTML,
    so the browser /render fallback must not apply there.
    """
    raw_fetch = raw_fetch or _httpx_fetch
    signals = count_job_signals(html)
    api = None
    # EXPENSIVE API/bundle probing is gated: only when the page is NOT already a
    # listing (no product/job signals) — i.e. it's a JS shell that loads data via
    # an API (aya). Listing pages (calvklein watches → 186 ProductTiles) and bare
    # homepages skip this, so the cost is paid only on the "correct" traversal leg.
    listing_via_signals = (
        signals["results_items"] >= RESULTS_GOAL_THRESHOLD
        or signals["job_links"] >= RESULTS_GOAL_THRESHOLD
    )
    if not listing_via_signals:
        for cand in find_api_candidates(html, base_url)[:8]:
            api = verify_api(cand, raw_fetch)
            if api:
                break
        if not api and _has_search_app_signal(html):
            for cand in scan_bundles_for_api(html, base_url, raw_fetch)[:8]:
                api = verify_api(cand, raw_fetch)
                if api:
                    break
    signals["api"] = api
    # Bug fix: results_items alone is too weak (blog posts have 'card' classes).
    # Require either a verified API, OR job_links (actual job-detail hrefs), OR
    # a very strong results_items signal (>=15, not just >=5). This prevents
    # blog-post URLs from passing as the goal (locumtenens false positive).
    strong_listing = (
        signals["job_links"] >= RESULTS_GOAL_THRESHOLD
        or signals["results_items"] >= RESULTS_GOAL_THRESHOLD * 3
    )
    reached = bool(api or strong_listing)
    signals["reached"] = reached
    return signals


# ─── The traversal ──────────────────────────────────────────────────────────

@dataclass
class _Candidate:
    kind: str            # "link" | "submit"
    url: str
    method: str = "GET"
    data: dict = field(default_factory=dict)
    label: str = ""
    depth: int = 0
    path: list[str] = field(default_factory=list)
    keep: bool = False   # auto-kept (query-keyword match) — bypasses the LLM prune


@dataclass
class TraversalResult:
    reached: bool
    goal_url: str | None
    path: list[str]                      # homepage → … → goal
    mechanism: str                       # api | http_requests | embedded_json | detail_links | unknown
    api: dict | None
    signals: dict
    visited: list[str]
    pruned: list[str]
    notes: str = ""
    goal_method: str = "GET"             # how the goal page was reached (for replay)
    goal_data: dict = field(default_factory=dict)
    goal_request_url: str = ""           # the URL the winning action hit (form action)
    item_links: list = field(default_factory=list)  # real item/detail hrefs from the rendered goal page
    discovery: dict = field(default_factory=dict)  # {listing_url, listing_reached, pagination} — the contract


def _pick_mechanism(reached_by: str, signals: dict) -> str:
    if signals.get("api"):
        return "api"
    # a POST that yielded SSR results → http_requests (form-POST→SSR, replayable)
    if reached_by == "POST" and (signals.get("results_items") or signals.get("job_links")):
        return "http_requests"
    if signals.get("job_links") or signals.get("results_items"):
        return "detail_links"
    return "unknown"


def traverse(
    start_url: str,
    content_type: str,
    query: str,
    *,
    fetch_fn: Callable[..., dict] | None = None,
    raw_fetch: Callable[..., dict] | None = None,
    judge_fn: Callable[[list[dict], str, str, str], dict] | None = None,
    max_depth: int = MAX_DEPTH,
    max_visits: int = MAX_VISITS,
    max_judge_calls: int = MAX_JUDGE_CALLS,
) -> TraversalResult:
    """Run the bounded, LLM-pruned BFS from the homepage to a job listing.

    fetch_fn(url, method=..., params=..., data=...) -> {"ok": bool, "status": int,
        "final_url": str, "text": html}. Page fetch; defaults to httpx with a
        browser_service /render fallback for anti-bot/CSR sites.
    raw_fetch: used for API verify + JS-bundle scans (plain httpx — those return
        JSON/text, not HTML, so the browser fallback must not apply). Defaults to
        _httpx_fetch. Tests pass the same mock for both.
    judge_fn(candidates, content_type, query, site_url) -> {"ranking":[...]}.
        Defaults to url_judge.judge_candidate_urls (imported lazily).
    """
    fetch_fn = fetch_fn or _default_fetch
    raw_fetch = raw_fetch or _httpx_fetch
    judge = judge_fn or _default_judge

    start = start_url.rstrip("/") + "/"
    visited: list[str] = []
    visited_set: set[str] = set()
    pruned: list[str] = []
    judge_calls = 0

    # frontier of candidates; seed with the homepage (a GET).
    frontier: list[_Candidate] = [_Candidate("link", start, "GET", label="homepage", depth=0, path=[start])]

    best_partial: TraversalResult | None = None

    while frontier and len(visited) < max_visits:
        cand = frontier.pop(0)
        # key by (method, url): a form's GET (view page) and POST (submit) hit
        # the same URL but are different traversal edges (locumtenens QuickSearch).
        key = (cand.method.upper(), cand.url.split("#")[0].rstrip("/"))
        if key in visited_set:
            continue
        visited_set.add(key)
        visited.append(f"{cand.method} {cand.url}")

        try:
            r = fetch_fn(cand.url, method=cand.method, params=None, data=cand.data if cand.kind == "submit" else None)
        except Exception as exc:
            logger.warning("fetch failed %s %s: %s", cand.method, cand.url[:80], exc)
            continue
        if not r.get("ok"):
            continue
        html = r.get("text") or ""
        final_url = r.get("final_url") or cand.url

        # 1) deterministic goal check
        # goal_check's API/bundle probes use raw httpx (no /render fallback);
        # the page itself was already fetched (with fallback) above.
        signals = goal_check(html, final_url, raw_fetch)
        reached_by = "POST" if (cand.kind == "submit" and cand.method.upper() == "POST") else "GET"
        if signals.get("reached"):
            path = cand.path + [final_url]
            return TraversalResult(
                reached=True, goal_url=final_url, path=path,
                mechanism=_pick_mechanism(reached_by, signals),
                api=signals.get("api"), signals=signals,
                visited=visited, pruned=pruned,
                notes=f"reached via {cand.kind} ({cand.method}) at depth {cand.depth}",
                goal_method=cand.method, goal_data=dict(cand.data),
                goal_request_url=cand.url,
            )
        # track the strongest partial (for a not-found report)
        if best_partial is None or _signal_strength(signals) > _signal_strength(best_partial.signals):
            best_partial = TraversalResult(
                reached=False, goal_url=final_url, path=cand.path + [final_url],
                mechanism="unknown", api=None, signals=signals,
                visited=visited, pruned=pruned, notes="best partial (below goal threshold)",
            )

        if cand.depth >= max_depth:
            continue

        # 2) expand: links + text-keyword auto-keeps + search-form submit
        kw_urls = {lk["href"] for lk in _keyword_links(html, final_url, query)}
        candidates: list[_Candidate] = []
        seen_cand: set[str] = set()
        for link in extract_links(html, final_url):
            if link["href"] in seen_cand:
                continue
            seen_cand.add(link["href"])
            candidates.append(_Candidate("link", link["href"], "GET", label=link["text"],
                                         depth=cand.depth + 1, path=cand.path + [final_url],
                                         keep=(link["href"] in kw_urls)))
        # keyword text-matches beyond the cap are still added (and auto-kept)
        for link in _keyword_links(html, final_url, query):
            if link["href"] in seen_cand:
                continue
            seen_cand.add(link["href"])
            candidates.append(_Candidate("link", link["href"], "GET", label=link["text"],
                                         depth=cand.depth + 1, path=cand.path + [final_url],
                                         keep=True))
        form = detect_search_form(html, final_url)
        if form and form.is_search:
            payload = _form_payload(form, query or "")
            candidates.append(_Candidate("submit", form.action, form.method.upper(),
                                         data=payload, label=f"submit-search({form.method})",
                                         depth=cand.depth + 1, path=cand.path + [final_url]))

        # 3) LLM prune (batched, bounded) — auto-keeps (text-keyword matches) and
        # submit actions bypass the LLM; the rest are judged.
        to_judge = [c for c in candidates if not c.keep and c.kind == "link"]
        if to_judge and judge_calls < max_judge_calls:
            judge_calls += 1
            try:
                judgment = judge(
                    [{"href": c.url, "text": c.label} for c in to_judge],
                    content_type, query, start,
                )
            except Exception as exc:
                logger.warning("judge failed: %s", exc)
                judgment = {"ranking": []}
            correct = {r["url"] for r in (judgment.get("ranking") or []) if r.get("verdict") == "correct"}
            kept: list[_Candidate] = []
            for c in candidates:
                # keep: submit-search actions, text-keyword auto-keeps, LLM-`correct` links
                if c.kind == "submit" or c.keep or c.url in correct:
                    kept.append(c)
                else:
                    pruned.append(f"{c.label or ''} {c.url}")
            candidates = kept
        # else: out of judge budget → keep all (let the deterministic goal-check decide)

        # best-confidence-first: keep order (judge already returned ranked); forms first
        candidates.sort(key=lambda c: 0 if c.kind == "submit" else 1)
        frontier.extend(candidates)

    if best_partial:
        best_partial.visited = visited
        best_partial.pruned = pruned
        return best_partial
    return TraversalResult(False, None, [start], "unknown", None, {}, visited, pruned, "budget exhausted; nothing visited productively")


def _signal_strength(signals: dict) -> int:
    return (signals.get("results_items") or 0) + (signals.get("job_links") or 0)


# ─── Default fetch + judge (real I/O; tests inject mocks) ───────────────────

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/json,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_BLOCKED_MARKERS = ("akamai", "_abck", "challenge-form", "cf-browser-verification",
                    "access denied", "enable javascript", "bot detection", "px-captcha")


def _is_blocked(html: str, status: int) -> bool:
    if status == 0 or status >= 400:
        return True
    low = (html or "").lower()
    return any(m in low for m in _BLOCKED_MARKERS)


def _httpx_fetch(url: str, method: str = "GET", params: dict | None = None,
                 data: dict | None = None, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Plain httpx GET/POST (no browser fallback). Used for API verify + bundle fetch."""
    try:
        with httpx.Client(headers=_HEADERS, timeout=timeout, follow_redirects=True) as client:
            if (method or "GET").upper() == "POST":
                resp = client.post(url, data=data or {})
            else:
                resp = client.get(url, params=params or {})
        return {"ok": resp.status_code < 400, "status": resp.status_code,
                "final_url": str(resp.url), "text": resp.text}
    except Exception as exc:
        logger.warning("_httpx_fetch %s %s failed: %s", method, url[:80], exc)
        return {"ok": False, "status": 0, "final_url": url, "text": ""}


def _render_via_browser(url: str, timeout: int = 120) -> dict:
    """Fetch RENDERED html via browser_service POST /render (cloak applied server-side).

    This is the anti-bot bypass the existing pipeline already uses (navigate_explore's
    _fetch_via_probe_html). Called only as a fallback when plain HTTP is blocked
    (Akamai/CF) or the page is JS-rendered. Reuses the service read-only — no existing
    agent is modified.
    """
    try:
        r = httpx.post(f"{BROWSER_SERVICE_URL}/render", json={"url": url, "timeout": timeout},
                       timeout=timeout + 15)
        data = r.json()
        if data.get("success"):
            return {"ok": True, "status": 200, "final_url": url,
                    "text": data.get("html", ""), "via": f"browser:{data.get('method','?')}"}
        logger.warning("/render failed for %s: %s", url[:80], data.get("error"))
    except Exception as exc:
        logger.warning("/render error for %s: %s", url[:80], exc)
    return {"ok": False, "status": 0, "final_url": url, "text": ""}


def _default_fetch(url: str, method: str = "GET", params: dict | None = None,
                   data: dict | None = None, timeout: float = DEFAULT_TIMEOUT,
                   browser_fallback: bool = True) -> dict:
    """HTTP-first GET/POST; on block/timeout, fall back to browser_service /render.

    Returns {ok, status, final_url, text, via?}. ``via`` is 'browser:<method>' when the
    fallback was used (e.g. Akamai sites like calvklein.co.uk).
    """
    r = _httpx_fetch(url, method, params, data, timeout)
    if r["ok"] and not _is_blocked(r["text"], r["status"]):
        r["via"] = "http"
        return r
    if not browser_fallback:
        return r
    # anti-bot block or JS shell → rendered fetch (cloak handled server-side)
    rb = _render_via_browser(url)
    if rb["ok"]:
        return rb
    return r


def _default_judge(candidates: list[dict], content_type: str, query: str, site_url: str) -> dict:
    """Use the existing url_judge LLM judge (imported lazily, read-only)."""
    from agents.nodes.url_judge import judge_candidate_urls
    return judge_candidate_urls(candidates, content_type, query, site_url)


# ─── Network-log capture (gated to the reached listing page only) ────────────
# Simple goal: once we've reached a listing, look at the browser's network log to
# see whether the data actually comes from a backend API (and which one). Reuses
# the Playwright MCP browser that navigate_explore already uses — no service or
# agent changes. NOT run on every page — only on the listing we reached.

_NETWORK_JS = (
    "() => JSON.stringify((performance.getEntriesByType('resource')||[])"
    ".map(e => ({url: e.name, t: e.initiatorType})))"
)
_STATIC_RE = re.compile(r"\.(js|css|png|jpe?g|svg|woff2?|gif|ico|webp|ttf)(\?|$)", re.I)
# analytics / ads / telemetry — NOT data APIs. Reject these even if they're fetch/xhr.
_TELEMETRY_RE = re.compile(
    r"google-analytics\.com|googletagmanager|doubleclick|google\.(com|adservices)/("
    r"ccm|rmkt|analytics|ads|pagead|g/collect)|connect\.facebook|facebook\.com/tr|"
    r"stackadapt|singular\.net|company-target|criteo|bat\.bing|bing\.com/bat|"
    r"pinterest|hotjar|clarity|segment\.io|mixpanel|amplitude|optimizely|zaius|"
    r"bat\.bing|/collect|/track|/pixel|/beacon|saq_pxl|telemetry|tags\.srv|"
    r"sdk-api|/events?\b|linkedin\.com|ttw\.com|tiktok|"
    r"px\.ads|attribution_trigger|usbrowserspeed|propensity|adsystem|adserver|"
    r"/ads?\b|/log\b|fingerprint|/j/collect|/b/r|/i/ads",
    re.I,
)
_API_HINT_TOKENS_NET = ("search", "product", "job", "listing", "feed", "graphql",
                        "/api/", "catalog", "items", "results")


def _parse_resource_entries(raw: str) -> list[dict]:
    """Pull the JSON array of {url,t} out of an MCP evaluate result string."""
    if not raw or not isinstance(raw, str):
        return []
    import json as _json
    m = re.search(r"### Result\s*\n(.+?)(?:\n###|\Z)", raw, re.DOTALL)
    blob = m.group(1).strip() if m else raw.strip()
    if blob.startswith('"') and blob.endswith('"'):
        try:
            blob = _json.loads(blob)
        except Exception:
            pass
    if isinstance(blob, str):
        try:
            blob = _json.loads(blob)
        except Exception:
            return []
    if isinstance(blob, str):
        return []
    return [x for x in blob if isinstance(x, dict)] if isinstance(blob, list) else []


def capture_network_resources(url: str, settle_seconds: float = 3.0) -> list[dict]:
    """Open ``url`` in the MCP browser and return its network resource entries.

    Reuses ``create_playwright_tools_sync`` (the same tool navigate_explore uses).
    Returns ``[]`` if the MCP browser is unavailable. Call this ONLY on the listing
    page we reached — it's a real browser navigation, not cheap.
    """
    try:
        from agents.tools.playwright_tools import create_playwright_tools_sync
    except Exception as exc:
        logger.warning("network capture: MCP tools unavailable: %s", exc)
        return []
    try:
        tools = create_playwright_tools_sync(fresh=True)
    except Exception as exc:
        logger.warning("network capture: MCP init failed: %s", exc)
        return []
    if not tools:
        return []
    nav = next((t for t in tools if getattr(t, "name", "") == "playwright_browser_navigate"), None)
    ev = next((t for t in tools if getattr(t, "name", "") == "playwright_browser_evaluate"), None)
    if not nav or not ev:
        return []
    try:
        nav.invoke({"url": url})
    except Exception as exc:
        logger.warning("network capture: navigate failed: %s", exc)
        return []
    import time
    time.sleep(settle_seconds)  # let XHR/fetch calls fire
    # retry the evaluate once if the page was slow to settle (shared MCP browser
    # can race the first read)
    raw = None
    for _ in range(2):
        try:
            raw = ev.invoke({"function": _NETWORK_JS})
        except Exception as exc:
            logger.warning("network capture: evaluate failed: %s", exc)
            return []
        entries = _parse_resource_entries(raw.content if hasattr(raw, "content") else str(raw))
        if entries:
            return entries
        time.sleep(settle_seconds)
    return []


def api_from_network(resources: list[dict]) -> list[str]:
    """Pick the data-API-looking URLs out of a network log.

    Keeps fetch/XHR URLs that aren't static assets and look like a data endpoint
    (search/product/job/api/listing/feed/graphql). This is a *hint* that the data
    comes from an API — we don't call them here, just report what the page fetched.
    """
    out: list[str] = []
    seen: set[str] = set()
    for r in resources:
        url = (r.get("url") or "").strip()
        init = (r.get("t") or "").lower()
        if not url or url in seen:
            continue
        if _STATIC_RE.search(url.lower()):
            continue
        if _TELEMETRY_RE.search(url):
            continue
        is_xhr = init in ("fetch", "xmlhttprequest")
        looks_data = any(tok in url.lower() for tok in _API_HINT_TOKENS_NET)
        if is_xhr and looks_data:
            seen.add(url)
            out.append(url)
    return out


def _capture_api_from_session(ev, goal_url: str, query: str):
    """Discover the backend data API from the LIVE browser session on the goal page.

    Called by ``browser_traverse`` once the LLM judges a listing — the browser is
    already on the goal page, so this is a cheap read of what has already loaded
    (no fresh re-navigation that would discard session XHR history).

    Gathers candidates from two complementary, generic signals (both reuse helpers
    ``traverse()`` already exercises), then picks the BEST:

    1. **Network resource log** — ``performance.getEntriesByType('resource')`` from
       the current session. Catches any data-API XHR that fired during traversal,
       i.e. XHR-on-load sites (myntra, calvklein) and interaction-triggered sites
       *if* the traversal submitted a search.
    2. **JS-bundle scan** — fetch the goal page's external scripts and scan them for
       endpoint literals. Catches interaction-triggered APIs whose XHR never fired
       because no search was submitted (aya, amn) — the endpoint URL is constructed
       client-side from a literal in the bundle.

    Both signals can produce false positives — e.g. aya fires an on-load taxonomy
    XHR (``wp-json/.../joblookups``: 2217 city/state records, no count) that looks
    data-ish but isn't the jobs API. So instead of returning the first hit, we
    collect ALL verified candidates and rank them: prefer an API that exposes an
    explicit record count (real paginated data) and the richest record schema. The
    real jobs API (aya ``/job/search``: count=26803, 90+ fields) beats the taxonomy
    endpoint (count=null, 3 fields).

    Returns a ``verify_api()`` descriptor ``{url, count, sample_keys, ...}`` or
    ``None``. No site-specific strings; falls through gracefully to ``None`` for
    SSR sites with no backend API (no regression).
    """
    candidates: list[dict] = []
    seen: set[str] = set()

    def _consider(api):
        if not api:
            return
        base = (api.get("url") or "").split("?")[0]
        if base and base not in seen:
            seen.add(base)
            candidates.append(api)

    # Short-timeout fetch for candidate probing: verify_api GETs many candidate
    # URLs (host variants of the same path), and on anti-bot / slow domains the
    # real-host candidates hang ~20s each → minutes of accumulation (calvklein). A
    # real data API responds in <2s; 8s is plenty and lets blocked candidates fail
    # fast so the capture step stays bounded. (aya's API verifies in <1s.)
    import functools

    _fast_fetch = functools.partial(_httpx_fetch, timeout=8.0)

    # 1. live network resource log
    try:
        raw = ev.invoke({"function": _NETWORK_JS})
        entries = _parse_resource_entries(raw.content if hasattr(raw, "content") else str(raw))
        for cand in api_from_network(entries):
            _consider(verify_api(cand, _fast_fetch, query))
    except Exception as exc:
        logger.warning("browser_traverse: network API capture failed: %s", exc)

    # 2. JS-bundle scan (catches aya/amn-style interaction-triggered APIs)
    try:
        page = _fast_fetch(goal_url)
        if page.get("ok"):
            for cand in scan_bundles_for_api(page.get("text", ""), goal_url, _fast_fetch):
                _consider(verify_api(cand, _fast_fetch, query))
    except Exception as exc:
        logger.warning("browser_traverse: bundle-scan API capture failed: %s", exc)

    if not candidates:
        return None

    def _score(api):
        # Real paginated data APIs expose an explicit count; taxonomies/lookups
        # usually don't. Richer record schema = more likely a real entity list.
        has_count = 1 if api.get("count") is not None else 0
        n_keys = len(api.get("sample_keys") or [])
        return (has_count, n_keys)

    best = max(candidates, key=_score)
    logger.info("browser_traverse: API captured (%d candidate(s), picked %s)",
                len(candidates), best.get("url", "")[:80])
    return best


# ═══════════════════════════════════════════════════════════════════════════════
# BROWSER-DRIVEN TRAVERSAL (do what a user does)
# ═══════════════════════════════════════════════════════════════════════════════
# The explore core: open the homepage in a real browser, and at each step the LLM
# decides the next action (click / scroll / go-to-URL) until the listing is found.
# HTTP is NOT used for discovery — only for the post-discovery extraction check.
# One evaluate per step returns BOTH the clickable surface AND the goal-check
# signals (compact JSON, no outerHTML round-trip).

_PAGE_STATE_JS = r"""
() => {
  function sel(el) {
    if (el.id) return '#' + CSS.escape(el.id);
    if (el.name) return el.tagName.toLowerCase() + '[name="' + el.name + '"]';
    var c = el.className;
    if (typeof c === 'string' && c.trim())
      return el.tagName.toLowerCase() + '.' + CSS.escape(c.trim().split(/\s+/)[0]);
    return el.tagName.toLowerCase();
  }
  var JUNK = /login|signin|sign in|register|sign up|cart|account|cookie|privacy|terms|facebook|twitter|instagram|linkedin|youtube|unsubscribe|gdpr|close/i;
  var clickables = [], seen = new Set();
  document.querySelectorAll('a[href], button, [role="button"], [role="link"]').forEach(function(el) {
    var t = (el.innerText || el.textContent || '').trim().slice(0, 80);
    if (!t || t.length < 2 || JUNK.test(t)) return;
    var href = el.href || '';
    if (!href || href.startsWith('#') || href.startsWith('javascript')) href = '';
    var s = sel(el);
    var key = href || s;
    if (seen.has(key)) return;
    seen.add(key);
    clickables.push({label: t, selector: s, href: href || null, kind: el.tagName === 'A' ? 'link' : 'button'});
  });
  // goal-check via DOM REPETITION (content-type-agnostic; replaces the token
  // lists that miss Workday hashed-class SPAs, myntra product grids, etc.).
  // Find a sibling group that looks like a listing: >=5 same-tag+class siblings
  // under a NON-nav/footer/aside/header parent, each anchoring to a DISTINCT
  // href that shares a path prefix (depth>=1) — this excludes top-nav/footer
  // menus (shared nav hrefs), pagination strips, and /about-style root links.
  function _commonPrefixDepth(uA, uB) {
    // Compare PATHNAME only (not host) so root-level nav links (/about vs
    // /contact) share 0 segments while item links (/jobs/123 vs /jobs/456)
    // share the "/jobs/" segment. Excludes nav menus / footer link farms.
    try {
      var pa = new URL(uA, location.href).pathname.replace(/^\/+/, '').split('/');
      var pb = new URL(uB, location.href).pathname.replace(/^\/+/, '').split('/');
    } catch (e) { return 0; }
    var n = 0;
    for (var i = 0; i < Math.min(pa.length, pb.length, 5); i++) {
      if (pa[i] === pb[i] && pa[i]) n++; else break;
    }
    return n;
  }
  var NAVISH = /^(NAV|FOOTER|ASIDE|HEADER)$/i;
  var containers = document.querySelectorAll('ul,ol,div,section,tbody,main');
  var bestN = 0;
  var bestDiv = false;
  for (var ci = 0; ci < containers.length && bestN < 60; ci++) {
    var p = containers[ci];
    if (NAVISH.test(p.tagName)) continue;
    var groups = {};
    for (var chi = 0; chi < p.children.length; chi++) {
      var c = p.children[chi];
      var cls = (c.className || '').toString().trim().split(/\s+/)[0] || '';
      // CSS-module hash tolerance: strip a trailing _/- segment that contains a
      // digit (hashes do; real class parts like "-tile" / "-card" don't).
      cls = cls.replace(/[_-][a-z0-9]*\d[a-z0-9]*$/i, '');
      var key = c.tagName + '|' + cls;
      if (!groups[key]) groups[key] = [];
      groups[key].push(c);
    }
    for (var gk in groups) {
      var sibs = groups[gk];
      if (sibs.length < 5) continue;
      // visible-text predicate (applies to both anchor + div-listing detection)
      var withText = 0;
      for (var ti = 0; ti < sibs.length; ti++)
        if ((sibs[ti].innerText || '').trim().length > 2) withText++;
      if (withText < sibs.length * 0.6) continue;
      // Anchor-based detection (links to distinct hrefs sharing a path prefix)
      var hrefs = [];
      for (var si = 0; si < sibs.length; si++) {
        var a = sibs[si].querySelector('a[href]');
        if (a && a.href && !/^(javascript|mailto|tel|#)/i.test(a.href))
          hrefs.push(a.href.split('#')[0]);
      }
      var distinct = hrefs.filter(function (v, i, arr) { return arr.indexOf(v) === i; });
      if (distinct.length >= 5) {
        var pref = 99;
        for (var di = 1; di < distinct.length && di < 10; di++)
          pref = Math.min(pref, _commonPrefixDepth(distinct[0], distinct[di]));
        if (pref >= 1 && distinct.length > bestN) bestN = distinct.length;
      } else {
        // DIV-LISTING FALLBACK (Part B1): items are server-rendered divs/li with
        // data-*-id attributes (e.g. dystaffing's <li data-job-id="...">) — no
        // per-item anchors. Count siblings carrying an item data-attr.
        var dataItems = 0;
        for (var di2 = 0; di2 < sibs.length; di2++) {
          var el = sibs[di2];
          if (el.hasAttribute('data-job-id') || el.hasAttribute('data-product-id') ||
              el.hasAttribute('data-id') || el.hasAttribute('data-sku') ||
              el.hasAttribute('data-item-id') ||
              el.querySelector('[data-job-id],[data-product-id],[data-id],[data-sku],[data-item-id]'))
            dataItems++;
        }
        if (dataItems >= 5 && dataItems > bestN) { bestN = dataItems; bestDiv = true; }
      }
    }
  }
  var ri = bestN, jl = bestN;
  return JSON.stringify({
    url: location.href, title: (document.title || '').slice(0, 80),
    clickables: clickables.slice(0, 25),
    scroll_hint: document.body.scrollHeight > window.innerHeight * 1.5,
    has_load_more: (function() {
      // Shared selector list with Layer C (src/discovery.py) via the sentinel
      // below — see src/pagination_patterns.py. Visibility/clickability gate
      // keeps this PRESENCE check honest with Layer C's visible+enabled CLICK
      // contract: a hidden Coveo "magicbox" suggestion loader or an off-screen
      // "More filters" facet button must NOT flip has_load_more (which would
      // mis-classify the page as load_more and send Layer C clicking the wrong
      // element). Mirrors click_load_more's visible+enabled semantics.
      var matches = document.querySelectorAll(/*__LOAD_MORE_SELECTORS__*/);
      for (var lmi = 0; lmi < matches.length; lmi++) {
        var el = matches[lmi];
        if (!el.offsetParent && el.getClientRects().length === 0) continue;  // hidden
        if (el.disabled) continue;
        if (el.getAttribute('aria-hidden') === 'true') continue;
        return true;
      }
      return false;
    })(),
    has_rel_next_page_param: (function() {
      var el = document.querySelector('a[rel="next"]');
      if (!el) return '';
      var href = el.getAttribute('href') || '';
      var m = href.match(/[?&](page|p|pg|pn|pagenum|start|offset)=(\d+)/i);
      return m ? m[1] : '';
    })(),
    signals: {results_items: ri, job_links: jl, reached: ri >= 5 || jl >= 5, div_listing: bestDiv}
  });
}
"""

# Inject the shared Layer-A/C load-more selector list into the JS probe. Done at
# module load (the JS runs verbatim in the page via ev.invoke and can't read
# files). Sentinel-replace rather than f-string: the JS body has many { }.
_PAGE_STATE_JS = _PAGE_STATE_JS.replace(
    "/*__LOAD_MORE_SELECTORS__*/",
    PATTERNS.load_more_presence_css_list,
)
assert "/*__LOAD_MORE_SELECTORS__*/" not in _PAGE_STATE_JS, (
    "_PAGE_STATE_JS sentinel was not replaced — pagination_patterns load_more_css_list changed?"
)


def _parse_mcp_json(raw) -> dict:
    """Parse a JSON object from an MCP evaluate result string."""
    content = raw.content if hasattr(raw, "content") else str(raw)
    if not content:
        return {}
    m = re.search(r"### Result\s*\n(.+?)(?:\n###|\Z)", content, re.DOTALL)
    blob = m.group(1).strip() if m else content.strip()
    if blob.startswith('"') and blob.endswith('"'):
        try:
            blob = json.loads(blob)
        except Exception:
            pass
    if isinstance(blob, str):
        try:
            blob = json.loads(blob)
        except Exception:
            return {}
    return blob if isinstance(blob, dict) else {}


def _render_surface_text(surface: dict) -> str:
    """Render a compact text view of the page-state surface for the LLM step.

    Replaces the heavy accessibility **snapshot** (which stalls the @playwright/mcp
    Node event loop on heavy SPAs — see _PAGE_STATE_JS) with a ~1-2 KB text summary:
    title/url, goal-check signals, and the visible clickable items (label + href).
    The LLM reads this to judge is_listing and pick a click target by its visible
    label (which _do_action resolves via find-by-text). llm_step's contract is
    unchanged — only the input text source differs.
    """
    clickables = surface.get("clickables") or []
    signals = surface.get("signals") or {}
    lines = [f"Page: {surface.get('title', '')[:60]}  ({surface.get('url', '')})"]
    lines.append(
        f"Goal signals: result_cards={signals.get('results_items', 0)}, "
        f"item_links={signals.get('job_links', 0)}"
    )
    if surface.get("scroll_hint"):
        lines.append("Scroll possible (page is tall).")
    if surface.get("has_load_more"):
        lines.append("A 'load more' / 'show more' control is present.")
    lines.append("Clickable items (click by the visible text shown):")
    for c in clickables[:25]:
        href = f"  -> {c.get('href')}" if c.get("href") else ""
        lines.append(f"- [{c.get('kind', 'link')}] {c.get('label', '')}{href}")
    return "\n".join(lines)


def choose_action(surface: dict, content_type: str, query: str, history: list) -> dict:
    """LLM picks the next browser action: click / scroll / goto / done.

    One cheap one-shot call. The LLM sees the clickable surface + scroll hint +
    goal + recent actions, and returns a single action to take.
    """
    clickables = surface.get("clickables") or []
    scroll_hint = surface.get("scroll_hint", False)
    lines = [f"{i}. [{c['kind']}] {c['label']}" for i, c in enumerate(clickables[:20])]
    cands = "\n".join(lines) or "(none visible)"
    hist = " -> ".join(f"{h.get('action','?')}" for h in history[-4:]) or "(start)"

    prompt = (
        "You are driving a browser to find a page that lists many items.\n"
        f"Content type: {content_type}. Looking for: '{query}'.\n"
        f"Actions so far: {hist}\n"
        f"Current page: {surface.get('title','')[:60]}\n"
        f"Scroll possible (page is tall): {scroll_hint}\n\n"
        f"Clickable items on this page:\n{cands}\n\n"
        "Pick the ONE best next action to reach a listing page.\n"
        "- click: pick a clickable by its index number (0-based).\n"
        "- scroll: scroll down to trigger lazy-loading / infinite scroll.\n"
        "- goto: navigate to a URL.\n"
        "- done: if you think the current page IS the listing.\n"
        "Respond with ONLY a JSON object (no markdown):\n"
        '{"action": "click"|"scroll"|"goto"|"done", "target": "<index or url or down>", "reason": "short"}'
    )
    try:
        from langchain_core.messages import HumanMessage
        from agents.llm import get_small_llm

        llm = get_small_llm(temperature=0.0)
        resp = llm.invoke([HumanMessage(content=prompt)])
        text = (resp.content or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
        if text[:4].lower() == "json":
            text = text[4:].lstrip()
        result = json.loads(text)
        # resolve click-by-index to the actual selector/href
        if result.get("action") == "click":
            try:
                idx = int(result.get("target", "0"))
                c = clickables[idx]
                result["target"] = c.get("href") or c["selector"]
                result["_kind"] = c.get("kind", "link")
            except (ValueError, IndexError):
                result["action"] = "done"
                result["target"] = ""
        return {"action": result.get("action", "done"),
                "target": result.get("target", ""),
                "reason": (result.get("reason") or "")[:200]}
    except Exception as exc:
        logger.warning("choose_action failed: %s", exc)
        return {"action": "done", "target": "", "reason": f"error: {exc}"[:200]}


def llm_step(snapshot_text: str, content_type: str, query: str, history: list) -> dict:
    """One LLM call: reads the a11y snapshot → is_listing + next action.

    The LLM sees the accessibility tree (includes hidden mega-menu items) and
    judges BOTH 'is this a listing?' (goal-check) and 'what to click next?'
    (action). No custom JS or CSS heuristics — pure LLM judgment from the
    semantic tree.
    """
    snap = (snapshot_text or "")[:8000]
    hist_parts = []
    for h in history[-4:]:
        a = h.get("action", "?")
        t = h.get("target", "")
        stuck = h.get("_stuck", False)
        hist_parts.append(f"{a}({t})" + (" [NO CHANGE]" if stuck else ""))
    hist = " -> ".join(hist_parts) or "(start)"

    # stuck warning: if the last click target repeats, tell the LLM to try differently
    stuck_warning = ""
    if len(history) >= 1:
        last = history[-1]
        if last.get("action") == "click" and last.get("target"):
            count = sum(1 for h in history[-3:] if h.get("target") == last["target"])
            if count >= 2:
                stuck_warning = (
                    f"\n⚠️ You clicked {last['target']} {count} times and the page didn't change. "
                    f"Do NOT click {last['target']} again — try a DIFFERENT ref or scroll.\n"
                )

    prompt = (
        "You are browsing a website to find a page listing many items.\n"
        f"Content type: {content_type}. Looking for: '{query}'.\n"
        f"Actions so far: {hist}\n"
        f"{stuck_warning}\n"
        "Here is the accessibility tree of the current page (YAML).\n"
        "Items with [ref=eN] are clickable — click them by their ref.\n"
        "Hidden items (mega-menus, dropdowns) ARE included.\n\n"
        f"{snap}\n\n"
        "Answer TWO questions:\n"
        f"1. Is this page a listing of many {content_type} (items in a list or grid)?\n"
        "2. If NOT, what is the best next action?\n"
        "IMPORTANT STRATEGY for job boards / search sites:\n"
        "- If the homepage has a 'Search Jobs' / 'Find a Job' / 'Quick Search' link, CLICK IT\n"
        "  FIRST to get to the actual search form page. Do NOT type into the homepage search\n"
        "  box if there's a dedicated search page.\n"
        "- Once on the search form page, use select_form to fill a dropdown + submit.\n"
        "- If you can infer a direct listing URL (e.g. site.com/watches, site.com/jobs), goto it.\n"
        "- If the page has a FEW items (result_cards < 15) but ALSO has a 'View All', 'Browse All',\n"
        "  or 'See All' link, CLICK IT — the current page may be a featured/curated subset.\n"
        "  Prefer pages with MORE items.\n"
        "Actions:\n"
        "   - goto: navigate directly to a URL you can infer (e.g. site.com/watches).\n"
        "   - click: provide the VISIBLE TEXT of the element to click (e.g. 'Men', 'Watches').\n"
        "   - type: type into the search box — provide 'search' as target + the text to type.\n"
        "   - select_form: if there's a search form with dropdowns, provide the VALUE to select\n"
        "     in the most specific field (e.g. a Specialty/Category). The system will pick the\n"
        "     first valid option + submit the form.\n"
        "   - scroll: scroll down.\n"
        "   - done: give up.\n"
        'Respond with ONLY a JSON object (no markdown):\n'
        '{"is_listing": true/false, "action": "goto"|"click"|"type"|"select_form"|"scroll"|"done", '
        '"target": "URL for goto | visible text for click | search for type | field-name for select_form", '
        '"text": "text to type or option-value to select", "reason": "short"}'
    )
    try:
        from langchain_core.messages import HumanMessage
        from agents.llm import get_small_llm

        llm = get_small_llm(temperature=0.0)
        resp = llm.invoke([HumanMessage(content=prompt)])
        text = (resp.content or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
        if text[:4].lower() == "json":
            text = text[4:].lstrip()
        result = json.loads(text)
        return {
            "is_listing": bool(result.get("is_listing", False)),
            "action": result.get("action", "done"),
            "target": result.get("target") or "",
            "text": result.get("text") or "",
            "reason": (result.get("reason") or "")[:200],
        }
    except Exception as exc:
        logger.warning("llm_step failed: %s", exc)
        return {"is_listing": False, "action": "done", "target": "", "reason": f"error: {exc}"[:200]}


def _do_action(action: dict, nav, click, ev, wait, type_t=None) -> str:
    """Execute one browser action. Uses text-based finding (not ephemeral refs).

    MCP snapshot refs expire on live SPAs (myntra rotates content → refs stale by
    execution time). Instead of click(ref), we find the element by its visible
    text via a tiny evaluate + click. The LLM identifies elements by TEXT (stable);
    this executes by finding that text on the page RIGHT NOW.
    """
    a = action.get("action", "done")
    target = action.get("target", "")  # now a TEXT LABEL, not a ref
    desc = a
    if a == "click" and target:
        if target.startswith("http"):
            nav.invoke({"url": target})
            desc = f"goto({target[:40]})"
        else:
            # find-by-text + click (stable — no ephemeral refs)
            js = (
                "() => { const t = " + json.dumps(target) + ";"
                " const els = document.querySelectorAll('a,button,[role=\"button\"],[role=\"link\"],input');"
                " for (const el of els) {"
                " const tx = (el.innerText||el.value||el.placeholder||el.getAttribute('aria-label')||'').trim();"
                " if (tx.includes(t)) { el.click(); return 'ok'; } } return 'not found'; }"
            )
            try:
                ev.invoke({"function": js})
                desc = f"click('{target[:25]}')"
            except Exception as exc:
                logger.warning("click '%s' failed: %s", target[:30], exc)
    elif a == "type" and target:
        text = action.get("text", "")
        # use the MCP type tool with a STABLE CSS selector — Playwright's real
        # keyboard simulation triggers React state correctly (unlike evaluate JS
        # which sets input.value directly and bypasses React).
        if type_t:
            try:
                type_t.invoke({"target": 'input[placeholder*="search" i]',
                               "text": text, "submit": True})
                desc = f"type('{text[:20]}')"
            except Exception as exc:
                logger.warning("type tool failed: %s", exc)
        else:
            logger.warning("type tool unavailable")
    elif a == "select_form":
        # Drive a search form with <select> dropdowns (locumtenens QuickSearch):
        # pick the first valid option in the most specific <select>, then POST the
        # form out-of-band via httpx. Submitting the form inside page.evaluate
        # triggers a navigation that destroys the JS context mid-evaluate, which
        # hangs Playwright forever (evaluate has no timeout). Instead we extract
        # {action, method, data} from the form WITHOUT submitting, then POST via
        # _httpx_fetch and navigate the browser to the redirect target.
        js = (
            "() => {"
            " const forms = document.querySelectorAll('form');"
            " for (const form of forms) {"
            "  const selects = form.querySelectorAll('select');"
            "  if (selects.length < 1) continue;"
            "  const hint = " + json.dumps(target.lower()) + ";"
            "  let best = null;"
            "  for (const sel of selects) {"
            "   const name = (sel.name||sel.id||'').toLowerCase();"
            "   if (name.includes(hint) || hint.includes(name)) { best = sel; break; }"
            "  }"
            "  if (!best) {"
            "   let maxOpts = 0;"
            "   for (const sel of selects) { if (sel.options.length > maxOpts) { maxOpts = sel.options.length; best = sel; } }"
            "  }"
            "  if (!best) continue;"
            "  for (let i = 0; i < best.options.length; i++) {"
            "   const v = best.options[i].value || '';"
            "   const t = (best.options[i].textContent||'').trim();"
            "   if (v && t && !/^(any|all|select|please)/i.test(t)) {"
            "    const fieldName = best.name || best.id || '';"
            "    const data = {};"
            "    if (fieldName) { data[fieldName] = v; }"
            "    return JSON.stringify({"
            "     action: form.action,"
            "     method: (form.method || 'GET').toUpperCase(),"
            "     data: data"
            "    });"
            "   }"
            "  }"
            " }"
            " return null;"
            " }"
        )
        try:
            raw = ev.invoke({"function": js})
            result = _parse_mcp_json(raw)
            if result and result.get("action"):
                resp = _httpx_fetch(
                    result["action"],
                    method=result.get("method") or "POST",
                    data=result.get("data") or {},
                )
                final_url = resp.get("final_url")
                # capture form replay info so code_writer can replay the search
                global _last_form_replay
                _last_form_replay = {
                    "action": result.get("action") or "",
                    "method": (result.get("method") or "POST").upper(),
                    "data": result.get("data") or {},
                    "final_url": final_url or "",
                }
                if final_url:
                    try:
                        nav.invoke({"url": final_url})
                    except Exception as exc:
                        logger.warning("select_form: navigate to result failed: %s", exc)
                    desc = f"select_form({target[:20]}) [posted]"
                else:
                    desc = f"select_form({target[:20]}) [no final_url]"
            else:
                logger.warning("select_form: no form/select found")
        except Exception as exc:
            logger.warning("select_form failed: %s", exc)
    elif a == "goto" and target:
        # domain guard: prevent the LLM from wandering off-site
        # (locumtenens: LLM guessed lw.com, kirkland.com, indeed.com)
        from urllib.parse import urlparse as _up
        goto_host = _norm_host(target)
        start_host = _norm_host(_START_URL) if _START_URL else goto_host
        if goto_host and start_host and not _same_site(target, _START_URL):
            logger.info("goto skipped (off-domain): %s (start: %s)", target[:60], _START_URL[:60])
            desc = f"goto_skipped_offdomain({target[:30]})"
        else:
            nav.invoke({"url": target})
            desc = f"goto({target[:40]})"
    elif a == "scroll":
        ev.invoke({"function": "() => window.scrollBy(0, window.innerHeight)"})
        desc = "scroll"
    if wait and a != "done":
        try:
            wait.invoke({"time": 2})
        except Exception:
            pass
    return desc


def _read_page_state_with_retry(ev, wait, *, waits=(0, 5, 8)):
    """Evaluate ``_PAGE_STATE_JS`` with progressive waits on empty results.

    JS-rendered listing pages (Coveo, React, Vue) mount their results AFTER the
    browser's ``load``/``networkidle`` events fire — the first evaluate can run
    on a page whose results container is still empty, yielding an unparseable
    result (``_parse_mcp_json`` returns ``{}``). It also covers transient MCP
    session errors: each ``ev.invoke`` runs via ``asyncio.run`` on a fresh event
    loop, so a retry gets a fresh SSE session + connection.

    Waits progressively longer between attempts (default: immediate, then 5s,
    then 8s) so a slow render has time to complete before we declare the page
    empty. Returns the parsed surface dict, or ``{}`` if every attempt fails.
    """
    for idx, delay_s in enumerate(waits):
        if delay_s and wait:
            try:
                wait.invoke({"time": delay_s})
            except Exception as wait_exc:
                logger.debug("page-state retry wait failed: %s", wait_exc)
        try:
            raw = ev.invoke({"function": _PAGE_STATE_JS})
        except Exception as exc:
            logger.warning(
                "browser_traverse: page-state evaluate error (attempt %d/%d): %s",
                idx + 1, len(waits), exc,
            )
            continue
        surface = _parse_mcp_json(raw)
        if surface:
            if idx > 0:
                logger.info(
                    "browser_traverse: page-state recovered on attempt %d (after %ss wait)",
                    idx + 1, delay_s,
                )
            return surface
    return {}


_ITEM_LINKS_JS = r"""
() => {
  // Mirror _PAGE_STATE_JS's DOM-STRUCTURE detection (not bare href-prefixing):
  // find the sibling-group container whose children anchor to distinct hrefs
  // sharing a path prefix — that is the listing's item grid. Bare href-prefix
  // grouping picks the WRONG set on pages that also list offices/filters/etc.
  // (lw.com: ~30 /offices links outnumber the 20 /people cards, but the cards
  // are the actual result grid). Returns up to 25 absolute item hrefs.
  function commonPrefixDepth(uA, uB) {
    try {
      var pa = new URL(uA, location.href).pathname.replace(/^\/+/, '').split('/');
      var pb = new URL(uB, location.href).pathname.replace(/^\/+/, '').split('/');
    } catch (e) { return 0; }
    var n = 0;
    for (var i = 0; i < Math.min(pa.length, pb.length, 5); i++) {
      if (pa[i] === pb[i] && pa[i]) n++; else break;
    }
    return n;
  }
  var NAVISH = /^(NAV|FOOTER|ASIDE|HEADER)$/i;
  var containers = document.querySelectorAll('ul,ol,div,section,tbody,main');
  var best = [], bestN = 0;
  for (var ci = 0; ci < containers.length && bestN < 60; ci++) {
    var p = containers[ci];
    if (NAVISH.test(p.tagName)) continue;
    var groups = {};
    for (var chi = 0; chi < p.children.length; chi++) {
      var c = p.children[chi];
      var cls = (c.className || '').toString().trim().split(/\s+/)[0] || '';
      cls = cls.replace(/[_-][a-z0-9]*\d[a-z0-9]*$/i, '');
      var key = c.tagName + '|' + cls;
      if (!groups[key]) groups[key] = [];
      groups[key].push(c);
    }
    for (var gk in groups) {
      var sibs = groups[gk];
      if (sibs.length < 5) continue;
      var hrefs = [];
      for (var si = 0; si < sibs.length; si++) {
        var a = sibs[si].querySelector('a[href]');
        if (a && a.href && !/^(javascript|mailto|tel|#)/i.test(a.href))
          hrefs.push(a.href.split('#')[0]);
      }
      var distinct = hrefs.filter(function (v, i, arr) { return arr.indexOf(v) === i; });
      if (distinct.length >= 5) {
        var pref = 99;
        for (var di = 1; di < distinct.length && di < 10; di++)
          pref = Math.min(pref, commonPrefixDepth(distinct[0], distinct[di]));
        if (pref >= 1 && distinct.length > bestN) { bestN = distinct.length; best = distinct; }
      }
    }
  }
  return best.slice(0, 25);
}
"""


def _extract_item_links(ev) -> list[str]:
    """Extract real item/detail hrefs from the rendered goal page via evaluate.

    Called from browser_traverse's is_listing branch while the browser is still
    on the goal page, so it sees the JS-rendered items (Coveo/React/Vue) — unlike
    a plain HTTP fetch, which only sees the pre-render template's nav links.
    Returns up to 25 absolute item URLs, or ``[]`` on any failure (non-fatal).
    """
    import json as _json

    try:
        raw = ev.invoke({"function": _ITEM_LINKS_JS})
    except Exception as exc:
        logger.warning("_extract_item_links: evaluate raised: %s", exc)
        return []
    content = getattr(raw, "content", None) or (raw if isinstance(raw, str) else str(raw))
    m = re.search(r"### Result\s*\n(.+?)(?:\n###|\Z)", content, re.DOTALL)
    blob = m.group(1).strip() if m else content.strip()
    if blob.startswith('"') and blob.endswith('"'):
        try:
            blob = _json.loads(blob)
        except Exception:
            pass
    try:
        arr = _json.loads(blob) if isinstance(blob, str) else blob
    except Exception as exc:
        logger.warning("_extract_item_links: parse failed (blob[:120]=%r): %s", str(blob)[:120], exc)
        return []
    if not isinstance(arr, list):
        logger.warning("_extract_item_links: expected list, got %s", type(arr).__name__)
        return []
    out = [str(u) for u in arr if isinstance(u, str) and u.startswith("http")]
    logger.info("_extract_item_links: parsed %d item links (raw_len=%d)", len(out), len(content))
    return out


def browser_traverse(
    start_url: str,
    content_type: str,
    query: str,
    *,
    mcp_tools: list | None = None,
    step_fn: Callable | None = None,
    max_actions: int = 12,
    trust_start_as_listing: bool = False,
) -> TraversalResult:
    """Browser-driven navigation via MCP snapshot + LLM.

    Opens the homepage in a real browser. At each step:
      1. Takes the accessibility-tree snapshot (includes hidden mega-menu items).
      2. One LLM call reads it → is this a listing? + what to click/scroll next?
      3. If listing → reached. Else execute the action (click ref / scroll).
    Bounded to ``max_actions`` steps. No custom JS for detection — the LLM judges
    from the semantic tree.
    """
    if mcp_tools is None:
        try:
            from agents.tools.playwright_tools import create_playwright_tools_sync
            mcp_tools = create_playwright_tools_sync(fresh=True)
        except Exception as exc:
            logger.warning("browser_traverse: MCP unavailable: %s", exc)
            return TraversalResult(False, None, [start_url], "unknown", None, {}, [start_url], [],
                                   f"MCP browser unavailable: {exc}")
    if not mcp_tools:
        return TraversalResult(False, None, [start_url], "unknown", None, {}, [start_url], [],
                               "MCP tools empty")

    def _tool(name):
        return next((t for t in mcp_tools if getattr(t, "name", "") == name), None)

    nav = _tool("playwright_browser_navigate")
    click_t = _tool("playwright_browser_click")
    ev = _tool("playwright_browser_evaluate")
    wait = _tool("playwright_browser_wait_for")
    snap = _tool("playwright_browser_snapshot")
    type_t = _tool("playwright_browser_type")
    if not nav or not ev or not snap:
        return TraversalResult(False, None, [start_url], "unknown", None, {}, [start_url], [],
                               "missing navigate/evaluate/snapshot tools")

    step = step_fn or llm_step

    try:
        global _START_URL, _last_form_replay
        _START_URL = start_url
        # Reset per call — the celery worker reuses one process across jobs, so a
        # stale _last_form_replay from a previous job (e.g. locumtenens' form_action)
        # otherwise leaks into this one's navigation_analysis.
        _last_form_replay = {}
        nav.invoke({"url": start_url})
        if wait:
            wait.invoke({"time": 3})
    except Exception as exc:
        logger.warning("browser_traverse: navigate failed: %s", exc)
        return TraversalResult(False, None, [start_url], "unknown", None, {}, [start_url], [],
                               f"navigate failed: {exc}")

    history: list[dict] = []
    path: list[str] = [start_url]

    for step_num in range(max_actions):
        try:
            # Use the compact _PAGE_STATE_JS evaluate (a ~1-2 KB JSON of clickables
            # + goal signals) instead of the accessibility SNAPSHOT. A heavy SPA's
            # snapshot serialization stalls the @playwright/mcp Node event loop →
            # SSE heartbeats stop → client's 90s read timeout fires ("Connection
            # closed"). The evaluate is a single fast page.evaluate() that avoids
            # that path entirely, and its `signals` give a reliable in-browser
            # goal check (no LLM judgment needed for is_listing).
            surface = _read_page_state_with_retry(ev, wait)
        except Exception as exc:
            logger.warning("browser_traverse: page-state read failed at step %d: %s", step_num, exc)
            break
        if not surface:
            logger.warning("browser_traverse: empty page-state at step %d after retries", step_num)
            break
        signals = surface.get("signals") or {}

        # Normalize URLs (scheme/netloc/path w/o trailing slash + drop query) so a
        # trailing-slash or query difference doesn't defeat the start-page checks
        # below (adameve.com/ vs adameve.com both = start page).
        from urllib.parse import urlsplit as _urlsplit

        def _norm_u(u: str):
            s = _urlsplit(u)
            return (s.scheme.lower(), s.netloc.lower(), (s.path or "").rstrip("/"))

        _on_start = _norm_u(surface.get("url") or "") == _norm_u(start_url)

        # On the start page (homepage), the DOM-repetition detector fires on the
        # site's own content grid — its counts would mislead the LLM into declaring
        # a premature listing. Suppress the counts on the start page so the LLM
        # judges by content + clickables and keeps navigating.
        # BUT for list_page/search_term, the start URL IS the listing — don't
        # suppress (the signals are accurate and needed for listing detection).
        if _on_start and not trust_start_as_listing:
            surface = {**surface, "signals": {"results_items": 0, "job_links": 0, "reached": False}}
            signals = surface["signals"]

        snap_text = _render_surface_text(surface)
        result = step(snap_text, content_type, query, history)

        # B1-veto: the LLM's is_listing judgment is FINAL. The DOM-repetition
        # detector's counts are shown in the surface text (_render_surface_text
        # includes "result_cards=N, item_links=N") so the LLM is INFORMED by
        # them — but the deterministic signal does NOT override the LLM's False.
        # This prevents false-positives when the relaxed detector (Part B1) fires
        # on non-listing card grids (blog/related-item/search-facet pages). The
        # LLM, with content_type + query context, is the better judge of "is
        # this REALLY a listing of the target type?"

        history.append(result)
        logger.info("browser_traverse: step %d — is_listing=%s action=%s target=%s signals=%s",
                     step_num, result.get("is_listing"), result.get("action"), result.get("target"), signals)

        # stuck detection: mark previous entry if same click target repeats
        if len(history) >= 2 and result.get("action") == "click" and result.get("target"):
            prev = history[-2]
            if prev.get("target") == result.get("target"):
                prev["_stuck"] = True
                # 3-strike breaker: same target clicked 3+ times → give up
                consecutive = sum(1 for h in history[-3:] if h.get("target") == result["target"])
                if consecutive >= 3:
                    logger.warning("browser_traverse: stuck on %s (%d×) — breaking",
                                   result["target"], consecutive)
                    break

        if result.get("is_listing"):
            url = surface.get("url") or start_url
            path.append(url)

            # Discover the backend data API from the LIVE browser session. The
            # browser is already on the goal page, so every XHR fired during
            # traversal is in the resource log. Two complementary signals, both
            # reusing the tested helpers traverse() already uses:
            #   1. network resource log -> XHR-on-load APIs (myntra, calvklein)
            #   2. JS-bundle scan       -> interaction-triggered APIs whose XHR
            #      never fired because no search was submitted (aya, amn — the
            #      endpoint lives as a literal in a JS bundle).
            api = _capture_api_from_session(ev, url, query)

            # Capture real item/detail hrefs from the RENDERED goal page (the
            # browser is on it now). These become url_examples for product_analyzer
            # + code_tester sample URLs — a plain HTTP fetch would only see the
            # pre-render nav links on CSR pages (Coveo/React/Vue).
            item_links = _extract_item_links(ev)
            if item_links:
                logger.info(
                    "browser_traverse: captured %d item links from rendered goal page",
                    len(item_links),
                )

            _is_div_listing = bool((signals or {}).get("div_listing"))
            # Build the discovery CONTRACT: pagination type from the surface's
            # has_load_more / scroll_hint signals (captured by _PAGE_STATE_JS).
            # These are DROPPED by the signals={"is_listing":True,...} overwrite
            # below — capture them NOW. This is the fix for the JS-listing+pagination
            # class (lw.com/Coveo): the navigator ALREADY detects the mechanism; the
            # graph just never carried it forward.
            _has_lm = bool(surface.get("has_load_more"))
            _scroll_hint = bool(surface.get("scroll_hint"))
            _rel_next_param = (surface.get("has_rel_next_page_param") or "").strip().lower()
            if _has_lm:
                _pag_type = "load_more"
            elif _rel_next_param:
                # Positive URL-pagination signal: a[rel="next"] href contains
                # ?page=N (or ?p=, ?pg=, etc.). This is stronger evidence than
                # scroll_hint (which is just a tall-page heuristic). Catches
                # desidime-class sites WITHOUT regressing lw.com/Coveo (which
                # has no ?page=N in its rel-next href).
                _pag_type = "page_param"
            elif _scroll_hint:
                # scroll_hint is a tall-page heuristic, NOT a real IS detector —
                # label it distinctly so the scraper doesn't blindly scroll tall
                # non-listing pages (Phase 4 adds a real IS probe).
                _pag_type = "infinite_scroll_tall"
            else:
                _pag_type = "page_param"
            _pagination = {
                "type": _pag_type,
                "items_per_page": int((signals or {}).get("results_items", 0)),
            }
            if _rel_next_param:
                _pagination["page_param_name"] = _rel_next_param
            _discovery = {
                "listing_url": url,
                "listing_reached": True,
                "pagination": _pagination,
            }
            return TraversalResult(
                reached=True, goal_url=url, path=path,
                mechanism="api" if api else ("ssr_div_list" if _is_div_listing else "browser_llm"),
                api=api,
                signals={"is_listing": True, "reason": result.get("reason", "")},
                visited=path, pruned=[],
                notes=f"LLM judged listing at step {step_num}: {result.get('reason', '')}",
                # propagate form replay info from select_form (so code_writer can replay)
                goal_method=_last_form_replay.get("method", "GET"),
                goal_data=_last_form_replay.get("data", {}),
                goal_request_url=_last_form_replay.get("action", ""),
                item_links=item_links,
                discovery=_discovery,
            )

        if result.get("action") == "done":
            break

        desc = _do_action(result, nav, click_t, ev, wait, type_t)
        path.append(desc)

    return TraversalResult(
        reached=False, goal_url=start_url, path=path,
        mechanism="unknown", api=None, signals={},
        visited=path, pruned=[],
        notes=f"budget exhausted after {len(history)} actions",
        # CRITICAL: a nav failure must NOT be disguised as a discovered listing.
        # listing_reached=False tells run_execution to OMIT --listing-url (so the
        # scraper's DEFAULT_LISTING_URL drives discovery, not the sample detail URL
        # that goal_url=start_url carries). This is the dominant fix for the
        # 0-item outcomes on JS-listing sites (lw.com: ~67% of runs).
        discovery={"listing_url": None, "listing_reached": False, "pagination": None},
    )


def _extract_url_from_mcp(raw) -> str:
    """Extract a URL string from an MCP evaluate result."""
    content = raw.content if hasattr(raw, "content") else str(raw)
    m = re.search(r'"(https?://[^"]+)"', content)
    return m.group(1) if m else ""
