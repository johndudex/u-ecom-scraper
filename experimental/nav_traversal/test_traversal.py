"""Unit tests for the navigation-traversal prototype (LLM judge + HTTP MOCKED).

These prove the LOGIC (pruning, goal detection, mechanism deduction) without the
live site or the LLM. Run from repo root:

    python -m pytest experimental/nav_traversal/test_traversal.py -q
(or) python experimental/nav_traversal/test_traversal.py
"""

from __future__ import annotations

import json
import os
import sys

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from experimental.nav_traversal.traversal import traverse  # noqa: E402


# ─── Fake HTTP ──────────────────────────────────────────────────────────────

class FakeFetch:
    """Routes (METHOD, url) -> response text. Matches base URL (ignores query)."""

    def __init__(self, routes: dict):
        self.routes = routes  # {"GET url": text, "POST url": text}
        self.calls: list[tuple] = []

    def __call__(self, url, method="GET", params=None, data=None, timeout=20.0):
        method = (method or "GET").upper()
        base = url.split("?")[0]
        self.calls.append((method, base))
        for k in (f"{method} {url}", f"{method} {base}", f"GET {base}"):
            if k in self.routes:
                return {"ok": True, "status": 200, "final_url": url, "text": self.routes[k]}
        return {"ok": False, "status": 404, "final_url": url, "text": ""}


# ─── aya-style: homepage -> category page with an inline backend API ────────

AYA_HOMEPAGE = """
<html><body>
 <a href="/travel-nursing/">Travel</a>
 <a href="/healthcare-jobs/">Search jobs</a>
 <a href="/about/">About</a>
</body></html>
"""
AYA_CATEGORY = """
<html><head>
<script>
  var apiUrl = "https://api.ayahealthcare.com/AyaHealthcareWeb/job/search?limit=10";
</script>
</head><body><h1>Jobs</h1></body></html>
"""
AYA_API_JSON = '{"items": [{"jobID": 1, "title": "RN"}, {"jobID": 2, "title": "LPN"}, {"jobID": 3}, {"jobID": 4}, {"jobID": 5}], "count": 26889}'


def aya_judge(candidates, content_type, query, site_url):
    # marks "Search jobs" correct, "Travel"/"About" wrong
    ranking = []
    for c in candidates:
        wrong = any(w in (c.get("text", "") + c.get("href", "")).lower()
                    for w in ("travel", "about"))
        ranking.append({"url": c["href"], "verdict": "wrong" if wrong else "correct",
                        "confidence": 0.9, "reason": ""})
    return {"ranking": ranking}


def test_aya_finds_api_and_prunes_marketing():
    routes = {
        "GET https://www.ayahealthcare.com/": AYA_HOMEPAGE,
        "GET https://www.ayahealthcare.com": AYA_HOMEPAGE,  # rstrip('/') variant
        "GET https://www.ayahealthcare.com/healthcare-jobs/": AYA_CATEGORY,
        "GET https://api.ayahealthcare.com/AyaHealthcareWeb/job/search": AYA_API_JSON,
    }
    fetch = FakeFetch(routes)
    res = traverse(
        "https://www.ayahealthcare.com/", "job_posting", "nursing",
        fetch_fn=fetch, raw_fetch=fetch, judge_fn=aya_judge,
    )
    assert res.reached, f"should reach goal; visited={fetch.calls}"
    assert res.mechanism == "api", f"expected api, got {res.mechanism}"
    assert res.api and res.api["api_url"] == "https://api.ayahealthcare.com/AyaHealthcareWeb/job/search"
    assert res.api["count"] == 26889
    # the marketing page must NOT have been visited
    visited_urls = " ".join(f"{m} {u}" for m, u in fetch.calls)
    assert "travel-nursing" not in visited_urls, "marketing page should be pruned"


# ─── locumtenens-style: homepage -> QuickSearch form -> POST -> SSR results ─

