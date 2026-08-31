"""Routing function after the code-tester phase.

[ADP #6/#7 / HIP #6 / G2]
"""

import logging
import re
from typing import Optional

from ..constants import DEAD_STATUS_CODES, FINAL_RETRY_SENTINEL, MAX_REMAPS, MAX_TEST_RETRIES
from ..state import ScrapeState

logger = logging.getLogger(__name__)

# Strategies where "0 items extracted" means ACCESS failed (the strategy can't
# reach the content) — switch strategy, don't retry the same one.
_HTTP_LIKE_STRATEGIES = {
    "http_requests", "internal_api", "api", "requests", "shopify_api",
    "http_navigation",
}
_TRACEBACK_RE = re.compile(
    # any Python exception/warning name (NameError, RecursionError, PlaywrightError, …).
    # is_timeout takes precedence so TimeoutError is treated as access, not code-bug.
    r"\b\w*(?:Error|Exception|Warning)\s*:",
    re.IGNORECASE,
)
_TIMEOUT_RE = re.compile(r"\btimed?\s*out\b|\btimeout\b|\bexceeded\b", re.IGNORECASE)
_BLOCKED_RE = re.compile(
    r"\b(blocked|forbidden|403|captcha|access denied|unavailable|oop[s]!|"
    r"robot|rate[\s-]?limit)\b",
    re.IGNORECASE,
)
# [A1] An explicit 429 / too-many-requests: the site rate-limited the window.
# Distinct from _BLOCKED_RE's "rate limit" token — this keys on the status
# code / reason phrase so a throttled test run routes to a same-draft re-test
# instead of a strategy switch or a code fix.
_RATE_LIMITED_RE = re.compile(r"\b429\b|too many requests", re.IGNORECASE)
# Playwright/Selenium "element not found" crashes — unambiguous CODE bugs (a
# wrong selector), NOT access/strategy failures. Without this they fall through
# to the generic "no items extracted — likely wrong strategy" branch and the
# cascade abandons a viable strategy (e.g. locumtenens Playwright crash on a
# non-existent `select[name='Specialties']`).
_SELECTOR_CRASH_RE = re.compile(
    r"failed to find element|eval_on_selector|query_selector|"
    r"element.{0,20}not found|no element found|locator.{0,20}not found|"
    r"waiting for selector|unable to find element",
    re.IGNORECASE,
)


def _extracted_item_count(report: dict) -> int:
    """Best-effort count of items the scraper actually extracted.

    Reads both top-level AND nested under ``results`` — code-tester writes
    ``successful_extractions`` nested in ``results`` (code-tester.md:99-100), so
    a top-level-only read returns 0 for a successful extraction and misroutes a
    viable strategy to "no items extracted" (the uindex false strategy-switch
    bug). The sibling _core_field_zero_coverage already reads the nested path.
    """
    results = report.get("results") if isinstance(report, dict) else None
    for key in ("successful_extractions", "extracted_items", "item_count"):
        v = report.get(key)
        if v is None and isinstance(results, dict):
            v = results.get(key)
        if isinstance(v, (int, float)) and v:
            return int(v)
    sp = report.get("sample_products") or []
    if sp:
        return len(sp)
    so = report.get("sample_output") or {}
    if isinstance(so, dict):
        for v in so.values():
            if isinstance(v, list):
                return len(v)
    return 0


# Discovery-coverage gate (Phase 3). Tier 1 stop_reasons that mean the scraper
# GAVE UP (errors / blocks / rate-limiting), NOT exhausted the source. A boolean
# ``exhausted`` would have passed these — the H4 false-pass the gate exists to stop.
# ``empty_first_page`` ([job-58 birkenstock]) is the zero-URL run with no hard
# error: an anti-bot challenge/consent wall served with HTTP 200 yields zero
# product links, which the template loop misread as a genuine end-of-catalog.
_COVERAGE_FAIL_STOP_REASONS = {"navigate_error", "dedup_flat", "empty_first_page"}


def _discovery_coverage_failure(report: dict) -> Optional[str]:
    """Return a short reason if discovery coverage is insufficient, else None.

    Reads ``test_report.discovery_coverage`` (code_tester copies it from the
    scraper output's ``metadata.discovery_coverage``). Tier 1 (always on, fully
    generic): ``stop_reason`` in {navigate_error, dedup_flat, empty_first_page}
    → the scraper stopped due to errors/blocks/rate-limiting, NOT exhaustion.

    Returns None when discovery didn't run or signals are absent — the gate is a
    NO-OP on missing data (never blocks).
    """
    if not isinstance(report, dict):
        return None
    cov = report.get("discovery_coverage")
    if not isinstance(cov, dict) or not cov.get("ran_phase1", True):
        return None
    stop_reason = cov.get("stop_reason")
    if stop_reason in _COVERAGE_FAIL_STOP_REASONS:
        return f"discovery {stop_reason} (gave up, not exhausted)"
    return None


