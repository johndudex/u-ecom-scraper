"""[wave-56b F3b] The transport-wall directive must speak the probe's REAL
vocabulary — and prescribe the OPPOSITE fix for fingerprint rungs.

Deep-audit proof (jobs 580/581, 2026-10-09): the wave-56 gate matched
``browser|fingerprint|stealth``, but the probe ladder emits ``direct_http*``,
``fingerprint_<profile>_<tier>``, ``playwright_<tier>``, ``cloak_<tier>``,
``uc_chrome*`` — so the directive was DEAD for nordstrom's ``cloak_none``
(and every playwright/uc_chrome probe), and WRONG where it did fire: a
``fingerprint_*`` method means the probe PROVED the site serves HTTP with a
browser TLS stack (``js_rendering_needed`` False), so telling the writer to
"move the fetch layer to the browser-class method" is backwards. Job 580
received that mis-prescription twice.
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


def _report(**over):
    report = {
        "overall_assessment": "FAIL",
        "confidence_score": 0.2,
        "results": {"successful_extractions": 0, "failed_extractions": 3},
        "issues": [
            {
                "severity": "high",
                "field": "discovery",
                "problem": "Every fetch was rejected",
                "details": "GET /catalogue/ -> HTTP 406 Not Acceptable "
                           "(vanilla urllib3/requests TLS client fingerprint)",
                "suggested_fix": "Switch the fetch layer",
            },
        ],
        "remediation": {"target": "scraper", "fields": [], "reason": "all 406"},
    }
    report.update(over)
    return report


def _state(report, method, **over):
    state = {
        "site_slug": "test-site",
        "job_id": 1,
        "test_report": report,
        "test_retry_count": 1,
        "probe_result": {"connectivity": {"method_that_worked": method}},
    }
    state.update(over)
    return state


def _summary(state):
    from agents.subagents import _summarize_test_report

    return _summarize_test_report(state)


class TestRenderClassProbesGetTheDirective:
    """The browser-class rungs the ladder actually emits."""

    @pytest.mark.parametrize("method", [
        "playwright_none",
        "cloak_residential",
        "uc_chrome_none",
        "browser_none",          # legacy vocabulary, still honoured
    ])
    def test_real_browser_rungs_fire_the_wall(self, method):
        out = _summary(_state(_report(), method))
        assert "TRANSPORT WALL" in out, f"{method} must fire the directive"
        assert method in out

    def test_wall_names_the_http_evidence_not_selectors(self):
        out = _summary(_state(_report(), "cloak_none"))
        assert "Fix the TRANSPORT" in out


class TestFingerprintProbesGetTheHonestDirective:
    """fingerprint_* = HTTP with a browser TLS stack ALREADY worked. The
    directive must say 'replicate the proven TLS client', never 'switch to
    a browser'."""

    def test_fingerprint_method_fires_a_tls_directive(self):
        out = _summary(_state(_report(), "fingerprint_chrome_none"))
        assert "TRANSPORT WALL" in out
        assert "TLS" in out
        assert "fingerprint_chrome_none" in out

    def test_fingerprint_directive_does_not_prescribe_a_browser(self):
        out = _summary(_state(_report(), "fingerprint_chrome_none"))
        assert "move the fetch layer to the browser-class method" not in out


class TestSilenceArms:
    def test_direct_http_probe_gets_no_directive(self):
        out = _summary(_state(_report(), "direct_http_none"))
        assert "TRANSPORT WALL" not in out

    def test_no_wall_evidence_gets_no_directive(self):
        rep = _report()
        rep["issues"][0]["details"] = "selector .price not found in DOM"
        out = _summary(_state(rep, "playwright_none"))
        assert "TRANSPORT WALL" not in out

    def test_successful_extractions_get_no_directive(self):
        rep = _report(results={"successful_extractions": 2,
                               "failed_extractions": 1})
        out = _summary(_state(rep, "playwright_none"))
        assert "TRANSPORT WALL" not in out
