"""[session-audit job-329] The draft-corruption walls.

Job 329 (proven RCA, 2026-09-05 forensic audit): the code_writer exceeded its
900s wall-clock twice and its ABANDONED threads kept ``edit_file``-ing
``workspace/crocs-com/scraper_draft.py`` AFTER the tester verdict — the last
edit landed 19s before ``run_execution`` launched. The corrupted draft died at
Python compile time (55s, zero HTTP requests):

    SyntaxError: expected ':'  (scraper_draft.py, line 1292)

and the job was first RCA'd as a rate-band failure because the gate that
should have caught it — ``run_execution._accepted_cli_flags`` — parses the
very same file with ``ast.parse`` inside ``except Exception: return None``,
laundering "this file is not Python" into "flags undeterminable" and launching
a doomed subprocess. This module pins the walls that make that class impossible
to miss again:

- loud compile gate: ``_draft_parse_error`` reports corruption; run_execution
  refuses to launch a non-parsing draft and fails the job with the real
  SyntaxError in ``error_message`` — BEFORE any dispatch.
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

# agents.nodes shadows the submodule name with the node FUNCTION, so import
# the true module by path (its dict is what run_execution's globals resolve).
import importlib

rexec = importlib.import_module("agents.nodes.run_execution")

# The exact corruption that killed job 329 (line 1292 of its draft).
CORRUPT_DRAFT = (
    "def _dom_price(soup) -> str:\n"
    "    return ''\n"
    "\n"
    "\n"
    "def _dom_category(soup) -> str joined_marker:\n"
    "    return ''\n"
)

VALID_DRAFT = (
    "import argparse\n"
    "\n"
    "parser = argparse.ArgumentParser()\n"
    "parser.add_argument('--sample', action='store_true')\n"
)


def _seed_workspace(tmp_path, body: str, slug: str = "crocs-com"):
    ws = tmp_path / "workspace" / slug
    ws.mkdir(parents=True)
    (ws / "scraper_draft.py").write_text(body)
    return tmp_path


class TestLoudCompileGate:
    def test_parse_error_reporter_names_the_corruption(self, tmp_path):
        """A corrupt draft yields a reporter string carrying SyntaxError and
        the offending line number — the signal job 329's RCA never read."""
        draft = _seed_workspace(tmp_path, CORRUPT_DRAFT) / "workspace" / "crocs-com" / "scraper_draft.py"
        err = rexec._draft_parse_error(str(draft))
        assert err is not None
        assert "SyntaxError" in err
        assert "line 5" in err

    def test_parse_error_reporter_silent_on_valid_draft(self, tmp_path):
        draft = _seed_workspace(tmp_path, VALID_DRAFT) / "workspace" / "crocs-com" / "scraper_draft.py"
        assert rexec._draft_parse_error(str(draft)) is None

    def test_run_execution_refuses_corrupt_draft_before_any_dispatch(self, tmp_path, monkeypatch):
        """The 329 kill chain ends here: a non-parsing draft must produce an
        honest FAILED with the SyntaxError in error_message, and the node must
        exit before flag-probing or dispatching — a doomed subprocess is never
        launched, so the failure cannot masquerade as a zero-item run."""
        tmp = _seed_workspace(tmp_path, CORRUPT_DRAFT)
        monkeypatch.setattr(rexec, "_get_project_root", lambda: str(tmp))
        # _notify_phase is imported function-locally from ..graph — patch it there.
        import agents.graph as ag
        monkeypatch.setattr(ag, "_notify_phase", lambda *a, **k: None)
        probes: list = []
        monkeypatch.setattr(rexec, "_accepted_cli_flags", lambda p: probes.append(p))

        import agents.tools.browser_http as bh

        dispatches: list = []
        monkeypatch.setattr(bh, "post_scrape_with_retry", lambda *a, **k: dispatches.append(a))

        result = rexec.run_execution({"job_id": 0, "site_slug": "crocs-com"})

        assert result["execution_status"] == "FAILED"
        assert "SyntaxError" in result["error_message"]
        assert probes == [], "gate must fire before the flag probe"
        assert dispatches == [], "gate must fire before any dispatch"

    def test_valid_draft_still_reaches_the_flag_probe(self, tmp_path, monkeypatch):
        """The gate is a wall against corruption, not a new bottleneck: a
        parsing draft passes the entry check and the node proceeds (proven by
        the flag probe being reached)."""
        tmp = _seed_workspace(tmp_path, VALID_DRAFT)
        monkeypatch.setattr(rexec, "_get_project_root", lambda: str(tmp))
        # _notify_phase is imported function-locally from ..graph — patch it there.
        import agents.graph as ag
        monkeypatch.setattr(ag, "_notify_phase", lambda *a, **k: None)
        probes: list = []
        monkeypatch.setattr(rexec, "_accepted_cli_flags", lambda p: probes.append(p) or set())

        import agents.tools.browser_http as bh

        monkeypatch.setattr(bh, "post_scrape_with_retry", lambda *a, **k: (_ for _ in ()).throw(AssertionError("dispatch not expected in this test")))

        # wave-30 W30-4: --fresh-discovery (which used to make `args`
        # non-empty unconditionally) is now gated to Phase-1 modes, and the
        # CLI-contract probe below only runs `if args:`. scope=firstn keeps
        # args non-empty without engaging any mode-specific gate.
        rexec.run_execution(
            {"job_id": 0, "site_slug": "crocs-com", "scope": "firstn", "scope_value": "5"}
        )
        assert probes, "valid draft must pass the entry gate and reach flag probing"