LT_HOMEPAGE = """
<html><body>
 <a href="/Resources/JobSearch/QuickSearch">Search Jobs</a>
 <a href="/about/">About</a>
</body></html>
"""
LT_QUICKSEARCH = """
<html><body>
 <form id="qs" action="/Resources/JobSearch/QuickSearch" method="post">
   <select name="Disciplines"><option value="">Any</option><option value="1">Physician</option></select>
   <select name="Specialties"><option value="">Any</option><option value="339">Emergency Medicine</option><option value="340">IM</option></select>
   <select name="Locations"><option value="">Any</option><option value="AL">AL</option></select>
   <input type="submit" value="Search">
 </form>
</body></html>
"""
# 10 job-results-item cards (> RESULTS_GOAL_THRESHOLD of 8)
LT_RESULTS = "<html><body><ul>" + "".join(
    '<li class="job-results-item"><a class="job-link" href="/emergency-medicine-jobs/np/alabama/job-%d">Job %d</a></li>' % (i, i)
    for i in range(1, 11)
) + "</ul><div>1 - 10 of 3790</div></body></html>"


def lt_judge(candidates, content_type, query, site_url):
    ranking = []
    for c in candidates:
        wrong = "about" in (c.get("text", "") + c.get("href", "")).lower()
        ranking.append({"url": c["href"], "verdict": "wrong" if wrong else "correct",
                        "confidence": 0.9, "reason": ""})
    return {"ranking": ranking}


def test_locumtenens_finds_form_post_ssr():
    routes = {
        "GET https://www.locumtenens.com/": LT_HOMEPAGE,
        "GET https://www.locumtenens.com": LT_HOMEPAGE,
        "GET https://www.locumtenens.com/Resources/JobSearch/QuickSearch": LT_QUICKSEARCH,
        "POST https://www.locumtenens.com/Resources/JobSearch/QuickSearch": LT_RESULTS,
    }
    fetch = FakeFetch(routes)
    res = traverse(
        "https://www.locumtenens.com/", "job_posting", "physician",
        fetch_fn=fetch, raw_fetch=fetch, judge_fn=lt_judge,
    )
    assert res.reached, f"should reach goal; visited={fetch.calls}"
    assert res.mechanism == "http_requests", f"expected http_requests, got {res.mechanism}"
    assert res.goal_method.upper() == "POST"
    assert res.goal_request_url.endswith("/Resources/JobSearch/QuickSearch")
    # the form POST was fired (with a Specialties value)
    assert ("POST", "https://www.locumtenens.com/Resources/JobSearch/QuickSearch") in fetch.calls
    assert "Specialties" in res.goal_data


def test_budget_exhaustion_reports_best_partial():
    # a site with only marketing links, no listing anywhere -> not reached, partial returned
    nopage = "<html><body><a href='/x/'>X</a></body></html>"
    routes = {"GET https://noplace.com/": nopage, "GET https://noplace.com": nopage,
              "GET https://noplace.com/x/": "<html><body>nothing</body></html>"}
    fetch = FakeFetch(routes)
    res = traverse("https://noplace.com/", "job_posting", "x",
                   fetch_fn=fetch, raw_fetch=fetch,
                   judge_fn=lambda c, ct, q, s: {"ranking": [{"url": x["href"], "verdict": "correct"} for x in c]})
    assert res.reached is False
    assert res.visited  # did visit some pages


# ─── Browser-driven traversal (snapshot + LLM) ─────────────────────────────

from experimental.nav_traversal.traversal import browser_traverse  # noqa: E402


class _FakeTool:
    """Mock MCP tool."""
    def __init__(self, name, fn):
        self.name = name
        self._fn = fn

    def invoke(self, kwargs):
        return self._fn(kwargs)


class _FakeResp:
    def __init__(self, content):
        self.content = content


class FakeSnapBrowser:
    """Mock MCP browser with snapshot support."""

    def __init__(self, snapshots: list[str]):
        self.snapshots = snapshots
        self.snap_idx = 0
        self.click_refs: list[str] = []
        self.nav_calls: list[str] = []
        self.scroll_count = 0

    def make_tools(self):
        return [
            _FakeTool("playwright_browser_navigate", self._nav),
            _FakeTool("playwright_browser_click", self._click),
            _FakeTool("playwright_browser_evaluate", self._eval),
            _FakeTool("playwright_browser_wait_for", lambda kw: None),
            _FakeTool("playwright_browser_snapshot", self._snap),
        ]

    def _nav(self, kw):
        self.nav_calls.append(kw.get("url", ""))

    def _click(self, kw):
        self.click_refs.append(kw.get("target", ""))

    def _eval(self, kw):
        fn = kw.get("function", "")
        if "scrollBy" in fn:
            self.scroll_count += 1
        if "location.href" in fn:
            return _FakeResp('"https://myntra.com/watches"')
        return _FakeResp("ok")

    def _snap(self, kw):
        idx = min(self.snap_idx, len(self.snapshots) - 1)
        self.snap_idx += 1
        return _FakeResp(self.snapshots[idx])


