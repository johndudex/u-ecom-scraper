"""Run the generated scraper and capture output.

This node NEVER throws.  All exceptions are caught and recorded
in ``state["execution_status"]`` so that ``cleanup`` can always run.

For browser-based scrapers, dispatches to browser_service via HTTP.
For lightweight scrapers, runs in-process via subprocess.
"""

import json
import logging
import os
import re
import select
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from src.field_mapping import union_output_fields
from src.listing_discovery import CHECKPOINT_FILENAME, CHECKPOINT_REUSE_ENV

from ..constants import STEALTH_METHOD_PREFIXES
from ..state import ScrapeState

logger = logging.getLogger(__name__)

BROWSER_METHODS = {
    "undetected_chromedriver",
    "seleniumbase_uc",
    "playwright",
    "undetected_chromedriver_scraper",
    "stealth_browser",
    "uc_chrome",
}


def _get_project_root() -> str:
    try:
        from django.conf import settings

        if hasattr(settings, "PROJECT_ROOT"):
            return str(settings.PROJECT_ROOT)
    except Exception:
        pass
    return os.getcwd()


def _get_browser_service_url() -> str:
    try:
        from django.conf import settings

        url = getattr(settings, "BROWSER_SERVICE_URL", "")
        if url:
            return url
    except Exception:
        pass
    return os.environ.get("BROWSER_SERVICE_URL", "http://browser_service:8001")


def _accepted_cli_flags(scraper_path: str) -> set[str] | None:
    """Return the long-flag names the scraper's argparse accepts.

    Done by static AST analysis of ``add_argument("--flag", ...)`` calls — no
    execution, so it works even if the scraper's deps (playwright, etc.) aren't
    importable in this container. Returns None if undeterminable (caller then
    passes args unchanged rather than risk breaking an opaque scraper).

    Why this exists: the generated scraper's argparse is LLM-written and may
    drop flags run_execution passes (e.g. ``--query`` for nav jobs,
    ``--category-url`` for list pages). An unsupported flag makes argparse
    exit(2) before any scraping happens — the scraper never even starts. This
    probe lets run_execution pass only flags the scraper actually accepts.
    [generic CLI-contract guard, no site-specific assumptions]
    """
    try:
        import ast

        with open(scraper_path, errors="ignore") as fh:
            tree = ast.parse(fh.read())
        flags: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "attr", "") == "add_argument"
            ):
                for arg in node.args:
                    if (
                        isinstance(arg, ast.Constant)
                        and isinstance(arg.value, str)
                        and arg.value.startswith("--")
                    ):
                        flags.add(arg.value[2:])
        return flags or None
    except Exception as exc:
        logger.warning("run_execution: could not parse scraper CLI flags: %s", exc)
        return None


def _draft_parse_error(scraper_path: str) -> str | None:
    """Return a formatted SyntaxError if the draft is not valid Python, else None.

    [job-329 wall] The CLI-flag probe parses the same file inside ``except
    Exception: return None`` — a draft corrupted mid-flight (abandoned writer
    threads kept editing after the tester verdict) was laundered into "flags
    undeterminable", launched, and died at compile time in 55s with zero HTTP
    requests while the RCA chased a rate band. This reporter makes corruption
    nameable BEFORE dispatch; run_execution refuses to launch on it.
    """
    import ast

    try:
        with open(scraper_path, errors="ignore") as fh:
            ast.parse(fh.read())
    except SyntaxError as exc:
        return f"scraper draft failed to compile: SyntaxError: {exc}"
    except Exception:
        return None  # unreadable/odd file: the flag probe's lenient path handles it
    return None


def _sha256_file(path: str) -> str | None:
    import hashlib

    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def _stamp_tested_draft(
    update: dict, slug: str, project_root: str | None = None
) -> None:
    """Pin ``update["tested_draft_sha256"]`` to the draft as the tester judged it.

    [job-329 wall] The tester PASSED at 05:39:40; abandoned writer threads
    edited the draft at 05:39:56/05:40:14/05:40:45; execution launched 19s
    later on a file the tester never saw. Stamping the verdict-time hash lets
    run_execution refuse any post-verdict drift. Missing draft → None (no
    false wall; the compile gate owns that case)."""
    root = project_root or _get_project_root()
    update["tested_draft_sha256"] = _sha256_file(
        os.path.join(root, "workspace", slug, "scraper_draft.py")
    )


def _filter_supported_args(
    args: list[str], accepted: set[str] | None
) -> list[str]:
    """Keep only ``--flag`` tokens (and their values) the scraper accepts.

    If ``accepted`` is None (introspection failed), return args unchanged.
    Handles ``--flag value``, ``--flag v1 v2`` (nargs="+"), and bare
    ``--store_true`` flags by consuming consecutive non-flag tokens as values.
    """
    if accepted is None:
        return args
    filtered: list[str] = []
    i = 0
    while i < len(args):
        tok = args[i]
        if tok.startswith("--") and tok[2:] in accepted:
            filtered.append(tok)
            i += 1
            # consume all consecutive values (handles nargs="+" + single value)
            while i < len(args) and not args[i].startswith("--"):
                filtered.append(args[i])
                i += 1
        elif tok.startswith("--"):
            # unsupported flag — skip it and its values
            i += 1
            while i < len(args) and not args[i].startswith("--"):
                i += 1
        else:
            # orphan value with no kept flag — drop
            i += 1
    return filtered


# ── Deterministic discovery CLI-contract check (FIX 1, plan v2) ──────────────

_ENV_LISTING_READS = (
    'os.environ.get("SCRAPER_LISTING_URL"',
    "os.environ.get('SCRAPER_LISTING_URL'",
    'os.getenv("SCRAPER_LISTING_URL"',
    "os.getenv('SCRAPER_LISTING_URL'",
)
# Signal only — NEVER satisfying (SCRAPER_FORCE_DISCOVERY is set nowhere in the
# repo; the api family's real execution trigger is the --fresh-discovery flag,
# which run_execution always appends). Critique round 1, vector 1A.
_ENV_FORCE_READS = (
    'os.environ.get("SCRAPER_FORCE_DISCOVERY"',
    "os.environ.get('SCRAPER_FORCE_DISCOVERY'",
    'os.getenv("SCRAPER_FORCE_DISCOVERY"',
)

# Seed-file markers: a main() with NONE of these cannot silently degrade to
# seed-only output (it either discovers unconditionally or crashes honestly,
# e.g. UC exits 1 with no URLs).
_SEED_FILE_MARKERS = ("input_urls.json", "INPUT_FILE")


def _strip_comments(src: str) -> str:
    return "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#")
    )


def cli_contract_violation(
    scraper_path: str, input_mode: str, strategy: str = ""
) -> str | None:
    """Static discovery CLI-contract check on a generated scraper draft.

    Returns a violation description (consumable by the L1 fix instruction and
    the L2 tester force-FAIL) or None when compliant / out of scope /
    indeterminate (NEVER blocks an opaque draft — the syntax fixer owns those).

    Contract per input_mode × strategy (the dispatcher side is
    run_execution.py's arg/env computation, lines ~205-313):

    - url_list / unknown: exempt (no Phase 1).
    - navigation / list_page: satisfied by ANY of
        M0  no seed-file branch (cannot silently degrade to seed-only)
        M1  reads the SCRAPER_LISTING_URL env (non-api strategies)
        M3  declares --listing-url AND consumes args.listing_url
            — OR, api family: declares --fresh-discovery AND consumes
            args.fresh_discovery (the flag run_execution always passes)
    - search_term: any of the above, or
        M4  declares --query AND consumes args.query
    """
    from ..constants import (
        API_STRATEGIES,
        NAV_INPUT_MODES,
        SCRAPER_ENV_LISTING,
    )

    im = (input_mode or "").strip().lower()
    if im not in NAV_INPUT_MODES:
        return None
    if not scraper_path or not os.path.isfile(scraper_path):
        return None
    accepted = _accepted_cli_flags(scraper_path)
    if accepted is None:
        return None  # unparseable → don't block; the syntax fixer owns it
    try:
        with open(scraper_path, errors="ignore") as fh:
            src = _strip_comments(fh.read())
    except OSError:
        return None

    is_api = (strategy or "").strip().lower() in API_STRATEGIES
    m0 = not any(marker in src for marker in _SEED_FILE_MARKERS)
    m1 = (not is_api) and any(f in src for f in _ENV_LISTING_READS)
    if is_api:
        m3 = "fresh-discovery" in accepted and "args.fresh_discovery" in src
    else:
        m3 = "listing-url" in accepted and "args.listing_url" in src
    m4 = im == "search_term" and "query" in accepted and "args.query" in src

    if m0 or m1 or m3 or m4:
        return None

    # Reach here = no wired discovery trigger. Name the remedy precisely.
    if is_api:
        need = 'declare --fresh-discovery and consume args.fresh_discovery (the dispatcher always passes this flag — it is the api family\'s execution trigger)'
    else:
        need = (
            f'read the {SCRAPER_ENV_LISTING} env var (the deterministic-discovery gate), '
            'or declare --listing-url and consume args.listing_url'
        )
        if im == "search_term":
            need += ', or declare --query and consume args.query'
    return (
        f"CLI CONTRACT VIOLATION (input_mode={im}, strategy={strategy or 'unknown'}): "
        f"the draft has no wired discovery trigger. Need: {need}. Declared flags: "
        f"{sorted(accepted)}. run_execution strips undeclared discovery flags "
        f"(_filter_supported_args) — execution would silently fall back to "
        f"input_urls.json (the seed file) and return only the seed count."
    )


def _needs_browser(state: ScrapeState, scraper_path: str = "") -> bool:
    """True if this scraper must run in browser_service (Chrome/Playwright).

    Uses the SAME detector as code_tester's ``run_scraper`` tool
    (``agents.tools.shell_tools._scraper_needs_browser``) so execution and
    testing can never disagree on what counts as a browser scraper — that
    disagreement is what previously routed a playwright scraper to celery
    (which has no Playwright) and crashed it instantly. The analyzer's
    ``scraping_method`` is checked first as a cheap fast-path; the shared
    whole-file scan (catches lazy / inner-function imports) is ground truth.
    """
    if state.get("scraping_method", "") in BROWSER_METHODS:
        return True
    # Lazy import avoids a module-load cycle between nodes and tools.
    from agents.tools.shell_tools import _scraper_needs_browser

    return _scraper_needs_browser(scraper_path)


def _needs_cloak(state: ScrapeState) -> bool:
    """True when the site needs CloakBrowser stealth at scrape runtime.

    Anti-bot sites (Akamai/Cloudflare) block a plain Playwright Chromium, so the
    generated scraper must drive CloakBrowser's stealth Chromium (auto-applied via
    the browser_service cloak_stealth_patch .pth when STEALTH_BROWSER=cloak). This
    is checked from several state sources because the signal lives in different
    places depending on which node last wrote it (probe_result.anti_bot.detected,
    probe_result.method, connectivity.method_that_worked, or a denormalised
    anti_bot_detected flag). Generic — no site names.
    """
    if state.get("anti_bot_detected"):
        return True
    probe = state.get("probe_result") or {}
    if isinstance(probe, dict):
        ab = probe.get("anti_bot") or {}
        if isinstance(ab, dict) and ab.get("detected"):
            return True
        conn = probe.get("connectivity") or {}
        method = (
            probe.get("method")
            or (conn.get("method_that_worked") if isinstance(conn, dict) else "")
            or ""
        )
        if isinstance(method, str) and method.startswith(STEALTH_METHOD_PREFIXES):
            return True
        # [wave-17 S4/S14] The LISTING's measured rung counts too: a site whose
        # PDP is plain-HTTP but whose listing (what discovery fetches) only
        # opens through a stealth browser rung needs cloak at runtime — the
        # classic crocs-class shape the PDP-only signal structurally missed.
        listing = probe.get("listing_connectivity") or {}
        if isinstance(listing, dict):
            lmethod = str(listing.get("method_that_worked") or "")
            if lmethod.startswith(STEALTH_METHOD_PREFIXES):
                return True
    # scraper_analysis.access_recipe (S14) is the strategy's own record of the
    # measured rung — honor it on resumes where probe_result may be sparse.
    sa = state.get("scraper_analysis") or {}
    if isinstance(sa, dict):
        recipe = sa.get("access_recipe") or {}
        if isinstance(recipe, dict) and str(recipe.get("stealth") or "") == "cloak":
            return True
    pm = state.get("probe_method") or ""
    if isinstance(pm, str) and pm.startswith(STEALTH_METHOD_PREFIXES):
        return True
    return False


_PROXY_TIERS = ("residential", "datacenter")


def _scraper_proxy_tier(state: ScrapeState) -> str:
    """The PDP's working proxy TIER, staged as SCRAPER_PROXY_TIER [wave-15 3.5].

    The generated templates read SCRAPER_PROXY_TIER for the EXTRACTION
    browser, so a site whose probe only succeeded on a proxied tier starts
    there instead of re-running the whole ladder from "none".

    [wave-17 S17] This is deliberately the PDP-only tier now. It used to be
    raised by the LISTING probe's tier (S4), which measured crocs-class sites
    wrong: listing cloak_residential + PDP cloak_datacenter staged residential
    for BOTH phases and every phase-2 fetch sat in a challenge forever. The
    listing's tier travels separately, as SCRAPER_DISCOVERY_PROXY_TIER
    (:func:`_discovery_proxy_tier`).
    """
    probe = state.get("probe_result") or {}
    method = state.get("probe_method") or ""
    if not method and isinstance(probe, dict):
        conn = probe.get("connectivity") or {}
        method = (
            probe.get("method")
            or (conn.get("method_that_worked") if isinstance(conn, dict) else "")
            or ""
        )
    method = method if isinstance(method, str) else ""
    # [wave-17 S4/S14] access_recipe FIRST — it is the strategy's own synthesis
    # of the PDP measurement (+ prior escalation; _derive_strategy raises,
    # never lowers). With S17 the listing tier lives on
    # recipe.discovery_proxy_tier and no longer feeds this phase-2 value.
    sa = state.get("scraper_analysis") or {}
    if isinstance(sa, dict):
        recipe = sa.get("access_recipe") or {}
        if isinstance(recipe, dict):
            rtier = str(recipe.get("proxy_tier") or "")
            if rtier in _PROXY_TIERS:
                return rtier
    for tier in _PROXY_TIERS:
        if method.endswith(f"_{tier}"):
            return tier
    return ""  # no signal → don't stage; templates default to "none"/ladder top


def _discovery_proxy_tier(state: ScrapeState) -> str:
    """The LISTING's working proxy TIER → SCRAPER_DISCOVERY_PROXY_TIER [S17].

    Phase-1 discovery faces the listing URL, whose measured identity can
    differ from the sample PDP's (crocs: listing cloak_residential, PDP
    cloak_datacenter). Templates launch their discovery browsers with the
    tier staged here, falling back to SCRAPER_PROXY_TIER when unset.
    """
    sa = state.get("scraper_analysis") or {}
    if isinstance(sa, dict):
        recipe = sa.get("access_recipe") or {}
        if isinstance(recipe, dict):
            rtier = str(recipe.get("discovery_proxy_tier") or "")
            if rtier in _PROXY_TIERS:
                return rtier
    probe = state.get("probe_result") or {}
    if isinstance(probe, dict):
        listing = probe.get("listing_connectivity") or {}
        if isinstance(listing, dict):
            lmethod = str(listing.get("method_that_worked") or "")
            for tier in _PROXY_TIERS:
                if lmethod.endswith(f"_{tier}"):
                    return tier
    return ""


def _stealth_env(state: ScrapeState) -> dict[str, str]:
    env = {"STEALTH_BROWSER": "cloak"} if _needs_cloak(state) else {}
    tier = _scraper_proxy_tier(state)
    if tier:
        env["SCRAPER_PROXY_TIER"] = tier
    dtier = _discovery_proxy_tier(state)
    if dtier and dtier != tier:
        # Same-tier is the common case; staging the var then would only invite
        # drift between the two values. Stage it when the classes differ.
        env["SCRAPER_DISCOVERY_PROXY_TIER"] = dtier
    return env


# [job-315 citybeach] Progress-aware wall-clock extension. EXECUTION_TIMEOUT is
# a BASE budget, not a verdict: citybeach's healthy 1,317-item extraction
# (Progress line every ~90s, 25 items per 90s) died at 72% under the flat
# 3600s backstop — extraction alone at a polite ~2.4s/item needs ~79 min.
# The template's extraction progress lines are evidence of life, so the
# monitor extends the deadline to cover the REMAINING items at a generous
# per-item allowance, clamped to EXECUTION_MAX_TIMEOUT (which must stay under
# the celery task soft time limit so a ceiling-capped run still finalizes).
# Hangs remain the stall detector's job (EXECUTION_STALL_TIMEOUT) — a hung
# scraper emits no Progress lines and no activity.
_EXTRACTION_PROGRESS_RE = re.compile(r"Progress:\s*\[(\d+)/(\d+)\]")
_EXEC_PER_ITEM_SECONDS = 12  # ~4x the observed polite pace
_EXEC_EXTENSION_SLACK_S = 600  # last page write + output serialization


def _extended_wall_clock_deadline(
    deadline: float,
    now: float,
    chunk_text: str,
    started_at: float,
    max_total: float,
) -> float:
    """Extend ``deadline`` for extraction progress seen in a stderr chunk.

    Pure arithmetic; the monitor loop calls it per chunk. Uses the LAST
    ``Progress: [k/N]`` match in the chunk. Never shortens an existing
    deadline, never extends past ``started_at + max_total``, and a completed
    total (``k == N``) extends nothing.
    """
    matches = _EXTRACTION_PROGRESS_RE.findall(chunk_text)
    if not matches:
        return deadline
    done, total = (int(g) for g in matches[-1])
    remaining = total - done
    if remaining <= 0:
        return deadline
    proposed = now + remaining * _EXEC_PER_ITEM_SECONDS + _EXEC_EXTENSION_SLACK_S
    return min(max(deadline, proposed), started_at + max_total)


def _url_shaped_criteria(state: ScrapeState) -> str:
    """The user's search_criteria when it is itself a URL (job 85's real
    listing lived ONLY in search_criteria while the job URL was a PDP).
    Multi-line criteria: the FIRST http(s) line — a mangled joined URL is
    a 404, not a listing (prod job 758)."""
    from src.seed_urls import first_criteria_url

    return first_criteria_url(state.get("search_criteria"))