class TestDraftFreeze:
    """Wall #2: what executes must be byte-identical to what the tester judged.

    Job 329: the tester PASSED at 05:39:40; abandoned writer threads edited the
    draft at 05:39:56/05:40:14/05:40:45; execution launched 05:41:04 on a file
    the tester never saw. The freeze pins the verdict-time hash into state at
    the tester and refuses execution on any drift."""

    def _workspace(self, tmp_path, body=VALID_DRAFT):
        tmp = _seed_workspace(tmp_path, body)
        return tmp, tmp / "workspace" / "crocs-com" / "scraper_draft.py"

    def _patch_node(self, monkeypatch, tmp):
        monkeypatch.setattr(rexec, "_get_project_root", lambda: str(tmp))
        import agents.graph as ag

        monkeypatch.setattr(ag, "_notify_phase", lambda *a, **k: None)
        probes: list = []
        monkeypatch.setattr(rexec, "_accepted_cli_flags", lambda p: probes.append(p) or set())
        import agents.tools.browser_http as bh

        monkeypatch.setattr(bh, "post_scrape_with_retry", lambda *a, **k: (_ for _ in ()).throw(AssertionError("dispatch not expected")))
        return probes

    def test_modified_draft_refused_at_execution(self, tmp_path, monkeypatch):
        tmp, _draft = self._workspace(tmp_path)
        probes = self._patch_node(monkeypatch, tmp)
        result = rexec.run_execution(
            {"job_id": 0, "site_slug": "crocs-com", "tested_draft_sha256": "0" * 64}
        )
        assert result["execution_status"] == "FAILED"
        assert "modified after" in result["error_message"]
        assert "tested" in result["error_message"]
        assert probes == [], "refusal must precede flag probing"

    def test_matching_hash_passes_the_gate(self, tmp_path, monkeypatch):
        import hashlib

        tmp, draft = self._workspace(tmp_path)
        probes = self._patch_node(monkeypatch, tmp)
        good = hashlib.sha256(draft.read_bytes()).hexdigest()
        rexec.run_execution(
            {
                "job_id": 0,
                "site_slug": "crocs-com",
                "tested_draft_sha256": good,
                # wave-30 W30-4: keep args non-empty (the probe is `if args:`)
                "scope": "firstn",
                "scope_value": "5",
            }
        )
        assert probes, "byte-identical draft must sail through the freeze"

    def test_legacy_job_without_hash_is_not_blocked(self, tmp_path, monkeypatch):
        tmp, _ = self._workspace(tmp_path)
        probes = self._patch_node(monkeypatch, tmp)
        rexec.run_execution(
            {
                "job_id": 0,
                "site_slug": "crocs-com",
                "scope": "firstn",  # wave-30 W30-4: keep args non-empty
                "scope_value": "5",
            }
        )
        assert probes, "no recorded hash (legacy/resume path) must not block"

    def test_stamp_helper_hashes_current_draft(self, tmp_path):
        import hashlib

        tmp, draft = self._workspace(tmp_path)
        update: dict = {}
        rexec._stamp_tested_draft(update, "crocs-com", project_root=str(tmp))
        assert update["tested_draft_sha256"] == hashlib.sha256(draft.read_bytes()).hexdigest()

    def test_stamp_helper_tolerates_missing_draft(self, tmp_path):
        tmp = tmp_path  # no workspace seeded
        update: dict = {}
        rexec._stamp_tested_draft(update, "never-existed", project_root=str(tmp))
        assert update["tested_draft_sha256"] is None

    def test_tester_stamps_hash_at_verdict(self):
        """Source contract: _invoke_code_tester stamps the freeze hash before
        its single return, so EVERY verdict branch (pass/fail/recycled) pins
        the draft it judged."""
        with open(
            os.path.join(ROOT, "webapp", "agents", "graph.py"), encoding="utf-8"
        ) as fh:
            src = fh.read()
        fn_start = src.index("def _invoke_code_tester(")
        fn_end = src.index("def _invoke_cleanup(")
        body = src[fn_start:fn_end]
        stamp_pos = body.index("_stamp_tested_draft(update")
        ret_pos = body.rindex("return update")
        assert stamp_pos < ret_pos, "hash must be stamped before the verdict return"


