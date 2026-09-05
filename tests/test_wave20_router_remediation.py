"""[wave-20 T1] The router must honor ``remediation.target == "scraper"``.

Prod RCA (jobs 359 michaelhill / 360 marimekko, 2026-09-05): both testers
diagnosed the exact code bug — ``remediation.target: "scraper"`` with a
concrete field — and both times ``classify_test_failure`` overrode the expert
diagnosis with a coarse heuristic, strategy-switched, and drove the writer
into regenerating near-identical drafts against the same context:

- 359 (cycle 2): items=0 on an http-like strategy → the zero-item branch
  (line ~258) fired "returned no items (empty)" — the tester's "parser never
  executes" diagnosis never reached the writer loop as a targeted fix.
- 360 (cycle 2): items=1 (the seed extracted 1/1) → the zero-item branches
  were skipped and ``_discovery_coverage_failure`` fired "all_tiers_blocked
  (gave up, not exhausted)" — the deterministic probe had stamped the
  coverage AFTER the tester named the mode-gate bug. A promotion guarding
  only the zero-item branch would have missed this job entirely.

Contract:

- ``remediation.target == "scraper"`` WITH a concrete diagnosis
  (``field``/``fix``/non-empty ``issues``) outranks the zero-item and
  discovery-coverage verdicts → ``("scraper", ...)`` (targeted code fix);
- every unproven-run guard keeps precedence: selector crash, absent draft,
  429, throttle, navigate_unavailable;
- vague remediation (no field/fix/issues) changes nothing;
- no remediation → legacy verdicts byte-for-byte;
- target=mapping keeps the job-118 early promo; target=strategy keeps the
  refine-upgrade at the caller.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

from webapp.agents.nodes.route_after_testing import (  # noqa: E402
    _discovery_coverage_failure,
    classify_test_failure,
)


# ─── Prod-shaped fixtures ─────────────────────────────────────────────────────

def _michaelhill_report() -> dict:
    """Job 359 cycle 2: http_requests, 0 items, real 200 bodies, parser inert."""
    return {
        "overall_assessment": "FAIL",
        "confidence_score": 0.95,
        "ready_for_execution": False,
        "results": {"successful_extractions": 0, "failed_extractions": 5},
        "crash_error": "",
        "discovery_coverage": {
            "ran_phase1": True,
            "stop_reason": "empty_first_page",
            "discovered_urls": 0,
        },
        "remediation": {
            "target": "scraper",
            "field": "extraction",
            "fix": "pure-Python JSON-LD parse; /p/ href filter + ItemList fallback",
        },
        "issues": [
            {"severity": "high", "message": "Phase 2 extraction 0/5 — parser never selects the JSON-LD Product block"},
        ],
    }


def _marimekko_report() -> dict:
    """Job 360 cycle 2: 1 seed extracted, probe-stamped all_tiers_blocked,
    tester's diagnosis = mode-gate bug (input_urls.json overrides flags)."""
    return {
        "overall_assessment": "NEEDS_FIXES",
        "confidence_score": 0.8,
        "ready_for_execution": False,
        "results": {"successful_extractions": 1},
        "crash_error": "",
        "discovery_coverage": {
            "ran_phase1": True,
            "stop_reason": "all_tiers_blocked",
            "discovered_urls": 0,
            "probe_scope": "discover_only",
        },
        "remediation": {
            "target": "scraper",
            "field": "discovery",
            "fix": "mode gate lets input_urls.json override --fresh-discovery",
        },
        "issues": [
            {"severity": "high", "message": "Phase 1: SKIPPED (url_list mode with 1 seed URLs)"},
        ],
    }


class TestPromotionOnProdShapes:
    def test_michaelhill_zero_item_diagnosis_outranks_empty(self):
        report = _michaelhill_report()
        action, why = classify_test_failure(report, "http_requests")
        assert action == "scraper", (
            f"expected targeted code fix, got ({action!r}, {why!r})"
        )
        assert "extraction" in why or "JSON-LD" in why

    def test_marimekko_coverage_diagnosis_outranks_all_tiers_blocked(self):
        report = _marimekko_report()
        # The coverage gate alone would say "discovery all_tiers_blocked" →
        # strategy-switch. The tester's scraper diagnosis outranks it.
        assert _discovery_coverage_failure(report)  # sanity: the branch is live
        action, why = classify_test_failure(report, "http_navigation")
        assert action == "scraper", (
            f"expected targeted code fix, got ({action!r}, {why!r})"
        )
        assert "discovery" in why or "mode gate" in why


class TestGuardsKeepPrecedence:
    def test_selector_crash_still_outranks_promotion(self):
        report = _michaelhill_report()
        report["crash_error"] = (
            "TimeoutError: Timeout 30000ms exceeded waiting for selector '.price'"
        )
        report["remediation"] = {"target": "scraper", "field": "extraction"}
        action, why = classify_test_failure(report, "playwright")
        assert action == "scraper"  # selector crash → same arm, code-fix reason
        assert "selector" in why or "element not found" in why

    def test_rate_limited_still_outranks_promotion(self):
        report = _michaelhill_report()
        report["crash_error"] = "429 Too Many Requests — rate limited"
        action, _ = classify_test_failure(report, "http_requests")
        assert action == "retest"

    def test_vague_scraper_remediation_changes_nothing(self):
        # target=scraper but NO concrete diagnosis → heuristic verdicts stand.
        report = _michaelhill_report()
        report["remediation"] = {"target": "scraper"}
        report["issues"] = []
        action, why = classify_test_failure(report, "http_requests")
        assert action == "strategy"
        assert "no items" in why


class TestLegacyBehaviorPreserved:
    def test_no_remediation_zero_item_http_still_strategy(self):
        report = _michaelhill_report()
        report.pop("remediation")
        report.pop("issues")
        action, why = classify_test_failure(report, "http_requests")
        assert action == "strategy"
        assert "no items" in why

    def test_no_remediation_coverage_failure_still_strategy(self):
        report = _marimekko_report()
        report.pop("remediation")
        report.pop("issues")
        action, why = classify_test_failure(report, "http_navigation")
        assert action == "strategy"
        assert "all_tiers_blocked" in why

    def test_traceback_still_code_error(self):
        report = _michaelhill_report()
        report.pop("remediation")
        report["crash_error"] = (
            "Traceback (most recent call last):\n  File \"scraper.py\", "
            "line 42, in <module>\nNameError: name 'session' is not defined"
        )
        action, why = classify_test_failure(report, "http_requests")
        assert action == "scraper"
        assert "code error" in why


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
