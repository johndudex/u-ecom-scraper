"""[wave-22 A6] The writer wall-clock counter must count EVERY wall-clock
death — including the ones that never produced a draft.

The S-4 counter ``writer_wall_clock_timeouts`` was only incremented in the
"dead invocation BUT a draft exists" arm of ``_invoke_code_writer``. A
wall-clock death with NO draft fell into the no-draft arm, which counts the
dedicated ``code_writer_error_count`` — so a job that burned three writer
windows against nothing reported ``writer_wall_clock_timeouts=0`` forever and
the step-budget telemetry under-counted the most expensive failure class
(the job-8 RCA's budget-observability family: step tables that lie).

Contract:
- in the no-draft arm, a wall-clock timeout increments
  ``writer_wall_clock_timeouts`` exactly like the draft-exists arm does;
- the counter still RESETS to 0 on a healthy writer return (existing
  behavior, unchanged);
- no routing change: the no-draft arm keeps its own escalation
  (``code_writer_error_count``) — this is observability, not a new gate.
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


class TestNoDraftWallClockCounting:
    def _writer_src(self) -> str:
        with open(os.path.join(ROOT, "webapp", "agents", "graph.py")) as fh:
            return fh.read()

    def _no_draft_arm(self) -> str:
        """The ``if not _draft_ok:`` arm of _invoke_code_writer (up to the
        draft-exists wall-clock arm that already counts). The arm itself may
        contain the same wall-clock signature line (A6), so the boundary is
        the SECOND occurrence of the signature after the arm opens."""
        src = self._writer_src()
        i_arm = src.index("if not _draft_ok:")
        i_first = src.index('if "wall-clock timeout" in _cw_err', i_arm)
        try:
            i_after = src.index('if "wall-clock timeout" in _cw_err', i_first + 1)
        except ValueError:
            i_after = i_first  # A6 not implemented yet — RED boundary
        return src[i_arm:i_after]

    def test_no_draft_arm_counts_wall_clock_timeouts(self):
        arm = self._no_draft_arm()
        assert "writer_wall_clock_timeouts" in arm, (
            "the no-draft arm never increments writer_wall_clock_timeouts — "
            "a wall-clock death with no draft is invisible to the budget "
            "telemetry (job-8 RCA: step tables that lie)"
        )

    def test_no_draft_arm_gates_the_count_on_the_timeout_signature(self):
        """The increment must key on the same wall-clock signature the
        draft-exists arm uses — not on every no-draft failure."""
        arm = self._no_draft_arm()
        assert 'in _cw_err' in arm or "wall-clock timeout" in arm, (
            "no-draft arm counts writer_wall_clock_timeouts unconditionally"
        )

    def test_healthy_return_still_resets(self):
        src = self._writer_src()
        i_count = src.index('update["writer_wall_clock_timeouts"] = 0')
        # the reset lives in the healthy-return update, after both arms
        assert i_count > src.index("if not _draft_ok:")


class TestStateContractUnchanged:
    def test_state_declares_the_counter(self):
        state_mod = importlib.import_module("agents.state")
        assert hasattr(state_mod.ScrapeState, "__annotations__")
        assert "writer_wall_clock_timeouts" in state_mod.ScrapeState.__annotations__


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