class TestAbandonedWriterGuard:
    """Wall #0 — the ROOT of the 329 chain: an abandoned wall-clock thread
    must lose its tools.

    ``_invoke_agent_with_timeout`` abandons the agent thread on deadline and
    returns; the zombie keeps looping LLM rounds whose ``edit_file`` calls
    landed on the draft AFTER the tester verdict (05:39:56/05:40:14/05:40:45
    on job 329). The fix is cooperative: abandonment latches an
    invocation-cancelled flag in the shared tool context, and a global
    BaseTool patch makes every subsequent tool call raise until the next
    fresh invocation resets it. The thread cannot be killed (Python), but it
    can be disarmed."""

    @pytest.fixture(autouse=True)
    def _rearm_after_test(self):
        """These tests latch the process-global cancel; the latch deliberately
        survives clear_tool_context (that is the design), so each test must
        leave a fresh (armed) context behind or every later tool-using test in
        the same process hits '[invocation-cancelled]' refusals."""
        yield
        self._fresh_context()

    def _fresh_context(self):
        from agents.tools.context import set_tool_context

        set_tool_context({}, agent_name="code_writer")

    def test_cancel_latch_survives_clear_and_resets_on_next_invoke(self):
        from agents.tools import context as tctx

        self._fresh_context()
        assert not tctx.is_invocation_cancelled()
        tctx.mark_invocation_cancelled("code_writer")
        assert tctx.is_invocation_cancelled()
        # The node's finally clear_tool_context() runs while the zombie lives:
        tctx.clear_tool_context()
        assert tctx.is_invocation_cancelled(), (
            "the latch must outlive the context clear or the zombie rearms"
        )
        # A fresh invocation re-arms:
        self._fresh_context()
        assert not tctx.is_invocation_cancelled()

    def test_tool_run_refuses_once_cancelled(self):
        from agents.subagents import _install_invocation_cancellation
        from agents.tools import context as tctx
        from langchain_core.tools import tool

        _install_invocation_cancellation()
        self._fresh_context()

        @tool
        def echo_tool(x: str) -> str:
            """echoes x"""

            return x

        assert echo_tool.invoke({"x": "hi"}) == "hi"
        tctx.mark_invocation_cancelled("code_writer")
        with pytest.raises(Exception, match="cancelled"):
            echo_tool.invoke({"x": "hi"})
        # and a fresh invocation works again:
        self._fresh_context()
        assert echo_tool.invoke({"x": "hi"}) == "hi"

    def test_wall_clock_abandon_latches_the_cancel(self, monkeypatch):
        import threading
        import time as _time

        import agents.graph as ag
        from agents.tools import context as tctx

        self._fresh_context()

        class SleepyAgent:
            def invoke(self, messages, cfg=None):
                _time.sleep(1.5)
                return {"messages": ["late"]}

        monkeypatch.setattr(ag, "_log_event_row", lambda *a, **k: None)
        monkeypatch.setattr(ag, "_notify_phase", lambda *a, **k: None)

        result = ag._invoke_agent_with_timeout(
            SleepyAgent(), [], {}, "code_writer", 0, timeout=0.3
        )

        assert result.get("_error_class") == "WallClockTimeout"
        assert tctx.is_invocation_cancelled(), (
            "abandonment must latch the cancel — the zombie's next edit_file "
            "must refuse"
        )
        assert not threading.current_thread().is_alive() or True


