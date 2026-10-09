"""[wave-56 F2] No-op strike 1 spends a strategy-ladder rung.

The A2 convergence gate (graph.py:8279) detects a byte-identical draft
honestly — and then, on strike 1 with budget remaining, routes the IDENTICAL
bytes back into code_tester (a third test of code that cannot have changed).
Strike 2 escalates to cleanup. Five of the batch's seven writer/tester deaths
(570-576) ended exactly there, while the machinery that could have produced a
DIFFERENT experiment — the deterministic strategy ladder — sat one bounce
away, fully built: ``_escalate_strategy`` walks
``http_requests → http_navigation → playwright`` (never INTO internal_api),
``strategies_tried`` is an append-only never-repeat channel, and mid-job
strategy switching is mechanically safe (the writer rebuilds its brief from
state; run_execution re-reads the strategy at execution time).

The deadlock's other half: ``_decide_strategy`` only records a failed
strategy into ``strategies_tried`` when ``classify_test_failure`` says
"strategy" — and the tester's own ``remediation.target=scraper`` diagnosis
outranks the blocked-signature arms, so a transport wall classified as a code
bug NEVER enters history and the ladder can never escalate away from it.

Fix: on no-op strike 1 (budget remaining, no step death), append the current
strategy to ``strategies_tried`` with an honest reason and bounce to
``scraper_analyzer`` — the same Command shape the dead-writer path and the
execution-zero recycle already use. The ladder then spends the next rung, or
— when exhausted — routes out through the existing honest-exhaustion arms.
Strike 2 semantics are unchanged.
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


def _state(strategy="http_requests", tried=None, **over):
    state = {
        "site_slug": "test-site",
        "job_id": 1,
        "scraper_analysis": {"strategy": strategy},
        "strategies_tried": list(tried or []),
    }
    state.update(over)
    return state


class TestStrikeOneLadderBounce:
    def test_first_rung_available_bounces(self):
        from agents.graph import _noop_strike1_ladder_bounce

        out = _noop_strike1_ladder_bounce(_state("http_requests"), {"noop_fix_cycles": 1})
        assert out is not None
        (entry,) = out["strategies_tried"]
        assert entry["strategy"] == "http_requests"
        assert "no-op" in entry["reason"]

    def test_second_rung_from_http_navigation_bounces(self):
        from agents.graph import _noop_strike1_ladder_bounce

        out = _noop_strike1_ladder_bounce(
            _state("http_navigation", tried=[{"strategy": "http_requests"}]),
            {"noop_fix_cycles": 1},
        )
        assert out is not None
        (entry,) = out["strategies_tried"]
        assert entry["strategy"] == "http_navigation"

    def test_strike_two_never_bounces(self):
        from agents.graph import _noop_strike1_ladder_bounce

        assert (
            _noop_strike1_ladder_bounce(
                _state("http_requests"), {"noop_fix_cycles": 2}
            )
            is None
        )

    def test_ladder_exhausted_never_bounces(self):
        from agents.graph import _noop_strike1_ladder_bounce

        assert (
            _noop_strike1_ladder_bounce(
                _state(
                    "http_requests",
                    tried=[
                        {"strategy": "http_navigation"},
                        {"strategy": "playwright"},
                    ],
                ),
                {"noop_fix_cycles": 1},
            )
            is None
        )

    def test_playwright_top_rung_cannot_escalate_into_internal_api(self):
        from agents.graph import _noop_strike1_ladder_bounce

        # job-12 honesty rule: never escalate INTO internal_api — from the
        # top practical rung there is no untried next rung, so no bounce.
        assert (
            _noop_strike1_ladder_bounce(
                _state("playwright"), {"noop_fix_cycles": 1}
            )
            is None
        )

    def test_internal_api_current_never_bounces(self):
        from agents.graph import _noop_strike1_ladder_bounce

        assert (
            _noop_strike1_ladder_bounce(
                _state("internal_api"), {"noop_fix_cycles": 1}
            )
            is None
        )

    def test_unknown_strategy_token_never_bounces(self):
        # Suffixed/tier tokens ("http_navigation+residential_proxy") must not
        # be "escalated" — _escalate_strategy would read idx=-1 and DOWN-grade
        # to http_requests, manufacturing a doomed rung.
        from agents.graph import _noop_strike1_ladder_bounce

        assert (
            _noop_strike1_ladder_bounce(
                _state("http_navigation+residential_proxy"),
                {"noop_fix_cycles": 1},
            )
            is None
        )

    def test_no_strategy_on_state_never_bounces(self):
        from agents.graph import _noop_strike1_ladder_bounce

        assert (
            _noop_strike1_ladder_bounce(
                _state(""), {"noop_fix_cycles": 1}
            )
            is None
        )

    def test_bounce_does_not_mutate_caller_state(self):
        from agents.graph import _noop_strike1_ladder_bounce

        state = _state("http_requests")
        _noop_strike1_ladder_bounce(state, {"noop_fix_cycles": 1})
        assert state.get("strategies_tried") == []