def _query_shaped(url: str, query: str) -> bool:
    """True when ``url`` is the query's own search URL — the query rides in
    it, raw, ``+``-joined, or percent-encoded. [wave-50 T6c]

    A search job's discovery is owned by ``--query`` (the draft's Query-mode
    branch builds the search URL itself); an env-gate candidate that is NOT
    this URL is a navigator detour (a browsed category — briscoes /electrical/,
    job 497) and must never pin the SCRAPER_LISTING_URL gate."""
    if not (url and query):
        return False
    from urllib.parse import quote as _quote_q

    return any(
        needle and needle in url
        for needle in (query, query.replace(" ", "+"), _quote_q(query))
    )


def _scope_target(state: ScrapeState) -> int:
    """The record count this job was ASKED for (intake scope), minimum 1.

    [rag-bone job 72] scope=firstn/10 asked for 10 and got 1 — every
    zero-keyed gate read that as success. With a target, rescue paths can arm
    on "under-delivered" instead of only "delivered nothing". A job with no
    parseable firstn scope targets 1 → rescue arms on a true zero only
    (unchanged pre-job-72 behavior)."""
    try:
        if str(state.get("scope") or "").strip().lower() == "firstn":
            return max(1, int(str(state.get("scope_value") or "").strip() or 0))
    except (TypeError, ValueError):
        pass
    return 1


def listing_primary_chain(state) -> list[tuple[str, str]]:
    """[wave-61] The unified SCRAPER_LISTING_URL injection order for
    navigation/list_page jobs: URL-shaped search_criteria (job-72, the
    user's own listing assertion) → the list_page job URL (job-310 contract —
    the draft's discovery selectors were built from the job URL's shape) →
    the navigator's promotion. F17 drops each candidate against the job's
    registrable domain with fall-through (wave-32 E1a: a dropped candidate
    must not null the whole chain).

    [wave-61 demotion] When ``state["listing_probe_verdict"]`` arms (the
    Phase-1 probe measured a DEAD yield against the current primary, with a
    surviving fallback — see ``_arm_listing_probe_verdict``), the demotion
    target is hoisted over the proven-dead candidate and its note gains
    ``(demoted)``. Absent a verdict this is byte-for-byte the job-310 order
    that job 310 shipped with.

    Returns ``[(url, source_note), ...]``; empty for other input modes (the
    search_term/url_list branches keep their own arms).
    """
    mode = str(state.get("input_mode") or "")
    if mode not in ("navigation", "list_page"):
        return []
    entries: list[tuple[str, str]] = []
    _crit = _url_shaped_criteria(state)
    if _crit:
        entries.append((_crit, "criteria"))
    if mode == "list_page":
        _jl = (state.get("url") or "").strip()
        if _jl.startswith(("http://", "https://")):
            entries.append((_jl, "job_url"))
    _nav = state.get("navigation_analysis") or {}
    _disc = (_nav.get("discovery") if isinstance(_nav, dict) else None) or {}
    _promo = (_disc.get("listing_url") if isinstance(_disc, dict) else "") or ""
    if _promo:
        entries.append((_promo, "promotion"))
    _verdict = state.get("listing_probe_verdict")
    if isinstance(_verdict, dict) and _verdict.get("verdict") == "zero_yield":
        _demoted = str(_verdict.get("candidate") or "").strip()
        _target = str(_verdict.get("demotion_target") or "").strip()
        if _demoted and _target:
            _didx = next(
                (i for i, (u, _s) in enumerate(entries) if u == _demoted), -1
            )
            _tidx = next(
                (i for i, (u, _s) in enumerate(entries) if u == _target), -1
            )
            if 0 <= _didx < _tidx:
                _t_entry = entries.pop(_tidx)
                entries.insert(_didx, (_t_entry[0], f"{_t_entry[1]}(demoted)"))
    job_reg = _registrable_of(state.get("url", ""))
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for u, s in entries:
        u = str(u or "").strip()
        if not u or u in seen:
            continue
        u_reg = _registrable_of(u)
        if job_reg and u_reg and u_reg != job_reg:
            continue
        seen.add(u)
        out.append((u, s))
    return out


def listing_fallback_chain(state) -> list[str]:
    """[wave-61] The rescue/retry order once a primary is dead: the navigator
    promotion → URL-shaped criteria → the list_page job URL (job-76/85: the
    navigator-VERIFIED candidate outranks the user's assertion after death;
    the job URL joins last). F17-guarded, deduped, every verdict-dead
    candidate removed — the chain is the full 3-deep survivor list; WHICH
    survivor serves is the consumer's call (``resolve_listing_candidate``
    skips the chain head, ``_distinct_same_domain_listing`` skips the URL
    execution actually used).

    Returns ``[url, ...]``; empty for other input modes.
    """
    mode = str(state.get("input_mode") or "")
    if mode not in ("navigation", "list_page"):
        return []
    _nav = state.get("navigation_analysis") or {}
    _disc = (_nav.get("discovery") if isinstance(_nav, dict) else None) or {}
    _jl = ""
    if mode == "list_page":
        _jl_raw = (state.get("url") or "").strip()
        if _jl_raw.startswith(("http://", "https://")):
            _jl = _jl_raw
    entries = [
        (_disc.get("listing_url") if isinstance(_disc, dict) else "") or "",
        _url_shaped_criteria(state),
        _jl,
    ]
    dead: set[str] = set()
    _verdict = state.get("listing_probe_verdict")
    if isinstance(_verdict, dict):
        if _verdict.get("verdict") == "zero_yield" and _verdict.get("candidate"):
            dead.add(str(_verdict["candidate"]).strip())
        for _a in _verdict.get("attempts") or []:
            if (
                isinstance(_a, dict)
                and _a.get("dead") is True
                and _a.get("candidate")
            ):
                dead.add(str(_a["candidate"]).strip())
    job_reg = _registrable_of(state.get("url", ""))
    out: list[str] = []
    for u in entries:
        u = str(u or "").strip()
        if not u or u in out or u in dead:
            continue
        u_reg = _registrable_of(u)
        if job_reg and u_reg and u_reg != job_reg:
            continue
        out.append(u)
    return out


def resolve_listing_candidate(state) -> tuple[str, str, str]:
    """[wave-61] ``(primary, alt, source_note)`` for one state — the one
    candidate answer every consumer (execution env chain, tester injection,
    probe candidates, tester CLI hint) shares. ``alt`` is the first fallback
    survivor DISTINCT from the primary ('' when none)."""
    chain = listing_primary_chain(state)
    if not chain:
        return "", "", ""
    primary = chain[0][0]
    alt = ""
    for cand in listing_fallback_chain(state):
        if cand and cand != primary:
            alt = cand
            break
    return primary, alt, chain[0][1]


def _distinct_same_domain_listing(state: ScrapeState, primary_listing: str) -> str:
    """[job-77 RC1] The listing-fallback rescue candidate, when it is a
    DIFFERENT same-domain URL than the one execution just used.

    [wave-61] Walks the shared fallback chain (``listing_fallback_chain``:
    navigator promotion → URL-shaped criteria → job URL — the job-85
    promotion-first order preserved, the job URL added last — the 3-deep
    chain), F17-guarded and verdict-dead-filtered, skipping the URL that
    just failed. Returns "" when there is nothing better to try.
    """
    primary = str(primary_listing or "").strip()
    try:
        chain = listing_fallback_chain(state)
    except Exception:
        chain = []
    for alt in chain:
        if alt and alt != primary:
            return alt
    return ""


_PHASE1_MODES = frozenset({"navigation", "list_page", "search_term"})


def _wants_fresh_discovery(input_mode: str) -> bool:
    """[wave-30 W30-4] ``--fresh-discovery`` belongs only to jobs with a
    Phase-1 discovery leg. url_list runs the user's URLs through the seed
    path (never a discovery checkpoint, never Phase 1) — passing the flag
    there only arms wrong-branch triggers (job 570)."""
    return input_mode in _PHASE1_MODES


def _checkpoint_reuse_env(
    state: ScrapeState | None, workspace_folder: str
) -> dict[str, str]:
    """[wave-40 T15] Arm (or explicitly disarm) checkpoint reuse for one run.

    Prod 769: the tester banked 20 discovered URLs in
    ``discovered_urls_checkpoint.json``; the execution Phase 1 re-ran hours
    later, found 0 (``empty_first_page``), exited 3 DISCOVERY_ZERO and the job
    FAILED with the good URL set sitting unread in the workspace. When reuse is
    armed, this env reaches the scraper subprocess (both lanes: the in-process
    Popen env AND the browser_service ``/scrape`` ``env_overrides``) and the
    template's T14 rescue branch does the rest — on a rescuable zero it loads
    the checkpoint through ``src.listing_discovery.load_discovery_checkpoint``
    (same-host filter, age cap, yield floor) and stamps
    ``stop_reason=checkpoint_reused`` with the fresh verdict preserved.

    Returns an EXPLICIT value in BOTH directions:
    - armed      → ``{"SCRAPER_CHECKPOINT_REUSE": "1"}``
    - NOT armed  → ``{"SCRAPER_CHECKPOINT_REUSE": "0"}``

    The explicit off matters: ``browser_service`` is a separate container whose
    ambient env may carry ``SCRAPER_CHECKPOINT_REUSE=1`` from an operator
    experiment while celery's does not (and vice versa). Only an explicit
    ``"0"`` reaches the subprocess and makes the arming decision single-sourced
    here — the loader (T13) treats an UNSET var as enabled by design, so
    "don't send anything" would be "inherit whatever the other container has".

    Armed requires ALL of:
    - ambient ``SCRAPER_CHECKPOINT_REUSE != "0"`` (the operator kill switch);
    - ``input_mode`` is a Phase-1 mode — via ``_wants_fresh_discovery`` /
      ``_PHASE1_MODES`` above, the SAME frozenset the ``--fresh-discovery``
      append uses (never a second literal set);
    - the checkpoint file exists in ``workspace_folder``;
    - NOT a discover-only / force-full run. ``run_execution`` never passes
      ``--discover-only`` (that flag is the probe lane's —
      ``graph._probe_phase1_discovery_once`` builds its own env and is
      deliberately checkpoint-free), so the only force-full concept this lane
      owns is ``state["force_full"]`` — the user-declared full re-run that
      wipes the workspace and must regenerate every phase (check_tracker).
    """
    armed = (
        os.getenv(CHECKPOINT_REUSE_ENV, "0") != "0"
        and bool(workspace_folder)
        and _wants_fresh_discovery(str((state or {}).get("input_mode") or ""))
        and not bool((state or {}).get("force_full"))
        and os.path.isfile(os.path.join(workspace_folder, CHECKPOINT_FILENAME))
    )
    return {CHECKPOINT_REUSE_ENV: "1" if armed else "0"}


EXEC_TIME_BUDGET_ENV = "SCRAPER_EXEC_TIME_BUDGET"
# [wave-64 F1] Draft-side compatibility alias. The job-663 writer invented
# this name (defaulting 540 — a category-leg value) instead of consuming the
# contract env, so run_execution had no control over the bound. Both names
# now carry the same system-chosen value; templates never read the alias.
ALIAS_TIME_BUDGET_ENV = "SCRAPER_TIME_BUDGET_S"


def _item_time_budget() -> int:
    """The configured per-execution item time budget (0 ⇒ disabled)."""
    from django.conf import settings

    try:
        return int(getattr(settings, "EXECUTION_ITEM_TIME_BUDGET", 3000) or 0)
    except (TypeError, ValueError):
        return 0


def _exec_budget_env(budget_seconds: int | None = None) -> dict[str, str]:
    """[wave-63] Graceful execution time budget for one run (job 662).

    662: the tester certified the draft at ``--limit 50`` (579s) then
    execution fired the SAME scraper UNCAPPED on a catalogue-scale listing —
    the walk mathematically exceeded the runner's 3600s wall, the SIGKILL lost
    the end-of-run-only output, and a certified-good draft shipped an honest
    FAILED pc=0. The budget travels by ENV (immune to
    ``_filter_supported_args`` — no ``--limit`` ladder at execution, per the
    MUST-NOT); the templates split it into TWO deadlines (discovery gets the
    budget minus a clamped reserve; extraction may run to the full budget) so
    execution can always FINISH with rows.

    Returns an EXPLICIT value in BOTH directions (the T15 shape):
    - budget on → ``{"SCRAPER_EXEC_TIME_BUDGET": "<seconds>",
      "SCRAPER_TIME_BUDGET_S": "<seconds>"}`` — the second key is the
      [wave-64 F1] draft-side alias (job-663's writer invented that name and
      self-bounded at 540s, ignoring the contract env; both spellings are now
      system-controlled).
    - disabled  → ``{}`` — but the AMBIENT env is never a carrier here (the
      in-process Popen starts from celery's env and the bs lane stages a fresh
      subprocess env), so nothing to explicitly zero; the probe lane composes
      its own env and never sees this (probes are discovery, not execution).
    """
    budget = budget_seconds
    if budget is None:
        budget = _item_time_budget()
    try:
        budget = int(budget)
    except (TypeError, ValueError):
        return {}
    if budget <= 0:
        return {}
    return {EXEC_TIME_BUDGET_ENV: str(budget), ALIAS_TIME_BUDGET_ENV: str(budget)}


def _append_execution_note(job_id, note: str) -> None:
    """[wave-63] Run-scope note delivery to the job record.

    No note mechanism existed in run_execution (grep-verified at plan time);
    this mirrors graph.py's PDP-flip note append (ORM append +
    ``update_fields=["notes"]``). A note is instrumentation — never worth a
    run — so every failure path is swallowed.
    """
    if not job_id or not note:
        return
    try:
        from scraper.models import ScrapeJob

        _job = ScrapeJob.objects.filter(pk=job_id).first()
        if _job is not None:
            _job.notes = ((_job.notes + "\n") if _job.notes else "") + note
            _job.save(update_fields=["notes"])
    except Exception as exc:
        logger.warning("run_execution: note write failed: %s", exc)


def _note_budget_bound(job_id, coverage) -> None:
    """Deliver the bounded-run honesty note when the template reports
    ``time_budget_bounded`` in its discovery_coverage."""
    if not isinstance(coverage, dict) or not coverage.get("time_budget_bounded"):
        return
    _budget = coverage.get("exec_time_budget_seconds") or "?"
    _append_execution_note(
        job_id,
        "EXEC BUDGET: scope bounded at {}s (graceful time budget) — "
        "found={} discovered={} stop={}".format(
            _budget, coverage.get("found"), coverage.get("discovered_urls"),
            coverage.get("stop_reason"),
        ),
    )


def _heal_url_list_seed(
    scraper_path: str, input_mode: str, seed_urls, job_url: str = "",
) -> None:
    """[wave-56b F10] url_list: the JOB's seed replaces any on-disk
    input_urls.json beside the scraper BEFORE execution reads it.

    A resumed job's scraper_path points at the production ``scrapers/{slug}/``
    dir, where a PREVIOUS job's seed still sits — 592 scraped a Sep-29
    homepage seed over its submitted travel_2 payload, and cleanup rewrote
    the file 23s after the run, erasing the evidence. Both execution lanes
    read the seed from that directory (the in-process subprocess directly via
    the template's ``SCRIPT_DIR/input_urls.json``, the browser_service lane
    via ``_stage_extra_files``), so the heal lands in the file itself and
    both lanes measure the payload the user submitted. Nav families are out
    of scope: their input_urls.json is the persisted-discovery carrier the
    tester wrote, not the payload. No seed on state → no guess (the C2
    rule): the disk file stands and the scope gate still judges rows.

    [wave-56b critique fix] The healed set passes the SAME A4 filter
    setup_workspace applies (``src.seed_urls.filter_seed_urls``): healing the
    raw paste over the workspace's filtered file would reintroduce the
    cross-host/pagination strays A4 exists to keep out — and widen the C2
    scope gate's seed set so rows A4 would have dropped read as "submitted".
    Filter failure falls back to the unfiltered set (a heal must not crash
    the run).
    """
    if str(input_mode or "").strip().lower() != "url_list":
        return
    seeds = [str(u).strip() for u in (seed_urls or []) if str(u or "").strip()]
    if not seeds:
        return
    if job_url:
        try:
            from src.seed_urls import filter_seed_urls

            seeds = filter_seed_urls(seeds, job_url) or seeds
        except Exception:
            pass
    try:
        with open(
            os.path.join(os.path.dirname(scraper_path), "input_urls.json"),
            "w", encoding="utf-8",
        ) as fh:
            json.dump({"urls": seeds}, fh, indent=2, ensure_ascii=False)
    except OSError:
        pass