def classify_test_failure(report: dict, strategy: str) -> tuple[str, str]:
    """Classify a failed test into an action + reason.

    Returns (action, reason) where action ∈ {"strategy", "scraper", "mapping",
    "refine", "retest"}:
    - "strategy": access/strategy-class failure → switch strategy (timeout, 0-item
      http/api run, blocked). The current strategy can't reach the content.
    - "scraper": a code bug (Python traceback, not a timeout) → fix the scraper.
    - "mapping": items extracted but a core field is at ~0% → re-map (caller checks).
    - "refine": items extracted, fields mostly present, low quality → tweak.
    - "retest": the run is UNPROVEN coverage (429 / browser-service throttle /
      transient render block) — re-test the SAME draft, no strategy switch and
      no code change. Neither the strategy nor the code was on trial.

    Deterministic — used as a guard that can override code_tester's LLM diagnosis.
    """
    crash = (report.get("crash_error") or "") if isinstance(report, dict) else ""
    # code_tester nests crash info at script_checks.crash_error (not top-level).
    # Without this fallback, AttributeError tracebacks are invisible to the
    # classifier → misclassified as "low quality" instead of "code error".
    if not crash and isinstance(report, dict):
        _sc = report.get("script_checks")
        if isinstance(_sc, dict):
            crash = _sc.get("crash_error") or _sc.get("error_message") or ""
    items = _extracted_item_count(report or {})
    is_timeout = bool(_TIMEOUT_RE.search(crash))
    is_blocked = bool(_BLOCKED_RE.search(crash))
    is_rate_limited = bool(_RATE_LIMITED_RE.search(crash))
    is_traceback = bool(_TRACEBACK_RE.search(crash)) and not is_timeout
    is_selector_crash = bool(_SELECTOR_CRASH_RE.search(crash))
    strat = (strategy or "").strip()
    is_http_like = strat in _HTTP_LIKE_STRATEGIES
    # W8: discovery stopped on browser-service backpressure (429 throttle).
    _cov_dc = report.get("discovery_coverage") if isinstance(report, dict) else None
    is_throttled = (
        isinstance(_cov_dc, dict) and _cov_dc.get("stop_reason") == "navigate_throttled"
    )

    # Code bug: a wrong CSS selector — the strategy is fine, the selector is
    # wrong. This MUST be checked before the access/strategy branches below,
    # otherwise a selector crash on a viable browser strategy gets misrouted to
    # "switch strategy" and the cascade abandons the only working approach.
    if is_selector_crash:
        return ("scraper", f"selector crash — element not found ({crash[:80]})")

    # [A1] An explicit 429/too-many-requests is neither a strategy verdict nor
    # a code bug — the site rate-limited the window. Re-test the same draft
    # after a backoff (the router owns the backoff + the retest cap). Checked
    # before the zero-item branches for the same reason as is_throttled below.
    if is_rate_limited:
        return ("retest", f"rate limited (429) — back off and re-test ({crash[:80]})")

    # Browser-service backpressure: a run stopped by a 429 throttle is UNPROVEN
    # coverage, not a verdict on the strategy or the code. It must be checked
    # BEFORE the zero-item branches — a throttled run completes cleanly (no
    # traceback), so `items == 0 and is_http_like` would strategy-switch (the
    # strategy never got a fair window), and omitting "navigate_throttled" from
    # the coverage-FAIL set would otherwise just fall through to "refine"
    # (code_writer tweaking field mappings on a zero-item run — equally wrong).
    if is_throttled:
        return (
            "retest",
            (
                "navigate throttled (browser-service backpressure) — re-test, "
                "no strategy switch"
            ),
        )

    # Access/strategy-class: the strategy can't reach the content.
    if is_timeout and strat in ("playwright", "stealth_browser", "seleniumbase_uc", ""):
        return ("strategy", f"playwright timed out ({crash[:80]})")
    if items == 0 and is_http_like and not is_traceback:
        return ("strategy", f"{strat} returned no items ({is_blocked and 'blocked' or 'empty'})")
    if items == 0 and is_blocked and not is_traceback:
        return ("strategy", f"blocked ({crash[:80]})")
    # Code bug: a real exception (not a timeout/blocked access issue).
    if is_traceback:
        return ("scraper", f"code error ({crash[:80]})")
    # Job-311: a TRANSIENT site-side render block is not a strategy verdict.
    # When the newest test output stopped with empty_render while an earlier
    # output in the same phase carried items (evidence attached by
    # _attach_transient_render_evidence), the draft+strategy demonstrably
    # work — re-test the same draft ("retest" action: same draft, no strategy
    # switch, the rung is NOT recorded tried) instead of burning it on a block
    # window that may already have passed (job 311's had).
    _tr = report.get("discovery_transient") if isinstance(report, dict) else None
    if items == 0 and isinstance(_tr, dict) and _tr.get("suspected"):
        return (
            "retest",
            "transient render block — newest test run rendered 0 items "
            f"(stop_reason={_tr.get('latest_stop_reason')}) while an earlier "
            f"run in this phase extracted {_tr.get('best_items')} items; "
            "re-test the same draft, no strategy switch",
        )
    # Items extracted but poor → let the caller decide mapping vs refine.
    if items == 0:
        return ("strategy", "no items extracted — likely wrong strategy")
    # Discovery-coverage failure: items WERE extracted, but discovery gave up
    # (navigate_error/dedup_flat). This is a strategy/access problem (the current
    # approach can't cover the source), not a field-quality problem — switch
    # strategy rather than "refine".
    _cov = _discovery_coverage_failure(report)
    if _cov:
        return ("strategy", _cov)
    return ("refine", f"{items} items, low quality")

MIN_CONFIDENCE_PASS = 0.85
MIN_CONFIDENCE_PARTIAL = 0.5

SOFT_404_MARKERS = (
    "soft 404",
    "product not found",
    "no longer available",
    "discontinued",
    "not a product page",
)


def _is_dead_product(p: dict) -> bool:
    status = p.get("status_code", 200)
    if status in DEAD_STATUS_CODES:
        return True
    remarks = (p.get("remarks") or "").lower()
    if any(marker in remarks for marker in SOFT_404_MARKERS):
        return True
    return False


def _is_discovery_output(data: dict) -> bool:
    """Is this output file a ``--discover-only`` artifact (URL stubs, no
    extraction)?

    Two-phase templates tag their output with ``metadata.phase`` (job-76
    myhouse: the discovery file's 40 ``{url, src_url}`` stubs polluted every
    consumer that just counted rows). Files without the tag are treated as
    extraction — pre-tagging outputs keep their old meaning.
    """
    try:
        return (data.get("metadata") or {}).get("phase") == "discovery"
    except AttributeError:
        return False


def _scraper_produced_valid_output(state: ScrapeState) -> bool:
    """Did the scraper produce at least 1 valid item? (output-file-aware)

    Previously read only ``report.sample_products`` — a field code_tester NEVER
    populates (not in its prompt schema). The function could essentially never
    return True. Now delegates to ``_scraper_has_real_items`` which has the
    3-tier fallback: report → sample_output → output JSON files on disk.
    """
    # Adaptive min_count: url_list/list_page jobs extract from specific URLs —
    # 1 real item with rich data IS a success. Navigation/search_term jobs need
    # 3+ to prove discovery worked.
    _mode = (state.get("input_mode") or "").strip()
    _min = 1 if _mode in ("url_list", "list_page") else 3
    return _scraper_has_real_items(state, min_count=_min)


def _freshness_floor(state: ScrapeState, draft_path: str) -> float:
    """[A6] Output-freshness floor keyed to the draft's CONTENT, not its mtime.

    Job 309's floor (draft mtime) stops outputs from PREVIOUS drafts rescuing
    the current one — but a no-op fix cycle (A2) re-tests a byte-identical
    draft, and an unchanged draft's mtime stays old: the previous attempt's
    outputs remain >= floor and the best-of-5 scan happily reads a stale run
    that predates the current test. When the draft fingerprint matches the one
    recorded at last test time (state.last_tested_draft_fp, written by
    _invoke_code_tester), raise the floor to last_tested_at so only THIS
    attempt's outputs count as ground truth.
    """
    import hashlib as _hash
    import os as _os

    floor = 0.0
    try:
        if draft_path and _os.path.isfile(draft_path):
            floor = _os.path.getmtime(draft_path)
            with open(draft_path, "rb") as fh:
                fp = _hash.sha1(fh.read()).hexdigest()
            last_fp = str(state.get("last_tested_draft_fp") or "")
            try:
                last_at = float(state.get("last_tested_at") or 0.0)
            except (TypeError, ValueError):
                last_at = 0.0
            if fp and last_fp and fp == last_fp and last_at > floor:
                floor = last_at
    except Exception:
        pass
    return floor


