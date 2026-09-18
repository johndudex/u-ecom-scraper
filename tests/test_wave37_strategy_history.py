"""[wave-37 W37-NEW-E] Strategy history must accumulate on EVERY failed
cycle so the deterministic re-derivation can never re-pick a tried+failed
strategy (the scan's playwright→playwright no-op remap, prod 671/672).

Prod signature (671 seq 170-171, 672 seq 126-127):
  [CASCADE] action=strategy-ladder … reason=no items extracted — likely wrong strategy
  [CASCADE] action=code-fix (strategy rerun) strategy=playwright→playwright
            reason=scraper_analyzer re-picked the same strategy

The leak: _decide_strategy's tried-append gate requires
``overall_assessment not in (None, "PASS")`` — but a false PASS over zero
extracted items (672's PASS conf 0.25) IS a failed cycle. The gate skips
it, strategies_tried stays empty, escalation sees nothing tried, and the
derived playwright is re-picked verbatim.
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

from webapp.agents import graph  # noqa: E402


@pytest.fixture(autouse=True)
def _quiet_graph(monkeypatch, tmp_path):
    """No DB, no anti-bot rewrite, artifact write into tmp."""
    monkeypatch.setattr(graph, "_notify_phase", lambda *a, **k: None)
    monkeypatch.setattr(graph, "_log_event_row", lambda *a, **k: None)
    monkeypatch.setattr(graph, "_PATCHES_ENABLED", False)
    monkeypatch.setattr(graph, "_get_project_root", lambda: str(tmp_path))
    (tmp_path / "workspace" / "acme-com").mkdir(parents=True, exist_ok=True)


def _state(report, tried=None):
    return {
        "job_id": 0,
        "site_slug": "acme-com",
        "input_mode": "url_list",
        "skip_approvals": True,
        "probe_result": {"connectivity": {"method_that_worked": "browser"}},
        "strategies_tried": list(tried or []),
        "scraper_analysis": {"strategy": "playwright"},
        "test_report": report,
    }


def _run(state):
    out = graph._decide_strategy(state)
    if hasattr(out, "update"):
        return dict(out.update or {}), getattr(out, "goto", None)
    return dict(out or {}), None


def test_false_pass_zero_items_never_noop_reruns(monkeypatch, tmp_path):
    # Exact 671/672 shape: http_navigation already tried+failed in an earlier
    # cycle; escalation moved to playwright; the playwright cycle then died on
    # a false PASS (conf 0.25, zero items). The append gate skips PASS
    # reports → playwright is never recorded → escalation derives
    # http_navigation, finds it tried, escalates right back onto the
    # playwright that just failed.
    update, goto = _run(_state({
        "overall_assessment": "PASS",
        "confidence_score": 0.25,
        "successful_extractions": 0,
    }, tried=[{"strategy": "http_navigation", "reason": "test FAILED"}]))
    analysis = update.get("scraper_analysis") or {}
    noop_rerun = (
        analysis.get("strategy") == "playwright"
        and not update.get("strategies_tried")
        and goto in (None, "code_writer")
    )
    assert not noop_rerun, (
        f"no-op remap reproduced: playwright re-picked with empty history "
        f"(goto={goto!r}, tried={update.get('strategies_tried')!r})"
    )


def test_failed_report_records_strategy():
    update, goto = _run(_state({
        "overall_assessment": "NEEDS_FIXES",
        "confidence_score": 0.3,
        "successful_extractions": 0,
    }))
    tried = {
        (t.get("strategy") if isinstance(t, dict) else t)
        for t in (update.get("strategies_tried") or [])
    }
    assert "playwright" in tried, f"failed cycle not recorded: tried={tried!r}"


def test_pass_with_items_not_recorded():
    update, goto = _run(_state({
        "overall_assessment": "PASS",
        "confidence_score": 0.9,
        "successful_extractions": 5,
    }))
    assert not update.get("strategies_tried"), (
        "a PASSING cycle must not poison strategy history"
    )
    assert goto in (None, "code_writer")