def _stamp_seed_provenance(
    output_file: str, seed_urls, input_mode: str,
) -> bool:
    """[wave-56b F10] Stamp ``metadata.seed_urls_used`` into the run's own
    artifact so payload identity is auditable post-hoc — a 592-shaped
    substitution must be visible in the bytes the pointer names, not only in
    logs cleanup can rotate. url_list only (the one mode whose seed IS the
    payload contract); metadata edit only, rows are never touched; called
    before the gates so a gate-failed run keeps its provenance too."""
    if str(input_mode or "").strip().lower() != "url_list":
        return False
    seeds = [str(u).strip() for u in (seed_urls or []) if str(u or "").strip()]
    if not seeds or not output_file or not os.path.isfile(output_file):
        return False
    try:
        with open(output_file, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return False
        meta = data.get("metadata")
        if not isinstance(meta, dict):
            meta = {}
            data["metadata"] = meta
        meta["seed_urls_used"] = seeds
        with open(output_file, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        return True
    except Exception:
        return False


def _stage_extra_files(
    scraper_path: str, input_mode: str, seed_urls=None, job_url: str = "",
) -> dict:
    """Sibling files the /scrape payload must carry (stateless staging — the
    browser_service container has no shared filesystem with this one).

    input_urls.json is staged UNCONDITIONALLY: it is the url_list seed AND
    the persisted-discovery carrier the nav families re-read. The discovery
    checkpoint is NOT — [wave-55 C2] it rides only where checkpoint reuse can
    be armed (the SAME ``_wants_fresh_discovery`` set the ``--fresh-discovery``
    append and ``_checkpoint_reuse_env`` key on, never a second literal set).
    Staging it for url_list handed execution the tester's ``--sample``
    discovery: job 1227 shipped 10 unrelated made-in-china products off a
    1-URL PDP seed and 1229 shipped 2 sandro strays, both stamped
    ``skipped_reason=checkpoint_loaded``. Contents go across VERBATIM — the
    T13 reader owns host/age/floor filtering at the point of use.
    """
    staged = ["input_urls.json", "discovery_config.json"]
    if _wants_fresh_discovery(input_mode):
        staged.append(CHECKPOINT_FILENAME)
    # [wave-56b F10] Stage the JOB's seed, never a stale production-dir file —
    # the heal rewrites input_urls.json before the read below, so what crosses
    # to browser_service is the payload the user submitted (592).
    _heal_url_list_seed(scraper_path, input_mode, seed_urls,
                        job_url=str(job_url or ""))
    _extra: dict = {}
    for _sf in staged:
        _sp = os.path.join(os.path.dirname(scraper_path), _sf)
        if os.path.isfile(_sp):
            try:
                with open(_sp, encoding="utf-8", errors="replace") as _fh:
                    _extra[_sf] = _fh.read()
            except OSError:
                pass
    return _extra


def _args_with_listing_url(base_args: list, alt_url: str) -> list:
    """Swap the value after an existing ``--listing-url`` flag, or append the
    flag pair when the primary run carried none."""
    out = list(base_args)
    for i, a in enumerate(out):
        if a == "--listing-url" and i + 1 < len(out):
            out[i + 1] = alt_url
            return out
    out.extend(["--listing-url", alt_url])
    return out


def _execution_zero_discovery(result: dict) -> bool:
    """[job-77 RC1] Did the execution run CLEAN but discover no usable item URLs?

    Only a clean zero warrants the listing fallback — a crash/timeout is a
    code or access problem that the strategy ladder owns, not a listing
    choice (same rule as the F6 probe retry). Reads the run's own
    ``metadata.discovery_coverage`` through the shared predicate
    (``src.listing_discovery.listing_yield_failure``): ``discovered_urls ==
    0``, the job-58 ``empty_first_page`` stop reason, or — since job 85 —
    a junk-only yield (a PDP-as-listing's 1 self link with ``found: 0``),
    which the old raw-count check read as success.
    A run flagged ``no_fresh_output`` (clean rc=0, no output file — the
    template's zero-discovery exit, job 88) qualifies with no file to read.
    """
    if (result or {}).get("no_fresh_output"):
        return True
    output_file = (result or {}).get("output_file") or ""
    if not output_file or not os.path.isfile(output_file):
        return False
    try:
        import json as _json

        with open(output_file, encoding="utf-8", errors="replace") as f:
            data = _json.load(f)
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    cov = (data.get("metadata") or {}).get("discovery_coverage") or {}
    if not isinstance(cov, dict):
        return False
    try:
        from src.listing_discovery import listing_yield_failure

        return listing_yield_failure(cov)
    except Exception:
        # Shared module unavailable — degrade to the legacy raw-count check.
        disc = cov.get("discovered_urls")
        if isinstance(disc, list):
            disc = len(disc)
        if str(cov.get("stop_reason") or "") == "empty_first_page":
            return True
        try:
            return int(disc or 0) == 0
        except (TypeError, ValueError):
            return False


def _maybe_retry_execution_listing(
    result: dict,
    state: ScrapeState,
    primary_listing: str,
    redispatch,
    job_id: int | None = None,
) -> dict:
    """[job-77 RC1] One bounded listing fallback for a clean-zero execution.

    The job-310 contract injects the list_page JOB URL as the discovery
    listing unconditionally — but when the job URL is an ITEM page (adoreme
    job 77: …/kaia-black-2-1), discovery on it can only ever yield 0, while
    the navigator had already verified and promoted the real listing
    (…/bras — 107 URLs, 10 s). Testing proved the draft works; execution was
    pointed at the one URL that cannot. Mirrors the F6 probe retry:
    re-dispatch ONCE with the promoted same-domain listing and adopt its
    result when it yields items. ``redispatch(alt_url)`` runs the whole
    execution again under the caller's control (subprocess or
    browser_service path); both attempts stay bounded at one retry.
    [job-88 selfridges] The FAILED guard admits the ``no_fresh_output``
    shape — a clean rc=0 run that wrote no output file IS the clean zero
    (its discovery found nothing to extract), and that exact run shape was
    the one where the rescue never fired.
    [wave-61 N2] Shape B (wall-clock/transport FAILED, NO output file) now
    admits too — but ONLY on direct probe evidence: the attempt-attributed
    ``listing_probe_verdict`` naming THIS primary as dead. A failed run has
    no yield evidence of its own; without the verdict it stays skipped (no
    speculative rescue on an unmeasured primary). Every admission exit
    writes a durable record (``_log_event_row`` SessionLog row + a result
    field) — 653's rescue silence was unrecoverable because the only log
    stream died mid-batch.
    """
    admissions: list[dict] = []

    def _admit(event: str, **detail) -> None:
        # [wave-61 N2] durable at EVERY exit — no silent skips remain. The
        # record rides the result dict (every return path) AND one SessionLog
        # row; 653's rescue silence was unrecoverable because the only log
        # stream died mid-batch.
        rec = {"event": event, **detail}
        admissions.append(rec)
        try:
            (result or {}).setdefault(
                "listing_rescue_admissions", []
            ).append(rec)
        except Exception:
            pass
        try:
            from agents.graph import _log_event_row

            _log_event_row(
                job_id,
                "run_execution",
                "LISTING_RESCUE_ADMISSION "
                + json.dumps(rec, default=str)[:1500],
            )
        except Exception:
            pass

    try:
        _status = str((result or {}).get("execution_status") or "")
        _no_out = bool((result or {}).get("no_fresh_output"))
        _out_file = (result or {}).get("output_file") or ""
        _zero = _execution_zero_discovery(result)
        _verdict = (
            state.get("listing_probe_verdict")
            if isinstance(state, dict) else None
        )
        _verdict_on_primary = bool(
            isinstance(_verdict, dict)
            and _verdict.get("verdict") == "zero_yield"
            and str(_verdict.get("candidate") or "").strip()
            == str(primary_listing or "").strip()
        )
        # [wave-61 N2] shape B: FAILED with no on-disk output to read — the
        # run never produced yield evidence. The probe verdict supplies it.
        _shape_b = (
            _status == "FAILED"
            and not _no_out
            and not (_out_file and os.path.isfile(_out_file))
        )
        primary_dead = _zero
        if not primary_dead and _shape_b and _verdict_on_primary:
            primary_dead = True
            _admit(
                "shape_b_admitted_by_verdict",
                execution_status=_status,
                stop_reason=str(_verdict.get("stop_reason") or ""),
                primary_listing=str(primary_listing or "")[:200],
            )
        if _status == "FAILED" and not _no_out and not primary_dead:
            _admit(
                "skip_failed_no_rescue_evidence",
                execution_status=_status,
                shape_b=_shape_b,
                verdict_on_primary=_verdict_on_primary,
            )
            return result
        if not primary_dead:
            _admit(
                "skip_not_a_clean_zero",
                execution_status=_status,
                no_fresh_output=_no_out,
            )
            return result
        alt = _distinct_same_domain_listing(state, primary_listing)
        if not alt:
            _admit(
                "skip_no_alt",
                primary_listing=str(primary_listing or "")[:200],
                shape_b=_shape_b,
            )
            return result
        logger.warning(
            "run_execution: clean zero (0 discovered URLs or no output file) "
            "under listing %s — retrying ONCE with the navigator-promoted "
            "listing %s [job-77 RC1]",
            str(primary_listing)[:80], alt[:80],
        )
        _admit(
            "rescue_fired",
            primary_listing=str(primary_listing or "")[:200],
            fallback_listing=alt[:200],
            shape_b=_shape_b,
        )
        retry_result = redispatch(alt)
        if not isinstance(retry_result, dict) or not retry_result:
            _admit("rescue_result_unusable", fallback_listing=alt[:200])
            return result
        _n = int(retry_result.get("product_count") or 0)
        if _n > 0:
            logger.info(
                "run_execution: listing fallback produced %d items (primary "
                "had 0) — adopting the retry result", _n,
            )
            _admit("rescue_adopted", fallback_listing=alt[:200], items=_n)
            # The adopted result replaces the primary's — carry the admission
            # trail over so the adopt boundary keeps the full record.
            retry_result.setdefault("listing_rescue_admissions", [])
            retry_result["listing_rescue_admissions"].extend(admissions)
            retry_result["listing_fallback"] = {
                "primary_listing": primary_listing, "fallback_listing": alt,
                "adopted": True,
            }
            return retry_result
        # The fallback also yielded nothing — keep the primary result (the
        # honest zero now rests on evidence from BOTH listings).
        logger.warning(
            "run_execution: listing fallback also produced 0 items — keeping "
            "the primary (honest zero, both listings tried)"
        )
        _admit("rescue_zero_kept_primary", fallback_listing=alt[:200])
    except Exception as exc:
        logger.warning("run_execution: listing fallback skipped: %s", exc)
        try:
            _admit("rescue_error", error=str(exc)[:300])
        except Exception:
            pass
    return result


def _cancel_job_scrapes(job_id: int) -> None:
    """[wave-33 C2] Terminal-job containment: ask browser_service to cancel
    any in-flight /scrape run(s) for this job.

    Today only the RUNNING-status watchdog cancels (tasks.py
    cleanup_stuck_jobs); a job whose dispatch failed client-side (transport
    death, infra park, rc≠0) left its server-side run browsing for nobody
    until the watchdog noticed. Fire-and-forget — never raises. job_id=0
    (local/test runs) never hits the service.
    """
    if not job_id:
        return
    try:
        from ..tools.browser_http import cancel_scrape

        cancel_scrape(int(job_id))
    except Exception as exc:
        logger.warning("run_execution: scrape cancel failed: %s", exc)


def _clear_transient_channels(result: dict) -> dict:
    """[wave-40 T1 guards 2+3] Keep the newly declared channels from going stale.

    ``browser_unavailable_detail`` and ``no_fresh_output`` used to be stripped
    from every node return by langgraph, so nothing ever accumulated in state.
    Declaring them (ScrapeState) means a value stamped by one attempt would
    otherwise ride into the NEXT attempt's routing: a stale detail would park a
    job that just produced a real report, and a stale ``no_fresh_output=True``
    would re-cycle a plain crash through the strategy ladder.

    Every run_execution exit goes through this normalization (the entry
    refusals wrap their return; the two dispatch legs clear in place once the
    leg's result is final):
    - only the infra-unavailable returns (pre-flight unhealthy, classified
      502/503, NAVIGATE_UNAVAILABLE) keep a ``browser_unavailable_detail`` —
      every other return clears it (the tester node clears its own side at its
      success stamp, graph.py);
    - only the zero-output paths (rc=3 DISCOVERY_ZERO in-process and via
      browser_service, and the clean rc=0 that wrote no output file) keep
      ``no_fresh_output=True`` — every other return, including a delivering
      run, stamps an explicit False.

    Mutates ``result`` in place and returns it, so callers may use it either
    way.
    """
    if not isinstance(result, dict):
        return result
    if not result.get("browser_unavailable_detail"):
        result["browser_unavailable_detail"] = ""
    if "no_fresh_output" not in result:
        result["no_fresh_output"] = False
    return result


def _ensure_draft_or_restore(root: str, slug: str, job_id: int, ws: Path) -> dict | None:
    """[wave-40 T10] Draft missing at execution → try THIS job's FM archive first.

    Prod 807 (papier): the tester PASSED at 0.94 with 25 products on disk, the
    draft vanished from the workspace mid-run, and the old unconditional
    refusal ("scraper_draft.py not found") turned a deliverable job into a
    FAILED — for want of a file the File Master was still holding. So the
    refusal is now the LAST resort: ``restore_job_draft`` re-hydrates this
    job's per-job archive (``scrapers/{slug}/jobs/scraper-draft-{job_id}.py``
    — the same key the writer snapshots and the tester/writer restores read)
    into the workspace before the node gives up.

    Returns ``None`` when a draft is present or was restored (execution
    proceeds), else the honest refusal state-update — its message stays
    byte-identical to the pre-T10 refusal.
    """
    from ..draft_safety import restore_job_draft

    scraper_path = os.path.join(str(ws), "scraper_draft.py")
    try:
        restored = restore_job_draft(root, slug, job_id)
    except Exception as exc:  # it swallows its own errors; belt for the seam
        logger.warning(
            "run_execution: draft restore errored before execution "
            "(job %s): %s", job_id, exc,
        )
        restored = None
    if os.path.isfile(scraper_path):
        if restored:
            logger.warning(
                "run_execution: scraper_draft.py was missing from the "
                "workspace — restored THIS job's FM archive copy %s (job %s)",
                restored, job_id,
            )
        return None
    logger.error("run_execution: scraper not found at %s", scraper_path)
    return _clear_transient_channels({
        "execution_status": "FAILED",
        "error_message": f"scraper_draft.py not found at {scraper_path}",
    })


def run_execution(state: ScrapeState) -> dict:
    from ..graph import _notify_phase

    job_id = state.get("job_id", 0)
    _notify_phase(job_id, "execution", "running")
    slug = state["site_slug"]
    root = _get_project_root()
    workspace_folder = os.path.join(root, "workspace", slug)
    scraper_path = os.path.join(workspace_folder, "scraper_draft.py")
    site_folder = os.path.join(root, "scrapers", slug)

    if not os.path.isfile(scraper_path):
        # [wave-40 T10] the draft may have been lost mid-run (prod 807) —
        # restore THIS job's FM archive before refusing.
        _refusal = _ensure_draft_or_restore(root, slug, job_id,
                                            Path(workspace_folder))
        if _refusal is not None:
            return _refusal

    # [job-329 wall] Refuse to dispatch a draft that is not valid Python —
    # fail with the real SyntaxError before any launch, never as a 55s
    # compile-death disguised as a zero-item run.
    _parse_err = _draft_parse_error(scraper_path)
    if _parse_err:
        logger.error("run_execution: %s", _parse_err)
        return _clear_transient_channels({
            "execution_status": "FAILED",
            "error_message": _parse_err,
        })

    # [job-329 wall] Freeze: refuse to execute a draft that drifted after the
    # tester verdict. What runs must be byte-identical to what was judged.
    _tested_sha = state.get("tested_draft_sha256")
    if _tested_sha:
        _current_sha = _sha256_file(scraper_path)
        if _current_sha != _tested_sha:
            logger.error(
                "run_execution: draft drift after tester verdict "
                "(tested %s…, on-disk %s…) — refusing execution",
                str(_tested_sha)[:12],
                str(_current_sha)[:12],
            )
            return _clear_transient_channels({
                "execution_status": "FAILED",
                "error_message": (
                    "scraper draft was modified after the tester verdict "
                    f"(tested sha {_tested_sha[:12]}…, on-disk sha "
                    f"{str(_current_sha)[:12]}…) — refusing to execute an "
                    "untested file"
                ),
            })

    # [wave-19 T1.1] Structural ladder gate: an HTTP-family nav draft with no
    # proxy-aware fetch path (no src.http_fetch import, no proxies= kwarg, no
    # shared-ladder call) executes unproxied and zeroes the moment the site
    # throttles the direct egress IP (job 324 myhouse) — refuse before any
    # launch, like the parse and sha walls above.
    try:
        from ..draft_safety import ladder_preservation_violation

        _ladder_violation = ladder_preservation_violation(
            scraper_path,
            state.get("input_mode", ""),
            (state.get("scraper_analysis") or {}).get("strategy", "")
            if isinstance(state.get("scraper_analysis"), dict)
            else "",
        )
    except Exception as _exc:
        logger.warning("run_execution: ladder gate errored: %s", _exc)
        _ladder_violation = None
    if _ladder_violation:
        logger.error("run_execution: %s", _ladder_violation)
        return _clear_transient_channels({
            "execution_status": "FAILED",
            "error_message": (
                f"{_ladder_violation} Refusing unproxied execution — "
                "regenerate the scraper so it fetches through the shared "
                "proxy ladder."
            ),
        })

    # NOTE: the FINAL execution always extracts the FULL result set (--sample is
    # only for code_tester validation). "sample_only" still skips the approval
    # gates for unattended runs, but must NOT cap the extraction — users expect
    # complete data (no item limits). [goal: full extraction / no limits]
    args = []

    input_mode = state.get("input_mode", "")
    search_criteria = state.get("search_criteria", "")
    # `search_criteria` semantics differ by input_mode (parse_command.py:31-36
    # is the contract): it is a production FILTER only for `search_term` jobs
    # (and url_list+criteria, which parse_command flips to search_term). For
    # navigation/list_page it is a discovery HINT, not a filter — passing
    # `--query` here collapses full taxonomy iteration into a single keyword
    # search (e.g. locumtenens: 1000+ → 25). Route navigation/list_page to
    # full discovery via the proven `--listing-url` (working results page).
    # Pre-bind for all modes (P2 fix): the env-injection block below reads
    # _working_url/_listing_reached/_respect_flag for search_term jobs too,
    # but they were previously bound only inside the navigation/list_page
    # elif → UnboundLocalError on a search_term job with empty
    # discovery.listing_url.
    _working_url = ""
    _listing_reached = False
    _respect_flag = True
    # Job 310 (pillowtalk e2e): for list_page the JOB URL is the user-provided
    # listing BY DEFINITION. The navigator may have promoted a different
    # landing page (discovery.listing_url=/collections/bestsellers/), but the
    # draft's discovery selector is built from what product_analyzer/code_writer
    # actually verified — often the job's own listing shape (search.php cards).
    # Forcing the promoted URL on a draft that can't parse it → fresh discovery
    # finds 0 URLs → 0-item execution blessed COMPLETED. So for list_page the
    # job URL outranks every navigator candidate (both the --listing-url chain
    # and the SCRAPER_LISTING_URL env gate below; F17 still domain-guards it).
    _job_listing = ""
    if input_mode == "list_page":
        _jl = (state.get("url") or "").strip()
        if _jl.startswith(("http://", "https://")):
            _job_listing = _jl
    if input_mode == "search_term" and search_criteria:
        args.extend(["--query", search_criteria])
        logger.info("run_execution: search_term job, passing --query '%s'", search_criteria)
    elif input_mode in ("navigation", "list_page"):
        _nav = state.get("navigation_analysis") or {}
        _discovery = (_nav.get("discovery") if isinstance(_nav, dict) else None) or {}
        _listing_reached = bool(_discovery.get("listing_reached", False))
        try:
            from django.conf import settings as _settings
            _respect_flag = bool(getattr(_settings, "RESPECT_LISTING_REACHED_FLAG", True))
        except Exception:
            _respect_flag = True

        if _listing_reached or not _respect_flag:
            # Nav reached the listing → pass the best listing URL. F7 chain:
            # URL-shaped search_criteria FIRST (the user's own listing
            # assertion — [rag-bone job 72] the intake UI puts the sample PDP
            # in `url` and the real listing in search_criteria; the tester
            # discovered 25 URLs through exactly this URL while execution got
            # 2 off the PDP), then the list_page job URL (job-310 contract —
            # still outranks every NAVIGATOR candidate, see _job_listing
            # above), then discovery.listing_url, then search.working_url /
            # listing_url_used (the traversal's actual landing page — blank
            # for 8/9 sites' on-disk analyses if listing_url is absent, so
            # the chain must fall through, never replace). F17: each candidate
            # domain-guarded against the job URL's registrable domain (prod
            # 331 shipped 80/80 .com.au rows under a .us job).
            _search = _nav.get("search") or {}
            _disc = (_nav.get("discovery") if isinstance(_nav, dict) else None) or {}
            _job_reg = _registrable_of(state.get("url", ""))
            _candidates = [
                _url_shaped_criteria(state),
                _job_listing,
                (_disc.get("listing_url") if isinstance(_disc, dict) else ""),
                (_search.get("working_url") if isinstance(_search, dict) else ""),
                (_search.get("listing_url_used") if isinstance(_search, dict) else ""),
            ]
            _working_url = ""
            for _c in _candidates:
                if not (isinstance(_c, str) and _c.strip()):
                    continue
                _c_reg = _registrable_of(_c)
                if _job_reg and _c_reg and _c_reg != _job_reg:
                    logger.warning(
                        "run_execution: F17 dropped cross-domain --listing-url "
                        "candidate %s (job domain %s)", _c[:70], _job_reg,
                    )
                    continue
                _working_url = _c.strip()
                break
            if _working_url:
                args.extend(["--listing-url", _working_url])
                logger.info(
                    "run_execution: navigation job, passing --listing-url (full discovery) '%s'",
                    _working_url,
                )
        else:
            # Nav did NOT reach the listing (budget exhausted / render race).
            # OMIT --listing-url so the scraper's DEFAULT_LISTING_URL (which
            # code_writer sets from the user's search_criteria) drives discovery.
            # Without this, goal_url=start_url (the sample detail URL) is passed
            # → discovery on a profile page → 0 items (the dominant failure for
            # the JS-listing+pagination class — lw.com: ~67% of runs were 0/1).
            logger.warning(
                "run_execution: navigation did NOT reach the listing "
                "(listing_reached=False) — OMITTING --listing-url; the scraper's "
                "DEFAULT_LISTING_URL will drive discovery"
            )

    # H3 (checkpoint cross-contamination): code_tester writes a
    # discovered_urls_checkpoint.json during its capped sample run, and without
    # this flag the execution phase loads it and skips Phase 1 — silently
    # extracting only the test sample (the locumtenens 38-of-3771 bug).
    # --fresh-discovery makes the scraper ignore any existing checkpoint and run
    # Phase 1 from scratch. The CLI-contract guard below drops this flag if the
    # generated scraper's argparse doesn't define it yet. [discovery-coverage-gate §4]
    # [wave-30 W30-4] The FLAG stays Phase-1 modes only: in the api family
    # _force_fresh turns it into "ignore the user's URLs, crawl the API
    # catalog instead" (job 570: a draft that promoted the flag to a Phase-1
    # trigger crashed at execution after a PASSing test cycle). [wave-55 C2]
    # url_list still writes a checkpoint though — the tester's --sample run
    # banks one (job 1227 shipped 10 unrelated products off a 1-URL PDP seed
    # through exactly this hole) — so url_list is closed at the STAGING edge
    # instead: _stage_extra_files below never ships the file to
    # browser_service, and the in-process lane deletes it before launch.
    if _wants_fresh_discovery(input_mode):
        args.append("--fresh-discovery")

    # [wave-40 T15] Checkpoint-reuse arming for THIS run. RULING R1 — the
    # --fresh-discovery append above stays UNCONDITIONAL on purpose: the flag
    # is the api family's execution trigger (see the entry contract at the top
    # of this file; it is also appended to the probe args in graph.py), so
    # gating it on reuse would break api-family execution, and skipping it
    # would push armed runs onto the template's legacy B-core resume lane
    # (stop_reason "skipped", unvalidated) instead of the validated T14 rescue
    # lane. Armed runs therefore take the FRESH-THEN-VALIDATED-RESCUE lane:
    # Phase 1 runs fresh under --fresh-discovery, and only on a rescuable zero
    # does the template's T14 branch read the staged checkpoint through
    # load_discovery_checkpoint (host filter + age cap + yield floor) and stamp
    # checkpoint_reused. The explicit "0" below disarms the other container's
    # ambient env (see _checkpoint_reuse_env).
    _ckpt_reuse_env = _checkpoint_reuse_env(state, workspace_folder)
    if _ckpt_reuse_env[CHECKPOINT_REUSE_ENV] == "1":
        logger.info(
            "run_execution: SCRAPER_CHECKPOINT_REUSE=1 — phase-1 checkpoint "
            "rescue armed (%s present)",
            CHECKPOINT_FILENAME,
        )
    # [wave-55 C2] A non-Phase-1 run can never arm the T14 rescue above, so a
    # checkpoint still in the workspace is contamination, not a rescue asset:
    # the in-process subprocess shares this filesystem and the template reads
    # any checkpoint it finds there unconditionally. The browser_service lane
    # closes the same hole by never staging the file (see _stage_extra_files).
    if not _wants_fresh_discovery(input_mode):
        _delete_discovery_checkpoint(workspace_folder, os.path.dirname(scraper_path))

    # DETERMINISTIC DISCOVERY (env-var): compute the listing URL for env-var
    # injection. This bypasses the argparse + _filter_supported_args chain —
    # code_writer's per-run argparse may or may not declare --listing-url/
    # --fresh-discovery, but env vars always reach the scraper. The template's
    # main() checks SCRAPER_LISTING_URL before the seed-file gate.
    # F7+M6: prefer discovery.listing_url (the navigator's authoritative
    # contract; working_url may be a detail page — the uindex root cause),
    # fall back to the CLI chain's value; F17 domain-guarded either way.
    _listing_url_env = ""
    if input_mode in ("navigation", "list_page"):
        # [wave-61] the unified candidate chain (criteria → list_page job URL
        # → navigator promotion; F17 per-candidate with fall-through)
        # replaces the inline job-310 ranking — same order absent a probe
        # verdict, demoted when the probe verified the primary dead. The
        # job-310 contract survives as the no-verdict order (pinned).
        _chain_env = listing_primary_chain(state)
        if _chain_env:
            _env_candidate, _env_source = _chain_env[0]
        else:
            _env_candidate, _env_source = "", ""
        if not _env_candidate:
            _env_candidate = _working_url if (_listing_reached or not _respect_flag) else ""
            _env_source = "working_url"
        _listing_url_env = _env_candidate
        if _listing_url_env:
            logger.info(
                "run_execution: setting SCRAPER_LISTING_URL=%s (env-var, "
                "deterministic discovery, source=%s)",
                _listing_url_env[:80], _env_source or "unknown",
            )
    elif input_mode == "search_term":
        # [wave-50 T6c — job 497] A search job's discovery is owned by
        # --query: the draft's Query-mode branch builds the search URL
        # (catalogsearch/result/?q=<query>) itself. The navigator's promoted
        # listing — often a CATEGORY it browsed en route (briscoes
        # /electrical/) — must NOT hijack phase 1 via the env gate: the gate
        # overrides query mode inside the draft, client-rendered categories
        # yield 0 HTTP anchors, and the rescue arm then ships
        # query-irrelevant rows. Pin the gate ONLY when the candidate IS the
        # query's own search URL; F17 domain-guard still applies.
        _nav_env = state.get("navigation_analysis") or {}
        _disc_env = (_nav_env.get("discovery") if isinstance(_nav_env, dict) else None) or {}
        _q_env = (state.get("search_criteria") or "").strip()
        _env_candidate = (_disc_env.get("listing_url") if isinstance(_disc_env, dict) else "") or ""
        if not _env_candidate:
            _env_candidate = _working_url if (_listing_reached or not _respect_flag) else ""
        if _env_candidate and not _query_shaped(_env_candidate, _q_env):
            logger.info(
                "run_execution: search_term job — NOT setting SCRAPER_LISTING_URL=%s "
                "(not the query's search URL; --query owns discovery)",
                _env_candidate[:70],
            )
            _env_candidate = ""
        _job_reg_env = _registrable_of(state.get("url", ""))
        if _env_candidate:
            _cand_reg = _registrable_of(_env_candidate)
            if _job_reg_env and _cand_reg and _cand_reg != _job_reg_env:
                logger.warning(
                    "run_execution: F17 dropped cross-domain SCRAPER_LISTING_URL "
                    "%s (job domain %s)", _env_candidate[:70], _job_reg_env,
                )
                _env_candidate = ""
        _listing_url_env = _env_candidate
        if _listing_url_env:
            logger.info("run_execution: setting SCRAPER_LISTING_URL=%s (env-var, deterministic discovery)", _listing_url_env[:80])
    elif input_mode == "url_list":
        # [wave-58 1b — job 613] A url_list job whose seeds are LISTING
        # positions is a DISCOVERY job: the seeds name categories/collections,
        # so a single-phase run extracts the listing pages themselves (3 rows
        # that ARE the 3 seeds, defaulted prices) and every downstream gate
        # that trusted url_list's item-paste premise credited it. Arm the
        # same deterministic env gate the nav modes use — the template's
        # two-phase branch then walks the listing and extracts the items
        # behind it. Item-shaped seed sets never reach this arm
        # (`url_list_discovery_seed` returns '' for them).
        try:
            from src.discovery_quality import url_list_discovery_seed
        except Exception:
            url_list_discovery_seed = None
        _seed_env = (
            url_list_discovery_seed(
                state.get("input_urls"), state.get("url", ""))
            if url_list_discovery_seed else ""
        )
        if _seed_env:
            _listing_url_env = _seed_env
            logger.info(
                "run_execution: url_list seeds are listing-shaped — setting "
                "SCRAPER_LISTING_URL=%s (deterministic discovery)", _seed_env[:80],
            )

    # Enforce scope=firstn (intake UI): cap extraction at the user's N records.
    # scope_value is a CharField → parse defensively. The _filter_supported_args
    # guard below drops --limit if the generated scraper doesn't declare it.
    if (state.get("scope") or "").strip() == "firstn":
        _sv = (state.get("scope_value") or "").strip()
        try:
            _n = int(_sv)
        except (ValueError, TypeError):
            _n = 0
        if _n > 0:
            args.extend(["--limit", str(_n)])
            logger.info("run_execution: scope=firstn, passing --limit %d", _n)

    # CLI-contract guard: drop any flag the generated scraper doesn't define,
    # so an LLM-authored argparse can't exit(2) and silently zero the output.
    # CRITICAL: if discovery-forcing flags (--listing-url for nav/list_page,
    # --query for search_term) are stripped, the scraper silently falls back to
    # reading input_urls.json (the seed file) — discovery (pagination) is never
    # called, output == seed count (the 1-item root cause). Warn loudly so the
    # operator knows discovery was suppressed.
    if args:
        accepted = _accepted_cli_flags(scraper_path)
        filtered = _filter_supported_args(args, accepted)
        if filtered != args:
            _stripped = [a for a in args if a.startswith("--") and a[2:] not in (accepted or set())]
            logger.warning(
                "run_execution: scraper doesn't accept some flags — "
                "passed %s, accepted %s, filtering to %s",
                args,
                sorted(accepted) if accepted else "unknown",
                filtered,
            )
            # Flag discovery-critical flag stripping (the root cause of 1-item
            # outputs). L3 honesty floor (CLI-contract plan v2): when critical
            # flags are stripped AND the draft has no wired discovery trigger
            # (no env gate, no consuming flag), execution would be a silent
            # seed-only run — refuse it and fail honestly instead. A COMPLIANT
            # draft (e.g. reads SCRAPER_LISTING_URL) is safe: the env carries
            # the listing and the stripped flags are behaviorally inert, so it
            # proceeds (today's behavior). Kill-switch: DISCOVERY_CONTRACT_STRICT.
            _discovery_critical = {"listing-url", "fresh-discovery", "query"}
            _stripped_critical = [f for f in _stripped if f[2:] in _discovery_critical]
            if _stripped_critical:
                logger.error(
                    "run_execution: DISCOVERY-CRITICAL flags stripped: %s — "
                    "the scraper will fall back to input_urls.json (seed file) "
                    "and discovery/pagination will NOT run. Output will be "
                    "limited to the seed count. The scraper's argparse must "
                    "declare these flags for discovery to work.",
                    _stripped_critical,
                )
                _contract = None
                try:
                    _contract = cli_contract_violation(
                        scraper_path,
                        input_mode,
                        (state.get("scraper_analysis") or {}).get("strategy", "")
                        if isinstance(state.get("scraper_analysis"), dict)
                        else "",
                    )
                except Exception as _exc:
                    logger.warning(
                        "run_execution: cli_contract_violation check errored: %s", _exc
                    )
                try:
                    from django.conf import settings as _st

                    _strict = bool(getattr(_st, "DISCOVERY_CONTRACT_STRICT", True))
                except Exception:
                    _strict = True
                if _contract and _strict:
                    return _clear_transient_channels({
                        "execution_status": "FAILED",
                        "error_message": (
                            f"{_contract} Refusing silent seed-only execution. "
                            "Remedy: regenerate the scraper (delete the cached "
                            "draft to force code_writer) or re-submit the job."
                        ),
                    })
        args = filtered

    # Execution-mode feature flag (settings.SCRAPER_EXECUTION_MODE):
    #   "auto" (default) — _needs_browser decides per-scraper; today's behavior.
    #   "force_scrape"   — always route via browser_service /scrape (rollback lane
    #                      to the subprocess model; see rework plan §4).
    #   "force_http"     — always run in-process (forces the new HTTP navigation
    #                      model even for legacy Playwright scrapers, post-migration).
    from django.conf import settings

    exec_mode = getattr(settings, "SCRAPER_EXECUTION_MODE", "auto")
    if exec_mode == "force_scrape":
        needs_browser = True
    elif exec_mode == "force_http":
        needs_browser = False
    else:
        needs_browser = _needs_browser(state, scraper_path)

    if needs_browser:
        logger.info("run_execution: browser-based scraper, dispatching to browser_service")
        # M6: pass the computed listing env through state so the browser path
        # uses the SAME value the in-process path would (single source of truth).
        _state_bs = dict(state)
        _state_bs["_listing_url_env"] = _listing_url_env

        def _redispatch_browser(alt_url: str) -> dict:
            _bs = dict(state)
            _bs["_listing_url_env"] = alt_url
            return _run_via_browser_service(
                scraper_path, _args_with_listing_url(args, alt_url),
                site_folder, _bs,
            )

        result = _run_via_browser_service(scraper_path, args, site_folder, _state_bs)

        # MULTI-SOURCE: for navigation jobs, also run the scraper against
        # watch-related category pages. A single search page often returns
        # fewer products than the site has. This re-runs with --category-url
        # for each relevant category + merges the output. Generic.
        if input_mode in ("navigation", "list_page", "search_term") and search_criteria:
            result = _run_category_sources(
                _state_bs, scraper_path, args, site_folder, result, search_criteria
            )

        # [job-77 RC1] bounded listing fallback on a clean zero (see helper).
        result = _maybe_retry_execution_listing(
            result, state, _listing_url_env or _working_url, _redispatch_browser,
            job_id=job_id,
        )
        # [wave-33 C2] the browser-service leg ended FAILED (transport death,
        # infra park, rc≠0 — whatever the fallback/merge decided): cancel any
        # in-flight server-side run for this job so it stops holding a Chrome.
        if (result or {}).get("execution_status") == "FAILED":
            _cancel_job_scrapes(job_id)
        # [wave-40 T1 guards 2+3] the leg is final — clear the transient
        # channels in place so a stale detail / no_fresh_output=True from an
        # earlier attempt cannot ride into the router (an infra return keeps
        # its own stamps; see _clear_transient_channels).
        _clear_transient_channels(result)
        return result

    def _redispatch_inprocess(alt_url: str) -> dict:
        # [wave-40 T1 r1] the fallback ADOPTS this dict verbatim when it
        # delivers (product_count>0) — a delivering run stamps neither channel,
        # and an absent key is a no-op in a LastValue channel, so the primary
        # attempt's no_fresh_output=True / browser_unavailable_detail would
        # ride into the router. Normalize here, at the adopt boundary (the
        # wave-33 source pin on `return _maybe_retry_execution_listing(` below
        # is untouched).
        return _clear_transient_channels(_run_in_process(
            scraper_path, _args_with_listing_url(args, alt_url),
            root, site_folder, workspace_folder, job_id=job_id,
            # [wave-40 T15] the armed/disarmed env rides every in-process leg.
            env_overrides={**_stealth_env(state), **_ckpt_reuse_env},
            listing_url_env=alt_url,
            input_mode=input_mode,
            # [wave-36 B1] two-vocabulary union — the scraper emits both key
            # sets pre-rename.
            target_fields=union_output_fields(state),
            seed_urls=state.get("input_urls"),
            job_url=str((state or {}).get("url") or ""),
        ))

    result = _run_in_process(
        scraper_path, args, root, site_folder, workspace_folder, job_id=job_id,
        env_overrides={**_stealth_env(state), **_ckpt_reuse_env},
        listing_url_env=_listing_url_env,
        input_mode=input_mode,
        target_fields=union_output_fields(state),
        seed_urls=state.get("input_urls"),
        job_url=str((state or {}).get("url") or ""),
    )
    # [wave-40 T1 guards 2+3] clear in place on the leg's RAW result: the
    # listing fallback below either keeps this dict (already normalized) or
    # adopts a delivering retry, which carries no stale channel values.
    _clear_transient_channels(result)
    # [job-77 RC1] bounded listing fallback on a clean zero (see helper).
    return _maybe_retry_execution_listing(
        result, state, _listing_url_env or _working_url, _redispatch_inprocess,
        job_id=job_id,
    )


def _run_category_sources(state, scraper_path, base_args, site_folder, primary_result, search_term):
    """Run the scraper against category pages related to the search term + merge.

    Generic: reads category URLs from navigation_findings.json, filters to those
    containing the search term, runs the scraper with --category-url for each,
    and merges new products into the primary result. [multi-source discovery]
    """
    import json as _json
    import os as _os

    from ..tools.browser_http import post_scrape_with_retry

    try:
        slug = state.get("site_slug", "")
        root = _os.environ.get("PROJECT_ROOT", "/app")
        cat_links: list = []
        # [job-85 supercheapauto] navigation_findings.json is one navigation
        # artifact among several — its absence used to hard-return and blind
        # this entire pass. Read whatever exists.
        nf_path = _os.path.join(root, "workspace", slug, "navigation_findings.json")
        if _os.path.isfile(nf_path):
            nf = _json.load(open(nf_path))
            # Get category URLs from homepage_nav.category_links
            hp = nf.get("homepage_nav") or {}
            cat_links = list(hp.get("category_links") or [])
        # Also check navigation_analysis categories (+ its promoted listing,
        # kept aside as a zero-rescue candidate below — re-running the
        # promotion on a HEALTHY primary would only re-discover the same URLs).
        na_path = _os.path.join(root, "workspace", slug, "navigation_analysis.json")
        _promoted = ""
        if _os.path.isfile(na_path):
            na = _json.load(open(na_path))
            na_cats = na.get("categories") or []
            if isinstance(na_cats, list):
                cat_links = cat_links + list(na_cats)
            _disc = na.get("discovery") if isinstance(na.get("discovery"), dict) else {}
            _promoted = str(_disc.get("listing_url") or "").strip()

        # Filter to categories related to the search term
        term = search_term.lower()
        relevant = []
        seen = set()
        for cat in cat_links:
            cat_url = cat if isinstance(cat, str) else (cat.get("url", "") if isinstance(cat, dict) else "")
            if not cat_url or cat_url in seen:
                continue
            if term in cat_url.lower() or (isinstance(cat, dict) and term in str(cat.get("name", "")).lower()):
                relevant.append(cat_url)
                seen.add(cat_url)

        # Load primary output products
        ct_config = state.get("content_type_config") or {}
        output_key = ct_config.get("output_key", "products") if ct_config else "products"

        primary_file = primary_result.get("output_file", "")
        primary_products = []
        if primary_file and _os.path.isfile(primary_file):
            primary_data = _json.load(open(primary_file))
            primary_products = primary_data.get(output_key, [])

        # [job-85 supercheapauto] Zero-rescue candidates: when the primary run
        # extracted NOTHING, the navigator's promoted listing and the user's
        # own URL-shaped search_criteria are direct targets — job 85's real
        # listing lived only in search_criteria while the job URL was a PDP.
        # [rag-bone job 72] "Nothing" widened to "fewer than the job asked
        # for": scope=firstn/10 delivered 1 and every zero-keyed gate read it
        # as success. Never added on a healthy primary: a top-up re-run of
        # the same listing would only re-discover the same URLs (dedup saves
        # the output, but the wall clock is real) — so candidates still skip
        # the listing the primary run already used.
        if len(primary_products) < _scope_target(state):
            _used = str(state.get("_listing_url_env") or "").strip()
            for _direct in (_promoted, _url_shaped_criteria(state)):
                _u = str(_direct or "").strip()
                if _u and _u != _used and _u not in seen:
                    logger.info(
                        "multisource: primary yielded %d items (< target %d) — "
                        "rescue candidate %s",
                        len(primary_products), _scope_target(state), _u[:70],
                    )
                    relevant.append(_u)
                    seen.add(_u)

        if not relevant:
            logger.info("multisource: no category pages matching '%s'", search_term)
            return primary_result

        logger.info("multisource: %d category pages match '%s'", len(relevant), search_term)

        existing_urls = set(p.get("url", "") for p in primary_products)
        all_products = list(primary_products)
        service_url = _os.environ.get("BROWSER_SERVICE_URL", "http://browser_service:8001")
        stealth_env = _stealth_env(state)
        # [wave-40 T15] every /scrape dispatch carries the explicit
        # armed-or-disarmed value — this multi-source lane POSTs its own payload,
        # so without the merge here it would be the one lane left inheriting the
        # browser_service container's ambient env (see _checkpoint_reuse_env).
        stealth_env.update(_checkpoint_reuse_env(state, os.path.dirname(scraper_path)))
        accepted_flags = _accepted_cli_flags(scraper_path)
        # If the scraper doesn't accept --category-url, multisource can't target
        # a category page — every run would just repeat full discovery (the
        # --query guard already stripped the flag). Skip entirely to avoid
        # several multi-minute duplicate discovery runs. [generic]
        if not accepted_flags or "category-url" not in accepted_flags:
            logger.info(
                "multisource: scraper doesn't support --category-url, skipping "
                "category runs (would duplicate primary discovery)"
            )
            return primary_result

        for cat_url in relevant[:5]:  # max 5 category pages
            logger.info("multisource: running scraper on category %s", cat_url[:60])
            try:
                cat_args = ["--category-url", cat_url]
                # Stateless /scrape: read local source, POST it; parse output content directly.
                try:
                    with open(scraper_path, encoding="utf-8", errors="replace") as _cf:
                        _cat_source = _cf.read()
                except OSError:
                    _cat_source = ""
                # W8: 429/502 backpressure used to land here un-checked — the
                # error body became output_content="" and the category was
                # silently skipped, under-counting the merged output.
                # [wave-63] Budget SCALED per leg: min(item budget, leg wall
                # 600s − 60s finalize margin) ⇒ 540 today — the clamped-reserve
                # template math keeps the leg's discovery window alive (B4).
                _res = post_scrape_with_retry(
                    f"{service_url}/scrape",
                    {"scraper_source": _cat_source, "scraper_name": os.path.basename(scraper_path), "args": cat_args, "timeout": 600, "env_overrides": {**stealth_env, **_exec_budget_env(min(
                        _item_time_budget(), 600 - 60
                    ))}},
                    timeout=620,
                )
                if not _res.ok:
                    logger.warning(
                        "multisource: category %s skipped (%s: %s)",
                        cat_url[:40], _res.error_class, _res.error,
                    )
                    continue
                cat_result = _res.data
                cat_content = cat_result.get("output_content") or ""
                if cat_content:
                    try:
                        cat_data = _json.loads(cat_content)
                    except Exception:
                        cat_data = {}
                    cat_products = cat_data.get(output_key, [])
                    new_count = 0
                    for p in cat_products:
                        url = p.get("url", "")
                        if url and url not in existing_urls:
                            if p.get("price"):  # only priced products
                                all_products.append(p)
                                existing_urls.add(url)
                                new_count += 1
                    if new_count:
                        logger.info("multisource: %s → %d new products (total %d)", cat_url[:40], new_count, len(all_products))
            except Exception as exc:
                logger.warning("multisource: category %s failed: %s", cat_url[:40], exc)

        # Write merged output
        if len(all_products) > len(primary_products):
            from datetime import datetime, timezone
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
            merged_path = _os.path.join(site_folder, f"output_merged_{timestamp}.json")
            site_block = primary_data.get("site", {}) if primary_file else {}
            # T0.5/H1: normalize the string-`site` template shape — a merged
            # output re-deriving from a string would re-break every reader.
            if not isinstance(site_block, dict):
                from src.output_site import normalize_site_block

                site_block = normalize_site_block(site_block) or {}
            # discovery_coverage: Phase 5 will fully aggregate across sources
            # (sum found, max dimensions_total, stop_reason precedence). For now
            # propagate the primary source's block as-is so the gate has something
            # to read on the merged output. [discovery-coverage-gate §6, H2]
            primary_coverage = _read_discovery_coverage(primary_file)
            merged_meta: dict = {
                "scraping_method": "multisource_playwright",
                "sources": 1 + min(len(relevant), 5),
                "total_products": len(all_products),
                "merged": True,
            }
            if isinstance(primary_coverage, dict):
                merged_meta["discovery_coverage"] = primary_coverage
            merged = {
                "site": site_block,
                output_key: all_products,
                "metadata": merged_meta,
            }
            with open(merged_path, "w") as f:
                _json.dump(merged, f, indent=2, ensure_ascii=False, default=str)
            logger.info("multisource: merged output → %d total products → %s", len(all_products), merged_path)
            return {
                "execution_status": "SUCCESS",
                "output_file": merged_path,
                "product_count": len(all_products),
                "discovery_coverage": primary_coverage,
                "error_message": "",
            }

        return primary_result
    except Exception as exc:
        logger.warning("multisource: failed: %s", exc)
        return primary_result


def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGKILL the scraper's whole process group so its Chrome grandchildren die
    with it.

    ``proc.kill()`` only SIGKILLs the python3 child; the browser subprocesses it
    spawned (Playwright/Selenium connecting to/launching Chrome) are in a
    different process group and survive as orphans — holding the shared Scraper
    Chrome (9223) and wedging subsequent scrapers. ``start_new_session=True`` on
    the Popen makes the child its own process-group leader, so ``os.getpgid`` is
    the child's pgid and ``killpg`` reaches everyone it spawned.

    Safe to call on an already-exited process: a dead leader's pgid is gone, so
    we fall back to ``proc.kill()`` (which is itself a no-op if already reaped).
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:
            pass


def _run_in_process(
    scraper_path: str, args: list[str], cwd: str, site_folder: str,
    workspace_folder: str = "", job_id: int = 0,
    env_overrides: dict[str, str] | None = None,
    listing_url_env: str = "",
    input_mode: str = "",
    target_fields: list[str] | None = None,
    seed_urls: list | None = None,
    job_url: str = "",
) -> dict[str, Any]:
    cmd = ["python3", scraper_path] + args
    start = time.time()
    # [wave-56b F10] The subprocess reads the seed from the scraper's own
    # directory — heal a stale production-dir input_urls.json with the JOB's
    # seed before dispatch (592 measured a Sep-29 homepage payload).
    _heal_url_list_seed(scraper_path, input_mode, seed_urls,
                        job_url=str(job_url or ""))

    # The in-process subprocess can run 30+ min on a long scrape (the HTTP
    # navigation template's 200-page discovery is exactly that). The Celery
    # watchdog (cleanup_stuck_jobs, tasks.py) reaps any RUNNING job whose
    # newest SessionLog row is older than STUCK_JOB_ACTIVITY_TIMEOUT_MINUTES
    # (30 min), so a bare subprocess.run is marked FAILED mid-scrape with the
    # misleading "Worker process crashed ... Likely OOM killed" message even
    # though the subprocess is still running. Mirror _run_via_browser_service's
    # heartbeat (240 s) so the watchdog sees activity throughout. Lazy import
    # avoids a circular dependency (graph imports this node module).
    hb = None
    try:
        from webapp.agents.graph import _start_heartbeat

        if job_id:
            hb = _start_heartbeat(job_id, "run_execution", interval=240, prefix="[EXEC-ALIVE]")
    except Exception as _hb_exc:
        logger.warning("run_execution: heartbeat start failed: %s", _hb_exc)

    try:
        # Stall-bound execution — applies to ALL scrapers (template AND custom
        # code_writer code). The template DISCOVERY_DEADLINE_SECONDS only governs
        # template scrapers; a custom scraper had no internal bound and could hang
        # for the full wall-clock timeout on a JS-blocked/rate-limited site. Monitor
        # stderr (scrapers log page/item progress there): if no output for
        # EXECUTION_STALL_TIMEOUT the scraper is stalled → kill it. Hard backstop
        # EXECUTION_TIMEOUT. Binary pipes + os.read so a partial line can't block
        # the stall timer.
        from django.conf import settings as _settings
        _stall = getattr(_settings, "EXECUTION_STALL_TIMEOUT", 300)
        _hard = getattr(_settings, "EXECUTION_TIMEOUT", 3600)
        _max_total = getattr(_settings, "EXECUTION_MAX_TIMEOUT", 9600)
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd,
            env={**os.environ, **(env_overrides or {}),
                 # [wave-63] Graceful execution time budget (job 662) — every
                 # in-process scraper subprocess gets it (this lane is the
                 # DEFAULT for the HTTP family).
                 **_exec_budget_env(),
                 # DETERMINISTIC DISCOVERY: set SCRAPER_LISTING_URL for nav/list_page
                 # jobs so the template's main() runs Phase 1 discovery (env-var gate)
                 # regardless of CLI flags (which code_writer may not declare). This
                 # bypasses the argparse + _filter_supported_args chain that silently
                 # suppresses discovery → output == seed count (the 1-item root cause).
                 **({"SCRAPER_LISTING_URL": listing_url_env} if listing_url_env else {})},
            # Make the scraper its own process-group leader so a timeout/stall
            # kill can reach its Chrome grandchildren too. Without this, killpg
            # can't target them and proc.kill() only reaps the python3 child,
            # leaving browser subprocesses orphaned (holding the shared Chrome).
            start_new_session=True,
        )
        err_fd = proc.stderr.fileno()
        stderr_buf = bytearray()
        last_activity = time.time()
        stall_reason = ""
        deadline = start + _hard  # progress lines extend this (job-315)
        while True:
            ready, _, _ = select.select([proc.stderr], [], [], 5.0)
            if ready:
                chunk = os.read(err_fd, 65536)
                if chunk:
                    last_activity = time.time()
                    stderr_buf.extend(chunk)
                    if len(stderr_buf) > 200_000:
                        del stderr_buf[:100_000]
                    # [job-315] "Progress: [k/N]" on stderr = measured life.
                    # Extend the wall-clock to budget the remaining items —
                    # bookkeeping must never kill the monitor, hence try/except.
                    try:
                        _new_deadline = _extended_wall_clock_deadline(
                            deadline, time.time(),
                            chunk.decode("utf-8", "replace"), start, _max_total,
                        )
                        if _new_deadline > deadline + 1:
                            logger.info(
                                "run_execution: extraction progress extended "
                                "wall-clock deadline to +%ds (ceiling %ds)",
                                int(_new_deadline - start), _max_total,
                            )
                        deadline = _new_deadline
                    except Exception as _ext_exc:
                        logger.warning(
                            "run_execution: deadline extension check failed: %s",
                            _ext_exc,
                        )
                elif proc.poll() is not None:
                    break  # EOF and process exited
            elif proc.poll() is not None:
                break  # no output this tick but process exited
            if time.time() - last_activity > _stall:
                stall_reason = (
                    f"scraper stalled — no output for {_stall}s "
                    f"(likely hung/blocked on a JS or rate-limited site)"
                )
                _kill_process_group(proc)
                break
            if time.time() > deadline:
                stall_reason = (
                    f"scraper exceeded {int(time.time() - start)}s wall-clock "
                    f"(base {_hard}s, progress-extension ceiling {_max_total}s)"
                )
                _kill_process_group(proc)
                break
        try:
            proc.communicate(timeout=30)  # drain + reap the killed/finished process
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
        returncode = proc.returncode if proc.returncode is not None else -1
        elapsed = round(time.time() - start, 2)
        stderr = stderr_buf.decode("utf-8", "replace")

        if stall_reason:
            logger.error("run_execution: %s after %ds", stall_reason, elapsed)
            # [wave-63] Salvage flushed rows before the honest FAILED — the
            # floor (subprocess start) is LOAD-BEARING here: the tester's
            # output lives in this same workspace (prod 319 class).
            _salvage = _inprocess_partial_harvest(
                workspace_folder, site_folder, start, job_id,
                why=(
                    "budget_wall_timeout" if "wall-clock" in stall_reason
                    else "stall"
                ),
            )
            if _salvage:
                _msg = f"{stall_reason}. Last output: {stderr[-4000:]}"
                if "wall-clock" in stall_reason:
                    _msg += " [budget_wall_timeout]"
                _salvage["error_message"] = _msg
                return _salvage
            return {
                "execution_status": "FAILED",
                "error_message": f"{stall_reason}. Last output: {stderr[-4000:]}",
            }

        logger.info("run_execution: scraper exited with code %d in %ds", returncode, elapsed)

        if returncode != 0:
            # [T2.10/wave-13] The template's DISCOVERY_ZERO exit (rc=3) is a
            # ZERO-DISCOVERY verdict, not a crash: Phase 1 ran cleanly and saw
            # no item URLs under the given listing. Tag it no_fresh_output so
            # the RC1 listing fallback fires, and attach a FAIL-class
            # discovery_coverage so _route_after_execution can recycle the
            # strategy — a bare FAILED here sent the clean-zero shape straight
            # to cleanup (job-85's trap).
            if returncode == 3 or "DISCOVERY_ZERO" in stderr:
                logger.error(
                    "run_execution: DISCOVERY_ZERO (rc=%d) — Phase 1 found no "
                    "item URLs; tagging zero-discovery for fallback/recycle",
                    returncode,
                )
                return {
                    "execution_status": "FAILED",
                    "no_fresh_output": True,
                    "discovery_coverage": {
                        "ran_phase1": True,
                        "discovered_urls": 0,
                        "stop_reason": "empty_first_page",
                        "zero_discovery_exit": True,
                    },
                    "error_message": (
                        f"Scraper exited with code {returncode}. {stderr[-4000:]}"
                    ),
                }
            # [wave-63] Crash salvage: a nonzero exit may still have flushed
            # partial rows (every-25 flush) — record them under an honest
            # FAILED instead of dropping them. rc==3 never reaches here.
            _salvage = _inprocess_partial_harvest(
                workspace_folder, site_folder, start, job_id, why="crash"
            )
            if _salvage:
                _salvage["error_message"] = (
                    f"Scraper exited with code {returncode}. {stderr[-4000:]}"
                )
                return _salvage
            return {
                "execution_status": "FAILED",
                "error_message": f"Scraper exited with code {returncode}. {stderr[-4000:]}",
            }

        _slug = os.path.basename(site_folder.rstrip("/")) if site_folder else None
        # F8: floor = subprocess start so only files THIS run wrote are eligible
        # (prod 319 shipped code_tester's sample written 32s BEFORE execution).
        # A floored call returns "" when nothing passed — the caller below fails
        # the run instead of crediting a stale/absent file. No FM fallback here:
        # an FM download lands in a fresh-mtime tmpfile that launders stale
        # content through any floor.
        output_file = _find_newest_output(
            workspace_folder, site_folder, slug=None, mtime_floor=start
        )
        if not output_file:
            return {
                "execution_status": "FAILED",
                # [job-88 selfridges] A clean rc=0 run that wrote NOTHING is the
                # zero-discovery shape by template contract (http_navigation
                # exits 0 on zero discovery) — mark it so the RC1 listing
                # fallback can fire; without the flag both guards below
                # short-circuited and the rescue was structurally unreachable.
                "no_fresh_output": True,
                "error_message": (
                    "Execution produced no output file (rc=0 but no fresh "
                    "output_*.json with mtime >= subprocess start; discovery "
                    "likely found 0 items or the scraper wrote nothing)."
                ),
            }
        # [wave-56b F10] Payload identity rides the artifact itself — stamped
        # before the gates so a gate-failed run keeps its provenance too.
        _stamp_seed_provenance(output_file, seed_urls, input_mode)
        product_count = _count_products(output_file)
        discovery_coverage = _read_discovery_coverage(output_file)
        # [wave-63] Deliver the bounded-run honesty note (instrumentation —
        # the run itself is judged by the normal gates).
        _note_budget_bound(job_id, discovery_coverage)
        # F9 quality gate (nav modes): collapse-level failure rates -> FAILED
        _q = _extraction_quality_gate(
            output_file, input_mode, product_count,
            target_fields=target_fields,
        )
        if _q:
            # [wave-19 T1.9] same diagnosis as the subprocess path — the
            # in-process run burns fetches on malformed hosts just the same.
            _hy = _hygiene_diagnosis(output_file, listing_url_env)
            if _hy:
                _q += _hy
            return {
                "execution_status": "FAILED",
                "output_file": output_file,
                "product_count": product_count,
                "discovery_coverage": discovery_coverage,
                "error_message": _q,
            }

        # [wave-55 C2] Nav-tree junk bar + non-item row bar, mirroring the
        # browser_service lane's ordering (F9 -> shallow -> nonitem -> scope).
        # This lane is the DEFAULT for the HTTP family (requests / api /
        # http_navigation), so a url_list leg here shipped /#main shell rows
        # and shallow-section rows with no junk verdict at all — F9 exempts
        # url_list by design. listing_url_env is this lane's carried anchor
        # (state url's equivalent); url_list has none, so the gate takes the
        # seed's own URL.
        _sh = _shallow_section_gate(
            output_file, listing_url_env, input_mode, seed_urls=seed_urls,
        )
        if _sh:
            logger.error("run_execution: %s", _sh)
            return {
                "execution_status": "FAILED",
                "output_file": output_file,
                "product_count": product_count,
                "discovery_coverage": discovery_coverage,
                "error_message": _sh,
            }
        _ni = _nonitem_row_gate(output_file, input_mode)
        if _ni:
            logger.error("run_execution: %s", _ni)
            return {
                "execution_status": "FAILED",
                "output_file": output_file,
                "product_count": product_count,
                "discovery_coverage": discovery_coverage,
                "error_message": _ni,
            }

        # [wave-55 C2] Post-run scope assertion for the one mode whose
        # discovery the user owns. Same verdict shape as F9 above: fail the
        # run, never edit rows.
        _us = _url_list_scope_gate(output_file, seed_urls, input_mode)
        if _us:
            logger.error("run_execution: %s", _us)
            return {
                "execution_status": "FAILED",
                "output_file": output_file,
                "product_count": product_count,
                "discovery_coverage": discovery_coverage,
                "error_message": _us,
            }

        # [wave-58 1d] Listing-seed expansion bar: rows==seeds on a
        # listing-shaped seed set means discovery never ran (the 613 junk
        # pass). Same verdict shape — honest FAILED, no row edits.
        _lse = _listing_seed_expansion_gate(output_file, seed_urls, input_mode)
        if _lse:
            logger.error("run_execution: %s", _lse)
            return {
                "execution_status": "FAILED",
                "output_file": output_file,
                "product_count": product_count,
                "discovery_coverage": discovery_coverage,
                "error_message": _lse,
            }

        # H3: in-process path owns its own checkpoint cleanup (no browser_service
        # to do it). Delete so a subsequent invocation starts fresh. Tried in both
        # the workspace dir (SCRIPT_DIR location) and the subprocess cwd, since
        # the checkpoint location varies by template. [discovery-coverage-gate §4]
        _delete_discovery_checkpoint(workspace_folder, cwd)

        return {
            "execution_status": "SUCCESS",
            "output_file": output_file or "",
            "product_count": product_count,
            "discovery_coverage": discovery_coverage,
            "error_message": "",
        }
    except Exception as exc:
        logger.exception("run_execution: unexpected error")
        return {
            "execution_status": "FAILED",
            "error_message": str(exc),
        }
    finally:
        if hb is not None:
            try:
                from webapp.agents.graph import _stop_heartbeat

                _stop_heartbeat(hb)
            except Exception:
                pass


def _run_via_browser_service(
    scraper_path: str, args: list[str], site_folder: str, state: ScrapeState | None = None
) -> dict[str, Any]:
    import httpx

    # QW-0 Step emits (deferred like the heartbeat import below — graph
    # imports this node module).
    from ..graph import _notify_phase
    from ..tools.browser_http import (
        PREFLIGHT_MAX_WAIT_S,
        bounded_scrape_attempts,
        post_scrape_with_retry,
        wait_for_browser_service,
    )

    _exec_job_id = (state or {}).get("job_id", 0)

    service_url = _get_browser_service_url()

    # [wave-16 B3] Pre-flight: bounded wait for browser_service /health before
    # handing it a full run. A short blip self-heals inside the window; a real
    # outage parks the job (non-terminal, beat-resumed) instead of feeding a
    # dead gateway a full execution that reads as a strategy failure.
    if not wait_for_browser_service():
        _notify_phase(_exec_job_id, "execution", "failed")
        return {
            "execution_status": "FAILED",
            "browser_unavailable_detail": (
                f"browser_service unhealthy before execution "
                f"(waited {int(PREFLIGHT_MAX_WAIT_S)}s for /health)"
            ),
            # [wave-16 B3] Normalized into the coverage verdict so
            # _route_after_execution parks this with every other infra
            # failure (it routes on stop_reason alone).
            "discovery_coverage": {
                "ran_phase1": False,
                "discovered_urls": 0,
                "stop_reason": "navigate_unavailable",
            },
        }
    stealth_env = _stealth_env(state) if state else {}
    # DETERMINISTIC DISCOVERY: inject SCRAPER_LISTING_URL into the browser_service
    # env_overrides so it reaches the scraper subprocess. M6: single source of
    # truth — the caller (run_execution) computes `_listing_url_env` once and
    # stashes it on state; this path used to independently re-derive from
    # navigation_analysis, so any change to the computation had to be made
    # twice (and the two could silently diverge).
    _listing_env_bs = (state or {}).get("_listing_url_env") or ""
    if not _listing_env_bs:
        _nav_bs = (state or {}).get("navigation_analysis") or {}
        _disc_bs = (_nav_bs.get("discovery") if isinstance(_nav_bs, dict) else None) or {}
        _listing_env_bs = (_disc_bs.get("listing_url") if isinstance(_disc_bs, dict) else "") or ""
    if _listing_env_bs and (
        (state or {}).get("input_mode") in ("navigation", "list_page", "search_term")
        # [wave-58 1b] a listing-seed url_list run IS a discovery run — the
        # gate armed `_listing_url_env` upstream, so the browser lane must
        # carry it too (M6's single source of truth, no re-derivation).
        or ((state or {}).get("input_mode") == "url_list" and _listing_env_bs
            == ((state or {}).get("_listing_url_env") or ""))
    ):
        stealth_env["SCRAPER_LISTING_URL"] = _listing_env_bs
    # [wave-40 T15] Arm (or explicitly disarm) checkpoint reuse on the /scrape
    # payload env_overrides — browser_service forwards them into the scraper
    # subprocess, and the T14 template rescue branch reads the value there. The
    # explicit "0" defeats a stale ambient SCRAPER_CHECKPOINT_REUSE in the
    # browser_service container (see _checkpoint_reuse_env). The checkpoint
    # file itself rides the extra_files staging tuple below (VERBATIM — no seed
    # filtering; the T13 reader does the host/age/floor filtering), because the
    # browser_service container has no shared filesystem with this one.
    stealth_env.update(_checkpoint_reuse_env(
        state, os.path.dirname(scraper_path)
    ))
    logger.info(
        "run_execution: dispatching to browser_service at %s: %s (cloak=%s)",
        service_url,
        scraper_path,
        bool(stealth_env),
    )

    # Execution fail-fast bound (same EXECUTION_TIMEOUT backstop as the in-process
    # path — bounds a hung browser_service scrape, applies to all scrapers).
    from django.conf import settings as _settings
    timeout = getattr(_settings, "EXECUTION_TIMEOUT", 3600)
    # The /scrape call blocks for the whole run (a 60-100 item scrape takes
    # 15-20 min). The celery watchdog (cleanup_stuck_jobs) kills jobs with no
    # SessionLog activity for STUCK_JOB_ACTIVITY_TIMEOUT_MINUTES, so without a
    # heartbeat it would mark this job failed mid-scrape. Reuse the graph
    # heartbeat to keep the watchdog informed. Lazy import avoids a circular
    # dependency (graph imports this node module).
    hb = None
    try:
        from webapp.agents.graph import _start_heartbeat, _stop_heartbeat

        _job_id = (state or {}).get("job_id", 0)
        if _job_id:
            hb = _start_heartbeat(_job_id, "run_execution", interval=240, prefix="[EXEC-ALIVE]")
    except Exception as _hb_exc:
        logger.warning("run_execution: heartbeat start failed: %s", _hb_exc)

    try:
        # Stateless /scrape: read the local scraper source, POST it; browser_service
        # returns output CONTENT (no shared FS). Persist it next to the scraper
        # draft (workspace/{slug}/) so downstream reads + _finalize_job's
        # workspace→scrapers promotion keep working unchanged.
        try:
            with open(scraper_path, encoding="utf-8", errors="replace") as _f:
                _source = _f.read()
        except OSError as exc:
            return {
                "execution_status": "FAILED",
                "error_message": f"Could not read scraper source {scraper_path}: {exc}",
            }
        # Read sibling files (input_urls.json, discovery_config.json) for
        # staging [wave-40 T15] — see _stage_extra_files for the checkpoint's
        # own Phase-1-only rule (content goes across VERBATIM either way: no
        # filter_seed_payload, src.listing_discovery.load_discovery_checkpoint
        # owns host/age/floor filtering at the point of use). This is the
        # EXECUTION lane's staging; the probe lane's own loop in
        # graph._probe_phase1_discovery_once deliberately does NOT stage a
        # checkpoint — a probe must never see one (763 nastygal).
        _extra = _stage_extra_files(
            scraper_path, str((state or {}).get("input_mode") or ""),
            seed_urls=(state or {}).get("input_urls"),
            job_url=str((state or {}).get("url") or ""),
        )
        # W8: bounded retry on 429/502/503/504 + transport errors — a bare
        # raise_for_status() turned backpressure into an execution failure.
        # [wave-33 C3] crash-retries stay load-bearing but are budget-bounded
        # (2 attempts for the 3600s execution: one F2/F3 crash recovery).
        _res = post_scrape_with_retry(
            f"{service_url}/scrape",
            {
                "scraper_source": _source,
                "scraper_name": os.path.basename(scraper_path),
                "extra_files": _extra,
                "args": args,
                "timeout": timeout,
                "max_retries": bounded_scrape_attempts(timeout),
                # [wave-63] budget env rides the payload (merged, not mutated —
                # stealth_env is reused by the category-leg lane below).
                "env_overrides": {**stealth_env, **_exec_budget_env()},
            },
            timeout=timeout + 60,
        )

        # ScrapeResult (browser_http), not httpx.Response — the W8 migration
        # missed this site and .status_code AttributeError'd here (prod job 51
        # class). error_class is the 404 bucket ("not_found").
        if _res.error_class == "not_found":
            return {
                "execution_status": "FAILED",
                "error_message": "Scraper rejected by browser_service (source invalid)",
            }

        # [wave-16 B3] The gateway itself named an infra class (chrome_crash /
        # memory_pressure / resource) on its classified 502/503 body, or was
        # unreachable outright after the full retry ladder. The strategy was
        # never given a fair window — route to the dependency park, never the
        # strategy ladder (a strategy switch against dead infra lies).
        if _res.unavailable:
            _notify_phase(_exec_job_id, "execution", "failed")
            return {
                "execution_status": "FAILED",
                "browser_unavailable_detail": _res.error
                or f"browser_service unavailable (error_class={_res.server_error_class})",
                # Same normalization as the pre-flight arm above: infra
                # failures read as navigate_unavailable coverage, which parks.
                "discovery_coverage": {
                    "ran_phase1": False,
                    "discovered_urls": 0,
                    "stop_reason": "navigate_unavailable",
                },
            }

        if not _res.ok:
            return {
                "execution_status": "FAILED",
                "error_message": _res.error,
            }
        result = _res.data

        if result.get("returncode", 0) != 0:
            # TAIL + 4000: the exception line lives at the END of a traceback;
            # head-truncation kept the banner and cut the actual error (prod
            # 351: "page.goto timeout" was invisible — 2000 chars of log start
            # ate the whole budget). TextField on the model — no DB cap.
            stderr = result.get("stderr", "")[-4000:]
            # [T2.10/wave-13] Same DISCOVERY_ZERO tagging as the in-process
            # path: the template's rc=3 exit is a zero-discovery verdict, not
            # a crash — the listing fallback and the strategy recycle must
            # both see it.
            # [wave-16 B3] The template prints NAVIGATE_UNAVAILABLE on stderr
            # when discovery ended zero because browser_service /navigate was
            # itself down — an infrastructure verdict, not a site one. Reclass
            # the stop_reason and carry the detail so _route_after_execution
            # parks the job instead of recycling the strategy ladder.
            _stderr = result.get("stderr") or ""
            _nav_unavailable = "NAVIGATE_UNAVAILABLE:" in _stderr
            if result.get("returncode") == 3 or "DISCOVERY_ZERO" in _stderr:
                logger.error(
                    "run_execution: DISCOVERY_ZERO (browser_service, rc=%s%s) — "
                    "tagging zero-discovery for fallback/recycle",
                    result.get("returncode"),
                    ", browser_service unavailable" if _nav_unavailable else "",
                )
                _cov = {
                    "ran_phase1": True,
                    "discovered_urls": 0,
                    "stop_reason": "empty_first_page",
                    "zero_discovery_exit": True,
                }
                _failed = {
                    "execution_status": "FAILED",
                    "no_fresh_output": True,
                    "discovery_coverage": _cov,
                    "error_message": (
                        f"Scraper exited with code {result['returncode']}. {stderr}"
                    ),
                }
                if _nav_unavailable:
                    _marker = ""
                    for _line in _stderr.splitlines():
                        if _line.startswith("NAVIGATE_UNAVAILABLE:"):
                            _marker = _line[len("NAVIGATE_UNAVAILABLE:"):].strip()
                            break
                    _cov["stop_reason"] = "navigate_unavailable"
                    _failed["browser_unavailable_detail"] = (
                        _marker or "browser_service unavailable during discovery"
                    )
                return _failed
            # [wave-63] Salvage arm — trigger key (pinned): ``timed_out`` from
            # the runner OR fresh floored partial output on a crashed run. The
            # flushed rows persist + coverage-read BEFORE the honest FAILED
            # (662: a certified draft died at the wall with rows flushed but
            # unread). Status stays FAILED — NEVER COMPLETED (C4 belt: rows
            # bypass prune/normalize/gates, so the raw count is stated as raw).
            _h_content = result.get("output_content") or ""
            _h_name = result.get("output_name") or ""
            if result.get("timed_out") or _h_content:
                output_file = ""
                if _h_content and _h_name:
                    output_file = os.path.join(os.path.dirname(scraper_path), _h_name)
                    try:
                        with open(output_file, "w", encoding="utf-8") as _of:
                            _of.write(_h_content)
                    except OSError as exc:
                        logger.warning(
                            "run_execution: could not persist salvaged output: %s", exc
                        )
                discovery_coverage = (
                    _read_discovery_coverage(output_file) if output_file else {}
                )
                _h_count = result.get("product_count") or 0
                _h_why = "budget_wall_timeout" if result.get("timed_out") else "crash"
                _append_execution_note(
                    _exec_job_id,
                    f"PARTIAL HARVEST ({_h_why}; raw row count {_h_count})",
                )
                if result.get("timed_out"):
                    _note_budget_bound(_exec_job_id, discovery_coverage)
                logger.warning(
                    "run_execution: partial harvest (%s, %d raw rows) — FAILED with rows",
                    _h_why, _h_count,
                )
                if result.get("timed_out"):
                    _h_head = "Scraper timed out (budget wall)"
                else:
                    _h_head = f"Scraper exited with code {result['returncode']}"
                return {
                    "execution_status": "FAILED",
                    "partial_harvest": True,
                    "output_file": output_file,
                    "product_count": _h_count,
                    "discovery_coverage": discovery_coverage or {},
                    "error_message": (
                        f"{_h_head}; partial output harvested. {stderr}"
                    ),
                }
            return {
                "execution_status": "FAILED",
                "error_message": f"Scraper exited with code {result['returncode']}. {stderr}",
            }

        # Persist the returned output content locally; discovery_coverage is read
        # from it. Checkpoint cleanup is owned by browser_service's scraper_runner.
        _output_content = result.get("output_content") or ""
        _output_name = result.get("output_name") or ""
        output_file = ""
        if _output_content and _output_name:
            output_file = os.path.join(os.path.dirname(scraper_path), _output_name)
            try:
                with open(output_file, "w", encoding="utf-8") as _of:
                    _of.write(_output_content)
            except OSError as exc:
                logger.warning("run_execution: could not persist output locally: %s", exc)
        discovery_coverage = _read_discovery_coverage(output_file) if output_file else {}
        # [wave-63] Deliver the bounded-run honesty note (instrumentation —
        # the run itself is judged by the normal gates).
        _note_budget_bound(_exec_job_id, discovery_coverage)
        # [wave-56b F10] Payload identity rides the artifact itself (592:
        # cleanup rewrote the seed file 23s post-run, erasing the evidence).
        _stamp_seed_provenance(
            output_file, (state or {}).get("input_urls"),
            str((state or {}).get("input_mode") or ""),
        )
        try:
            _pruned = prune_empty_records(
                output_file, union_output_fields(state) or None
            )
            if _pruned:
                logger.info("run_execution: pruned %d empty records", _pruned)
        except Exception as exc:
            logger.warning("run_execution: prune step: %s", exc)
        # [A3] normalize formatted price strings ("$17.00") to numerics so the
        # tester's WRONG_VALUE pass and the deterministic checker agree.
        try:
            _norm = normalize_output_prices(output_file)
            if _norm:
                logger.info(
                    "run_execution: normalized %d price string(s) to numeric", _norm
                )
        except Exception as exc:
            logger.warning("run_execution: price normalize step: %s", exc)
        # F9 quality gate (nav modes): collapse-level failure rates -> FAILED
        _q = _extraction_quality_gate(
            output_file, state.get("input_mode", ""), result.get("product_count", 0),
            target_fields=union_output_fields(state),
        )
        if _q:
            # [wave-19 T1.9] Name the myhouse class when it fires: a collapsed
            # extraction whose DISCOVERED URLs fail shape/domain hygiene is a
            # URL-construction bug, not a selector bug — say so.
            _hy = _hygiene_diagnosis(output_file, state.get("url") or "")
            if _hy:
                _q += _hy
                logger.error("run_execution: %s", _hy.strip())
            _notify_phase(_exec_job_id, "execution", "failed")
            return {
                "execution_status": "FAILED",
                "output_file": output_file,
                "product_count": result.get("product_count", 0),
                "discovery_coverage": discovery_coverage,
                "error_message": _q,
            }

        # [wave-50 T5e] Nav-tree junk bar: junk rows carry core fields (a nav
        # title + the template's defaulted price), so F9 alone cannot see them
        # — this gate is its own verdict, and it fails the run rather than
        # editing rows (honest FAILED beats junk shipped as COMPLETED).
        _sh = _shallow_section_gate(
            output_file, state.get("url") or "", state.get("input_mode", ""),
            seed_urls=(state or {}).get("input_urls"),
        )
        if _sh:
            logger.error("run_execution: %s", _sh)
            _notify_phase(_exec_job_id, "execution", "failed")
            return {
                "execution_status": "FAILED",
                "output_file": output_file,
                "product_count": result.get("product_count", 0),
                "discovery_coverage": discovery_coverage,
                "error_message": _sh,
            }

        # [wave-51 T9] Non-item row bar: a shell URL (/#main, bare origin)
        # carries title+price so F9 and the core-value bar read it as data;
        # this gate is its own verdict and fails the run rather than editing
        # rows (honest FAILED beats junk shipped as COMPLETED — job 499).
        _ni = _nonitem_row_gate(output_file, state.get("input_mode", ""))
        if _ni:
            logger.error("run_execution: %s", _ni)
            _notify_phase(_exec_job_id, "execution", "failed")
            return {
                "execution_status": "FAILED",
                "output_file": output_file,
                "product_count": result.get("product_count", 0),
                "discovery_coverage": discovery_coverage,
                "error_message": _ni,
            }

        # [wave-55 C2] Post-run scope assertion: the one mode whose discovery
        # the user owns must not ship rows the user never asked for. Same
        # verdict shape as the two junk bars above — fail the run, never edit
        # rows. Seeded from state, NOT the on-disk input_urls.json (the
        # tester's own sample run rewrites that file, which is the
        # laundering vector this gate exists to catch).
        _us = _url_list_scope_gate(
            output_file, (state or {}).get("input_urls"),
            state.get("input_mode", ""),
        )
        if _us:
            logger.error("run_execution: %s", _us)
            _notify_phase(_exec_job_id, "execution", "failed")
            return {
                "execution_status": "FAILED",
                "output_file": output_file,
                "product_count": result.get("product_count", 0),
                "discovery_coverage": discovery_coverage,
                "error_message": _us,
            }

        # [wave-58 1d] Listing-seed expansion bar (browser lane mirror) —
        # seeds from state, NOT the on-disk input_urls.json the template
        # rewrites (same spelling as the scope gate above).
        _lse = _listing_seed_expansion_gate(
            output_file, (state or {}).get("input_urls"),
            state.get("input_mode", ""), state.get("url") or "",
        )
        if _lse:
            logger.error("run_execution: %s", _lse)
            _notify_phase(_exec_job_id, "execution", "failed")
            return {
                "execution_status": "FAILED",
                "output_file": output_file,
                "product_count": result.get("product_count", 0),
                "discovery_coverage": discovery_coverage,
                "error_message": _lse,
            }

        _notify_phase(_exec_job_id, "execution", "done")
        return {
            "execution_status": "SUCCESS",
            "output_file": output_file,
            "product_count": result.get("product_count", 0),
            "discovery_coverage": discovery_coverage,
            "error_message": "",
        }

    except httpx.ConnectError:
        _notify_phase(_exec_job_id, "execution", "failed")
        return {
            "execution_status": "FAILED",
            "error_message": f"browser_service ({service_url}) is unreachable",
        }
    except httpx.TimeoutException:
        _notify_phase(_exec_job_id, "execution", "failed")
        return {
            "execution_status": "FAILED",
            "error_message": f"Scraper timed out on browser_service after {timeout}s",
        }
    except Exception as exc:
        logger.exception("run_execution: browser_service dispatch failed")
        _notify_phase(_exec_job_id, "execution", "failed")
        return {
            "execution_status": "FAILED",
            "error_message": f"browser_service dispatch failed: {exc}",
        }
    finally:
        if hb is not None:
            try:
                _stop_heartbeat(hb)
            except Exception:
                pass


def _substantive_item_count(path: str, fields: list[str] | None = None) -> int:
    """F8/F16/F9 helper: items with >=1 content-type core field (0 on any error).

    Same predicate family as route_after_testing's F15 fix — an output whose
    rows carry no core field (price/availability for products...) is not
    'real' data (prod 337: 36 brand-only rows). Unparseable files count 0.
    The output file does not record the job's content type, so when the
    registry returns no fields we fall back to the union of common core
    fields across types — deliberately EXCLUDING title, which brand-only
    soft failures still carry (337's rows all had titles).
    """
    try:
        from src.content_types import _field_has_value

        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return 0
        if fields:
            use_fields = list(fields)
        else:
            try:
                from src.content_types import output_filter_fields

                use_fields = output_filter_fields("") or []
            except Exception:
                use_fields = []
            if not use_fields:
                # [wave-50 T3] currency dropped: every template default-fills
                # it, so it can't certify a row.
                use_fields = [
                    "price", "availability",
                    "author", "publish_date", "company", "location",
                    "content", "snippet", "rank", "posts",
                    "current_price", "previous_price", "list_price",
                    "sale_price", "was_price", "regular_price",
                ]
        count = 0
        for key in ("products", "jobs", "articles", "results", "items", "threads", "pages"):
            val = data.get(key)
            if isinstance(val, list) and val:
                for item in val:
                    if not isinstance(item, dict):
                        continue
                    if any(_field_has_value(item.get(f)) for f in use_fields):
                        count += 1
                break
        return count
    except Exception:
        return 0


def prune_empty_records(output_file: str, target_fields: list[str] | None = None) -> int:
    """Row-level dilution guard (job-10 lesson): records carrying NONE of the
    requested (or core) fields are discovery noise (delivery pseudo-SKUs, soft
    404s) — PRUNE, not fail (sparse is honest for some content types: optional
    salaries, OOS prices; those rows carry at least one field and survive).
    Applied post-run, zero LLM cost. Returns rows removed."""
    import json as _json
    import os as _os

    from src.content_types import _field_has_value

    if not output_file or not _os.path.isfile(output_file):
        return 0
    try:
        with open(output_file, encoding="utf-8") as fh:
            data = _json.load(fh)
    except Exception:
        return 0
    if not isinstance(data, dict):
        return 0
    fields = None
    if target_fields:
        fields = [f for f in target_fields if isinstance(f, str)]
    if not fields:
        try:
            from src.content_types import output_filter_fields

            fields = [f for f in (output_filter_fields("") or []) if isinstance(f, str)]
        except Exception:
            fields = []
    if not fields:
        # [wave-50 T3] currency dropped — defaulted by every template.
        fields = [
            "price", "availability",
            "author", "publish_date", "company", "location",
            "content", "snippet", "rank", "posts",
            "current_price", "previous_price", "list_price",
            "sale_price", "was_price", "regular_price",
        ]
    for key in ("products", "jobs", "articles", "results", "items", "threads", "pages"):
        val = data.get(key)
        if isinstance(val, list) and val:
            kept = [it for it in val
                    if isinstance(it, dict)
                    and any(_field_has_value(it.get(f)) for f in fields)]
            removed = len(val) - len(kept)
            if removed:
                data[key] = kept
                meta = data.get("metadata") or {}
                meta["pruned_empty_records"] = removed
                data["metadata"] = meta
                try:
                    with open(output_file, "w", encoding="utf-8") as fh:
                        _json.dump(data, fh, indent=1)
                except Exception:
                    return 0
                return removed
            return 0
    return 0


def _hygiene_diagnosis(output_file: str, anchor_url: str) -> str:
    """[wave-19 T1.9] When the run's discovered-URL set (the draft's own fresh
    ``input_urls.json``) fails shape/domain hygiene by a majority, return the
    diagnostic sentence naming the myhouse class — protocol-relative payload
    paths surviving urljoin as a bogus netloc ('https://products/x'). Empty
    when the url list is missing or healthy (gate no-ops)."""
    try:
        from ..graph import _discovered_url_issues

        seed_p = os.path.join(os.path.dirname(output_file or ""), "input_urls.json")
        urls: list = []
        if seed_p and os.path.isfile(seed_p):
            with open(seed_p, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and isinstance(data.get("urls"), list):
                urls = data["urls"]
        if not urls:
            return ""
        bad = _discovered_url_issues(urls, anchor_url or "")
        if len(bad) * 2 > len(urls):
            return (
                f" Discovery URL hygiene: {len(bad)}/{len(urls)} "
                f"discovered URLs fail shape/domain checks "
                f"(e.g. {bad[0][:100]!r}) — the draft's URL construction is "
                f"malformed; fix the join, not the selectors."
            )
    except Exception:
        pass
    return ""


_OUTPUT_ROW_KEYS = ("products", "jobs", "articles", "results", "items", "threads", "pages")
_ROW_URL_KEYS = ("url", "product_url", "item_url", "link", "page_url")


def _output_row_urls(output_file: str) -> list:
    """Best-effort URL harvest from the run's own output rows (T5e
    candidates). No key match or unparseable file → empty; the gate no-ops."""
    try:
        with open(output_file, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    for key in _OUTPUT_ROW_KEYS:
        val = data.get(key)
        if isinstance(val, list) and val:
            urls = []
            for it in val:
                if isinstance(it, dict):
                    for uk in _ROW_URL_KEYS:
                        v = it.get(uk)
                        if isinstance(v, str) and v.strip():
                            urls.append(v.strip())
                            break
            return urls
    return []


def _nonitem_row_gate(output_file: str, input_mode: str) -> str:
    """[wave-51 T9] Structural floor: root-only / fragment-only row URLs are
    never items. Job 499 shipped ``https://www.briscoes.co.nz/#main`` as a
    product (site <title> + defaulted price) — a URL with no path is a page
    POSITION (nav anchor / shell), not a page, but it carries title+price so
    F9 and the core-value bar read it as data. Detection is structural
    (src/discovery_quality.row_url_is_item_shaped), site agnostic.

    Fires when junk rows are half or more of the output: all-junk runs must
    not ship COMPLETED, while a mostly-real run with one shell row is real
    yield this gate must not punish. [wave-55 C2] url_list is IN scope: a
    user-provided seed is by definition an item page, so any shell row in the
    output is the scraper's doing, not the seed's (job 1229 shipped under the
    old exemption).

    Returns the diagnostic sentence, empty when healthy (gate no-ops)."""
    urls = _output_row_urls(output_file)
    if not urls:
        return ""
    try:
        from src.discovery_quality import row_url_is_item_shaped

        junk = [u for u in urls if not row_url_is_item_shaped(u)]
    except Exception:
        return ""
    if not junk or len(junk) * 2 < len(urls):
        return ""
    example = junk[0]
    return (
        f"Non-item row bar: {len(junk)}/{len(urls)} output rows point at "
        f"root-only or fragment-only URLs (e.g. {example[:100]!r}) — a URL "
        f"with no path is a page position (nav anchor / page shell), not an "
        f"item page. Discovery shipped navigation as products; fix the "
        f"discovery item-URL filtering, not the field selectors."
    )


def _shallow_section_gate(
    output_file: str, listing_url: str, input_mode: str,
    seed_urls: list | None = None,
) -> str:
    """[wave-50 T5e] Generic nav-tree junk bar: fail list_page runs whose
    output rows are overwhelmingly sibling section links shallower in the tree
    than the listing itself (the job-1116/488 shape — navigation shipped as
    products). Detection is structural (src/discovery_quality.py), site
    agnostic.

    list_page and url_list: their anchor URL is exactly the page the run was
    pointed at (the user's listing, or the user's own PDP), so the depth
    comparison is grounded. navigation/search_term anchors are the submitted
    URL (often a deep category picked by search), not necessarily the walked
    listing — a depth comparison there could false-fire on sites whose real
    PDPs sit in a flat namespace under a shared first segment, so those modes
    keep their own junk nets (T5d gate, core-value bar, F9, and — since
    [wave-55 C2] — the url_list scope assertion).

    url_list grades against the DISPATCHED ``seed_urls``, never the on-disk
    input_urls.json: the templates rewrite that file with their own Phase-1
    discoveries (save_urls_to_file), so reading it back here would judge the
    run by the very rows this bar exists to police. list_page keeps the file
    — its anchor legitimately is the walked listing.

    Returns the diagnostic sentence, empty when healthy (gate no-ops)."""
    if str(input_mode or "").lower() not in ("list_page", "url_list"):
        return ""
    urls = _output_row_urls(output_file)
    if str(input_mode or "").lower() == "url_list":
        seeds = [u for u in (seed_urls or [])
                 if isinstance(u, str) and u.strip()]
        seen = set(urls)
        urls += [u for u in seeds if u not in seen]
        if not listing_url and seeds:
            # A url_list run's anchor IS its seed PDP: the browser_service
            # lane hands it over as the listing URL, the in-process lane has
            # no listing env for this mode.
            listing_url = seeds[0]
    else:
        seed_p = os.path.join(os.path.dirname(output_file or ""), "input_urls.json")
        if os.path.isfile(seed_p):
            try:
                with open(seed_p, encoding="utf-8") as fh:
                    seed = json.load(fh)
                if isinstance(seed, dict) and isinstance(seed.get("urls"), list):
                    seen = set(urls)
                    urls += [u for u in seed["urls"]
                             if isinstance(u, str) and u not in seen]
            except Exception:
                pass
    if not urls:
        return ""
    try:
        from src.discovery_quality import shallow_section_links

        res = shallow_section_links(listing_url or "", urls)
    except Exception:
        return ""
    if not res.get("fired"):
        return ""
    example = (res.get("examples") or [""])[0]
    return (
        f"Nav-tree junk bar: {res['suspicious']}/{res['checked']} discovered "
        f"URLs are shallower section links under the listing's own first "
        f"path segment '/{res['segment']}' (e.g. {example[:100]!r}) — phase 1 "
        f"returned the listing's navigation tree, not item pages, and the "
        f"extraction counted navigation as products. Fix discovery (item-level "
        f"URL shape / platform catalog path), not the field selectors."
    )


def _submitted_url_key(url: str) -> str:
    """Comparison key for "is this output row one the user submitted".

    [wave-58 B4] Scheme now matches its own contract: a ``www.`` prefix,
    host case, THE SCHEME, the query string and a trailing slash are the
    same item — a seed submitted as ``http://shop.com/p/1?utm_source=feed``
    routinely comes back as ``https://www.shop.com/p/1/`` once the site
    canonicalises it (the audit's own 1226/1227 seeds carry four tracking
    params; 613's seeds were submitted http:// and came back https://).
    Dropping the scheme LOOSENS in the anti-veto direction only: fewer
    honest rows read as strays in the strict scope arm, and canonicalized
    SEED rows now count as seeds in the listing-seed gates (stricter against
    junk, never against extraction). Host + path is the item identity;
    anything else — a different path segment above all — is a different
    product and stays a violation. Fragment dropped: it never reaches the
    server.
    """
    s = str(url or "").strip()
    try:
        from urllib.parse import urlsplit

        p = urlsplit(s)
        if not p.netloc:
            return s.split("#", 1)[0].rstrip("/")
        host = p.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return f"{host}{p.path.rstrip('/')}"
    except Exception:
        return s.split("#", 1)[0].rstrip("/")


def _listing_seed_expansion_gate(
    output_file: str, seed_urls, input_mode: str = "", job_url: str = "",
) -> str:
    """[wave-58 1d] Post-run assertion: a listing-seed url_list run must have
    DISCOVERED something.

    Companion to the wave-57 scope loosen arm: when the seed set reads as
    listings, that loosen arm judges rows by item-shape + host — and 613
    shipped 3 rows that WERE its 3 listing seeds (category titles, defaulted
    prices) through it, because ``row_url_is_item_shaped('/all/x')`` passes.
    This gate closes that hole: url_list + listing-shaped seeds + >=2 rows
    AND ZERO rows whose URL is not one of the submitted seeds ⇒ every
    "product" is a submitted listing page; discovery never ran. Honest
    FAILED diagnostic — the run is never edited.

    Deliberate bars (critique S1): ONE all-seed row stays silent — a correct
    one-product store whose only page sits at a listing-shaped URL is
    byte-identical to the junk shape, and a listing that expands yields
    many. Rows with no URL key are unverifiable (F4b) and never assert.
    Item-shaped seed sets never reach here: the url_list contract IS
    rows==seeds for a PDP paste. Multi-host seed pastes stay silent — no
    runtime arm drives discovery for them anyway.
    """
    if str(input_mode or "").strip().lower() != "url_list":
        return ""
    seed_list = [u for u in (seed_urls or []) if str(u or "").strip()]
    if not seed_list:
        return ""
    try:
        from src.discovery_quality import (
            listing_seed_urls,
            seeds_are_listing_shaped,
            url_list_discovery_seed,
        )

        if not seeds_are_listing_shaped(seed_list):
            return ""
        if not listing_seed_urls(seed_list):
            return ""
        # Same eligibility the env gate demanded: one unambiguous seed host
        # (+ F17 when the job URL is known). A set no discovery arm would
        # walk gets no second verdict here.
        if not url_list_discovery_seed(seed_list, job_url):
            return ""
    except Exception:
        return ""
    urls = _output_row_urls(output_file)
    if len(urls) < 2:
        # Zero rows is the zero-yield gate's subject; one row is S1's
        # degenerate-store bar; no URL keys at all is unverifiable (F4b).
        return ""
    seeds = {_submitted_url_key(u) for u in seed_list}
    expanded = [u for u in urls if _submitted_url_key(u) not in seeds]
    if not expanded:
        return (
            f"url_list listing-seed no-op: all {len(urls)} extracted rows ARE "
            f"the submitted listing URLs — the run extracted the listing "
            f"pages themselves (category titles, defaulted fields), not the "
            f"items behind them; SCRAPER_LISTING_URL discovery never ran or "
            f"produced nothing. Fix the draft's Phase-1 discovery (listing "
            f"walk / pagination / item-link harvest), not the field "
            f"selectors."
        )
    return ""


def _output_row_count(output_file: str) -> int:
    """Row count of the run's first record array, URL harvestability aside.

    [wave-56b F4b] ``_output_row_urls`` returns [] both for zero rows and for
    rows carrying none of ``_ROW_URL_KEYS`` — and the C2 scope gate must tell
    those apart: rows shipped but unharvestable make the scope comparison
    UNVERIFIABLE, not the run empty (a title+price-only url_list output is a
    delivering run; "0 of N seeds produced rows" would be a false assertion).
    """
    try:
        with open(output_file, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return 0
    if not isinstance(data, dict):
        return 0
    for key in _OUTPUT_ROW_KEYS:
        val = data.get(key)
        if isinstance(val, list) and val:
            return len(val)
    return 0


def _url_list_scope_gate(
    output_file: str, seed_urls, input_mode: str = "",
) -> str:
    """[wave-55 C2] Post-run assertion: a url_list job ships ONLY submitted URLs.

    url_list is the one mode whose discovery is decided by the user, so an
    output row pointing anywhere else means the run went looking on its own —
    job 1227 shipped 10 unrelated made-in-china laptops off a 1-URL PDP seed
    and 1229 shipped 2 sandro strays, both COMPLETED and both invisible to
    every existing gate (F9 exempts url_list, and the two junk bars used to).
    Strict by design: ANY stray fails, the run is never edited. No seed list
    is a no-op — a gate must not guess. [wave-56 F4] A PRESENT output file
    with ZERO rows is no longer a no-op: that is the wholesale-seed-failure
    shape F9's url_list exemption trusted code_tester to catch, and books 579
    rode a tester PASS (1/3 live seeds) to ``SUCCESS, product_count=0`` with
    cleanup recording outcome=success behind it. Seeds dispatched + zero rows
    shipped = the run extracted nothing from the user's list.

    [wave-57] LISTING-seed exception: a seed set that reads as listings
    (``src.discovery_quality.seeds_are_listing_shaped`` — >=1 positively
    listing-shaped seed, zero strongly item-shaped) extends the scope:
    rows are judged by item-shape + registrable host, not membership.

    url_list ONLY: Site.input_urls reaches graph state for every mode, and
    navigation/list_page/search_term deliberately merge category-page rows
    (_run_category_sources), so a seed comparison there would fail honest
    multi-source runs. Same spelling as _shallow_section_gate's mode check.

    Returns the diagnostic sentence, empty when healthy (gate no-ops)."""
    if str(input_mode or "").strip().lower() != "url_list":
        return ""
    seed_list = [u for u in (seed_urls or []) if str(u or "").strip()]
    seeds = {_submitted_url_key(u) for u in seed_list}
    if not seeds:
        return ""
    urls = _output_row_urls(output_file)
    if not urls:
        # [wave-56 F4] Zero rows from a PRESENT output file is a verdict, not
        # a blind spot. A MISSING file stays the no_fresh_output arm's
        # subject — this gate never saw it and still doesn't.
        if not os.path.isfile(output_file or ""):
            return ""
        # [wave-56b F4b] Rows shipped but none carry a URL key: the scope
        # comparison is unverifiable here, not the run empty — failing it
        # "0 of N" would be a false assertion on a delivering run.
        if _output_row_count(output_file) > 0:
            return ""
        return (
            f"url_list zero-yield: 0 of {len(seeds)} submitted seeds produced "
            f"rows — the run extracted nothing from the user's URL list "
            f"(books 579 rode a tester PASS to a SUCCESS + 0-rows record). "
            f"Fix the scraper's url_list branch (seed file / --urls / "
            f"transport), not discovery."
        )
    # [wave-57 class 1] Listing-seed semantics (job #566): when the seed SET
    # reads as listings (>=1 positively listing-shaped, zero strongly
    # item-shaped), the submitted listings EXTEND the discovery scope —
    # rows are judged by item-shape + same registrable host, not by
    # membership. 913 item-shaped priced rows behind 19 category seeds were
    # vetoed by a gate whose premise (every seed is an item page) was
    # false. Genuine item-seeded sets never reach this arm: membership
    # stays demanded (the job-1227 class is still policed by the branch
    # below).
    try:
        from src.discovery_quality import seeds_are_listing_shaped

        _listing_seeds = seeds_are_listing_shaped(seed_list)
    except Exception:
        _listing_seeds = False
    if _listing_seeds:
        # Host comparator needs ONE unambiguous seed host: a multi-host
        # paste (intake's B3 gate only covers listing/search nav methods,
        # not url_list) has no "the seeds' own host" to extend scope across
        # — an ambiguous-host listing-seed set falls through to the strict
        # membership branch below (never loosens on a guess).
        _loosen_host = ""
        try:
            from src.registrable import registrable_of

            _seed_hosts = {registrable_of(u) for u in seed_list}
            _seed_hosts.discard("")
            if len(_seed_hosts) == 1:
                _loosen_host = next(iter(_seed_hosts))
        except Exception:
            _loosen_host = ""
        if _loosen_host:
            try:
                from src.discovery_quality import row_url_is_item_shaped

                strays = [
                    u for u in urls
                    if (registrable_of(u) != _loosen_host)
                    or not row_url_is_item_shaped(u)
                ]
            except Exception:
                strays = []
            if not strays:
                return ""
            examples = ", ".join(repr(s[:100]) for s in strays[:3])
            return (
                f"url_list scope violation (listing seeds): {len(strays)}/"
                f"{len(urls)} output rows are off-host or not item-shaped "
                f"({examples}) — the submitted seeds are LISTING urls, so "
                f"discovery scope extends behind them, but every row must "
                f"still be an item page on the seeds' own host "
                f"(jjshouse 566 class). Fix discovery's item-URL filtering, "
                f"not the field selectors."
            )
    strays = [u for u in urls if _submitted_url_key(u) not in seeds]
    if not strays:
        return ""
    examples = ", ".join(repr(s[:100]) for s in strays[:3])
    return (
        f"url_list scope violation: {len(strays)}/{len(urls)} output rows are "
        f"not submitted URLs ({examples}) — url_list discovery IS the seed "
        f"list, so the run extracted pages the user never submitted "
        f"(job 1227 shipped 10 unrelated products off a 1-URL PDP seed). "
        f"Fix the scraper's url_list branch (seed file / --urls), not "
        f"discovery."
    )


def _extraction_quality_gate(
    output_file: str, input_mode: str, product_count: int,
    target_fields: list[str] | None = None,
) -> str:
    """F9: fail nav-mode executions whose output is overwhelmingly empty.

    Prod shipped COMPLETED jobs whose extraction quietly collapsed: 330 kept
    4 of 80 discovered (95% failed), 335 kept 5 of 39 (87%), 337 shipped 36
    rows with zero core fields. url_list jobs are out of scope (their seeds
    are user-provided; a wholesale seed failure is code_tester's signal).

    good = items with >=1 core field — where "core" is the JOB'S REQUESTED
    fields when the user asked for a custom set (a request for
    {current_price, ratings} must be judged on those, not on the registry
    default), falling back to the content-type/alias core list otherwise.
    bad  = failed_products + core-less items. Denominator is PROCESSED
    (good+bad), never total_discovered — limit-truncated runs must not
    false-positive (a --limit 5 run of a healthy site processes 5, fails 0).
    Fires when processed >= 5 and fail-ratio >= 0.8 -> the caller turns the
    SUCCESS into FAILED. Warn-only log at >= 0.5. Returns the error message
    ('' when the gate passes).
    """
    if input_mode not in ("navigation", "list_page", "search_term"):
        return ""
    try:
        # The user's requested set IS the contract when present (job-9: a
        # request for {current_price, ratings...} must be judged on those).
        good_fields = list(target_fields) if target_fields else None
        good = _substantive_item_count(output_file, good_fields) if output_file else 0
        failed = 0
        with open(output_file, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            meta = data.get("metadata") or {}
            if isinstance(meta, dict):
                try:
                    failed = int(meta.get("failed_products") or 0)
                except (TypeError, ValueError):
                    failed = 0
            # core-less items among the shipped rows
            for key in ("products", "jobs", "articles", "results", "items",
                        "threads", "pages"):
                val = data.get(key)
                if isinstance(val, list):
                    def _good(it):
                        if good_fields:
                            return any(it.get(f) for f in good_fields)
                        return _item_has_core_field(it)

                    coreless = sum(1 for it in val if isinstance(it, dict)
                                   and not _good(it))
                    bad = failed + coreless
                    processed = good + bad
                    if processed >= 5 and bad / processed >= 0.8:
                        msg = (
                            f"Extraction quality gate: {bad}/{processed} processed "
                            f"items lacked core fields or failed "
                            f"({good} good, {failed} failed_products, "
                            f"{coreless} core-less) — failing rather than "
                            f"shipping a collapsed extraction."
                        )
                        logger.error("run_execution: %s", msg)
                        return msg
                    if processed >= 5 and bad / processed >= 0.5:
                        logger.warning(
                            "run_execution: extraction quality warning — "
                            "%d/%d processed items bad (failed/core-less)",
                            bad, processed,
                        )
                    return ""
        return ""
    except Exception:
        # A gate that cannot read the output must never invent a failure —
        # F8 already handles the no-output case; unreadable output here is
        # the counting path's problem, not a quality verdict.
        return ""


def _registrable_of(url_val: str) -> str:
    """F17 helper: best-effort registrable domain (lowercased, www-stripped).

    Mirrors traversal._registrable; duplicated because browser_service's
    run_execution copy must not import across package roots. '' on failure.
    """
    try:
        from urllib.parse import urlparse

        host = (urlparse(url_val).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        two_part = (
            ".co.uk", ".org.uk", ".com.au", ".co.nz", ".co.za", ".com.br",
            ".co.jp", ".com.sg", ".com.mx",
        )
        for tld in two_part:
            if host.endswith(tld):
                pre = host[: -len(tld)].rstrip(".")
                return f"{pre.split('.')[-1]}{tld}" if pre else host
        parts = host.split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else host
    except Exception:
        return ""


def _item_has_core_field(item: dict) -> bool:
    """F9/F16 helper: does this item carry >=1 core field? (shared predicate)"""
    try:
        from src.content_types import output_filter_fields

        fields = output_filter_fields("") or []
    except Exception:
        fields = []
    if not fields:
        # inlined (not the module global): these functions are exec-extracted
        # into bare namespaces by test_f8_f16 — module globals aren't there
        # [wave-50 T3] currency dropped — defaulted by every template.
        fields = [
            "price", "availability",
            "author", "publish_date", "company", "location",
            "content", "snippet", "rank", "posts",
            "current_price", "previous_price", "list_price",
            "sale_price", "was_price", "regular_price",
        ]
    from src.content_types import _field_has_value

    return any(_field_has_value(item.get(f)) for f in fields)


# Price aliases generated scrapers legitimately emit (job-9 Priceline:
# current_price was requested INSTEAD of price — a perfect extraction was
# failed by the gate because 'current_price' didn't string-match 'price').
_PRICE_ALIASES = ("price", "current_price", "previous_price", "list_price",
                  "sale_price", "was_price", "regular_price")
# The no-registry fallback list (was inline in _item_has_core_field).
# [wave-50 T3] currency dropped — defaulted by every template.
_FALLBACK_CORE_FIELDS = [
    "price", "availability",
    "author", "publish_date", "company", "location",
    "content", "snippet", "rank", "posts",
] + list(_PRICE_ALIASES[1:])


# ── [A3] Price normalization at persist time ─────────────────────────────────
# The deterministic checker (route_after_testing._price_value) happily parses
# "$17.00" / "1,299.00" — but the tester's LLM condemns the same string as a
# WRONG_VALUE ("not a clean price"), and consumers get inconsistent types.
# Normalizing formatted price strings to floats at persist time removes the
# disagreement class: what the checker tolerates, the tester no longer sees.

_PRICE_TOKEN_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")
# Whatever remains after stripping the number tokens must be nothing but
# currency symbols/codes and separators for the string to count as a bare price.
_PRICE_RESIDUE_RE = re.compile(
    r"^[\s$€£¥₹.,\-]*(?:USD|AUD|NZD|GBP|EUR|CAD)?[\s$€£¥₹.,\-]*$",
    re.IGNORECASE,
)


def _parse_price_string(v: str) -> float | None:
    """Parse a price-ish string the same way the framework's checker does —
    but ONLY when the string is nothing but a price. ``"From $10"`` /
    ``"Free"`` stay strings; ``"$17.00"`` → 17.0."""
    s = str(v).strip()
    m = _PRICE_TOKEN_RE.search(s)
    if not m:
        return None
    if not _PRICE_RESIDUE_RE.match(_PRICE_TOKEN_RE.sub("", s)):
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def normalize_output_prices(output_file: str) -> int:
    """Rewrite price-alias fields in an output JSON to numeric values when the
    scraper emitted formatted strings. Returns the number of fields rewritten
    (0 on any read/parse problem — never raises)."""
    try:
        with open(output_file, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return 0
    if not isinstance(data, dict):
        return 0
    changed = 0
    for key in ("products", "jobs", "articles", "results", "items", "threads", "pages"):
        rows = data.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            for fld in _PRICE_ALIASES:
                v = row.get(fld)
                if isinstance(v, str):
                    num = _parse_price_string(v)
                    if num is not None:
                        row[fld] = num
                        changed += 1
        break  # the rows live under the first populated item key only
    if not changed:
        return 0
    try:
        with open(output_file, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
    except Exception:
        return 0
    return changed


def _find_newest_output(
    *directories: str, slug: str | None = None, mtime_floor: float | None = None
) -> str:
    """Select the best ``output_*.json`` across the given LOCAL directories.

    The scraper writes its output next to itself (``SCRIPT_DIR`` = dirname of
    the scraper file), which during a run is ``workspace/{slug}/``. Selecting by
    name OR scanning ``scrapers/{slug}/`` first returns a *stale* output from a
    prior job on a re-scrape, so picking the newest mtime reliably identifies
    the file THIS run just wrote.

    F8 (``mtime_floor``): only accept files with mtime >= floor. Fresh-
    execution callers pass the subprocess start so a stale file can never be
    credited to this run (prod 319 shipped code_tester's sample written 32s
    BEFORE execution started). With a floor set, an empty result returns ""
    (FAILED by the caller) — the File-Master fallback is BYPASSED because an
    FM download lands in a fresh-mtime tmpfile that launders stale content
    through any floor.

    F16: among floor-passing candidates, prefer the file with the MOST
    substantive items; mtime is only the tiebreak (mirrors route_after_testing's
    own best-of-N fix — the newest file is frequently a 0-item Phase-1 crash
    while a full file sits minutes older). NOTE: this deliberately does NOT
    rescue prod 324/336 — their 5-item files are testing-phase samples that
    predate the execution subprocess (below the floor); widening the window
    would ship tester samples as full scrapes. Those jobs fail honestly via
    F9.

    Without a floor (legacy callers: graph.py's tester context and
    store_job_listings' resume path), behavior is the old newest-mtime pick
    WITH the FM fallback preserved.
    """
    candidates: list[tuple[float, str]] = []
    for directory in directories:
        if not directory or not os.path.isdir(directory):
            continue
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            if not (name.startswith("output_") and name.endswith(".json")):
                continue
            path = os.path.join(directory, name)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime_floor is not None and mtime < mtime_floor:
                continue
            candidates.append((mtime, path))
    if not candidates:
        if mtime_floor is not None:
            # F8: floored fresh-execution call — no FM fallback (laundering).
            return ""
        if slug:
            try:
                import tempfile

                import src.artifacts as artifacts

                fm_key = artifacts.latest_output_key(slug)
                if fm_key:
                    _tmp = tempfile.NamedTemporaryFile(
                        prefix="fm_output_", suffix=".json", delete=False
                    )
                    _tmp.write(artifacts.read(fm_key))
                    _tmp.close()
                    return _tmp.name
            except Exception as exc:
                logger.debug("_find_newest_output: FM fallback failed: %s", exc)
        return ""
    # F16: rank by substantive count DESC, then mtime DESC.
    scored = [(mtime, _substantive_item_count(path), path) for mtime, path in candidates]
    scored.sort(key=lambda t: (t[1], t[0]), reverse=True)
    return scored[0][2]


def _inprocess_partial_harvest(
    workspace_folder: str,
    site_folder: str,
    start: float,
    job_id: int,
    why: str = "crash",
) -> dict[str, Any] | None:
    """[wave-63] In-process lane salvage (stall kill / nonzero exit).

    The templates flush partial output every FLUSH_EVERY items; this reads the
    freshest output floored to the SUBPROCESS start (load-bearing in this lane:
    the tester's output lives in the same workspace — prod 319 class). Returns
    None when nothing fresh — the caller keeps today's exact shape. The status
    stays FAILED (C4 belt: partial rows are recorded honestly, never completed;
    the count is RAW — flushed rows bypass the core-field filter).
    """
    if not workspace_folder and not site_folder:
        return None
    try:
        path = _find_newest_output(
            workspace_folder, site_folder, slug=None, mtime_floor=start
        )
    except Exception:
        return None
    if not path:
        return None
    coverage = _read_discovery_coverage(path) or {}
    count = _count_products(path)
    _append_execution_note(
        job_id, f"PARTIAL HARVEST ({why}; raw row count {count})"
    )
    _note_budget_bound(job_id, coverage)
    return {
        "execution_status": "FAILED",
        "partial_harvest": True,
        "output_file": path,
        "product_count": count,
        "discovery_coverage": coverage,
    }


def _count_products(output_path: str) -> int:
    """Count extracted items in an output file, across content types.

    Outputs use different keys by domain (products/jobs/articles/results/...).
    Count the first non-empty list so the job's ``product_count`` reflects
    reality regardless of content type.  Generic. [summary correctness]
    """
    if not output_path or not os.path.isfile(output_path):
        return 0
    try:
        with open(output_path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            for key in ("products", "jobs", "articles", "results", "items", "threads", "pages"):
                val = data.get(key)
                if isinstance(val, list) and val:
                    return len(val)
        return 0
    except Exception:
        return 0


def _read_discovery_coverage(output_path: str) -> dict[str, Any] | None:
    """Read the ``discovery_coverage`` block from a scraper output's metadata.

    Two-phase scrapers emit this block (see
    docs/discovery-coverage-gate-contract.md §1) inside ``metadata``. Returns
    None when absent (url_list scrapers with no discovery phase, older outputs,
    or missing file) so the state field stays unset rather than polluted.
    """
    if not output_path or not os.path.isfile(output_path):
        return None
    try:
        with open(output_path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            metadata = data.get("metadata") or {}
            if isinstance(metadata, dict):
                block = metadata.get("discovery_coverage")
                if isinstance(block, dict):
                    return block
    except Exception:
        pass
    return None


def _delete_discovery_checkpoint(*candidate_dirs: str) -> None:
    """Delete ``discovered_urls_checkpoint.json`` so the next run starts fresh (H3).

    Without this, code_tester's capped-sample checkpoint persists into
    run_execution, which loads it and skips Phase 1 — silently extracting only
    the test sample (the locumtenens 38-of-3771 bug). Tried across all candidate
    dirs because the checkpoint location varies by template (SCRIPT_DIR vs cwd).
    Silent no-op when the file is absent. [discovery-coverage-gate §4]
    """
    for d in candidate_dirs:
        if not d:
            continue
        path = os.path.join(d, "discovered_urls_checkpoint.json")
        try:
            if os.path.isfile(path):
                os.remove(path)
                logger.info("run_execution: removed discovery checkpoint %s", path)
        except OSError as exc:
            logger.warning(
                "run_execution: could not remove checkpoint %s: %s", path, exc
            )