class TestPerInvocationZombieDisarm:
    """[wave-24 W24-1] The prod-395 regression: the wave-17 global latch is
    SELF-RESETTING — every fresh ``set_tool_context`` re-arms
    ``invocation_cancelled=False``, and a fresh invocation is exactly what a
    zombie thread crosses (writer wall-clock death → tester phase starts →
    the zombie's ``edit_file`` landed 2s into the tester's run).

    The per-invocation latch: ``_invoke_agent_with_timeout`` mints an id on
    the node thread and stamps it INSIDE the spawned invoke thread's context;
    LangGraph's ToolNode dispatches sync tools through a
    ``ContextThreadPoolExecutor`` that copies the SUBMITTING thread's
    context, so every tool call this invocation makes executes with that id
    visible. Cancellation records the id into a process-global set that no
    fresh ``set_tool_context`` resets. The legacy global flag keeps its
    semantics for unstamped callers (pinned by TestAbandonedWriterGuard)."""

    def _echo_tool(self):
        from agents.subagents import _install_invocation_cancellation
        from langchain_core.tools import tool

        @tool
        def echo_tool(x: str) -> str:
            """echoes x"""

            return x

        _install_invocation_cancellation()
        return echo_tool

    def test_prod395_regression_zombie_stays_disarmed_across_set_tool_context(
        self, monkeypatch
    ):
        """The exact 395 shape through the REAL abandon path: the writer times
        out, the tester phase calls ``set_tool_context``, and the zombie's
        copied context must STILL refuse while the next invocation is armed."""
        import threading
        import time as _time
        from contextvars import copy_context

        import agents.graph as ag
        from agents.tools import context as tctx

        echo_tool = self._echo_tool()

        captured: dict = {}

        class SleepyAgent:
            def invoke(self, messages, cfg=None):
                # Runs INSIDE the zombie thread — its context carries the
                # invocation stamp if _invoke_agent_with_timeout applied it.
                captured["ctx"] = copy_context()
                _time.sleep(1.5)
                return {"messages": ["late"]}

        monkeypatch.setattr(ag, "_log_event_row", lambda *a, **k: None)
        monkeypatch.setattr(ag, "_notify_phase", lambda *a, **k: None)

        result = ag._invoke_agent_with_timeout(
            SleepyAgent(), [], {}, "code_writer", 0, timeout=0.3
        )

        assert result.get("_error_class") == "WallClockTimeout"
        assert "ctx" in captured, "agent must have run inside a stamped thread"

        # The tester phase starts — the prod-395 re-arm trigger:
        tctx.set_tool_context({"url": "http://tester"}, agent_name="code_tester")
        assert not tctx.is_invocation_cancelled(), "fresh phase must be armed"

        # The zombie's NEXT tool call — dispatched through ITS copied context —
        # must refuse even though the process-global flag was just reset:
        with pytest.raises(Exception, match="cancelled"):
            captured["ctx"].run(echo_tool.invoke, {"x": "hi"})

        # A fresh stamped invocation (the tester's own agent thread) is armed:
        fresh_box: dict = {}

        def fresh_thread():
            tctx.stamp_invocation(tctx.new_invocation_id())
            fresh_box["ctx"] = copy_context()

        t = threading.Thread(target=fresh_thread, daemon=True)
        t.start()
        t.join(5)
        assert fresh_box["ctx"].run(echo_tool.invoke, {"x": "hi"}) == "hi"

    def test_cancelled_stamp_propagates_through_langgraph_executor(self):
        """The mechanism the latch leans on: the executor returned by
        ``get_executor_for_config`` (what ToolNode uses) must copy the
        submitting context's stamp, so a cancelled invocation refuses inside
        the worker thread even with the LEGACY flag reset."""
        from contextvars import copy_context as _ctx_copy  # noqa: F401

        from agents.tools import context as tctx
        from langchain_core.runnables.config import get_executor_for_config

        echo_tool = self._echo_tool()

        token = tctx.stamp_invocation(tctx.new_invocation_id())
        tctx.mark_invocation_cancelled("code_writer")
        tctx.set_tool_context({}, agent_name="code_tester")  # resets LEGACY flag only

        assert tctx.is_invocation_cancelled(), (
            "stamped view must stay disarmed across set_tool_context"
        )

        with get_executor_for_config({}) as executor:
            with pytest.raises(Exception, match="cancelled"):
                executor.submit(lambda: echo_tool.invoke({"x": "hi"})).result(10)

        tctx.unstamp_invocation(token)
        assert not tctx.is_invocation_cancelled()
        assert echo_tool.invoke({"x": "hi"}) == "hi"

    def test_cancelled_set_is_capped(self):
        from agents.tools import context as tctx

        tctx.reset_invocation_latches()
        try:
            total = tctx._CANCELLED_INVOCATIONS_CAP + 100
            for i in range(total):
                tctx.mark_invocation_cancelled("phase", invocation_id=f"id-{i}")
            assert len(tctx._cancelled_invocations) <= tctx._CANCELLED_INVOCATIONS_CAP
            newest = f"id-{total - 1}"
            assert newest in tctx._cancelled_invocations, "newest must survive"
        finally:
            tctx.reset_invocation_latches()

    def test_reset_invocation_latches_clears_everything(self):
        from agents.tools import context as tctx

        token = tctx.stamp_invocation(tctx.new_invocation_id())
        tctx.mark_invocation_cancelled("phase")
        assert tctx.is_invocation_cancelled()
        tctx.reset_invocation_latches()
        assert not tctx.is_invocation_cancelled()
        assert not tctx._cancelled_invocations
        assert tctx._current_invocation_id.get(None) is None
        tctx.unstamp_invocation(token)  # token reset after None-set is tolerated