def _scraper_has_real_items(state: ScrapeState, min_count: int = 3) -> bool:
    """GROUND-TRUTH check: did the scraper extract enough real items to PASS?

    Overrides the code_tester LLM's subjective assessment (it's strict + variance-
    prone). The success criterion is: enough items with a ``title`` AND at least
    one of the content type's core fields — content-type-aware via
    ``src.content_types.output_filter_fields`` (products: price/availability;
    jobs: company/location; articles: author/publish_date). A scraper that
    extracted real jobs (title+company) WORKS, even though it has no price.

    Checks (in order): test_report.sample_products → test_report.sample_output →
    the actual output JSON file in workspace/ (the true ground truth).
    """
    report = state.get("test_report") or {}
    sample_products = report.get("sample_products") or []
    if not sample_products:
        so = report.get("sample_output") or {}
        if isinstance(so, dict):
            ct_config = state.get("content_type_config") or {}
            output_key = ct_config.get("output_key", "products") if ct_config else "products"
            sample_products = so.get(output_key) or so.get("products", [])

    # Content-type-aware "real item" check (price for products, company/location
    # for jobs, etc.). Falls back to title-only for unknown content types.
    # [job-73 F2] Computed BEFORE the file scan: the scan now ranks candidate
    # output files by GOOD item count, which needs this predicate.
    try:
        from src.content_types import output_filter_fields
        ct_config = state.get("content_type_config") or {}
        ct = ct_config.get("content_type", "") if ct_config else ""
        fields = output_filter_fields(ct) or []
    except Exception:
        fields = []
    try:
        from src.content_types import has_substantive_field
    except Exception:
        has_substantive_field = lambda p: bool(p.get("title"))  # type: ignore

    # FALLBACK: read the actual output JSON file (ground truth from the scraper run)
    if not sample_products:
        try:
            import json as _json
            import os as _os
            slug = state.get("site_slug", "")
            root = _os.environ.get("PROJECT_ROOT", "/app")
            ws = _os.path.join(root, "workspace", slug)
            if _os.path.isdir(ws):
                # FRESHNESS FLOOR (job 309 / F16 lesson): only outputs produced
                # by the CURRENT draft count as ground truth. Without this, the
                # scan below happily reads output files left by PREVIOUS retry
                # cycles' drafts — job 309: the http_requests/http_navigation
                # cycles' verification outputs (a few real items) rescued the
                # exhausted playwright draft into execution, which then
                # extracted 0 and finalized COMPLETED. The tester always runs
                # the current draft AFTER the writer's last edit, so a legit
                # rescue's outputs are always >= the draft's mtime.
                _floor = _freshness_floor(
                    state, _os.path.join(ws, "scraper_draft.py")
                )
                outs = sorted(
                    [f for f in _os.listdir(ws)
                     if f.startswith("output_") and f.endswith(".json")
                     and _os.path.getmtime(_os.path.join(ws, f)) >= _floor],
                    key=lambda f: _os.path.getmtime(_os.path.join(ws, f)),
                    reverse=True,
                )
                if not outs:
                    # [job-73 F5] The floor is the only thing that can empty
                    # this scan — say so, loudly. Job 73's 20/20 output was
                    # excluded with zero signal; only the routing warning
                    # remained, and the failure looked like "no items".
                    logger.warning(
                        "route_after_testing: ground-truth scan found NO output "
                        "files in %s — freshness floor %.0f excluded everything "
                        "(draft mtime %.0f, last_tested_at %.0f). If outputs "
                        "exist on disk with later mtimes, the A6 floor is "
                        "blinding the current attempt.",
                        ws, _floor,
                        _os.path.getmtime(_os.path.join(ws, "scraper_draft.py"))
                        if _os.path.isfile(_os.path.join(ws, "scraper_draft.py")) else 0.0,
                        float(state.get("last_tested_at") or 0.0),
                    )
                ct_config = state.get("content_type_config") or {}
                output_key = ct_config.get("output_key", "products") if ct_config else "products"
                # Bug A fix: take the BEST output file (max real items), not the
                # newest. code_tester runs Phase 2 first (5 items) then Phase 1
                # (1 item — discovery crash). The newest file is always the WORST
                # result → 1 < 3 → cascade. Taking MAX across the last 5 files
                # lets the 5-item Phase-2 result pass the gate.
                # [job-73 F2] "Best" = most GOOD items (core-field-carrying,
                # non-dead), not most raw rows: an untagged --discover-only
                # stub file (40 {url, src_url} rows) must not outrank a
                # 20-row extraction file and then fail the filter below.
                _best_items = []
                _best_good = 0
                for _out_name in outs[:5]:
                    try:
                        _data = _json.load(open(_os.path.join(ws, _out_name)))
                        # job-76 myhouse: a --discover-only run's output holds
                        # URL stubs ({url, src_url}), never extraction — it
                        # must not stand in for Phase-2 ground truth.
                        if _is_discovery_output(_data):
                            continue
                        _items = _data.get(output_key) or _data.get("products", [])
                        _live_i = [p for p in _items if not _is_dead_product(p)]
                        _good_i = (
                            [p for p in _live_i if any(p.get(f) for f in fields)]
                            if fields
                            else [p for p in _live_i if has_substantive_field(p)]
                        )
                        if len(_good_i) > _best_good:
                            _best_items = _items
                            _best_good = len(_good_i)
                    except Exception:
                        pass
                sample_products = _best_items
        except Exception:
            pass

    # Content-type-aware "real item" check — fields/has_substantive_field are
    # computed above (the file scan ranks by them too).
    live = [p for p in sample_products if not _is_dead_product(p)]
    if fields:
        # F15: when the content type defines filter fields, a "real" item must
        # carry at least one of THEM — not just any substantive field. The old
        # `or has_substantive_field(p)` escape let job 337 ship 36 rows whose
        # only populated field was `brand` while price/availability sat at 0%
        # (the ground-truth override then blessed a FAIL/0.35 test report).
        # `any(filter_fields)` (not all-fields) so a product missing only
        # availability/currency still counts — 320's 511 rows stay safe.
        good = [p for p in live if any(p.get(f) for f in fields)]
    else:
        good = [p for p in live if has_substantive_field(p)]
    if len(good) >= min_count:
        return True

    # OUTPUT-AS-TRUTH FALLBACK: code_tester is an LLM that independently
    # re-derives "what counts as success" — it may report sample_products=[]
    # despite the output file having real items (the whack-a-mole root cause:
    # every stage independently judges). The output file is the GROUND TRUTH.
    # Read it directly + count real items with the content-type-agnostic
    # has_substantive_field predicate. This bypasses the LLM's summary.
    try:
        import os as _os, glob as _glob, json as _json
        _slug = state.get("site_slug", "")
        if _slug:
            _root = _os.environ.get("PROJECT_ROOT", "/app")
            # Same freshness floor as the primary fallback above (job 309): a
            # previous cycle's output must not rescue the current draft.
            _draft2 = _os.path.join(_root, "workspace", _slug, "scraper_draft.py")
            _floor2 = _freshness_floor(state, _draft2)
            _pattern = _os.path.join(_root, "workspace", _slug, "output_*.json")
            _outputs = sorted(
                [p for p in _glob.glob(_pattern) if _os.path.getmtime(p) >= _floor2],
                key=lambda f: _os.path.getmtime(f), reverse=True,
            )
            # Bug A fix: iterate the last 5 outputs, take the one with the MOST
            # real items (not the newest — the newest may be the 1-item Phase-1
            # crash file while a 5-item Phase-2 file exists alongside it).
            for _out_path in _outputs[:5]:
                try:
                    with open(_out_path, "r") as _f:
                        _out = _json.load(_f)
                    if _is_discovery_output(_out):
                        continue  # discovery stubs are never extraction truth
                    for _ck in ("products", "jobs", "articles", "results", "items", "threads", "pages"):
                        _items = _out.get(_ck)
                        if isinstance(_items, list) and _items:
                            _live = [p for p in _items if not _is_dead_product(p)]
                            # F15: same core-field requirement as the primary
                            # predicate — a file full of brand-only rows must
                            # not rescue a failing test (job 337 pattern).
                            if fields:
                                _good = [p for p in _live if any(p.get(f) for f in fields)]
                            else:
                                _good = [p for p in _live if has_substantive_field(p)]
                            if len(_good) >= min_count:
                                logger.info(
                                    "route_after_testing: OUTPUT-AS-TRUTH rescue — found %d real "
                                    "items in %s (≥%d) → PASS",
                                    len(_good), _os.path.basename(_out_path), min_count,
                                )
                                return True
                except Exception:
                    pass
    except Exception:
        pass
    return False


# Back-compat alias for any external callers.
_scraper_has_priced_products = _scraper_has_real_items


