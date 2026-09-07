"""[wave-24 W24-3] Access-wall ×2 early terminal — no writer cycles against walls.

Prod 393: three ~1800s writer turns against a migrating stop-reason signature
(``empty_first_page`` → ``all_tiers_blocked`` → ``navigate_throttled`` —
cycle-2's root cause was our OWN browser-service 429). No scraper code can
fix infra throttling, but the bounded-bounce arms kept sending access-class
failures back to code_writer on the full retry budget (~3h of honest-but-
avoidable burn per job; crocs-382 is the same class).

Two halves, tested separately (routing functions cannot mutate state):
- tester side: the zero-yield arm counts infra-class stops into
  ``access_wall_cycles`` via ``_access_wall_update``, with a one-reset
  escape on a concrete scraper-target diagnosis (395's late Algolia
  re-tier fix was real code work discovered only after the walls);
- router side: ≥2 counted cycles terminalize AHEAD of every code_writer
  bounce and ahead of the retest-exhausted→scraper conversion that was
  393's exact waste path; ground-truth rescues still win; an all-throttled
  wall (our own browser-service 429) parks instead of FAIL-cleaning.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django

django.setup()

import agents.graph as ag
# NOTE: agents.nodes re-exports the route_after_testing FUNCTION under the
# module's name — bind the module itself via sys.modules.
import sys as _sys

rat = _sys.modules["agents.nodes.route_after_testing"]


# Slug with no workspace artifacts in the container — keeps the router's
# CLI-contract re-check and count-regression reads inert for these tests.
SLUG = "wave24-access-wall-test"


def _zero_cov(stop_reason):
    return {
        "ran_phase1": True,
        "discovered_urls": 0,
        "found": 0,
        "stop_reason": stop_reason,
    }


def _wall_state(**over):
    """Zero-yield FAIL on a discovery-driven job — the 393 report shape."""
    state = {
        "job_id": 0,
        "site_slug": SLUG,
        "input_mode": "navigation",
        "test_retry_count": 0,
        "test_report": {
            "overall_assessment": "FAIL",
            "confidence_score": 0.1,
            "issues": [],
            "discovery_coverage": _zero_cov("empty_render"),
        },
    }
    state.update(over)
    return state


class TestAccessWallVocabulary:
    """The wall set is infra-only: code bugs and the gateway-down lane stay OUT."""

    def test_infra_stops_counted(self):
        assert {
            "navigate_throttled", "all_tiers_blocked", "empty_render",
            "empty_first_page", "navigate_error",
        } <= rat._ACCESS_WALL_STOP_REASONS

    def test_code_and_gateway_stops_excluded(self):
        for stop in (
            "navigate_unavailable",  # own park/resume lane — never FAIL it
            "dedup_flat",            # code bug wearing a FAIL label
            "phase1_skipped",        # code never looked
            "malformed_discovery_urls",
            "target_met",
        ):
            assert stop not in rat._ACCESS_WALL_STOP_REASONS

    def test_navigate_throttled_is_a_coverage_failure_for_direct_callers(self):
        """The _COVERAGE_FAIL_STOP_REASONS addition: the router's _cov_reason
        (ground-truth veto + anti-bot exemption) now sees throttling too."""
        reason = rat._discovery_coverage_failure(
            {"discovery_coverage": _zero_cov("navigate_throttled")}
        )
        assert reason and "navigate_throttled" in reason

    def test_throttle_still_classifies_retest_not_strategy(self):
        """classify_test_failure's throttle guard must keep precedence: a
        throttled run is UNPROVEN coverage (re-test the same draft), never a
        strategy verdict — the vocabulary addition must not change that."""
        action, _reason = rat.classify_test_failure(
            {
                "issues": [],
                "discovery_coverage": _zero_cov("navigate_throttled"),
            },
            "http_navigation",
        )
        assert action == "retest"


class TestTesterSideAccessWallAccounting:
    """_access_wall_update: the state fragment the tester's zero-yield arm
    merges (routing functions cannot mutate state, so the counter lives here)."""

    def _upd(self, state, stop, live_block=True, report=None):
        return ag._access_wall_update(state, stop, live_block, report)

    def test_first_throttled_stop_counts_and_flags_throttled(self):
        upd = self._upd({}, "navigate_throttled")
        assert upd["access_wall_cycles"] == 1
        assert upd["access_wall_all_throttled"] is True

    def test_second_non_throttled_stop_clears_all_throttled(self):
        upd = self._upd(
            {"access_wall_cycles": 1, "access_wall_all_throttled": True},
            "empty_render",
        )
        assert upd["access_wall_cycles"] == 2
        assert upd["access_wall_all_throttled"] is False

    def test_throttle_after_non_throttle_stays_mixed(self):
        upd = self._upd(
            {"access_wall_cycles": 1, "access_wall_all_throttled": False},
            "navigate_throttled",
        )
        assert upd["access_wall_cycles"] == 2
        assert upd["access_wall_all_throttled"] is False

    def test_gateway_and_code_stops_never_count(self):
        for stop in ("navigate_unavailable", "dedup_flat", "phase1_skipped",
                     "target_met", ""):
            assert self._upd({}, stop) == {}, stop

    def test_stashed_probe_zero_is_not_a_wall(self):
        """A zero stashed under stronger run evidence (W24-2) must not count —
        the run proved its discovery works."""
        assert self._upd({}, "empty_render", live_block=False) == {}

    def test_concrete_scraper_diagnosis_escapes_once(self):
        report = {"remediation": {"target": "scraper", "field": "product_url"}}
        upd = self._upd({"access_wall_cycles": 2}, "empty_render", report=report)
        assert upd["access_wall_cycles"] == 0  # reset — one real fix window
        assert upd["access_wall_escape_used"] == 1

    def test_diagnosis_via_issues_fallback_escapes(self):
        report = {
            "remediation": {"target": "scraper"},
            "issues": [{"message": "selector returns no anchors"}],
        }
        upd = self._upd({"access_wall_cycles": 1}, "empty_first_page", report=report)
        assert upd["access_wall_cycles"] == 0
        assert upd["access_wall_escape_used"] == 1

    def test_strategy_targeted_remediation_never_escapes(self):
        """393 cycle-2: ``target: "strategy"`` on a BS-429 diagnosis produced
        the second pointless writer cycle — it must not reset the counter."""
        report = {"remediation": {"target": "strategy", "fix": "re-tier"}}
        upd = self._upd({"access_wall_cycles": 1}, "navigate_throttled", report=report)
        assert upd["access_wall_cycles"] == 2
        assert "access_wall_escape_used" not in upd

    def test_bare_scraper_target_is_not_concrete(self):
        report = {"remediation": {"target": "scraper"}}
        upd = self._upd({"access_wall_cycles": 1}, "empty_render", report=report)
        assert upd["access_wall_cycles"] == 2

    def test_escape_is_capped_at_one(self):
        report = {"remediation": {"target": "scraper", "field": "price"}}
        upd = self._upd(
            {"access_wall_cycles": 2, "access_wall_escape_used": 1},
            "empty_render", report=report,
        )
        assert upd["access_wall_cycles"] == 3
        assert "access_wall_escape_used" not in upd


class TestRouterAccessWallTerminal:
    """≥2 counted wall cycles terminalize ahead of every code_writer bounce."""

    def test_two_wall_cycles_terminalize_to_cleanup(self):
        state = _wall_state(
            access_wall_cycles=2, access_wall_all_throttled=False,
            skip_approvals=True,
        )
        assert rat.route_after_testing(state) == "cleanup"

    def test_all_throttled_walls_park_not_fail(self):
        """Throttle-only walls are OUR browser-service 429s — park the job
        (queue slot preserved, beat-resumed) instead of an honest-FAIL."""
        state = _wall_state(
            access_wall_cycles=2, access_wall_all_throttled=True,
            skip_approvals=True,
            test_report={
                "overall_assessment": "FAIL",
                "confidence_score": 0.1,
                "issues": [],
                "discovery_coverage": _zero_cov("navigate_throttled"),
            },
        )
        assert rat.route_after_testing(state) == "park_browser_unavailable"

    def test_without_skip_approvals_escalates_to_human(self):
        state = _wall_state(access_wall_cycles=2, access_wall_all_throttled=False)
        assert rat.route_after_testing(state) == "human_approval"

    def test_one_wall_cycle_still_climbs_the_ladder(self):
        """The terminal is ×2 — a single wall cycle keeps the normal ladder."""
        state = _wall_state(access_wall_cycles=1, access_wall_all_throttled=True)
        dest = rat.route_after_testing(state)
        assert dest not in ("cleanup", "human_approval", "park_browser_unavailable")

    def test_rescue_wins_ahead_of_the_terminal(self, monkeypatch):
        """Real items on disk beat the wall (ground truth, same as siblings)."""
        monkeypatch.setattr(rat, "_scraper_has_real_items", lambda *a, **k: True)
        state = _wall_state(
            access_wall_cycles=2, access_wall_all_throttled=False,
            skip_approvals=True,
        )
        assert rat.route_after_testing(state) == "field_confirmation"


class TestWiringContracts:
    """Source contracts pinning where the arms live (regression anchor)."""

    def _router_src(self):
        with open(
            os.path.join(ROOT, "webapp", "agents", "nodes", "route_after_testing.py"),
            encoding="utf-8",
        ) as fh:
            return fh.read()

    def test_terminal_sits_after_override_before_volume_gap(self):
        src = self._router_src()
        body = src[src.index("GROUND-TRUTH OVERRIDE"):]
        terminal = body.index("access_wall_cycles")
        volume = body.index("# T2.1 volume-gap bounce")
        assert terminal < volume, "×2 arm must precede the volume-gap bounce"
        override_return = body.index('return "field_confirmation"')
        assert override_return < terminal, "×2 arm must follow the override block"

    def test_terminal_wrapped_in_sibling_terminal_checks(self):
        src = self._router_src()
        arm = src[src.index("Access-wall ×2 early terminal"):]
        chunk = arm[:arm.index("if _volume_reason") if "if _volume_reason" in arm else 4000]
        assert "_terminal_after_grace_check" in chunk
        assert "_terminal_after_retest_check" in chunk
        assert "park_browser_unavailable" in chunk

    def test_tester_arm_wires_the_accounting_helper(self):
        with open(os.path.join(ROOT, "webapp", "agents", "graph.py"), encoding="utf-8") as fh:
            src = fh.read()
        body = src[src.index("def _invoke_code_tester("):src.index("def _invoke_cleanup(")]
        assert "_access_wall_update(" in body
        # The stop must be counted only when the probe's zero IS the live block:
        assert "_live_cov is _zcov" in body
