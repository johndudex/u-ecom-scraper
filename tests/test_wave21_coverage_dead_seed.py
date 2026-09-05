"""[wave-21 T6] validate_coverage must honor the analyzer's dead-seed verdict.

Local e2e job 336 (marimekko): product_analyzer honestly wrote
``"verdict": "TARGET_URL_DEAD"`` into product_analysis.json after finding
the seed PDP 404'd — and the pipeline ignored it. The coverage gate counts
every mapping that merely HAS a method/selector (wave-13 presence credit),
and the live-render check that would prove mappings dead can't run on a
page that doesn't exist — so a dead-seed analysis sailed through at 100%
coverage and code generation shipped guesses (job 335 froze at the same
wall two tester cycles later).

Contract:
- an analysis whose ``verdict`` is ``TARGET_URL_DEAD`` (or whose analyzer
  critical_finding declares the target URL dead) is treated like MISSING
  analysis: interrupt for human decision, no matter how many mapped fields
  it carries — mapped-off-a-dead-page is worth zero;
- retry counter reuses ``product_analysis_retries``/MAX_VALIDATE_RETRIES
  (same "analysis unusable" semantics as the missing-file arm); at the cap
  the interrupt becomes ``dead_seed_exhausted`` (Continue anyway / Abort);
- an analysis with NO dead-seed verdict passes through unchanged.
"""
from __future__ import annotations

import importlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

# the package __init__ re-exports the FUNCTION under the module's name, which
# shadows normal import syntax — resolve the real module from sys.modules
vc_mod = importlib.import_module("agents.nodes.validate_coverage")  # noqa: E402

# Every core field mapped with method+selector and NOT proven dead — the
# 100%-coverage shape that used to buy the dead seed a free pass.
FULL_COVERAGE_FIELDS = {
    "title": {"method": "css", "selector": "h1.product-title"},
    "price": {"method": "jsonld", "selector": "Product.offers.price"},
    "availability": {"method": "jsonld", "selector": "Product.offers.availability"},
    "currency": {"method": "jsonld", "selector": "Product.offers.priceCurrency"},
    "url": {"method": "css", "selector": "link[rel=canonical]"},
    "src_url": {"method": "constant", "selector": "request.url"},
}


def _state(**extra):
    return {"site_slug": "example-com", **extra}


def _patch_analysis(monkeypatch, analysis):
    monkeypatch.setattr(vc_mod, "_load_product_analysis", lambda slug: analysis)


class TestDeadSeedVerdictInterrupts:
    def test_target_url_dead_beats_full_coverage(self, monkeypatch):
        """THE regression: verdict says the seed is dead, coverage says 100%
        — the verdict must win."""
        _patch_analysis(
            monkeypatch,
            {
                "verdict": "TARGET_URL_DEAD",
                "fields": dict(FULL_COVERAGE_FIELDS),
            },
        )
        cmd = vc_mod.validate_coverage(_state())
        assert cmd.goto == "human_approval"
        assert cmd.update["interrupt_reason"] == "dead_seed"
        assert "TARGET_URL_DEAD" in cmd.update["interrupt_message"]

    def test_critical_finding_dead_seed_also_interrupts(self, monkeypatch):
        _patch_analysis(
            monkeypatch,
            {
                "fields": dict(FULL_COVERAGE_FIELDS),
                "critical_finding": {
                    "severity": "BLOCKER",
                    "finding": "TARGET PRODUCT URL IS DEAD — returns HTTP 404",
                },
            },
        )
        cmd = vc_mod.validate_coverage(_state())
        assert cmd.goto == "human_approval"
        assert cmd.update["interrupt_reason"] == "dead_seed"

    def test_exhausted_retries_escalate_not_skip(self, monkeypatch):
        """At the retry cap the node must NOT skip to scraper_analyzer on a
        dead seed (that is exactly the loop 335 died in) — it interrupts."""
        _patch_analysis(
            monkeypatch,
            {
                "verdict": "TARGET_URL_DEAD",
                "fields": dict(FULL_COVERAGE_FIELDS),
            },
        )
        cmd = vc_mod.validate_coverage(
            _state(product_analysis_retries=vc_mod.MAX_VALIDATE_RETRIES)
        )
        assert cmd.goto == "human_approval"
        assert cmd.update["interrupt_reason"] == "dead_seed_exhausted"
        assert "Continue anyway" in cmd.update["interrupt_options"]
        assert "Abort" in cmd.update["interrupt_options"]

    def test_dead_seed_interrupt_bumps_retry_counter(self, monkeypatch):
        _patch_analysis(
            monkeypatch,
            {"verdict": "TARGET_URL_DEAD", "fields": dict(FULL_COVERAGE_FIELDS)},
        )
        cmd = vc_mod.validate_coverage(_state(product_analysis_retries=0))
        assert cmd.update["product_analysis_retries"] == 1
        assert cmd.update["interrupt_reason"] == "dead_seed"


class TestHealthyPathUnchanged:
    def test_full_coverage_without_verdict_still_passes(self, monkeypatch):
        _patch_analysis(
            monkeypatch,
            {"fields": dict(FULL_COVERAGE_FIELDS)},
        )
        cmd = vc_mod.validate_coverage(_state())
        assert cmd.goto == "scraper_analyzer"
        assert cmd.update["error_message"] == ""

    def test_unrelated_verdict_value_ignored(self, monkeypatch):
        """A content verdict that is NOT about a dead seed (e.g. OK/SPA)
        must not trigger the arm."""
        _patch_analysis(
            monkeypatch,
            {
                "verdict": "OK",
                "fields": dict(FULL_COVERAGE_FIELDS),
            },
        )
        cmd = vc_mod.validate_coverage(_state())
        assert cmd.goto == "scraper_analyzer"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
