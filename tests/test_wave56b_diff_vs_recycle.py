"""[wave-56b F1b] The DIFFERENTIATION directive must not lie on execution
recycles.

Deep-audit proof (2026-10-09): the wave-56 evidence builder fires
"DIFFERENTIATION REQUIRED … was already tested and FAILED" whenever
``retry_count and last_tested_draft_fp`` are set — but an execution-recycle
cycle (F4's zero-yield arm) hands the writer a report whose draft PASSED
testing and died at execution. Telling the writer it must differ from a
FAILED draft is a false assertion on a passing draft; the directive must
read the recycle's actual verdict instead.
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


def _passing_report():
    return {
        "overall_assessment": "PASS",
        "confidence_score": 0.9,
        "results": {"successful_extractions": 3, "failed_extractions": 0},
        "issues": [],
    }


def _state(**over):
    state = {
        "site_slug": "test-site",
        "job_id": 1,
        "test_report": _passing_report(),
        "test_retry_count": 1,
        "last_tested_draft_fp": "abc123",
        "feedback_for_writer": (
            "EXECUTION RECYCLE (url_list) — the draft passed testing but "
            "shipped 0 rows for 2 submitted seeds; rerun with a wider net."
        ),
    }
    state.update(over)
    return state


def _summary(state):
    from agents.subagents import _summarize_test_report

    return _summarize_test_report(state)


class TestRecycleArm:
    def test_recycled_url_list_report_is_not_called_failed(self):
        out = _summary(_state())
        assert "was already tested and FAILED" not in out
        assert "DIFFERENTIATION REQUIRED" not in out

    def test_recycled_report_names_the_true_verdict(self):
        out = _summary(_state())
        assert "EXECUTION RECYCLE" in out
        assert "passed testing" in out

    def test_nav_recycle_report_same_honesty(self):
        out = _summary(_state(feedback_for_writer=(
            "EXECUTION RECYCLE — the draft passed testing but produced 0 "
            "item rows; retry execution once with the same strategy."
        )))
        assert "was already tested and FAILED" not in out
        assert "EXECUTION RECYCLE" in out


class TestRealFailuresUnchanged:
    def test_genuine_retry_fail_still_demands_differentiation(self):
        out = _summary(_state(
            test_report={
                "overall_assessment": "FAIL",
                "confidence_score": 0.2,
                "results": {"successful_extractions": 0,
                            "failed_extractions": 3},
                "issues": [{"severity": "high", "field": "price",
                            "problem": "no price extracted",
                            "details": ".price missing",
                            "suggested_fix": "fix selector"}],
            },
            feedback_for_writer=" Tester flagged: price selector broken.",
        ))
        assert "DIFFERENTIATION REQUIRED" in out
        assert "already tested and FAILED" in out

    def test_first_attempt_still_silent(self):
        out = _summary(_state(test_retry_count=0,
                              last_tested_draft_fp=""))
        assert "DIFFERENTIATION REQUIRED" not in out
