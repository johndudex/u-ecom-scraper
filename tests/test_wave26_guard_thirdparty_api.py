"""[wave-26 hotfix #3, job 355] Third-party widget APIs are not contamination.

forevernew's traversal came back clean (goal + item links on-domain) EXCEPT
for one captured XHR: ``consumer-v2.truefitcorp.com/api/store/fnw/ui-configs``
— Forever New's embedded TrueFit size-widget. The wave-19 guard vetoes ANY
off-domain api.url, so a legitimate third-party widget killed the job with
"Navigator returned cross-domain traversal results twice".

Contract evolution (documented, deliberate): an off-domain ``api.url`` is a
contamination violation ONLY when the capture CLAIMS product data — count /
items_per_page / sample_keys — i.e. when it could become the EXTRACTION
SOURCE (karenmillen's verbolia brand-protection trap serves exactly that
shape, and stays vetoed). A bare widget/config endpoint carries none of
that evidence, and the strategy gate's shape check would reject it as an
extraction source anyway.

The other walls are unchanged: goal_url off-domain vetoes; majority
off-domain item_links veto; _sanitize_nav_domains still blanks any
off-domain api_endpoint.url that reaches the analysis.
"""
from __future__ import annotations

import os
import re
import sys
import types
import unittest.mock as mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from types import SimpleNamespace as _NS

_graph_src = open(os.path.join(ROOT, "webapp", "agents", "graph.py")).read()


def _extract(fn_name: str) -> str:
    m = re.search(rf"^def {fn_name}\(.*?(?=^def |\Z)", _graph_src, re.S | re.M)
    assert m, f"{fn_name} not found in graph.py"
    return m.group(0)


_ns: dict = {"logger": mock.MagicMock(), "__builtins__": __builtins__}
try:
    from experimental.nav_traversal.traversal import _registrable
except Exception:
    _trav = types.ModuleType("experimental.nav_traversal.traversal")
    _trav._registrable = lambda u: (
        u.split("//")[-1].split("/")[0].removeprefix("www.").rsplit(".", 2)[-2]
        if "//" in u
        else ""
    )
    sys.modules.setdefault("experimental.nav_traversal.traversal", _trav)

FNW = "https://www.forevernew.com.au/"
TRUEFIT = "https://consumer-v2.truefitcorp.com/api/store/fnw/ui-configs"
POISON = "https://karenmillen-engb.backend.verbolia.com/shop/api/gial"


def _result(goal_url="", api=None, item_links=None):
    return _NS(goal_url=goal_url, api=api or {}, item_links=item_links or [])


def _fn():
    # the helper does its own local `from experimental... import _registrable`
    exec(compile(_extract("_nav_result_contamination"), "g:_c", "exec"), _ns)
    return _ns["_nav_result_contamination"]


class TestThirdPartyWidgetNotContamination:
    def test_truefit_widget_endpoint_passes(self):
        """The exact job-355 shape: clean walk + a bare config XHR."""
        r = _result(
            goal_url="https://www.forevernew.com.au/c/womens-dresses",
            api={"url": TRUEFIT, "count": None, "sample_keys": []},
            item_links=[
                "https://www.forevernew.com.au/amy-knit-midi-dress",
                "https://www.forevernew.com.au/lucy-midi-dress",
                "https://www.forevernew.com.au/bella-dress",
            ],
        )
        assert _fn()(r, FNW) == [], (
            "a third-party widget XHR with NO data evidence must not veto a "
            "clean walk (job 355 false positive)"
        )

    def test_empty_api_dict_passes(self):
        r = _result(goal_url="https://www.forevernew.com.au/c/dresses")
        assert _fn()(r, FNW) == []


class TestDataClaimingOffDomainApiStillVetoed:
    def test_count_claiming_api_flagged(self):
        """The verbolia trap shape: off-domain AND claims product records."""
        r = _result(
            goal_url="https://www.karenmillen.com/categories/womens-plus-size",
            api={"url": POISON, "count": 25, "sample_keys": ["title", "price"]},
            item_links=["https://www.karenmillen.com/p/a"],
        )
        bad = _fn()(r, "https://www.karenmillen.com/")
        assert len(bad) == 1 and "api" in bad[0]

    def test_sample_keys_claiming_api_flagged(self):
        r = _result(
            goal_url="https://www.karenmillen.com/c/x",
            api={"url": POISON, "sample_keys": ["title"]},
        )
        assert _fn()(r, "https://www.karenmillen.com/")

    def test_items_per_page_claiming_api_flagged(self):
        r = _result(
            goal_url="https://www.karenmillen.com/c/x",
            api={"url": POISON, "items_per_page": 24},
        )
        assert _fn()(r, "https://www.karenmillen.com/")


class TestUnchangedWalls:
    def test_goal_url_off_domain_still_vetoes(self):
        bad = _fn()(
            _result(goal_url="https://www.karenmillen.com/categories/womens-plus-size"),
            "https://www.nike.in/",
        )
        assert len(bad) == 1 and "goal_url" in bad[0]

    def test_majority_off_domain_links_still_veto(self):
        links = ["https://www.karenmillen.com/p/x"] + [
            "https://www.forevernew.com.au/p/a",
            "https://www.forevernew.com.au/p/b",
            "https://www.forevernew.com.au/p/c",
        ]
        bad = _fn()(
            _result(goal_url="https://www.karenmillen.com/c/x", item_links=links),
            "https://www.karenmillen.com/",
        )
        assert len(bad) == 1 and "item_links" in bad[0]

    def test_on_domain_api_still_clean(self):
        r = _result(
            goal_url="https://www.forevernew.com.au/c/dresses",
            api={"url": "https://www.forevernew.com.au/api/products", "count": 12},
        )
        assert _fn()(r, FNW) == []


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