def _core_field_zero_coverage(report: dict, state: ScrapeState) -> str | None:
    """Return the name of a CORE output field the scraper left at ~0% coverage.

    code_tester records per-field coverage in ``results.field_coverage`` (e.g.
    ``{"company": {"count": 0, "coverage": "0%", "status": "MISSING"}}``) but
    only promotes SOME problems into the ``issues`` list.  A systematically
    empty core field is a mapping bug (the code-writer pulled from the wrong
    source) and must trigger a retry so the loop remaps it — otherwise a
    scraper that silently drops a required field ships as PASS.
    Returns the missing field name, or None.
    """
    results = report.get("results") or {}
    field_coverage = results.get("field_coverage") or {}
    if not field_coverage:
        return None
    # Only meaningful when the scrape actually extracted rows (otherwise the
    # whole run failed for other reasons and those issues already dominate).
    successful = results.get("successful_extractions", 0)
    try:
        successful = int(successful)
    except (ValueError, TypeError):
        successful = 0
    if successful <= 0:
        return None

    page_type = (state.get("page_type") or "product").lower()
    core: set[str] = set()
    try:
        from src.content_types import get_content_type

        ct = get_content_type(page_type)
        if ct and getattr(ct, "core_fields", None):
            core = {str(f).lower() for f in ct.core_fields}
    except Exception:
        pass
    cfg = state.get("content_type_config") or {}
    if cfg.get("core_field_names"):
        core = {str(f).lower() for f in cfg["core_field_names"]}
    if not core:
        return None

    for field, info in field_coverage.items():
        if not isinstance(info, dict) or str(field).lower() not in core:
            continue
        status = str(info.get("status") or "").upper()
        cov = info.get("coverage", info.get("coverage_pct"))
        is_zero = status == "MISSING"
        if isinstance(cov, str):
            try:
                is_zero = is_zero or float(cov.strip().rstrip("%")) < 1.0
            except ValueError:
                pass
        elif isinstance(cov, (int, float)):
            is_zero = is_zero or float(cov) < 1.0
        if is_zero:
            return field
    return None


# ── T2.1/T2.2/T2.3 deterministic output signals ──────────────────────────────
# The tester's LLM misses mechanical defects (double-joined URLs, inverted
# price pairs, mapped-but-empty fields); these checks read the OUTPUT rows
# directly and never depend on the model noticing. Severity stays MEDIUM —
# routing arms only on the WRONG_VALUE+anchored-fix shape (see
# _det_blockers in route_after_testing), not on severity.

_SAMPLE_CAP = 5  # code_tester's --sample run is 5-bounded
_PRICE_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")
_EMPTY_STRINGS = ("", "none", "null", "[]", "n/a", "na")
# Job-303: product_analyzer mapped `ratings` → numberOfReviews (a COUNT), the
# writer shipped it, and every review-bearing product got its review count as
# its star rating. A rating must be the average/star VALUE.
_RATING_FIELDS = frozenset({
    "ratings", "rating", "average_rating", "avg_rating", "rating_value",
    "star_rating", "averagerating",
})
_COUNT_TOKEN_RE = re.compile(
    r"num(?:ber)?\s*_?of\s*_?reviews|review\s*_?count|reviews?\s*_?count",
    re.IGNORECASE,
)


def _price_value(v) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = _PRICE_RE.search(str(v).replace(" ", " "))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _is_double_host(url: str) -> bool:
    """True for the double-join artifact: base_url + an already-absolute URL.

    Catches both shapes seen in the wild — a duplicated scheme
    (``https://hosthttps://host/path``) and a host repeated inside the path
    (``https://host/https://host/path`` or ``https://host/host/path``).
    """
    u = str(url or "").strip()
    if u.lower().count("://") > 1:
        return True
    m = re.match(r"^[a-z][a-z0-9+.\-]*://([^/?#]+)", u.lower())
    if not m:
        return False
    host = m.group(1).split("@")[-1].split(":")[0]
    rest = u.lower()[m.end():]
    return (f"//{host}" in rest) or (f"/{host}/" in rest)


def _items_from_output_file(slug: str, mtime_floor: float | None = None) -> list[dict]:
    """Read the newest scrape output's item rows (any content type).

    Scans every top-level list-of-dicts (products/articles/jobs/…) — no
    content-type coupling. Empty when nothing was written (the checks are
    then silent — same no-op-on-missing-data philosophy as the coverage gate).

    ``mtime_floor`` (job 306): the F16 best-of-N picker ranks by substantive
    count, so a PREVIOUS job's larger production output outranks this run's
    5-row sample and the checks validate the map against the wrong job's rows.
    Callers pass the current job's started_at so stale files are excluded —
    the same stale-artifact re-injection class as prod job 12.
    """
    if not slug:
        return []
    import json
    import os as _os

    try:
        from .run_execution import _find_newest_output
    except Exception:
        return []
    try:
        root = _os.environ.get("PROJECT_ROOT", "/app")
        path = _find_newest_output(
            _os.path.join(root, "workspace", slug),
            _os.path.join(root, "scrapers", slug),
            slug=slug,
            mtime_floor=mtime_floor,
        )
        if not path or not _os.path.isfile(path):
            return []
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except Exception as exc:
        logger.debug("_items_from_output_file: read failed for %s: %s", slug, exc)
        return []
    if not isinstance(data, dict):
        return []
    for v in data.values():
        if isinstance(v, list) and v and all(isinstance(r, dict) for r in v):
            return [r for r in v if isinstance(r, dict)]
    return []


def _job_started_floor(state: ScrapeState) -> float | None:
    """mtime floor for output reads: this job's started_at (job 306).

    A floor of None means "no job context" (unit tests, playground) — the
    legacy best-of-N pick then applies. Best-effort by design: a DB hiccup
    degrades to the legacy behavior rather than blinding the checks.
    """
    job_id = (state or {}).get("job_id") if isinstance(state, dict) else None
    if not job_id:
        return None
    try:
        from scraper.models import ScrapeJob

        started = ScrapeJob.objects.only("started_at").get(pk=job_id).started_at
    except Exception:
        return None
    if not started:
        return None
    return max(0.0, started.timestamp() - 5.0)


