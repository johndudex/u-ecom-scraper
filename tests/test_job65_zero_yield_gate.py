"""Job-65 citybeach Phase-2 regressions: the deterministic zero-yield gate.

What happened (job 65): the tester discovered 1,317 URLs and PASSED; the
execution run hit an execution-window soft block (200-but-zero-links) and
finalized 0 items. Nothing in the graph re-verified discovery under EXECUTION
conditions — ``phases_tested.phase1_discovery`` was the tester LLM's
self-report and the probe only looked for crashes. (And Phase 2 alone would
NOT have saved job 65 — its discovery worked at test time; Phase 1's shared
module fixes the block itself. This gate catches the class that can never
work: the draft whose discovery yields 0 under the same listing/env the
executor will use.)

Fixes pinned here:

- C0 (``_normalize_probe_stop_reason``): an exhaustion-flavored stop_reason on
  a ZERO-URL probe is reclassified to ``empty_first_page`` — the graph-level
  mirror of the draft-side reclassification — so ``_discovery_coverage_failure``
  arms and ``classify_test_failure`` lands "strategy" (never "refine").
- C1 (``_probe_phase1_discovery``): the probe returns its YIELD as a third
  element (mtime-floored to THIS probe's run — never a stale artifact), and
  ``_invoke_code_tester`` force-FAILs a clean-exit 0-URL run (unless
  ``discovery_transient`` evidence suppresses it — job-311 lesson), sets
  ``phase1_discovery`` deterministically on both branches, and fails honestly
  on the final attempt instead of releasing a guaranteed-0-item execution.
- C2 (same probe): the probe injects ``SCRAPER_LISTING_URL`` exactly like
  ``run_execution`` — list_page's JOB URL outranks the navigator's promotion
  (job 310), else ``navigation_analysis.discovery.listing_url``; F17 drops
  cross-domain candidates.

Run: docker compose exec -T -e DJANGO_SETTINGS_MODULE=config.settings -e PYTHONPATH=/app:/app/webapp django sh -c "cd /app && pytest tests/test_job65_zero_yield_gate.py -q"
"""
from __future__ import annotations

import os
import re
import subprocess as _subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django

django.setup()


def _grab(src: str, name: str) -> str:
    m = re.search(rf"^def {name}\(.*?(?=^def |\Z)", src, re.MULTILINE | re.DOTALL)
    assert m, f"function {name} not found"
    return m.group(0)


# ─── C0: zero-URL stop-reason normalization ──────────────────────────────────


class TestProbeStopReasonNormalization:
    def test_exhaustion_flavored_reasons_reclassify_to_empty_first_page(self):
        from webapp.agents.graph import _normalize_probe_stop_reason

        for sr in ("short_page", "no_next_link", "no_new_items"):
            assert _normalize_probe_stop_reason(sr) == "empty_first_page", sr

    def test_hard_and_unknown_reasons_pass_through(self):
        from webapp.agents.graph import _normalize_probe_stop_reason

        assert _normalize_probe_stop_reason("navigate_error") == "navigate_error"
        assert _normalize_probe_stop_reason("") == ""
        assert _normalize_probe_stop_reason("max_pages_hit") == "max_pages_hit"
        assert _normalize_probe_stop_reason("empty_render") == "empty_render"

    def test_reclassified_reason_arms_the_coverage_gate_and_lands_strategy(self):
        """The full downstream shape: a zero-URL probe verdict, normalized,
        must FAIL via the coverage gate and classify as a strategy problem —
        the retry must hand code_writer/refit a strategy switch, not a
        field-tweak loop."""
        from webapp.agents.graph import _normalize_probe_stop_reason
        from webapp.agents.nodes.route_after_testing import (
            _discovery_coverage_failure,
            classify_test_failure,
        )

        report = {
            "overall_assessment": "FAIL",
            "results": {"successful_extractions": 0},
            "discovery_coverage": {
                "ran_phase1": True,
                "stop_reason": _normalize_probe_stop_reason("no_next_link"),
                "discovered_urls": 0,
                "found": 0,
                "probe_scope": "discover_only",
            },
        }
        reason = _discovery_coverage_failure(report)
        assert reason and "empty_first_page" in reason
        action, why = classify_test_failure(report, "http_requests")
        assert action == "strategy"
        assert "empty_first_page" in why or "no items" in why


