"""Job-81 (revolveclothing) — the tester wall-clock contract.

What happened (2026-08-31): job 81's tester invocation mandated TWO blocking
browser runs (phase-1 discovery + phase-2 sample), each floored at 600s by
run_scraper, inside the flat 900s AGENT_INVOKE_TIMEOUT. Two ~510s runs left
370s of window mid-cascade — abandonment was STRUCTURAL, not a slow-site
anomaly. Worse: the abandoned invocation adopted the PREVIOUS cycle's CRASH
report (written 70 min earlier, about an earlier draft) and routed "cascade
exhausted" — while cycle-3's fresh, syntax-valid draft sat unjudged. The job
failed with 0 products though its scraper probably worked.

Three mechanisms pinned here (no caps — windows widened/honest, escalation
mirrors the writer's wall-clock arm):

  N-A  the code_tester window is DERIVED from the tool budget its own prompt
       mandates: 2 x (BROWSER_RUN_TIMEOUT_FLOOR + httpx margin) + LLM margin —
       not the flat 900s.
  N-B  run_scraper refuses to launch a blocking browser run that cannot
       finish inside the invoking agent's remaining wall clock (deadline
       stamped by _invoke_agent_with_timeout) — the run's result would be
       lost with the abandoned thread anyway; an explicit SKIPPED marker lets
       the tester write a truthful verdict instead.
  N-C  a dead/no-op invocation never routes on a stale verdict: the report
       must have been written DURING the attempt (mtime floor), a previous
       cycle's report must not ride along in LangGraph state, and two
       consecutive wall-clock deaths escalate (human_approval, or honest
       cleanup under skip_approvals) instead of regenerating an unjudged
       draft.

Run: docker compose exec -T -w /app/webapp django python -m pytest ../tests/test_job81_tester_wall_clock_contract.py -q
"""
from __future__ import annotations

import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

from django.conf import settings as dj_settings  # noqa: E402
from langgraph.types import RunnableConfig  # noqa: E402

import agents.graph as g  # noqa: E402
from agents.tools import shell_tools  # noqa: E402
from agents.tools.context import (  # noqa: E402
    clear_tool_context,
    set_tool_deadline,
)

SLUG = "t"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(dj_settings, "PROJECT_ROOT", str(tmp_path), raising=True)
    clear_tool_context()
    yield
    clear_tool_context()


def _write_report(tmp_path, assessment="CRASH", age=0.0):
    import json

    d = tmp_path / "workspace" / SLUG
    d.mkdir(parents=True, exist_ok=True)
    p = d / "test_report.json"
    p.write_text(json.dumps({
        "overall_assessment": assessment,
        "confidence_score": 0.2,
        "ready_for_execution": False,
        "issues": [],
    }))
    if age:
        past = time.time() - age
        os.utime(p, (past, past))
    return p


# ─── N-A: the tester's window covers its own mandated work ──────────────────


class TestTesterWindow:
    def test_window_covers_two_floored_browser_runs(self):
        from agents.tools.shell_tools import BROWSER_RUN_TIMEOUT_FLOOR

        assert g._tester_invoke_timeout() >= 2 * (BROWSER_RUN_TIMEOUT_FLOOR + 60), (
            "the tester's wall clock must cover TWO floored browser runs + "
            "httpx margin — job 81's flat 900s could not contain the two "
            "~510s runs its own prompt mandates, so abandonment was structural"
        )

    def test_derived_floor_uses_the_shared_constant(self):
        """The derivation must read shell_tools' floor, not a second hand-tuned
        number — one vocabulary for 'how long may a browser run take'."""
        import inspect

        src = inspect.getsource(g._tester_invoke_timeout)
        assert "BROWSER_RUN_TIMEOUT_FLOOR" in src

    def test_env_override_can_raise_but_not_defeat_the_derivation(self, monkeypatch):
        monkeypatch.setattr(g, "_AGENT_INVOKE_TIMEOUT", 60)
        assert g._tester_invoke_timeout() > 2 * 600, (
            "a small AGENT_INVOKE_TIMEOUT must not shrink the tester below "
            "its mandated tool budget"
        )
        monkeypatch.setattr(g, "_AGENT_INVOKE_TIMEOUT", 99999)
        assert g._tester_invoke_timeout() == 99999

    def test_tester_call_site_uses_the_derived_window(self):
        import inspect

        src = inspect.getsource(g._invoke_code_tester)
        assert "timeout=_tester_invoke_timeout()" in src


