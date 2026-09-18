"""[wave-19 T1.6] Same-domain assertion on browser_traverse results (323/D1).

Job 323 (theiconic, driven concurrently with 324/myhouse on the shared
Chrome): the navigator handed back MYHOUSE pages as theiconic's working_url —
NAV-SUMMARY recorded working_url=myhouse/… — and the pipeline built a nav
analysis (and measured tiers) against the WRONG SITE. F17 blanks the artifact
AFTER the fact; by then discovery is aimed at the wrong domain.

Walls:
1. ``_sanitize_nav_domains`` also blanks a cross-domain ``api_endpoint.url``
   (the one identity-bearing field F17 missed);
2. ``_nav_result_contamination`` asserts the traversal RESULT itself
   (goal_url / api url / item-link majority) belongs to the job's registrable
   domain BEFORE the analysis is built;
3. the wrapper reacts: contaminated → ONE forced-homepage re-traverse;
   still contaminated → honest fail to cleanup. Never build a nav analysis
   for the wrong site.
"""
from __future__ import annotations

import os
import re
import sys
import types
import unittest.mock as mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    from types import SimpleNamespace as _NS
except Exception:  # pragma: no cover
    _NS = None

_graph_src = open(os.path.join(ROOT, "webapp", "agents", "graph.py")).read()

# ── source-extract the sanitizer + the new contamination helper ──────────────


def _extract(fn_name: str) -> str:
    m = re.search(rf"^def {fn_name}\(.*?(?=^def |\Z)", _graph_src, re.S | re.M)
    assert m, f"{fn_name} not found in graph.py"
    return m.group(0)


_ns: dict = {"logger": mock.MagicMock(), "__builtins__": __builtins__}
try:
    from experimental.nav_traversal.traversal import _registrable  # noqa: F401
except Exception:
    _trav = types.ModuleType("experimental.nav_traversal.traversal")
    _trav._registrable = lambda u: (
        u.split("//")[-1].split("/")[0].removeprefix("www.").rsplit(".", 2)[-2]
        if "//" in u
        else ""
    )
    sys.modules.setdefault("experimental.nav_traversal.traversal", _trav)
exec(compile(_extract("_sanitize_nav_domains"), "g:_sanitize_nav_domains", "exec"), _ns)
_sanitize_nav_domains = _ns["_sanitize_nav_domains"]

NAV_JOB_URL = "https://www.theiconic.com.au/"
POISON_URL = "https://www.myhouse.com.au/collections/sale-clearance"


def _result(goal_url="", api=None, item_links=None):
    return _NS(goal_url=goal_url, api=api or {}, item_links=item_links or [])


# ── wall 1: the sanitizer covers api_endpoint ────────────────────────────────


class TestSanitizerCoversApiEndpoint:
    def test_cross_domain_api_url_blanked(self):
        a = {"api_endpoint": {"url": "https://api.myhouse.com.au/search", "count": 40}}
        out = _sanitize_nav_domains(a, NAV_JOB_URL)
        assert out["api_endpoint"]["url"] == "", "cross-domain api_endpoint.url must be blanked"
        # the measured evidence survives; only the poisoned URL goes
        assert out["api_endpoint"]["count"] == 40

    def test_same_domain_api_url_kept(self):
        a = {"api_endpoint": {"url": "https://api.theiconic.com.au/search"}}
        out = _sanitize_nav_domains(a, NAV_JOB_URL)
        assert out["api_endpoint"]["url"] == "https://api.theiconic.com.au/search"

    def test_missing_api_untouched(self):
        out = _sanitize_nav_domains({"discovery": {}}, NAV_JOB_URL)
        assert out.get("api_endpoint", {}) == {}


# ── wall 2: the result-level contamination assert ────────────────────────────


class TestNavResultContamination:
    def _fn(self):
        exec(compile(_extract("_nav_result_contamination"), "g:_nav_result_contamination", "exec"), _ns)
        return _ns["_nav_result_contamination"]

    def test_clean_result_passes(self):
        r = _result(
            goal_url="https://www.theiconic.com.au/shop/women",
            api={"url": "https://api.theiconic.com.au/search"},
            item_links=["https://www.theiconic.com.au/p/a"],
        )
        assert self._fn()(r, NAV_JOB_URL) == []

    def test_poisoned_goal_url_flagged(self):
        bad = self._fn()(_result(goal_url=POISON_URL), NAV_JOB_URL)
        assert len(bad) == 1 and "goal_url" in bad[0]

    def test_poisoned_api_url_flagged(self):
        """[wave-26 contract refresh, job 355] an off-domain api.url vetoes
        only when it CLAIMS data (count/sample_keys) — a bare widget
        endpoint is no longer identity-bearing. The trap shape keeps the
        veto; see tests/test_wave26_guard_thirdparty_api.py."""
        bad = self._fn()(
            _result(
                goal_url="https://www.theiconic.com.au/x",
                api={"url": "https://api.myhouse.com.au/s", "count": 40},
            ),
            NAV_JOB_URL,
        )
        assert len(bad) == 1 and "api" in bad[0]

    def test_majority_off_domain_links_flagged(self):
        links = [POISON_URL] * 3 + ["https://www.theiconic.com.au/p/a"]
        bad = self._fn()(_result(goal_url="https://www.theiconic.com.au/x", item_links=links), NAV_JOB_URL)
        assert len(bad) == 1 and "item_links" in bad[0]

    def test_minority_off_domain_links_not_flagged(self):
        """One off-domain affiliate/ad link among real items is noise, not
        contamination — F17 still drops the examples downstream."""
        links = ["https://www.theiconic.com.au/p/a"] * 5 + [POISON_URL]
        assert self._fn()(_result(goal_url="https://www.theiconic.com.au/x", item_links=links), NAV_JOB_URL) == []


# ── wall 3: the wrapper re-traverses once, then fails honestly ───────────────


class TestWrapperWiring:
    def _wrapper_src(self):
        m = re.search(
            r"^def _invoke_navigation_traverse\(.*?(?=^def |\Z)", _graph_src, re.S | re.M
        )
        assert m, "_invoke_navigation_traverse not found"
        return m.group(0)

    def test_contamination_assert_precedes_analysis_build(self):
        src = self._wrapper_src()
        i_check = src.index("_nav_result_contamination(result, url)")
        i_build = src.index('"discovery_method": "browser_traverse"')
        assert i_check < i_build, "contamination assert must run BEFORE the analysis build"

    def test_one_forced_homepage_retraverse(self):
        src = self._wrapper_src()
        # [wave-38 T4] the recovery walk runs via _retraverse_locked — locked,
        # bounded-wait — instead of a bare browser_traverse call.
        assert "_retraverse_locked(" in src, (
            "the re-traverse must run under a freshly-acquired traversal lock"
        )
        m = re.search(
            r"^def _retraverse_locked\(.*?(?=^def |\Z)", _graph_src, re.S | re.M
        )
        assert m, "_retraverse_locked not found"
        assert "trust_start_as_listing=False" in m.group(0), (
            "the re-traverse must restart from the homepage, not trust the poisoned path"
        )

    def test_still_contaminated_fails_to_cleanup(self):
        src = self._wrapper_src()
        assert 'goto="cleanup"' in src
        assert "cross-domain" in src

    def test_clean_retraverse_replaces_result(self):
        src = self._wrapper_src()
        assert re.search(r"result\s*=\s*_retry", src), (
            "a clean re-traverse must REPLACE the poisoned result"
        )


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