def deterministic_output_issues(slug: str, state: ScrapeState) -> list[dict]:
    """Deterministic checks over the newest OUTPUT file rows.

    Three checks, each emitting a tester-shaped issue dict (so they land in
    ``test_report.issues`` next to the LLM's own findings):

    1. double-host URL — base+absolute double-join on >50% of rows;
    2. inverted price pair — previous_price < current_price on >50% of rows
       with both present (orient by VALUE: lower=current);
    3. mapped-but-empty field (T2.3) — a field with a non-empty selector /
       api_path in the analysis field map that is non-empty on <20% of rows.

    Requires ≥3 rows (fewer is a different failure class, already routed).
    """
    rows = _items_from_output_file(slug, mtime_floor=_job_started_floor(state))
    if len(rows) < 3:
        return []
    issues: list[dict] = []
    n = len(rows)

    dbl = sum(
        1 for r in rows
        if _is_double_host(str(r.get("url") or r.get("src_url") or ""))
    )
    if dbl > n // 2:
        issues.append({
            "field": "url",
            "issue_type": "WRONG_VALUE",
            "severity": "medium",
            "count": dbl,
            "description": (
                f"Deterministic check: {dbl}/{n} item URLs contain the host twice "
                "(base_url + already-absolute URL double-join)"
            ),
            "suggested_fix": (
                "Join with urllib.parse.urljoin(base_url, raw_url) — urljoin passes "
                "absolute URLs through unchanged; never string-concatenate a base and "
                "an absolute path."
            ),
        })

    pairs = [
        (_price_value(r.get("previous_price")), _price_value(r.get("price")))
        for r in rows
    ]
    both = [(p, c) for p, c in pairs if p is not None and c is not None]
    if both:
        inv = sum(1 for p, c in both if p < c)
        if inv > len(both) // 2:
            issues.append({
                "field": "price",
                "issue_type": "WRONG_VALUE",
                "severity": "medium",
                "count": inv,
                "description": (
                    f"Deterministic check: {inv}/{len(both)} rows have previous_price "
                    "< current_price (inverted price pair)"
                ),
                "suggested_fix": (
                    "Orient price pairs by VALUE: the lower number is the CURRENT "
                    "price, the higher is the previous/was price — regardless of the "
                    "source field names; only fall back to naming when values tie."
                ),
            })

    analysis = state.get("content_analysis") or state.get("product_analysis") or {}
    fields = analysis.get("fields") if isinstance(analysis, dict) else None
    if isinstance(fields, dict):
        for fname, meta in fields.items():
            if not isinstance(meta, dict) or not fname:
                continue
            anchor = (
                meta.get("selector") or meta.get("api_path")
                or meta.get("api_fallback_path") or meta.get("jsonld_key")
                or meta.get("attribute") or ""
            )
            if not str(anchor).strip():
                continue
            filled = sum(
                1 for r in rows
                if str(r.get(fname) or "").strip().lower() not in _EMPTY_STRINGS
            )
            if filled / n < 0.2:
                issues.append({
                    "field": str(fname),
                    "issue_type": "MISSING",
                    "severity": "medium",
                    "count": n - filled,
                    "description": (
                        f"Deterministic check: field '{fname}' is mapped ({anchor!r}) "
                        f"but empty on {n - filled}/{n} rows"
                    ),
                    "suggested_fix": (
                        f"The mapping for '{fname}' yields nothing on the live pages — "
                        "re-anchor it to a populated source (verify the selector/api_path "
                        "against the live DOM/API and prefer the structured JSON-LD/API "
                        "value over a brittle CSS path) instead of shipping empty."
                    ),
                })

    # 4. rating mapped from a review-COUNT source (job-303 class). Deliberately
    # NOT the mapped-but-empty check: products without reviews legitimately
    # leave ratings empty, so sparseness alone is a false-positive magnet. The
    # defect is in the MAP — a rating field anchored at a count field is wrong
    # wherever it fires.
    if isinstance(fields, dict):
        for fname, meta in fields.items():
            if not isinstance(meta, dict) or str(fname).lower() not in _RATING_FIELDS:
                continue
            _map_text = " ".join(
                str(meta.get(k) or "")
                for k in ("json_path", "api_path", "api_fallback_path",
                          "selector", "jsonld_key")
            )
            # Structural anchors ONLY (job 306): 'notes' is prose — the analyzer
            # legitimately writes "only has numberOfReviews (a count, not a
            # rating value)" when DECLINING to map, and regexing that prose
            # fired the check on a correct artifact. A declined map has no
            # anchor at all → empty _map_text → silent (the right verdict).
            if not _COUNT_TOKEN_RE.search(_map_text):
                continue
            if not any(
                str(r.get(fname) or "").strip() not in _EMPTY_STRINGS
                for r in rows
            ):
                continue  # nothing extracted — different failure class
            issues.append({
                "field": str(fname),
                "issue_type": "WRONG_VALUE",
                "severity": "medium",
                "description": (
                    f"Deterministic check: '{fname}' is mapped to a review-COUNT "
                    f"source ({_map_text.strip()[:80]!r}) — it carries the number "
                    "of reviews, not the star/average value"
                ),
                "suggested_fix": (
                    f"Map '{fname}' to the average-rating VALUE field "
                    "(e.g. averageRating / rating_value), never a count field "
                    "(numberOfReviews / reviewCount). The count answers 'how "
                    "many reviews exist'; the rating answers 'what score'."
                ),
            })
    return issues


def _volume_gap(report: dict, state: ScrapeState) -> Optional[str]:
    """T2.1: discovered-vs-extracted gap, armed ONLY beyond sample scope.

    The tester's --sample run is 5-bounded: judging volume on it fails the run
    that SUCCEEDED (a job-302-shaped state — PASS / 5 extracted / 97 discovered
    — must stay silent). Arms when the tester extracted beyond the sample cap,
    the run is not scope-narrowed, discovery covered ≥2 pages' worth of URLs,
    and extraction captured <50% of them. Returns a reason string or None.
    """
    if not isinstance(report, dict):
        return None
    cov = report.get("discovery_coverage")
    if not isinstance(cov, dict) or not cov.get("ran_phase1", True):
        return None
    try:
        discovered = int(cov.get("found") or len(cov.get("discovered_urls") or []) or 0)
    except (TypeError, ValueError):
        return None
    if discovered <= 0:
        return None
    extracted = _extracted_item_count(report)
    if extracted <= _SAMPLE_CAP:
        return None  # sample-shaped run — volume is unknowable from it
    scope = (state.get("scope") or "").strip().lower()
    if scope in ("firstn", "filter") or (state.get("scope_value") or "").strip():
        return None
    nav = state.get("navigation_analysis")
    ep = (nav.get("api_endpoint") or {}) if isinstance(nav, dict) else {}
    ipp = ep.get("items_per_page")
    if not isinstance(ipp, int) or isinstance(ipp, bool) or ipp <= 0:
        ipp = cov.get("items_per_page")
        if not isinstance(ipp, int) or isinstance(ipp, bool) or ipp <= 0:
            return None  # no page-size denominator → gate stays silent
    if discovered >= 2 * ipp and extracted < 0.5 * discovered:
        return (
            f"volume gap: discovery found {discovered} URLs (≥2 pages of {ipp}) but "
            f"the run extracted only {extracted} (<50%) — pagination/discovery is "
            "stopping early, not the strategy"
        )
    return None


