"""[wave-61] Evidence-gated execution-seed demotion — the shared candidate
chain and the probe-verdict arming rules.

Job 653 (ultrabeauty): the tester validated the navigator's promotion
(20/20 PASS) while execution seeded discovery with the list_page JOB URL —
a PDP that can only ever yield 0. Four self-heals caught the zero; every
one was overridden by positive evidence from the OTHER candidate. The fix:

- ONE resolver (``listing_primary_chain`` / ``listing_fallback_chain`` /
  ``resolve_listing_candidate``) answers the candidate question for every
  consumer: execution's SCRAPER_LISTING_URL env gate, the tester's env
  injection, the probe's candidates, and the tester CLI hint.
- The Phase-1 probe stamps an attempt ledger; ``_arm_listing_probe_verdict``
  arms a demotion ONLY when the dead yield is attributed to the CURRENT
  primary and a fallback survived the ledger (a dead fallback vetoes — the
  653-inverse guard).
- Absent a verdict the chain is byte-for-byte the job-310 order (pinned).

Run: docker compose exec -T -w /app/webapp django python -m pytest ../tests/test_wave61_seed_demotion.py -q
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

django.setup()  # noqa: E402


PDP = "https://www.ultrabeauty.com/buy/153911/serum"
PROMO = "https://www.ultrabeauty.com/shop-online/5112/makeup"
CRITERIA = "https://www.ultrabeauty.com/shop-online/5112/makeup?sort=new"
CROSS = "https://www.loveamika.com/collections/all"

VERDICT_653 = {
    "verdict": "zero_yield",
    "candidate": PDP,
    "demotion_target": PROMO,
    "stop_reason": "empty_render",
    "attempts": [
        {"candidate": PDP, "crashed": False, "dead": True, "inconclusive": False},
        {"candidate": PROMO, "crashed": True, "dead": False, "inconclusive": False},
    ],
}


def _state(mode="list_page", url=PDP, criteria="", nav_listing=PROMO,
           verdict=None, **over):
    nav = {"discovery": {"listing_url": nav_listing}} if nav_listing else {}
    st = {"input_mode": mode, "url": url, "navigation_analysis": nav}
    if criteria:
        st["search_criteria"] = criteria
    if verdict is not None:
        st["listing_probe_verdict"] = verdict
    st.update(over)
    return st


# ─── the shared resolver ─────────────────────────────────────────────────────


class TestUnifiedChain:
    def test_no_verdict_is_the_job310_order(self):
        from webapp.agents.nodes.run_execution import listing_primary_chain

        chain = listing_primary_chain(_state(criteria=CRITERIA))
        assert [u for u, _s in chain] == [CRITERIA, PDP, PROMO]
        assert [s for _u, s in chain] == ["criteria", "job_url", "promotion"]

    def test_criteria_outranks_the_job_url(self):
        from webapp.agents.nodes.run_execution import resolve_listing_candidate

        primary, _alt, note = resolve_listing_candidate(_state(criteria=CRITERIA))
        assert primary == CRITERIA and note == "criteria"

    def test_653_replay_verdict_demotes_the_dead_primary(self):
        from webapp.agents.nodes.run_execution import listing_primary_chain

        chain = listing_primary_chain(_state(verdict=dict(VERDICT_653)))
        assert chain[0] == (PROMO, "promotion(demoted)")
        assert chain[1] == (PDP, "job_url")

    def test_verdict_for_a_foreign_candidate_demotes_nothing(self):
        from webapp.agents.nodes.run_execution import listing_primary_chain

        v = dict(VERDICT_653, candidate="https://www.ultrabeauty.com/other")
        chain = listing_primary_chain(_state(verdict=v))
        assert [u for u, _s in chain] == [PDP, PROMO]

    def test_navigation_mode_has_no_job_url_rung(self):
        from webapp.agents.nodes.run_execution import listing_primary_chain

        chain = listing_primary_chain(_state(mode="navigation", criteria=CRITERIA))
        assert [u for u, _s in chain] == [CRITERIA, PROMO]

    def test_url_list_and_search_term_have_no_chain(self):
        from webapp.agents.nodes.run_execution import resolve_listing_candidate

        assert resolve_listing_candidate(_state(mode="url_list")) == ("", "", "")
        assert resolve_listing_candidate(_state(mode="search_term")) == ("", "", "")

    def test_f17_drops_cross_domain_with_fall_through(self):
        from webapp.agents.nodes.run_execution import resolve_listing_candidate

        primary, _alt, note = resolve_listing_candidate(
            _state(criteria=CROSS, nav_listing=PROMO)
        )
        assert primary == PDP and note == "job_url"


class TestFallbackChain:
    def test_three_deep_promotion_criteria_job_url(self):
        from webapp.agents.nodes.run_execution import listing_fallback_chain

        # the FULL 3-deep survivor list — which survivor serves is the
        # consumer's call (resolve skips the chain head, the rescue skips
        # the URL that just failed).
        assert listing_fallback_chain(_state(criteria=CRITERIA)) == [
            PROMO, CRITERIA, PDP,
        ]

    def test_verdict_dead_candidate_is_excluded(self):
        from webapp.agents.nodes.run_execution import (
            listing_fallback_chain,
            resolve_listing_candidate,
        )

        chain = listing_fallback_chain(_state(verdict=dict(VERDICT_653)))
        # the PDP is verdict-dead → excluded; the alive promotion survives
        assert chain == [PROMO]
        # ...but resolve never offers the chain head (PROMO) as its own alt —
        # after demotion to the promotion there is NO alt (653-inverse guard)
        _primary, alt, _note = resolve_listing_candidate(
            _state(verdict=dict(VERDICT_653))
        )
        assert alt == ""

    def test_ledger_dead_fallback_is_skipped(self):
        from webapp.agents.nodes.run_execution import listing_fallback_chain

        v = dict(
            VERDICT_653,
            candidate=CRITERIA,
            demotion_target=PROMO,
            attempts=[
                {"candidate": CRITERIA, "crashed": False, "dead": True,
                 "inconclusive": False},
                {"candidate": PROMO, "crashed": False, "dead": True,
                 "inconclusive": False},
            ],
        )
        st = _state(criteria=CRITERIA, verdict=v)
        # promotion probed dead → the job URL is the only survivor
        assert listing_fallback_chain(st) == [PDP]

    def test_cross_domain_dropped(self):
        from webapp.agents.nodes.run_execution import listing_fallback_chain

        # the cross-host promotion is F17-dropped; the same-domain job URL
        # stays (the rescue consumer skips it only while it is the URL that
        # just ran).
        assert listing_fallback_chain(_state(nav_listing=CROSS)) == [PDP]


class TestConsumerParity:
    """tester env, probe candidates, and the resolver answer identically."""

    def test_no_verdict_all_consumers_say_job_url(self):
        from webapp.agents.graph import _probe_listing_candidates
        from webapp.agents.nodes.run_execution import resolve_listing_candidate
        from webapp.agents.tools import shell_tools

        st = _state()
        primary, _alt, _note = resolve_listing_candidate(st)
        probe_primary, _probe_alt = _probe_listing_candidates(st)
        tester_cand, _tester_note = shell_tools._tester_listing_candidate(st)
        assert primary == probe_primary == tester_cand == PDP

    def test_verdict_all_consumers_say_promotion(self):
        from webapp.agents.graph import _probe_listing_candidates
        from webapp.agents.nodes.run_execution import resolve_listing_candidate
        from webapp.agents.tools import shell_tools

        st = _state(verdict=dict(VERDICT_653))
        primary, _alt, note = resolve_listing_candidate(st)
        probe_primary, _probe_alt = _probe_listing_candidates(st)
        tester_cand, _tester_note = shell_tools._tester_listing_candidate(st)
        assert primary == probe_primary == tester_cand == PROMO
        assert note == "promotion(demoted)"

    def test_hint_routes_through_the_resolver(self):
        # consumer #4: the tester message builder's --listing-url hint.
        src = inspect.getsource(
            __import__("webapp.agents.subagents", fromlist=["x"])
        )
        i = src.find('elif "listing-url" in _accepted:')
        assert i != -1, "listing-url hint branch missing"
        assert "resolve_listing_candidate" in src[i : i + 700], (
            "the CLI hint must route through the shared resolver (consumer #4)"
        )

    def test_state_channel_declared(self):
        from webapp.agents.state import ScrapeState

        assert "listing_probe_verdict" in ScrapeState.__annotations__, (
            "undeclared channels are silently stripped from node updates "
            "(wave-56b rule)"
        )


# ─── the arming rules (R2/R5) ────────────────────────────────────────────────


def _yield(candidate, attempts, dead=True, stop="empty_render"):
    return {
        "discovered_urls": 0 if dead else 40,
        "stop_reason": stop,
        "listing_candidate": candidate,
        "listing_attempts": attempts,
    }


class TestArmListingProbeVerdict:
    def _helper(self):
        from webapp.agents.graph import _arm_listing_probe_verdict

        return _arm_listing_probe_verdict

    def test_653_shape_arms(self):
        # PDP dead on attempt 1; the promotion retry CRASHED (unknown, not
        # dead) → the promotion is a legitimate demotion target.
        v = self._helper()(
            _state(),
            _yield(PDP, VERDICT_653["attempts"]),
            crashed=False,
        )
        assert v == VERDICT_653

    def test_unattributed_yield_never_arms(self):
        assert self._helper()(
            _state(), {"discovered_urls": 0, "stop_reason": "empty_render"},
            crashed=False,
        ) is None

    def test_dead_candidate_must_be_the_current_primary(self):
        # the final dead yield is the FALLBACK's (promotion) → no demotion
        attempts = [
            {"candidate": PDP, "crashed": False, "dead": False,
             "inconclusive": False},
            {"candidate": PROMO, "crashed": False, "dead": True,
             "inconclusive": False},
        ]
        assert self._helper()(_state(), _yield(PROMO, attempts), False) is None

    def test_dead_fallback_vetoes_the_demotion(self):
        # both candidates probed DEAD → demoting onto the promotion is the
        # 653-inverse; no verdict.
        attempts = [
            {"candidate": PDP, "crashed": False, "dead": True,
             "inconclusive": False},
            {"candidate": PROMO, "crashed": False, "dead": True,
             "inconclusive": False},
        ]
        assert self._helper()(_state(), _yield(PDP, attempts), False) is None

    def test_crashed_probe_never_arms(self):
        assert self._helper()(_state(), None, crashed=True) is None

    def test_healthy_yield_never_arms(self):
        attempts = [
            {"candidate": PDP, "crashed": False, "dead": False,
             "inconclusive": False},
        ]
        assert self._helper()(
            _state(), _yield(PDP, attempts, dead=False), crashed=False
        ) is None

    def test_predicate_is_probe_yield_dead_not_raw_zero(self):
        # R5: the arming consults _probe_yield_dead — a healthy yield with a
        # junk `found` split, and every dead shape, through ONE predicate.
        from webapp.agents.graph import _probe_yield_dead

        junk = {"discovered_urls": 1, "stop_reason": "short_page",
                "coverage": {"found": 0}}
        assert _probe_yield_dead(junk) is True
        healthy = {"discovered_urls": 900, "stop_reason": "max_pages_hit"}
        assert _probe_yield_dead(healthy) is False


# ─── suppression seam (R4) + untouched mode branches ─────────────────────────


class TestSuppressionSeamAndModeBranches:
    def test_transient_suppression_is_verdict_aware(self):
        # R4: other-candidate positive evidence no longer silences a
        # verdict-armed dead primary (the 653 shape).
        import webapp.agents.graph as graph

        src = inspect.getsource(graph)
        i = src.find("and report.get(\"discovery_transient\")")
        assert i != -1
        assert "and not _lpv" in src[i : i + 120], (
            "the job-311 transient arm must not suppress a verdict-armed "
            "candidate-attributed zero"
        )

    def test_search_term_env_branch_untouched(self):
        # T6c: a search job's discovery is owned by --query; a non-query
        # candidate must not pin the gate. The elif arm still exists.
        import webapp.agents.nodes.run_execution as re_mod

        src = inspect.getsource(re_mod)
        assert 'elif input_mode == "search_term":' in src
        assert "NOT setting SCRAPER_LISTING_URL" in src

    def test_url_list_seed_branch_untouched(self):
        import webapp.agents.nodes.run_execution as re_mod

        src = inspect.getsource(re_mod)
        assert 'elif input_mode == "url_list":' in src
        assert "url_list_discovery_seed" in src

    def test_tester_url_list_seed_preserved(self):
        # R3: the unified chain is list_page/navigation-only; the wave-58
        # url_list seed arm keeps serving the tester verbatim.
        from src.discovery_quality import url_list_discovery_seed  # noqa: F401
        from webapp.agents.tools import shell_tools

        st = {
            "input_mode": "url_list",
            "url": "https://s.example.com",
            "input_urls": ["https://s.example.com/collections/a"],
            "navigation_analysis": {},
        }
        cand, _note = shell_tools._tester_listing_candidate(st)
        assert cand == "https://s.example.com/collections/a"

    def test_tester_search_term_legacy_branch(self):
        # search_term keeps the legacy promotion→criteria branch (T6c owns
        # search_term's env gate; the tester branch is unchanged). Same-domain
        # promotion — the legacy branch is F17-guarded like everything else.
        from webapp.agents.tools import shell_tools

        st = {
            "input_mode": "search_term",
            "url": "https://www.ultrabeauty.com/product/x",
            "navigation_analysis": {"discovery": {"listing_url": PROMO}},
        }
        cand, _note = shell_tools._tester_listing_candidate(st)
        assert cand == PROMO


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
