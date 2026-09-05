"""[wave-22 A5] A phase that died on its wall clock with no artifact must
fast-fail the job in minutes — with a NAMED error — not limp through retry
cycles that cannot possibly succeed.

Prod shape (365/370/371/372 family): the tester (or product_analyzer) hits its
invoke wall clock, the thread is abandoned, and the pipeline treats the death
like any other missing-artifact cycle: auto-extend re-invoke → budget
interrupt → missing-artifact redo interrupt → retry ladder — each a full
window against the same wall — until SoftTimeLimitExceeded kills the task with
a billiard traceback instead of a name.

Contract (trimmed per critique: fires ONLY for code_tester and
product_analyzer — code_writer's timeout arm deliberately keeps a usable
draft, navigation has no single artifact to miss):
- pure helper ``_fast_fail_detail``: names phase + missing artifact + last
  tool; empty string when the artifact exists, the death wasn't a wall-clock
  death, or the result shape is unrecognized;
- product_analyzer: the arm sits where ``output_exists`` is a FRESH FS check,
  BEFORE the auto-extend re-invoke (a dead thread gets no second window), and
  Command-routes to cleanup with the named error (established Command pattern
  of the budget interrupts);
- code_tester: the node cannot Command-route (D6 union trap), so it stamps
  ``fast_fail_detail`` (+ ``error_message``) and the router acts ABOVE its
  no-report ladder — cleanup in BOTH skip_approvals modes (recoverable via
  re-drive, not a new terminal class).
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


class ToolMessage:  # class NAME is the contract the helper checks
    def __init__(self, name=""):
        self.name = name


class AIMessage:
    pass


def _dead_result(last_tool="browser_run_code", err_class="WallClockTimeout"):
    out = {
        "messages": [
            AIMessage(),
            ToolMessage(name=last_tool),
        ],
        "_error": "wall-clock timeout after 900s",
        "_error_class": err_class,
    }
    return out


class TestFastFailHelper:
    def test_names_phase_artifact_and_last_tool(self):
        detail = graph._fast_fail_detail(
            "code_tester", "test_report.json", _dead_result(), output_exists=False
        )
        assert "code_tester" in detail
        assert "test_report.json" in detail
        assert "browser_run_code" in detail

    def test_silent_when_artifact_exists(self):
        assert (
            graph._fast_fail_detail(
                "code_tester", "test_report.json", _dead_result(), output_exists=True
            )
            == ""
        )

    def test_silent_when_not_a_wall_clock_death(self):
        assert (
            graph._fast_fail_detail(
                "code_tester",
                "test_report.json",
                _dead_result(err_class="ProviderError"),
                output_exists=False,
            )
            == ""
        )
        no_err = {"messages": _dead_result()["messages"]}
        assert (
            graph._fast_fail_detail(
                "code_tester", "test_report.json", no_err, output_exists=False
            )
            == ""
        )

    def test_silent_on_unrecognized_result_shape(self):
        assert (
            graph._fast_fail_detail(
                "code_tester", "test_report.json", None, output_exists=False
            )
            == ""
        )

    def test_says_none_when_no_tool_was_called(self):
        res = _dead_result()
        res["messages"] = [AIMessage()]
        detail = graph._fast_fail_detail(
            "product_analyzer", "content_analysis.json", res, output_exists=False
        )
        assert "none" in detail


class TestRouterFastFailArm:
    """The router must act on the stamped flag ABOVE the no-report ladder."""

    def _route(self, extra: dict, skip_approvals: bool) -> str:
        state = {
            "job_id": 0,
            "site_slug": "example-com",
            "test_report": None,
            "test_retry_count": 0,
            "skip_approvals": skip_approvals,
            "tester_wall_clock_timeouts": 0,
        }
        state.update(extra)
        return rat.route_after_testing(state)

    def test_fast_fail_beats_the_no_report_ladder(self):
        nxt = self._route(
            {"fast_fail_detail": "code_tester hit its wall clock and wrote no "
             "test_report.json"}, skip_approvals=True
        )
        assert nxt == "cleanup", (
            f"fast-fail detail set but router returned {nxt!r} — the job "
            f"limped into the no-report retry ladder instead of dying fast"
        )

    def test_fast_fail_routes_cleanup_without_skip_approvals_too(self):
        assert (
            self._route(
                {"fast_fail_detail": "x"}, skip_approvals=False
            )
            == "cleanup"
        )

    def test_fast_fail_beats_the_final_attempt_ladder(self):
        nxt = self._route(
            {
                "fast_fail_detail": "x",
                "test_retry_count": rat.FINAL_RETRY_SENTINEL,
            },
            skip_approvals=True,
        )
        assert nxt == "cleanup"

    def test_absent_flag_never_triggers_the_arm(self):
        state = {
            "job_id": 0,
            "site_slug": "example-com",
            "test_report": None,
            "test_retry_count": 0,
            "skip_approvals": True,
            "tester_wall_clock_timeouts": 0,
        }
        assert rat.route_after_testing(state) == "scraper_analyzer"


class TestNodeWiring:
    def _graph_src(self) -> str:
        with open(os.path.join(ROOT, "webapp", "agents", "graph.py")) as fh:
            return fh.read()

    def _tester_src(self) -> str:
        src = self._graph_src()
        i = src.index("def _invoke_code_tester")
        j = src.index("\ndef ", i + 10)
        return src[i:j]

    def _budgeted_src(self) -> str:
        src = self._graph_src()
        i = src.index("def _run_budgeted_agent")
        j = src.index("\ndef ", i + 10)
        return src[i:j]

    def test_tester_stamps_fast_fail_from_wall_clock_death(self):
        block = self._tester_src()
        i = block.index("if not report:")
        j = block.index("# [B2.6/wave-13] Parity:", i)
        no_report = block[i:j]
        assert "fast_fail_detail" in no_report, (
            "the tester's no-verdict block never stamps fast_fail_detail — a "
            "wall-clock death with no report on disk limps into the retry "
            "ladder"
        )
        assert "_fast_fail_detail(" in no_report
        assert "os.path.isfile" in no_report, (
            "the fast-fail decision must re-check the FS at decision time, "
            "not trust an earlier observation"
        )

    def test_tester_also_sets_error_message(self):
        block = self._tester_src()
        i = block.index("if not report:")
        j = block.index("# [B2.6/wave-13] Parity:", i)
        assert "error_message" in block[i:j], (
            "the named error must ride the state so the terminal job row "
            "carries the reason, not a blank"
        )

    def test_budgeted_agent_fast_fails_product_analyzer_before_autoextend(self):
        src = self._budgeted_src()
        assert "A5_FAST_FAIL_PHASES" in src, (
            "_run_budgeted_agent has no fast-fail arm"
        )
        assert "product_analyzer" in graph.A5_FAST_FAIL_PHASES
        assert "site_analyzer" not in graph.A5_FAST_FAIL_PHASES, (
            "site_analyzer must not fast-fail — the trimmed contract is "
            "code_tester/product_analyzer only"
        )
        assert "navigation_agent" not in graph.A5_FAST_FAIL_PHASES
        # the arm must sit BEFORE the auto-extend re-invoke (a dead thread
        # gets no second window)
        i_ff = src.index("A5_FAST_FAIL_PHASES", src.index("output_exists = os.path.isfile"))
        i_auto = src.index("auto-extending budget")
        assert i_ff < i_auto, (
            "the fast-fail arm fires after the auto-extend re-invoke — the "
            "dead thread got a second full window (the 2.4h limp)"
        )
        i_cmd = src.index('goto="cleanup"', i_ff)
        between = src[i_ff:i_cmd]
        assert "error_message" in between, "the cleanup route must carry the named error"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
