"""[wave-30 W30-7] Zombie-latch refusals leave a forensic trail in SessionLog.

Prod proof (job 569 + the job-329 lineage): when ``_invoke_agent_with_timeout``
abandons its thread, the wave-24 latch makes every subsequent tool call raise
``[invocation-cancelled]`` — but the raise is INVISIBLE. SessionLog shows the
abandoned agent simply stopping mid-phase, and the ToolCallLog trail (W30-6)
says what was called BEFORE the deadline, not that the zombie kept trying
after it. RCA on 569 initially concluded "the writer did nothing" partly
because nothing distinguished a clean finish from a disarmed zombie still
burning LLM rounds.

Contract:
1. Every refusal raises exactly as before (behavior UNCHANGED) and additionally
   writes ONE ``[ZOMBIE-REFUSED]`` SessionLog row naming the tool, attributed
   to the job and the invoking agent from the tool context.
2. Rows are capped at the first 10 per invocation — a zombie looping LLM
   rounds can refuse hundreds of times; 10 rows is forensic signal, the rest
   is noise.
3. The cap counter is PER INVOCATION: a second latched invocation logs its
   own refusals from 1.
4. No job context (unstamped legacy latch, playground) → refusal unchanged,
   no row, no crash.
5. A telemetry failure (DB down) must never change the refusal: the raise
   still happens, still the latch's marker.
"""
from __future__ import annotations

import importlib
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

ctx = importlib.import_module("agents.tools.context")  # noqa: E402
subagents = importlib.import_module("agents.subagents")  # noqa: E402

from langchain_core.tools import BaseTool  # noqa: E402


class EchoTool(BaseTool):
    name: str = "echo"
    description: str = "test double"

    def _run(self, x: str = "") -> str:
        return x


class FakeManager:
    def __init__(self):
        self.rows = []

    def filter(self, **kw):
        return self

    def count(self):
        return len(self.rows)

    def create(self, **kw):
        self.rows.append(kw)
        return types.SimpleNamespace(**kw)


class FakeModel:
    ROLE_SYSTEM = "system"
    ROLE_ASSISTANT = "assistant"
    ROLE_TOOL = "tool"
    ROLE_USER = "user"

    def __init__(self):
        self.objects = FakeManager()


@pytest.fixture()
def env(monkeypatch):
    import scraper.models as sm

    sl = FakeModel()
    monkeypatch.setattr(sm, "SessionLog", sl)
    ctx.reset_invocation_latches()
    subagents._install_invocation_cancellation()
    yield sl.objects
    ctx.reset_invocation_latches()


def _refuse_once(tool):
    with pytest.raises(RuntimeError, match="invocation-cancelled"):
        tool.invoke({"x": "hi"})


class TestRefusalTelemetry:
    def test_refusal_writes_one_zombie_row(self, env):
        tool = EchoTool()
        iid = ctx.new_invocation_id()
        ctx.set_tool_context({"job_id": 77}, agent_name="code_writer")
        token = ctx.stamp_invocation(iid)
        try:
            ctx.mark_invocation_cancelled(phase="code_writer", invocation_id=iid)
            _refuse_once(tool)
        finally:
            ctx.unstamp_invocation(token)
        assert len(env.rows) == 1, (
            "the refusal must leave exactly one forensic SessionLog row — "
            "today the raise is invisible and RCA reads it as 'agent did "
            "nothing'"
        )
        row = env.rows[0]
        assert row["job_id"] == 77
        assert row["role"] == FakeModel.ROLE_SYSTEM
        assert row["agent"] == "code_writer"
        assert "[ZOMBIE-REFUSED]" in row["content"]
        assert "echo" in row["content"]

    def test_only_first_ten_refusals_are_logged(self, env):
        tool = EchoTool()
        iid = ctx.new_invocation_id()
        ctx.set_tool_context({"job_id": 77}, agent_name="code_writer")
        token = ctx.stamp_invocation(iid)
        try:
            ctx.mark_invocation_cancelled(phase="code_writer", invocation_id=iid)
            for _ in range(12):
                _refuse_once(tool)
        finally:
            ctx.unstamp_invocation(token)
        assert len(env.rows) == subagents._ZOMBIE_REFUSAL_LOG_CAP == 10, (
            "a zombie looping LLM rounds can refuse hundreds of times — "
            "rows must cap at 10 per invocation"
        )

    def test_counters_are_per_invocation(self, env):
        tool = EchoTool()
        ctx.set_tool_context({"job_id": 77}, agent_name="code_writer")
        iid_a, iid_b = ctx.new_invocation_id(), ctx.new_invocation_id()

        token = ctx.stamp_invocation(iid_a)
        try:
            ctx.mark_invocation_cancelled(invocation_id=iid_a)
            for _ in range(10):
                _refuse_once(tool)
        finally:
            ctx.unstamp_invocation(token)

        token = ctx.stamp_invocation(iid_b)
        try:
            ctx.mark_invocation_cancelled(invocation_id=iid_b)
            _refuse_once(tool)
        finally:
            ctx.unstamp_invocation(token)

        assert len(env.rows) == 11, (
            "a second latched invocation must log its own refusals from 1 — "
            "the cap is per invocation, not per process"
        )
        assert "1" in env.rows[-1]["content"]

    def test_refusal_without_job_context_still_refuses(self, env):
        tool = EchoTool()
        ctx.set_tool_context({}, agent_name="")
        ctx.mark_invocation_cancelled()  # legacy (unstamped) latch
        _refuse_once(tool)
        assert env.rows == [], "no job context → nothing to attribute, no row"

    def test_healthy_call_is_untouched(self, env):
        tool = EchoTool()
        ctx.set_tool_context({"job_id": 77}, agent_name="code_writer")
        assert tool.invoke({"x": "hi"}) == "hi"
        assert env.rows == []

    def test_sessionlog_failure_does_not_block_the_raise(self, env, monkeypatch):
        def boom(**kw):
            raise RuntimeError("db down")

        monkeypatch.setattr(env, "create", boom)
        tool = EchoTool()
        iid = ctx.new_invocation_id()
        ctx.set_tool_context({"job_id": 77}, agent_name="code_writer")
        token = ctx.stamp_invocation(iid)
        try:
            ctx.mark_invocation_cancelled(invocation_id=iid)
            # The RAISE must be the latch's, not the telemetry's.
            with pytest.raises(RuntimeError, match="invocation-cancelled"):
                tool.invoke({"x": "hi"})
        finally:
            ctx.unstamp_invocation(token)


class TestRefusalCounter:
    def test_note_tool_refusal_counts_and_exposes(self):
        ctx.reset_invocation_latches()
        iid = ctx.new_invocation_id()
        token = ctx.stamp_invocation(iid)
        try:
            assert ctx.get_invocation_refusal_count() == 0
            assert ctx.note_tool_refusal("echo") == 1
            assert ctx.note_tool_refusal("edit_file") == 2
            assert ctx.get_invocation_refusal_count() == 2
        finally:
            ctx.unstamp_invocation(token)
            ctx.reset_invocation_latches()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
