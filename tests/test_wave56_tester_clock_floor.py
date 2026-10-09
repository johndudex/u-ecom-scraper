"""[wave-56 F6] After a writer wall-clock death, the next writer window may
not spend below the tester's floor.

Job 575 (revolve, 2026-10-09): three writer abandonments (3x2700s) drained
the task budget, the tester's 1800s request clamped to 887s, and its FIRST
LLM turn never returned — no test_report.json, no diagnostics, nothing for
any repair machinery to read. The honest fast-fail recorded the death, but
the spend that starved the tester was the writer's.

The generic phase clamp (``_effective_timeout``) can only divide what is
left; it cannot reserve. The reservation lives at the writer's own window:
once ``writer_wall_clock_timeouts`` >= 1 (a death with a usable draft, which
falls through to testing), the writer's NEXT window is capped at remaining
budget minus ``TESTER_WINDOW_FLOOR_S`` — the tester always has a viable
window to judge whatever draft exists. No deadline (local runs, tests) and
no prior deaths leave the base window byte-identical.
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

import pytest  # noqa: E402

from agents.graph import TESTER_WINDOW_FLOOR_S, _writer_invoke_timeout, _writer_post_death_timeout


def _cfg(deadline_in: float) -> dict:
    return {"configurable": {"task_deadline": time.time() + deadline_in}}


class TestTesterClockFloor:
    def test_no_deaths_base_window_unchanged(self):
        state = {"writer_wall_clock_timeouts": 0}
        assert (
            _writer_post_death_timeout(state, _cfg(10_000))
            == _writer_invoke_timeout()
        )

    def test_no_deadline_base_window_unchanged(self):
        state = {"writer_wall_clock_timeouts": 2}
        assert _writer_post_death_timeout(state, {}) == _writer_invoke_timeout()

    def test_death_caps_window_to_leave_tester_floor(self):
        # 3000s of budget − 360s finalize margin − 900s tester floor = 1740s
        # the writer may spend; base (>=2700) must lose to the cap.
        state = {"writer_wall_clock_timeouts": 1}
        got = _writer_post_death_timeout(state, _cfg(3000))
        assert got <= 1740
        assert got > 0

    def test_generous_budget_keeps_base_window(self):
        state = {"writer_wall_clock_timeouts": 3}
        assert (
            _writer_post_death_timeout(state, _cfg(100_000))
            == _writer_invoke_timeout()
        )

    def test_budget_below_floor_does_not_manufacture_time(self):
        # Under the floor there is no viable tester window regardless — the
        # helper must not return a fake 0 (a literal 0 fires the join(0)
        # WallClockTimeout arm and counts a PHONY writer death); it returns
        # the base and the generic clamp owns the honest refusal.
        state = {"writer_wall_clock_timeouts": 1}
        got = _writer_post_death_timeout(state, _cfg(500))
        assert got == _writer_invoke_timeout()

    def test_floor_constant_is_a_viable_llm_window(self):
        # 887s was not enough for revolve's tester to return one turn. The
        # floor must exceed that measured death bound.
        assert TESTER_WINDOW_FLOOR_S >= 900
