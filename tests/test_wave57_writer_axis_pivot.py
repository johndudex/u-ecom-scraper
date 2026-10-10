"""[wave-57 class 2] The identical-draft death demands a strategy-axis try.

jomashop #597 (a proven passer) died "code_writer produced an identical
draft twice after failed tests" with ZERO strategy-axis attempts: the
read-spiral abort arm (66 reads / 0 edits) force-set ``noop_fix_cycles=2``
and the terminal return fired BEFORE the wave-56 strike-1 ladder bounce was
reachable. nordstrom #581 and jjshouse #586 show the same verdict text.

Fix: when the terminal no-op arm is about to kill the job and a strategy
rung (or the tier axis) is still unspent, spend it — bounce to
scraper_analyzer with a writer-visible strategy-axis directive — and die
only once a pivot was actually attempted (or no rung exists to manufacture).
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


def _state(strategy="http_requests", tried=None, **over):
    state = {
        "site_slug": "jomashop-com",
        "job_id": 597,
        "scraper_analysis": {"strategy": strategy},
        "strategies_tried": list(tried or []),
    }
    state.update(over)
    return state


class TestWriterAxisPivotDeclared:
    def test_writer_axis_pivot_count_is_declared(self):
        from agents.state import ScrapeState

        assert "writer_axis_pivot_count" in ScrapeState.__annotations__

    def test_pivot_key_spelled_in_state_source(self):
        src = open(
            os.path.join(ROOT, "webapp", "agents", "state.py")
        ).read()
        assert "    writer_axis_pivot_count:" in src


class TestWriterAxisPivot:
    def test_strike2_with_rung_available_pivots(self):
        """The #597 shape: strike 2 (spiral-abort force-2), http_requests
        current, nothing tried — the pivot must spend the rung, not die."""
        from agents.graph import _writer_axis_pivot

        out = _writer_axis_pivot(
            _state("http_requests"), {"noop_fix_cycles": 2}
        )
        assert out is not None
        (entry,) = out["strategies_tried"]
        assert entry["strategy"] == "http_requests"
        assert out["writer_axis_pivot_count"] == 1

    def test_pivot_carries_writer_directive(self):
        from agents.graph import _writer_axis_pivot

        out = _writer_axis_pivot(
            _state("http_requests"), {"noop_fix_cycles": 2}
        )
        directive = out.get("feedback_for_writer") or ""
        assert "strategy" in directive.lower()
        assert "http_navigation" in directive  # the concrete next rung

    def test_pivot_spent_never_pivots_again(self):
        from agents.graph import _writer_axis_pivot

        assert (
            _writer_axis_pivot(
                # state carries the spent pivot from the prior cycle
                _state("http_requests", writer_axis_pivot_count=1),
                {"noop_fix_cycles": 2},
            )
            is None
        )

    def test_exhausted_both_axes_returns_none(self):
        """http_navigation current, everything tried, residential tier
        already spent — no rung exists, the honest death stands."""
        from agents.graph import _writer_axis_pivot

        out = _writer_axis_pivot(
            _state(
                "http_navigation",
                tried=[
                    {"strategy": "http_requests"},
                    {"strategy": "playwright"},
                ],
                scraper_analysis={
                    "strategy": "http_navigation",
                    "proxy_tier": "residential",
                },
            ),
            {"noop_fix_cycles": 2},
        )
        assert out is None

    def test_unknown_strategy_token_never_pivots(self):
        from agents.graph import _writer_axis_pivot

        assert (
            _writer_axis_pivot(
                _state("http_navigation+residential_proxy"),
                {"noop_fix_cycles": 2},
            )
            is None
        )

    def test_step_death_arm_pivots_too(self):
        """The terminal arm is shared by the step-death and exhausted-budget
        shapes — a rung available means a pivot, whatever brought us here."""
        from agents.graph import _writer_axis_pivot

        out = _writer_axis_pivot(
            _state("http_requests", tried=[{"strategy": "http_navigation"}]),
            {"noop_fix_cycles": 1},
        )
        assert out is not None
        assert out["writer_axis_pivot_count"] == 1

    def test_strike1_bounce_unchanged(self):
        """The wave-56 F2 pin: strike 1 still bounces with the same shape."""
        from agents.graph import _noop_strike1_ladder_bounce

        out = _noop_strike1_ladder_bounce(
            _state("http_requests"), {"noop_fix_cycles": 1}
        )
        assert out is not None
        (entry,) = out["strategies_tried"]

    def test_strike1_bounce_carries_directive(self):
        """[wave-57] the strike-1 bounce now also tells the writer the axis
        changed — today it bounces silently and the writer re-reads."""
        from agents.graph import _noop_strike1_ladder_bounce

        out = _noop_strike1_ladder_bounce(
            _state("http_requests"), {"noop_fix_cycles": 1}
        )
        assert out is not None
        assert "feedback_for_writer" in out

    def test_strike1_bounce_never_spends_the_pivot_ledger(self):
        """Critique fold: the strike-1 ladder depends on the shared escape
        body NOT consuming the pivot budget — one strike-1 bounce that set
        writer_axis_pivot_count would collapse the ladder to a single rung
        (a pass-rate regression on the jomashop class)."""
        from agents.graph import _noop_strike1_ladder_bounce

        out = _noop_strike1_ladder_bounce(
            _state("http_requests"), {"noop_fix_cycles": 1}
        )
        assert out is not None
        assert "writer_axis_pivot_count" not in out

    def test_tier_pivot_directive_names_the_tier(self):
        """The 580 shape: strategy ladder exhausted, tier axis is what fires
        — the directive must say TIER (the death note never claims a
        strategy change that did not happen)."""
        from agents.graph import _writer_axis_pivot

        out = _writer_axis_pivot(
            _state(
                "http_requests",
                tried=[
                    {"strategy": "http_navigation"},
                    {"strategy": "playwright"},
                ],
            ),
            {"noop_fix_cycles": 2},
        )
        assert out is not None
        assert "proxy TIER" in (out.get("feedback_for_writer") or "")
        assert out["writer_axis_pivot_count"] == 1


def test_terminal_arm_pivots_before_dying():
    """Wiring pin: the escalation arm in _invoke_code_writer must consult
    the pivot BEFORE the terminal cleanup return (the #597 hole — the abort
    returned before the strike-1 bounce line was reachable)."""
    src = open(
        os.path.join(ROOT, "webapp", "agents", "graph.py")
    ).read()
    arm = src[src.index("def _invoke_code_writer"):]
    pivot_at = arm.index("_writer_axis_pivot(state, update)")
    terminal_at = arm.rindex('goto="cleanup"')
    assert pivot_at < terminal_at
