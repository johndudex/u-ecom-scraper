"""[job-76 myhouse, latent] The Phase-1 probe must retry with the navigator's
listing when the primary candidate yields zero.

Job 76's post-mortem surfaced a latent probe bug shared by every list_page
job (jobs 77-93 are all PDP-URL list_page jobs): the probe injects the JOB
URL as ``SCRAPER_LISTING_URL`` first (the job-310 contract — correct for a
real listing URL), but job 76's job URL is an ITEM page. As a listing it can
only ever yield 0, while the navigator's promoted listing yields 40 — so the
probe (and the zero-yield gate that trusts it) tests the one URL that is
guaranteed dead before ever trying the one that works.

Fix: the probe gets ONE retry with the navigator's ``discovery.listing_url``
when (and only when) the first attempt ran on the primary candidate and
yielded 0. Bounds that keep this honest, not a flailer:

- crashed / inconclusive first probe → no retry (a crash is a code bug, not
  a listing choice);
- no distinct navigator listing → no retry (nothing better to try);
- cross-domain navigator listing → no retry (F17 applies to the retry too);
- both candidates yield 0 → the honest zero stands (the caller's zero-yield
  gate fires on real evidence from BOTH listings).

The job-310 ordering is untouched: the job URL is still tried FIRST, and a
nonzero first attempt never retries.
"""
from __future__ import annotations

import json
import os
import subprocess as _subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django

django.setup()


JOB_URL = "https://myhouse.example.com/products/ashler-rug"
NAV_LISTING = "https://myhouse.example.com/collections/rugs"


def _state(mode="list_page", nav_listing=NAV_LISTING, url=JOB_URL):
    nav = {"discovery": {"listing_url": nav_listing}} if nav_listing else {}
    return {"input_mode": mode, "url": url, "navigation_analysis": nav}


# ─── candidate chain (shared by the probe and the retry decision) ────────────


class TestListingCandidates:
    def test_list_page_job_url_first_navigator_second(self):
        from webapp.agents.graph import _probe_listing_candidates

        assert _probe_listing_candidates(_state()) == (JOB_URL, NAV_LISTING)

    def test_non_list_page_has_no_job_url_candidate(self):
        from webapp.agents.graph import _probe_listing_candidates

        assert _probe_listing_candidates(_state(mode="navigation")) == (
            "", NAV_LISTING,
        )

    def test_missing_navigator_listing_is_empty_alt(self):
        from webapp.agents.graph import _probe_listing_candidates

        assert _probe_listing_candidates(_state(nav_listing="")) == (JOB_URL, "")
        assert _probe_listing_candidates({}) == ("", "")


# ─── retry decision: bounded, evidence-driven ────────────────────────────────


class TestRetryWarranted:
    def test_zero_yield_with_distinct_alt_is_a_retry(self):
        from webapp.agents.graph import _probe_retry_warranted

        assert _probe_retry_warranted(_state(), {"discovered_urls": 0}) is True

    def test_nonzero_yield_never_retries(self):
        from webapp.agents.graph import _probe_retry_warranted

        assert _probe_retry_warranted(_state(), {"discovered_urls": 12}) is False

    def test_inconclusive_probe_never_retries(self):
        from webapp.agents.graph import _probe_retry_warranted

        assert _probe_retry_warranted(_state(), None) is False

    def test_no_alt_or_same_alt_never_retries(self):
        from webapp.agents.graph import _probe_retry_warranted

        assert _probe_retry_warranted(_state(nav_listing=""), {"discovered_urls": 0}) is False
        assert _probe_retry_warranted(_state(nav_listing=JOB_URL), {"discovered_urls": 0}) is False

    def test_cross_domain_alt_never_retries(self):
        from webapp.agents.graph import _probe_retry_warranted

        st = _state(nav_listing="https://other-domain.org/c/rugs")
        assert _probe_retry_warranted(st, {"discovered_urls": 0}) is False


# ─── wrapper behavior: exactly one retry, verdict from the LAST attempt ──────


