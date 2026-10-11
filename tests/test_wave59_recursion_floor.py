"""[wave-59 C5] The writer's recursion floor stays past the max wall.

Wonderbly 625 died on ``RecursionError`` BELOW its wall clock at limit 452:
the R2 window restoral (1200/1200 → 1800/2700) buys ~120 more turns at the
measured 15 s/turn floor than the 1200s pin did, so the step counter bound
below the wall clock it was never meant to outrun — again. 200 rounds
(limit 602 ≈ 3070s at the 15 s/turn floor) keeps GraphRecursionError past
the 2700s MAX window any operator can configure: the wall clock is the
backstop, the recursion floor is theoretical.

Scope note (critic-2, plan v3.2): reclassification of fast-ceiling deaths
was DROPPED — a recursion-class death stays recursion-class (S3's wall-only
T12 grant is untouched). This change is the counter and nothing else.
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

import pytest  # noqa: E402

from agents.graph import AGENT_RECURSION_MAP, _WRITER_ROUNDS  # noqa: E402

# The wall clock's maximum turn capacity at the measured floor: the 2700s
# operator max window / 15 s per turn.
_WALL_MAX_TURNS = 2700 // 15


class TestRecursionFloor:
    def test_rounds_raised_to_200(self):
        assert _WRITER_ROUNDS == 200, (
            "625 died on RecursionError below its wall at 150 rounds — the "
            "floor must clear every configurable window"
        )

    def test_limit_formula_and_value(self):
        assert AGENT_RECURSION_MAP["code_writer"] == _WRITER_ROUNDS * 3 + 2
        assert AGENT_RECURSION_MAP["code_writer"] == 602

    def test_floor_past_the_max_wall_turn_capacity(self):
        """The invariant the number exists for: at the fastest measured turn
        rate the 2700s max wall runs out long before the step counter does —
        with margin (the wall binds first, always)."""
        assert AGENT_RECURSION_MAP["code_writer"] > _WALL_MAX_TURNS * 2

    def test_writer_only_raise(self):
        """No other phase's budget moves (the wave-58 scope pin, restated)."""
        assert AGENT_RECURSION_MAP["code_tester"] == 120
        assert AGENT_RECURSION_MAP["site_analyzer"] == 250


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
