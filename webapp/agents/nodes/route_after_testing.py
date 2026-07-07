"""Routing function after the code-tester phase.

[ADP #6/#7 / HIP #6 / G2]
"""

import logging
import re

from ..constants import DEAD_STATUS_CODES, FINAL_RETRY_SENTINEL, MAX_REMAPS, MAX_TEST_RETRIES
from ..state import ScrapeState

logger = logging.getLogger(__name__)

# Strategies where "0 items extracted" means ACCESS failed (the strategy can't
# reach the content) — switch strategy, don't retry the same one.
_HTTP_LIKE_STRATEGIES = {"http_requests", "internal_api", "api", "requests", "shopify_api"}
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


def _extracted_item_count(report: dict) -> int:
    """Best-effort count of items the scraper actually extracted."""
    for key in ("successful_extractions", "extracted_items", "item_count"):
        v = report.get(key)
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


def classify_test_failure(report: dict, strategy: str) -> tuple[str, str]:
    """Classify a failed test into an action + reason.

    Returns (action, reason) where action ∈ {"strategy", "scraper", "mapping", "refine"}:
    - "strategy": access/strategy-class failure → switch strategy (timeout, 0-item
      http/api run, blocked). The current strategy can't reach the content.
    - "scraper": a code bug (Python traceback, not a timeout) → fix the scraper.
    - "mapping": items extracted but a core field is at ~0% → re-map (caller checks).
    - "refine": items extracted, fields mostly present, low quality → tweak.

    Deterministic — used as a guard that can override code_tester's LLM diagnosis.
    """
    crash = (report.get("crash_error") or "") if isinstance(report, dict) else ""
    items = _extracted_item_count(report or {})
    is_timeout = bool(_TIMEOUT_RE.search(crash))
    is_blocked = bool(_BLOCKED_RE.search(crash))
    is_traceback = bool(_TRACEBACK_RE.search(crash)) and not is_timeout
    strat = (strategy or "").strip()
    is_http_like = strat in _HTTP_LIKE_STRATEGIES

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
    # Items extracted but poor → let the caller decide mapping vs refine.
    if items == 0:
        return ("strategy", "no items extracted — likely wrong strategy")
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


def _scraper_produced_valid_output(state: ScrapeState) -> bool:
    report = state.get("test_report") or {}
    sample_products = report.get("sample_products") or []
    if not sample_products:
        return False
    live_products = [p for p in sample_products if not _is_dead_product(p)]
    valid = [p for p in live_products if p.get("title") and p.get("price")]
    return len(valid) > 0


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

    # FALLBACK: read the actual output JSON file (ground truth from the scraper run)
    if not sample_products:
        try:
            import json as _json
            import os as _os
            slug = state.get("site_slug", "")
            root = _os.environ.get("PROJECT_ROOT", "/app")
            ws = _os.path.join(root, "workspace", slug)
            if _os.path.isdir(ws):
                outs = sorted(
                    [f for f in _os.listdir(ws) if f.startswith("output_") and f.endswith(".json")],
                    key=lambda f: _os.path.getmtime(_os.path.join(ws, f)),
                    reverse=True,
                )
                if outs:
                    data = _json.load(open(_os.path.join(ws, outs[0])))
                    ct_config = state.get("content_type_config") or {}
                    output_key = ct_config.get("output_key", "products") if ct_config else "products"
                    sample_products = data.get(output_key) or data.get("products", [])
        except Exception:
            pass

    # Content-type-aware "real item" check (price for products, company/location
    # for jobs, etc.). Falls back to title-only for unknown content types.
    try:
        from src.content_types import output_filter_fields
        ct_config = state.get("content_type_config") or {}
        ct = ct_config.get("content_type", "") if ct_config else ""
        fields = output_filter_fields(ct) or []
    except Exception:
        fields = []
    live = [p for p in sample_products if not _is_dead_product(p)]
    if fields:
        good = [p for p in live if p.get("title") and any(p.get(f) for f in fields)]
    else:
        good = [p for p in live if p.get("title")]
    return len(good) >= min_count


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


def route_after_testing(state: ScrapeState) -> str:
    report = state.get("test_report")
    retry_count = state.get("test_retry_count", 0)
    is_final_attempt = retry_count == FINAL_RETRY_SENTINEL

    if not report:
        if is_final_attempt:
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

    if assessment == "PASS" and confidence >= MIN_CONFIDENCE_PASS and not high_severity:
        logger.info("route_after_testing: PASS (confidence=%.2f)", confidence)
        return "field_confirmation"

    # GROUND-TRUTH OVERRIDE (content-type-aware): if the scraper actually
    # extracted enough real items (title + a core field of the content type —
    # price for products, company/location for jobs), it WORKS — regardless of
    # code_tester's subjective high_severity flags. The output is the truth.
    if _scraper_has_real_items(state, min_count=3):
        logger.info(
            "route_after_testing: GROUND-TRUTH PASS — scraper produced ≥3 real "
            "items (overriding code_tester's high_severity flags)"
        )
        return "field_confirmation"

    if is_final_attempt:
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
    # recorded into state.strategies_tried by _invoke_scraper_analyzer (the router
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
    if _action == "strategy" and _anti_bot and _strategy in ("playwright", "stealth_browser", "seleniumbase_uc", ""):
        _action, _reason = "scraper", f"anti-bot site: fix playwright+cloak run ({_reason})"
    # code_tester's LLM diagnosis can refine an ambiguous "refine" into mapping.
    remediation = report.get("remediation") if isinstance(report, dict) else None
    if _action == "refine" and isinstance(remediation, dict):
        if remediation.get("target") == "mapping" and remediation.get("fields"):
            _action, _reason = "mapping", f"field gap: {remediation.get('fields')}"
        elif remediation.get("target") == "strategy":
            _action, _reason = "strategy", "code_tester: strategy"

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
        logger.info(
            "route_after_testing: %s (confidence=%.2f, high_severity=%s), "
            "retry %d/%d via scraper_analyzer",
            assessment,
            confidence,
            high_severity,
            retry_count + 1,
            MAX_TEST_RETRIES + 1,
        )
        return "scraper_analyzer"

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