class TestProbeWrapper:
    @pytest.fixture()
    def fake_once(self, monkeypatch):
        import webapp.agents.graph as graph

        calls = []

        def _once(slug, state, job_id, listing_override=""):
            calls.append({"listing_override": listing_override})
            return _once.script[len(calls) - 1]

        _once.script = []
        _once.calls = calls
        monkeypatch.setattr(graph, "_probe_phase1_discovery_once", _once)
        return _once

    def _run(self, state):
        from webapp.agents.graph import _probe_phase1_discovery

        return _probe_phase1_discovery("s", state, 1)

    def test_zero_first_attempt_retries_once_with_navigator_listing(self, fake_once):
        fake_once.script = [
            (False, None, {"discovered_urls": 0, "stop_reason": ""}),
            (False, None, {"discovered_urls": 40, "stop_reason": "max_pages_hit"}),
        ]
        crashed, tb, probe_yield = self._run(_state())
        assert len(fake_once.calls) == 2
        assert fake_once.calls[0]["listing_override"] == ""
        assert fake_once.calls[1]["listing_override"] == NAV_LISTING
        assert crashed is False
        assert probe_yield["discovered_urls"] == 40

    def test_nonzero_first_attempt_never_retries(self, fake_once):
        fake_once.script = [(False, None, {"discovered_urls": 7, "stop_reason": ""})]
        _crashed, _tb, probe_yield = self._run(_state())
        assert len(fake_once.calls) == 1
        assert probe_yield["discovered_urls"] == 7

    def test_crashed_first_attempt_does_not_retry(self, fake_once):
        fake_once.script = [(True, "Traceback ...", None)]
        crashed, tb, probe_yield = self._run(_state())
        assert len(fake_once.calls) == 1
        assert crashed is True and tb == "Traceback ..." and probe_yield is None

    def test_both_candidates_dead_is_an_honest_zero(self, fake_once):
        fake_once.script = [
            (False, None, {"discovered_urls": 0, "stop_reason": ""}),
            (False, None, {"discovered_urls": 0, "stop_reason": ""}),
        ]
        crashed, tb, probe_yield = self._run(_state())
        assert len(fake_once.calls) == 2
        assert crashed is False
        assert probe_yield["discovered_urls"] == 0

    def test_no_alt_means_single_attempt(self, fake_once):
        fake_once.script = [(False, None, {"discovered_urls": 0, "stop_reason": ""})]
        _crashed, _tb, probe_yield = self._run(_state(nav_listing=""))
        assert len(fake_once.calls) == 1
        assert probe_yield["discovered_urls"] == 0


# ─── end-to-end through the real `once` (local subprocess path) ──────────────


def _probe_draft() -> str:
    return (
        "import argparse\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--discover-only', action='store_true')\n"
        "p.add_argument('--fresh-discovery', action='store_true')\n"
    )


class TestEndToEndThroughOnce:
    """The full wrapper: attempt 1 injects the job URL, attempt 2 injects the
    navigator's listing, and the verdict reads the SECOND attempt's output."""

    def _probe(self, monkeypatch, tmp_path, urls_per_attempt):
        import webapp.agents.graph as graph

        ws = tmp_path / "workspace" / "s"
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "scraper_draft.py").write_text(_probe_draft(), encoding="utf-8")

        attempts = []

        def fake_run(argv, **kwargs):
            attempts.append((kwargs.get("env") or {}).get("SCRAPER_LISTING_URL"))
            n = len(attempts) - 1
            out = ws / f"output_probe_{n}.json"
            out.write_text(json.dumps({
                "products": [],
                "metadata": {
                    "phase": "discovery",
                    "discovery_coverage": {
                        "discovered_urls": urls_per_attempt[min(n, len(urls_per_attempt) - 1)],
                        "stop_reason": "",
                    },
                },
            }))
            return _subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr(_subprocess, "run", fake_run)
        from django.test.utils import override_settings

        with override_settings(PROJECT_ROOT=str(tmp_path)):
            result = graph._probe_phase1_discovery("s", _state(), 1)
        return result, attempts

    def test_job76_shape_retry_lands_on_the_working_listing(self, monkeypatch, tmp_path):
        result, attempts = self._probe(monkeypatch, tmp_path, [0, 40])
        assert attempts == [JOB_URL, NAV_LISTING]
        crashed, tb, probe_yield = result
        assert crashed is False
        assert probe_yield is not None and probe_yield["discovered_urls"] == 40

    def test_single_attempt_when_first_yields(self, monkeypatch, tmp_path):
        result, attempts = self._probe(monkeypatch, tmp_path, [40])
        assert attempts == [JOB_URL]
        _crashed, _tb, probe_yield = result
        assert probe_yield is not None and probe_yield["discovered_urls"] == 40


# ─── static: the F17 domain guard still wraps the override ──────────────────


class TestOverrideIsDomainGuarded:
    def test_f17_guard_sits_after_the_candidate_selection(self):
        src_path = os.path.join(ROOT, "webapp", "agents", "graph.py")
        with open(src_path, encoding="utf-8") as fh:
            src = fh.read()
        sel = src.find("listing_override or _primary or _alt")
        guard = src.find("_registrable_of", sel)
        assert sel != -1, "override-aware candidate selection missing"
        assert guard != -1 and guard - sel < 4000, "F17 guard must still apply"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
