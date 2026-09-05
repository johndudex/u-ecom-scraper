"""[wave-20 T2] A draft that SKIPS its own Phase 1 is a code verdict.

Prod RCA (job 360 marimekko, 2026-09-05): the writer-generated draft logged
``Phase 1: SKIPPED (url_list mode with 1 seed URLs)`` and returned in 0.0 s —
its mode gate let the existence of ``input_urls.json`` override the explicit
discovery flags. The deterministic probe then read the dead yield, escalated
a network identity against it, still got nothing, and stamped
``stop_reason="all_tiers_blocked"`` — reporting a CODE bug as SITE BLOCKING.
The job died with "the draft's discovery cannot see this site's items", which
is false: the draft never LOOKED.

Contract:

- when the draft's own coverage says ``ran_phase1: False``, the probe stamps
  ``stop_reason="phase1_skipped"`` and does NOT run the identity escalation
  (never escalate against a draft that didn't look);
- ``phase1_skipped`` classifies as the ``scraper`` action (targeted code
  fix) — it must NOT join ``_COVERAGE_FAIL_STOP_REASONS`` (that set means
  strategy-switch);
- a genuinely dead yield (draft ran, got nothing) keeps the wave-19
  escalation behavior untouched.
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
from webapp.agents.nodes.route_after_testing import (  # noqa: E402
    _COVERAGE_FAIL_STOP_REASONS,
    classify_test_failure,
)

SLUG = "marimekko-com"
JOB_URL = "https://www.marimekko.com/us_en/harhautus-unikko-knitted-cardigan"
LISTING = "https://www.marimekko.com/us_en/c/clothing/knitwear"


def _skipped_yield(discovered=0):
    """What the marimekko draft's output actually carried: it never ran."""
    return {
        "discovered_urls": discovered,
        "stop_reason": "",
        "coverage": {"ran_phase1": False, "discovered_urls": discovered},
    }


def _dead_yield():
    return {
        "discovered_urls": 0,
        "stop_reason": "empty_first_page",
        "coverage": {"stop_reason": "empty_first_page", "discovered_urls": 0},
    }


@pytest.fixture
def tiers_configured(monkeypatch):
    monkeypatch.setattr(graph, "_tier_configured", lambda tier: tier == "none" or bool(tier))


class TestProbeSkippedVerdict:
    def _patch_once(self, monkeypatch, results):
        calls = []

        def _once(slug, state, job_id, listing_override=""):
            calls.append({"listing": listing_override, "state": state})
            return results.pop(0)

        monkeypatch.setattr(graph, "_probe_phase1_discovery_once", _once)
        monkeypatch.setattr(graph, "_probe_retry_warranted", lambda state, y: False)
        return calls

    def test_self_skipped_stamps_phase1_skipped_and_never_escalates(
        self, monkeypatch, tiers_configured
    ):
        calls = self._patch_once(monkeypatch, [(False, None, _skipped_yield())])
        state = {"url": JOB_URL, "input_mode": "list_page", "search_criteria": LISTING}
        crashed, tb, y = graph._probe_phase1_discovery(SLUG, state, 0)
        assert crashed is False
        assert y["stop_reason"] == "phase1_skipped"
        assert y["coverage"]["stop_reason"] == "phase1_skipped"
        assert len(calls) == 1, "never escalate against a draft that didn't look"
        assert "tier_escalation" not in y["coverage"]

    def test_genuinely_dead_yield_still_escalates(self, monkeypatch, tiers_configured):
        calls = self._patch_once(
            monkeypatch, [(False, None, _dead_yield()), (False, None, _dead_yield())]
        )
        state = {"url": JOB_URL, "input_mode": "list_page", "search_criteria": LISTING}
        y = graph._probe_phase1_discovery(SLUG, state, 0)[2]
        assert len(calls) == 2, "wave-19 escalation contract unchanged"
        assert y["stop_reason"] == "all_tiers_blocked"


class TestSkippedVerdictIsCodeClass:
    def test_normalize_passes_phase1_skipped_through(self):
        assert graph._normalize_probe_stop_reason("phase1_skipped") == "phase1_skipped"

    def test_phase1_skipped_not_in_strategy_stop_reasons(self):
        # That set means strategy-switch — exactly the misroute this fix kills.
        assert "phase1_skipped" not in _COVERAGE_FAIL_STOP_REASONS

    def test_router_lands_code_fix_on_phase1_skipped(self):
        report = {
            "overall_assessment": "FAIL",
            "results": {"successful_extractions": 0},
            "discovery_coverage": {
                "ran_phase1": True,  # forced True by the zero-yield guard
                "stop_reason": "phase1_skipped",
                "discovered_urls": 0,
                "probe_scope": "discover_only",
            },
        }
        action, why = classify_test_failure(report, "http_requests")
        assert action == "scraper", f"got ({action!r}, {why!r})"
        assert "phase 1" in why.lower() or "skipped" in why.lower()

    def test_router_no_remediation_needed_for_the_verdict(self):
        # Deliberately NO remediation key — the skipped stamp alone must carry
        # the code verdict (unlike the T1 promotion, which needs a diagnosis).
        report = {
            "results": {"successful_extractions": 1},
            "discovery_coverage": {
                "ran_phase1": True,
                "stop_reason": "phase1_skipped",
                "discovered_urls": 0,
            },
        }
        action, _ = classify_test_failure(report, "http_navigation")
        assert action == "scraper"


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
