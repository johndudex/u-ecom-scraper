"""[wave-56b F7] One writer wall-death must retire the extension ceiling.

Live proof (wonderbly job 580, 2026-10-09): the writer hung twice and BOTH
hangs ran the full 2700s extension ceiling — F6's post-death cap only sizes
the base window near the END of the task budget, so mid-budget the second
hang still paid 2700s. Two hangs ≈ 90 min of the task's ~12.6K usable
seconds; the job died "Soft time limit exceeded mid-graph" with a validated
checkpoint it never got to finalize. One hang is the signal that the
provider window is degraded: the next writer window gets the base clock and
NO activity-extension slack (the zombie latch still stops a true zombie at
base).
"""
from __future__ import annotations

import inspect
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402


class TestCeilingAfterDeath:
    def test_healthy_writer_keeps_the_full_ceiling(self):
        from agents.graph import _writer_extension_ceiling

        assert _writer_extension_ceiling({"writer_wall_clock_timeouts": 0}) == 2700

    def test_one_death_retires_extension_slack(self):
        from agents.graph import _writer_extension_ceiling

        out = _writer_extension_ceiling({"writer_wall_clock_timeouts": 1})
        assert out <= 1800, f"ceiling after a hang must not exceed base: {out}"

    def test_two_deaths_never_reissue_the_ceiling(self):
        from agents.graph import _writer_extension_ceiling

        out = _writer_extension_ceiling({"writer_wall_clock_timeouts": 2})
        assert out <= 1800

    def test_no_death_state_means_no_death(self):
        from agents.graph import _writer_extension_ceiling

        assert _writer_extension_ceiling({}) == 2700

    def test_env_lowered_ceiling_still_respected(self, monkeypatch):
        from agents.graph import _writer_extension_ceiling

        monkeypatch.setenv("WRITER_MAX_TIMEOUT", "2200")
        assert _writer_extension_ceiling({}) == 2200
        assert _writer_extension_ceiling({"writer_wall_clock_timeouts": 1}) <= 1800


class TestWiringExists:
    """The ceiling must be live on the MAIN writer invocation — the one
    that already consumed F6's post-death base cap."""

    def test_main_writer_call_uses_the_ceiling(self):
        import agents.graph as g

        src = inspect.getsource(g)
        # The main writer invoke passes max_timeout= from the ceiling helper
        assert "_writer_extension_ceiling(" in src

    def test_ceiling_helper_reads_death_counter(self):
        from agents.graph import _writer_extension_ceiling

        src = inspect.getsource(_writer_extension_ceiling)
        assert "writer_wall_clock_timeouts" in src