class TestMidTestDraftMutation:
    """[wave-24 W24-1 layer 2] Verdict integrity: a draft mutated WHILE the
    tester ran (prod 395: zombie edit_file 2s into the tester's browser run)
    makes the verdict a judgment of a moving target. The tester fingerprints
    the draft at entry and exit and stamps ``draft_mutated_during_test``; the
    router spends ONE code_tester pass on what is actually on disk before
    accepting a PASS. (Layer 2 of the zombie defense — layer 1 disarms the
    zombie itself; this wall catches every other mutation path.)"""

    def _pass_state(self, **over):
        state = {
            "job_id": 0,
            "test_report": {
                "overall_assessment": "PASS",
                "confidence_score": 0.9,
                "issues": [],
                "successful_extractions": 5,
            },
            "test_retry_count": 0,
            "input_mode": "url_list",
            "site_slug": "crocs-com",
            "draft_mutated_during_test": True,
            "mutated_verdict_retests": 0,
        }
        state.update(over)
        return state

    def test_mutated_pass_bounces_to_one_retest(self):
        from agents.nodes.route_after_testing import route_after_testing

        assert route_after_testing(self._pass_state()) == "code_tester"

    def test_mutated_pass_accepted_once_allowance_spent(self):
        from agents.nodes.route_after_testing import route_after_testing

        assert (
            route_after_testing(self._pass_state(mutated_verdict_retests=1))
            == "field_confirmation"
        )

    def test_clean_pass_not_bounced(self):
        from agents.nodes.route_after_testing import route_after_testing

        state = self._pass_state(draft_mutated_during_test=False)
        assert route_after_testing(state) == "field_confirmation"

    def test_mutated_fail_keeps_fail_ladder(self):
        """The un-proven wall guards PASS acceptance only — a FAIL verdict on
        a mutated draft still routes through the normal fail ladder (the
        writer fixes the draft currently on disk)."""
        from agents.nodes.route_after_testing import route_after_testing

        state = self._pass_state(
            test_report={
                "overall_assessment": "FAIL",
                "confidence_score": 0.2,
                "issues": [],
            },
        )
        assert route_after_testing(state) != "code_tester"

    def test_stamp_helper_semantics(self):
        import agents.graph as ag

        m = ag._draft_moved_during_test
        assert m("", "") is False
        assert m("aaa", "") is False
        assert m("", "bbb") is False
        assert m("aaa", "aaa") is False
        assert m("aaa", "bbb") is True

    def test_tester_stamps_mutation_flag_at_verdict(self):
        """Source contract: _invoke_code_tester fingerprints the draft at
        ENTRY (before the invoke) and stamps draft_mutated_during_test into
        its update — so every verdict branch records whether the draft moved
        under the test, and a re-entry consumes the router's allowance."""
        with open(
            os.path.join(ROOT, "webapp", "agents", "graph.py"), encoding="utf-8"
        ) as fh:
            src = fh.read()
        fn_start = src.index("def _invoke_code_tester(")
        fn_end = src.index("def _invoke_cleanup(")
        body = src[fn_start:fn_end]
        # Entry fingerprint precedes the agent invocation:
        entry_pos = body.index("draft_file_fp(_te_draft)")
        invoke_pos = body.index("_invoke_agent_with_timeout(")
        assert entry_pos < invoke_pos, "entry fp must be taken before the invoke"
        # The stamp lands in the update dict before the single return:
        stamp_line = 'update["draft_mutated_during_test"] = _draft_moved_during_test('
        stamp_pos = body.index(stamp_line)
        assert stamp_pos < body.rindex("return update")
        # Re-entry consumes the router's one-re-test allowance:
        assert 'update["mutated_verdict_retests"]' in body
