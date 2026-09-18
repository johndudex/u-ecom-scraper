"""[wave-38] Shared-browser isolation: in-walk guards (T1 abort, T2 quiesce,
T3 capture filters).

The 412 bleed: the MCP one-shot session always lands on the context's OLDEST
tab, so a concurrent job's in-flight goto was read, judged is_listing=True,
and captured (its API + item links) — all upstream nets, nothing in-walk.
These tests pin the in-walk defenses. Run inside the docker suite:
  docker compose exec -T django sh -c 'cd /app/webapp && pytest ../tests . -q'
"""
from __future__ import annotations

import json
import os
import sys
import time

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

    def invoke(self, kwargs):
        return self._fn(kwargs)


def _surface(url, signals=None):
    return {"url": url, "title": "t", "clickables": [], "scroll_hint": False,
            "has_load_more": False, "signals": signals or {}}


WESTELM = "https://www.westelm.com.au/bath"
RTR = "https://www.renttherunway.com/collections"


def _make_tools(surfaces, network="[]", item_links="[]"):
    """One fake MCP toolset. surfaces[i] is the i-th _PAGE_STATE_JS read (an
    Exception instance = raise). Later reads clamp to the last entry.

    JS routing by unique markers in the REAL payloads: getEntriesByType =
    network resource log; _commonPrefixDepth (underscored helper) =
    _PAGE_STATE_JS; bare commonPrefixDepth = _ITEM_LINKS_JS (its helper is
    NOT underscored — checked against traversal.py, the plan's original
    single marker collided with the page-state JS)."""
    reads = {"n": 0}

    def ev_fn(kwargs):
        js = kwargs.get("function", "")
        if "getEntriesByType" in js:
            return _Resp(network)
        if "_commonPrefixDepth" in js:
            i = reads["n"]
            reads["n"] += 1
            s = surfaces[min(i, len(surfaces) - 1)]
            if isinstance(s, Exception):
                raise s
            return _resp(s)
        if "commonPrefixDepth" in js:
            return _Resp(item_links)
        return _Resp("{}")

    def noop(kwargs):
        return _Resp("ok")

    return [
        _FakeTool("playwright_browser_navigate", noop),
        _FakeTool("playwright_browser_click", noop),
        _FakeTool("playwright_browser_evaluate", ev_fn),
        _FakeTool("playwright_browser_wait_for", noop),
        _FakeTool("playwright_browser_snapshot", noop),
    ]


def _step(action="click", listing=False):
    def _fn(text, content_type, query, history):
        return {"is_listing": listing, "action": action,
                "target": "Shop", "reason": "test"}
    return _fn


_REAL_STABLE = getattr(tv, "wait_for_stable_page", None)  # pre-stub ref (None until T2 lands)
_REAL_CAPTURE = tv._capture_api_from_session  # pre-stub refs: the _hermetic
_REAL_LINKS = tv._extract_item_links          # fixture stubs these, but T3's
# TestCaptureFilters exercises the REAL functions directly against fake evs.


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """No LLM, no network, no real capture, no quiesce ev-consumption:
    walks are pure state machines over the scripted surfaces. (The real
    wait_for_stable_page would CONSUME _PAGE_STATE_JS reads from the fake
    ev, shifting every scripted sequence — stub it; TestWaitForStablePage
    calls the captured _REAL_STABLE directly. raising=False: the attr only
    exists once T2 has landed.)"""
    monkeypatch.setattr(tv, "_do_action", lambda *a, **k: "acted")
    monkeypatch.setattr(tv, "_capture_api_from_session", lambda *a, **k: None)
    monkeypatch.setattr(tv, "_extract_item_links", lambda *a, **k: [])
    monkeypatch.setattr(tv, "wait_for_stable_page", lambda *a, **k: True,
                        raising=False)
    monkeypatch.setattr(tv, "llm_step", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("llm_step must not run under test")))


def _walk(monkeypatch, surfaces, step, **kwargs):
    return tv.browser_traverse(
        RTR, "product", "dress", mcp_tools=_make_tools(surfaces),
        step_fn=step, max_actions=6, **kwargs)