# ─── N-B: the deadline is published and honored ──────────────────────────────


class TestDeadlinePublication:
    def test_invoke_stamps_tool_deadline(self, monkeypatch):
        class _Fast:
            def invoke(self, *a, **k):
                return {"messages": ["done"]}

        monkeypatch.setattr(g, "_async_execution_enabled", lambda: False)
        try:
            g._invoke_agent_with_timeout(_Fast(), [], {}, "code_tester", 0, timeout=5)
            assert g.get_tool_deadline() is not None, (
                "_invoke_agent_with_timeout must publish the deadline so "
                "blocking tools can self-check"
            )
        finally:
            clear_tool_context()

    def test_clear_tool_context_clears_deadline(self):
        set_tool_deadline(time.time() + 100)
        assert g.get_tool_deadline() is not None
        clear_tool_context()
        assert g.get_tool_deadline() is None


class TestRunScraperHonesty:
    @staticmethod
    def _tool(monkeypatch, needs_browser=True):
        tools = {t.name: t for t in shell_tools.get_shell_tools()}
        monkeypatch.setattr(shell_tools, "_scraper_needs_browser",
                            lambda p: needs_browser)
        return tools["run_scraper"].func

    def test_skips_run_that_cannot_finish(self, monkeypatch):
        # 100s left < floored 600s run + margin → refuse honestly.
        run_scraper = self._tool(monkeypatch)
        set_tool_deadline(time.time() + 100)
        result = run_scraper(
            scraper_path="workspace/t/scraper_draft.py", cli_args="", timeout=300,
        )
        assert result.startswith("SKIPPED:"), result
        assert "wall clock" in result
        assert "NOT tested" in result

    def test_run_with_room_proceeds(self, monkeypatch):
        # Plenty of window: the guard must stay dormant (the dispatch itself
        # then fails on the missing source file — no network touched).
        run_scraper = self._tool(monkeypatch)
        set_tool_deadline(time.time() + 3000)
        result = run_scraper(
            scraper_path="workspace/t/scraper_draft.py", cli_args="", timeout=300,
        )
        assert not result.startswith("SKIPPED:"), result

    def test_http_scrapers_are_never_blocked(self, monkeypatch):
        """The guard is browser-only — HTTP runs are cheap and never abandoned
        against a browser wall."""
        run_scraper = self._tool(monkeypatch, needs_browser=False)
        set_tool_deadline(time.time() + 1)
        # HTTP dispatch runs the draft locally; with the file missing the
        # subprocess fails fast — the assertion is that no SKIPPED marker
        # was produced by the deadline guard.
        result = run_scraper(
            scraper_path="workspace/t/missing.py", cli_args="", timeout=60,
        )
        assert not str(result).startswith("SKIPPED:")

    def test_no_deadline_guard_stays_dormant(self, monkeypatch):
        """No deadline published (e.g. run_execution's direct path) → tools
        never refuse work for lack of information."""
        run_scraper = self._tool(monkeypatch)
        set_tool_deadline(None)
        result = run_scraper(
            scraper_path="workspace/t/scraper_draft.py", cli_args="", timeout=300,
        )
        assert not result.startswith("SKIPPED:"), result

    def test_guard_sits_before_the_dispatch(self):
        src = open(shell_tools.__file__, encoding="utf-8").read()
        i_guard = src.find("get_tool_deadline()")
        i_dispatch = src.find("if needs_browser:\n            logger.info")
        assert 0 < i_guard < i_dispatch, (
            "the honesty guard must run before the blocking dispatch — after "
            "it, the launch already happened"
        )


# ─── N-C: no routing on stale verdicts ───────────────────────────────────────


