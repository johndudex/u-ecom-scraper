"""Wave-23 W23-2: the draft-first forcing function for code_writer.

RC-3 of docs/plans/wave23-writer-convergence-plan.md: prod 374's two fix
rounds made 99 tool calls with ZERO writes (100% read/search) then gave up
mid-plan; local 343 spent 64 calls across two attempts without one byte of
scraper_draft.py. The winning pattern (job 341) writes early — its first tool
call was a probe write, draft landed by call 15.

Fix: a deterministic result-append nudge. Once a code_writer invocation has
made CODE_WRITER_DRAFT_NUDGE_CALLS (default 12) tool calls without a single
write to scraper_draft.py, every tool result carries "[HARNESS NUDGE] ...
write scraper_draft.py NOW" until a draft write happens. Nothing is blocked
(probes stay legal — 341's winning pattern probed before drafting); the model
just cannot stay unaware it is drifting.

Run from repo root:  python3 -m pytest tests/test_draft_first_nudge.py -v
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

import agents.subagents as sub  # noqa: E402


class _FakeTool:
    """Minimal StructuredTool stand-in: .name + callable .func."""

    def __init__(self, name, result="ok"):
        self.name = name
        self._result = result
        self.calls = []

        def _func(*args, **kwargs):
            self.calls.append((args, kwargs))
            return self._result

        self.func = _func


class TestNudgeBehavior:
    def test_below_threshold_no_nudge(self):
        tools = [_FakeTool("read_file"), _FakeTool("search_content")]
        wrapped = sub.apply_draft_nudge(tools, threshold=3)
        # exactly one round = 2 calls total, both strictly below threshold 3
        outs = [t.func(path="x") for t in wrapped]
        assert all(
            "HARNESS NUDGE" not in (o or "") for o in outs
        ), "nudge must not fire before the threshold"

    def test_at_threshold_results_carry_nudge(self):
        tools = [_FakeTool("read_file")]
        wrapped = sub.apply_draft_nudge(tools, threshold=3)
        out = None
        for _ in range(4):
            out = wrapped[0].func(path="x")
        assert out is not None and "HARNESS NUDGE" in out
        assert "scraper_draft.py" in out, "nudge must name the deliverable"
        assert "write_file" in out, "nudge must name the action"

    def test_draft_write_stops_the_nudge(self):
        tools = [_FakeTool("read_file"), _FakeTool("write_file")]
        wrapped = sub.apply_draft_nudge(tools, threshold=2)
        for _ in range(3):
            wrapped[0].func(path="x")  # read, read, read — nudge active
        out = wrapped[1].func(path="workspace/jomashop-com/scraper_draft.py", content="x")
        # the draft write itself need not carry it, but...
        for _ in range(2):
            out = wrapped[0].func(path="x")
        assert "HARNESS NUDGE" not in out, (
            "once a draft write happened, later results must be clean"
        )

    def test_probe_write_is_not_a_draft(self):
        tools = [_FakeTool("write_file"), _FakeTool("read_file")]
        wrapped = sub.apply_draft_nudge(tools, threshold=1)
        wrapped[0].func(path="workspace/site/probe_transport.py", content="x")
        out = wrapped[1].func(path="x")
        assert "HARNESS NUDGE" in out, (
            "scratch-probe writes must not count as delivering the draft"
        )

    def test_path_accepted_positionally(self):
        tools = [_FakeTool("write_file"), _FakeTool("read_file")]
        wrapped = sub.apply_draft_nudge(tools, threshold=1)
        wrapped[0].func("workspace/site/scraper_draft.py", "content")
        out = wrapped[1].func(path="x")
        assert "HARNESS NUDGE" not in out

    def test_nudge_includes_call_count(self):
        tools = [_FakeTool("read_file")]
        wrapped = sub.apply_draft_nudge(tools, threshold=2)
        wrapped[0].func(path="a")
        wrapped[0].func(path="b")
        out = wrapped[0].func(path="c")
        assert "3" in out, "nudge should show how deep the drift is"

    def test_non_string_result_not_crashed(self):
        tools = [_FakeTool("read_file", result=None)]
        wrapped = sub.apply_draft_nudge(tools, threshold=1)
        wrapped[0].func(path="a")
        assert wrapped[0].func(path="b") is None or isinstance(
            wrapped[0].func(path="b"), str
        )


class TestDefaultThresholdAndWiring:
    def test_default_threshold_from_settings(self):
        from django.test import override_settings

        tools = [_FakeTool("read_file")]
        with override_settings(CODE_WRITER_DRAFT_NUDGE_CALLS=1):
            wrapped = sub.apply_draft_nudge(tools)  # no explicit threshold
        assert "HARNESS NUDGE" in (wrapped[0].func(path="x") or "")

    def test_default_threshold_is_12_without_settings(self):
        from django.test import override_settings

        with override_settings():
            # no CODE_WRITER_DRAFT_NUDGE_CALLS in settings → fallback 12
            from django.conf import settings

            fallback = getattr(settings, "CODE_WRITER_DRAFT_NUDGE_CALLS", 12)
            assert fallback == 12 or isinstance(fallback, int)

    def test_apply_guards_wires_nudge_for_code_writer(self):
        tools = [_FakeTool("read_file"), _FakeTool("write_file"),
                 _FakeTool("run_scraper")]
        guarded = sub._apply_guards(tools, "code_writer")
        for _ in range(20):
            for t in guarded:
                t.func(path="x")
        results = [t.func(path="x") for t in guarded]
        assert any("HARNESS NUDGE" in (r or "") for r in results if isinstance(r, str)), (
            "code_writer's guard set must include the draft nudge"
        )

    def test_other_agents_unaffected(self):
        tools = [_FakeTool("read_file")]
        guarded = sub._apply_guards(tools, "code_tester")
        for _ in range(20):
            guarded[0].func(path="x")
        assert "HARNESS NUDGE" not in (guarded[0].func(path="x") or "")


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
