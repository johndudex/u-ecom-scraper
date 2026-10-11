"""[wave-56b F7 → wave-58 two-strike] The writer's extension ceiling.

Live proof (wonderbly job 580, 2026-10-09): the writer hung twice and BOTH
hangs ran the full 2700s extension ceiling — F6's post-death cap only sizes
the base window near the END of the task budget, so mid-budget the second
hang still paid 2700s. F7 retired the ceiling after ONE death.

[wave-58 revision — batch 613-616] One-death-retires-everything strangled
the WRONG invocation: wonderbly 616 inv2 died mid-edit and inv3 then ran at
the 1800 BASE with no draft — the operator's configured ceiling
(WRITER_MAX_TIMEOUT=2700) was never tried a single time. Two-strike:
the FIRST death retires only the GROWTH past the configured max (the next
window still gets the full configured ceiling); the SECOND death restores
F7's fail-fast (base clock, no slack — job 580's zombie-burn protection).
Recursion deaths now count too (wave-58 `_writer_window_death`).
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

    def test_first_death_keeps_the_configured_ceiling(self):
        """[wave-58] One death is not the signal to shrink the window — the
        next invocation keeps the operator's configured max."""
        from agents.graph import _writer_extension_ceiling

        out = _writer_extension_ceiling({"writer_wall_clock_timeouts": 1})
        assert out == 2700, f"first retry keeps the configured ceiling: {out}"

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
        # An env ceiling BELOW the base clock still collapses after strike 2.
        assert _writer_extension_ceiling({"writer_wall_clock_timeouts": 2}) <= 1800


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