def test_browser_myntra_mega_menu():
    """LLM navigates a mega-menu: click Men → Watches appears → click → listing."""
    snaps = [
        # step 0: homepage — nav has Men, Watches not visible
        "- link 'Men' [ref=e5]\n- link 'Women' [ref=e6]\n- link 'Sale' [ref=e7]",
        # step 1: after clicking Men — mega-menu expanded, Watches visible
        "- link 'Men' [ref=e5]\n  - link 'Watches' [ref=e12]\n  - link 'T-shirts' [ref=e13]",
        # step 2: watches listing — many products visible
        "- heading 'Watches' [ref=e2]\n- list [ref=e3]:\n"
        "  - product 'Fastrack Watch' [ref=e4]\n  - product 'Titan Watch' [ref=e5]\n  - product 'Casio Watch' [ref=e6]",
    ]
    fb = FakeSnapBrowser(snaps)
    # now uses TEXT labels (not refs) — refs expire on live pages
    steps = [
        {"is_listing": False, "action": "click", "target": "Men", "reason": "click Men to expand"},
        {"is_listing": False, "action": "click", "target": "Watches", "reason": "click Watches"},
        {"is_listing": True, "action": "done", "target": "", "reason": "many watches visible"},
    ]
    step_fn = lambda snap, ct, q, h: steps[len(h)] if len(h) < len(steps) else {"is_listing": False, "action": "done", "target": ""}

    res = browser_traverse("https://myntra.com/", "product", "watches",
                           mcp_tools=fb.make_tools(), step_fn=step_fn, max_actions=5)
    assert res.reached, f"should reach; path={res.path}"


def test_browser_reached_on_first_snapshot():
    """Listing detected immediately — no actions needed."""
    snaps = ["- heading 'Shop All' [ref=e1]\n- list [ref=e2]:\n  - product 'A' [ref=e3]\n  - product 'B' [ref=e4]"]
    fb = FakeSnapBrowser(snaps)
    step_fn = lambda *a: {"is_listing": True, "action": "done", "target": None, "reason": "products visible"}
    res = browser_traverse("https://shop.example/", "product", "",
                           mcp_tools=fb.make_tools(), step_fn=step_fn, max_actions=3)
    assert res.reached


def test_browser_budget_exhausted():
    """Never reaches listing → returns not-reached."""
    snaps = ["- heading 'About Us' [ref=e1]"]
    fb = FakeSnapBrowser(snaps)
    step_fn = lambda *a: {"is_listing": False, "action": "done", "target": None, "reason": "give up"}
    res = browser_traverse("https://about.example/", "product", "test",
                           mcp_tools=fb.make_tools(), step_fn=step_fn, max_actions=2)
    assert res.reached is False


# ─── verify_api: select-option guard (F17) + content-type capture (F7) ──────

from types import SimpleNamespace  # noqa: E402

import httpx as _httpx  # noqa: E402

import experimental.nav_traversal.traversal as traversal_mod  # noqa: E402
from experimental.nav_traversal.traversal import _httpx_fetch, verify_api  # noqa: E402

# The real sidley payload (workspace/sidley-com/navigation_analysis.json):
# a people-directory FACET response, records shaped {text, value, count}.
SIDLEY_PEOPLE_SEARCH = json.dumps([
    {"text": "Attorneys", "value": "attorneys", "count": 2145},
    {"text": "Counsel", "value": "counsel", "count": 318},
    {"text": "Partners", "value": "partners", "count": 96},
])

AYA_SEARCH = ('{"items": ['
              '{"jobID": 1, "title": "RN 1"}, {"jobID": 2, "title": "RN 2"}, '
              '{"jobID": 3, "title": "RN 3"}, {"jobID": 4, "title": "RN 4"}, '
              '{"jobID": 5, "title": "RN 5"}], "count": 26889}')


def _fetch_ok(body: str, content_type: str | None = None):
    """Minimal fetch_fn: fixed body for every URL/params (no network)."""
    def _fetch(url, method="GET", params=None, data=None, timeout=20.0):
        r = {"ok": True, "status": 200, "final_url": url, "text": body}
        if content_type is not None:
            r["content_type"] = content_type
        return r
    return _fetch


