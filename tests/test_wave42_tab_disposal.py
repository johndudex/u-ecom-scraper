"""[wave-42 T1] Post-walk tab disposal — hand the shared walk tab back EMPTY.

The idle-memory RCA: browser_traverse leaves the last job's listing page
resident on the shared MCP Chrome forever (the reaper keeps the OLDEST tabs
and the walk tab IS the oldest). T1 makes the wrapper navigate the tab to
about:blank after the result is computed, behind NAV_TAB_DISPOSAL (default
OFF — prod behavior identical until enabled).

Dev-verified 2026-09-24 against MCP 0.0.78: browser_navigate accepts
about:blank; a fresh one-shot session lands on the same tab; the W38-A1
gate treats about:blank as a navigation state that never feeds its counter.

Run inside the docker suite:
  docker compose exec -T django sh -c 'cd /app/webapp && pytest ../tests . -q'
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "webapp"))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django  # noqa: E402

django.setup()

import pytest  # noqa: E402

from experimental.nav_traversal import traversal as tv  # noqa: E402


class _Resp:
    def __init__(self, content):
        self.content = content


def _resp(obj):
    return _Resp(json.dumps(obj))


class _FakeTool:
    def __init__(self, name, fn):
        self.name, self._fn = name, fn
        self.calls: list[dict] = []

    def invoke(self, kwargs):
        self.calls.append(kwargs)
        return self._fn(kwargs)


def _surface(url, signals=None):
    return {"url": url, "title": "t", "clickables": [], "scroll_hint": False,
            "has_load_more": False, "signals": signals or {}}


def _make_tools(listing_url=None):
    """Fake toolset ending on a listing surface; the navigate tool records
    every invoke so tests can assert the disposal call's presence/absence
    and ORDER (about:blank strictly after the walk's own navigation).

    ev routes by the REAL JS payloads' unique markers (wave-38 test
    precedent) so capture helpers see empty logs/lists and never attempt a
    real HTTP fetch."""
    listing_url = listing_url or "https://www.westelm.com.au/bath"

    def ev_fn(kwargs):
        js = kwargs.get("function", "")
        if "getEntriesByType" in js:
            return _resp([])          # network resource log — empty
        if "commonPrefixDepth" in js:
            if "_commonPrefixDepth" in js:
                return _resp(_surface(listing_url))  # _PAGE_STATE_JS read
            return _resp([])          # _ITEM_LINKS_JS — no links
        return _resp({})

    nav = _FakeTool("playwright_browser_navigate", lambda kw: _Resp("ok"))
    ev = _FakeTool("playwright_browser_evaluate", ev_fn)
    noop = lambda kw: _Resp("ok")  # noqa: E731
    return nav, ev, [
        nav, _FakeTool("playwright_browser_click", noop), ev,
        _FakeTool("playwright_browser_wait_for", noop),
        _FakeTool("playwright_browser_snapshot", noop),
    ]


def _step_listing():
    def _fn(text, content_type, query, history):
        return {"is_listing": True, "reason": "test"}
    return _fn


WESTELM = "https://www.westelm.com.au/bath"


@pytest.fixture(autouse=True)
def _no_real_fetches(monkeypatch):
    """The listing path's API-capture builds platform-template probe URLs
    from the fixture domain (search.<registrable>/...) and really fetches
    them — minutes of DNS-fail retries per walk. Stub the fetcher: capture
    must not influence these tests (they pin the DISPOSAL, not capture)."""
    monkeypatch.setattr(
        tv, "_httpx_fetch",
        lambda *a, **k: {"ok": False, "status": 0, "final_url": "",
                         "text": "", "content_type": None},
    )


class TestTabDisposal:
    def test_flag_off_makes_no_disposal_call(self, monkeypatch):
        monkeypatch.delenv("NAV_TAB_DISPOSAL", raising=False)
        nav, _ev, tools = _make_tools()
        result = tv.browser_traverse(
            WESTELM, "product", "bath", mcp_tools=tools, step_fn=_step_listing(),
            max_actions=2,
        )
        assert result.reached is True
        # the walk's ONE navigate (start_url) — no trailing about:blank
        assert [c["url"] for c in nav.calls] == [WESTELM]

    def test_flag_on_disposes_to_about_blank_after_result(self, monkeypatch):
        monkeypatch.setenv("NAV_TAB_DISPOSAL", "1")
        nav, _ev, tools = _make_tools()
        result = tv.browser_traverse(
            WESTELM, "product", "bath", mcp_tools=tools, step_fn=_step_listing(),
            max_actions=2,
        )
        # the walk result is computed BEFORE disposal and unchanged by it
        assert result.reached is True
        assert result.goal_url == "https://www.westelm.com.au/bath"
        assert [c["url"] for c in nav.calls] == [WESTELM, "about:blank"]

    def test_disposal_failure_is_swallowed_result_intact(self, monkeypatch):
        monkeypatch.setenv("NAV_TAB_DISPOSAL", "1")
        nav, ev, tools = _make_tools()
        inner_ev = ev._fn

        # both disposal mechanisms raise — the wrapper must still return
        nav._fn = lambda kw: (
            (_ for _ in ()).throw(RuntimeError("cdp gone"))
            if kw.get("url") == "about:blank" else _Resp("ok")
        )

        def ev_fn(kwargs):
            if "location.replace" in kwargs.get("function", ""):
                raise RuntimeError("context destroyed")
            return inner_ev(kwargs)

        ev._fn = ev_fn
        result = tv.browser_traverse(
            WESTELM, "product", "bath", mcp_tools=tools, step_fn=_step_listing(),
            max_actions=2,
        )
        assert result.reached is True  # walk verdict untouched by disposal failure

    def test_disposal_on_not_reached_walk_too(self, monkeypatch):
        """The budget-exhausted path (the other big return site) disposes too."""
        monkeypatch.setenv("NAV_TAB_DISPOSAL", "1")
        nav, _ev, tools = _make_tools()

        def _never(text, content_type, query, history):
            return {"is_listing": False, "action": "done", "reason": "give up"}

        result = tv.browser_traverse(
            WESTELM, "product", "bath", mcp_tools=tools, step_fn=_never,
            max_actions=2,
        )
        assert result.reached is False
        assert nav.calls[-1]["url"] == "about:blank"

    def test_empty_tools_no_disposal_crash(self, monkeypatch):
        monkeypatch.setenv("NAV_TAB_DISPOSAL", "1")
        result = tv.browser_traverse(
            WESTELM, "product", "bath", mcp_tools=[], step_fn=_step_listing(),
            max_actions=2,
        )
        assert result.reached is False
        assert "MCP tools empty" in (result.notes or "")

    def test_flag_values(self, monkeypatch):
        """The gate reads the env LIVE: only truthy strings arm disposal."""
        nav, _ev, tools = _make_tools()
        for raw, expect in (("0", False), ("", False), ("false", False),
                            ("1", True), ("true", True), ("yes", True)):
            monkeypatch.setenv("NAV_TAB_DISPOSAL", raw)
            nav.calls.clear()
            tv.browser_traverse(
                WESTELM, "product", "bath", mcp_tools=tools,
                step_fn=_step_listing(), max_actions=2,
            )
            disposed = any(c.get("url") == "about:blank" for c in nav.calls)
            assert disposed is expect, raw