class TestStaleReportRejection:
    def test_load_rejects_report_predating_the_attempt(self, tmp_path):
        _write_report(tmp_path, age=3600)
        assert g._load_test_report(SLUG, min_mtime=time.time()) is None, (
            "a report written an hour before this attempt is the PREVIOUS "
            "cycle's verdict — job 81 routed 'cascade exhausted' on exactly "
            "such a file"
        )

    def test_load_accepts_report_written_during_the_attempt(self, tmp_path):
        _write_report(tmp_path, age=0)
        report = g._load_test_report(SLUG, min_mtime=time.time() - 60)
        assert report and report["overall_assessment"] == "CRASH"

    def test_no_floor_is_backwards_compatible(self, tmp_path):
        _write_report(tmp_path, age=86400)
        assert g._load_test_report(SLUG) is not None


class TestDeadTesterInvocation:
    """The node-level contract: a dead invocation counts, rejects stale
    verdicts, and escalates on the second consecutive death."""

    def _run_node(self, monkeypatch, tmp_path, state, agent_result):
        monkeypatch.setattr(g, "_notify_phase", lambda *a, **k: None)
        monkeypatch.setattr(g, "set_tool_context", lambda *a, **k: None)
        monkeypatch.setattr(g, "clear_tool_context", lambda: None)
        monkeypatch.setattr(g, "_get_project_root", lambda: str(tmp_path))
        monkeypatch.setattr(g, "build_code_tester_message", lambda state: [])
        monkeypatch.setattr(g, "_log_agent_context", lambda *a, **k: None)
        monkeypatch.setattr(g, "_start_heartbeat", lambda *a, **k: 0)
        monkeypatch.setattr(g, "_stop_heartbeat", lambda *a, **k: None)
        monkeypatch.setattr(g, "_agent_config", lambda *a, **k: {})
        monkeypatch.setattr(g, "_persist_agent_logs", lambda *a, **k: None)
        monkeypatch.setattr(g, "create_code_tester", lambda site_slug="", **_: object())
        monkeypatch.setattr(g, "_invoke_agent_with_timeout",
                            lambda *a, **k: agent_result)
        monkeypatch.setattr(g, "_probe_phase1_discovery",
                            lambda *a, **k: (False, None, None))
        return g._invoke_code_tester(state, RunnableConfig())

    _DEAD = {"messages": [], "_error": "wall-clock timeout after 900s",
             "_error_class": "WallClockTimeout"}

    def test_first_death_rejects_stale_report_and_counts(self, monkeypatch, tmp_path):
        _write_report(tmp_path, assessment="CRASH", age=3600)
        state = {"job_id": 0, "site_slug": SLUG, "test_retry_count": 0,
                 "test_report": {"overall_assessment": "CRASH"}}
        update = self._run_node(monkeypatch, tmp_path, state, self._DEAD)
        assert update.get("tester_wall_clock_timeouts") == 1
        # BOTH stale vectors closed: the file (mtime floor) and state (the
        # previous cycle's report must not ride along).
        assert update.get("test_report") is None
        assert "interrupt_reason" not in update

    def test_second_death_stages_the_escalation(self, monkeypatch, tmp_path):
        state = {"job_id": 0, "site_slug": SLUG, "test_retry_count": 0,
                 "tester_wall_clock_timeouts": 1}
        update = self._run_node(monkeypatch, tmp_path, state, self._DEAD)
        assert update.get("tester_wall_clock_timeouts") == 2
        assert update.get("interrupt_reason") == "code_tester_wall_clock"
        labels = [d.get("label") for d in update.get("interrupt_decisions", [])]
        assert "Retry testing" in labels and "Execute anyway" in labels

    def test_second_death_skip_approvals_fails_honestly(self, monkeypatch, tmp_path):
        state = {"job_id": 0, "site_slug": SLUG, "test_retry_count": 0,
                 "tester_wall_clock_timeouts": 1, "skip_approvals": True}
        update = self._run_node(monkeypatch, tmp_path, state, self._DEAD)
        assert update.get("execution_status") == "FAILED"
        assert update.get("error_message")
        assert "interrupt_reason" not in update, (
            "skip_approvals auto-approves — staging a retry interrupt would "
            "burn a third full window against the same wall"
        )

    def test_healthy_invocation_resets_counter_and_adopts_fresh_report(
        self, monkeypatch, tmp_path,
    ):
        _write_report(tmp_path, assessment="PASS")
        # mtime floor: the report must postdate the attempt stamp.
        p = tmp_path / "workspace" / SLUG / "test_report.json"
        future = time.time() + 120
        os.utime(p, (future, future))
        state = {"job_id": 0, "site_slug": SLUG, "test_retry_count": 0,
                 "tester_wall_clock_timeouts": 1}
        update = self._run_node(
            monkeypatch, tmp_path, state, {"messages": ["verdict written"]},
        )
        assert update.get("tester_wall_clock_timeouts") == 0
        assert (update.get("test_report") or {}).get("overall_assessment") == "PASS"

    def test_non_wallclock_death_does_not_count_but_still_rejects_stale(
        self, monkeypatch, tmp_path,
    ):
        _write_report(tmp_path, age=3600)
        state = {"job_id": 0, "site_slug": SLUG, "test_retry_count": 0,
                 "tester_wall_clock_timeouts": 1}
        update = self._run_node(
            monkeypatch, tmp_path, state,
            {"messages": [], "_error": "429 too many requests",
             "_error_class": "RateLimitError"},
        )
        # Counter untouched (it counts wall-clock deaths specifically — the
        # state value of 1 simply persists) but the stale verdict is still
        # rejected — no dead invocation routes on it.
        assert "tester_wall_clock_timeouts" not in update
        assert update.get("test_report") is None


