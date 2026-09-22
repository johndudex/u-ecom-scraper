"""[wave-40 T1] Cross-node channels must be DECLARED in ScrapeState.

langgraph strips undeclared keys from node returns (verified live: a node
returning {"ghost": 1} leaves the key out of state, no error). Six shipped
channels were undeclared, so their safety nets were silently off. Pins the six
keys AND the round-trip: declared + last-write-wins (never operator.add).
Prod evidence: job 769 re-ran discovery, rc=3 DISCOVERY_ZERO, no_fresh_output
was stripped, the recycle at graph.py:5369 never fired, job FAILED n=0.

Run: docker compose exec django sh -c "cd /app/webapp && pytest ../tests/test_wave40_state_channel_contract.py -q"
"""

from __future__ import annotations

import os
import re  # noqa: F401  (kept from the brief's import surface)
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()  # agents.graph (imported in-test) needs configured settings

from agents.state import ScrapeState  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402

CHANNELS = {                       # key -> the value a writer stamps
    "tester_wall_clock_timeouts": 1,
    "fast_fail_detail": "code_tester hit its wall clock",
    "browser_unavailable_detail": "waited 120s for /health",
    "draft_absent_count": 1,
    "last_tested_draft_bytes": 1024,
    "no_fresh_output": True,
}


def _stale(value):
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 7
    return "stale " + value


def test_six_channels_are_declared():
    for key in CHANNELS:
        assert key in ScrapeState.__annotations__, f"ScrapeState lacks {key}"


def test_no_channel_is_an_accumulating_reducer():
    for key, ann in ScrapeState.__annotations__.items():
        if key in CHANNELS:
            assert "operator.add" not in repr(ann), key
            assert "add_messages" not in repr(ann), key


@pytest.mark.parametrize("key,value", sorted(CHANNELS.items()))
def test_channel_survives_a_node_return_and_overwrites(key, value):
    """A seeded prior value must be REPLACED by the writer's value. Undeclared
    -> stripped (reads None); operator.add -> summed. Both are regressions."""
    seen = {}
    g = StateGraph(ScrapeState)
    g.add_node("seed", lambda _s: {key: _stale(value)})
    g.add_node("writer", lambda _s: {key: value})
    g.add_node("reader", lambda s: seen.update(s) or {})
    g.add_edge(START, "seed")
    g.add_edge("seed", "writer")
    g.add_edge("writer", "reader")
    g.add_edge("reader", END)
    g.compile().invoke({})
    assert seen.get(key) == value, f"{key} stripped or merged instead of overwritten"


def _zero_state(**overrides):
    st = {
        "job_id": 0, "url": "https://x.example/", "site_slug": "x",
        "input_mode": "navigation", "test_retry_count": 0,
        "execution_recycle_count": 0, "execution_status": "", "product_count": 0,
        "scraper_analysis": {"strategy": "http_requests"},
    }
    st.update(overrides)
    return st


def test_769_discovery_zero_recycles_instead_of_cleanup():
    # run_execution's exact rc=3 update shape (run_execution.py:1432-1450)
    from agents.graph import _route_after_execution
    res = _route_after_execution(_zero_state(
        execution_status="FAILED", product_count=0, no_fresh_output=True,
        discovery_coverage={"ran_phase1": True, "discovered_urls": 0,
                            "stop_reason": "empty_first_page"}))
    goto = res.goto if hasattr(res, "goto") else res.get("goto", "")
    assert goto == "scraper_analyzer"        # was: cleanup (dead channel)
    assert res.update["execution_recycle_count"] == 1


def test_no_fresh_output_absent_still_cleans_up_crashes():
    from agents.graph import _route_after_execution
    res = _route_after_execution(_zero_state(
        execution_status="FAILED", product_count=0, discovery_coverage={}))
    goto = res.goto if hasattr(res, "goto") else res.get("goto", "")
    assert goto == "cleanup"


def _rat_mod():
    # agents.nodes.__init__ re-exports the route_after_testing FUNCTION over
    # the submodule — attribute access yields a function, not the module.
    import agents.nodes.route_after_testing  # noqa: F401
    return sys.modules["agents.nodes.route_after_testing"]


def test_fast_fail_double_strike_defaults_off(monkeypatch):
    rat = _rat_mod()
    monkeypatch.delenv("SCRAPER_FAST_FAIL", raising=False)
    assert rat._fast_fail_escalation_enabled() is False
    monkeypatch.setenv("SCRAPER_FAST_FAIL", "1")
    assert rat._fast_fail_escalation_enabled() is True