# ─── C1: probe return contract (always a 3-tuple) ────────────────────────────


class TestProbeReturnContract:
    def _probe(self, state, slug="s", tmp=None):
        from django.test.utils import override_settings

        from webapp.agents.graph import _probe_phase1_discovery

        with override_settings(PROJECT_ROOT=str(tmp or ROOT)):
            return _probe_phase1_discovery(slug, state, 1)

    def test_every_early_exit_is_a_three_tuple(self, tmp_path):
        # no slug / no Phase-1 mode / missing draft — all inconclusive 3-tuples
        assert self._probe({}, slug="") == (False, None, None)
        assert self._probe({"input_mode": "url_list"}, tmp=tmp_path) == (
            False, None, None,
        )
        assert self._probe({"input_mode": "list_page"}, tmp=tmp_path) == (
            False, None, None,
        )

    def test_draft_without_discover_only_flag_is_not_probed(self, tmp_path):
        ws = tmp_path / "workspace" / "s"
        ws.mkdir(parents=True)
        (ws / "scraper_draft.py").write_text(
            "import argparse\n"
            "p = argparse.ArgumentParser()\n"
            "p.add_argument('--sample', action='store_true')\n",
            encoding="utf-8",
        )
        assert self._probe({"input_mode": "list_page"}, tmp=tmp_path) == (
            False, None, None,
        )


# ─── C2: the probe runs under EXECUTION conditions ───────────────────────────


def _probe_draft() -> str:
    return (
        "import argparse\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--discover-only', action='store_true')\n"
        "p.add_argument('--fresh-discovery', action='store_true')\n"
    )


class TestProbeExecutionConditions:
    """SCRAPER_LISTING_URL injection must mirror run_execution exactly."""

    def _capture_run(self, monkeypatch, fail_with_timeout=True):
        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured["env"] = kwargs.get("env")
            if fail_with_timeout:
                raise _subprocess.TimeoutExpired(argv, 180)
            return _subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr(_subprocess, "run", fake_run)
        return captured

    def _probe_with(self, monkeypatch, state, tmp_path):
        from django.test.utils import override_settings

        from webapp.agents.graph import _probe_phase1_discovery

        ws = tmp_path / "workspace" / "s"
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "scraper_draft.py").write_text(_probe_draft(), encoding="utf-8")
        captured = self._capture_run(monkeypatch)
        with override_settings(PROJECT_ROOT=str(tmp_path)):
            result = _probe_phase1_discovery("s", state, 1)
        return result, captured

    def test_list_page_job_url_outranks_navigator_promotion(self, monkeypatch, tmp_path):
        """Job 310: for list_page the JOB URL is the listing execution uses —
        the probe must inject that one, not the navigator's."""
        state = {
            "input_mode": "list_page",
            "url": "https://shop.example.com/c/mens",
            "navigation_analysis": {
                "discovery": {"listing_url": "https://shop.example.com/nav-promoted"},
            },
        }
        result, captured = self._probe_with(monkeypatch, state, tmp_path)
        # TimeoutExpired → inconclusive 3-tuple (never a verdict from a timeout)
        assert result == (False, None, None)
        assert captured["env"] is not None
        assert (
            captured["env"]["SCRAPER_LISTING_URL"]
            == "https://shop.example.com/c/mens"
        )

    def test_f17_drops_cross_domain_navigator_listing(self, monkeypatch, tmp_path):
        """Navigation mode: the navigator's promoted listing is the only
        candidate — if F17 judges it cross-domain, the probe drops it and runs
        on the draft's own default (no SCRAPER_LISTING_URL at all), exactly
        like run_execution."""
        state = {
            "input_mode": "navigation",
            "url": "https://shop.example.com",
            "navigation_analysis": {
                "discovery": {"listing_url": "https://other-domain.org/x"},
            },
        }
        _result, captured = self._probe_with(monkeypatch, state, tmp_path)
        assert not (captured["env"] or {}).get("SCRAPER_LISTING_URL")

    def test_navigation_mode_uses_navigator_listing_url(self, monkeypatch, tmp_path):
        state = {
            "input_mode": "navigation",
            "url": "https://shop.example.com",
            "navigation_analysis": {
                "discovery": {"listing_url": "https://shop.example.com/c/all"},
            },
        }
        _result, captured = self._probe_with(monkeypatch, state, tmp_path)
        assert (
            captured["env"]["SCRAPER_LISTING_URL"]
            == "https://shop.example.com/c/all"
        )

    def test_probe_flags_are_discover_only_and_fresh(self, monkeypatch, tmp_path):
        state = {"input_mode": "list_page", "url": "https://shop.example.com/c/x"}
        _result, captured = self._probe_with(monkeypatch, state, tmp_path)
        assert "--discover-only" in captured["argv"]
        assert "--fresh-discovery" in captured["argv"]


