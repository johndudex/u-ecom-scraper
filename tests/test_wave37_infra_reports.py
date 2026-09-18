"""[wave-37 W37-NEW-A] Infra signatures inside test reports must park, not
consume cascade retest arms.

Prod 671/672: 4 retest arms burned on BROWSER_SERVICE_UNAVAILABLE (the
tester's run_scraper /scrape 429'd; the outage text landed INSIDE the
written report via shell_tools) → jobs failed without ever executing. The
wave-16 B3 preflight park only catches the NO-report shape. The classifier
recognizes infra-only failure sets; a report carrying a REAL draft defect
alongside infra noise (or one that extracted real items) stays normal —
the cascade must keep judging those.
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

from webapp.agents.tools.browser_http import report_is_infra_blocked  # noqa: E402


def _report(issues, crash="", stop_reason=None):
    rep = {
        "issues": issues,
        "crash_error": crash,
        "discovery_coverage": {"stop_reason": stop_reason} if stop_reason else {},
    }
    return rep


def test_browser_service_unavailable_issue_is_infra():
    rep = _report([
        {
            "issue_type": "crash",
            "message": (
                "BROWSER_SERVICE_UNAVAILABLE: the browser-service gateway "
                "itself cannot serve this run (ConnectError). This is an "
                "infrastructure outage, NOT a site or scraper problem."
            ),
        }
    ])
    reason = report_is_infra_blocked(rep)
    assert reason and "BROWSER_SERVICE_UNAVAILABLE" in reason


def test_navigate_throttled_stop_reason_is_infra():
    reason = report_is_infra_blocked(
        _report([], stop_reason="navigate_throttled")
    )
    assert reason and "navigate_throttled" in reason


def test_429_in_crash_is_infra():
    assert report_is_infra_blocked(
        _report([], crash="RuntimeError: HTTP 429 after 3 attempt(s) — backpressure")
    )


def test_real_defect_is_not_infra():
    assert report_is_infra_blocked(
        _report([{"issue_type": "wrong_type", "message": "price is a string"}])
    ) is None


def test_mixed_report_is_not_infra():
    rep = _report([
        {"issue_type": "wrong_type", "message": "price is a string"},
        {"issue_type": "crash", "message": "BROWSER_SERVICE_UNAVAILABLE: gateway"},
    ])
    assert report_is_infra_blocked(rep) is None


def test_infra_report_with_items_is_not_infra():
    rep = _report([{"issue_type": "crash", "message": "BROWSER_SERVICE_UNAVAILABLE"}])
    rep["successful_extractions"] = 5
    assert report_is_infra_blocked(rep) is None


def test_infra_report_with_nested_items_is_not_infra():
    # code-tester nests the count under results (code-tester.md:99-100) — the
    # top-level-only read misreads a producing run as zero-item.
    rep = _report([{"issue_type": "crash", "message": "BROWSER_SERVICE_UNAVAILABLE"}])
    rep["results"] = {"successful_extractions": 7}
    assert report_is_infra_blocked(rep) is None


def test_empty_report_is_not_infra():
    assert report_is_infra_blocked({}) is None
    assert report_is_infra_blocked(None) is None
