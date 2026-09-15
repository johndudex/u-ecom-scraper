"""[wave-32 D1+D2] Writer budget in rounds + tool deadline sees the cap.

D1: ``AGENT_RECURSION_MAP["code_writer"]`` was 120 raw recursions — but
langgraph's prebuilt loop burns ~3 super-steps per tool round, so that was
only ~40 rounds while the wall clock allowed 1800s+ (job 587: writer died
with rounds left ON THE WALL, starved on steps). The recursion count now
DERIVES from a named round budget.

D2: the tool deadline (``set_tool_deadline``) was stamped with the BASE
window even for the extendable writer — so ``run_scraper``'s honesty guard
refused browser verification inside the 1800→2700s zone the activity-aware
join would happily wait through. Extendable phases now publish the
extension CAP; the finisher window (``allow_activity_extension=False``)
keeps the base so its disarm latch stays sharp.
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import webapp.agents.graph as g  # noqa: E402
from webapp.agents.tools.context import (  # noqa: E402
    clear_tool_context,
)


class _Fast:
    def invoke(self, *a, **k):
        return {"messages": ["done"]}


class TestWriterRounds:
    def test_writer_recursion_derives_from_rounds(self):
        """The recursion count is DERIVED from a named round budget — the
        3-super-steps-per-round tax documented at the map, ~50 rounds."""
        assert g.AGENT_RECURSION_MAP["code_writer"] == g._WRITER_ROUNDS * 3 + 2
        assert g._WRITER_ROUNDS >= 50, (
            "the writer must get at least ~50 tool rounds (was ~40) — 587's "
            "writer died on steps while its wall clock had room"
        )


class TestToolDeadlineCap:
    def test_extendable_phase_tool_deadline_uses_cap(self, monkeypatch):
        monkeypatch.setattr(g, "_async_execution_enabled", lambda phase="": False)
        monkeypatch.setattr(g, "_writer_max_timeout", lambda: 2700)
        try:
            g._invoke_agent_with_timeout(
                _Fast(), [], {}, "code_writer", 0, timeout=5
            )
            left = g.get_tool_deadline() - time.time()
            assert left > 2000, (
                "an extendable writer must publish the extension CAP as the "
                "tool deadline (browser verification must be allowed in the "
                "1800→2700s zone), not the base window"
            )
        finally:
            clear_tool_context()

    def test_non_extendable_phase_keeps_base_deadline(self, monkeypatch):
        monkeypatch.setattr(g, "_async_execution_enabled", lambda phase="": False)
        try:
            g._invoke_agent_with_timeout(
                _Fast(), [], {}, "code_tester", 0, timeout=5
            )
            left = g.get_tool_deadline() - time.time()
            assert left < 60, (
                "non-extendable phases keep the base window — no cap stamp"
            )
        finally:
            clear_tool_context()

    def test_finisher_window_not_stamped_with_cap(self, monkeypatch):
        """[wave-32 D2 gate mirror] allow_activity_extension=False (the
        W30-2 finisher) keeps the BASE tool deadline — a 2700s stamp it can
        never use would blunt the disarm latch in the salvage window."""
        monkeypatch.setattr(g, "_async_execution_enabled", lambda phase="": False)
        monkeypatch.setattr(g, "_writer_max_timeout", lambda: 2700)
        try:
            g._invoke_agent_with_timeout(
                _Fast(), [], {}, "code_writer", 0, timeout=5,
                allow_activity_extension=False,
            )
            left = g.get_tool_deadline() - time.time()
            assert left < 60, (
                "the finisher window must NOT inherit the extension cap as "
                "its tool deadline"
            )
        finally:
            clear_tool_context()

    def test_disarm_latch_still_fires(self, monkeypatch):
        """The cap stamp must not disturb the W24-1 disarm latch: an
        abandoned writer invocation still marks its invocation cancelled."""

        from webapp.agents.tools import context as tctx

        fired: list[tuple] = []
        real = tctx.mark_invocation_cancelled

        def spy(*a, **k):
            fired.append((a, k))
            return real(*a, **k)

        monkeypatch.setattr(tctx, "mark_invocation_cancelled", spy)

        class _Slow:
            def invoke(self, *a, **k):
                time.sleep(2.5)
                return {"messages": ["late"]}

        monkeypatch.setattr(g, "_async_execution_enabled", lambda phase="": False)
        try:
            g._invoke_agent_with_timeout(
                _Slow(), [], {}, "code_writer", 0, timeout=0.5,
                allow_activity_extension=True,
            )
            assert fired, (
                "abandonment after the extended window must still fire the "
                "disarm latch"
            )
        finally:
            clear_tool_context()


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
