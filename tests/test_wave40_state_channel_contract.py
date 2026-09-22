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


# ───────────── [wave-40 T1 r1] review round: three follow-ups ─────────────


def _tester_src() -> str:
    """The ``_invoke_code_tester`` source block (same idiom as
    tests/test_wave22_fast_fail.py::TestNodeWiring)."""
    with open(os.path.join(ROOT, "webapp", "agents", "graph.py")) as fh:
        src = fh.read()
    i = src.index("def _invoke_code_tester")
    j = src.index("\ndef ", i + 10)
    return src[i:j]


def test_report_branch_clears_fast_fail_detail():
    """[T1 r1 Finding 2] The tester's report branch resets the wall-clock
    counter but never cleared the detail. Declared, a detail stamped in cycle N
    (while SCRAPER_FAST_FAIL is on) would survive into a LATER cycle that died
    of a different cause and misfire the A5 arm. The reset must carry both."""
    block = _tester_src()
    i = block.index('update["tester_wall_clock_timeouts"] = 0')
    window = block[max(0, i - 400):i + 400]
    assert 'update["fast_fail_detail"] = ""' in window, (
        "the report branch resets tester_wall_clock_timeouts but leaves "
        "fast_fail_detail standing — once SCRAPER_FAST_FAIL is ever enabled a "
        "stale detail fires the A5 arm on a later cycle that died of a "
        "different cause"
    )


def test_report_branch_still_resets_the_counter():
    """[T1 r1 Finding 2 companion pin] the healthy-report reset that Finding 2
    extends must stay in place."""
    assert 'update["tester_wall_clock_timeouts"] = 0' in _tester_src()


def test_stale_detail_with_a_real_report_does_not_park(monkeypatch):
    """[T1 r1 Finding 3] Guard 1's positive half: the park arm must trust the
    detail ONLY when there is no report to judge. A stale
    ``browser_unavailable_detail`` alongside a REAL verdict must fall through
    to the normal ladder — parking here would loop the park forever (the
    reason the guard exists). Regression pin, not RED-first: the negative half
    (detail + no report → park) is already pinned at
    tests/test_wave16_browser_park.py::test_preflight_flag_parks_above_no_report_arms."""
    rat = _rat_mod()
    monkeypatch.setattr(rat, "_log_cascade", lambda *a, **k: None, raising=True)
    state = {
        "job_id": 0,
        "site_slug": "example-com",
        "url": "https://example.com/",
        "input_mode": "url_list",
        "test_retry_count": 0,
        "skip_approvals": True,
        # the stale detail from an earlier attempt ...
        "browser_unavailable_detail": "waited 120s for /health",
        # ... and a REAL verdict: 5 extracted items, a live defect, no infra
        "test_report": {
            "overall_assessment": "NEEDS_FIXES",
            "confidence_score": 0.9,
            "results": {"successful_extractions": 5},
            "issues": [{
                "severity": "medium", "field": "price",
                "issue_type": "EXTRACTION_FAILED",
                "message": "price selector returned None on 2 of 5 items",
            }],
            "discovery_coverage": {
                "ran_phase1": True, "discovered_urls": 12, "stop_reason": "ok",
            },
        },
        "scraper_analysis": {"strategy": "http_requests"},
    }
    goto = rat.route_after_testing(state)
    assert goto != "park_browser_unavailable", (
        "a real verdict must be judged — parking on a stale detail loops the "
        "park arm forever"
    )
    # and the ladder actually JUDGED it: a NEEDS_FIXES verdict with a medium
    # issue and retry budget left goes to the writer's fix arm
    assert goto == "code_writer"


def test_adopted_inprocess_retry_return_is_normalized(monkeypatch, tmp_path):
    """[T1 r1 Finding 1] The in-process leg's listing fallback can ADOPT the
    retry's raw ``_run_in_process`` dict — a delivering run (product_count>0)
    stamps neither channel, and an absent key is a no-op in a LastValue
    channel, so the PRIMARY attempt's ``no_fresh_output=True`` /
    ``browser_unavailable_detail`` would ride into the router. The redispatch
    closure must normalize what it adopts (the wave-33 source pin on
    ``return _maybe_retry_execution_listing(`` stays intact)."""
    import agents.nodes.run_execution  # noqa: F401  (registers the submodule)

    # agents.nodes.__init__ re-exports the run_execution FUNCTION over the
    # submodule — take the real module out of sys.modules (same idiom as
    # _rat_mod above), so the patches land on the instance the code uses.
    re_mod = sys.modules["agents.nodes.run_execution"]

    monkeypatch.setattr(re_mod, "_get_project_root", lambda: str(tmp_path))
    ws = tmp_path / "workspace" / "x"
    ws.mkdir(parents=True)
    (ws / "scraper_draft.py").write_text("print('hi')\n", encoding="utf-8")
    # keep the entry gates out of the way (this test is about the leg's exit)
    monkeypatch.setattr(
        "agents.draft_safety.ladder_preservation_violation",
        lambda *a, **k: None, raising=True,
    )
    monkeypatch.setattr(
        re_mod, "_distinct_same_domain_listing",
        lambda state, primary: "https://example.com/collections/all",
        raising=True,
    )

    calls = {"n": 0}

    def fake_run_in_process(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # PRIMARY: the exact clean-zero shape the in-process leg stamps
            # (run_execution's rc=3 DISCOVERY_ZERO / no-output-file exit)
            return {"execution_status": "COMPLETED", "product_count": 0,
                    "no_fresh_output": True}
        # RETRY, adopted: a DELIVERING raw runner dict — note the ABSENT
        # channel keys (a delivering run never stamps no_fresh_output).
        return {"execution_status": "COMPLETED", "product_count": 5}

    monkeypatch.setattr(re_mod, "_run_in_process", fake_run_in_process,
                        raising=True)

    from django.conf import settings

    monkeypatch.setattr(settings, "SCRAPER_EXECUTION_MODE", "force_http",
                        raising=False)

    result = re_mod.run_execution(_zero_state(input_mode="url_list"))

    assert calls["n"] == 2, "exactly one bounded listing fallback"
    assert result.get("product_count") == 5, "the delivering retry is adopted"
    assert result.get("no_fresh_output") is False, (
        "an adopted retry return must stamp an EXPLICIT no_fresh_output=False "
        "— an absent key lets the primary's True ride into the recycle router"
    )
    assert result.get("browser_unavailable_detail") == "", (
        "an adopted retry return must stamp an EXPLICIT empty detail — an "
        "absent key lets a stale detail ride into the park arm"
    )