class TestWrongSiteGate:
    def test_first_read_listing_judgment_aborts_immediately(self, monkeypatch):
        """THE 412 REGRESSION TEST. The incident was a FIRST off-domain read
        judged is_listing=True and captured — a tolerance counter never
        reaches 2 on that shape. A listing judgment on an off-domain surface
        aborts NOW: no capture, no action on the wrong site."""
        actions = []
        monkeypatch.setattr(
            tv, "_do_action", lambda *a, **k: actions.append("x") or "x")
        result = _walk(monkeypatch, [_surface(WESTELM)], _step(listing=True))
        assert result.reached is False
        assert result.wrong_site_abort is True
        assert result.wrong_site_url == WESTELM
        assert result.discovery["listing_reached"] is False
        assert actions == [], "must not click around someone else's site"

    def test_off_domain_non_listing_reads_abort_at_tolerance(self, monkeypatch):
        """Tier 2: non-listing off-domain reads (consent-wall flavor)
        tolerate 2 consecutive; the second aborts before a second action."""
        result = _walk(monkeypatch, [_surface(WESTELM), _surface(WESTELM)],
                       _step())
        assert result.wrong_site_abort is True
        assert result.wrong_site_url == WESTELM
        assert result.discovery["listing_reached"] is False

    def test_abort_notes_never_contain_mcp(self, monkeypatch):
        """Routing contract: 'MCP' in notes sends _invoke_navigation_traverse
        into the navigate_explore fallback — which drives the SAME dirty
        browser. The abort note must never say MCP."""
        result = _walk(monkeypatch, [_surface(WESTELM)], _step(listing=True))
        assert "MCP" not in (result.notes or "")
        assert "wrong-site" in (result.notes or "")

    def test_single_off_domain_read_then_on_domain_recovers(self, monkeypatch):
        """D3 tier 2: one consent-wall read must NOT abort — the counter
        resets on the first on-domain read and the walk finds the listing."""
        calls = {"n": 0}

        def step(text, ct, q, history):
            calls["n"] += 1
            return {"is_listing": calls["n"] >= 2, "action": "click",
                    "target": "Shop", "reason": "test"}

        result = _walk(monkeypatch, [_surface(WESTELM), _surface(RTR)], step)
        assert result.reached is True
        assert result.wrong_site_abort is False

    def test_unclassifiable_surfaces_never_count_as_off_domain(
            self, monkeypatch):
        """about:blank / chrome-error:// are navigation states, not another
        site's content — a flaky site bouncing to chrome-error twice must
        not masquerade as a 412 bleed."""
        result = _walk(monkeypatch,
                       [_surface("about:blank"), _surface("about:blank")],
                       _step())
        assert result.wrong_site_abort is False

    def test_fields_default_off(self):
        """Existing positional constructions (10 call sites) must be
        untouched by the new fields."""
        r = tv.TraversalResult(False, None, ["u"], "unknown", None, {}, ["u"], [])
        assert r.wrong_site_abort is False
        assert r.wrong_site_url == ""

    def test_job_id_flows_through(self, monkeypatch):
        result = _walk(monkeypatch, [_surface(WESTELM)], _step(listing=True),
                       job_id=412)
        assert result.wrong_site_abort is True