def route_after_testing(state: ScrapeState) -> str:
    report = state.get("test_report")
    retry_count = state.get("test_retry_count", 0)
    is_final_attempt = retry_count == FINAL_RETRY_SENTINEL

    # Route functions return only a node name (no state update possible), so
    # the exhausted-retries error is recorded by the CALLER (code_tester's
    # exhausted-retry return) — see _invoke_code_tester. This fn only routes.
    if not report:
        # [job-81 N-C] Two consecutive code_tester wall-clock deaths and no
        # verdict for the current attempt: the draft is UNJUDGED, not failed
        # — regenerating it (the default no-report ladder below) would
        # discard usable work. Escalate: a human decides re-test vs execute
        # vs cancel; under skip_approvals an auto-approved retry would just
        # burn a third window against the same wall, so it's an honest
        # cleanup instead. The interrupt_* fields are staged by the tester
        # node (routing functions cannot mutate state).
        _twc = int(state.get("tester_wall_clock_timeouts") or 0)
        if _twc >= 2:
            if state.get("skip_approvals", False):
                logger.error(
                    "route_after_testing: code_tester wall-clock ×2 + "
                    "skip_approvals → cleanup (honest failure, draft unjudged)"
                )
                return "cleanup"
            logger.error(
                "route_after_testing: code_tester wall-clock ×2, no verdict "
                "→ human_approval (draft unjudged)"
            )
            return "human_approval"
        if is_final_attempt:
            # Output-file rescue: even with no test_report, the scraper may have
            # produced real output during testing. Under skip_approvals,
            # field_confirmation goes straight to run_execution.
            _rescue_min = 1 if (state.get("input_mode") or "") in ("url_list", "list_page") else 3
            if _scraper_has_real_items(state, min_count=_rescue_min):
                logger.info(
                    "route_after_testing: no test_report (final) but output has real "
                    "items → field_confirmation (rescue)"
                )
                return "field_confirmation"
            logger.error(
                "route_after_testing: FINAL attempt produced no test_report → cleanup"
            )
            return "cleanup"
        if retry_count < MAX_TEST_RETRIES:
            logger.warning(
                "route_after_testing: no test_report, retry %d/%d via scraper_analyzer",
                retry_count + 1,
                MAX_TEST_RETRIES + 1,
            )
            return "scraper_analyzer"
        # Retries exhausted with no test_report — check output files before giving up.
        _rescue_min = 1 if (state.get("input_mode") or "") in ("url_list", "list_page") else 3
        if _scraper_has_real_items(state, min_count=_rescue_min):
            logger.info(
                "route_after_testing: no test_report after %d retries but output has "
                "real items → field_confirmation (rescue)"
            )
            return "field_confirmation"
        logger.error(
            "route_after_testing: no test_report after %d retries → cleanup",
            retry_count,
        )
        return "cleanup"

    assessment = report.get("overall_assessment", "FAIL")
    try:
        confidence = float(report.get("confidence_score", 0.0))
    except (ValueError, TypeError):
        confidence = 0.0
    issues = report.get("issues", [])
    high_severity = any(i.get("severity") == "high" for i in issues)

    # A core field left at ~0% coverage is a field-mapping bug — force a retry
    # so the code-writer remaps it to a populated source.
    missing_core = _core_field_zero_coverage(report, state)
    if missing_core and not high_severity:
        high_severity = True
        logger.warning(
            "route_after_testing: core field '%s' at ~0%% coverage — treating as "
            "high severity to force a remap retry",
            missing_core,
        )

    # Count-regression band (job-10 lesson: 3,616 → 68 undetected). Compare
    # the tester's extracted count against the best prior COMPLETED run on
    # this site — scope-matched (skipped when this run narrows: --limit/
    # --sample/scope=firstn) and banded (never a bare percentage), because
    # the tester's own --sample legitimately extracts 5 against a 3,616
    # baseline. Only a full-scope test run extracting a tiny fraction of the
    # prior catalog is a discovery-regression signal.
    _count_regression = None
    try:
        _slug_cr = (state.get("site_slug") or "").strip()
        _scope_cr = (state.get("scope") or "").strip().lower()
        _narrowed = _scope_cr in ("firstn", "filter") or bool(
            (state.get("scope_value") or "").strip()
            and _scope_cr == "firstn"
        )
        if _slug_cr and not _narrowed:
            from scraper.models import ScrapeJob as _SJ

            _prior_n = (
                _SJ.objects.filter(
                    site_folder__contains=_slug_cr, status=_SJ.STATUS_COMPLETED,
                    product_count__gt=0,
                )
                .exclude(pk=state.get("job_id") or 0)
                .order_by("-product_count")
                .values_list("product_count", flat=True)
                .first()
            )
            if _prior_n and _prior_n >= 20:
                _tested_n = int(
                    (report.get("results") or {}).get("successful_extractions") or 0
                )
                # tester --sample caps at 5; only compare when the tester ran
                # beyond sample size (a real mini-run) AND it's far below prior
                if _tested_n > 5 and _tested_n < (_prior_n * 0.10):
                    _count_regression = (
                        f"discovery regression: extracted {_tested_n} vs prior "
                        f"completed run's {_prior_n} on this site (<10%) — "
                        "pagination or category enumeration is likely missing"
                    )
    except Exception:
        pass

    # Discovery-coverage signal: computed once, used to (a) downgrade a
    # field-PASS, (b) bypass the ground-truth override, and (c) exempt the cascade
    # from the anti-bot downgrade. None ⇒ no coverage problem (gate is a no-op).
    _cov_reason = _discovery_coverage_failure(report)

    # T2.1 volume signal (sample-scope-guarded) + T2.2 deterministic output
    # blockers. The blocker shape is narrow by design: a WRONG_VALUE on a
    # value-shaped field WITH a non-empty suggested_fix (emitted only by the
    # deterministic checks — the LLM's unanchored WRONG_VALUEs stay advisory).
    # `src_url=listing` is BY DESIGN in two-phase scrapers and is never
    # flaggable, so src_url is deliberately NOT in the blocker field set.
    _volume_reason = _volume_gap(report, state)
    _det_blockers = [
        i for i in issues
        if str(i.get("issue_type") or "").upper() == "WRONG_VALUE"
        and (i.get("suggested_fix") or "").strip()
        and str(i.get("field") or "").lower()
        in ("url", "price", "previous_price", "original_price",
            "ratings", "rating", "average_rating")
    ]

    # L2 CLI-contract signal (docs/cli-contract-plan.md): static re-check of the
    # DRAFT — a violation means execution would be seed-only. Belt-and-braces
    # alongside the tester-side force-FAIL (the load-bearing closure lives in
    # _invoke_code_tester; this re-derivation makes routing LLM-proof).
    _contract_bad = False
    try:
        _slug_cb = (state.get("site_slug") or "").strip()
        _im_cb = (state.get("input_mode") or "").strip()
        if _slug_cb and _im_cb in ("navigation", "list_page", "search_term"):
            import os as _os

            _root_cb = _os.environ.get("PROJECT_ROOT", "/app")
            _draft_cb = _os.path.join(
                _root_cb, "workspace", _slug_cb, "scraper_draft.py"
            )
            if _os.path.isfile(_draft_cb):
                from ..nodes.run_execution import cli_contract_violation

                _sa_cb = state.get("scraper_analysis")
                _st_cb = (
                    (_sa_cb.get("strategy") or "")
                    if isinstance(_sa_cb, dict)
                    else ""
                )
                _contract_bad = (
                    cli_contract_violation(_draft_cb, _im_cb, _st_cb) is not None
                )
    except Exception as _exc_cb:
        logger.debug("route_after_testing: contract re-check errored: %s", _exc_cb)

    if (
        assessment == "PASS"
        and confidence >= MIN_CONFIDENCE_PASS
        and not high_severity
        and not _contract_bad
        and not _count_regression
        and not _volume_reason
        and not _det_blockers
    ):
        # Phase-coverage gate (deterministic backstop): for two-phase
        # navigation scrapers, a PASS is only valid if Phase 1 discovery was
        # actually exercised. code_tester historically ran `--input
        # input_urls.json` (bypassing discovery) — so pagination,
        # form-submit discovery, and dimension iteration all shipped
        # unvalidated (the locumtenens 25-vs-1000 bug hid here for this
        # reason). The tester reports phases_tested; if Phase 1 wasn't run,
        # downgrade PASS → re-test via scraper_analyzer (which re-runs with
        # discovery). Generic for any two-phase navigation/list_page/search_term job.
        _input_mode = (state.get("input_mode") or "").strip()
        if _input_mode in ("navigation", "list_page", "search_term"):
            _phases = report.get("phases_tested") if isinstance(report, dict) else None
            _phase1 = (
                _phases.get("phase1_discovery")
                if isinstance(_phases, dict)
                else None
            )
            if not _phase1:
                logger.warning(
                    "route_after_testing: PASS rejected — Phase 1 discovery was NOT "
                    "tested (tester used --input, bypassing discovery). Routing to "
                    "scraper_analyzer for a discovery-validating re-test."
                )
                return "scraper_analyzer"
        if _cov_reason:
            # Field quality is fine BUT discovery coverage is insufficient (scraper
            # gave up, or iterated too few dimensions). A field-PASS here would
            # rubber-stamp the locumtenens failure (38 good jobs, 3733 missed).
            # Fall through to the cascade so classify_test_failure routes to a
            # strategy switch (overall_assessment stays "PASS" — graph.py's
            # strategies_tried recording keys off _cov_bad too).
            logger.warning(
                "route_after_testing: field-PASS DOWNGRADED — discovery coverage "
                "insufficient (%s). Falling through to strategy cascade.",
                _cov_reason,
            )
        else:
            logger.info("route_after_testing: PASS (confidence=%.2f)", confidence)
            return "field_confirmation"

    # GROUND-TRUTH OVERRIDE (content-type-aware): if the scraper actually
    # extracted enough real items (title + a core field of the content type —
    # price for products, company/location for jobs), it WORKS — regardless of
    # code_tester's subjective high_severity flags. The output is the truth.
    # BUT: ≥3 real items does NOT mean full coverage (locumtenens had 38 real
    # jobs, 3733 missed) — so a coverage failure overrides ground-truth too.
    # F15: a core field at ~0% coverage ALSO overrides ground-truth (job 337:
    # 36 rows with only `brand` populated, price/availability empty, FAIL/0.35
    # report, shipped COMPLETED).
    # job-71 popsockets: the item-count bar matches _scraper_produced_valid_output
    # (1 for url_list/list_page, 3 for discovery-driven modes) — the old
    # hardcoded 3 meant a url_list job whose every URL extracted (1 rich item)
    # could never clear the override its own pre-check used.
    _override_min = 1 if (state.get("input_mode") or "").strip() in ("url_list", "list_page") else 3
    if (
        not _cov_reason
        and not missing_core
        and not _contract_bad
        and not _count_regression
        and not _volume_reason
        and not _det_blockers
        and _scraper_has_real_items(state, min_count=_override_min)
    ):
        logger.info(
            "route_after_testing: GROUND-TRUTH PASS — scraper produced ≥%d real "
            "items (overriding code_tester's high_severity flags)",
            _override_min,
        )
        return "field_confirmation"

    # T2.1 volume-gap bounce (bounded, contract-bounce shape): a big
    # discovered-vs-extracted gap on a beyond-sample run is a precisely-known
    # fix (pagination), not a strategy question.
    if _volume_reason:
        _retry_vg = int(state.get("test_retry_count", 0) or 0)
        if _retry_vg >= MAX_TEST_RETRIES or is_final_attempt:
            logger.error(
                "route_after_testing: %s — retries exhausted, proceeding", _volume_reason
            )
        else:
            logger.warning(
                "route_after_testing: %s — bouncing to code_writer", _volume_reason
            )
            # [A5] go straight to code_writer: scraper_analyzer re-picks the
            # SAME strategy (nothing about it failed) and the writer regenerates
            # from scratch; a pagination gap is a targeted edit.
            return "code_writer"

    # T2.2 deterministic-defect bounce (same shape): the WRONG_VALUE issues
    # carry a mechanical suggested_fix, so a targeted fix beats shipping.
    if _det_blockers:
        _retry_db = int(state.get("test_retry_count", 0) or 0)
        if _retry_db >= MAX_TEST_RETRIES or is_final_attempt:
            logger.error(
                "route_after_testing: %d deterministic output defects — retries "
                "exhausted, proceeding",
                len(_det_blockers),
            )
        else:
            logger.warning(
                "route_after_testing: %d deterministic output defects (e.g. %s) "
                "— bouncing to code_writer",
                len(_det_blockers),
                _det_blockers[0].get("description", "")[:120],
            )
            # [A5] direct to code_writer — the WRONG_VALUE carries a mechanical
            # suggested_fix; a strategy re-pick regenerates instead of editing.
            return "code_writer"

    # Count-regression bounce (bounded, same shape as the contract bounce):
    # a big discovery drop vs the site's prior run is a precisely-known fix
    # (pagination/categories), not a strategy question.
    if _count_regression:
        _retry_cr = int(state.get("test_retry_count", 0) or 0)
        if _retry_cr >= MAX_TEST_RETRIES or is_final_attempt:
            logger.error(
                "route_after_testing: %s — retries exhausted, proceeding", _count_regression
            )
        else:
            logger.warning("route_after_testing: %s — bouncing to code_writer", _count_regression)
            # [A5] direct to code_writer — missing pagination/categories is a
            # targeted edit to the working draft, not a strategy question.
            return "code_writer"

    # L2 CLI-contract bounce (before the strategy cascade — a contract violation
    # is a precisely-known fix, not a strategy question; classify_test_failure
    # would guess). Bounded: the test_retry_count bump in _invoke_code_writer
    # fires on every re-entry with a truthy test_report, so this loops at most
    # MAX_TEST_RETRIES times before the exhausted arms below take over.
    if _contract_bad:
        _retry_now = int(state.get("test_retry_count", 0) or 0)
        if _retry_now >= MAX_TEST_RETRIES or is_final_attempt:
            if state.get("skip_approvals", False):
                logger.error(
                    "route_after_testing: CLI contract violation + retries "
                    "exhausted + skip_approvals → cleanup (honest failure)"
                )
                return "cleanup"
            logger.error(
                "route_after_testing: CLI contract violation + retries "
                "exhausted → human_approval"
            )
            return "human_approval"
        logger.info(
            "route_after_testing: CLI contract violation → code_writer "
            "(targeted fix, retry %d/%d)",
            _retry_now + 1, MAX_TEST_RETRIES,
        )
        return "code_writer"

    if is_final_attempt:
        # Job 309 (pillowtalk e2e): under skip_approvals the human_approval
        # auto-approve EXECUTED the draft the tester had just marked
        # "Ready for execution: false" → 0-item output → finalize blessed it
        # COMPLETED. Same honest-failure treatment as the contract arm above
        # and the cascade arm below: with no human to break the loop, a final
        # FAIL goes to cleanup — UNLESS real items exist (ground truth beats
        # the tester's verdict, same rescue as the exhausted-cascade arm).
        if state.get("skip_approvals", False):
            _rescue_min = 1 if (state.get("input_mode") or "") in ("url_list", "list_page") else 3
            if _scraper_has_real_items(state, min_count=_rescue_min):
                logger.info(
                    "route_after_testing: FINAL attempt FAILED but output has real "
                    "items → field_confirmation (rescue, skip_approvals → run_execution)"
                )
                return "field_confirmation"
            logger.error(
                "route_after_testing: FINAL attempt FAILED (assessment=%s, "
                "confidence=%.2f) + skip_approvals, no real items → cleanup "
                "(honest failure, not auto-approved execution)",
                assessment,
                confidence,
            )
            return "cleanup"
        logger.error(
            "route_after_testing: FINAL attempt FAILED (assessment=%s, confidence=%.2f) "
            "→ human_approval",
            assessment,
            confidence,
        )
        return "human_approval"

    # ── Strategy cascade ────────────────────────────────────────────────
    # Classify WHY the test failed: access/strategy-class (switch strategy) vs
    # code bug (fix) vs mapping (remap). Deterministic guards override code_tester
    # so an unambiguous signal (timeout / 0-item http run / traceback) always
    # routes correctly even if the LLM mis-diagnoses. The failed strategy is
    # recorded into state.strategies_tried by _decide_strategy (the router
    # can't update state), so scraper_analyzer picks a DIFFERENT strategy.
    _strategy = (state.get("scraper_analysis") or {}).get("strategy", "")
    _action, _reason = classify_test_failure(report, _strategy)
    # Anti-bot guard: for anti-bot sites, playwright+cloak is the ONLY viable
    # strategy (http/api are blocked too), so switching away is futile — a 0-item
    # Playwright failure there is a code/cloak bug to FIX, not a strategy to switch.
    # Detect anti_bot from the probe result (same sources as run_execution._needs_cloak).
    _probe = state.get("probe_result") or {}
    _ab = _probe.get("anti_bot") if isinstance(_probe, dict) else None
    _conn = _probe.get("connectivity") if isinstance(_probe, dict) else None
    _meth = (
        (_probe.get("method") if isinstance(_probe, dict) else "")
        or (_conn.get("method_that_worked") if isinstance(_conn, dict) else "")
        or ""
    )
    _anti_bot = bool(
        state.get("anti_bot_detected")
        or (isinstance(_ab, dict) and _ab.get("detected"))
        or str(_meth).startswith(("uc_chrome", "cloak"))
    )
    if _action == "strategy" and _anti_bot and _strategy in ("playwright", "stealth_browser", "seleniumbase_uc", "http_navigation", "") and not _cov_reason:
        # Coverage failures are exempt from the anti-bot downgrade: a coverage gap
        # (gave-up / partial iteration) is a discovery-mechanics problem, not a
        # cloak problem — downgrading to "scraper" would swallow the switch and
        # silently rubber-stamp the under-coverage. (Only non-coverage strategy
        # failures on anti-bot sites get downgraded, as before.)
        _action, _reason = "scraper", f"anti-bot site: fix browser+cloak run ({_reason})"
    # code_tester's LLM diagnosis can refine an ambiguous "refine" into mapping.
    remediation = report.get("remediation") if isinstance(report, dict) else None
    if _action == "refine" and isinstance(remediation, dict):
        if remediation.get("target") == "mapping" and remediation.get("fields"):
            _action, _reason = "mapping", f"field gap: {remediation.get('fields')}"
        elif remediation.get("target") == "strategy":
            _action, _reason = "strategy", "code_tester: strategy"

    # Cap on the strategy/scraper cascade: the early-return branches below
    # previously bypassed MAX_TEST_RETRIES (only the LLM fallback enforced it).
    if retry_count >= MAX_TEST_RETRIES:
        if confidence >= MIN_CONFIDENCE_PARTIAL and _scraper_produced_valid_output(state):
            logger.warning(
                "route_after_testing: retries exhausted in cascade (count=%d, "
                "action=%s) → field_confirmation (partial valid output)",
                retry_count, _action,
            )
            return "field_confirmation"
        # For skip_approvals jobs (intake), human_approval auto-approves and
        # loops back to scraper_analyzer — creating an infinite retry cycle.
        # BUT: if the output files contain real items, route to field_confirmation
        # (which under skip_approvals goes straight to run_execution — NOT a loop).
        if state.get("skip_approvals", False):
            _rescue_min = 1 if (state.get("input_mode") or "") in ("url_list", "list_page") else 3
            if _scraper_has_real_items(state, min_count=_rescue_min):
                logger.info(
                    "route_after_testing: retries exhausted (count=%d, reason=%s) but "
                    "output has real items → field_confirmation (rescue, skip_approvals → run_execution)",
                    retry_count, _reason,
                )
                return "field_confirmation"
            logger.error(
                "route_after_testing: retries exhausted in cascade (count=%d, "
                "action=%s, reason=%s) → FAIL (skip_approvals job, no human to break the loop)",
                retry_count, _action, _reason,
            )
            return "cleanup"
        logger.warning(
            "route_after_testing: retries exhausted in cascade (count=%d, "
            "action=%s, reason=%s) → human_approval",
            retry_count, _action, _reason,
        )
        return "human_approval"

    # [A1/QW-3] "retest": unproven coverage (429 / browser-service throttle /
    # transient render block). Re-test the SAME draft — code_tester →
    # code_tester — after a backoff. The retest counter is maintained inside
    # _invoke_code_tester (same-draft fingerprint → increment, changed draft →
    # reset) because routing functions cannot mutate state. test_retry_count
    # deliberately does NOT advance: this is not a strategy or code failure,
    # and it must not consume the fix budget or approach FINAL_RETRY_SENTINEL.
    if _action == "retest":
        _retests = int(state.get("test_retest_count", 0) or 0)
        if _retests < 2:
            import time as _time

            _backoff = min(30.0 * (_retests + 1), 60.0)
            logger.warning(
                "route_after_testing: %s — re-testing the same draft after a "
                "%.0fs backoff (retest %d/2, retry budget untouched)",
                _reason, _backoff, _retests + 1,
            )
            _time.sleep(_backoff)
            return "code_tester"
        logger.warning(
            "route_after_testing: %s — retest budget exhausted (%d), treating "
            "as a code-fix failure",
            _reason, _retests,
        )
        _action = "scraper"

    if _action == "strategy":
        logger.info(
            "route_after_testing: STRATEGY switch — '%s' failed (%s) → scraper_analyzer "
            "(retry %d/%d)",
            _strategy or "(none)",
            _reason,
            retry_count + 1,
            MAX_TEST_RETRIES + 1,
        )
        return "scraper_analyzer"
    if _action == "scraper":
        logger.info(
            "route_after_testing: code bug (%s) → code_writer (targeted fix, retry %d/%d)",
            _reason,
            retry_count + 1,
            MAX_TEST_RETRIES + 1,
        )
        return "code_writer"

    if retry_count < MAX_TEST_RETRIES:
        # LLM-decided routing: code_tester's remediation.target says whether the
        # failure is a MAPPING problem (re-run product_analyzer for the field) or
        # a SCRAPER problem (regenerate the scraper, as before). If code_tester
        # didn't emit a remediation, default to the existing scraper_analyzer path.
        remediation = (report.get("remediation") or {}) if isinstance(report, dict) else {}
        remap_count = state.get("remap_count", 0) or 0
        if (
            isinstance(remediation, dict)
            and remediation.get("target") == "mapping"
            and remap_count < MAX_REMAPS
            and remediation.get("fields")
        ):
            logger.info(
                "route_after_testing: %s (confidence=%.2f) — mapping failure, re-map %s via "
                "product_analyzer (remap %d/%d, retry %d/%d)",
                assessment,
                confidence,
                remediation.get("fields"),
                remap_count + 1,
                MAX_REMAPS,
                retry_count + 1,
                MAX_TEST_RETRIES + 1,
            )
            return "product_analyzer"
        # [A5] Default: a refine-class failure (items extracted, quality gaps)
        # is a CODE fix — send it straight to code_writer. Routing through
        # scraper_analyzer re-picks the SAME strategy and the writer
        # regenerates from scratch (job 46: ~25 min of regenerate loops on a
        # draft that was one targeted edit away from passing).
        logger.info(
            "route_after_testing: %s (confidence=%.2f, high_severity=%s), "
            "retry %d/%d via code_writer (targeted fix)",
            assessment,
            confidence,
            high_severity,
            retry_count + 1,
            MAX_TEST_RETRIES + 1,
        )
        return "code_writer"

    if confidence >= MIN_CONFIDENCE_PARTIAL and _scraper_produced_valid_output(state):
        logger.warning(
            "route_after_testing: retries exhausted (count=%d, assessment=%s, "
            "confidence=%.2f) → field_confirmation (partial output with valid products)",
            retry_count,
            assessment,
            confidence,
        )
        return "field_confirmation"

    logger.warning(
        "route_after_testing: retries exhausted (count=%d, assessment=%s) "
        "→ human_approval",
        retry_count,
        assessment,
    )

    return "human_approval"