class TestEscalationRouting:
    def test_two_deaths_route_to_human_approval(self):
        from agents.nodes.route_after_testing import route_after_testing

        assert route_after_testing({
            "test_report": None, "test_retry_count": 0,
            "tester_wall_clock_timeouts": 2,
        }) == "human_approval"

    def test_two_deaths_skip_approvals_route_to_cleanup(self):
        from agents.nodes.route_after_testing import route_after_testing

        assert route_after_testing({
            "test_report": None, "test_retry_count": 0,
            "tester_wall_clock_timeouts": 2, "skip_approvals": True,
        }) == "cleanup"

    def test_one_death_follows_the_normal_no_report_ladder(self):
        from agents.nodes.route_after_testing import route_after_testing

        assert route_after_testing({
            "test_report": None, "test_retry_count": 0,
            "tester_wall_clock_timeouts": 1,
        }) == "scraper_analyzer"

    def test_counter_ignored_when_a_verdict_exists(self):
        from agents.nodes.route_after_testing import route_after_testing

        # A fresh PASS verdict overrides the historical counter — the draft
        # was judged; route on the verdict.
        assert route_after_testing({
            "test_report": {"overall_assessment": "PASS",
                            "confidence_score": 0.95, "issues": []},
            "test_retry_count": 0, "tester_wall_clock_timeouts": 2,
        }) == "field_confirmation"


class TestResumeRouting:
    def test_retry_retests_the_same_draft(self):
        assert g.route_from_human_approval({
            "interrupt_reason": "code_tester_wall_clock",
            "human_response": {"decision": "approve", "label": "Retry testing"},
        }) == "code_tester"

    def test_execute_anyway_proceeds(self):
        assert g.route_from_human_approval({
            "interrupt_reason": "code_tester_wall_clock",
            "human_response": {"decision": "approve", "label": "Execute anyway"},
        }) == "field_confirmation"

    def test_cancel_ends(self):
        assert g.route_from_human_approval({
            "interrupt_reason": "code_tester_wall_clock",
            "human_response": {"decision": "reject", "label": "Cancel"},
        }) == "__end__"

    def test_code_tester_is_a_legal_resume_destination(self):
        """The resume map must contain code_tester — a path fn returning an
        unmapped node kills edge resolution."""
        import re

        src = open(os.path.join(ROOT, "webapp", "agents", "graph.py"),
                   encoding="utf-8").read()
        assert re.search(r'"code_tester":\s*"code_tester"', src)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
