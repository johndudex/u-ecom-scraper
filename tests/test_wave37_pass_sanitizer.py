"""[wave-37 W37-NEW-B] A PASS over zero real extracted items is not a PASS.

Prod 672 ended on PASS conf 0.25 with nothing extracted; the exhausted arm
refused execution (correctly — no items) and the job died on a passing
verdict. The tester's report boundary force-FAILs that shape (same idiom as
the discovery-probe force-FAIL arms, wave-22 B4 confidence preservation).
Items are read with the canonical extracted-count ladder
(successful_extractions/extracted_items/item_count, top-level AND nested
under results) — the code-tester report contract's real keys.
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

from webapp.agents.graph import _sanitize_pass_verdict  # noqa: E402


def test_pass_with_zero_items_force_failed():
    rep = {
        "overall_assessment": "PASS",
        "confidence_score": 0.25,
        "successful_extractions": 0,
    }
    out = _sanitize_pass_verdict(rep)
    assert out["overall_assessment"] == "FAIL"
    assert out["confidence_score"] == 0.0
    assert out["phase2_confidence"] == 0.25  # wave-22 B4 preserved
    assert out["ready_for_execution"] is False
    assert any("PASS" in str(i.get("message", "")) for i in out["issues"])


def test_pass_with_items_untouched():
    rep = {
        "overall_assessment": "PASS",
        "confidence_score": 0.9,
        "successful_extractions": 10,
    }
    assert _sanitize_pass_verdict(rep) is rep


def test_pass_with_nested_items_untouched():
    rep = {
        "overall_assessment": "PASS",
        "confidence_score": 0.9,
        "results": {"successful_extractions": 4},
    }
    assert _sanitize_pass_verdict(rep) is rep


def test_pass_with_sample_products_untouched():
    rep = {
        "overall_assessment": "PASS",
        "confidence_score": 0.8,
        "sample_products": [{"title": "x"}],
    }
    assert _sanitize_pass_verdict(rep) is rep


def test_fail_reports_untouched():
    rep = {"overall_assessment": "FAIL", "successful_extractions": 0}
    assert _sanitize_pass_verdict(rep) is rep


def test_missing_verdict_untouched():
    rep = {"confidence_score": 0.5}
    assert _sanitize_pass_verdict(rep) is rep


def test_none_report_passthrough():
    assert _sanitize_pass_verdict(None) is None
