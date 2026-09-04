"""[wave-19 T1.2] Identity escalation at the probe's death point.

Job 324 (myhouse): the Phase-1 probe zeroed on a throttled direct egress IP
and the zero-yield gate killed the job — without ever trying the experiment
a different network identity represents. The shipped tier ladder
(``_escalate_tier_axis``) already encodes the semantics (never downgrade,
skip unconfigured tiers, no-op at residential); the probe now reuses it ONCE
on a dead yield, staging the escalated identity through the recipe so
``_stealth_env`` carries it (T0.2's plumbing).

Honesty contract:
- the escalation attempt is recorded on the yield's coverage
  (``tier_escalation``);
- dead at BOTH identities ⇒ ``stop_reason="all_tiers_blocked"`` — a FAIL-class
  reason the coverage gates recognize;
- residential (or no configured higher tier) ⇒ no re-run at all.
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

from agents import graph  # noqa: E402

SLUG = "myhouse-com-au"
JOB_URL = "https://www.myhouse.com.au/products/arcosteel-popcorn-maker-black"
LISTING = "https://www.myhouse.com.au/collections/sale-clearance"


def _dead_yield():
    return {
        "discovered_urls": 0,
        "stop_reason": "empty_first_page",
        "coverage": {"stop_reason": "empty_first_page", "discovered_urls": 0},
    }


def _alive_yield():
    return {
        "discovered_urls": 40,
        "stop_reason": "no_next_link",
        "coverage": {"stop_reason": "no_next_link", "discovered_urls": 40},
    }


@pytest.fixture
def tiers_configured(monkeypatch):
    """Every named tier counts as configured (real env has no proxies)."""
    monkeypatch.setattr(graph, "_tier_configured", lambda tier: tier == "none" or bool(tier))


class TestStateAtNextTier:
    def test_no_recipe_escalates_from_none(self, tiers_configured):
        state = {"url": JOB_URL, "input_mode": "list_page", "search_criteria": LISTING}
        new_state, nxt, prev = graph._state_at_next_tier(state)
        assert new_state is not None
        assert prev == "none"
        assert nxt == "datacenter"
        recipe = new_state["scraper_analysis"]["access_recipe"]
        assert recipe["proxy_tier"] == "datacenter"
        assert recipe["discovery_proxy_tier"] == "datacenter"
        # the ORIGINAL state is untouched (probe re-runs must not leak tiers)
        assert "scraper_analysis" not in state

    def test_datacenter_recipe_goes_residential(self, tiers_configured):
        state = {
            "url": JOB_URL,
            "scraper_analysis": {
                "access_recipe": {"proxy_tier": "datacenter", "stealth": "cloak"},
            },
        }
        _, nxt, prev = graph._state_at_next_tier(state)
        assert (prev, nxt) == ("datacenter", "residential")

    def test_residential_recipe_is_ladder_end(self, tiers_configured):
        state = {
            "url": JOB_URL,
            "scraper_analysis": {"access_recipe": {"proxy_tier": "residential"}},
        }
        assert graph._state_at_next_tier(state) == (None, "", "residential")

    def test_unconfigured_tiers_are_skipped(self, monkeypatch):
        # datacenter NOT configured → the ladder skips straight to residential
        monkeypatch.setattr(
            graph, "_tier_configured", lambda tier: tier in ("none", "residential")
        )
        state = {"url": JOB_URL}
        new_state, nxt, prev = graph._state_at_next_tier(state)
        assert nxt == "residential"
        assert new_state["scraper_analysis"]["access_recipe"]["proxy_tier"] == "residential"


class TestProbeIdentityEscalation:
    def _patch_once(self, monkeypatch, results):
        """Script _probe_phase1_discovery_once results in call order."""
        calls = []

        def _once(slug, state, job_id, listing_override=""):
            calls.append({"listing": listing_override, "state": state})
            return results.pop(0)

        monkeypatch.setattr(graph, "_probe_phase1_discovery_once", _once)
        # no listing retry in these scenarios
        monkeypatch.setattr(graph, "_probe_retry_warranted", lambda state, y: False)
        return calls

    def test_dead_yield_re_runs_once_at_next_tier(self, monkeypatch, tiers_configured):
        calls = self._patch_once(
            monkeypatch, [(False, None, _dead_yield()), (False, None, _alive_yield())]
        )
        state = {"url": JOB_URL, "input_mode": "list_page", "search_criteria": LISTING}
        crashed, tb, y = graph._probe_phase1_discovery(SLUG, state, 0)
        assert len(calls) == 2, "exactly ONE escalation re-run"
        # the re-run's state carries the escalated identity
        recipe = calls[1]["state"]["scraper_analysis"]["access_recipe"]
        assert recipe["proxy_tier"] == "datacenter"
        assert y is not None and y["discovered_urls"] == 40

    def test_dead_at_both_tiers_sets_all_tiers_blocked(self, monkeypatch, tiers_configured):
        self._patch_once(
            monkeypatch, [(False, None, _dead_yield()), (False, None, _dead_yield())]
        )
        state = {"url": JOB_URL, "input_mode": "list_page", "search_criteria": LISTING}
        crashed, tb, y = graph._probe_phase1_discovery(SLUG, state, 0)
        assert not crashed
        assert y["stop_reason"] == "all_tiers_blocked"
        esc = y["coverage"]["tier_escalation"]
        assert esc["attempted"] is True
        assert esc["tier"] == "datacenter"
        assert esc["still_dead"] is True

    def test_escalated_crash_keeps_original_dead_evidence(self, monkeypatch, tiers_configured):
        """A crash at the higher tier is the same code — the honest zero from
        the first run stands, annotated, instead of a traceback retry loop."""
        calls = self._patch_once(
            monkeypatch, [(False, None, _dead_yield()), (True, "Traceback...", None)]
        )
        state = {"url": JOB_URL, "input_mode": "list_page", "search_criteria": LISTING}
        crashed, tb, y = graph._probe_phase1_discovery(SLUG, state, 0)
        assert crashed is False, "an escalated-run crash must not become the verdict"
        assert y["discovered_urls"] == 0
        assert y["coverage"]["tier_escalation"]["attempted"] is True
        assert y["coverage"]["tier_escalation"]["crashed"] is True

    def test_residential_recipe_never_re_runs(self, monkeypatch, tiers_configured):
        calls = self._patch_once(monkeypatch, [(False, None, _dead_yield())])
        state = {
            "url": JOB_URL,
            "input_mode": "list_page",
            "search_criteria": LISTING,
            "scraper_analysis": {"access_recipe": {"proxy_tier": "residential"}},
        }
        graph._probe_phase1_discovery(SLUG, state, 0)
        assert len(calls) == 1, "no escalation past residential"

    def test_listing_retry_then_escalation(self, monkeypatch, tiers_configured):
        """The full 324 chain: primary dead → navigator listing dead → tier."""
        calls = []

        def _once(slug, state, job_id, listing_override=""):
            calls.append({"listing": listing_override, "state": state})
            return False, None, _dead_yield()

        monkeypatch.setattr(graph, "_probe_phase1_discovery_once", _once)
        monkeypatch.setattr(
            graph,
            "_probe_listing_candidates",
            lambda state: (JOB_URL, LISTING),
        )
        monkeypatch.setattr(graph, "_probe_retry_warranted", lambda state, y: True)
        state = {"url": JOB_URL, "input_mode": "list_page", "search_criteria": LISTING}
        graph._probe_phase1_discovery(SLUG, state, 0)
        assert len(calls) == 3
        assert calls[1]["listing"] == LISTING, "second run rides the navigator listing"
        assert calls[2]["listing"] == LISTING, "escalation keeps the same listing"
        recipe = calls[2]["state"]["scraper_analysis"]["access_recipe"]
        assert recipe["proxy_tier"] == "datacenter"


class TestAllTiersBlockedIsFailClass:
    def test_in_coverage_fail_stop_reasons(self):
        from agents.nodes.route_after_testing import _COVERAGE_FAIL_STOP_REASONS

        assert "all_tiers_blocked" in _COVERAGE_FAIL_STOP_REASONS
