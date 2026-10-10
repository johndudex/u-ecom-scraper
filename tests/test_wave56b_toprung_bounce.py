"""[wave-56b F2b] Strike 1 must spend a rung at the TOP of the strategy
ladder too — the proxy-tier axis is the rung.

Deep-audit proof (jobs 580 AND 581, 2026-10-09): from ``playwright``,
``_escalate_strategy`` always returns a goto (``internal_api`` is skipped),
so the wave-56 bounce returned None and both jobs re-tested byte-identical
drafts — the exact behavior F2 existed to kill, on both live no-op deaths.
From the top strategy rung the only experiment that can still differ is the
proxy tier (``_PROXY_TIER_LADDER``), so the bounce must record it and hand
scraper_analyzer an escalated analysis instead of declining.
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


def _state(strategy="playwright", tried=None, tier="none", **over):
    state = {
        "site_slug": "test-site",
        "job_id": 1,
        "scraper_analysis": {"strategy": strategy, "proxy_tier": tier},
        "strategies_tried": list(tried or []),
    }
    state.update(over)
    return state


@pytest.fixture
def tiers_configured(monkeypatch):
    """Unit scope: every tier is provisioned (real proxy config is
    orthogonal to the bounce contract)."""
    import agents.graph as g

    monkeypatch.setattr(g, "_tier_configured", lambda t: True)


class TestTopRungBounce:
    def test_playwright_strike1_bounces_to_a_higher_tier(self, tiers_configured):
        from agents.graph import _noop_strike1_ladder_bounce

        out = _noop_strike1_ladder_bounce(
            _state("playwright", [{"strategy": "http_navigation"}]),
            {"noop_fix_cycles": 1},
        )
        assert out is not None, (
            "top-rung strike 1 must spend the tier axis, not decline"
        )
        assert out["scraper_analysis"]["proxy_tier"] == "datacenter"
        assert out["strategies_tried"][0]["strategy"] == "playwright"
        assert "tier" in out["strategies_tried"][0]["reason"]

    def test_tier_ladder_climbs_to_residential_then_stops(self, tiers_configured):
        from agents.graph import _noop_strike1_ladder_bounce

        out = _noop_strike1_ladder_bounce(
            _state("playwright", [{"strategy": "http_navigation"}], tier="datacenter"),
            {"noop_fix_cycles": 1},
        )
        assert out is not None
        assert out["scraper_analysis"]["proxy_tier"] == "residential"
        out = _noop_strike1_ladder_bounce(
            _state("playwright", [{"strategy": "http_navigation"}], tier="residential"),
            {"noop_fix_cycles": 1},
        )
        assert out is None, "exhausted ladder: strike 2 owns the exit"

    def test_disabled_two_dim_ladder_declines(self, monkeypatch):
        from agents.graph import _noop_strike1_ladder_bounce

        monkeypatch.setattr(
            "os.environ", {**os.environ, "SCRAPER_TWO_DIM_LADDER": "0"}
        )
        out = _noop_strike1_ladder_bounce(
            _state("playwright", [{"strategy": "http_navigation"}]),
            {"noop_fix_cycles": 1},
        )
        assert out is None

    def test_lower_rungs_still_move_strategy_not_tier(self, tiers_configured):
        from agents.graph import _noop_strike1_ladder_bounce

        out = _noop_strike1_ladder_bounce(
            _state("http_requests", []),
            {"noop_fix_cycles": 1},
        )
        assert out is not None
        assert "scraper_analysis" not in out, (
            "a strategy move needs no tier mutation"
        )
        assert out["strategies_tried"][0]["strategy"] == "http_requests"

    def test_strike2_still_declines_everywhere(self, tiers_configured):
        from agents.graph import _noop_strike1_ladder_bounce

        assert _noop_strike1_ladder_bounce(
            _state("playwright"), {"noop_fix_cycles": 2}
        ) is None
        assert _noop_strike1_ladder_bounce(
            _state("http_requests"), {"noop_fix_cycles": 2}
        ) is None

    def test_never_into_internal_api(self, tiers_configured):
        from agents.graph import _noop_strike1_ladder_bounce

        out = _noop_strike1_ladder_bounce(
            _state("playwright", [{"strategy": "http_navigation"}]),
            {"noop_fix_cycles": 1},
        )
        assert out is not None
        assert out["scraper_analysis"].get("strategy") == "playwright", (
            "the tier axis must not quietly swap the strategy token"
        )

    def test_unknown_strategy_token_still_declines(self, tiers_configured):
        from agents.graph import _noop_strike1_ladder_bounce

        assert _noop_strike1_ladder_bounce(
            _state("http_navigation+residential_proxy"),
            {"noop_fix_cycles": 1},
        ) is None

    def test_no_caller_state_mutation(self, tiers_configured):
        from agents.graph import _noop_strike1_ladder_bounce

        s = _state("playwright", [{"strategy": "http_navigation"}])
        _noop_strike1_ladder_bounce(s, {"noop_fix_cycles": 1})
        assert s["scraper_analysis"]["proxy_tier"] == "none"
        assert s["strategies_tried"] == [{"strategy": "http_navigation"}]


class TestExhaustedByHistory:
    """Guardian checkpoint 1-B, demand on the record: job 580 no-op'd at
    strike 1 on **http_navigation** — mid-ladder, the same strategy token
    that bounced live on 582 — and took the legacy fall-through. The
    discriminator cannot be rung position; ``_escalate_strategy`` returns a
    goto on ladder EXHAUSTION GIVEN ``strategies_tried``, so a restored /
    pre-populated history that has already walked to the current rung must
    spend the TIER AXIS at strike 1, exactly like the top rung."""

    def test_mid_ladder_with_full_history_spends_the_tier_axis(
        self, tiers_configured
    ):
        from agents.graph import _noop_strike1_ladder_bounce

        out = _noop_strike1_ladder_bounce(
            _state(
                "http_navigation",
                [
                    {"strategy": "http_requests", "reason": "initial"},
                    {"strategy": "http_navigation", "reason": "escalation"},
                ],
            ),
            {"noop_fix_cycles": 1},
        )
        assert out is not None, (
            "exhausted-by-history strike 1 must spend the tier axis, not "
            "re-test identical bytes (the 580 death)"
        )
        assert out["scraper_analysis"]["proxy_tier"] == "datacenter"

    def test_history_including_higher_rungs_also_spends_tier(
        self, tiers_configured
    ):
        from agents.graph import _noop_strike1_ladder_bounce

        out = _noop_strike1_ladder_bounce(
            _state(
                "http_navigation",
                [
                    {"strategy": "http_requests"},
                    {"strategy": "http_navigation"},
                    {"strategy": "playwright"},
                ],
            ),
            {"noop_fix_cycles": 1},
        )
        assert out is not None
        assert out["scraper_analysis"]["proxy_tier"] == "datacenter"
        assert out["scraper_analysis"]["strategy"] == "http_navigation"

    def test_clean_history_still_moves_strategy_not_tier(
        self, tiers_configured
    ):
        from agents.graph import _noop_strike1_ladder_bounce

        out = _noop_strike1_ladder_bounce(
            _state("http_navigation", [{"strategy": "http_requests"}]),
            {"noop_fix_cycles": 1},
        )
        assert out is not None
        assert "scraper_analysis" not in out or (
            out["scraper_analysis"].get("proxy_tier") == "none"
        )
