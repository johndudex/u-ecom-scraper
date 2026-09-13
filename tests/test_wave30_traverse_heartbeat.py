"""[wave-30 W30-8] browser_traverse heartbeat + wall-clock ceiling.

Prod proof (job 569): the MCP browser walk has NO deadline. When the site
keeps the walk alive (slow renders, LLM turns at 145-161s each), the
traversal runs silent for the better part of an hour — SessionLog shows
nothing between "browser_traverse running" and the eventual verdict, and
operators cannot tell a healthy walk from a wedged one. The plan's audit
confirmed: no deadline exists on the traversal path today.

Contract:
1. Heartbeat: ``browser_traverse`` calls ``heartbeat_fn`` at most every
   ``heartbeat_interval`` seconds (default 300) as the walk progresses;
   the graph's writer closure turns each call into one ``[NAV-TRAVERSE]``
   SessionLog row (agent-heartbeat idiom).
2. Ceiling: ``NAV_TRAVERSE_MAX_TIMEOUT`` (default 3600s) is a HARD stop —
   checked at the top of each step so a walk never begins another 150s+
   LLM turn past the ceiling. The stop is HONEST: not-reached result whose
   notes name the wall-clock ceiling (never disguised as "budget
   exhausted"), with the standard listing_reached=False discovery contract.
3. Short traverses are unchanged: same notes text as before when the action
   budget ends the walk; heartbeat_fn exceptions never break the walk.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

trv = importlib.import_module("experimental.nav_traversal.traversal")  # noqa: E402


class _FakeTool:
    def __init__(self, name, fn):
        self.name = name
        self._fn = fn

    def invoke(self, kwargs):
        return self._fn(kwargs)


class _FakeResp:
    def __init__(self, content):
        self.content = content


class _StateBrowser:
    """MCP browser whose evaluate returns scripted page-state surfaces."""

    def __init__(self, surfaces):
        self.surfaces = list(surfaces)
        self.nav_calls: list[str] = []

    def tools(self):
        return [
            _FakeTool("playwright_browser_navigate", self._nav),
            _FakeTool("playwright_browser_click", lambda kw: None),
            _FakeTool("playwright_browser_evaluate", self._eval),
            _FakeTool("playwright_browser_wait_for", lambda kw: None),
            _FakeTool("playwright_browser_snapshot", lambda kw: _FakeResp("")),
        ]

    def _nav(self, kw):
        self.nav_calls.append(kw.get("url", ""))

    def _eval(self, kw):
        if self.surfaces:
            return _FakeResp(json.dumps(self.surfaces.pop(0)))
        return _FakeResp(json.dumps({"url": "https://s.example/", "signals": {}}))


def _surf(url="https://s.example/"):
    return {"url": url, "signals": {"results_items": 0, "reached": False}, "clickables": []}


def _clock(monkeypatch, start=0.0):
    t = {"v": start}
    monkeypatch.setattr(trv, "_now", lambda: t["v"])
    return t


def _stop_step(burn_s):
    """A step_fn that burns wall-clock then ends the walk."""
    def _step(snap, ct, q, h):
        nonlocal_burn[0]()
        return {"is_listing": False, "action": "done", "target": None, "reason": "stop"}
    nonlocal_burn = [lambda: None]
    # wire the burn against the shared clock dict at call time
    def set_clock(t):
        nonlocal_burn[0] = lambda: t.__setitem__("v", t["v"] + burn_s)
    _step._set_clock = set_clock
    return _step


class TestHeartbeat:
    def test_beat_fires_once_per_interval(self, monkeypatch):
        t = _clock(monkeypatch)
        browser = _StateBrowser([_surf(), _surf(), _surf()])
        beats: list = []

        def step(snap, ct, q, h):
            t["v"] += 400.0  # each LLM turn burns > the 300s interval
            return {"is_listing": False, "action": "done", "target": None, "reason": "stop"}

        res = trv.browser_traverse(
            "https://s.example/", "product", "",
            mcp_tools=browser.tools(), step_fn=step,
            heartbeat_fn=beats.append, heartbeat_interval=300.0, max_actions=3,
        )
        assert res.reached is False
        assert len(beats) >= 1, (
            "a walk whose turns each outlast the interval must emit heartbeats — "
            "569 ran silent for the better part of an hour"
        )
        assert beats[0]["step"] == 0
        assert beats[0]["elapsed_s"] >= 300.0

    def test_no_beat_below_interval(self, monkeypatch):
        t = _clock(monkeypatch)
        browser = _StateBrowser([_surf(), _surf()])

        def step(snap, ct, q, h):
            t["v"] += 10.0  # fast turns: never crosses 300s
            return {"is_listing": False, "action": "done", "target": None, "reason": "stop"}

        beats: list = []
        trv.browser_traverse(
            "https://s.example/", "product", "",
            mcp_tools=browser.tools(), step_fn=step,
            heartbeat_fn=beats.append, heartbeat_interval=300.0, max_actions=2,
        )
        assert beats == []

    def test_heartbeat_exception_never_breaks_the_walk(self, monkeypatch):
        t = _clock(monkeypatch)
        browser = _StateBrowser([_surf(), _surf()])

        def step(snap, ct, q, h):
            t["v"] += 400.0
            return {"is_listing": False, "action": "done", "target": None, "reason": "stop"}

        def bad_beat(info):
            raise RuntimeError("session log down")

        res = trv.browser_traverse(
            "https://s.example/", "product", "",
            mcp_tools=browser.tools(), step_fn=step,
            heartbeat_fn=bad_beat, heartbeat_interval=300.0, max_actions=2,
        )
        assert res.reached is False and res.notes.startswith("budget exhausted"), (
            "telemetry failure must never change traversal behavior"
        )


class TestCeiling:
    def test_ceiling_stops_honestly(self, monkeypatch):
        t = _clock(monkeypatch)
        browser = _StateBrowser([_surf() for _ in range(6)])
        steps: list = []

        def step(snap, ct, q, h):
            steps.append(len(h))
            t["v"] += 500.0  # two turns ≈ 1000s > the 900s ceiling
            return {"is_listing": False, "action": "click", "target": "Shop", "reason": "keep going"}

        res = trv.browser_traverse(
            "https://s.example/", "product", "",
            mcp_tools=browser.tools(), step_fn=step,
            max_seconds=900.0, max_actions=10,
        )
        assert res.reached is False
        assert len(steps) == 2, (
            "the ceiling is checked at the TOP of each step: no new 150s+ LLM "
            "turn may begin once the walk is past it"
        )
        assert "ceiling" in res.notes.lower(), (
            "the stop must be honest — never disguised as budget exhaustion"
        )
        assert "budget exhausted" not in res.notes
        assert res.discovery.get("listing_reached") is False, (
            "a ceiling stop keeps the standard not-reached discovery contract"
        )

    def test_ceiling_env_override(self, monkeypatch):
        t = _clock(monkeypatch)
        monkeypatch.setenv("NAV_TRAVERSE_MAX_TIMEOUT", "123")
        browser = _StateBrowser([_surf() for _ in range(4)])

        def step(snap, ct, q, h):
            t["v"] += 200.0  # one turn already crosses the 123s ceiling
            return {"is_listing": False, "action": "click", "target": "Shop", "reason": "go"}

        res = trv.browser_traverse(
            "https://s.example/", "product", "",
            mcp_tools=browser.tools(), step_fn=step, max_actions=10,
        )
        assert "ceiling" in res.notes.lower()
        assert "123" in res.notes

    def test_short_traverse_unchanged(self, monkeypatch):
        _clock(monkeypatch)
        monkeypatch.delenv("NAV_TRAVERSE_MAX_TIMEOUT", raising=False)
        browser = _StateBrowser([_surf(), _surf()])

        def step(snap, ct, q, h):
            return {"is_listing": False, "action": "done", "target": None, "reason": "give up"}

        res = trv.browser_traverse(
            "https://s.example/", "product", "test",
            mcp_tools=browser.tools(), step_fn=step, max_actions=2,
        )
        assert res.reached is False
        assert res.notes.startswith("budget exhausted"), (
            "a walk ended by the action budget keeps its existing notes text"
        )


class TestGraphWiring:
    @pytest.fixture()
    def db_fakes(self, monkeypatch):
        import scraper.models as sm

        class FakeManager:
            def __init__(self):
                self.rows = []

            def filter(self, **kw):
                return self

            def count(self):
                return len(self.rows)

            def create(self, **kw):
                self.rows.append(kw)
                return types.SimpleNamespace(**kw)

        class FakeModel:
            ROLE_SYSTEM = "system"

            def __init__(self):
                self.objects = FakeManager()

        sl = FakeModel()
        monkeypatch.setattr(sm, "SessionLog", sl)
        return sl.objects

    def test_writer_closure_writes_nav_traverse_row(self, db_fakes):
        from agents.graph import _traverse_heartbeat_writer

        _traverse_heartbeat_writer(77)(
            {"step": 2, "elapsed_s": 631.0, "actions": 2, "reason": "step"}
        )
        assert len(db_fakes.rows) == 1
        row = db_fakes.rows[0]
        assert row["job_id"] == 77
        assert row["agent"] == "browser_traverse"
        assert row["role"] == "system"
        assert "[NAV-TRAVERSE]" in row["content"]
        assert "631" in row["content"]

    def test_writer_closure_swallows_db_failure(self, monkeypatch):
        from agents.graph import _traverse_heartbeat_writer

        class Boom:
            ROLE_SYSTEM = "system"

            class objects:
                @staticmethod
                def filter(**kw):
                    raise RuntimeError("db down")

        import scraper.models as sm

        monkeypatch.setattr(sm, "SessionLog", Boom)
        # must not raise
        _traverse_heartbeat_writer(77)(
            {"step": 1, "elapsed_s": 320.0, "actions": 1, "reason": "step"}
        )

    def test_wrapper_passes_heartbeat_to_traverse(self):
        src = open(os.path.join(ROOT, "webapp", "agents", "graph.py")).read()
        i_fn = src.index("def _invoke_navigation_traverse(")
        i_next = src.index("\ndef ", i_fn + 10)
        body = src[i_fn:i_next]
        assert "heartbeat_fn=" in body, (
            "the graph wrapper must wire the SessionLog heartbeat into "
            "browser_traverse (the walk is otherwise invisible)"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
