"""[wave-32 E1] F17 chain parity — the probe and the tester must both
domain-guard their ``SCRAPER_LISTING_URL`` candidate, with FALL-THROUGH.

Job 587 (marimekko, 2h22m writer-wall FAIL): the intake accepted a
loveamika.com listing on a marimekko job (fixed at the door by B3, but the
pipeline must also survive one that arrives another way — an old queued job,
a direct DB edit, a future intake regression). Two latent amplifiers:

- graph ``_probe_phase1_discovery_once`` resolved ``override or primary or
  alt`` and then NULLED the whole env candidate when F17 rejected it — the
  smoke probe ran with the draft's default discovery instead of falling
  through to the same-domain candidate it had. run_execution's chain
  (run_execution.py:815-826) walks its candidates with per-candidate F17
  drops; the probe's own C2 mirror contract says it must do the same.
- shell_tools ``run_scraper`` injected ``discovery.listing_url`` / a
  URL-shaped ``search_criteria`` into the tester's env with NO domain guard
  at all — the tester then proved (or failed) a listing execution can never
  use.

Both fixes mirror run_execution's F17 loop: guard EACH candidate against the
job URL's registrable domain; a dropped candidate falls through to the next;
'' only when nothing survives.
"""
from __future__ import annotations

import inspect
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django

django.setup()

import pytest

JOB_URL = "https://www.marimekko.com/us_en/harhautus-unikko-cardigan"
CROSS_LISTING = "https://www.loveamika.com/collections/all"
SAME_LISTING = "https://shop.marimekko.com/us_en/c/clothing/knitwear"


def _state(criteria="", nav_listing="", mode="list_page", url=JOB_URL):
    nav = {"discovery": {"listing_url": nav_listing}} if nav_listing else {}
    st = {"input_mode": mode, "url": url, "navigation_analysis": nav}
    if criteria:
        st["search_criteria"] = criteria
    return st


class TestProbeFallsThrough:
    """E1a — ``_probe_listing_env_candidate`` F17-walks the chain."""

    def test_cross_host_primary_falls_through_to_same_host_alt(self):
        from webapp.agents.graph import _probe_listing_env_candidate

        # list_page: primary = URL-shaped search_criteria (cross-host),
        # alt = the navigator's same-domain promotion. Old code nulled the
        # whole candidate; the chain walk must land on the alt.
        st = _state(criteria=CROSS_LISTING, nav_listing=SAME_LISTING)
        assert _probe_listing_env_candidate(st) == SAME_LISTING

    def test_same_host_override_wins_without_fallthrough(self):
        from webapp.agents.graph import _probe_listing_env_candidate

        st = _state(criteria=CROSS_LISTING, nav_listing=SAME_LISTING)
        assert (
            _probe_listing_env_candidate(st, listing_override=SAME_LISTING)
            == SAME_LISTING
        )

    def test_cross_host_override_falls_to_chain(self):
        from webapp.agents.graph import _probe_listing_env_candidate

        # The retry's navigator promotion is F17-guarded too; a dropped
        # override falls to the surviving chain, not to "".
        st = _state(criteria=CROSS_LISTING, nav_listing=SAME_LISTING)
        assert (
            _probe_listing_env_candidate(st, listing_override=CROSS_LISTING)
            == SAME_LISTING
        )

    def test_all_candidates_cross_host_or_empty_yields_empty(self):
        from webapp.agents.graph import _probe_listing_env_candidate

        st = _state(criteria=CROSS_LISTING, nav_listing=CROSS_LISTING)
        assert _probe_listing_env_candidate(st) == ""

    def test_no_job_registrable_is_unguarded(self):
        from webapp.agents.graph import _probe_listing_env_candidate

        # '' on the job's registrable means "cannot judge" — never gate.
        st = _state(criteria=CROSS_LISTING, nav_listing="", url="not a url")
        assert _probe_listing_env_candidate(st) == CROSS_LISTING

    def test_probe_wiring_uses_the_chain_walk(self):
        from webapp.agents import graph

        src = inspect.getsource(graph._probe_phase1_discovery_once)
        assert "_probe_listing_env_candidate(" in src, (
            "the probe env chain must go through the F17 walk, not the old "
            "or-chain + nuke-on-drop"
        )
        assert "or _primary or _alt" not in src


class TestTesterToolGuard:
    """E1b — run_scraper never injects a cross-host SCRAPER_LISTING_URL."""

    def _helper(self):
        from webapp.agents.tools import shell_tools

        return shell_tools._tester_listing_candidate

    def test_cross_host_listing_url_is_dropped_with_note(self):
        st = _state(nav_listing=CROSS_LISTING)
        cand, note = self._helper()(st)
        assert cand == ""
        assert "loveamika.com" in note and "marimekko.com" in note

    def test_cross_host_criteria_is_dropped_with_note(self):
        st = _state(criteria=CROSS_LISTING, nav_listing="")
        cand, note = self._helper()(st)
        assert cand == ""
        assert "F17" in note

    def test_same_host_listing_url_passes(self):
        st = _state(nav_listing=SAME_LISTING)
        cand, note = self._helper()(st)
        assert cand == SAME_LISTING
        assert note == ""

    def test_nothing_to_inject_is_quiet(self):
        cand, note = self._helper()(_state(nav_listing=""))
        assert cand == "" and note == ""

    def test_same_host_multiline_criteria_candidate_is_line_one(self):
        # [wave-40 T4 r1] intake.html:509 stores "One listing page per line"
        # text verbatim in search_criteria; urlsplit deletes the newline, so
        # the WHOLE multi-line string used to become ONE same-host mangled
        # SCRAPER_LISTING_URL. The candidate is the FIRST http(s) line and
        # search_criteria itself stays verbatim.
        multi = f"{SAME_LISTING}\n{SAME_LISTING}?page=2"
        st = _state(criteria=multi, nav_listing="")
        cand, note = self._helper()(st)
        assert cand == SAME_LISTING and "\n" not in cand
        assert st["search_criteria"] == multi
        assert note == ""

    def test_multiline_criteria_line_two_never_rides_past_f17(self):
        # The old joined-string parse read as ONE same-host URL, so an
        # off-host line 2 rode into the tester's env inside the mangled
        # candidate, past the F17 registrable guard.
        multi = f"{SAME_LISTING}\n{CROSS_LISTING}"
        st = _state(criteria=multi, nav_listing="")
        cand, note = self._helper()(st)
        assert cand == SAME_LISTING and CROSS_LISTING not in cand
        assert st["search_criteria"] == multi

    def test_run_scraper_wiring_uses_the_guard(self):
        from webapp.agents.tools import shell_tools

        # run_scraper is nested inside the get_shell_tools factory.
        src = inspect.getsource(shell_tools.get_shell_tools)
        assert "_tester_listing_candidate(" in src, (
            "the tester's SCRAPER_LISTING_URL injection must go through the "
            "F17 guard"
        )
