"""Run the generated scraper and capture output.

This node NEVER throws.  All exceptions are caught and recorded
in ``state["execution_status"]`` so that ``cleanup`` can always run.

For browser-based scrapers, dispatches to browser_service via HTTP.
For lightweight scrapers, runs in-process via subprocess.
"""

import json
import logging
import os
import select
import signal
import subprocess
import time
from typing import Any, Optional

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

        with open(scraper_path, "r", errors="ignore") as fh:
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
    from ..constants import API_STRATEGIES, NAV_INPUT_MODES, SCRAPER_ENV_LISTING

    im = (input_mode or "").strip().lower()
    if im not in NAV_INPUT_MODES:
        return None
    if not scraper_path or not os.path.isfile(scraper_path):
        return None
    accepted = _accepted_cli_flags(scraper_path)
    if accepted is None:
        return None  # unparseable → don't block; the syntax fixer owns it
    try:
        with open(scraper_path, "r", errors="ignore") as fh:
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
        if isinstance(method, str) and method.startswith(("uc_chrome", "cloak")):
            return True
    pm = state.get("probe_method") or ""
    if isinstance(pm, str) and pm.startswith(("uc_chrome", "cloak")):
        return True
    return False


def _stealth_env(state: ScrapeState) -> dict[str, str]:
    return {"STEALTH_BROWSER": "cloak"} if _needs_cloak(state) else {}


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
        logger.error("run_execution: scraper not found at %s", scraper_path)
        return {
            "execution_status": "FAILED",
            "error_message": f"scraper_draft.py not found at {scraper_path}",
        }

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
            # list_page job URL first (see _job_listing above), then
            # discovery.listing_url (the navigator's authoritative contract),
            # then search.working_url / listing_url_used (the traversal's
            # actual landing page — blank for 8/9 sites' on-disk analyses if
            # listing_url is absent, so the chain must fall through, never
            # replace). F17: each candidate domain-guarded against the job
            # URL's registrable domain (prod 331 shipped 80/80 .com.au rows
            # under a .us job).
            _search = _nav.get("search") or {}
            _disc = (_nav.get("discovery") if isinstance(_nav, dict) else None) or {}
            _job_reg = _registrable_of(state.get("url", ""))
            _candidates = [
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
    args.append("--fresh-discovery")

    # DETERMINISTIC DISCOVERY (env-var): compute the listing URL for env-var
    # injection. This bypasses the argparse + _filter_supported_args chain —
    # code_writer's per-run argparse may or may not declare --listing-url/
    # --fresh-discovery, but env vars always reach the scraper. The template's
    # main() checks SCRAPER_LISTING_URL before the seed-file gate.
    # F7+M6: prefer discovery.listing_url (the navigator's authoritative
    # contract; working_url may be a detail page — the uindex root cause),
    # fall back to the CLI chain's value; F17 domain-guarded either way.
    _listing_url_env = ""
    if input_mode in ("navigation", "list_page", "search_term"):
        _nav_env = state.get("navigation_analysis") or {}
        _disc_env = (_nav_env.get("discovery") if isinstance(_nav_env, dict) else None) or {}
        # Job 310: same list_page priority as the --listing-url chain — the
        # user-provided listing (job URL) outranks the navigator's promotion.
        _env_candidate = _job_listing or ""
        if not _env_candidate:
            _env_candidate = (_disc_env.get("listing_url") if isinstance(_disc_env, dict) else "") or ""
        if not _env_candidate:
            _env_candidate = _working_url if (_listing_reached or not _respect_flag) else ""
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
                    return {
                        "execution_status": "FAILED",
                        "error_message": (
                            f"{_contract} Refusing silent seed-only execution. "
                            "Remedy: regenerate the scraper (delete the cached "
                            "draft to force code_writer) or re-submit the job."
                        ),
                    }
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
        result = _run_via_browser_service(scraper_path, args, site_folder, _state_bs)

        # MULTI-SOURCE: for navigation jobs, also run the scraper against
        # watch-related category pages. A single search page often returns
        # fewer products than the site has. This re-runs with --category-url
        # for each relevant category + merges the output. Generic.
        if input_mode in ("navigation", "list_page", "search_term") and search_criteria:
            result = _run_category_sources(
                _state_bs, scraper_path, args, site_folder, result, search_criteria
            )

        return result

    return _run_in_process(
        scraper_path, args, root, site_folder, workspace_folder, job_id=job_id,
        env_overrides=_stealth_env(state),
        listing_url_env=_listing_url_env,
        input_mode=input_mode,
        target_fields=list(state.get("target_fields") or []),
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
        nf_path = _os.path.join(root, "workspace", slug, "navigation_findings.json")
        if not _os.path.isfile(nf_path):
            return primary_result

        nf = _json.load(open(nf_path))
        # Get category URLs from homepage_nav.category_links
        hp = nf.get("homepage_nav") or {}
        cat_links = hp.get("category_links") or []
        # Also check navigation_analysis categories
        na_path = _os.path.join(root, "workspace", slug, "navigation_analysis.json")
        if _os.path.isfile(na_path):
            na = _json.load(open(na_path))
            na_cats = na.get("categories") or []
            if isinstance(na_cats, list):
                cat_links = list(cat_links) + list(na_cats)

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

        if not relevant:
            logger.info("multisource: no category pages matching '%s'", search_term)
            return primary_result

        logger.info("multisource: %d category pages match '%s'", len(relevant), search_term)

        # Load primary output products
        ct_config = state.get("content_type_config") or {}
        output_key = ct_config.get("output_key", "products") if ct_config else "products"

        primary_file = primary_result.get("output_file", "")
        primary_products = []
        if primary_file and _os.path.isfile(primary_file):
            primary_data = _json.load(open(primary_file))
            primary_products = primary_data.get(output_key, [])

        existing_urls = set(p.get("url", "") for p in primary_products)
        all_products = list(primary_products)
        service_url = _os.environ.get("BROWSER_SERVICE_URL", "http://browser_service:8001")
        stealth_env = _stealth_env(state)
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
                    with open(scraper_path, "r", encoding="utf-8", errors="replace") as _cf:
                        _cat_source = _cf.read()
                except OSError:
                    _cat_source = ""
                # W8: 429/502 backpressure used to land here un-checked — the
                # error body became output_content="" and the category was
                # silently skipped, under-counting the merged output.
                _res = post_scrape_with_retry(
                    f"{service_url}/scrape",
                    {"scraper_source": _cat_source, "scraper_name": os.path.basename(scraper_path), "args": cat_args, "timeout": 600, "env_overrides": stealth_env},
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
) -> dict[str, Any]:
    cmd = ["python3", scraper_path] + args
    start = time.time()

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
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd,
            env={**os.environ, **(env_overrides or {}),
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
        while True:
            ready, _, _ = select.select([proc.stderr], [], [], 5.0)
            if ready:
                chunk = os.read(err_fd, 65536)
                if chunk:
                    last_activity = time.time()
                    stderr_buf.extend(chunk)
                    if len(stderr_buf) > 200_000:
                        del stderr_buf[:100_000]
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
            if time.time() - start > _hard:
                stall_reason = f"scraper exceeded {_hard}s wall-clock"
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
            return {
                "execution_status": "FAILED",
                "error_message": f"{stall_reason}. Last output: {stderr[-4000:]}",
            }

        logger.info("run_execution: scraper exited with code %d in %ds", returncode, elapsed)

        if returncode != 0:
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
                "error_message": (
                    "Execution produced no output file (rc=0 but no fresh "
                    "output_*.json with mtime >= subprocess start; discovery "
                    "likely found 0 items or the scraper wrote nothing)."
                ),
            }
        product_count = _count_products(output_file)
        discovery_coverage = _read_discovery_coverage(output_file)
        # F9 quality gate (nav modes): collapse-level failure rates -> FAILED
        _q = _extraction_quality_gate(
            output_file, input_mode, product_count,
            target_fields=target_fields,
        )
        if _q:
            return {
                "execution_status": "FAILED",
                "output_file": output_file,
                "product_count": product_count,
                "discovery_coverage": discovery_coverage,
                "error_message": _q,
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

    from ..tools.browser_http import post_scrape_with_retry

    service_url = _get_browser_service_url()
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
    if _listing_env_bs and (state or {}).get("input_mode") in ("navigation", "list_page", "search_term"):
        stealth_env["SCRAPER_LISTING_URL"] = _listing_env_bs
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
            with open(scraper_path, "r", encoding="utf-8", errors="replace") as _f:
                _source = _f.read()
        except OSError as exc:
            return {
                "execution_status": "FAILED",
                "error_message": f"Could not read scraper source {scraper_path}: {exc}",
            }
        # Read sibling files (input_urls.json, discovery_config.json) for staging
        _extra = {}
        for _sf in ("input_urls.json", "discovery_config.json"):
            _sp = os.path.join(os.path.dirname(scraper_path), _sf)
            if os.path.isfile(_sp):
                try:
                    with open(_sp, "r", encoding="utf-8", errors="replace") as _fh:
                        _extra[_sf] = _fh.read()
                except OSError:
                    pass
        # W8: bounded retry on 429/502/503/504 + transport errors — a bare
        # raise_for_status() turned backpressure into an execution failure.
        _res = post_scrape_with_retry(
            f"{service_url}/scrape",
            {
                "scraper_source": _source,
                "scraper_name": os.path.basename(scraper_path),
                "extra_files": _extra,
                "args": args,
                "timeout": timeout,
                "env_overrides": stealth_env,
            },
            timeout=timeout + 60,
        )

        if _res.status_code == 404:
            return {
                "execution_status": "FAILED",
                "error_message": "Scraper rejected by browser_service (source invalid)",
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
        try:
            _pruned = prune_empty_records(
                output_file, list(state.get("target_fields") or []) or None
            )
            if _pruned:
                logger.info("run_execution: pruned %d empty records", _pruned)
        except Exception as exc:
            logger.warning("run_execution: prune step: %s", exc)
        # F9 quality gate (nav modes): collapse-level failure rates -> FAILED
        _q = _extraction_quality_gate(
            output_file, state.get("input_mode", ""), result.get("product_count", 0),
            target_fields=list(state.get("target_fields") or []),
        )
        if _q:
            return {
                "execution_status": "FAILED",
                "output_file": output_file,
                "product_count": result.get("product_count", 0),
                "discovery_coverage": discovery_coverage,
                "error_message": _q,
            }

        return {
            "execution_status": "SUCCESS",
            "output_file": output_file,
            "product_count": result.get("product_count", 0),
            "discovery_coverage": discovery_coverage,
            "error_message": "",
        }

    except httpx.ConnectError:
        return {
            "execution_status": "FAILED",
            "error_message": f"browser_service ({service_url}) is unreachable",
        }
    except httpx.TimeoutException:
        return {
            "execution_status": "FAILED",
            "error_message": f"Scraper timed out on browser_service after {timeout}s",
        }
    except Exception as exc:
        logger.exception("run_execution: browser_service dispatch failed")
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
        with open(path, "r", encoding="utf-8") as fh:
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
                use_fields = [
                    "price", "availability", "currency",
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
                    if any(item.get(f) for f in use_fields):
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
    if not output_file or not _os.path.isfile(output_file):
        return 0
    try:
        with open(output_file, "r", encoding="utf-8") as fh:
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
        fields = [
            "price", "availability", "currency",
            "author", "publish_date", "company", "location",
            "content", "snippet", "rank", "posts",
            "current_price", "previous_price", "list_price",
            "sale_price", "was_price", "regular_price",
        ]
    for key in ("products", "jobs", "articles", "results", "items", "threads", "pages"):
        val = data.get(key)
        if isinstance(val, list) and val:
            kept = [it for it in val if isinstance(it, dict) and any(it.get(f) for f in fields)]
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
        with open(output_file, "r", encoding="utf-8") as fh:
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
        fields = [
            "price", "availability", "currency",
            "author", "publish_date", "company", "location",
            "content", "snippet", "rank", "posts",
            "current_price", "previous_price", "list_price",
            "sale_price", "was_price", "regular_price",
        ]
    return any(item.get(f) for f in fields)


# Price aliases generated scrapers legitimately emit (job-9 Priceline:
# current_price was requested INSTEAD of price — a perfect extraction was
# failed by the gate because 'current_price' didn't string-match 'price').
_PRICE_ALIASES = ("price", "current_price", "previous_price", "list_price",
                  "sale_price", "was_price", "regular_price")
# The no-registry fallback list (was inline in _item_has_core_field).
_FALLBACK_CORE_FIELDS = [
    "price", "availability", "currency",
    "author", "publish_date", "company", "location",
    "content", "snippet", "rank", "posts",
] + list(_PRICE_ALIASES[1:])


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
                import src.artifacts as artifacts
                import tempfile

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


def _count_products(output_path: str) -> int:
    """Count extracted items in an output file, across content types.

    Outputs use different keys by domain (products/jobs/articles/results/...).
    Count the first non-empty list so the job's ``product_count`` reflects
    reality regardless of content type.  Generic. [summary correctness]
    """
    if not output_path or not os.path.isfile(output_path):
        return 0
    try:
        with open(output_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            for key in ("products", "jobs", "articles", "results", "items", "threads", "pages"):
                val = data.get(key)
                if isinstance(val, list) and val:
                    return len(val)
        return 0
    except Exception:
        return 0


def _read_discovery_coverage(output_path: str) -> Optional[dict[str, Any]]:
    """Read the ``discovery_coverage`` block from a scraper output's metadata.

    Two-phase scrapers emit this block (see
    docs/discovery-coverage-gate-contract.md §1) inside ``metadata``. Returns
    None when absent (url_list scrapers with no discovery phase, older outputs,
    or missing file) so the state field stays unset rather than polluted.
    """
    if not output_path or not os.path.isfile(output_path):
        return None
    try:
        with open(output_path, "r", encoding="utf-8") as fh:
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