class _FakeClient:
    """httpx.Client stand-in returning one canned response."""

    def __init__(self, resp):
        self._resp = resp

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, params=None):
        return self._resp

    def post(self, url, data=None):
        return self._resp


def _patch_httpx(monkeypatch, resp):
    """Point traversal's httpx at a canned response (module-local, no global bleed)."""
    monkeypatch.setattr(traversal_mod, "httpx",
                        SimpleNamespace(Client=lambda **kw: _FakeClient(resp)))


def test_select_option_guard_catches_count_bearing_facet_response():
    """F17: sidley's ["text","value","count"] facet payload must be REJECTED.

    The guard exists to reject select-option/taxonomy responses but omitted
    "count" — so a payload that reports a count could never be a subset of the
    key set and slipped through as a data API (sidley persisted
    data_source=api with items_per_page=100 for a dropdown taxonomy).
    """
    got = verify_api("https://www.sidley.com/sitecore/api/people/search",
                     _fetch_ok(SIDLEY_PEOPLE_SEARCH))
    assert got is None, f"facet taxonomy must be rejected, got {got}"


def test_guard_still_admits_real_records_that_carry_a_count_field():
    """Adding "count" must not reject a real data API whose records happen to
    carry a count-ish field alongside real entity fields."""
    got = verify_api("https://api.ayahealthcare.com/AyaHealthcareWeb/job/search",
                     _fetch_ok(AYA_SEARCH))
    assert got is not None, "real record list must not be rejected"
    assert got["count"] == 26889
    assert "title" in got["sample_keys"]


def test_httpx_fetch_returns_content_type(monkeypatch):
    """F7 (capture only): the response content-type must survive the fetch.

    Real httpx headers are case-insensitive — use httpx.Headers with the
    capitalized spelling to pin that the impl reads it case-insensitively.
    """
    resp = SimpleNamespace(
        status_code=200, text=AYA_SEARCH,
        url="https://api.ayahealthcare.com/AyaHealthcareWeb/job/search",
        headers=_httpx.Headers({"Content-Type": "application/json; charset=utf-8"}),
    )
    _patch_httpx(monkeypatch, resp)
    r = _httpx_fetch("https://api.ayahealthcare.com/AyaHealthcareWeb/job/search")
    assert r["ok"] is True
    assert r["content_type"] == "application/json; charset=utf-8"


def test_httpx_fetch_content_type_defaults_to_none_when_absent(monkeypatch):
    resp = SimpleNamespace(status_code=200, text="<html></html>",
                           url="https://example.com/", headers=_httpx.Headers({}))
    _patch_httpx(monkeypatch, resp)
    r = _httpx_fetch("https://example.com/")
    assert r["ok"] is True
    assert r["content_type"] is None


def test_httpx_fetch_error_result_also_carries_content_type(monkeypatch):
    """Both return paths carry the key, so downstream never sees a shape fork."""
    def _boom(**kw):
        raise RuntimeError("connect timeout")

    monkeypatch.setattr(traversal_mod, "httpx", SimpleNamespace(Client=_boom))
    r = _httpx_fetch("https://example.com/")
    assert r["ok"] is False
    assert r["content_type"] is None


def test_verify_api_descriptor_includes_content_type():
    """The descriptor persists the captured content-type (no enforcement here)."""
    got = verify_api("https://api.ayahealthcare.com/AyaHealthcareWeb/job/search",
                     _fetch_ok(AYA_SEARCH, content_type="application/json"))
    assert got is not None
    assert got["content_type"] == "application/json"
    assert set(got) == {"url", "sample_params", "count", "items_per_page",
                        "sample_keys", "content_type"}


def test_verify_api_descriptor_content_type_defaults_to_none():
    """A fetch_fn that predates the capture (no content_type key) yields None."""
    got = verify_api("https://api.ayahealthcare.com/AyaHealthcareWeb/job/search",
                     _fetch_ok(AYA_SEARCH))
    assert got is not None
    assert got["content_type"] is None


if __name__ == "__main__":
    # ad-hoc runner: call each test, print pass/fail
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{passed}/{len(fns)} passed")
    raise SystemExit(0 if passed == len(fns) else 1)