# ─── C1 caller gate: static contract of _invoke_code_tester ──────────────────


class TestCallerGateStatic:
    @pytest.fixture(scope="class")
    def tester_src(self):
        with open(
            os.path.join(ROOT, "webapp", "agents", "graph.py"), encoding="utf-8"
        ) as fh:
            return _grab(fh.read(), "_invoke_code_tester")

    def test_probe_unpacks_the_three_tuple(self, tester_src):
        assert (
            "crashed, tb, probe_yield = _probe_phase1_discovery(" in tester_src
        )

    @staticmethod
    def _zero_branch(tester_src: str) -> str:
        # The zero-check appears in BOTH the suppression branch and the
        # force-FAIL branch — the force-FAIL branch is the SECOND occurrence
        # (order pinned by test_transient_evidence_suppresses_...).
        # [job-85 wave 7] the check itself is now the shared predicate
        # ``_probe_yield_dead(probe_yield)`` — a raw ``== 0`` comparison would
        # re-open the junk-yield blindness (a PDP's 1 self link read as
        # success).
        first = tester_src.find("_probe_yield_dead(probe_yield)")
        i = tester_src.find("_probe_yield_dead(probe_yield)", first + 1)
        assert i != -1, "zero-yield force-FAIL branch missing"
        return tester_src[i : i + 3200]

    def test_zero_checks_use_the_shared_predicate(self, tester_src):
        """A bare ``discovered_urls == 0`` here would bless a PDP's junk link
        (the exact job-85 miss) — every zero-verdict must go through
        ``_probe_yield_dead``."""
        assert 'probe_yield["discovered_urls"] == 0' not in tester_src
        assert tester_src.count("_probe_yield_dead(probe_yield)") >= 2

    def test_zero_yield_force_fails_the_test(self, tester_src):
        gate = self._zero_branch(tester_src)
        for marker in (
            '"overall_assessment"] = "FAIL"',
            '"confidence_score"] = 0.0',
            '"ready_for_execution"] = False',
            '"phase1_discovery"] = False',
            '"probe_scope": "discover_only"',
        ):
            assert marker in gate, marker

    def test_transient_evidence_suppresses_the_zero_yield_verdict(self, tester_src):
        # The suppression branch is `(dead and report.get("discovery_transient"))`
        # and must sit BEFORE the force-FAIL branch: "discovery_transient"
        # belongs to the FIRST dead-check condition, the force-FAIL branch is
        # the SECOND one.
        j = tester_src.find("discovery_transient")
        first = tester_src.find("_probe_yield_dead(probe_yield)")
        second = tester_src.find("_probe_yield_dead(probe_yield)", first + 1)
        assert first != -1 and second != -1
        assert first < j < second

    def test_phase1_discovery_becomes_deterministic_on_success(self, tester_src):
        assert '["phase1_discovery"] = (\n                probe_yield["discovered_urls"] > 0' in tester_src

    def test_final_attempt_fails_honestly_instead_of_releasing(self, tester_src):
        gate = self._zero_branch(tester_src)
        assert "FINAL_RETRY_SENTINEL" in gate
        assert 'update["execution_status"] = "FAILED"' in gate

    def test_success_merge_does_not_clobber_tester_found(self, tester_src):
        """_volume_gap reads ``found`` — the probe (which skips Phase 2, so its
        own found is 0 by construction) must never overwrite the tester's."""
        i = tester_src.find("_pcov = dict(")
        assert i != -1, "success merge missing"
        merge = tester_src[i : i + 900]
        assert '"found"' not in merge
        assert '"probe_scope": "discover_only"' in merge

    def test_transient_evidence_attaches_before_the_probe_runs(self, tester_src):
        """The suppression branch reads report['discovery_transient'] — the
        evidence attach must precede the probe call or it suppresses nothing."""
        assert (
            tester_src.find("_attach_transient_render_evidence")
            < tester_src.find("_probe_phase1_discovery(")
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
