"""Job-12 fix plan S5 — validate_coverage api_endpoint bypass tightening.

The bypass (SPA-over-API jobs: field mapping happens generically at scrape
time from the API, so a low single-detail-page coverage is a false negative)
used to fire on ANY api_endpoint URL — including poison descriptors
(ketchcdn consent config, useinsider personalization) that would never
deliver the fields. Now the loose rule only:

    data_source == "api"  AND  items_per_page > 0 (int, bool-guarded)

Deliberately NOT the strict count predicate — amn-class descriptors report
no total (count null) and the bypass exists for exactly that class; a bare
URL with no measured page size no longer buys the bypass.
"""

import importlib

import pytest

# agents.nodes re-exports the function as `validate_coverage`, shadowing the
# submodule attribute — importlib guarantees we get the MODULE for patching.
vc = importlib.import_module("agents.nodes.validate_coverage")

LOW_COVERAGE_ANALYSIS = {
    # 1 of 7 product core fields — ratio ~14% (< MIN_COVERAGE 0.80)
    "fields": {"title": {"method": "jsonld", "selector": "script[type=application/ld+json]"}},
}


def _state(nav):
    return {
        "site_slug": "cov-test",
        "page_type": "product",
        "navigation_analysis": nav,
    }


def _run(monkeypatch, nav, analysis=LOW_COVERAGE_ANALYSIS):
    monkeypatch.setattr(vc, "_load_product_analysis", lambda slug: analysis)
    return vc.validate_coverage(_state(nav))


def _api_nav(data_source, ipp, url="https://t.com/api/search"):
    ep = {"url": url}
    if ipp is not ...:
        ep["items_per_page"] = ipp
    nav = {"api_endpoint": ep}
    if data_source is not ...:
        nav["data_source"] = data_source
    return nav


def test_bypass_fires_for_full_loose_rule(monkeypatch):
    """data_source=='api' + measured ipp>0 → bypass kept (amn/aya class)."""
    cmd = _run(monkeypatch, _api_nav("api", 20))
    assert cmd.goto == "scraper_analyzer"
    assert "interrupt_reason" not in (cmd.update or {})


def test_bypass_fires_without_count_strict_predicate(monkeypatch):
    """count:null must NOT disarm the bypass — the loose rule is deliberate
    (amn-class fresh descriptors report no total)."""
    nav = _api_nav("api", 20)
    nav["api_endpoint"]["count"] = None
    cmd = _run(monkeypatch, nav)
    assert cmd.goto == "scraper_analyzer"


@pytest.mark.parametrize(
    "nav",
    [
        # Bare URL — the OLD behavior bought the bypass on this alone
        # (priceline-class fresh runs; the stale-amn descriptor shape).
        _api_nav(..., ...),
        _api_nav(None, 20),
        _api_nav("browser_llm", 20),
        _api_nav("api", None),
        _api_nav("api", 0),
        _api_nav("api", -1),
        # bool is an int subclass — True is not a measured page size
        _api_nav("api", True),
        # No navigation_analysis at all
        {},
    ],
    ids=[
        "bare_url_only",
        "no_data_source",
        "non_api_data_source",
        "ipp_null",
        "ipp_zero",
        "ipp_negative",
        "ipp_bool",
        "no_nav_analysis",
    ],
)
def test_gate_arms_when_loose_rule_not_met(monkeypatch, nav):
    """Every not-quite-evidence descriptor falls through to the coverage gate:
    low coverage interrupts for a human instead of silently proceeding."""
    cmd = _run(monkeypatch, nav)
    assert cmd.goto == "human_approval"
    assert (cmd.update or {}).get("interrupt_reason") == "low_coverage"


def test_high_coverage_still_passes_without_any_api(monkeypatch):
    """Regression guard on the untouched path: full field coverage proceeds
    (no interrupt) regardless of navigation_analysis."""
    full = {
        "fields": {
            k: {"method": "css", "selector": f".{k}"}
            for k in ("title", "price", "availability", "original_price", "currency", "url", "src_url")
        }
    }
    cmd = _run(monkeypatch, {}, analysis=full)
    assert cmd.goto == "scraper_analyzer"
    assert "interrupt_reason" not in (cmd.update or {})
