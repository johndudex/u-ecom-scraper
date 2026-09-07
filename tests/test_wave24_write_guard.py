"""[wave-24 W24-4] Writers must not spend a fix window regenerating the file.

Prod 395 cycle-2: a ``code-fix`` cascade with an explicit "do NOT rewrite
from scratch" + preserve-list — the writer spent 990s (55% of the window)
reading, then a single full ``write_file`` regenerated the file from the
template header and consumed 100% of the clock → timeout. Instruction-only
constraints have no mechanical force.

Fix: on a code-fix cycle (remediation present + same template + parseable
draft on disk — derived once in the writer node and stamped through
``_writer_codefix_cycle``), the harness intercepts a bare ``write_file``
targeting scraper_draft.py: first offense gets a nudge pointing at
``edit_file``; a write that carries ``full_rewrite_reason`` is allowed and
logged. A stubborn writer is allowed through after one nudge — the guard
must never be why a window ends with zero writes. Like the W23-2 nudge
state, the latch is per agent instance, so it spans the main + syntax + CLI
fix windows of one cycle; that is the desired behavior.
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


DRAFT = "workspace/jomashop-com/scraper_draft.py"


class TestCodeFixWriteGuard:
    def _wrapped(self, monkeypatch, codefix=True):
        if codefix:
            token = sub._writer_codefix_cycle.set(True)
        else:
            token = sub._writer_codefix_cycle.set(False)
        tools = [_FakeTool("write_file"), _FakeTool("edit_file")]
        try:
            wrapped = sub._apply_codefix_write_guard(tools)
        finally:
            sub._writer_codefix_cycle.reset(token)
        return wrapped

    def test_first_bare_draft_write_is_nudged_not_executed(self, monkeypatch):
        wrapped = self._wrapped(monkeypatch)
        out = wrapped[0].func(path=DRAFT, content="# full rewrite")
        assert "edit_file" in out and "full_rewrite_reason" in out
        assert wrapped[0].calls == [], "the bare rewrite must NOT hit disk"

    def test_second_bare_write_is_allowed(self, monkeypatch):
        """One nudge is enough — a stubborn writer must never end a window
        with zero writes because of the guard."""
        wrapped = self._wrapped(monkeypatch)
        wrapped[0].func(path=DRAFT, content="# full rewrite")
        out = wrapped[0].func(path=DRAFT, content="# full rewrite 2")
        assert out == "ok"
        assert len(wrapped[0].calls) == 1

    def test_write_with_full_rewrite_reason_allowed(self, monkeypatch):
        wrapped = self._wrapped(monkeypatch)
        out = wrapped[0].func(
            path=DRAFT, content="# rewrite", full_rewrite_reason="template drift"
        )
        assert out == "ok"
        assert len(wrapped[0].calls) == 1

    def test_non_draft_writes_never_intercepted(self, monkeypatch):
        wrapped = self._wrapped(monkeypatch)
        out = wrapped[0].func(path="workspace/jomashop-com/probe_transport.py", content="x")
        assert out == "ok"
        assert len(wrapped[0].calls) == 1

    def test_edit_file_untouched(self, monkeypatch):
        wrapped = self._wrapped(monkeypatch)
        out = wrapped[1].func(path=DRAFT, old_string="a", new_string="b")
        assert out == "ok"

    def test_off_codefix_cycle_tools_untouched(self, monkeypatch):
        wrapped = self._wrapped(monkeypatch, codefix=False)
        out = wrapped[0].func(path=DRAFT, content="# full rewrite")
        assert out == "ok"
        assert len(wrapped[0].calls) == 1

    def test_latch_spans_invocation_windows(self, monkeypatch):
        """The wrapper is applied at agent-build time; the main + syntax +
        CLI fix windows share one agent instance, so one nudge covers the
        whole cycle (documented W24-4 behavior)."""
        wrapped = self._wrapped(monkeypatch)
        for _ in range(5):
            wrapped[0].func(path="x", content="chatter")  # window 1 chatter
        first = wrapped[0].func(path=DRAFT, content="# rewrite")  # syntax window
        second = wrapped[0].func(path=DRAFT, content="# rewrite")  # CLI window
        assert "full_rewrite_reason" in first
        assert second == "ok"

    def test_apply_guards_wires_guard_for_code_writer(self, monkeypatch):
        token = sub._writer_codefix_cycle.set(True)
        try:
            tools = sub._apply_guards(
                [_FakeTool("write_file"), _FakeTool("run_scraper")], "code_writer"
            )
        finally:
            sub._writer_codefix_cycle.reset(token)
        out = tools[0].func(path=DRAFT, content="# rewrite")
        assert "full_rewrite_reason" in out


class TestWiringContracts:
    def test_node_stamps_codefix_signal(self):
        graph_path = os.path.join(ROOT, "webapp", "agents", "graph.py")
        with open(graph_path, encoding="utf-8") as fh:
            src = fh.read()
        body = src[
            src.index("def _invoke_code_writer("):src.index("def _invoke_code_tester(")
        ]
        assert "_writer_codefix_cycle.set(" in body
        assert "_writer_codefix_cycle.reset(" in body
        deriv_line = next(
            ln for ln in body.splitlines() if ln.strip().startswith("_w24_codefix =")
        )
        assert "_w24_fix" in deriv_line and "_eow_active" in deriv_line, (
            "the guard needs the full triple: remediation present AND the "
            "edit-over-write base (same template + parseable draft on disk)"
        )

    def test_write_file_schema_accepts_full_rewrite_reason(self):
        """The escape hatch rides the tool schema — without the param, a
        model-supplied ``full_rewrite_reason`` would die in pydantic
        validation before the wrapper ever saw it."""
        path = os.path.join(ROOT, "webapp", "agents", "tools", "filesystem_tools.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        block = src[src.index("def write_file("):src.index("def edit_file(")]
        assert "full_rewrite_reason" in block
