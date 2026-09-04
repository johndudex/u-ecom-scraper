"""[wave-19 T1.7] skip_approvals conservative defaults: testing-exhausted must
NOT auto-approve execution of a failed draft. (323/D4)

The leak: a testing-exhausted arm returned ``human_approval`` with
``interrupt_reason="testing_exhausted"`` → the node's skip_approvals
auto-approve answered ``{"decision": "approve", "label": "Continue"}`` →
``route_from_human_approval`` read that as "Continue anyway" →
``field_confirmation`` → run_execution — executing the draft the tester had
just failed three cycles in a row. The jobs-79/80 patch covered the
writer-failed arm only; this closes the same class at the router, the single
choke point every exhausted arm drains through.

Contract:
- skip_approvals + testing_exhausted → ``cleanup`` (honest failure). No human
  exists, so "Continue anyway" was never actually chosen by anyone;
- a REAL user (no skip_approvals) still gets the field_confirmation /
  final-retry-with-feedback options — the fix must not touch attended jobs.
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

from webapp.agents.graph import route_from_human_approval  # noqa: E402


def _state(**extra):
    return {
        "interrupt_reason": "testing_exhausted",
        "human_response": {"decision": "approve", "label": "Continue"},
        "human_feedback": "",
        **extra,
    }


class TestSkipApprovalsTestingExhausted:
    def test_auto_approved_exhausted_routes_cleanup(self):
        """THE 323/D4 shape: node auto-approve + skip_approvals → cleanup."""
        assert route_from_human_approval(_state(skip_approvals=True)) == "cleanup"

    def test_attended_continue_still_field_confirmation(self):
        """A real user choosing Continue still ships (unchanged contract)."""
        assert route_from_human_approval(_state()) == "field_confirmation"

    def test_attended_final_retry_feedback_still_scraper_analyzer(self):
        st = _state(
            human_response={
                "decision": "approve",
                "label": "Provide feedback for final retry",
            },
            human_feedback="prices are mapped from the wrong node",
        )
        assert route_from_human_approval(st) == "scraper_analyzer"

    def test_skip_approvals_low_coverage_still_retries_analysis(self):
        """Conservative default is scoped to testing_exhausted: earlier
        build-phase gates (low_coverage) keep their auto-proceed — they lead
        to MORE building, not to executing a failed draft."""
        st = {
            "interrupt_reason": "low_coverage",
            "human_response": {"decision": "approve", "label": "Continue"},
            "skip_approvals": True,
        }
        assert route_from_human_approval(st) == "scraper_analyzer"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
