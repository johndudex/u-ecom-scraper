"""[wave-22 A2] No-verdict tester attempts must count against the retry
budget — the 3-hour silent death.

Prod jobs 371/372 (and 365/370) each logged ``[CASCADE] retry-no-report
retry=0/2`` six or seven times before dying at exactly 3h of
``SoftTimeLimitExceeded``. The loop: when a tester attempt produces NO
verdict, ``route_after_testing`` re-runs the writer→tester cycle while
``test_retry_count < MAX_TEST_RETRIES`` — but the ONLY increment of that
counter sits behind ``if state.get("test_report"):`` in the writer node, so
no-verdict cycles never advance it. The loop is unbounded.

Contract:
- ``_invoke_code_tester``'s no-verdict block increments
  ``test_retry_count`` (the same budget every other arm uses), guarded
  against ``FINAL_RETRY_SENTINEL`` (a human's final-retry decision must not
  be corrupted — same guard the writer increment uses);
- the writer increment stays gated on a truthy report (no double counting:
  exactly one of the two fires per cycle);
- GIVEN that increment, the router's no-report ladder is bounded: with
  ``test_report=None``, retry counts 0 and 1 route to ``scraper_analyzer``,
  count 2 (≥MAX_TEST_RETRIES) must NOT — it escalates (cleanup under
  skip_approvals, human_approval otherwise).
"""
from __future__ import annotations

import importlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

graph = importlib.import_module("agents.graph")  # noqa: E402
rat = importlib.import_module("agents.nodes.route_after_testing")  # noqa: E402


class TestTesterCountsNoVerdictAttempts:
    def _tester_src(self) -> str:
        with open(os.path.join(ROOT, "webapp", "agents", "graph.py")) as fh:
            return fh.read()

    def _no_verdict_block(self) -> str:
        src = self._tester_src()
        i = src.index("if not report:")
        j = src.index("# [B2.6/wave-13] Parity:", i)
        return src[i:j]

    def test_no_verdict_block_increments_test_retry_count(self):
        block = self._no_verdict_block()
        assert "test_retry_count" in block, (
            "the tester's no-verdict block never touches test_retry_count — "
            "no-report cycles cannot advance the retry budget and the "
            "writer→tester loop is unbounded (371/372 died at 3h with "
            "retry=0/2 logged 7 times)"
        )

    def test_increment_guards_the_final_retry_sentinel(self):
        block = self._no_verdict_block()
        assert "FINAL_RETRY_SENTINEL" in block, (
            "the increment must not run past FINAL_RETRY_SENTINEL — a "
            "human-granted final retry must keep its meaning"
        )

    def test_writer_increment_stays_report_gated(self):
        src = self._tester_src()
        i_writer = src.index("def _invoke_code_writer")
        i_tester = src.index("def _invoke_code_tester")
        writer_src = src[i_writer:i_tester]
        i_gate = writer_src.index("if state.get(\"test_report\"):")
        i_count = writer_src.index('update["test_retry_count"]', i_gate)
        assert i_count > i_gate, "writer counter moved out from behind the report gate"


class TestRouterNoReportLadderBounded:
    """GIVEN the tester now advances the counter, the ladder must terminate."""

    def _route(self, retry: int, skip_approvals: bool) -> str:
        state = {
            "job_id": 0,
            "site_slug": "example-com",
            "test_report": None,
            "test_retry_count": retry,
            "skip_approvals": skip_approvals,
            "tester_wall_clock_timeouts": 0,
        }
        return rat.route_after_testing(state)

    @pytest.mark.parametrize("retry", [0, 1])
    def test_early_no_report_retries(self, retry):
        assert self._route(retry, skip_approvals=True) == "scraper_analyzer"

    def test_budget_exhausted_escalates_not_loops(self):
        nxt = self._route(rat.MAX_TEST_RETRIES, skip_approvals=True)
        assert nxt != "scraper_analyzer", (
            "the no-report ladder re-entered the writer→tester cycle at the "
            "retry cap — the 371/372 unbounded loop"
        )
        assert nxt == "cleanup"

    def test_sentinel_routes_to_human_not_writer(self):
        nxt = self._route(graph.FINAL_RETRY_SENTINEL, skip_approvals=False)
        assert nxt not in ("scraper_analyzer", "code_writer")

    def test_exhausted_escalates_without_skip_approvals_too(self):
        # the no-report exhausted arm's escalation is CLEANUP in both modes
        # (unlike the report-carrying ladder's human_approval) — the point is
        # that it is NOT scraper_analyzer.
        assert self._route(rat.MAX_TEST_RETRIES, skip_approvals=False) == "cleanup"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
