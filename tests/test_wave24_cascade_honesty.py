"""[wave-24 W24-7] Cascade labels must not lie about strategy switches.

Prod 394: the router logged ``[CASCADE] action=strategy-switch
strategy=playwright`` BEFORE scraper_analyzer picked — and scraper_analyzer
re-picked playwright (the anti-bot strategy authority can force the same
strategy back regardless of the ladder). The premature label fed cycle-2 a
template-rewrite framing ("Read the template at templates/... and use it as
your base") for what was actually an edit-to-the-existing-draft cycle —
directly feeding W24-4's full-write burns.

Honest order: the router's strategy arm logs the neutral ``strategy-ladder``
decision; scraper_analyzer (``_decide_strategy``) logs the RESOLUTION once
the pick exists — ``strategy-switch`` only when the strategy actually
changed, ``code-fix (strategy rerun)`` when it re-picked — and stamps
``strategy_rerun`` so the writer prompt gets edit-over-write framing.
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

import agents.graph as graph  # noqa: E402
from agents.nodes import route_after_testing as rat_mod  # noqa: E402
from agents.subagents import build_code_writer_message  # noqa: E402

import sys as _sys

rat = _sys.modules["agents.nodes.route_after_testing"]

SLUG = "wave24-rerun-test"


def _decide(monkeypatch, tmp_path, prior_strategy, derived_strategy):
    """Run the deterministic strategy node hermetically; return (command, rows)."""
    ws = tmp_path / "workspace" / SLUG
    ws.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(graph, "_derive_strategy", lambda state: {"strategy": derived_strategy})
    monkeypatch.setattr(graph, "_PATCHES_ENABLED", False)
    monkeypatch.setattr(
        graph, "_escalate_strategy",
        lambda analysis, tried, skip_approvals=False: (analysis, None),
    )
    monkeypatch.setattr(graph, "_notify_phase", lambda *a, **k: None)
    monkeypatch.setattr(graph, "_get_project_root", lambda: str(tmp_path))
    rows = []
    monkeypatch.setattr(
        graph, "_log_event_row", lambda job_id, agent, content: rows.append(content)
    )
    state = {
        "job_id": 1,
        "site_slug": SLUG,
        "test_report": {"overall_assessment": "FAIL", "issues": []},
    }
    if prior_strategy:
        state["scraper_analysis"] = {"strategy": prior_strategy}
    result = graph._decide_strategy(state)
    return result, rows


class TestStrategyResolutionLogging:
    def test_same_pick_logged_as_rerun(self, tmp_path, monkeypatch):
        result, rows = _decide(monkeypatch, tmp_path, "playwright", "playwright")
        assert result.update["strategy_rerun"] is True
        assert any("code-fix (strategy rerun)" in r for r in rows)
        assert not any("action=strategy-switch" in r for r in rows)

    def test_changed_pick_logged_as_strategy_switch(self, tmp_path, monkeypatch):
        result, rows = _decide(monkeypatch, tmp_path, "http_requests", "playwright")
        assert result.update["strategy_rerun"] is False
        assert any("action=strategy-switch" in r for r in rows)
        assert not any("strategy rerun" in r for r in rows)

    def test_first_pick_logs_no_cascade_row(self, tmp_path, monkeypatch):
        _result, rows = _decide(monkeypatch, tmp_path, "", "http_navigation")
        assert rows == [], "no prior strategy → nothing to resolve honestly"


class TestRouterNeutralLabel:
    def test_strategy_arm_logs_ladder_not_switch(self, monkeypatch):
        """The router cannot know the pick — its row must not claim one."""
        actions = []
        monkeypatch.setattr(
            rat, "_log_cascade",
            lambda state, action, reason: actions.append(action),
        )
        state = {
            "job_id": 0,
            "site_slug": "no-such-workspace-slug",
            "input_mode": "navigation",
            "test_retry_count": 0,
            "test_report": {
                "overall_assessment": "FAIL",
                "confidence_score": 0.1,
                "issues": [],
                "discovery_coverage": {
                    "ran_phase1": True, "discovered_urls": 0, "found": 0,
                    "stop_reason": "empty_render",
                },
            },
        }
        assert rat.route_after_testing(state) == "scraper_analyzer"
        assert actions and actions[-1] == "strategy-ladder"


class TestRerunWriterFraming:
    def test_rerun_cycle_gets_edit_mode_not_template_rewrite(self):
        state = {
            "url": "https://x.com",
            "site_slug": "x-com",
            "sample_url": "https://x.com/p/1",
            "input_mode": "list_page",
            "scraper_analysis": {"strategy": "http_navigation"},
            "strategy_rerun": True,
        }
        msgs = build_code_writer_message(state)
        text = "\n".join(
            getattr(m, "content", "") or "" for m in (msgs if isinstance(msgs, list) else [msgs])
        )
        assert "EDIT MODE" in text, "rerun cycle must get edit-over-write framing"
        assert (
            "Read the template at: templates/http_navigation_scraper.py" not in text
        ), "template-rewrite hint is exactly the 394 lie"

    def test_real_switch_keeps_template_rewrite_hint(self):
        state = {
            "url": "https://x.com",
            "site_slug": "x-com",
            "sample_url": "https://x.com/p/1",
            "input_mode": "list_page",
            "scraper_analysis": {"strategy": "http_navigation"},
            "strategy_rerun": False,
        }
        msgs = build_code_writer_message(state)
        text = "\n".join(
            getattr(m, "content", "") or "" for m in (msgs if isinstance(msgs, list) else [msgs])
        )
        assert "Read the template at: templates/http_navigation_scraper.py" in text