class TestCaptureFilters:
    @staticmethod
    def _api(url, count):
        return {"url": url, "count": count, "sample_keys": ["x"]}

    def _run(self, monkeypatch, net_candidates, job_reg, verify_map):
        probed = []

        def fake_verify(cand, fetch, query):
            # REAL contract: api_from_network yields URL STRINGS
            # (traversal.api_from_network -> list[str]); tolerate dicts
            # defensively for non-network candidate shapes.
            u = cand.get("url") if isinstance(cand, dict) else str(cand)
            probed.append(u)
            return verify_map.get(u)

        monkeypatch.setattr(tv, "verify_api", fake_verify)
        monkeypatch.setattr(tv, "api_from_network",
                            lambda entries: list(net_candidates))
        monkeypatch.setattr(tv, "_httpx_fetch", lambda *a, **k: {"ok": False})
        ev = _FakeTool("playwright_browser_evaluate",
                       lambda kw: _Resp("[]"))
        api = _REAL_CAPTURE(
            ev, "https://www.renttherunway.com/collections", "dress",
            job_registrable=job_reg)
        return api, probed

    def test_off_domain_not_probed_when_on_domain_has_count(self, monkeypatch):
        """The bleed case: westelm's /api/items is real and count>0, but the
        JOB is renttherunway — the on-domain candidate wins and the
        off-domain one is never even probed (count-gated pass 2 skipped)."""
        api, probed = self._run(
            monkeypatch,
            ["https://www.renttherunway.com/api/items",
             "https://www.westelm.com.au/api/items"],
            "renttherunway.com",
            {"https://www.renttherunway.com/api/items": self._api(
                "https://www.renttherunway.com/api/items", 3)})
        assert not any("westelm" in u for u in probed), (
            "off-domain pass must be skipped when an on-domain candidate "
            "already returned count>0"
        )
        assert api and "renttherunway.com" in api["url"]

    def test_vendor_api_still_probed_when_on_domain_lacks_count(
            self, monkeypatch):
        """The aya class (wave-34 F1): a VENDOR-domain API with count>0 must
        stay reachable — on-domain candidates exist but verify count-less,
        so the off-domain pass MUST run and the count-ranking picks it. A
        naive pre-verify filter would have silently broken aya."""
        api, probed = self._run(
            monkeypatch,
            ["https://jobs.example.com/taxonomy",
             "https://vendor-jobs.io/search"],
            "jobs.example.com",
            {"https://jobs.example.com/taxonomy": self._api(
                "https://jobs.example.com/taxonomy", None),
             "https://vendor-jobs.io/search": self._api(
                 "https://vendor-jobs.io/search", 26803)})
        assert any("vendor-jobs.io" in u for u in probed)
        assert api and "vendor-jobs.io" in api["url"]

    def test_off_domain_selected_when_it_is_all_there_is(self, monkeypatch):
        """Pure bleed, no on-domain API at all: the wrong-site candidate is
        probed and returned — the graph's wave-34 F2 drop is the existing
        downstream net for exactly this residual."""
        api, probed = self._run(
            monkeypatch,
            ["https://www.westelm.com.au/api/items"],
            "renttherunway.com",
            {"https://www.westelm.com.au/api/items": self._api(
                "https://www.westelm.com.au/api/items", 40)})
        assert api and "westelm" in api["url"]

    def test_no_job_registrable_probes_everything(self, monkeypatch):
        """Backwards compat: empty anchor = today's behavior (both probed,
        count-ranking decides)."""
        urls = ("https://www.renttherunway.com/api/items",
                "https://www.westelm.com.au/api/items")
        api, probed = self._run(
            monkeypatch,
            list(urls), "",
            {u: self._api(u, 1) for u in urls})
        assert len(probed) == 2

    def test_item_links_filtered_by_job_domain(self, monkeypatch):
        # _extract_item_links regexes raw.content — it must be a JSON
        # STRING, never a bare Python list.
        ev = _FakeTool(
            "playwright_browser_evaluate",
            lambda kw: _Resp(json.dumps([
                "https://www.renttherunway.com/dresses/p/1",
                "https://www.westelm.com.au/p/2",
                "https://www.renttherunway.com/dresses/p/3",
            ])))
        out = _REAL_LINKS(ev, job_registrable="renttherunway.com")
        assert out == [
            "https://www.renttherunway.com/dresses/p/1",
            "https://www.renttherunway.com/dresses/p/3",
        ]

    def test_item_links_unfiltered_without_anchor(self, monkeypatch):
        ev = _FakeTool(
            "playwright_browser_evaluate",
            lambda kw: _Resp(json.dumps([
                "https://www.renttherunway.com/dresses/p/1",
                "https://www.westelm.com.au/p/2",
            ])))
        assert len(_REAL_LINKS(ev)) == 2

    def test_two_part_tld_anchor(self):
        """Same _registrable rule as T1.6 — .com.au subdomain matches its
        apex, so the JOB's own regional host is never its own off-domain."""
        assert tv._registrable("https://www.westelm.com.au/x") == \
            tv._registrable("https://westelm.com.au/y")


class TestWaitForStablePage:
    def _ev(self, surfaces):
        reads = {"n": 0}

        def ev_fn(kwargs):
            i = reads["n"]
            reads["n"] += 1
            # CYCLE, not clamp: a "churning" sequence must keep churning —
            # clamping to the last entry would let it settle and defeat
            # test_false_on_churning_urls_and_bounded.
            s = surfaces[i % len(surfaces)]
            if isinstance(s, Exception):
                raise s
            return _resp(s)

        return _FakeTool("playwright_browser_evaluate", ev_fn)

    def test_true_when_url_settles(self):
        ev = self._ev([_surface(RTR), _surface(RTR)])
        assert _REAL_STABLE(
            ev, timeout_s=5.0, stable_reads=2, poll_s=0.01) is True

    def test_false_on_churning_urls_and_bounded(self):
        t0 = time.monotonic()
        ev = self._ev([_surface(RTR + "/a"), _surface(RTR + "/b")])
        ok = _REAL_STABLE(ev, timeout_s=0.3, stable_reads=2, poll_s=0.05)
        assert ok is False
        assert time.monotonic() - t0 < 5.0, "must respect its timeout"

    def test_tool_errors_are_false_not_raise(self):
        ev = self._ev([RuntimeError("Execution context destroyed")])
        assert _REAL_STABLE(ev, timeout_s=1.0, poll_s=0.01) is False

    def test_none_ev_is_false(self):
        assert _REAL_STABLE(None) is False

    def test_quiesce_runs_at_walk_boundaries(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            tv, "wait_for_stable_page",
            lambda *a, **k: calls.append(k.get("timeout_s")) or True)
        _walk(monkeypatch, [_surface(RTR)], _step(action="click",
                                                  listing=True))
        assert len(calls) >= 2, (
            "quiesce must run at walk start AND before the is_listing capture"
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
