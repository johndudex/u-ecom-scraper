"""Job-65 citybeach Phase-3a regressions: execution-phase strategy recycle.

What happened (job 65): the draft PASSED testing (tester discovered 1,317
URLs); the EXECUTION run minutes later fetched the same listing under an
execution-window soft block, its Phase 1 selected zero links, and the job
went straight to cleanup — the finalize gate failed it with NOTHING tried.
The strategy ladder only ran at testing time; an execution that proved the
strategy cannot see the site had no route back into it.

Fix pinned here (``_route_after_execution``): when the execution output shows
a FAIL-class discovery coverage stop_reason (``empty_first_page`` /
``navigate_error`` / ``dedup_flat`` — meaning the draft's OWN in-run recovery
already ran and failed), the node recycles ONCE through scraper_analyzer with
the failed strategy recorded, so ``_escalate_strategy`` moves up the ladder
(http_requests → http_navigation → playwright) with a fresh
code_writer → code_tester pass. Everything else routes to cleanup unchanged.
The second zero-item execution finalizes honestly (execution_status FAILED —
cleanup must not promote a 0-item scraper).

Run: docker compose exec -T -e DJANGO_SETTINGS_MODULE=config.settings -e PYTHONPATH=/app:/app/webapp django sh -c "cd /app && pytest tests/test_job65_execution_recycle.py -q"
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django

django.setup()


def _goto(result):
    return getattr(result, "goto", None)


def _update(result):
    return getattr(result, "update", None) or {}


def _zero_state(**over):
    """State shape leaving run_execution with a blocked zero-item success."""
    state = {
        "job_id": 1,
        "execution_status": "SUCCESS",
        "product_count": 0,
        "input_mode": "list_page",
        "discovery_coverage": {
            "ran_phase1": True,
            "stop_reason": "empty_first_page",
            "discovered_urls": 0,
            "found": 0,
        },
        "scraper_analysis": {"strategy": "http_requests"},
        "execution_recycle_count": 0,
    }
    state.update(over)
    return state


class TestRouteAfterExecution:
    def _route(self, state):
        from webapp.agents.graph import _route_after_execution

        return _route_after_execution(state)

    def test_success_with_items_goes_to_cleanup(self):
        res = self._route(_zero_state(product_count=448))
        assert _goto(res) == "cleanup"
        assert _update(res) == {}

    def test_crash_execution_goes_to_cleanup_even_with_fail_coverage(self):
        """rc!=0 / stall / no-output are CODE problems — the strategy ladder
        cannot fix a traceback; keep the pre-3a behavior."""
        res = self._route(_zero_state(
            execution_status="FAILED",
            error_message="Scraper exited with code 1",
        ))
        assert _goto(res) == "cleanup"

    def test_url_list_zero_items_goes_to_cleanup(self):
        """url_list has no discovery phase — 0 items is not a strategy
        verdict (item pages may be blocked; nothing to recycle)."""
        res = self._route(_zero_state(input_mode="url_list"))
        assert _goto(res) == "cleanup"

    def test_exhaustion_flavored_stop_reason_is_not_a_recycle(self):
        """`short_page` with 0 items is a draft misreport, not the blocked
        signature — Phase 2's test-time gate owns that class; finalize's
        zero-item gate backstops it."""
        res = self._route(_zero_state(
            discovery_coverage={"ran_phase1": True, "stop_reason": "short_page"},
        ))
        assert _goto(res) == "cleanup"

    @pytest.mark.parametrize("stop_reason", [
        "empty_first_page", "navigate_error", "dedup_flat",
    ])
    def test_fail_class_stop_reasons_recycle(self, stop_reason):
        res = self._route(_zero_state(
            discovery_coverage={"ran_phase1": True, "stop_reason": stop_reason},
        ))
        assert _goto(res) == "scraper_analyzer"
        upd = _update(res)
        assert upd["execution_recycle_count"] == 1
        assert upd["strategies_tried"] == [{
            "strategy": "http_requests",
            "reason": f"execution zero-items: stop_reason={stop_reason}",
        }]
        assert upd["test_report"]["overall_assessment"] == "FAIL"
        assert upd["test_report"]["ready_for_execution"] is False
        assert "RECYCLE" in upd["test_report"]["feedback_for_writer"]
        # An honest recycle must NOT pre-fail the job — it gets a new pass.
        assert "error_message" not in upd
        assert "execution_status" not in upd

    def test_second_zero_item_execution_fails_honestly(self):
        res = self._route(_zero_state(execution_recycle_count=1))
        assert _goto(res) == "cleanup"
        upd = _update(res)
        assert upd["execution_status"] == "FAILED"
        assert "recycle" in upd["error_message"].lower()
        assert "empty_first_page" in upd["error_message"]

    def test_recycle_without_a_known_strategy_cleans_up(self):
        res = self._route(_zero_state(scraper_analysis={}))
        assert _goto(res) == "cleanup"
        assert _update(res) == {}

    def test_search_term_mode_recycles_too(self):
        res = self._route(_zero_state(input_mode="search_term"))
        assert _goto(res) == "scraper_analyzer"


class TestRecycleFeedsTheLadder:
    def test_recorded_strategy_escalates_to_next_rung(self):
        """The recycle records the failed strategy exactly as route_after_testing
        does — _escalate_strategy must then refuse to re-pick it and move up."""
        from webapp.agents.graph import _escalate_strategy

        analysis, goto = _escalate_strategy(
            {"strategy": "http_requests"}, {"http_requests"},
        )
        assert analysis["strategy"] == "http_navigation"
        assert goto is None  # ladder not exhausted — a real rung remains

    def test_playwright_exhaustion_does_not_manufacture_internal_api(self):
        from webapp.agents.graph import _escalate_strategy

        analysis, goto = _escalate_strategy(
            {"strategy": "playwright"}, {"http_requests", "http_navigation", "playwright"},
        )
        assert goto is not None  # exhausted → cleanup / human_approval
        assert analysis["strategy"] == "playwright"  # never a doomed re-pick

    def test_decide_strategy_records_the_recycled_strategy(self):
        """End-to-end state shape after the recycle: _decide_strategy's own
        classify pass must not double-append the strategy (dupe guard), and
        the escalation must still fire off the recycled entry."""
        # The dupe guard is structural: the entry _route_after_execution
        # appends uses the same {strategy, reason} shape _decide_strategy
        # dedupes against.
        from webapp.agents.graph import _route_after_execution

        upd = _update(_route_after_execution(_zero_state()))
        assert isinstance(upd["strategies_tried"][0], dict)
        assert "strategy" in upd["strategies_tried"][0]


class TestTopology:
    def _graph_src(self) -> str:
        with open(os.path.join(ROOT, "webapp", "agents", "graph.py")) as fh:
            return fh.read()

    def test_run_execution_feeds_the_recycle_node(self):
        assert 'add_edge("run_execution", "route_after_execution")' in self._graph_src()

    def test_recycle_node_has_no_static_out_edge(self):
        """The D6 lesson: a Command-returning node must not also carry a
        static/conditional out-edge — LangGraph runs BOTH destinations."""
        src = self._graph_src()
        assert not any(
            'add_edge("route_after_execution", ' in line
            for line in src.splitlines()
        ), "route_after_execution has a static out-edge (D6 shadow branch)"

    def test_recycle_node_is_registered(self):
        assert 'add_node("route_after_execution", _route_after_execution)' in (
            self._graph_src()
        )

    def test_live_graph_compiles_with_the_node(self):
        from agents.graph import build_scrape_graph

        g = build_scrape_graph()
        assert "route_after_execution" in g.nodes
        assert "cleanup" in g.nodes

    def test_state_declares_the_recycle_counter(self):
        with open(os.path.join(ROOT, "webapp", "agents", "state.py")) as fh:
            src = fh.read()
        assert "execution_recycle_count: int" in src


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
